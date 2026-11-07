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
        d=service.store.db.execute("SELECT * FROM drafts WHERE chat=? AND (state='uncertain' OR state='ready' AND handoff=1) ORDER BY id DESC LIMIT 1",(c['id'],)).fetchone()
        m=service.store.db.execute('SELECT state FROM messages WHERE chat=? AND mid=?',(c['id'],c['latest'])).fetchone()
        event=latest(service.store,c['id'])
        if (d and (d['state']=='uncertain' or d['state']=='ready' and d['handoff'])
                or event and event['state'] in ('auto_blocked','send_uncertain')
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
    configured=('все разрешённые чаты' if service.auto_for_all else
                'ID '+', '.join(str(i) for i in sorted(service.auto_test_ids)) if service.auto_test_ids else
                'не настроена; автоматическая отправка недоступна')
    text=('🤖 Управление помощником\n\nРежим: '+MODES.get(store.get('mode'),'неизвестен; отправка запрещена')
          +'\nТестовый получатель AUTO: '+configured
          +f'\n\nАвтоответы: {counts["auto"]} чат(ов)\nТолько черновики: {counts["draft"]}\nНа паузе: {counts["off"]}'
          +f'\nТребуют внимания: {len(attention_chats(service))}\nГотовых черновиков: {ready}'
          +f'\n\nСобирается обращений: {queued}\nГотовится ответов: {working}'
          +f'\nСбор сообщений: {delay_label(store.batch_seconds)}.'
          +f'\nAPI сегодня (UTC): {used}/{service.daily_limit} этапов. Это не денежный лимит.'
          +'\n\nДоступ Telegram проверяется отдельно перед отправкой. Состояние магазина: /shop_status.'
          +'\nНовые чаты автоматически не получают разрешение AUTO.\n/chats — все чаты · /pending — черновики · /stop — остановить всем'
          +'\n/draft — только черновики · /retry НОМЕР — повторить подготовку'
          +'\n/dialog_test — платный изолированный тест. Вложения тестовых чатов могут передаваться модели.')
    if service.auto_suspended:text+='\n\n🛑 AUTO остановлен в этом процессе после ошибки сохранения отправки. Проверьте личку и состояние хранилища перед перезапуском.'
    rows=[[('Остановить всем','global:off')],
          [('Все чаты','chats:0'),('Требуют внимания','attention:0')],
          [('Настройки автоответов','auto:settings'),('Только черновики','global:draft')],
          [('Таймер сбора сообщений','delay:view')],
          [('Обновить','control:refresh')]]
    return text,rows


def delay_label(seconds):
    if seconds==0:return 'без ожидания'
    if seconds%60==0:return str(seconds//60)+' мин. от первого сообщения'
    return str(seconds)+' сек. от первого сообщения'


def delay_settings(service):
    store=service.store;rev=store.get('batch_delay_revision')
    text=('⏱ Таймер сбора сообщений\n\nСейчас: '+delay_label(store.batch_seconds)
          +'\n\nНастройка общая для черновиков и AUTO. Несколько сообщений внутри окна объединяются в одно обращение. '
          'Новое сообщение внутри окна не отсчитывает таймер заново.'
          '\n\nБез ожидания: подготовка начинается при первой возможности. Это не гарантирует мгновенную отправку: '
          'ещё нужны обработка, проверка и доступ Telegram. Ответы могут стать чаще, расходы API — выше.'
          '\n\nИзменение применяется и к ожидающим обращениям. Уже готовящийся ответ не отменяется; '
          'завершённые и отменённые обращения повторно не запускаются.'
          '\n\nСвоё время: /delay 2 — две минуты; /delay 0 — без ожидания. Диапазон: 0–60 минут.')
    rows=[[('Без ожидания'+(' ✓' if store.batch_seconds==0 else ''),f'delay:set:0:{rev}')],
          [(str(minutes)+' мин.'+(' ✓' if store.batch_seconds==minutes*60 else ''),f'delay:set:{minutes*60}:{rev}') for minutes in (1,3,5)],
          [('Своё время','delay:custom')]]
    if store.batch_seconds==0:
        rows.append([('Вернуть ожидание ('+delay_label(int(store.get('batch_saved_seconds')))+')',f'delay:restore:{rev}')])
    rows.append([('Обновить','delay:view'),('Управление','control:refresh')])
    return text,rows


def settings(service):
    store=service.store
    group=('все разрешённые чаты' if service.auto_for_all else
           ', '.join(str(i) for i in sorted(service.auto_test_ids)) if service.auto_test_ids else 'не настроена')
    text=('⚙️ Автоответы · тестовый запуск\n\n'
          'Автоматическая отправка доступна тестовой группе из переменных окружения и только после включения в карточке чата. Отдельный переключатель включает её для всех разрешённых чатов.'
          '\n\nНеподтверждённый ответ остаётся вам для проверки. Клиент получает короткое уведомление, если Telegram разрешает отправку. Режим не меняется из-за ошибок.'
          '\nРучное сообщение отменяет устаревшую подготовку, но не меняет режим. Остановить помощника можно в меню чата или командой /stop.'
          '\nПереключения не отправляют старые ответы. /stop не отзывает запрос, уже переданный Telegram.'
          '\n\nПолучатели: '+group
          +'\nТекущий общий режим: '+MODES.get(store.get('mode'),'неизвестен'))
    rows=[]
    if service.auto_suspended:text+='\nAUTO заблокирован после ошибки сохранения отправки. Сначала проверьте личку и состояние базы; затем перезапустите сервис.'
    if (service.auto_test_ids or service.auto_for_all) and not service.auto_suspended and store.get('mode')!='auto':
        rows.append([('Разрешить тестовый режим AUTO',f'auto:arm:{store.get("revision")}')])
    if service.auto_test_ids and not service.auto_for_all:
        marks=','.join('?' for _ in service.auto_test_ids)
        for c in store.db.execute('SELECT * FROM chats WHERE uid IN ('+marks+') ORDER BY id DESC LIMIT 8',tuple(service.auto_test_ids)):
            rows.append([(f'Тестовый чат №{c["id"]}',f'view:{c["id"]}')])
    elif not service.auto_for_all:
        text+='\nЗадайте SUPPORT_AUTO_TEST_CHAT_IDS (список через запятую) или оставьте SUPPORT_AUTO_TEST_CHAT_ID. ID должны входить в SUPPORT_TEST_CHAT_IDS. Для всех чатов отдельно: SUPPORT_AUTO_FOR_ALL=yes.'
    rows.extend([[('Остановить всем','global:off')],[('Назад','control:refresh')]])
    return text,rows


def chat_details(service,c):
    store=service.store
    event=latest(store,c['id'])
    text='\nФактически: '+MODES.get(store.effective_mode(c['id']),'неизвестно')
    if c['mode']=='auto' and service.auto_suspended:text+='\nAUTO заблокирован: ошибка сохранения отправки; нужна проверка хранилища и лички.'
    elif c['mode']=='auto' and not service.auto_permitted(c):text+='\nAUTO заблокирован: этот ID не является настроенным тестовым получателем.'
    if event:
        states={'auto_sent':'автоответ отправлен','notice_sent':'клиенту отправлено служебное уведомление; исходный ответ требует проверки','auto_blocked':'автоответ передан на проверку',
                'send_uncertain':'результат отправки неизвестен','manual':'вы ответили вручную','owner_sent':'ответ отправлен после вашего одобрения','auto_enabled':'автоответы включены для новых обращений'}
        text+='\nПоследнее действие: '+states.get(event['state'],event['state'])+' · '+clock(event['at'])
        if event['reason']:text+='\nПричина последнего действия: '+event['reason']
    d=store.db.execute('SELECT * FROM drafts WHERE chat=? ORDER BY id DESC LIMIT 1',(c['id'],)).fetchone()
    pending=store.db.execute('SELECT state FROM messages WHERE chat=? AND mid=?',(c['id'],c['latest'])).fetchone()
    current=bool(d and d['mid']==c['latest'] and d['revision']==c['revision']
                 and d['global_revision']==int(store.get('revision')) and d['state']!='stale')
    if pending and pending['state'] in ('queued','working') and not current:
        text+='\nТекущее обращение: '+('идёт сбор сообщений' if pending['state']=='queued' else 'ответ готовится')
        text+='\nРешение об отправке AUTO по нему ещё не принято.'
    if d:
        text+='\n'+('Текущий ответ' if current else 'Предыдущий ответ')+': №'+str(d['id'])+' · '+d['state']
        if current:
            if d['summary']:text+='\nКраткий разбор (проверьте): '+scrub(d['summary'])[:240]
            text+='\nПроверка ответа владельцем: '+('нужна' if d['handoff'] else 'не запрошена; допуск AUTO проверяется отдельно')
            if store.evidence(d['id']).get('owner_action_required'):
                text+='\nОформление выполняет Александр; подтверждённую инструкцию можно отправить автоматически.'
        elif d['state']=='stale':text+='\nЭтот ответ отменён и не является решением по новому обращению.'
    notice=store.db.execute('SELECT original FROM auto_notices WHERE draft=?',(d['id'],)).fetchone() if d else None
    if notice:
        text+='\nИсходный черновик для проверки: №'+str(notice['original'])
        reason=store.evidence(notice['original']).get('auto_block_reason','')
        if reason:text+='\nПричина ручной проверки: '+scrub(reason)[:700]
    return text
