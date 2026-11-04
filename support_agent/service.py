"""Pilot: draft review + explicit owner-approved sends. NO automatic client sends."""
import asyncio
import datetime as dt
import json
import html
import logging
from contextvars import ContextVar
import os
import re
import time
from pathlib import Path

from .clients import OpenAI, ShopReader, Telegram
from .privacy import scrub, sensitive
from .storage import Store
from .research import lookup, attach_evidence, outgoing_text, unavailable, failure_details, with_public_trace
from .response_policy import continuing_reply, missing_shop_reply, consultation_plan, current_request


# Polling must persist local controls before moving to the next update, while
# owner-only rendering/network work must not hold up reception of /stop.
_POLL_INGEST=ContextVar('support_poll_ingest',default=False)
_COMMAND_IO_GATE=ContextVar('support_command_io_gate',default=None)


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
        self.admin_tasks={}
        self.callback_tasks={}
        self.admin_retiring=set()
        self.admin_sequence=0

    def release_command_io_gate(self):
        gate=_COMMAND_IO_GATE.get()
        if gate is not None:gate.set()

    def schedule_admin_job(self,factory,*,readonly=False):
        """Bound owner I/O. Mutable command tasks reach their first I/O in order.

        At capacity, another read-only refresh is discarded. A local control
        may cancel an older presentation task whose local phase has completed;
        owner UI delivery can be lost, but a stop is never discarded for it.
        """
        running=[(key,value) for key,value in self.admin_tasks.items() if not value['task'].done()]
        if len(running)>=4:
            if readonly:
                logging.warning('Owner read-only task not queued: capacity=4; local controls remain available')
                return None
            candidates=[(key,value) for key,value in running if value['readonly'] or value['gate'].is_set()]
            if not candidates:
                raise RuntimeError('Owner command local phases exhausted capacity')
            key,value=candidates[0]
            value['task'].cancel()
            self.admin_retiring.add(value['task'])
            value['task'].add_done_callback(self.admin_retiring.discard)
            self.admin_tasks.pop(key,None)
        self.admin_sequence+=1
        ident=self.admin_sequence;gate=asyncio.Event()
        async def run():
            token=_COMMAND_IO_GATE.set(gate)
            try:
                await factory()
            except (ValueError,IndexError):
                try:await self.owner_message('Некорректная команда. Используйте /status.')
                except Exception:logging.warning('Invalid command notification unavailable')
            except Exception as exc:
                logging.warning('Owner task unavailable: %s',type(exc).__name__)
                try:
                    await self.owner_message('Этап: обработка команды владельца. Результат панели не получен. '
                        'Локальные переключения режима могли уже примениться; проверьте /status. '
                        'Первопричина не установлена; необработанная ошибка скрыта.')
                except Exception:
                    logging.warning('Owner task failure notification unavailable')
            finally:
                gate.set()
                _COMMAND_IO_GATE.reset(token)
                self.admin_tasks.pop(ident,None)
        task=asyncio.create_task(run())
        self.admin_tasks[ident]={'task':task,'gate':gate,'readonly':readonly}
        return gate

    async def dispatch_polled_command(self,text,admin_mid=None):
        parts=text.strip().split()
        if not parts:return
        cmd=parts[0].split('@')[0]
        readonly=(cmd in ('/start','/status','/shop_status','/workflow_test','/catalog_test','/chats','/pending')
                  or cmd.startswith(('chats:','pending:','view:','draft:','dialog:','dlg:')))
        gate=self.schedule_admin_job(lambda:self.command(text,admin_mid),readonly=readonly)
        if gate is not None and not readonly:
            # Stop/resume/retry/prepare mutate state synchronously before their
            # first owner I/O. Never let an older resume run after a newer stop.
            await gate.wait()

    def schedule_callback_ack(self,ident):
        if len(self.callback_tasks)>=4:
            logging.warning('Callback acknowledgement not queued: capacity=4')
            return
        self.admin_sequence+=1;key=self.admin_sequence
        async def run():
            try:
                await self.tg.call('answerCallbackQuery',callback_query_id=ident)
            except Exception:
                logging.warning('Callback acknowledgement unavailable')
            finally:self.callback_tasks.pop(key,None)
        self.callback_tasks[key]=asyncio.create_task(run())

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
        self.release_command_io_gate()
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
                    if _POLL_INGEST.get():
                        await self.dispatch_polled_command(q.get('data',''),q.get('message',{}).get('message_id'))
                    else:
                        await self.command(q.get('data',''),admin_mid=q.get('message',{}).get('message_id'))
                except (ValueError,IndexError):
                    await self.owner_message('Некорректная команда. Используйте /status.')
            # Expired callback acknowledgement must not block the update queue
            # or replay an already handled command after a restart.
            if _POLL_INGEST.get():self.schedule_callback_ack(q['id'])
            else:
                try:
                    await self.tg.call('answerCallbackQuery',callback_query_id=q['id'])
                except Exception:
                    logging.warning('Callback acknowledgement unavailable')
            return
        if 'message' in update:
            m=update['message']
            if (m.get('text','').strip()=='/id' and m.get('chat',{}).get('type')=='private'
                    and m.get('from',{}).get('id')==m.get('chat',{}).get('id')):
                if _POLL_INGEST.get():
                    self.schedule_admin_job(lambda:self.tg.call('sendMessage',chat_id=m['chat']['id'],text=f'Ваш Telegram ID: {m["chat"]["id"]}'),readonly=True)
                else:
                    await self.tg.call('sendMessage',chat_id=m['chat']['id'],text=f'Ваш Telegram ID: {m["chat"]["id"]}')
                return
            if m.get('from',{}).get('id')==self.owner and m.get('chat',{}).get('id')==self.owner:
                try:
                    if _POLL_INGEST.get():await self.dispatch_polled_command(m.get('text',''))
                    else:await self.command(m.get('text',''))
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
        if not secret:
            from .external_quote import public_sites
            sites=public_sites(raw)
            if sites:media['public_service_sites']=sites
        # Keep only a local message reference, never Telegram's quoted text or
        # credentials. Context resolution validates the same-chat stored row.
        reply_mid=m.get('reply_to_message',{}).get('message_id')
        if role=='client' and type(reply_mid) is int and 0<reply_mid<m['message_id']:
            media['reply_mid']=reply_mid
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
        self.release_command_io_gate()
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
                '/research_test — тест поиска без отправки клиентам (расходует API)\n'
                '/dialog_test — тестовые диалоги через рабочую логику (расходует API)\n'
                'AUTO в этой сборке отсутствует. Вложения тестовых чатов передаются OpenAI для анализа.',
                buttons([[('Остановить всем','global:off'),('Только черновики','global:draft')]]))
        elif cmd=='/dialog_test':
            running=getattr(self,'research_probe_task',None)
            if running and not running.done():
                await self.owner_message('Другой тест уже выполняется. Дождитесь окончания или используйте /stop.')
                return
            from .dialog_probe import run_probe
            self.research_probe_task=asyncio.create_task(run_probe(self))
            await self.owner_message('Запускаю 3 тестовых диалога, 6 ответов. До 20 учтённых этапов API в пределах дневного лимита; это не денежный лимит. История изолирована, отправки клиентам запрещены. /stop остановит следующие этапы; начатый запрос может списать средства.')
        elif cmd=='/research_test':
            running=getattr(self,'research_probe_task',None)
            if running and not running.done():
                await self.owner_message('Проверка интернета уже выполняется.')
                return
            from .research_probe import run_probe
            self.research_probe_task=asyncio.create_task(run_probe(self))
            await self.owner_message('Проверяю Zoom, Krea и Lovable через API. Это платные запросы в пределах дневного лимита. Клиентам ничего не отправляется. /stop остановит дальнейшие этапы.')
        elif cmd=='/shop_status':
            facts=await self.shop.facts(self.owner)
            labels={'available':'доступны','truncated':'доступны первые 150 записей',
                    'read_error':'ошибка чтения / прав доступа','not_connected':'не подключены'}
            await self.owner_message('Магазин · проверка доступа только на чтение\n\n'
                +'Каталог: '+labels.get(facts.get('catalog_status'),'статус неизвестен')
                +'\nТарифов с подтверждённой базовой ценой: '+str(len(facts.get('catalog',[])))
                +'\nЗаказы: '+labels.get(facts.get('orders_status'),'статус неизвестен')
                +'\nПерсональные цены: '+('расчёт доступен' if facts.get('customer_prices_status')=='available' else 'не подключены; доступен обычный прайс')
                +'\nЖивой каталог магазина: '+str(facts.get('live_shop_status','not_connected'))
                +('\nДиагностика подключения: '+scrub(str(facts['live_shop_diagnostic']))[:700] if facts.get('live_shop_diagnostic') else '')
                +'\nСпособы оформления: '+str(facts.get('workflow_status','not_connected'))
                +'\nНавигация по боту: '+str(facts.get('bot_navigation',{}).get('status','not_connected'))
                +'\nБот магазина: '+('@'+facts['bot_navigation']['username'] if facts.get('bot_navigation',{}).get('status')=='available' else 'ник не подтверждён')
                +'\nApp Store: '+str(facts.get('appstore',{}).get('status','not_connected'))
                +'\nРегионов App Store: '+str(len(facts.get('appstore',{}).get('regions',[])))
                +'\n\nПроверка не вызывает модель. Статусы оплаты и активации различаются.')
        elif cmd=='/workflow_test':
            from .catalog_context import annotate
            query=' '.join(parts[1:])[:120]
            facts=await self.shop.facts(self.owner)
            rows=annotate(facts,query)['requested_catalog'] if query else facts.get('catalog',[])
            lines=['Проверка инструкций оформления','Живой источник: '+str(facts.get('live_shop_status','not_connected'))]
            groups={}
            reasons={
                'manual_not_projected':'ручная ветка: точные поля и шаги отдельно не описаны в экспорте',
                'code_steps_not_projected':'кодовая ветка: инструкции активации в эту проекцию ещё не включены',
                'invalid_instruction_type':'в настройках инструкции получен неподдерживаемый тип значения',
                'default_fields_changed':'стандарт полей магазина изменился; прежняя схема не применена',
                'custom_fields_not_projected':'нестандартная инструкция не распознана как однозначный список полей',
                'fields_confirmed':'поля подтверждены действующей инструкцией',
                'default_steps_unavailable':'стандартная инструкция по ссылке не передана в проекцию',
                'default_steps_changed':'стандарт шагов магазина отличается от поддерживаемого',
                'custom_steps_not_projected':'у товара нестандартная инструкция по ссылке; её шаги не подтверждены',
                'steps_confirmed':'шаги подтверждены действующей инструкцией'}
            for row in rows:
                item=(str(row.get('service','')),str(row.get('fulfillment','not_verified')),
                      str(row.get('required_fields_status','not_verified')),str(row.get('workflow_steps_status','not_verified')),
                      row.get('workflow_reason',''))
                groups.setdefault(item,[]).append(str(row.get('plan','')))
            for index,(item,plans) in enumerate(groups.items()):
                if index>=40:
                    lines.append('Показаны первые 40 вариантов. Укажите сервис после команды.');break
                name,method,fields,steps,reason=item
                coverage=(f'поля={fields}' if method=='account_details' else
                          f'пошаговая инструкция={steps}' if method=='checkout_link' else
                          'поля и шаги не проверяются этой проекцией')
                lines.append(f'{name}: тарифы '+', '.join(dict.fromkeys(plans))+
                             f'\nСпособ={method}; {coverage}.'+
                             '\nПричина: '+reasons.get(reason,'точная причина не получена из экспорта; нужна проверка текущей версии магазина'))
                from .workflow_policy import policies_for
                policy=policies_for({'requested_catalog':[{'service':name}]})
                if policy:
                    for confirmed in policy:
                        fields=','.join(confirmed['required_fields']) or 'данные аккаунта клиента не требуются'
                        lines.append(name+': подтверждено владельцем '+confirmed['confirmed_on']+'; поля клиента='+fields+'.')
                        if confirmed.get('field_alternatives'):
                            lines.append(name+': альтернативные наборы (достаточно одного)='+
                                ' ИЛИ '.join(','.join(option) for option in confirmed['field_alternatives'])+'.')
                        if confirmed.get('workflow_steps_status')=='available':
                            lines.append(name+': пошаговая инструкция подтверждена владельцем '+confirmed['confirmed_on']
                                         +'; источник='+confirmed.get('workflow_steps_source','owner_confirmed')+'.')
                        if confirmed.get('provided_fields'):
                            lines.append(name+': клиенту выдаются '+','.join(confirmed['provided_fields'])+'; тип аккаунта='+confirmed.get('account_ownership','not_confirmed')+'; срок='+confirmed['duration']+'.')
                        compatible=confirmed.get('compatible_fulfillment',['account_details'])
                        if method in ('manual','account_details','checkout_link','code_activation_conditional') and method not in compatible:
                            lines.append('Конфликт '+name+': правило владельца='+confirmed['expected_fulfillment']+'; настройка магазина='+method+'. Метод не изменён автоматически.')
            if not rows:lines.append('В прочитанной выборке сервис не сопоставлен; это не подтверждает отсутствие товара.')
            lines.append('not_verified означает, что конкретные поля/шаги ещё не подтверждены, а не что оформить нельзя. Модель не вызывается.')
            await self.owner_message('\n\n'.join(lines))
        elif cmd=='/catalog_test':
            from .catalog_context import annotate
            name=' '.join(parts[1:])[:120] or 'Krea App Store'
            facts=annotate(await self.shop.facts(self.owner),name)
            rows=facts['requested_catalog']
            lines=['Проверка каталога · '+name,'Источник: '+str(facts.get('source')),
                   'Живой экспорт: '+str(facts.get('live_shop_status','not_connected'))]
            for row in rows[:30]:
                lines.append(str(row['service'])+' · '+str(row['plan'])+' — '+str(row.get('catalog_price_rub'))+' ₽')
            if not rows:lines.append('Совпадений в прочитанных данных нет. Это не доказательство отсутствия товара.')
            apple=facts.get('appstore',{})
            lines.append('App Store: '+str(apple.get('status','not_connected')))
            lines.extend(str(r['name'])+' · номиналов: '+str(len(r['denoms'])) for r in apple.get('regions',[])[:50])
            lines.append('Цены показаны для владельца до промокода/монет. Модель не вызывается.')
            await self.owner_message('\n\n'.join(lines))
        elif cmd=='/chats' or cmd.startswith('chats:'):
            await self.list_chats(int(cmd.split(':')[1]) if ':' in cmd else 0,admin_mid)
        elif cmd.startswith('view:'):
            await self.show_chat(int(cmd.split(':')[1]),admin_mid)
        elif cmd.startswith('draft:'):
            did=int(cmd.split(':')[1])
            if self.store.db.execute('SELECT 1 FROM drafts WHERE id=?',(did,)).fetchone():
                await self.show_draft(did,admin_mid)
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
            draft=self.store.db.execute('SELECT id FROM drafts WHERE chat=? AND admin_mid=? ORDER BY id DESC LIMIT 1',
                                        (cid,admin_mid)).fetchone() if admin_mid is not None else None
            if draft:
                await self.show_draft(draft['id'],admin_mid)
                return
            if admin_mid is not None:
                await self.show_chat(cid,admin_mid)
                return
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

    async def show_draft(self,did,admin_mid=None):
        d=self.store.db.execute('SELECT * FROM drafts WHERE id=?',(did,)).fetchone()
        if not d:
            return
        c=self.store.chat(d['chat'])
        rows=[self.chat_controls(c)]
        rows.append([('Весь диалог',f'dialog:{c["id"]}')])
        rows.append([('Все чаты','chats:0'),('Обновить',f'draft:{did}')])
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
            for index,source in enumerate(evidence['sources']):
                research_note+=f'<a href="{html.escape(source["url"],quote=True)}">Официальный источник [{source["id"]}]</a>\n'
                if (index+1)%8==0:research_note+='\n'
            for name,identity in evidence.get('discoveries',{}).items():
                research_note+=f'Сайт {html.escape(name)} найден поиском: <a href="{html.escape(identity["corroboration_url"],quote=True)}">проверка принадлежности</a>\n'
            for name,reason in evidence.get('failures',{}).items():
                research_note+=f'Не проверено: <b>{html.escape(name)}</b> — {html.escape(str(reason)[:180])}\n'
            research_note+='<i>Проверьте соответствие источников ответу. Это не прайс магазина.</i>\n\n'
            if evidence.get('citation_mode')=='bibliography':
                research_note+='<i>Модель не привязала все источники к фразам. Перед отправкой проверьте, подтверждают ли они ответ. Ссылки видны только вам.</i>\n\n'
        else:
            research_note={'catalog_direct':'📋 <b>Цены:</b> ответ собран из каталога без генерации моделью.\n\n',
                           'owner_policy':'📋 <b>Условия:</b> ответ по правилам владельца; право клиента ещё не подтверждено.\n\n',
                           'clarification':'💬 <b>Уточнение:</b> запрошены сведения для разбора; причина проблемы не установлена.\n\n',
                           'visual_clarification':'📷 <b>Фото:</b> нужна более чёткая деталь; распознавание не запускалось.\n\n',
                           'visual_review':'📷 <b>Фото:</b> ответ не прошёл проверку; нужен ручной разбор.\n\n',
                           'catalog_review':'📋 <b>Каталог:</b> показана только подтверждённая часть; свободный ответ отклонён проверкой.\n\n',
                           'unavailable':'🌐 <b>Интернет:</b> свежие сведения не подтверждены; нужна ручная проверка.\n\n',
                           'not_recorded':'🌐 <i>Для старого черновика сведения о проверке не сохранены.</i>\n\n'}.get(
                               evidence['status'],'')
            if evidence['status'] in ('unavailable','catalog_review','visual_clarification','visual_review') and evidence.get('diagnostic'):
                research_note+=f'🛠 <b>Диагностика для владельца</b>\n{html.escape(evidence["diagnostic"][:1500])}\n\n'
        atomic_diagnostics=evidence.get('atomic_diagnostics',[])
        if atomic_diagnostics:
            research_note+='🛠 <b>Проверка отдельных утверждений</b>\n'+html.escape('\n'.join(str(x) for x in atomic_diagnostics)[:1500])+'\n\n'
        note=('Проверьте личку перед дальнейшими действиями. Автоповтора нет.'
              if d['state']=='uncertain' else 'Клиент получит только текст ответа после вашего одобрения.'
              if d['state']=='ready' else 'Эта карточка показывает сохранённый черновик.')
        from .customer_format import format_customer
        _,customer_preview=format_customer(d['text'][:1800])
        text=(f'📝 <b>Черновик ответа №{did}</b>\n\n'
              f'👤 <b>Клиент:</b> {client}\n'
              f'<b>Telegram ID:</b> <code>{c["uid"]}</code> · <b>Чат:</b> №{c["id"]}\n\n'
              f'<b>Статус:</b> {state}\n'
              f'<b>Помощник в чате:</b> {"пауза" if c["mode"]=="off" else "подготовка ответов"}\n'
              f'<b>Участие Александра:</b> {"нужно" if d["handoff"] else "не требуется по оценке модели"}\n\n'
              f'🔎 <b>Разбор обращения</b>\n<i>Вывод модели — проверьте перед отправкой.</i>\n'
              f'{html.escape(d["summary"][:700])}\n\n'
              f'💬 <b>Ответ клиенту</b>\n{customer_preview}\n\n'
              f'{research_note}'
              f'<i>{note}</i>')
        if admin_mid is not None:
            if len(html.unescape(re.sub(r'<[^>]*>','',text)).encode('utf-16-le'))//2>3900:
                # A long draft was initially sent in several parts. Its final
                # action panel is updated without duplicating those parts.
                text=(f'📝 <b>Черновик ответа №{did}</b>\n\n'
                      f'👤 {client} · <code>{c["uid"]}</code> · Чат №{c["id"]}\n\n'
                      f'<b>Статус:</b> {state}\n'
                      f'<b>Помощник в чате:</b> {"пауза" if c["mode"]=="off" else "подготовка ответов"}\n\n'
                      f'💬 <b>Ответ клиенту</b>\n{customer_preview}\n\n'
                      f'<i>Разбор и источники сохранены в предыдущих частях карточки. {note}</i>')
            updated=await self.admin_panel(text,rows,admin_mid,parse_mode='HTML')
            if updated is not None:
                self.store.db.execute('UPDATE drafts SET admin_mid=? WHERE id=?',(admin_mid,did))
        else:
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
        try:
            await self.load_connection(c['connection'])
        except Exception as exc:
            status=getattr(exc,'status',None)
            detail=(' HTTP='+str(status)+'.') if type(status) is int and 100<=status<=599 else ''
            cause=('Время ожидания истекло.' if isinstance(exc,asyncio.TimeoutError) else
                   'Telegram не подтвердил доступность подключения; точная первопричина не получена.')
            await self.owner_message(self.chat_identity(c['id'])+f'\nЧерновик №{did}. '
                'Этап: проверка прав Telegram Business перед отправкой. '+cause+detail+
                ' Этим нажатием отправка клиенту не начиналась. Состояние черновика можно проверить через /pending.')
            return
        reserved=self.store.reserve_send(did,time.time())
        if not reserved:
            reason=self.store.send_block_reason(did,time.time())
            if d['admin_mid']:
                await self.show_draft(did,d['admin_mid'])
            await self.owner_message(self.chat_identity(c['id'])+f'\nЧерновик №{did}: отправка заблокирована. Этап: проверка перед отправкой. Причина: {reason}. Клиенту ничего не отправлено этим нажатием.')
            return
        if pause_after:
            # Pause before yielding to the network so a new incoming message cannot
            # be queued and then silently discarded after this send completes.
            self.store.switch('off',c['id'])
        try:
            text=outgoing_text(reserved['text'],self.store.evidence(did))
            from .customer_format import customer_payload
            text,payload=customer_payload(text)
            sent=await self.tg.call('sendMessage',chat_id=c['uid'],business_connection_id=c['connection'],**payload)
        except Exception:
            self.store.db.execute("UPDATE drafts SET state='uncertain' WHERE id=?",(did,))
            self.store.switch('off',c['id'])
            if d['admin_mid']:
                await self.show_draft(did,d['admin_mid'])
            await self.owner_message(self.chat_identity(c['id'])+f'\nНеизвестен результат отправки черновика №{did}. Чат поставлен на паузу. Проверьте личку перед возобновлением. Автоматического повтора не будет.',
                                     buttons([self.chat_controls(self.store.chat(c['id']))]))
            return
        with self.store.transaction():
            self.store.db.execute("UPDATE drafts SET state='sent',telegram_mid=? WHERE id=?",(sent['message_id'],did))
            self.store.db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?)',
                (c['id'],sent['message_id'],'assistant',text,'{}',time.time(),'done'))
        current=self.store.chat(c['id'])
        if d['admin_mid']:
            await self.show_draft(did,d['admin_mid'])
        await self.owner_message(self.chat_identity(c['id'])+f'\nЧерновик №{did} отправлен. '+(
            'Чат на паузе; отвечаете вы.' if current['mode']=='off' else
            'Помощник продолжит готовить черновики следующих сообщений.'),
            buttons([self.chat_controls(current),[('Все чаты','chats:0'),('Обновить',f'draft:{did}')]]))

    def charge_call(self):
        day=dt.datetime.now(dt.timezone.utc).date().isoformat()
        if not self.store.reserve_call(day,self.daily_limit):
            raise ValueError('Daily API request limit reached')

    async def generate(self,job):
        from .storage import HISTORY_MESSAGES,HISTORY_CHARACTERS
        batch_first=self.store.chat(job['chat'])['batch_first']
        from .topic_boundary import boundary_reply
        incoming=self.store.db.execute('SELECT text FROM messages WHERE chat=? AND role=\'client\' AND mid>=? AND mid<=? ORDER BY mid',
                                       (job['chat'],batch_first,job['mid'])).fetchall()
        previous=self.store.db.execute('SELECT text FROM messages WHERE chat=? AND role=\'client\' AND mid<? ORDER BY mid DESC LIMIT 1',
                                       (job['chat'],batch_first)).fetchone()
        blocked=boundary_reply('\n'.join(r['text'] or '' for r in incoming),previous['text'] if previous else '')
        if blocked:return blocked
        turn=self.store.db.execute('''SELECT count(*),COALESCE(sum(length(text)),0) FROM messages
            WHERE chat=? AND role='client' AND mid>=? AND mid<=?''',
            (job['chat'],batch_first,job['mid'])).fetchone()
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
            WHERE chat=? AND role='client' AND mid>=? AND mid<=?''',
            (job['chat'],batch_first,job['mid'])).fetchone()[0]
        if size>HISTORY_CHARACTERS:
            return {'reply':'Уточню детали вашего обращения и вернусь с ответом.',
                    'summary':'После расшифровки обращение превышает объём контекста. Проверьте весь диалог вручную; частичный ответ не подготовлен.',
                    'handoff':True}
        facts=await self.shop.facts(job['uid'])
        from .offer_terms import enrich as confirmed_offer_terms
        facts=confirmed_offer_terms(facts)
        from .referral_guide import referral_facts
        from .claude_policy import claude_policy_facts
        from .external_quote import POLICY
        facts=dict(facts,referral_policy=referral_facts(facts),owner_claude_policy=claude_policy_facts(),external_quote_policy=POLICY)
        if not self.store.still_current(job):return None
        facts=dict(facts,context_scope='История содержит только последние 60 сообщений в пределах 24000 символов; более ранние договорённости могут отсутствовать. Не придумывай их.')
        if not self.store.still_current(job):
            return None
        history=self.store.history(job['chat'])
        # Full history remains context for references; only the fixed collection
        # batch is a new request. An unapproved old draft must not revive its topic.
        batch=self.store.db.execute('''SELECT mid,role,text FROM messages
            WHERE chat=? AND role='client' AND mid>=? AND mid<=? ORDER BY mid''',
            (job['chat'],batch_first,job['mid'])).fetchall()
        latest=current_request(batch,batch_first,job['mid'])
        from .request_context import resolve_request
        latest=resolve_request(self.store.db,job['chat'],batch_first,job['mid'],latest,facts.get('catalog',[]))
        from .session_context import enrich_request
        latest=enrich_request(self.store.db,job['chat'],batch_first,job['mid'],latest,facts.get('catalog',[]))
        if not latest and image:
            latest='Клиент прислал изображение без подписи. Разбери, что на нём видно; если цель обращения неясна, уточни её. Не придумывай ошибку или намерение клиента.'
        if not latest:
            return {'reply':'Напомните, пожалуйста, ваш вопрос — не хочу ответить про другой сервис.',
                    'summary':'Клиент ждёт ответ, но исходный вопрос отсутствует в сохранённом контексте.',
                    'handoff':True}
        from .catalog_context import annotate
        facts=annotate(facts,latest)
        raw_batch='\n'.join(row['text'] or '' for row in batch)
        facts=dict(facts,case_fields={
            'payment_reference_supplied':bool(re.search(r'(?:номер\s+(?:заказа|платежа)|intid|заказ\s*№)\s*[:#№]?\s*\d{3,12}(?!\d)',raw_batch,re.I)),
            'payment_provider_supplied':bool(re.search(r'free\s*kassa|фр[ие]кас[с]?а',raw_batch,re.I))})
        site_rows=self.store.db.execute('SELECT media FROM messages WHERE chat=? AND role=\'client\' AND mid>=? AND mid<=? ORDER BY mid',
                                        (job['chat'],batch_first,job['mid'])).fetchall()
        sites=[]
        for row in site_rows:
            try:media=json.loads(row['media'] or '{}')
            except (ValueError,TypeError):continue
            sites.extend(media.get('public_service_sites',[]))
        facts=dict(facts,public_service_sites=list(dict.fromkeys(sites))[:2])
        if len(facts['public_service_sites'])==1 and re.fullmatch(r'\s*\[ссылка скрыта\]\s*',raw_batch):
            # A homepage sent after our clarification is a new identity hint,
            # not an instruction to reuse the last unrelated purchased service.
            from .service_discovery import public_name
            brand=facts['public_service_sites'][0].split('.')[0].replace('-',' ')
            if public_name(brand):
                latest='Как оформить '+brand+'? Какие тарифы и стоимость?'
                facts=annotate(facts,latest)
                raw_batch=latest
        from .referral_guide import referral_reply
        from .claude_policy import claude_risk_reply
        known_reply=referral_reply(facts,raw_batch) or claude_risk_reply(facts,raw_batch)
        if known_reply and not image:return known_reply
        from .external_quote import intake_reply
        external_intake=intake_reply(facts,raw_batch)
        if external_intake and not image:return external_intake
        from .consultation_intent import context as consultation_context
        consultation=consultation_context(latest,facts)
        from .selection_help import is_selection_request,has_selection_purpose
        transactional=bool(re.search(r'как[^.!?\n]{0,30}(?:оплат|активир|оформ)|я\s+оплат|уже\s+оплат|что[^.!?\n]{0,30}(?:нужно|потребуется)',raw_batch,re.I))
        current_consultation=consultation_context(raw_batch,facts)
        task_followup=has_selection_purpose(raw_batch) and not transactional
        if not (current_consultation.get('active') or is_selection_request(raw_batch,facts) or task_followup):
            consultation={'active':False}
        if consultation.get('active'):
            facts=dict(facts,consultation_intent=consultation)
        # Old questions provide reference slots, not the newest turn's intent.
        from .turn_scope import scope as scope_turn,purchase_reply
        resolved_context=latest
        facts,latest=scope_turn(facts,resolved_context,raw_batch)
        if getattr(self,'_probe_capture',False):
            self._probe_facts=facts
            self._probe_question=latest
        if image:
            boundary=missing_shop_reply(latest,facts,has_images=True)
            if boundary:return boundary
            from .visual_quality import assess_images,VisualQualityError
            from .troubleshooting import low_detail_reply,visual_failure
            try:
                clarification=low_detail_reply(assess_images(image),latest)
            except VisualQualityError as exc:
                return visual_failure(str(exc))
            if clarification:return clarification
        from .bot_guide import navigation_facts,navigation_reply
        facts=dict(facts,shop_navigation=navigation_facts(facts))
        from .request_goals import covers,goals
        facts=dict(facts,request_goals=sorted(goals(raw_batch or latest,facts)))
        raw_batch='\n'.join(row['text'] or '' for row in batch)
        from .activation_guidance import reply as activation_help
        activation=activation_help(facts,latest,raw_batch) if not image else None
        if activation:return activation
        purchase=purchase_reply(facts,raw_batch) if not image else None
        if purchase:return purchase
        from .troubleshooting import screenshot_request
        problem=screenshot_request(raw_batch or latest,has_images=bool(image))
        if problem:return problem
        from .troubleshooting import refund_decision_reply
        refund_policy=refund_decision_reply(raw_batch or latest,facts,has_images=bool(image))
        if refund_policy:return refund_policy
        from .course_bonus import context as course_context, reply as course_reply
        course=course_context(raw_batch or latest,history)
        if course:
            intake=course_reply(course)
            if intake and not image:return intake
            facts=dict(facts,course_bonus=course)
            if course['comparison_requested']:
                latest+='\nСравни ChatGPT и Claude для выбора бонусной подписки курса; учти актуальные региональные ограничения и условия доступа в России. Не подтверждай право на бонус.'
        from .selection_help import is_selection_request,selection_reply
        # Intent belongs to this batch; relevant past tasks may enrich the
        # facts, but an old request to choose must not override "more details".
        selection=(is_selection_request(raw_batch or latest,facts) or consultation.get('active',False)
                   or task_followup and bool(facts.get('requested_catalog'))) if not image and not course else False
        if selection:
            clarification=selection_reply(facts,latest)
            if clarification:return clarification
        facts=dict(facts,task_consultation=bool(selection))
        navigation=navigation_reply(facts,raw_batch or latest) if not image and covers(raw_batch,{'purchase'},facts) else None
        if navigation and not course:return navigation
        from .catalog_context import direct_workflow_reply,direct_apple_reply
        apple=direct_apple_reply(facts,latest) if not image and not course and covers(raw_batch,{'purchase','price','activation'},facts) else None
        if apple:return apple
        from .fulfillment_reply import workflow_reply
        fulfillment=workflow_reply(facts,latest) if not image and not course and not selection and covers(raw_batch,{'purchase','price'},facts) else None
        if fulfillment:return fulfillment
        workflow=direct_workflow_reply(facts,latest) if not image and not course and not selection and covers(raw_batch,{'purchase','price'},facts) else None
        if workflow:return workflow
        shortcut=missing_shop_reply(latest,facts,has_images=bool(image)) if not course and not selection else None
        if shortcut:
            return shortcut
        from .catalog_context import direct_catalog_reply
        catalog_goals={'price','purchase'} if 'appstore' in facts.get('catalog_selection',{}).get('mentioned_known_services',[]) else {'price'}
        direct=direct_catalog_reply(facts,latest) if not image and not course and not selection and covers(raw_batch,catalog_goals,facts) else None
        if direct:return direct
        from .conversation_style import session_info
        replied=session_info(self.store.db,job['chat'],batch_first,job['mid'])['continuing']
        facts=dict(facts,conversation={'continuing':replied,
                                      'latest_client_message':latest,
                                      'latest_message':batch[-1]['text'] if batch else ''})
        if selection and not image and not course:
            from .recommendation_flow import run as recommend_for_task
            recommendation=await recommend_for_task(self,facts,latest,raw_batch,job)
            if recommendation is not None:return recommendation
        if facts.get('public_service_sites') and not image and not course and not facts.get('requested_catalog'):
            from .external_quote import run as quote_external
            external=await quote_external(self,facts,latest,{'products':[]},job)
            if external is not None:return external
        self.charge_call()
        answer=await self.ai.draft(history,facts,image)
        if not self.store.still_current(job):
            return None
        plan=consultation_plan(latest,answer.get('research',{'topic':'none','products':[]}),facts)
        if not image and not course:
            from .external_quote import run as quote_external
            if not facts.get('public_service_sites') and len(plan.get('products',[]))==1:
                brand=re.sub(r'[^a-z0-9]','',plan['products'][0].casefold())
                for previous in self.store.db.execute('SELECT media FROM messages WHERE chat=? AND role=\'client\' AND mid<? ORDER BY mid DESC LIMIT 6',
                                                     (job['chat'],batch_first)).fetchall():
                    try:oldsites=json.loads(previous['media'] or '{}').get('public_service_sites',[])
                    except (ValueError,TypeError):continue
                    matching=[s for s in oldsites if brand and brand in re.sub(r'[^a-z0-9]','',s.split('.')[0])]
                    if matching:
                        facts=dict(facts,public_service_sites=matching[:1]);break
            external=await quote_external(self,facts,latest,plan,job)
            if external is not None:return external
        if course and course['comparison_requested']:plan={'topic':'comparison','products':['chatgpt','claude']}
        facts=annotate(facts,latest,plan['products'])
        facts,_=scope_turn(facts,resolved_context,raw_batch,discovered=True)
        if getattr(self,'_probe_capture',False):
            self._probe_facts=facts
        if plan['topic']=='none':
            # An unknown product has no selected rows. That must not exempt a
            # free-form promise about our ability to fulfil/pay from review.
            store_consultation=bool(re.search(r'оформ|актив|подписк|тариф|оплат|каталог|магазин|пополн',
                                             latest+' '+answer['reply'],re.I))
            if image or store_consultation or facts.get('requested_catalog') or facts.get('appstore') or (facts.get('catalog') and re.search(r'₽|руб|\bRUB\b',answer['reply'],re.I)):
                self.charge_call()
                review=await self.ai.check_answer(answer,facts,**({'images':image} if image else {}))
                if not self.store.still_current(job):return None
                if not review['supported']:
                    self.charge_call()
                    corrected=dict(facts,research_correction={'issue':'catalog_conflict',
                        'content_review':review['reason'],'instruction':'Исправь ответ по requested_catalog и appstore. Не отрицай имеющийся товар и не подменяй рублёвую цену официальной. Если новых фактов нет, не придумывай их.'})
                    answer=await self.ai.draft(history,corrected,image)
                    if not self.store.still_current(job):return None
                    if answer.get('research',{}).get('topic','none')!='none':
                        return unavailable('После исправления ответа требуется отдельная проверка публичных сведений.')
                    self.charge_call()
                    review=await self.ai.check_answer(answer,corrected,**({'images':image} if image else {}))
                    if not self.store.still_current(job):return None
                    if not review['supported']:
                        if image:
                            from .troubleshooting import visual_failure
                            result=visual_failure(review['reason'],latest)
                            result['evidence']['content_review']=review
                            if getattr(self,'_probe_capture',False):result['evidence']['rejected_reply']=scrub(answer.get('reply',''))[:1800]
                            return result
                        from .consultation_intent import fallback as consultation_fallback
                        useful=consultation_fallback(facts,review['reason'])
                        if useful:return useful
                        from .catalog_context import verified_catalog_reply
                        return verified_catalog_reply(facts,review['reason'])
                answer.setdefault('evidence',{'status':'catalog_checked','sources':[]})['content_review']=review
            return answer
        stage='search'
        evidence={}
        try:
            self.charge_call()
            def charge_search():
                if not self.store.still_current(job):
                    raise ValueError('Stale research job')
                self.charge_call()
            try:
                from .research import public_facets
                evidence=await lookup(self.ai,plan,charge_extra=charge_search,question_facets=public_facets(latest))
            except Exception as exc:
                if str(exc) in ('Stale research job','Daily API request limit reached'):
                    raise
                # Retain usable shop facts even when the public lookup fails.
                evidence={'status':'partial','checked_at':time.time(),'sources':[],
                    'products':[],'requested_products':plan['products'],'digest':'',
                    'failures':{p:failure_details(exc,'search') for p in plan['products']}}
            if not self.store.still_current(job):
                return None
            from .research import CitationError, evidence_domains, safe_url
            correction=None
            from .feature_reply import price_block,compose
            # Partial evidence still uses deterministic prices when at least
            # one product has sources; unknown products get explicit blocks.
            # A feature table cannot represent a recommendation or a mixed
            # request with purchase steps. Keep these in full reviewed prose.
            retail_block=price_block(facts,latest,topic=plan['topic']) if evidence.get('sources') and not course and not image and not selection else ''
            if retail_block:
                # Unknown services may need official prices; do not suppress
                # that part of a mixed question with the store-price-only mode.
                if any(not annotate({'catalog':facts.get('requested_catalog',[])},p)['requested_catalog'] for p in plan['products']):
                    retail_block=''
            for attempt in range(2):
                if not self.store.still_current(job):
                    return None
                stage='final_draft'
                self.charge_call()
                final_facts=dict(facts,public_research=evidence)
                if retail_block:
                    final_facts['feature_only']=True
                    final_facts['feature_claims_v2']=True
                if correction:final_facts['research_correction']=correction
                atomic_reviewed=False
                try:
                    final=await self.ai.draft(history,final_facts,image)
                    if not self.store.still_current(job):
                        return None
                    if final_facts.get('feature_claims_v2') and 'feature_claims' in final:
                        atomic_reviewed=True
                        # Review atomic records before citation formatting.
                        # A failed/uncited product must not bypass review, and
                        # a rejected optional claim must not erase other facts.
                        stage='answer_check'
                        self.charge_call()
                        review=await self.ai.check_answer(final,final_facts)
                        if not self.store.still_current(job):return None
                        if not review.get('supported'):
                            raise ValueError('Answer content unsupported')
                        target_labels={t['target_id']:t['service']+(' '+t['plan'] if t.get('plan') else '')
                                       for t in final['feature_claims'].get('targets',[])}
                        final=review.get('revised_answer',final)
                        atomic_diagnostics=final.get('evidence',{}).get('atomic_diagnostics',[])
                        checked=attach_evidence(dict(final),evidence,allow_catalog_only=True,semantic_review=True)
                        checked.setdefault('evidence',{})['content_review']={k:v for k,v in review.items() if k!='revised_answer'}
                        checked['evidence']['atomic_diagnostics']=atomic_diagnostics
                        if final.get('handoff'):
                            checked['evidence']['status']='insufficient'
                            missing=[target_labels[tid] for tid in review.get('missing_targets',[]) if tid in target_labels]
                            checked['evidence']['handoff_reason']=('Не подтверждены все запрошенные функции: '+', '.join(missing)+'.'
                                if missing else 'Проверка оставила неполные публичные сведения; конкретные причины указаны в проверке утверждений.')
                        return with_public_trace(checked,evidence)
                    if retail_block:
                        composed,reason=compose(final,retail_block)
                        if reason:
                            review={'supported':False,'reason':reason}
                            raise ValueError('Answer content unsupported')
                        final=composed
                    stage='citations'
                    checked=attach_evidence(dict(final),evidence,allow_catalog_only=bool(facts.get('catalog') or evidence.get('failures')),semantic_review=True)
                    missing=checked.get('evidence',{}).get('missing_products',[])
                    covered=all(any(safe_url(s['url'],evidence_domains(evidence,p))
                                for s in evidence['sources']) for p in missing)
                    if not missing:
                        stage='answer_check'
                        self.charge_call()
                        review=await self.ai.check_answer(final,final_facts,**({'images':image} if image else {}))
                        if not self.store.still_current(job):return None
                        if not review['supported']:
                            raise ValueError('Answer content unsupported')
                        checked.setdefault('evidence',{})['content_review']=review
                    if not missing or not covered or attempt:
                        if course:
                            checked['handoff']=True
                            checked.setdefault('evidence',{})['handoff_reason']='Доступность бонуса проверяет владелец вручную по почте регистрации на курсе.'
                        return with_public_trace(checked,evidence)
                    issue='missing_products'
                except Exception as exc:
                    # Retry citation formatting only, not transport, privacy,
                    # unsupported facts, or arbitrary model failures.
                    if (atomic_reviewed or 'feature_claims' in (locals().get('final') or {}) or attempt or not (isinstance(exc,CitationError) or
                                       getattr(exc,'reason',None) in ('feature_length','feature_incomplete') or
                                       getattr(exc,'reason',None)=='draft_source_ids' or
                                       str(exc)=='Answer content unsupported')):
                        raise
                    issue=(getattr(exc,'reason') if getattr(exc,'reason',None) in ('feature_length','feature_incomplete') else
                           'unsupported_content' if str(exc)=='Answer content unsupported'
                           else getattr(exc,'citation_reason','unknown_ids'))
                    missing=[]
                correction={'missing_products':missing,'issue':issue,
                    'content_review':review['reason'] if issue=='unsupported_content' else '',
                    'allowed_source_ids':[s['id'] for s in evidence['sources']],
                    'instruction':'Перепиши ответ только по имеющимся источникам. source_ids должен перечислять ID подтверждающих источников, а каждый [ID] в тексте должен входить в source_ids. Для каждого сервиса используй его источники. Не придумывай ссылки или факты. Если часть сведений отсутствует, сохрани подтверждённые сведения и их source_ids, handoff=true. Пустой source_ids допустим только без публичных фактических утверждений.'}
        except Exception as exc:
            if not self.store.still_current(job):
                return None
            from .consultation_intent import fallback as consultation_fallback
            useful=consultation_fallback(facts,review['reason'] if str(exc)=='Answer content unsupported' else failure_details(exc,stage))
            if useful:return with_public_trace(useful,evidence)
            if (locals().get('final_facts') or {}).get('feature_claims_v2'):
                diagnostic=failure_details(exc,stage)
                # A failed public-feature contract is not a purchase request.
                # Keep exact retail facts without unrelated password/workflow
                # fallback text; never release unchecked feature candidates.
                result={'reply':retail_block+'\n\nПодробности возможностей этих тарифов сейчас подтвердить не получилось. Уточню их перед рекомендацией.',
                        'summary':'Цены прочитаны из магазина. Проверка описаний возможностей не завершена; неподтверждённые функции не включены.',
                        'handoff':True,'source_ids':[],
                        'research':{'topic':'none','products':[]},
                        'evidence':{'status':'catalog_review','diagnostic':diagnostic,
                                    'sources':evidence.get('sources',[]),
                                    'handoff_reason':'Нужна проверка описаний функций; цены не являются подтверждением функций.'}}
                return with_public_trace(result,evidence)
            if image:
                from .troubleshooting import visual_failure
                diagnostic=review['reason'] if str(exc)=='Answer content unsupported' else failure_details(exc,stage)
                result=visual_failure(diagnostic,latest)
                if str(exc)=='Answer content unsupported':result['evidence']['content_review']=review
                if getattr(self,'_probe_capture',False):result['evidence']['rejected_reply']=scrub((locals().get('final') or {}).get('reply',''))[:1800]
                return with_public_trace(result,evidence)
            if course:
                # A failed public comparison must never become a paid order
                # recommendation just because ordinary catalogue rows exist.
                result=course_reply(dict(course,comparison_requested=False))
                result['reply']='Актуальные условия для полного сравнения сейчас подтвердить не получилось. '+result['reply']
                diagnostic=review['reason'] if str(exc)=='Answer content unsupported' else failure_details(exc,stage)
                result['evidence']={'status':'unavailable','sources':evidence.get('sources',[]),
                    'diagnostic':'Этап: сравнение бонусных подписок курса. '+diagnostic,
                    'handoff_reason':'Нужна ручная проверка бонуса и неподтверждённых условий сравнения.'}
                return with_public_trace(result,evidence)
            if str(exc)=='Answer content unsupported' and facts.get('requested_catalog'):
                from .catalog_context import verified_catalog_reply
                fallback=verified_catalog_reply(facts,review['reason'])
                fallback['evidence'].update(sources=evidence.get('sources',[]),content_review=review,
                                            checked_at=evidence.get('checked_at'))
                # Store only a scrubbed candidate, never raw model payload/history.
                if getattr(self,'_probe_capture',False):
                    fallback['evidence']['rejected_reply']=scrub(final.get('reply',''))[:2500]
                return with_public_trace(fallback,evidence)
            if str(getattr(exc,'reason','')).startswith('feature_') and facts.get('requested_catalog'):
                from .catalog_context import verified_catalog_reply
                fallback=verified_catalog_reply(facts,failure_details(exc,stage))
                fallback['evidence'].update(sources=evidence.get('sources',[]),checked_at=evidence.get('checked_at'))
                return with_public_trace(fallback,evidence)
            diagnostic=failure_details(exc,stage)
            logging.warning('Public research unavailable: %s',diagnostic)
            result=unavailable(diagnostic)
            if getattr(exc,'discovery_reason',None)=='uncertain':
                result['reply']='Не удалось однозначно определить официальный сайт сервиса. Уточните, пожалуйста, его полное название и для чего вы хотите его использовать?'
            elif str(exc)=='Service identity unconfirmed':
                result['reply']='Сейчас не получилось проверить актуальные условия сервиса. Не хочу назвать вам неверные тарифы или ограничения.'
            return with_public_trace(result,evidence)

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
                from .conversation_style import session_info,decorate_reply
                first=self.store.chat(job['chat'])['batch_first']
                answer['reply']=decorate_reply(answer['reply'],session_info(self.store.db,job['chat'],first,job['mid']))
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
                    token=_POLL_INGEST.set(True)
                    try:await self.ingest(update)
                    finally:_POLL_INGEST.reset(token)
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
    store=Store(str(path))
    interrupted=store.db.execute("SELECT count(*) FROM messages WHERE state='working'").fetchone()[0]
    interrupted_sends=store.db.execute("SELECT count(*) FROM drafts WHERE state IN ('sending','uncertain')").fetchone()[0]
    store.recover()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75)) as session:
            tg=Telegram(session,os.environ['SUPPORT_BOT_TOKEN'])
            me=await tg.call('getMe')
            if me.get('username','').lower()!='neirosetkaalexbot':
                raise ValueError('Token must belong to neirosetkaalexbot, not the shop bot')
            webhook=await tg.call('getWebhookInfo')
            if webhook.get('url'):
                raise ValueError('This bot already has a webhook; do not start polling over an existing service')
            shop=ShopReader(os.environ.get('SHOP_READONLY_DATABASE_URL',''),
                            os.environ.get('SUPPORT_SHOP_URL',''),os.environ.get('SUPPORT_SHOP_READ_TOKEN',''))
            await shop.start()
            ai=OpenAI(session,os.environ['OPENAI_API_KEY'],os.environ['SUPPORT_MODEL'],
                      os.environ['SUPPORT_AUDIO_MODEL'],os.getenv('SUPPORT_SEARCH_MODEL'))
            service=Service(store,tg,ai,shop,owner,allowed,limit);service.bot_id=me['id']
            service.owner_interval=1.05
            # Fail startup if owner has not started the bot; no client processing yet.
            await service.owner_message('Тестовый помощник запущен. Режим: '+store.get('mode')+'. /status — управление. Автоответов нет.'+
                ('\nПрерванных подготовок ответа: '+str(interrupted)+'. Автоматически не повторяются; проверьте /chats.' if interrupted else '')+
                ('\nНеопределённых отправок: '+str(interrupted_sends)+'. Соответствующие чаты на паузе; сначала проверьте личку и /pending.' if interrupted_sends else ''))
            from .probe_api import start_server,startup_diagnostic
            probe_server=None
            try:
                probe_server=await start_server(service)
            except Exception as exc:
                diagnostic=startup_diagnostic(exc)
                logging.warning('Remote test API unavailable: %s',diagnostic)
                await service.owner_message('Удалённый тестовый интерфейс не запущен. Этап: настройка HTTP-сервера; '
                    +diagnostic+' Основной помощник продолжает работу.')
            try:
                await asyncio.gather(service.poll(),service.worker(),service.worker(),service.worker())
            finally:
                if probe_server:await probe_server.cleanup()
                tasks=list(service.approval_tasks.values())
                tasks.extend(item['task'] for item in service.admin_tasks.values())
                tasks.extend(service.callback_tasks.values())
                tasks.extend(service.admin_retiring)
                probe=getattr(service,'research_probe_task',None)
                if probe and not probe.done():tasks.append(probe)
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
