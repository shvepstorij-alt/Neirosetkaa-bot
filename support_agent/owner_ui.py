"""Owner interface (16.11.2026): short main menu, settings, checks, compact draft card, /help."""
import datetime as dt
import html
import json

COMMANDS = [('start', 'Главное меню'), ('draft', 'Включить черновики'), ('pending', 'Черновики, ждущие ответа'), ('stats', 'Статистика по темам'),
            ('review', 'Файл с исправленными ответами'), ('stop', 'Остановить помощника'), ('help', 'Все команды')]

HELP = ('📖 Команды\n\n'
        '/start — главное меню\n/pending — черновики, ждущие ответа\n/stats 7 — статистика по темам за N дней\n'
        '/review 7 — файл с ответами, которые вы исправили или заменили\n/stop — остановить помощника всем\n'
        '/draft — режим «только черновики»\n/delay 3 — таймер сбора сообщений, минуты 0–60\n\n'
        'Проверки: /bench — эталон перед релизом, /check 1–7, /dialogs 1–4, /shop_status, /catalog_test, /holds.\n\n'
        'Как исправить черновик: нажмите «✏️ Исправить» и пришлите исправленный текст ответом на сообщение. '
        'Помощник отправит клиенту ваш текст.')

MODE_SHORT = {'off': '⏸ пауза', 'draft': '📝 черновики', 'auto': '🤖 автоответ'}


def main_menu(service):
    from .auto_panel import attention_chats
    store = service.store
    ready = store.db.execute("SELECT count(*) FROM drafts WHERE state='ready'").fetchone()[0]
    attention = len(attention_chats(service))
    day = dt.datetime.now(dt.timezone.utc).date().isoformat()
    row = store.db.execute('SELECT calls FROM quota WHERE day=?', (day,)).fetchone()
    text = (f'🤖 Помощник · режим: {MODE_SHORT.get(store.get("mode"), "неизвестен")}\n'
            f'Ждут ответа: {ready} · Требуют внимания: {attention}\n'
            f'Запросов сегодня: {row[0] if row else 0} из {service.daily_limit}')
    if service.auto_suspended:
        text += '\n\n🛑 Автоответ остановлен после ошибки отправки. Проверьте личку и перезапустите сервис.'
    rows = [[(f'📝 Черновики ({ready})', 'pending:0'), (f'⚠️ Внимание ({attention})', 'attention:0')],
            [('💬 Все чаты', 'chats:0'), ('📈 Статистика', 'menu:stats')],
            [('⚙️ Настройки', 'menu:settings')],
            # Owner 19.11.2026: on pause the main menu shows how to start again.
            [('▶️ Включить черновики', 'global:draft')] if store.get('mode') == 'off' else [('🛑 Остановить всё', 'global:off')]]
    return text, rows


def settings_menu(service):
    from .auto_panel import delay_label
    store = service.store
    mode = store.get('mode')
    text = (f'⚙️ Настройки\n\nРежим: {MODE_SHORT.get(mode, "неизвестен")}\n'
            f'Сбор сообщений: {delay_label(store.batch_seconds)}\n\n'
            '📝 Черновики — помощник готовит ответ, отправляете вы.\n'
            '🤖 Автоответ — только для разрешённых чатов и тем.')
    rows = [[('📝 Только черновики' + (' ✓' if mode == 'draft' else ''), 'global:draft'),
             ('🤖 Автоответы', 'auto:settings')],
            [('⏱ Таймер сбора сообщений', 'delay:view')],
            [('🧪 Проверки', 'menu:tests')],
            [('← Назад', 'control:refresh')]]
    return text, rows


def tests_menu():
    text = ('🧪 Проверки\n\nКнопками — быстрые проверки. Большие прогоны запускаются командой:\n'
            '/bench — эталонная проверка (30 случаев, перед каждым релизом)\n/check 1 … /check 7 — вопросы по сервисам\n/dialogs 1 … /dialogs 4 — реальные диалоги\n'
            'Большие прогоны расходуют запросы API.')
    rows = [[('🎯 Эталон', '/bench')], [('🛒 Магазин', '/shop_status'), ('📋 Каталог', '/catalog_test')],
            [('🛑 Почему были стопы', '/holds')],
            [('← Назад', 'menu:settings')]]
    return text, rows


