"""Pilot: draft review + explicit owner-approved sends. NO automatic client sends."""
import asyncio
import datetime as dt
import json
import html
import logging
import os
import re
import time
from pathlib import Path

from .clients import OpenAI, ShopReader, Telegram
from .privacy import scrub, sensitive
from .storage import Store
from .research import lookup, attach_evidence, outgoing_text, unavailable, failure_details
from .response_policy import continuing_reply, missing_shop_reply


def buttons(rows):
    def item(value):
        if isinstance(value,dict):
            return dict(value)
        label,data=value
        return dict(text=label,callback_data=data,
                    **({'style':'success'} if data.startswith('dialog:') else {}))
    return {'inline_keyboard':[[item(value) for value in row] for row in rows]}


class Service:
    def __init__(self,store,tg,ai,shop,owner,allowed,daily_limit=100):
        self.store,self.tg,self.ai,self.shop=store,tg,ai,shop
        self.owner,self.allowed,self.daily_limit=owner,set(allowed),daily_limit
        self.bot_id=None
        self.daily_cleanup=0
        self.active_chats=set()
        self.approval_tasks={}
        self.owner_delivery_lock=asyncio.Lock()
        self.owner_interval=0
        self.owner_last_send=0

    def chat_controls(self,c):
        return [('Возобновить помощника',f'chat:draft:{c["id"]}')] if c['mode']=='off' else [
            ('Отвечаю сам',f'chat:off:{c["id"]}')]

    def chat_identity(self,cid):
        """Plain-text context for every per-client owner confirmation."""
        c=self.store.chat(cid)
        if not c:
            return f'Чат №{cid} · клиент не найден'
        profile=self.store.profile(cid)
        username=''.join(str(profile['username'] or '').split())[:64] if profile else ''
        name=' '.join(str(profile['name'] or '').split())[:100] if profile else ''
        label='@'+username.lstrip('@') if username else name or 'Клиент без ника'
        return f'Чат №{cid} · {label} · ID {c["uid"]}'

    async def owner_message(self,text,reply_markup=None,parse_mode=None):
        # Cards have complete HTML tags within paragraphs. Split only between
        # paragraphs; never truncate an answer the owner is being asked to approve.
        parts=[];current=''
        def size(value):
            visible=html.unescape(re.sub(r'<[^>]*>','',value)) if parse_mode=='HTML' else value
            return len(visible.encode('utf-16-le'))//2
        for paragraph in text.split('\n\n'):
            candidate=current+'\n\n'+paragraph if current else paragraph
            if current and size(candidate)>3900:
                parts.append(current);current=paragraph
            else:
                current=candidate
        if current:
            parts.append(current)
        if any(size(part)>4096 for part in parts):
            raise ValueError('Owner message paragraph exceeds Telegram limit')
        data={'chat_id':self.owner}
        if parse_mode:
            data['parse_mode']=parse_mode
        result=None
        async with self.owner_delivery_lock:
            for index,part in enumerate(parts):
                delay=self.owner_interval-(time.monotonic()-self.owner_last_send)
                if delay>0:
                    await asyncio.sleep(delay)
                payload=dict(data,text=part)
                if reply_markup and index==len(parts)-1:
                    payload['reply_markup']=reply_markup
                self.owner_last_send=time.monotonic()
                result=await self.tg.call('sendMessage',**payload)
        return result

    async def load_connection(self,ident):
        value=await self.tg.call('getBusinessConnection',business_connection_id=ident)
        self.record_connection(value)

    def record_connection(self,value):
        if value.get('user',{}).get('id') != self.owner:
            return
        self.store.connection(value['id'],bool(value.get('is_enabled')),
                              bool(value.get('rights',{}).get('can_reply',value.get('can_reply',False))))

    async def ingest(self,update):
        if 'business_connection' in update:
            self.record_connection(update['business_connection'])
            return
        if 'callback_query' in update:
            q=update['callback_query']
            if q.get('from',{}).get('id')==self.owner and q.get('message',{}).get('chat',{}).get('id')==self.owner:
                try:
                    await self.command(q.get('data',''),admin_mid=q.get('message',{}).get('message_id'))
                except (ValueError,IndexError):
                    await self.owner_message('Некорректная команда. Используйте /status.')
            # Expired callback acknowledgement must not block the update queue
            # or replay an already handled command after a restart.
            try:
                await self.tg.call('answerCallbackQuery',callback_query_id=q['id'])
            except Exception:
                logging.warning('Callback acknowledgement unavailable')
            return
        if 'message' in update:
            m=update['message']
            if (m.get('text','').strip()=='/id' and m.get('chat',{}).get('type')=='private'
                    and m.get('from',{}).get('id')==m.get('chat',{}).get('id')):
                await self.tg.call('sendMessage',chat_id=m['chat']['id'],text=f'Ваш Telegram ID: {m["chat"]["id"]}')
                return
            if m.get('from',{}).get('id')==self.owner and m.get('chat',{}).get('id')==self.owner:
                try:
                    await self.command(m.get('text',''))
                except (ValueError,IndexError):
                    await self.owner_message('Некорректная команда. Используйте /status.')
            return
        if 'deleted_business_messages' in update:
            d=update['deleted_business_messages']
            if d['chat']['id'] in self.allowed:
                self.store.delete_messages(d['business_connection_id'],d['chat']['id'],d['message_ids'])
            return
        m=update.get('business_message') or update.get('edited_business_message')
        if not m or m.get('chat',{}).get('type')!='private' or m['chat']['id'] not in self.allowed:
            return
        ident=m.get('business_connection_id')
        if not ident:
            return
        if not self.store.db.execute('SELECT 1 FROM connections WHERE id=?',(ident,)).fetchone():
            await self.load_connection(ident)
        con=self.store.db.execute('SELECT * FROM connections WHERE id=?',(ident,)).fetchone()
        if not con or not con['enabled']:
            return
        # Bots' outgoing messages must not masquerade as manual owner replies.
        if m.get('sender_business_bot'):
            return
        sender=m.get('from',{}).get('id')
        uid=m['chat']['id']
        if sender not in (uid,self.owner):
            return
        role='owner' if sender==self.owner else 'client'
        raw=m.get('text') or m.get('caption') or ''
        secret=sensitive(raw)
        media={'kind':'none'}
        if m.get('photo'):
            media=dict(m['photo'][-1],kind='photo')
        elif m.get('voice'):
            media=dict(m['voice'],kind='voice')
        elif m.get('document') and m['document'].get('mime_type') in ('image/jpeg','image/png','image/webp'):
            media={k:v for k,v in m['document'].items() if k in ('file_id','file_size')}
            media['kind']='photo'
            media['delivery_kind']='document'
        elif any(m.get(key) for key in ('document','video','animation','audio','video_note','sticker','contact','location','venue','poll')) or not raw:
            media={'kind':'unsupported'}
        if secret:
            media={'kind':'sensitive'}
        # Never persist supplied credentials, or forward raw content to the admin bot.
        text='[Сообщение скрыто: возможны данные доступа; нужна ручная проверка]' if secret else scrub(raw)
        cid=self.store.ingest(ident,uid,m['message_id'],role,text,media,m['date'],
                              edited='edited_business_message' in update)
        if role=='client':
            person=m.get('from',{})
            name=' '.join(str(person.get(k) or '') for k in ('first_name','last_name')).strip()
            self.store.save_profile(cid,str(person.get('username') or '')[:64],name[:160])

    async def admin_panel(self,text,rows,admin_mid=None,parse_mode=None):
        """Bounded owner-only pages; callback navigation edits the existing panel."""
        markup=buttons(rows)
        if admin_mid is None:
            return await self.owner_message(text,markup,parse_mode=parse_mode)
        try:
            return await self.tg.call('editMessageText',chat_id=self.owner,
                message_id=admin_mid,text=text,reply_markup=markup,
                **({'parse_mode':parse_mode} if parse_mode else {}))
        except Exception:
            # A deleted panel or an unchanged-page error must not replay actions.
            logging.warning('Admin panel update unavailable')
            return None

    def chat_label(self,cid,uid):
        profile=self.store.profile(cid)
        label=('@'+profile['username']) if profile and profile['username'] else (
            profile['name'] if profile and profile['name'] else str(uid))
        return ' '.join(label.split())[:60]

    async def list_chats(self,page=0,admin_mid=None):
        count=self.store.db.execute('SELECT count(*) FROM chats').fetchone()[0]
        pages=max(1,(count+7)//8);page=min(max(0,page),pages-1)
        chats=self.store.db.execute('SELECT * FROM chats ORDER BY inbound DESC,id DESC LIMIT 8 OFFSET ?',
                                   (page*8,)).fetchall()
        lines=[f'💬 Чаты · {count} · страница {page+1}/{pages}',
               'Глобальный режим: '+self.store.get('mode'),'']
        rows=[]
        for c in chats:
            pending=self.store.db.execute("SELECT count(*) FROM drafts WHERE chat=? AND state='ready'",(c['id'],)).fetchone()[0]
            queued=self.store.db.execute("SELECT count(*) FROM messages WHERE chat=? AND state IN ('queued','working')",(c['id'],)).fetchone()[0]
            label=self.chat_label(c['id'],c['uid'])
            mode='подготовка' if c['mode']=='draft' else 'пауза'
            if c['uid'] not in self.allowed:
                mode='не разрешён настройками'
            lines.append(f'№{c["id"]} · {label} · {mode}\nЧерновиков: {pending} · в обработке/очереди: {queued}')
            rows.append([(f'№{c["id"]} · {label}',f'view:{c["id"]}')])
        if not chats:
            lines.append('Пока нет сообщений из разрешённых чатов.')
        nav=[]
        if page:nav.append(('← Назад',f'chats:{page-1}'))
        if page+1<pages:nav.append(('Далее →',f'chats:{page+1}'))
        if nav:rows.append(nav)
        rows.append([('Обновить',f'chats:{page}'),('Ожидают проверки','pending:0')])
        await self.admin_panel('\n\n'.join(lines),rows,admin_mid)

    async def list_pending(self,page=0,admin_mid=None):
        count=self.store.db.execute("SELECT count(*) FROM drafts WHERE state IN ('ready','uncertain')").fetchone()[0]
        pages=max(1,(count+7)//8);page=min(max(0,page),pages-1)
        drafts=self.store.db.execute("""SELECT d.id,d.chat,d.state,d.created,c.uid FROM drafts d JOIN chats c ON c.id=d.chat
            WHERE d.state IN ('ready','uncertain') ORDER BY d.id DESC LIMIT 8 OFFSET ?""",(page*8,)).fetchall()
        lines=[f'📝 Ожидают проверки · {count} · страница {page+1}/{pages}'];rows=[]
        for d in drafts:
            label=self.chat_label(d['chat'],d['uid'])
            state=('исход отправки неизвестен — проверить личку' if d['state']=='uncertain' else
                   'истёк срок отправки — нужна новая подготовка' if time.time()-d['created']>=900 else
                   'готов к проверке')
            lines.append(f'Черновик №{d["id"]} · чат №{d["chat"]} · {label} · ID {d["uid"]}\n{state}')
            rows.append([(f'№{d["id"]} · {label}',f'draft:{d["id"]}')])
        if not drafts:lines.append('Ожидающих черновиков нет.')
        nav=[]
        if page:nav.append(('← Назад',f'pending:{page-1}'))
        if page+1<pages:nav.append(('Далее →',f'pending:{page+1}'))
        if nav:rows.append(nav)
        rows.append([('Обновить',f'pending:{page}'),('Все чаты','chats:0')])
        await self.admin_panel('\n\n'.join(lines),rows,admin_mid)

    async def show_chat(self,cid,admin_mid=None):
        c=self.store.chat(cid)
        if not c:
            await self.admin_panel('Чат не найден.',[[('Все чаты','chats:0')]],admin_mid)
            return
        label=self.chat_label(cid,c['uid'])
        connection=self.store.db.execute('SELECT * FROM connections WHERE id=?',(c['connection'],)).fetchone()
        access=bool(connection and connection['enabled'] and connection['can_reply'])
        queued=self.store.db.execute("SELECT state FROM messages WHERE chat=? AND mid=?",(cid,c['latest'])).fetchone()
        status={'queued':'ожидает обработки','working':'готовится ответ','done':'обработано',
                'failed':'ошибка обработки','stale':'отменено/устарело','stored':'сохранено, не в очереди'}
        batching=''
        waiting=bool(queued and queued[0]=='queued')
        if waiting:
            count=self.store.db.execute(
                "SELECT count(*) FROM messages WHERE chat=? AND role='client' AND mid>=? AND mid<=?",
                (cid,c['batch_first'],c['latest'])).fetchone()[0]
            due=dt.datetime.fromtimestamp(c['batch_due'],dt.timezone(dt.timedelta(hours=5))).strftime('%H:%M:%S')
            batching=(f'Собираем обращение · сообщений: {count} · подготовка в {due} (UTC+5)\n'
                      if c['batch_due']>time.time() else
                      f'Обращение собрано · сообщений: {count} · ожидает свободного обработчика\n')
        text=(f'👤 {label}\nЧат №{cid} · Telegram ID {c["uid"]}\n\n'
              f'Чат: {"подготовка ответов" if c["mode"]=="draft" else "пауза"}\n'
              f'Глобальный режим: {self.store.get("mode")}\n'
              f'Разрешён настройками: {"да" if c["uid"] in self.allowed else "нет"}\n'
              f'Подключение и право ответа: {"есть" if access else "нет"}\n'
              f'Последнее обращение: {status.get(queued[0],queued[0]) if queued else "нет сохранённого сообщения"}\n\n'
              +batching+'Отправка только после вашего одобрения. Перед повтором проверьте, не ответили ли уже клиенту.')
        rows=[self.chat_controls(c)]
        if c['uid'] in self.allowed:
            if waiting and c['mode']=='draft' and self.store.get('mode')=='draft' and access and cid not in self.active_chats:
                rows.append([('Подготовить сейчас',f'prepare:{cid}')])
            else:
                rows.append([('Повторить подготовку',f'retry:{cid}')])
        rows.append([('Весь диалог',f'dialog:{cid}')])
        last=self.store.db.execute('SELECT id FROM drafts WHERE chat=? ORDER BY id DESC LIMIT 1',(cid,)).fetchone()
        if last:rows.append([('Последний черновик',f'draft:{last[0]}')])
        rows.append([('Все чаты','chats:0'),('Ожидают проверки','pending:0')])
        await self.admin_panel(text,rows,admin_mid)

    async def command(self,text,admin_mid=None):
        parts=text.strip().split()
        if not parts:
            return
        cmd=parts[0].split('@')[0]
        if cmd in ('/stop','global:off'):
            self.store.switch('off')
            await self.owner_message('Помощник остановлен. Черновики отменены. Уже отправленный Telegram запрос остановка не отзывает.')
        elif cmd in ('/draft','global:draft'):
            self.store.switch('draft')
            await self.owner_message('Режим черновиков включён для новых сообщений в разрешённых чатах. Клиентам автоматически ничего не отправляется.')
        elif cmd in ('/start','/status'):
            n=self.store.db.execute("SELECT count(*) FROM drafts WHERE state='ready'").fetchone()[0]
            await self.owner_message(
                f'Помощник: {self.store.get("mode")}\nТестовых клиентов: {len(self.allowed)}\nЧерновиков: {n}\n'
                '/chats — чаты и управление\n/pending — черновики\n/retry НОМЕР_ЧАТА — повторить подготовку\n'
                '/stop — остановить всем\n/draft — включить подготовку\n/shop_status — проверить подключение магазина\n'
                'AUTO в этой сборке отсутствует. Вложения тестовых чатов передаются OpenAI для анализа.',
                buttons([[('Остановить всем','global:off'),('Только черновики','global:draft')]]))
        elif cmd=='/shop_status':
            facts=await self.shop.facts(self.owner)
            labels={'available':'доступны','truncated':'доступны первые 150 записей',
                    'read_error':'ошибка чтения / прав доступа','not_connected':'не подключены'}
            await self.owner_message('Магазин · проверка доступа только на чтение\n\n'
                +'Каталог: '+labels.get(facts.get('catalog_status'),'статус неизвестен')
                +'\nТарифов с подтверждённой базовой ценой: '+str(len(facts.get('catalog',[])))
                +'\nЗаказы: '+labels.get(facts.get('orders_status'),'статус неизвестен')
                +'\n\nЭто не весь ассортимент: динамические товары, описания, остатки и способ активации ещё не подтверждаются. Проверка не вызывает модель.')
        elif cmd=='/chats' or cmd.startswith('chats:'):
            await self.list_chats(int(cmd.split(':')[1]) if ':' in cmd else 0,admin_mid)
        elif cmd.startswith('view:'):
            await self.show_chat(int(cmd.split(':')[1]),admin_mid)
        elif cmd.startswith('draft:'):
            did=int(cmd.split(':')[1])
            if self.store.db.execute('SELECT 1 FROM drafts WHERE id=?',(did,)).fetchone():
                await self.show_draft(did)
            else:
                await self.owner_message('Черновик не найден или удалён по сроку хранения. Используйте /pending.')
        elif cmd.startswith('prepare:'):
            cid=int(cmd.split(':')[1]);c=self.store.chat(cid)
            if c and c['uid'] in self.allowed and cid not in self.active_chats:
                self.store.prepare_now(cid,time.time())
            await self.show_chat(cid,admin_mid)
        elif cmd.startswith('retry:'):
            await self.command('/retry '+str(int(cmd.split(':')[1])))
        elif cmd.startswith('dialog:'):
            from .dialog_view import show_dialog
            await show_dialog(self,int(cmd.split(':')[1]))
        elif cmd.startswith('dlg:'):
            from .dialog_view import show_dialog
            _,cid,page,mid,part=cmd.split(':')
            await show_dialog(self,int(cid),int(page),int(mid) or None,int(part),admin_mid)
        elif cmd.startswith('chat:'):
            _,mode,raw=cmd.split(':')
            cid=int(raw)
            if not self.store.chat(cid):
                return
            changed=self.store.switch(mode,cid)
            await self.owner_message(self.chat_identity(cid)+'\n'+(
                'помощник уже включён. Готовые черновики сохранены.' if not changed else
                'подготовка ответов включена для новых сообщений.' if mode=='draft' else
                'помощник на паузе. Ожидающие черновики отменены.'),
                buttons([self.chat_controls(self.store.chat(cid))]))
        elif cmd=='/pending' or cmd.startswith('pending:'):
            await self.list_pending(int(cmd.split(':')[1]) if ':' in cmd else 0,admin_mid)
        elif cmd.startswith('send:'):
            if not self.schedule_approval(int(cmd.split(':')[1])):
                await self.owner_message('Уже отправляются четыре ответа. Этот черновик сохранён; повторите нажатие после завершения отправки.')
        elif cmd.startswith('sendpause:'):
            if not self.schedule_approval(int(cmd.split(':')[1]),pause_after=True):
                await self.owner_message('Уже отправляются четыре ответа. Этот черновик сохранён; повторите нажатие после завершения отправки.')
        elif cmd=='/retry' and len(parts)==2 and parts[1].isdigit():
            cid=int(parts[1]);c=self.store.chat(cid)
            if not c or c['mode']!='draft' or self.store.get('mode')!='draft':
                await self.owner_message(self.chat_identity(cid)+'\nСначала включите черновики для этого чата и глобально.')
                return
            m=self.store.db.execute("SELECT * FROM messages WHERE chat=? AND mid=? AND role='client'",(cid,c['latest'])).fetchone()
            if not m:
                await self.owner_message(self.chat_identity(cid)+'\nПоследнее сообщение уже недоступно. Нужно новое сообщение клиента.')
                return
            if m['state'] in ('queued','working') or cid in self.active_chats:
                await self.owner_message(self.chat_identity(cid)+'\nОтвет уже в очереди или готовится. Повторный запрос не запущен.')
                return
            previous=self.store.db.execute("SELECT state FROM drafts WHERE chat=? AND mid=? AND state IN ('sent','sending','uncertain') LIMIT 1",(cid,c['latest'])).fetchone()
            if previous:
                await self.owner_message(self.chat_identity(cid)+'\nНа это сообщение уже отправлялся ответ либо исход отправки неизвестен. Проверьте личку; повтор автоматически не создаю. Для продолжения нужно новое сообщение клиента.')
                return
            if m:
                self.store.ingest(c['connection'],c['uid'],m['mid'],'client',m['text'],json.loads(m['media']),m['at'],edited=True)
                self.store.prepare_now(cid,time.time())
                await self.owner_message(self.chat_identity(cid)+'\nПодготовка поставлена в очередь заново. Это может расходовать API-баланс.')

    async def show_draft(self,did):
        d=self.store.db.execute('SELECT * FROM drafts WHERE id=?',(did,)).fetchone()
        if not d:
            return
        c=self.store.chat(d['chat'])
        rows=[self.chat_controls(c)]
        rows.append([('Весь диалог',f'dialog:{c["id"]}')])
        expired=d['state']=='ready' and time.time()-d['created']>=900
        if d['state']=='ready' and not expired:
            rows.insert(0,[('Отправить и продолжить',f'send:{did}')])
            rows.insert(1,[('Отправить и взять чат себе',f'sendpause:{did}')])
        elif expired:
            rows.insert(0,[('Подготовить свежий ответ',f'retry:{c["id"]}')])
        profile=self.store.profile(c['id'])
        label=('@'+profile['username']) if profile and profile['username'] else (
            profile['name'] if profile and profile['name'] else 'Клиент')
        client=f'<a href="tg://user?id={int(c["uid"])}">{html.escape(label)}</a>'
        states={'ready':'Готов к проверке','sent':'Отправлен','stale':'Отменён — данные изменились',
                'sending':'Отправляется','uncertain':'Результат отправки неизвестен'}
        state=states.get(d['state'],'Требует проверки')
        if expired:
            state='Истёк срок проверки (15 минут) — подготовьте свежий ответ'
        evidence=self.store.evidence(did)
        if evidence['status'] in ('checked','insufficient'):
            checked=dt.datetime.fromtimestamp(evidence['checked_at'],dt.timezone(dt.timedelta(hours=5)))
            label=('Источники проверены' if evidence['status']=='checked'
                   else 'Поиск выполнен, сведений недостаточно для ответа')
            research_note=f'🌐 <b>{label}:</b> {checked:%d.%m.%Y %H:%M} (UTC+5)\n'
            for source in evidence['sources']:
                research_note+=f'<a href="{html.escape(source["url"],quote=True)}">Официальный источник [{source["id"]}]</a>\n'
            research_note+='<i>Проверьте соответствие источников ответу. Это не прайс магазина.</i>\n\n'
            if evidence.get('citation_mode')=='bibliography':
                research_note+='<i>Модель не привязала все источники к фразам. Перед отправкой проверьте, подтверждают ли они ответ. Ссылки видны только вам.</i>\n\n'
        else:
            research_note={'unavailable':'🌐 <b>Интернет:</b> свежие сведения не подтверждены; нужна ручная проверка.\n\n',
                           'not_recorded':'🌐 <i>Для старого черновика сведения о проверке не сохранены.</i>\n\n'}.get(
                               evidence['status'],'')
            if evidence['status']=='unavailable' and evidence.get('diagnostic'):
                research_note+=f'🛠 <b>Диагностика для владельца</b>\n{html.escape(evidence["diagnostic"][:600])}\n\n'
        note=('Проверьте личку перед дальнейшими действиями. Автоповтора нет.'
              if d['state']=='uncertain' else 'Клиент получит только текст ответа после вашего одобрения.'
              if d['state']=='ready' else 'Эта карточка показывает сохранённый черновик.')
        text=(f'📝 <b>Черновик ответа №{did}</b>\n\n'
              f'👤 <b>Клиент:</b> {client}\n'
              f'<b>Telegram ID:</b> <code>{c["uid"]}</code> · <b>Чат:</b> №{c["id"]}\n\n'
              f'<b>Статус:</b> {state}\n'
              f'<b>Участие Александра:</b> {"нужно" if d["handoff"] else "не требуется по оценке модели"}\n\n'
              f'🔎 <b>Разбор обращения</b>\n<i>Вывод модели — проверьте перед отправкой.</i>\n'
              f'{html.escape(d["summary"][:700])}\n\n'
              f'💬 <b>Ответ клиенту</b>\n{html.escape(d["text"][:1800])}\n\n'
              f'{research_note}'
              f'<i>{note}</i>')
        sent=await self.owner_message(text,buttons(rows),parse_mode='HTML')
        self.store.db.execute('UPDATE drafts SET admin_mid=? WHERE id=?',(sent['message_id'],did))

    def schedule_approval(self,did,pause_after=False):
        if did in self.approval_tasks:
            return True
        if len(self.approval_tasks)>=4:
            return False  # Draft remains ready; the owner can retry later.
        async def run():
            try:
                await self.approve(did,pause_after=pause_after)
            except Exception:
                logging.warning('Approval task failed; inspect pending drafts')
            finally:
                self.approval_tasks.pop(did,None)
        self.approval_tasks[did]=asyncio.create_task(run())
        return True

    async def approve(self,did,pause_after=False):
        d=self.store.db.execute('SELECT * FROM drafts WHERE id=?',(did,)).fetchone()
        if not d:
            return
        c=self.store.chat(d['chat'])
        if c['uid'] not in self.allowed:
            return
        # Recheck current Telegram permissions, then atomically consume draft.
        await self.load_connection(c['connection'])
        reserved=self.store.reserve_send(did,time.time())
        if not reserved:
            await self.owner_message(self.chat_identity(c['id'])+f'\nЧерновик №{did}: ответ отменён/устарел, чат остановлен или нет права отправки. Повторно не отправляю.')
            return
        if pause_after:
            # Pause before yielding to the network so a new incoming message cannot
            # be queued and then silently discarded after this send completes.
            self.store.switch('off',c['id'])
        try:
            text=outgoing_text(reserved['text'],self.store.evidence(did))
            sent=await self.tg.call('sendMessage',chat_id=c['uid'],business_connection_id=c['connection'],text=text)
        except Exception:
            self.store.db.execute("UPDATE drafts SET state='uncertain' WHERE id=?",(did,))
            self.store.switch('off',c['id'])
            await self.owner_message(self.chat_identity(c['id'])+f'\nНеизвестен результат отправки черновика №{did}. Чат поставлен на паузу. Проверьте личку перед возобновлением. Автоматического повтора не будет.',
                                     buttons([self.chat_controls(self.store.chat(c['id']))]))
            return
        with self.store.transaction():
            self.store.db.execute("UPDATE drafts SET state='sent',telegram_mid=? WHERE id=?",(sent['message_id'],did))
            self.store.db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?)',
                (c['id'],sent['message_id'],'assistant',text,'{}',time.time(),'done'))
        current=self.store.chat(c['id'])
        await self.owner_message(self.chat_identity(c['id'])+f'\nЧерновик №{did} отправлен. '+(
            'Чат на паузе; отвечаете вы.' if current['mode']=='off' else
            'Помощник продолжит готовить черновики следующих сообщений.'),
            buttons([self.chat_controls(current)]))

    def charge_call(self):
        day=dt.datetime.now(dt.timezone.utc).date().isoformat()
        if not self.store.reserve_call(day,self.daily_limit):
            raise ValueError('Daily API request limit reached')

    async def generate(self,job):
        from .storage import HISTORY_MESSAGES,HISTORY_CHARACTERS
        turn=self.store.db.execute('''SELECT count(*),COALESCE(sum(length(text)),0) FROM messages
            WHERE chat=? AND role='client' AND mid<=? AND mid>COALESCE(
              (SELECT MAX(mid) FROM messages WHERE chat=? AND role IN ('owner','assistant') AND mid<?),0)''',
            (job['chat'],job['mid'],job['chat'],job['mid'])).fetchone()
        if turn[0]>HISTORY_MESSAGES or turn[1]>HISTORY_CHARACTERS:
            return {'reply':'Спасибо за подробности! Уточню ваше обращение и вернусь с ответом.',
                    'summary':'Обращение превышает безопасный объём контекста. Автоматический анализ не запускался; проверьте весь диалог вручную.',
                    'handoff':True}
        from .media_batch import prepare_media
        image,notice=await prepare_media(self,job)
        if notice:return notice
        if image is None or not self.store.still_current(job):return None
        # Transcripts can make a previously small request exceed context capacity.
        size=self.store.db.execute('''SELECT COALESCE(sum(length(text)),0) FROM messages
            WHERE chat=? AND role='client' AND mid<=? AND mid>COALESCE(
              (SELECT MAX(mid) FROM messages WHERE chat=? AND role IN ('owner','assistant') AND mid<?),0)''',
            (job['chat'],job['mid'],job['chat'],job['mid'])).fetchone()[0]
        if size>HISTORY_CHARACTERS:
            return {'reply':'Уточню детали вашего обращения и вернусь с ответом.',
                    'summary':'После расшифровки обращение превышает объём контекста. Проверьте весь диалог вручную; частичный ответ не подготовлен.',
                    'handoff':True}
        facts=await self.shop.facts(job['uid'])
        facts=dict(facts,context_scope='История содержит только последние 60 сообщений в пределах 24000 символов; более ранние договорённости могут отсутствовать. Не придумывай их.')
        if not self.store.still_current(job):
            return None
        history=self.store.history(job['chat'])
        # One client turn can span many messages. Keep their order and interpret
        # the whole retained turn, not only its final word or question mark.
        current_turn=[]
        for message in reversed(history):
            if message['role'] in ('owner','assistant'):
                break
            if message['role']=='client':
                current_turn.append(message['text'])
        latest='\n'.join(reversed(current_turn))
        shortcut=missing_shop_reply(latest,facts,has_images=bool(image))
        if shortcut:
            return shortcut
        replied=bool(self.store.db.execute("SELECT 1 FROM messages WHERE chat=? AND role IN ('assistant','owner') LIMIT 1",(job['chat'],)).fetchone())
        facts=dict(facts,conversation={'continuing':replied,
                                      'latest_client_message':latest})
        self.charge_call()
        answer=await self.ai.draft(history,facts,image)
        if not self.store.still_current(job):
            return None
        plan=answer.get('research',{'topic':'none','products':[]})
        if plan['topic']=='none':
            return answer
        stage='search'
        try:
            self.charge_call()
            evidence=await lookup(self.ai,plan)
            if not self.store.still_current(job):
                return None
            stage='final_draft'
            self.charge_call()
            final=await self.ai.draft(history,dict(facts,public_research=evidence),image)
            if not self.store.still_current(job):
                return None
            stage='citations'
            return attach_evidence(final,evidence)
        except Exception as exc:
            diagnostic=failure_details(exc,stage)
            logging.warning('Public research unavailable: %s',diagnostic)
            return unavailable(diagnostic)

    async def work_once(self):
        job=self.store.claim(time.time(),self.active_chats)
        if not job:
            return False
        self.active_chats.add(job['chat'])
        if job['uid'] not in self.allowed or time.time()-job['at']>=86340:
            self.store.db.execute("UPDATE messages SET state='stale' WHERE chat=? AND mid=?",(job['chat'],job['mid']))
            self.active_chats.discard(job['chat'])
            return True
        try:
            answer=await self.generate(job)
            if answer:
                replied=self.store.db.execute("SELECT 1 FROM messages WHERE chat=? AND role IN ('assistant','owner') LIMIT 1",(job['chat'],)).fetchone()
                answer['reply']=continuing_reply(answer['reply'],[{'role':'assistant'}] if replied else [])
                did=self.store.finish(job,answer['reply'],answer['summary'],answer['handoff'],answer.get('evidence'))
                if did:
                    await self.show_draft(did)
        except Exception as exc:
            self.store.db.execute("UPDATE messages SET state='failed' WHERE chat=? AND mid=? AND state='working'",(job['chat'],job['mid']))
            diagnostic=failure_details(exc,'draft')
            logging.warning('Draft processing failed: %s',diagnostic)
            await self.owner_message(self.chat_identity(job['chat'])+f'\nНе удалось подготовить ответ. Клиенту ничего не отправлено. Проверьте /pending или возьмите диалог вручную. {diagnostic}')
        finally:
            self.active_chats.discard(job['chat'])
        return True

    async def worker(self):
        while True:
            try:
                await self.work_once()
                if time.time()-self.daily_cleanup>3600:
                    self.store.cleanup(time.time()-7*86400)
                    self.daily_cleanup=time.time()
            except Exception as exc:
                logging.warning('Worker unavailable: %s',type(exc).__name__)
            await asyncio.sleep(1)

    async def poll(self):
        allowed=['business_connection','business_message','edited_business_message','deleted_business_messages','message','callback_query']
        while True:
            try:
                updates=await self.tg.call('getUpdates',offset=int(self.store.get('offset')),timeout=25,allowed_updates=allowed)
                for update in updates:
                    # Advance only after local persistence/handling. Message dedup and
                    # send reservation protect against replay after a crash here.
                    await self.ingest(update)
                    self.store.put('offset',update['update_id']+1)
            except Exception as exc:
                logging.warning('Polling failed: %s',type(exc).__name__)
                await asyncio.sleep(5)


