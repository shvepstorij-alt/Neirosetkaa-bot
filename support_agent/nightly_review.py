"""Owner 24.11.2026: «вечерняя автопроверка всех ответов помощника — профессиональная машина самообучения».

Every evening (23:30 by the owner's clock, or on /night) the assistant reviews every answer it gave clients that
day, like a senior support lead would:
- what the client really needed, whether the answer covered every question, had the right steps and facts by the
  owner's rules and the catalog, whether it should have asked a clarifying question first, handed over to
  Alexander, or not handed over;
- a better answer for every weak one;
- general lessons («Когда клиент …, отвечай …»).
Alexander gets a short summary, a file with every weak answer and its better version, and lesson cards. Only lessons
he approves change the assistant (learning.py). Nothing is sent to clients by this review.
"""
import collections
import datetime as dt
import json
import time

LOCAL = dt.timezone(dt.timedelta(hours=5))  # Asia/Qyzylorda, the owner's clock
RUN_AT = (23, 30)
MAX_ITEMS = 60
BATCH = 6
VERDICTS = {'good': 'хорошо', 'improve': 'можно лучше', 'should_clarify': 'нужно было уточнить',
            'wrong': 'неверно', 'harmful': 'вредно'}
CATEGORIES = {'none': '—', 'missed_question': 'пропущен вопрос клиента', 'wrong_steps': 'неверные или неполные шаги',
              'wrong_fact': 'неверный факт или правило', 'price': 'цена или тариф', 'should_clarify': 'нужно было уточнить',
              'needless_question': 'лишний вопрос', 'should_handoff': 'нужно было передать Александру',
              'needless_handoff': 'лишняя передача Александру', 'lost_context': 'потерян контекст диалога',
              'too_long': 'слишком длинно', 'too_vague': 'слишком общо', 'tone': 'тон', 'language': 'язык',
              'screenshot': 'не разобран скриншот', 'no_followup': 'не задан вопрос, ведущий к следующему шагу',
              'double_greeting': 'повтор приветствия или склеенные шаблоны', 'other': 'другое'}

INSTRUCTIONS = (
    'Ты старший специалист поддержки магазина подписок на нейросети (бот @neirosetkaa_bot, владелец Александр). '
    'Вечером ты проверяешь все ответы ИИ-помощника клиентам за день, чтобы помощник учился. '
    'Для каждого случая дано: переписка до ответа (history), новые сообщения клиента (client), ответ помощника (assistant), '
    'ушёл ли он клиенту (sent), что потом написали Александр (alexander_after) и клиент (client_after). '
    'Оцени строго, но честно, по правилам Александра и каталогу ниже:\n'
    'verdict: good — ответ полный, верный, по шагам, по правилам; improve — по делу, но можно заметно лучше; '
    'should_clarify — сначала нужно было задать уточняющий вопрос (неясно, какой сервис, тариф, устройство или что не работает); '
    'wrong — не тот ответ, неверные шаги или правило, пропущен главный вопрос; '
    'harmful — выдуманный факт, цена, обещание, результат («активировано», «оплата прошла»), просьба данных до оплаты.\n'
    'Учитывай: помощник не умеет сам активировать, проверять оплату и возвращать деньги — для этого верно собрать данные и '
    'передать Александру. Если Александр потом написал клиенту — его ответ главный ориентир, как надо было. '
    'Если клиент потом написал «не понял», «не то», переспросил — ответ был слабым. '
    'Цены — только из каталога ниже; цена из каталога — не ошибка.\n'
    'Хороший ответ владелец описал так: дружелюбно, подробно по шагам и в конце один уточняющий вопрос, который держит клиента '
    'в диалоге и ведёт к следующему шагу (если следующий шаг зависит от клиента). Повтор приветствия посреди ответа и склеенные '
    'шаблоны (каталог + «Как оформить» + инструкции, когда клиент уже оплатил) — ошибка double_greeting.\n'
    'problem — одна короткая фраза, что не так (для good пусто). '
    'better_reply — как надо было ответить клиенту, готовым текстом от лица помощника, на «вы», мужской род, '
    'кратко и по шагам; для good пусто. Не обещай сроков и результата, не проси данные для активации до оплаты. '
    'lesson — если из случая следует ОБЩЕЕ правило на будущее, сформулируй его: «Когда клиент …, отвечай …» '
    '(без имён, цен, почт, номеров); иначе пусто.'
)


def due(now=None, last_day=''):
    local = dt.datetime.fromtimestamp(now or time.time(), LOCAL)
    return (local.hour, local.minute) >= RUN_AT and local.date().isoformat() != last_day


def _clean(text, limit=900):
    from .privacy import scrub, sensitive
    text = str(text or '').strip()
    if sensitive(text):
        return '[скрыто: данные доступа]'
    return scrub(text)[:limit]


