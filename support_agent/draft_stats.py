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



MEDIA_MARKERS = {'photo': '[клиент прислал скриншот]', 'voice': '[клиент прислал голосовое сообщение]',
                 'document': '[клиент прислал файл]', 'manual_text_file': '[клиент прислал текстовый файл]',
                 'unsupported': '[клиент прислал вложение]'}
LIVE_HISTORY = 15
LIVE_TEXT = 1500


def media_marker(raw, role='client'):
    """Text stand-in for a stored attachment: the re-run cannot see the picture, but must know it was there."""
    try:
        value = json.loads(raw or '{}') if isinstance(raw, str) else (raw or {})
    except ValueError:
        return ''
    marker = MEDIA_MARKERS.get(value.get('kind')) if isinstance(value, dict) else None
    if not marker:
        return ''
    return marker if role == 'client' else marker.replace('клиент', 'Александр' if role == 'owner' else 'помощник')


def _said(row):
    text = str(row['text'] or '').strip()
    marker = media_marker(row['media'], row['role'])
    return ((marker + ' ' + text).strip() if marker else text)[:LIVE_TEXT]


def live_cases(db, days=30, limit=100, now=None):
    """Owner 20.11.2026: every draft the owner corrected or replaced is a permanent check.

    Case = what the client wrote, the conversation before it, and the owner's own answer
    as the reference. /live re-answers them with the current assistant and grades it.

    Fair measurement (21.11.2026): the reference is ONLY Alexander's first reply after the
    client's burst — later messages (after a screenshot, a reactivation, new facts) are kept
    in 'later' for the report and never shown to the grader as the reference. Real times are
    kept ('times', 'burst'), attachments become text markers, media-only bursts are skipped.
    """
    out, seen = [], set()
    for row in _rows(db, days, now):
        if row['outcome'] != 'replaced' or not row['owner'].strip():
            continue
        d = db.execute('SELECT chat, mid, state FROM drafts WHERE id=?', (row['id'],)).fetchone()
        if not d:
            continue
        chat, mid = d['chat'], d['mid']
        before = list(reversed(db.execute('SELECT mid, role, text, media, at FROM messages WHERE chat=? AND mid<=? '
                                          'ORDER BY mid DESC LIMIT ?', (chat, mid, LIVE_HISTORY + 40)).fetchall()))
        burst = []
        while before and before[-1]['role'] == 'client':
            burst.insert(0, before.pop())
        if d['state'] == 'sent':
            # The owner edited the draft and sent it: the edited text is his first reply.
            reference, ref_mid, ref_at = row['owner'].strip(), mid, None
        else:
            first = db.execute("SELECT mid, text, at FROM messages WHERE chat=? AND mid>? AND role='owner' "
                               "AND trim(coalesce(text,''))!='' ORDER BY mid LIMIT 1", (chat, mid)).fetchone()
            if not first:
                continue
            reference, ref_mid, ref_at = str(first['text']).strip(), first['mid'], first['at']
            # Client lines written before Alexander answered are part of what he answered.
            burst += db.execute("SELECT mid, role, text, media, at FROM messages WHERE chat=? AND mid>? AND mid<? "
                                "AND role='client' ORDER BY mid", (chat, mid, ref_mid)).fetchall()
        if (chat, ref_mid) in seen:
            continue
        lines = [(_said(r), r['at']) for r in burst]
        lines = [(text, at) for text, at in lines if text]
        if not lines:
            continue
        seen.add((chat, ref_mid))
        later = db.execute("SELECT text, media, role, at FROM messages WHERE chat=? AND mid>? AND role='owner' "
                           "ORDER BY mid LIMIT 13", (chat, ref_mid)).fetchall()
        note = ' | '.join(' '.join(_said(r).split())[:120] for r in later if _said(r))[:500]
        history = [r for r in before if _said(r)][-LIVE_HISTORY:]
        out.append({'history': [[r['role'] if r['role'] in ('client', 'owner', 'assistant') else 'owner', _said(r)]
                                for r in history],
                    'times': [r['at'] for r in history],
                    'client': '\n'.join(text for text, _ in lines),
                    'burst': [[text, at] for text, at in lines],
                    'client_at': lines[-1][1],
                    'owner': reference[:LIVE_TEXT], 'owner_at': ref_at,
                    'later': note, 'later_count': len(later),
                    'draft_id': row['id']})
    return out[-limit:]
