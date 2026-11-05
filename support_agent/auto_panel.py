"""Owner-only control views and bounded local action history. No network/model."""
import datetime as dt
import json
import time
from .privacy import scrub

MODES={'off':'пауза','draft':'только черновики','auto':'автоответы в выбранном тестовом чате'}


def ensure(store):
    store.db.execute('CREATE TABLE IF NOT EXISTS auto_events(id INTEGER PRIMARY KEY,chat INTEGER,draft INTEGER,state TEXT,reason TEXT,at REAL)')


def record(store,cid,did,state,reason=''):
    ensure(store)
    store.db.execute('INSERT INTO auto_events(chat,draft,state,reason,at) VALUES (?,?,?,?,?)',
                     (cid,did,state,scrub(str(reason))[:1400],time.time()))
    store.db.execute('DELETE FROM auto_events WHERE at<?',(time.time()-7*86400,))


def latest(store,cid):
    ensure(store)
    return store.db.execute('SELECT * FROM auto_events WHERE chat=? ORDER BY id DESC LIMIT 1',(cid,)).fetchone()


def clock(at):
    return dt.datetime.fromtimestamp(at,dt.timezone(dt.timedelta(hours=5))).strftime('%d.%m %H:%M:%S')


def attention_chats(service):
    result=[]
    for c in service.store.db.execute('SELECT * FROM chats ORDER BY inbound DESC,id DESC').fetchall():
        if c['uid'] not in service.allowed:continue
        d=service.store.db.execute('SELECT * FROM drafts WHERE chat=? ORDER BY id DESC LIMIT 1',(c['id'],)).fetchone()
        m=service.store.db.execute('SELECT state FROM messages WHERE chat=? AND mid=?',(c['id'],c['latest'])).fetchone()
        event=latest(service.store,c['id'])
        if (d and (d['state']=='uncertain' or d['state']=='ready' and d['handoff'])
                or event and event['state'] in ('auto_blocked','send_uncertain') and c['mode']=='off'
                or m and m['state']=='failed' and not (event and event['state']=='manual')):
            result.append(c)
    return result


def overview(service):
    store=service.store
    counts={'auto':0,'draft':0,'off':0}
    for c in store.db.execute('SELECT * FROM chats').fetchall():
        if c['uid'] not in service.allowed:continue
        mode=store.effective_mode(c['id'])
        connection=store.db.execute('SELECT enabled,can_reply FROM connections WHERE id=?',(c['connection'],)).fetchone()
        if mode=='auto' and (not service.auto_permitted(c) or not connection or not connection['enabled'] or not connection['can_reply']):mode='off'
        counts[mode]+=1
    queued=working=0
    for row in store.db.execute("SELECT m.state,c.uid,c.id FROM messages m JOIN chats c ON c.id=m.chat WHERE m.state IN ('queued','working') AND m.mid=c.latest").fetchall():
        if row['uid'] not in service.allowed or store.effective_mode(row['id'])=='off':continue
        queued+=row['state']=='queued';working+=row['state']=='working'
    day=dt.datetime.now(dt.timezone.utc).date().isoformat()
    quota=store.db.execute('SELECT calls FROM quota WHERE day=?',(day,)).fetchone()
    used=quota[0] if quota else 0
    ready=store.db.execute("SELECT count(*) FROM drafts WHERE state='ready'").fetchone()[0]
    configured=('ID '+str(service.auto_test_id) if service.auto_test_id else 'не настроен; автоматическая отправка недоступна')
    text=('🤖 Управление помощником\n\nРежим: '+MODES.get(store.get('mode'),'неизвестен; отправка запрещена')
          +'\nТестовый получатель AUTO: '+configured
          +f'\n\nАвтоответы: {counts["auto"]} чат(ов)\nТолько черновики: {counts["draft"]}\nНа паузе: {counts["off"]}'
          +f'\nТребуют внимания: {len(attention_chats(service))}\nГотовых черновиков: {ready}'
          +f'\n\nСобирается обращений: {queued}\nГотовится ответов: {working}'
          +f'\nОкно сбора сообщений: {store.batch_seconds} сек. от первого сообщения обращения.'
          +f'\nAPI сегодня (UTC): {used}/{service.daily_limit} этапов. Это не денежный лимит.'
          +'\n\nДоступ Telegram проверяется отдельно перед отправкой. Состояние магазина: /shop_status.'
          +'\nНовые чаты автоматически не получают разрешение AUTO.\n/chats — все чаты · /pending — черновики · /stop — остановить всем'
          +'\n/draft — только черновики · /retry НОМЕР — повторить подготовку'
          +'\n/dialog_test — платный изолированный тест. Вложения тестовых чатов могут передаваться модели.')
    if service.auto_suspended:text+='\n\n🛑 AUTO остановлен в этом процессе после ошибки сохранения отправки. Проверьте личку и состояние хранилища перед перезапуском.'
    rows=[[('Остановить всем','global:off')],
          [('Все чаты','chats:0'),('Требуют внимания','attention:0')],
          [('Настройки автоответов','auto:settings'),('Только черновики','global:draft')],
          [('Обновить','control:refresh')]]
    return text,rows


