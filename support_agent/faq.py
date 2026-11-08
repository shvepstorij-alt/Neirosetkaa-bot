"""Owner answers for topics the catalogue does not settle. Empty means hold."""
from pathlib import Path

import yaml


_PATH = Path(__file__).with_name('data') / 'faq.yaml'
_PLACEHOLDERS = {'', '...', 'todo', 'заполнит владелец'}


def load_faq():
    raw = yaml.safe_load(_PATH.read_text(encoding='utf-8')) or {}
    topics = []
    seen = set()
    for row in raw.get('topics') or []:
        if not isinstance(row, dict):
            continue
        topic_id = str(row.get('id') or '').strip()
        title = ' '.join(str(row.get('title') or '').split())
        answer = str(row.get('answer') or '').strip()
        if not topic_id or topic_id in seen or topic_id == 'none':
            continue
        seen.add(topic_id)
        topics.append({'id': topic_id, 'title': title or topic_id, 'answer': answer})
    return {'topics': topics}


def usable_answer(topic_id, topics=None):
    """The owner's words for this topic, or an empty string when he has not written them."""
    rows = topics if topics is not None else load_faq()['topics']
    for row in rows:
        if row.get('id') == topic_id:
            answer = str(row.get('answer') or '').strip()
            if answer.casefold() in _PLACEHOLDERS:
                return ''
            return answer
    return ''


def for_context():
    rows = []
    for row in load_faq()['topics']:
        answer = usable_answer(row['id'], [row])
        rows.append({'id': row['id'], 'title': row['title'], 'answer': answer, 'known': bool(answer)})
    return rows
