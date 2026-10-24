"""Pilot: draft review + explicit owner-approved sends. NO automatic client sends."""
import asyncio
import datetime as dt
import json
import logging
import os
import time
from pathlib import Path

from .clients import OpenAI, ShopReader, Telegram, normalize_image
from .privacy import scrub, sensitive
from .storage import Store


def buttons(rows):
    return {'inline_keyboard':[[{'text':label,'callback_data':data} for label,data in row] for row in rows]}


class Service:
    def __init__(self,store,tg,ai,shop,owner,allowed,daily_limit=100):
        self.store,self.tg,self.ai,self.shop=store,tg,ai,shop
        self.owner,self.allowed,self.daily_limit=owner,set(allowed),daily_limit
        self.bot_id=None
        self.daily_cleanup=0

    async def owner_message(self,text,reply_markup=None):
        data={'chat_id':self.owner,'text':text[:3900]}
        if reply_markup:
            data['reply_markup']=reply_markup
        return await self.tg.call('sendMessage',**data)

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
                    await self.command(q.get('data',''))
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
        elif not raw:
            media={'kind':'unsupported'}
        if secret:
            media={'kind':'sensitive'}
        # Never persist supplied credentials, or forward raw content to the admin bot.
        text='[Сообщение скрыто: возможны данные доступа; нужна ручная проверка]' if secret else scrub(raw)
        self.store.ingest(ident,uid,m['message_id'],role,text,media,m['date'],
                          edited='edited_business_message' in update)

    async def command(self,text):
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
                '/stop — остановить всем\n/draft — включить подготовку\n'
                'AUTO в этой сборке отсутствует. Вложения тестовых чатов передаются OpenAI для анализа.',
                buttons([[('Остановить всем','global:off'),('Только черновики','global:draft')]]))
        elif cmd=='/chats':
            rows=self.store.db.execute('SELECT * FROM chats ORDER BY inbound DESC LIMIT 20').fetchall()
            if not rows:
                await self.owner_message('Пока нет сообщений из разрешённых тестовых чатов.')
            for c in rows:
                await self.owner_message(f'Чат №{c["id"]} · Telegram ID {c["uid"]}\nРежим: {c["mode"]}',
                    buttons([[('Отвечаю сам',f'chat:off:{c["id"]}'),('Готовить ответы',f'chat:draft:{c["id"]}')]]))
        elif cmd.startswith('chat:'):
            _,mode,raw=cmd.split(':')
            cid=int(raw)
            if not self.store.chat(cid):
                return
            self.store.switch(mode,cid)
            await self.owner_message(f'Чат №{cid}: {mode}. Старые ответы отменены.')
        elif cmd=='/pending':
            rows=self.store.db.execute("SELECT id FROM drafts WHERE state IN ('ready','uncertain') ORDER BY id DESC LIMIT 15").fetchall()
            for d in rows:
                await self.show_draft(d['id'])
            if not rows:
                await self.owner_message('Ожидающих черновиков нет.')
        elif cmd.startswith('send:'):
            await self.approve(int(cmd.split(':')[1]))
        elif cmd=='/retry' and len(parts)==2 and parts[1].isdigit():
            cid=int(parts[1]);c=self.store.chat(cid)
            if not c or c['mode']!='draft' or self.store.get('mode')!='draft':
                await self.owner_message('Сначала включите черновики для этого чата и глобально.')
                return
            m=self.store.db.execute("SELECT * FROM messages WHERE chat=? AND mid=? AND role='client'",(cid,c['latest'])).fetchone()
            if m:
                self.store.ingest(c['connection'],c['uid'],m['mid'],'client',m['text'],json.loads(m['media']),m['at'],edited=True)
                await self.owner_message('Подготовка поставлена в очередь заново. Это может расходовать API-баланс.')

    async def show_draft(self,did):
        d=self.store.db.execute('SELECT * FROM drafts WHERE id=?',(did,)).fetchone()
        if not d:
            return
        c=self.store.chat(d['chat'])
        rows=[[('Отвечаю сам',f'chat:off:{c["id"]}'),('Готовить ответы',f'chat:draft:{c["id"]}')]]
        if d['state']=='ready':
            rows.insert(0,[('Одобрить и отправить',f'send:{did}')])
        text=(f'Черновик №{did} · чат №{c["id"]} · клиент ID {c["uid"]}\n'
              f'Состояние: {d["state"]}\nНужен оператор: {"да" if d["handoff"] else "нет"}\n\n'
              f'Разбор (проверьте):\n{d["summary"]}\n\nПредложенный ответ:\n{d["text"]}')
        sent=await self.owner_message(text,buttons(rows))
        self.store.db.execute('UPDATE drafts SET admin_mid=? WHERE id=?',(sent['message_id'],did))

    async def approve(self,did):
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
            await self.owner_message('Ответ отменён/устарел, чат остановлен или нет права отправки. Повторно не отправляю.')
            return
        try:
            sent=await self.tg.call('sendMessage',chat_id=c['uid'],business_connection_id=c['connection'],text=reserved['text'])
        except Exception:
            self.store.db.execute("UPDATE drafts SET state='uncertain' WHERE id=?",(did,))
            await self.owner_message(f'Неизвестен результат отправки черновика №{did}. Проверьте личку. Автоматического повтора не будет.')
            return
        with self.store.transaction():
            self.store.db.execute("UPDATE drafts SET state='sent',telegram_mid=? WHERE id=?",(sent['message_id'],did))
            self.store.db.execute('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?,?)',
                (c['id'],sent['message_id'],'assistant',reserved['text'],'{}',time.time(),'done'))
        if reserved['handoff']:
            self.store.switch('off',c['id'])
        await self.owner_message(f'Черновик №{did} отправлен.'+(' Чат передан вам, помощник в нём остановлен.' if reserved['handoff'] else ''))

    def charge_call(self):
        day=dt.datetime.now(dt.timezone.utc).date().isoformat()
        if not self.store.reserve_call(day,self.daily_limit):
            raise ValueError('Daily API request limit reached')

    async def generate(self,job):
        media=json.loads(job['media']);kind=media.get('kind')
        if kind in ('sensitive','unsupported'):
            return {'reply':'Для этого вопроса нужна ручная проверка Александра. Пароли и коды входа мне отправлять не нужно.',
                    'summary':'Сообщение содержит возможные данные доступа или неподдерживаемое вложение. Посмотрите оригинал в личке. Секреты не использованы.', 'handoff':True}
        image=None
        if kind in ('voice','photo'):
            if media.get('file_size',0)>8_000_000 or media.get('duration',0)>180:
                raise ValueError('Attachment exceeds pilot limits')
            data=await self.tg.download(media['file_id'])
            if kind=='voice':
                self.charge_call()
                transcript=await self.ai.transcribe(data)
                if not self.store.still_current(job):
                    return None
                if sensitive(transcript):
                    return {'reply':'Для этого вопроса нужна ручная проверка Александра. Пароли и коды входа мне отправлять не нужно.',
                            'summary':'В расшифровке возможны данные доступа; текст не сохранён. Нужна ручная проверка.', 'handoff':True}
                text='[Расшифровка голосового; возможны ошибки]\n'+scrub(transcript)
                self.store.db.execute('UPDATE messages SET text=? WHERE chat=? AND mid=?',(text,job['chat'],job['mid']))
            else:
                image=normalize_image(data)
        if not self.store.still_current(job):
            return None
        facts=await self.shop.facts(job['uid'])
        if not self.store.still_current(job):
            return None
        self.charge_call()
        return await self.ai.draft(self.store.history(job['chat']),facts,image)

    async def work_once(self):
        job=self.store.claim(time.time())
        if not job:
            return False
        if job['uid'] not in self.allowed or time.time()-job['at']>=86340:
            self.store.db.execute("UPDATE messages SET state='stale' WHERE chat=? AND mid=?",(job['chat'],job['mid']))
            return True
        try:
            answer=await self.generate(job)
            if answer:
                did=self.store.finish(job,answer['reply'],answer['summary'],answer['handoff'])
                if did:
                    await self.show_draft(did)
        except Exception as exc:
            self.store.db.execute("UPDATE messages SET state='failed' WHERE chat=? AND mid=? AND state='working'",(job['chat'],job['mid']))
            logging.warning('Draft processing failed: %s',type(exc).__name__)
            await self.owner_message(f'Не удалось подготовить ответ для чата №{job["chat"]}. Клиенту ничего не отправлено. Проверьте /pending или возьмите диалог вручную. Ошибка: {type(exc).__name__}.')
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
            ai=OpenAI(session,os.environ['OPENAI_API_KEY'],os.environ['SUPPORT_MODEL'],os.environ['SUPPORT_AUDIO_MODEL'])
            service=Service(store,tg,ai,shop,owner,allowed,limit);service.bot_id=me['id']
            # Fail startup if owner has not started the bot; no client processing yet.
            await service.owner_message('Тестовый помощник запущен. Режим: '+store.get('mode')+'. /status — управление. Автоответов нет.')
            try:
                await asyncio.gather(service.poll(),service.worker())
            finally:
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