def settings(service):
    store=service.store
    text=('⚙️ Автоответы · тестовый запуск\n\n'
          'Автоматическая отправка доступна только одному явно настроенному тестовому Telegram ID и только после включения в карточке чата.'
          '\n\nВозвраты, секретные данные, ручные действия и неподтверждённые ответы автоматически не отправляются: чат переходит в черновики с причиной для вас.'
          '\nРучное сообщение с вашей стороны ставит этот чат на паузу.'
          '\nПереключения не отправляют старые ответы. /stop не отзывает запрос, уже переданный Telegram.'
          '\n\nПолучатель: '+(str(service.auto_test_id) if service.auto_test_id else 'не настроен')
          +'\nТекущий общий режим: '+MODES.get(store.get('mode'),'неизвестен'))
    rows=[]
    if service.auto_suspended:text+='\nAUTO заблокирован после ошибки сохранения отправки. Сначала проверьте личку и состояние базы; затем перезапустите сервис.'
    if service.auto_test_id and not service.auto_suspended and store.get('mode')!='auto':
        rows.append([('Разрешить тестовый режим AUTO',f'auto:arm:{store.get("revision")}')])
    if service.auto_test_id:
        for c in store.db.execute('SELECT * FROM chats WHERE uid=? ORDER BY id DESC LIMIT 5',(service.auto_test_id,)):
            rows.append([(f'Тестовый чат №{c["id"]}',f'view:{c["id"]}')])
    else:text+='\nДобавьте SUPPORT_AUTO_TEST_CHAT_ID в Railway; ID должен входить в SUPPORT_TEST_CHAT_IDS. Значения ключей не нужны.'
    rows.extend([[('Остановить всем','global:off')],[('Назад','control:refresh')]])
    return text,rows


def chat_details(service,c):
    store=service.store
    event=latest(store,c['id'])
    text='\nФактически: '+MODES.get(store.effective_mode(c['id']),'неизвестно')
    if c['mode']=='auto' and service.auto_suspended:text+='\nAUTO заблокирован: ошибка сохранения отправки; нужна проверка хранилища и лички.'
    elif c['mode']=='auto' and not service.auto_permitted(c):text+='\nAUTO заблокирован: этот ID не является настроенным тестовым получателем.'
    if event:
        states={'auto_sent':'автоответ отправлен','auto_blocked':'автоответ передан на проверку',
                'send_uncertain':'результат отправки неизвестен','manual':'вы ответили вручную','owner_sent':'ответ отправлен после вашего одобрения','auto_enabled':'автоответы включены для новых обращений'}
        text+='\nПоследнее действие: '+states.get(event['state'],event['state'])+' · '+clock(event['at'])
        if event['reason']:text+='\nПричина: '+event['reason']
    d=store.db.execute('SELECT * FROM drafts WHERE chat=? ORDER BY id DESC LIMIT 1',(c['id'],)).fetchone()
    if d:
        text+='\nПоследний ответ: №'+str(d['id'])+' · '+d['state']
        if d['summary']:text+='\nКраткий разбор (проверьте): '+scrub(d['summary'])[:240]
        text+='\nУчастие владельца: '+('нужно' if d['handoff'] else 'не запрошено; отправка всё равно проверяется отдельно')
    return text
