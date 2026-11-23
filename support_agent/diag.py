"""Owner 23.11.2026: «стоит AUTO для всех, но клиентам помощник не отвечает».

/diag shows, in one message, every place where a client message can stop on its way to an automatic reply:
the Telegram Business connection, messages the bot never received or dropped, chats on pause, and what
happened to the last client messages and replies. No message text is shown, only who, when and the outcome.
"""
import time

_MSG_STATE = {'queued': 'ждёт сбора сообщений', 'working': 'готовится ответ', 'done': 'ответ подготовлен',
              'stale': 'отменено (новое сообщение или ваш ответ)', 'failed': 'ошибка подготовки', 'stored': 'не в работу'}
_DRAFT_STATE = {'ready': 'черновик ждёт вас', 'sent': 'отправлен', 'sending': 'отправляется', 'stale': 'устарел',
                'uncertain': 'отправка не подтверждена', 'review': 'на проверке у вас', 'paused': 'на проверке у вас'}
_DROP = {'not_allowed': 'чат не разрешён (проверьте SUPPORT_DRAFTS_FOR_ALL=yes)',
         'no_connection': 'нет подключения Telegram Business',
         'connection_disabled': 'подключение Telegram Business выключено',
         'foreign_sender': 'сообщение не от клиента и не от вас'}


def note_business(store, drop=''):
    """Called for every business message the bot receives; never stores text."""
    try:
        now = str(int(time.time()))
        store.put('diag:last_business_at', now)
        if drop:
            store.put('diag:last_drop', drop + '|' + now)
            row = store.db.execute("SELECT value FROM settings WHERE key=?", ('diag:drops:' + drop,)).fetchone()
            store.put('diag:drops:' + drop, str(int(row[0] if row else 0) + 1))
    except Exception:
        pass


