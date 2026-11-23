"""Owner 24.11.2026: «помощник должен самообучаться по своим ответам и взаимодействию с клиентами».

How it learns, without learning wrong things from clients:
1. Signals are collected from real dialogs: a draft Alexander rewrote or answered himself, an answer that went out
   and Alexander still wrote to that client within 30 minutes, an answer the client did not understand or disliked.
2. Once a day (or on /learn) the model compares the client's message, the assistant's answer and Alexander's
   answer and proposes up to 5 short general lessons («Когда клиент …, отвечай …»).
3. Alexander approves each lesson with a button. Only approved lessons go into every next answer, as his rules.
   /lessons lists them; any lesson can be removed.
Client words alone never become a rule: a lesson needs Alexander's own answer or his approval.
"""
import json
import re
import time

MAX_ACTIVE = 60
MAX_PROPOSE = 5
OWNER_WINDOW = 1800
_NEGATIVE = re.compile(r'(?i)\b(?:не\s+понял|не\s+поняла|непонятно|не\s+то|не\s+помогло|не\s+работает|бред|ерунд|'
                       r'ты\s+бот|это\s+бот|позовите|живого|человек[ау]?\b|не\s+отвечаете\s+на\s+вопрос|я\s+же\s+спросил)')


def ensure(db):
    db.execute('''CREATE TABLE IF NOT EXISTS lessons(
        id INTEGER PRIMARY KEY, text TEXT, why TEXT, state TEXT, created REAL, decided REAL)''')
    db.execute('CREATE TABLE IF NOT EXISTS learn_seen(draft INTEGER PRIMARY KEY, at REAL)')


def active(db, limit=MAX_ACTIVE):
    try:
        ensure(db)
        return [row['text'] for row in db.execute(
            "SELECT text FROM lessons WHERE state='active' ORDER BY id DESC LIMIT ?", (limit,)).fetchall()][::-1]
    except Exception:
        return []


def prompt_block(lessons):
    lessons = [str(x).strip() for x in lessons or () if str(x).strip()]
    if not lessons:
        return ''
    return ('УРОКИ АЛЕКСАНДРА (утверждены владельцем по живым диалогам; выполняй их как его правила, '
            'но цены и наличие бери только из каталога):\n' + '\n'.join('- ' + x for x in lessons))


def _clean(text, limit=700):
    from .privacy import scrub, sensitive
    text = ' '.join(str(text or '').split())
    if sensitive(text):
        return '[скрыто: данные доступа]'
    return scrub(text)[:limit]


def signals(db, since, limit=20):
    """Cases worth learning from; each draft is used once."""
    ensure(db)
    rows = db.execute('''SELECT d.id, d.chat, d.mid, d.text, d.state, d.created, e.data FROM drafts d
        LEFT JOIN draft_evidence e ON e.draft=d.id
        WHERE d.created>=? AND d.id NOT IN (SELECT draft FROM learn_seen) ORDER BY d.id DESC LIMIT 400''', (since,)).fetchall()
    out = []
    for row in rows:
        try:
            ev = json.loads(row['data'] or '{}')
        except ValueError:
            ev = {}
        if not isinstance(ev, dict):
            ev = {}
        draft = ev.get('original_text') or row['text'] or ''
        if 'Александр ответит здесь' in draft and len(draft) < 140:
            continue  # a holding notice teaches nothing
        client = db.execute("SELECT text FROM messages WHERE chat=? AND role='client' AND mid<=? ORDER BY mid DESC LIMIT 3",
                            (row['chat'], row['mid'])).fetchall()
        after = db.execute("SELECT role,text,at FROM messages WHERE chat=? AND at>=? AND at<=? AND role IN ('owner','client') ORDER BY at LIMIT 4",
                           (row['chat'], row['created'], row['created'] + OWNER_WINDOW)).fetchall()
        owner = [r['text'] for r in after if r['role'] == 'owner' and (r['text'] or '').strip()]
        reaction = [r['text'] for r in after if r['role'] == 'client' and _NEGATIVE.search(r['text'] or '')]
        kind = ''
        if ev.get('owner_edited'):
            kind, owner = 'Александр исправил ответ', [row['text']]
        elif owner and row['state'] != 'sent':
            kind = 'Александр ответил сам вместо черновика'
        elif owner:
            kind = 'ответ ушёл, но Александр дописал клиенту'
        elif reaction and row['state'] == 'sent':
            kind = 'клиенту ответ не помог'
        if not kind:
            continue
        out.append({'draft_id': row['id'], 'kind': kind,
                    'client': _clean('\n'.join(r['text'] or '' for r in reversed(client))),
                    'assistant': _clean(draft, 900),
                    'alexander': _clean('\n'.join(owner), 900),
                    'client_after': _clean('\n'.join(reaction), 300)})
        if len(out) >= limit:
            break
    return out


def mark_seen(db, ids):
    ensure(db)
    for i in ids:
        db.execute('INSERT OR IGNORE INTO learn_seen VALUES (?,?)', (int(i), time.time()))


INSTRUCTIONS = (
    'Ты помогаешь Александру обучать его ИИ-помощника поддержки магазина подписок на нейросети (@neirosetkaa_bot). '
    'Тебе даны случаи из живых диалогов: что написал клиент, что ответил помощник и как ответил или поправил Александр '
    '(или как клиент отреагировал). Найди, чему помощник должен научиться, и сформулируй до 5 коротких ОБЩИХ уроков '
    'в виде правил: «Когда клиент …, отвечай …» или «Не …, а …». '
    'Главный источник — ответы Александра: урок повторяет его подход, тон и шаги. Реакция клиента без ответа Александра '
    'даёт урок только о понятности и полноте ответа, но не новые факты. '
    'Не пиши цены, сроки, имена, почты, номера заказов и личные данные. Не придумывай фактов, которых нет в ответах Александра. '
    'Не повторяй уроки из existing_lessons. Если чему-то учиться нечего, верни пустой список. '
    'Каждый урок до 300 символов, по-русски; в why коротко — из какого случая он взят.'
)