def _clean_reply(text, limit=3800):
    """The assistant's own reply is shown whole; only a written-out secret value is hidden."""
    from .privacy import reply_leaks_secret, scrub
    text = str(text or '').strip()
    return '[скрыто: данные доступа]' if reply_leaks_secret(text) else scrub(text)[:limit]


def collect(db, since, limit=MAX_ITEMS):
    from .turn_understanding import with_picture
    rows = db.execute('''SELECT d.id, d.chat, d.mid, d.text, d.state, d.created, e.data FROM drafts d
        LEFT JOIN draft_evidence e ON e.draft=d.id WHERE d.created>=? ORDER BY d.id''', (since,)).fetchall()
    items = []
    for row in rows:
        text = str(row['text'] or '').strip()
        if not text or (text.startswith('Александр ответит здесь') and len(text) < 160):
            continue  # holding notices are reviewed through the answer they replaced
        if row['state'] not in ('sent', 'ready', 'stale'):
            continue
        before = db.execute("SELECT role,text,media FROM messages WHERE chat=? AND mid<=? ORDER BY mid DESC LIMIT 10",
                            (row['chat'], row['mid'])).fetchall()[::-1]
        client, history = [], []
        for m in reversed(before):
            if m['role'] == 'client' and not history:
                client.insert(0, _clean(with_picture(m['text'], m['media']), 600))
            else:
                history.insert(0, [m['role'], _clean(with_picture(m['text'], m['media']), 400)])
        after = db.execute("SELECT role,text FROM messages WHERE chat=? AND at>? AND at<=? AND role IN ('owner','client') ORDER BY at LIMIT 4",
                           (row['chat'], row['created'], row['created'] + 1800)).fetchall()
        items.append({'id': row['id'], 'chat': row['chat'], 'history': history[-6:], 'client': '\n'.join(client),
                      'assistant': _clean_reply(text, 3800), 'sent': row['state'] == 'sent',
                      'alexander_after': _clean('\n'.join(a['text'] or '' for a in after if a['role'] == 'owner'), 700),
                      'client_after': _clean('\n'.join(a['text'] or '' for a in after if a['role'] == 'client'), 400)})
    # The newest answers first if the day was busy; every client gets at least one reviewed answer.
    if len(items) > limit:
        by_chat = collections.OrderedDict()
        for item in reversed(items):
            by_chat.setdefault(item['chat'], []).append(item)
        picked = [rows[0] for rows in by_chat.values()][:limit]
        rest = [i for i in reversed(items) if i not in picked]
        items = (picked + rest)[:limit]
    return items


async def _rules(service):
    parts = []
    try:
        from .playbook import business_profile, load as load_playbook
        from .faq import load_faq
        parts.append('ПРОФИЛЬ МАГАЗИНА (самые свежие решения владельца):\n' + business_profile())
        parts.append('ОТВЕТЫ АЛЕКСАНДРА ПО ТЕМАМ:\n' + '\n'.join(
            '- ' + row['title'] + ': ' + str(row.get('answer') or '')[:350] for row in load_faq()['topics'] if row.get('answer'))[:26000])
        parts.append('СИТУАЦИИ:\n' + '\n'.join('- ' + r['title'] + ': ' + r['rule'] for r in load_playbook().values()))
    except Exception:
        pass
    try:
        from .learning import active
        lessons = active(service.store.db)
        if lessons:
            parts.append('УТВЕРЖДЁННЫЕ УРОКИ:\n' + '\n'.join('- ' + x for x in lessons))
    except Exception:
        pass
    try:
        from .catalog_cache import load_catalog
        facts, _ = await load_catalog(service.catalog_cache, service.shop, service.owner)
        rows = [r for r in (facts or {}).get('catalog') or [] if isinstance(r, dict)]
        parts.append('КАТАЛОГ (цены в ₽):\n' + '\n'.join('- %s %s — %s' % (r.get('service'), r.get('plan'), r.get('catalog_price_rub'))
                                                         for r in rows if r.get('catalog_price_rub'))[:6000])
    except Exception:
        parts.append('КАТАЛОГ: не загружен.')
    return '\n\n'.join(parts)