def _ago(at):
    try:
        sec = max(0, int(time.time() - float(at)))
    except (TypeError, ValueError):
        return 'нет данных'
    if sec < 90:
        return '%d сек назад' % sec
    if sec < 5400:
        return '%d мин назад' % (sec // 60)
    if sec < 2 * 86400:
        return '%d ч назад' % (sec // 3600)
    return '%d дн назад' % (sec // 86400)


def report(service):
    store = service.store
    db = store.db
    get = store.get
    now = time.time()
    mode = get('mode')
    auto_all = (db.execute("SELECT value FROM settings WHERE key='auto_all'").fetchone() or ['0'])[0]
    lines = ['🩺 Диагностика автоответов', '']
    problems = []

    # 1. Switches
    lines.append('1) Переключатели')
    lines.append('• Общий режим: ' + {'auto': 'автоответы ✅', 'draft': 'только черновики ❌', 'off': 'пауза ❌'}.get(mode, str(mode)))
    lines.append('• AUTO для всех (SUPPORT_AUTO_FOR_ALL): ' + ('да ✅' if service.auto_for_all else 'нет ❌'))
    lines.append('• Черновики для всех (SUPPORT_DRAFTS_FOR_ALL): ' + ('да ✅' if getattr(service.allowed, 'drafts_for_all', False) else 'нет ❌'))
    if not getattr(service.allowed, 'drafts_for_all', False):
        problems.append('В Railway не включено SUPPORT_DRAFTS_FOR_ALL=yes — помощник видит только тестовые ID.')
    if service.auto_suspended:
        lines.append('• ❌ AUTO остановлен после ошибки отправки — нужен перезапуск сервиса.')
        problems.append('AUTO остановлен после ошибки отправки: перезапустите сервис в Railway.')
    if mode != 'auto':
        problems.append('Общий режим не «автоответы»: /auto → «Включить AUTO для всех клиентов».')
    if not service.auto_for_all:
        problems.append('В Railway не включено SUPPORT_AUTO_FOR_ALL=yes.')
    elif auto_all != '1':
        problems.append('Флаг «AUTO для всех» не записан в базу — перезапустите сервис.')

    # 2. Telegram Business
    lines += ['', '2) Подключение Telegram Business']
    cons = db.execute('SELECT * FROM connections').fetchall()
    if not cons:
        lines.append('• Подключений нет ❌')
        problems.append('Бот не подключён к Telegram Business: Настройки Telegram → Telegram для бизнеса → Чат-боты → @neirosetkaalexbot.')
    for c in cons:
        ok = bool(c['enabled']) and bool(c['can_reply'])
        lines.append('• Подключение: ' + ('включено' if c['enabled'] else 'выключено ❌') + ', право отвечать: '
                     + ('есть' if c['can_reply'] else 'нет ❌') + (' ✅' if ok else ''))
        if not ok:
            problems.append('В Telegram для бизнеса у бота выключено подключение или право «Отвечать на сообщения».')
    last = get('diag:last_business_at') if db.execute("SELECT 1 FROM settings WHERE key='diag:last_business_at'").fetchone() else None
    lines.append('• Последнее сообщение из ваших чатов получено: ' + (_ago(last) if last else 'с момента этого релиза не было'))
    drop = get('diag:last_drop') if db.execute("SELECT 1 FROM settings WHERE key='diag:last_drop'").fetchone() else ''
    if drop:
        kind, _, at = drop.partition('|')
        lines.append('• Последнее пропущенное сообщение: ' + _DROP.get(kind, kind) + ', ' + _ago(at))

    # 3. Chats
    lines += ['', '3) Чаты']
    counts = {row[0]: row[1] for row in db.execute('SELECT mode,count(*) FROM chats GROUP BY mode')}
    paused = db.execute('SELECT count(*) FROM chats WHERE manual_pause_until>?', (now,)).fetchone()[0]
    lines.append('• Всего: %d; на паузе в чате: %d; ваш ответ за последние 10 минут: %d'
                 % (sum(counts.values()), counts.get('off', 0), paused))
    if counts.get('off', 0):
        problems.append('%d чат(ов) стоят на паузе («Отправить и взять чат себе» или «Остановить в чате»). '
                        'Помощник в них молчит. Кнопка ниже снимает паузу во всех.' % counts['off'])
    waiting = db.execute("SELECT count(*) FROM messages WHERE role='client' AND state IN ('queued','working')").fetchone()[0]
    lines.append('• Сообщений в работе сейчас: %d (сбор сообщений: %s сек)' % (waiting, store.batch_seconds))

    # 4. Last client messages
    lines += ['', '4) Последние сообщения клиентов']
    rows = db.execute("""SELECT m.chat, m.at, m.state, c.mode, c.uid FROM messages m JOIN chats c ON c.id=m.chat
                         WHERE m.role='client' ORDER BY m.at DESC LIMIT 8""").fetchall()
    if not rows:
        lines.append('• Нет ни одного сообщения клиента в базе ❌')
    for r in rows:
        label = service.chat_label(r['chat'], r['uid'])
        eff = store.effective_mode(r['chat'])
        d = db.execute('SELECT state FROM drafts WHERE chat=? ORDER BY id DESC LIMIT 1', (r['chat'],)).fetchone()
        ev = db.execute('SELECT state,reason FROM auto_events WHERE chat=? ORDER BY id DESC LIMIT 1', (r['chat'],)).fetchone() \
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='auto_events'").fetchone() else None
        line = '• %s, %s: %s; режим чата: %s' % (label, _ago(r['at']), _MSG_STATE.get(r['state'], r['state']),
                                                 {'auto': 'автоответ', 'draft': 'черновик', 'off': 'пауза ❌'}.get(eff, eff))
        if d:
            line += '; ответ: ' + _DRAFT_STATE.get(d['state'], d['state'])
        if ev and ev['state'] == 'auto_blocked' and ev['reason']:
            line += '\n   причина: ' + str(ev['reason'])[:220]
        lines.append(line)

    lines += ['', '⚠️ Что мешает:' if problems else '✅ Переключатели и подключение в порядке.']
    lines += ['• ' + p for p in problems]
    rows = [[('🔄 Обновить', '/diag')]]
    if counts.get('off', 0):
        rows.insert(0, [('▶️ Снять паузу во всех чатах', 'diag:unpause')])
    if mode != 'auto' and service.auto_for_all:
        rows.insert(0, [('🤖 Включить автоответы для всех', 'auto:settings')])
    return '\n'.join(lines)[:4000], rows


def unpause_all(store):
    """Owner 23.11.2026: chats he once took over stay silent forever; one press returns them to the assistant."""
    with store.transaction():
        n = store.db.execute("UPDATE chats SET mode='draft', revision=revision+1 WHERE mode='off'").rowcount
    return n
