"""/stats and /review — how the owner used the drafts (owner 16.11.2026).

A draft counts as:
- «отправлен как есть» — sent with the button;
- «вы ответили сами» — not sent, and the owner wrote to that chat after the draft;
- «без действия» — neither (the client wrote again, or nobody answered yet).
/stats groups this by topic, so autoreplies can be turned on topic by topic.
/review sends one file with every draft the owner replaced, next to his own answer.
"""
import json
import time
from collections import Counter, defaultdict

TOPIC_NAMES = {'none': 'общие вопросы', 'paid_notice': 'сообщение «Оплатил заказ»', 'payment': 'как оплатить, тарифы, цены'}


def _topic(evidence, text):
    if not isinstance(evidence, dict):
        return 'none'
    if evidence.get('paid_notice'):
        return 'paid_notice'
    topic = str(evidence.get('owner_topic') or evidence.get('owner_followup_topic') or '')
    if topic and topic != 'none':
        return topic
    if '«Каталог»' in str(text or '') or '₽' in str(text or ''):
        return 'payment'
    return 'none'


def _rows(db, days, now=None):
    since = (now or time.time()) - days * 86400
    rows = db.execute('SELECT d.id, d.chat, d.mid, d.text, d.state, d.handoff, d.created, e.data FROM drafts d '
                      'LEFT JOIN draft_evidence e ON e.draft = d.id WHERE d.created >= ? ORDER BY d.created', (since,)).fetchall()
    out = []
    for row in rows:
        try:
            evidence = json.loads(row['data'] or '{}')
        except ValueError:
            evidence = {}
        owner = db.execute("SELECT text FROM messages WHERE chat=? AND role='owner' AND at>=? ORDER BY at LIMIT 3",
                           (row['chat'], row['created'])).fetchall()
        client = db.execute("SELECT text FROM messages WHERE chat=? AND role='client' AND mid<=? ORDER BY mid DESC LIMIT 3",
                            (row['chat'], row['mid'])).fetchall()
        if row['state'] == 'sent' and isinstance(evidence, dict) and evidence.get('owner_edited'):
            outcome = 'replaced'
            owner = [{'text': row['text']}]
        elif row['state'] == 'sent':
            outcome = 'sent'
        elif owner:
            outcome = 'replaced'
        else:
            outcome = 'idle'
        out.append({'id': row['id'], 'topic': _topic(evidence, row['text']), 'outcome': outcome, 'held': bool(row['handoff']),
                    'draft': (evidence.get('original_text') if isinstance(evidence, dict) else None) or row['text'] or '', 'owner': '\n'.join(r['text'] or '' for r in owner),
                    'client': '\n'.join(r['text'] or '' for r in reversed(client))})
    return out


def _name(topic, faq_titles):
    return TOPIC_NAMES.get(topic) or faq_titles.get(topic, topic)[:60]


def summary(db, days=7, now=None):
    rows = _rows(db, days, now)
    try:
        from .faq import load_faq
        titles = {t['id']: t['title'] for t in load_faq()['topics']}
    except Exception:
        titles = {}
    by = defaultdict(Counter)
    for row in rows:
        by[row['topic']][row['outcome']] += 1
    total = Counter(row['outcome'] for row in rows)
    lines = [f'Черновики за {days} дн.: {len(rows)}. Отправлено как есть: {total["sent"]}, '
             f'исправили или ответили сами: {total["replaced"]}, без действия: {total["idle"]}.', '',
             'По темам (как есть / исправлено / без действия):']
    order = sorted(by.items(), key=lambda item: -sum(item[1].values()))
    for topic, count in order[:20]:
        decided = count['sent'] + count['replaced']
        share = f' — {round(100 * count["sent"] / decided)}% без правки' if decided >= 3 else ''
        ready = ' ✅ можно в автоответ' if decided >= 10 and count['sent'] >= 0.9 * decided else ''
        lines.append(f'• {_name(topic, titles)}: {count["sent"]} / {count["replaced"]} / {count["idle"]}{share}{ready}')
    lines += ['', 'Тема готова к автоответу, когда в ней ≥10 решённых черновиков и ≥90% отправлены без правки.',
              '/review — файл с черновиками, которые вы исправили или заменили.']
    return '\n'.join(lines)


def review_file(db, days=7, now=None):
    rows = [row for row in _rows(db, days, now) if row['outcome'] == 'replaced']
    blocks = []
    for row in rows:
        blocks.append('\n'.join([f'#R{row["id"]}  [{row["topic"]}]' + ('  (стоп)' if row['held'] else ''),
                                 'Клиент: ' + row['client'], 'Черновик помощника: ' + row['draft'],
                                 'Вы ответили: ' + row['owner']]))
    return f'Черновики, которые вы исправили или заменили, за {days} дн.: {len(rows)}\n\n' + '\n\n'.join(blocks)
