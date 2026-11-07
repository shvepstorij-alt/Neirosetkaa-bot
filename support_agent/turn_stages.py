"""Small stage helpers: which rules and history a draft needs, and batch deadlines."""
import re
from pathlib import Path

_ROOT = Path(__file__).parent
_ALWAYS = (
    '## Разрешённая работа',
    '## Не раскрывать',
    '## Как обращаться с фактами',
    '## Карты владельца',
    '## Деньги и исключения',
    '## Решения, передаваемые владельцу',
    '## Запрещённые инструменты',
    '## Общение и передача человеку',
    '## Доступность владельца',
)
_OPTIONAL = (
    ('## Клиенты с курса', ('курс', 'бонус', 'куратор', 'обучен')),
    ('## Claude', ('claude', 'клод')),
    ('## Ошибки активации', ('активир', 'токен', 'сесс')),
    ('## Вложения и память', ('фото', 'скрин', 'голос', 'изображ')),
    ('## Посторонние темы', ('политик', 'конкурент')),
)


def select_rules(question, facts=None):
    """Keep secret, order and money rules. Attach the rest only when the question needs them."""
    style = (_ROOT / 'STYLE.md').read_text(encoding='utf-8')
    workflow = (_ROOT / 'WORKFLOW_RULES.md').read_text(encoding='utf-8')
    service = (_ROOT / 'SERVICE_RULES.md').read_text(encoding='utf-8')
    chunks = re.split(r'(?m)^(?=## )', service)
    chosen = [chunks[0]]
    folded = str(question or '').casefold().replace('ё', 'е')
    goals = ' '.join(facts.get('request_goals') or []) if isinstance(facts, dict) else ''
    folded += ' ' + goals
    for chunk in chunks[1:]:
        if chunk.startswith(_ALWAYS):
            chosen.append(chunk)
            continue
        for prefix, keys in _OPTIONAL:
            if chunk.startswith(prefix) and any(key in folded for key in keys):
                chosen.append(chunk)
                break
    return '\n\n'.join(part.strip() for part in (style, workflow, ''.join(chosen)) if part.strip())


def full_rules():
    return '\n\n'.join((_ROOT / name).read_text(encoding='utf-8')
                       for name in ('SERVICE_RULES.md', 'STYLE.md', 'WORKFLOW_RULES.md'))


def relevant_history(history, question, limit=12):
    """A self-contained question does not need the whole stored history."""
    rows = list(history or [])
    if re.search(r'раньше|выше|в прошл|как я писал|продолж', str(question or ''), re.I):
        return rows
    if len(rows) <= limit:
        return rows
    return rows[-limit:]


def next_batch_due(start, now, seconds, existing_due, collecting):
    """New fragments must not move an open batch past its original deadline."""
    seconds = max(0, int(seconds))
    if collecting:
        origin = float(start or 0)
        cap = origin + seconds if origin else float(existing_due or now)
        current = float(existing_due or cap)
        return min(current, cap)
    origin = float(start if start else now)
    return origin + seconds


def call_estimate(route):
    """Static count of model HTTP calls for a route, before a failed check retries.

    Measured from the call sites, not from a live provider. A repeated official
    lookup the same day is served from cache and is not counted again.
    """
    table = {
        'deterministic': 0,
        'catalog_direct': 0,
        'consultation': 2,
        'consultation_rewritten': 4,
        'public_features': 4,
        'public_features_cached': 3,
        'voice_extra': 1,
    }
    if route not in table:
        raise KeyError(route)
    return table[route]