async def propose(service, cases):
    """Ask the model for lessons; store them as «proposed». Returns the new rows."""
    db = service.store.db
    ensure(db)
    if not cases:
        return []
    existing = active(db) + [r['text'] for r in db.execute("SELECT text FROM lessons WHERE state IN ('proposed','rejected') ORDER BY id DESC LIMIT 60")]
    schema = {'type': 'object', 'additionalProperties': False,
              'properties': {'lessons': {'type': 'array', 'maxItems': MAX_PROPOSE, 'items': {
                  'type': 'object', 'additionalProperties': False,
                  'properties': {'text': {'type': 'string', 'maxLength': 320}, 'why': {'type': 'string', 'maxLength': 200}},
                  'required': ['text', 'why']}}},
              'required': ['lessons']}
    service.charge_call('learn')
    result = await service.ai.request('responses', json={
        'model': service.ai.model, 'store': False, 'instructions': INSTRUCTIONS,
        'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': json.dumps(
            {'existing_lessons': existing, 'cases': cases}, ensure_ascii=False)}]}],
        'max_output_tokens': 1500,
        'text': {'format': {'type': 'json_schema', 'name': 'lessons', 'strict': True, 'schema': schema}},
    })
    chunks = [c['text'] for item in result.get('output', []) if item.get('type') == 'message'
              for c in item.get('content', []) if c.get('type') == 'output_text']
    value = json.loads(''.join(chunks) or '{}')
    from .privacy import sensitive
    seen = {x.casefold() for x in existing}
    new = []
    for item in value.get('lessons') or []:
        text = ' '.join(str(item.get('text') or '').split())
        if len(text) < 15 or sensitive(text) or text.casefold() in seen:
            continue
        if re.search(r'\d{2,}\s*(?:₽|руб)', text):
            continue  # prices come only from the catalog
        cur = db.execute("INSERT INTO lessons(text,why,state,created) VALUES (?,?,'proposed',?)",
                         (text, ' '.join(str(item.get('why') or '').split())[:200], time.time()))
        new.append({'id': cur.lastrowid, 'text': text, 'why': item.get('why') or ''})
        seen.add(text.casefold())
    mark_seen(db, [c['draft_id'] for c in cases])
    return new


def add_proposals(db, items):
    """Store ready lesson texts (e.g. from the evening review) as proposals; duplicates are skipped."""
    ensure(db)
    known = {r['text'].casefold() for r in db.execute('SELECT text FROM lessons')}
    new = []
    for item in items or ():
        text = ' '.join(str(item.get('text') or '').split())[:320]
        if len(text) < 15 or text.casefold() in known:
            continue
        cur = db.execute("INSERT INTO lessons(text,why,state,created) VALUES (?,?,'proposed',?)",
                         (text, str(item.get('why') or '')[:200], time.time()))
        new.append({'id': cur.lastrowid, 'text': text, 'why': item.get('why') or ''})
        known.add(text.casefold())
    return new


def decide(db, lesson_id, ok):
    ensure(db)
    db.execute("UPDATE lessons SET state=?, decided=? WHERE id=? AND state='proposed'",
               ('active' if ok else 'rejected', time.time(), int(lesson_id)))
    if ok:
        extra = db.execute("SELECT id FROM lessons WHERE state='active' ORDER BY id DESC LIMIT -1 OFFSET ?", (MAX_ACTIVE,)).fetchall()
        for row in extra:
            db.execute("UPDATE lessons SET state='retired' WHERE id=?", (row['id'],))


def remove(db, lesson_id):
    ensure(db)
    db.execute("UPDATE lessons SET state='removed', decided=? WHERE id=? AND state='active'", (time.time(), int(lesson_id)))


def edit(db, lesson_id, text):
    ensure(db)
    text = ' '.join(str(text or '').split())[:320]
    if len(text) < 10:
        return False
    return db.execute("UPDATE lessons SET text=?, state='active', decided=? WHERE id=? AND state IN ('proposed','active')",
                      (text, time.time(), int(lesson_id))).rowcount > 0


def card(row):
    return ('🎓 Предложенный урок №%d\n\n%s\n\nОткуда: %s\n\nУтвердите — и помощник будет так отвечать. '
            'Исправить: /lesson %d новый текст' % (row['id'], row['text'], row.get('why') or '—', row['id']),
            [[('✅ Утвердить', 'lesson:ok:%d' % row['id']), ('❌ Не нужно', 'lesson:no:%d' % row['id'])]])


def listing(db):
    ensure(db)
    rows = db.execute("SELECT id,text FROM lessons WHERE state='active' ORDER BY id").fetchall()
    pending = db.execute("SELECT count(*) FROM lessons WHERE state='proposed'").fetchone()[0]
    if not rows:
        text = '🎓 Утверждённых уроков пока нет.'
    else:
        text = '🎓 Уроки помощника (%d):\n\n' % len(rows) + '\n'.join('№%d. %s' % (r['id'], r['text']) for r in rows)
    text += '\n\nЖдут вашего решения: %d. /learn — разобрать новые диалоги сейчас.' % pending
    buttons = [[('🗑 Убрать №%d' % r['id'], 'lesson:del:%d' % r['id'])] for r in rows[-12:]]
    return text[:4000], buttons