def process_lock(path):
    file=open(str(path)+'.lock','a+b')
    try:
        if os.name=='nt':
            import msvcrt
            file.seek(0);file.write(b'0');file.flush();file.seek(0)
            msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK,1)
        else:
            import fcntl
            fcntl.flock(file,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except Exception:
        file.close()
        raise RuntimeError('Another support process owns this state volume') from None
    return file


async def main():
    import aiohttp
    required=('SUPPORT_BOT_TOKEN','SUPPORT_OWNER_ID',
              'OPENAI_API_KEY','SUPPORT_MODEL','SUPPORT_AUDIO_MODEL','SUPPORT_STATE_PATH')
    missing=[k for k in required if not os.environ.get(k,'').strip()]
    if missing:
        raise ValueError('Missing variables: '+', '.join(missing))
    if os.environ.get('SUPPORT_MEDIA_CONSENT')!='yes':
        raise ValueError('Set SUPPORT_MEDIA_CONSENT=yes only after approving transfer of test chat media to OpenAI')
    owner=int(os.environ['SUPPORT_OWNER_ID'])
    allowed={int(x.strip()) for x in os.environ.get('SUPPORT_TEST_CHAT_IDS','').split(',') if x.strip()}
    if owner<=0 or any(x<=0 or x==owner for x in allowed):
        raise ValueError('Positive, separate test client IDs are required')
    limit=int(os.environ.get('SUPPORT_DAILY_CALL_LIMIT','100'))
    if not 1<=limit<=2000:
        raise ValueError('Invalid daily request limit')
    path=Path(os.environ['SUPPORT_STATE_PATH'])
    if not path.is_absolute():
        raise ValueError('State path must be absolute and on a persistent volume')
    if os.environ.get('RAILWAY_ENVIRONMENT_ID'):
        mount=os.environ.get('RAILWAY_VOLUME_MOUNT_PATH')
        if not mount or not path.resolve().is_relative_to(Path(mount).resolve()):
            raise ValueError('Railway requires a volume; SUPPORT_STATE_PATH must be inside it')
    path.parent.mkdir(parents=True,exist_ok=True)
    lock=process_lock(path)
    store=Store(str(path));store.recover()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75)) as session:
            tg=Telegram(session,os.environ['SUPPORT_BOT_TOKEN'])
            me=await tg.call('getMe')
            if me.get('username','').lower()!='neirosetkaalexbot':
                raise ValueError('Token must belong to neirosetkaalexbot, not the shop bot')
            webhook=await tg.call('getWebhookInfo')
            if webhook.get('url'):
                raise ValueError('This bot already has a webhook; do not start polling over an existing service')
            shop=ShopReader(os.environ.get('SHOP_READONLY_DATABASE_URL',''))
            await shop.start()
            ai=OpenAI(session,os.environ['OPENAI_API_KEY'],os.environ['SUPPORT_MODEL'],
                      os.environ['SUPPORT_AUDIO_MODEL'],os.getenv('SUPPORT_SEARCH_MODEL'))
            service=Service(store,tg,ai,shop,owner,allowed,limit);service.bot_id=me['id']
            service.owner_interval=1.05
            # Fail startup if owner has not started the bot; no client processing yet.
            await service.owner_message('Тестовый помощник запущен. Режим: '+store.get('mode')+'. /status — управление. Автоответов нет.')
            try:
                await asyncio.gather(service.poll(),service.worker(),service.worker(),service.worker())
            finally:
                tasks=list(service.approval_tasks.values())
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                if shop.pool:
                    await shop.pool.close()
    finally:
        store.db.close();lock.close()


if __name__=='__main__':
    logging.basicConfig(level=logging.INFO,format='%(levelname)s %(message)s')
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Config errors have controlled messages; network exceptions never expose URLs.
        logging.error('Startup failed: %s',str(exc) if isinstance(exc,ValueError) else type(exc).__name__)
        raise SystemExit(1)
