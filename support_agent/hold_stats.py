"""Owner command /holds: why drafts were held in the last days, grouped by reason."""
import json
import re
import time
from collections import Counter, defaultdict


def _reason(evidence):
    text = str(evidence.get('auto_block_reason') or evidence.get('handoff_reason')
               or evidence.get('diagnostic') or 'причина не записана')
    text = re.sub(r'^Этап: [^.]*\.\s*(?:Причина:\s*)?', '', text).strip()
    text = re.sub(r'\d+', 'N', text)
    return text.split('. ')[0][:160]


def summary(db, days=7, now=None):
    since = (now or time.time()) - days * 86400
    rows = db.execute('SELECT d.id, d.chat, d.state, d.handoff, e.data FROM drafts d '
                      'LEFT JOIN draft_evidence e ON e.draft = d.id WHERE d.created >= ?', (since,)).fetchall()
    total = len(rows)
    held = Counter()
    chats = defaultdict(set)
    for row in rows:
        if not row['handoff']:
            continue
        try:
            evidence = json.loads(row['data'] or '{}')
        except ValueError:
            evidence = {}
        reason = _reason(evidence if isinstance(evidence, dict) else {})
        held[reason] += 1
        chats[reason].add(row['chat'])
    lines = [f'Остановки за {days} дн.: {sum(held.values())} из {total} черновиков.']
    if not held:
        lines.append('Остановок не было.')
    for reason, count in held.most_common(12):
        lines.append(f'• {count} — {reason} (чатов: {len(chats[reason])})')
    return '\n'.join(lines)