async def review(service, items):
    schema = {'type': 'object', 'additionalProperties': False, 'properties': {'items': {'type': 'array', 'items': {
        'type': 'object', 'additionalProperties': False,
        'properties': {'id': {'type': 'integer'}, 'verdict': {'type': 'string', 'enum': list(VERDICTS)},
                       'category': {'type': 'string', 'enum': list(CATEGORIES)},
                       'problem': {'type': 'string', 'maxLength': 250}, 'better_reply': {'type': 'string', 'maxLength': 1500},
                       'lesson': {'type': 'string', 'maxLength': 320}},
        'required': ['id', 'verdict', 'category', 'problem', 'better_reply', 'lesson']}}}, 'required': ['items']}
    instructions = INSTRUCTIONS + '\n\n' + await _rules(service)
    out = {}
    for start in range(0, len(items), BATCH):
        chunk = items[start:start + BATCH]
        payload = [{k: v for k, v in item.items() if k != 'chat'} for item in chunk]
        try:
            service.charge_call('review')
            result = await service.ai.request('responses', json={
                'model': service.ai.model, 'store': False, 'instructions': instructions,
                'input': [{'role': 'user', 'content': [{'type': 'input_text', 'text': json.dumps(payload, ensure_ascii=False)}]}],
                'max_output_tokens': 4000,
                'text': {'format': {'type': 'json_schema', 'name': 'night_review', 'strict': True, 'schema': schema}},
            })
            chunks = [c['text'] for item in result.get('output', []) if item.get('type') == 'message'
                      for c in item.get('content', []) if c.get('type') == 'output_text']
            for row in json.loads(''.join(chunks) or '{}').get('items') or []:
                if row.get('id') in {i['id'] for i in chunk}:
                    out[row['id']] = row
        except ValueError:
            raise  # daily limit: stop, report what was reviewed
        except Exception:
            continue
    return out


def report_text(items, grades, day):
    counts = collections.Counter(g['verdict'] for g in grades.values())
    cats = collections.Counter(g['category'] for g in grades.values() if g['verdict'] != 'good' and g['category'] != 'none')
    lines = ['🌙 Вечерний разбор ответов за %s' % day, '',
             'Проверено ответов: %d из %d' % (len(grades), len(items))]
    lines += ['• %s — %d' % (VERDICTS[k], counts[k]) for k in VERDICTS if counts.get(k)]
    if cats:
        lines += ['', 'Главные проблемы:'] + ['• %s — %d' % (CATEGORIES.get(k, k), n) for k, n in cats.most_common(5)]
    good = counts.get('good', 0)
    if grades:
        lines += ['', 'Качество: %d%% ответов без замечаний.' % round(100 * good / len(grades))]
    return '\n'.join(lines)


def report_file(items, grades, day):
    order = ['harmful', 'wrong', 'should_clarify', 'improve']
    rows = [(i, grades[i['id']]) for i in items if i['id'] in grades and grades[i['id']]['verdict'] != 'good']
    rows.sort(key=lambda pair: order.index(pair[1]['verdict']) if pair[1]['verdict'] in order else 9)
    out = ['Вечерний разбор %s. Слабые ответы помощника и как надо было ответить.' % day, '']
    for item, g in rows:
        out += ['=' * 60, '№%d [%s · %s]%s' % (item['id'], VERDICTS.get(g['verdict']), CATEGORIES.get(g['category'], g['category']),
                                              '' if item['sent'] else ' (черновик, клиенту не ушёл)'),
                'Клиент: ' + item['client'], '', 'Помощник: ' + item['assistant'], '', 'Что не так: ' + g['problem']]
        if g.get('better_reply'):
            out += ['', 'Как надо было: ' + g['better_reply']]
        if item.get('alexander_after'):
            out += ['', 'Александр потом написал: ' + item['alexander_after']]
        if g.get('lesson'):
            out += ['', 'Урок: ' + g['lesson']]
        out.append('')
    if len(out) == 2:
        out.append('Замечаний нет.')
    return '\n'.join(out)


def save(db, grades, day):
    db.execute('''CREATE TABLE IF NOT EXISTS night_review(draft INTEGER PRIMARY KEY, day TEXT, verdict TEXT,
                  category TEXT, problem TEXT, better TEXT, lesson TEXT)''')
    for did, g in grades.items():
        db.execute('INSERT OR REPLACE INTO night_review VALUES (?,?,?,?,?,?,?)',
                   (did, day, g['verdict'], g['category'], g['problem'], g.get('better_reply', ''), g.get('lesson', '')))


def lesson_candidates(grades, limit=5):
    """Lessons from the worst answers first; the same lesson twice is one proposal."""
    from .privacy import sensitive
    order = {'harmful': 0, 'wrong': 1, 'should_clarify': 2, 'improve': 3, 'good': 4}
    seen, out = set(), []
    for g in sorted(grades.values(), key=lambda g: order.get(g['verdict'], 9)):
        text = ' '.join(str(g.get('lesson') or '').split())
        key = text.casefold()[:80]
        if len(text) < 15 or key in seen or sensitive(text):
            continue
        import re
        if re.search(r'\d{2,}\s*(?:₽|руб)', text):
            continue
        seen.add(key)
        out.append({'text': text[:320], 'why': 'вечерний разбор: ' + CATEGORIES.get(g['category'], g['category'])})
        if len(out) >= limit:
            break
    return out