def topic_name(evidence):
    from .draft_stats import TOPIC_NAMES, _topic
    topic = _topic(evidence, '')
    if topic in TOPIC_NAMES:
        return '' if topic == 'none' else TOPIC_NAMES[topic]
    try:
        from .faq import load_faq
        for item in load_faq()['topics']:
            if item['id'] == topic:
                return item['title'][:50]
    except Exception:
        pass
    return ''


def compact_card(service, d, c, client_label, state_label, customer_preview, reason=''):
    evidence = service.store.evidence(d['id'])
    rows = service.store.db.execute("SELECT text FROM messages WHERE chat=? AND role='client' AND mid<=? ORDER BY mid DESC LIMIT 3",
                                    (c['id'], d['mid'])).fetchall()
    question = '\n'.join(r['text'] or '' for r in reversed(rows)).strip()[:600] or '—'
    topic = topic_name(evidence if isinstance(evidence, dict) else {})
    head = f'💬 {client_label}' + (f' · {html.escape(topic)}' if topic else '') + f' · №{d["id"]}'
    text = (f'{head}\n<b>Клиент:</b> «{html.escape(question)}»\n\n'
            f'<b>Ответ помощника:</b>\n{customer_preview}')
    if d['handoff']:
        why = reason or str(evidence.get('diagnostic') or '') or str(d['summary'] or '')
        text += f'\n\n🛑 <i>Почему стоп: {html.escape(why[:250] or "причина не записана")}</i>'
    elif evidence.get('rescued'):
        text += f'\n\n🔁 <i>Был бы стоп — помощник ответил сам ({html.escape(str(evidence["rescued"]))}).</i>'
    if d['state'] == 'stale':
        text += '\n\n⚠️ <i>После этого черновика в чате были новые сообщения. Проверьте, что ответ всё ещё подходит — отправить или исправить можно в течение 12 часов.</i>'
    if evidence.get('owner_edited'):
        text += '\n\n✏️ <i>Текст исправлен вами.</i>'
    text += f'\n\n<i>{state_label}</i>'
    return text


def draft_rows(d, cid, active, expired):
    if active:
        return [[('✅ Отправить', f'send:{d["id"]}'), ('✏️ Исправить', f'edit:{d["id"]}')],
                [('🔁 Переписать', f'retry:{cid}'), ('🙅 Не отвечать', f'skip:{d["id"]}')],
                [('Ещё ▾', f'more:{d["id"]}')]]
    if expired:
        return [[('🔁 Подготовить свежий ответ', f'retry:{cid}')], [('Ещё ▾', f'more:{d["id"]}')]]
    return [[('Ещё ▾', f'more:{d["id"]}')]]


def more_rows(service, d, c):
    return [[('📤 Отправить и взять чат себе', f'sendpause:{d["id"]}')] if d['state'] == 'ready' else [],
            service.chat_controls(c),
            [('📜 Весь диалог', f'dialog:{c["id"]}'), ('🔎 Разбор', f'info:{d["id"]}')],
            [('← К черновику', f'draft:{d["id"]}')]]


def mark_edited(store, did, original):
    row = store.db.execute('SELECT data FROM draft_evidence WHERE draft=?', (did,)).fetchone()
    try:
        data = json.loads(row[0]) if row else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault('original_text', original)
    data['owner_edited'] = True
    if row:
        store.db.execute('UPDATE draft_evidence SET data=? WHERE draft=?', (json.dumps(data, ensure_ascii=False), did))
    else:
        store.db.execute('INSERT INTO draft_evidence VALUES (?,?)', (did, json.dumps(data, ensure_ascii=False)))
