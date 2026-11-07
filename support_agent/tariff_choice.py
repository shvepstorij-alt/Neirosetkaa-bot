"""Resolve a pending tariff choice against the list just shown.

The model chooses an id from that list. Code checks the id, the catalogue price,
and the general duration rule: a bare name means the monthly plan when both a
monthly and a yearly plan exist. The list is never widened.
"""
import re
from difflib import SequenceMatcher

from .dialog_state import _fold_tokens


_YEAR = re.compile(r'год(?:а|у|ом|овой|овая|овое|овые)?|year|annual|12\s*мес', re.I)
_MONTH = re.compile(r'месяц(?:а|ев)?|месячн\w*|monthly', re.I)
_STOP = {'tarif', 'tarifa', 'plan', 'na', 'i', 'za', 'mne', 'hochu', 'nuzhen', 'nuzhna',
         'podpiska', 'podpisku', 'the', 'a', 'eto', 'menya'}
_INTERNAL = re.compile(
    r'уже\s+известно|недостаточно\s+без|этап\s*:|dialog_state|pending_slot|причина\s*:', re.I)


def is_internal_client_text(text):
    return bool(_INTERNAL.search(str(text or '')))


def yearly(plan):
    return bool(_YEAR.search(str(plan or '')))


def duration_request(text):
    year = bool(_YEAR.search(str(text or '')))
    month = bool(_MONTH.search(str(text or '')))
    if year and not month:
        return 'year'
    if month and not year:
        return 'month'
    return ''


def narrow(candidates, text):
    """Drop plans whose duration contradicts the reply. Never add plans."""
    kind = duration_request(text)
    rows = [row for row in candidates if isinstance(row, dict) and row.get('id')]
    if kind == 'month':
        kept = [row for row in rows if not yearly(row.get('plan'))]
        return kept or rows
    if kind == 'year':
        kept = [row for row in rows if yearly(row.get('plan'))]
        return kept or rows
    return rows


def _content_tokens(text, service=''):
    cleaned = _MONTH.sub(' ', _YEAR.sub(' ', str(text or '')))
    service_tokens = [token for token in _fold_tokens(service) if token not in _STOP]
    kept = []
    for token in _fold_tokens(cleaned):
        if token in _STOP:
            continue
        if service_tokens and max(_hit(token, item) for item in service_tokens) >= 0.7:
            continue
        kept.append(token)
    return kept


def _plan_tokens(plan):
    return [token for token in _fold_tokens(plan) if token not in _STOP | {'na', 'god', 'goda', 'year', 'annual'}]


def _hit(left, right):
    if left == right:
        return 1.0
    if len(left) < 3 or len(right) < 3:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _matches(row, text):
    tokens = _content_tokens(text, row.get('service') or '')
    plan_tokens = _plan_tokens(row.get('plan'))
    if not tokens or not plan_tokens:
        return False
    for token in tokens:
        if max(_hit(token, plan) for plan in plan_tokens) < 0.70:
            return False
    return True


def _monthly_default(rows, text):
    """Bare «Pro» is the monthly plan when a yearly twin is also in the list."""
    if duration_request(text):
        return rows
    plain = [row for row in rows if not yearly(row.get('plan'))]
    yearly_rows = [row for row in rows if yearly(row.get('plan'))]
    if not plain or not yearly_rows:
        return rows
    plain_cores = {tuple(_plan_tokens(row.get('plan'))[:1]) for row in plain}
    yearly_cores = {tuple(_plan_tokens(row.get('plan'))[:1]) for row in yearly_rows}
    if plain_cores & yearly_cores:
        return plain
    return rows


def suggest(candidates, text):
    """General reading of one reply. Not a per-service synonym list."""
    narrowed = narrow(candidates, text)
    matched = [row for row in narrowed if _matches(row, text)]
    matched = _monthly_default(matched or narrowed, text) if matched else []
    if len(matched) == 1:
        return {'decision': 'chosen', 'tariff_id': matched[0]['id'], 'row': matched[0],
                'candidates': narrowed, 'reason': 'один тариф из показанного списка'}
    if matched:
        return {'decision': 'ambiguous', 'tariff_id': '', 'row': None, 'candidates': matched,
                'reason': 'несколько тарифов из показанного списка ещё подходят'}
    if _content_tokens(text) or duration_request(text):
        return {'decision': 'new_topic', 'tariff_id': '', 'row': None, 'candidates': narrowed,
                'reason': 'ответ не выбирает тариф из показанного списка'}
    return {'decision': 'ambiguous', 'tariff_id': '', 'row': None, 'candidates': narrowed,
            'reason': 'тариф из показанного списка не распознан'}


def _by_id(candidates):
    return {row.get('id'): row for row in candidates if row.get('id')}


def enforce(choice, candidates, text):
    """Reject an id outside the list and apply the monthly default to a model choice."""
    if not isinstance(choice, dict):
        return None
    allowed = _by_id(narrow(candidates, text))
    if choice.get('decision') == 'new_topic':
        return {'decision': 'new_topic', 'tariff_id': '', 'row': None, 'candidates': list(allowed.values()),
                'reason': str(choice.get('reason') or '')[:300]}
    if choice.get('decision') == 'ambiguous':
        ids = choice.get('tariff_ids') or []
        rows = [allowed[item] for item in ids if item in allowed] or list(allowed.values())
        rows = _monthly_default(rows, text)
        if len(rows) == 1:
            return {'decision': 'chosen', 'tariff_id': rows[0]['id'], 'row': rows[0],
                    'candidates': rows, 'reason': 'после правила срока остался один тариф'}
        return {'decision': 'ambiguous', 'tariff_id': '', 'row': None, 'candidates': rows,
                'reason': str(choice.get('reason') or '')[:300]}
    row = allowed.get(choice.get('tariff_id'))
    if not row:
        return None
    if _content_tokens(text, row.get('service') or '') and not _matches(row, text):
        return None
    prefer = _monthly_default([row] + [item for item in allowed.values() if item is not row], text)
    # Only replace a yearly pick when the client did not ask for a year and the
    # monthly twin is still in the list that was shown.
    if row in prefer or duration_request(text) == 'year':
        chosen = row
    else:
        twins = [item for item in prefer if _plan_tokens(item.get('plan'))[:1] == _plan_tokens(row.get('plan'))[:1]]
        chosen = twins[0] if len(twins) == 1 else row
    return {'decision': 'chosen', 'tariff_id': chosen['id'], 'row': chosen,
            'candidates': list(allowed.values()), 'reason': str(choice.get('reason') or '')[:300]}


async def resolve_selection(ai, candidates, text):
    narrowed = narrow(candidates, text)
    if ai is not None and hasattr(ai, 'choose_tariff'):
        try:
            raw = await ai.choose_tariff(narrowed, text)
        except Exception:
            raw = None
        checked = enforce(raw, narrowed, text) if raw else None
        local = suggest(narrowed, text)
        if checked and checked['decision'] == 'chosen':
            return checked
        if local['decision'] == 'chosen':
            return local
        if checked:
            return checked
        return local
    return suggest(narrowed, text)


def confirmation(row, facts):
    from .bot_guide import purchase_navigation
    from .situation import format_offer
    price = row.get('price')
    if type(price) is not int:
        return None
    title = format_offer(row.get('service'), row.get('plan'))
    navigation = purchase_navigation(facts or {})
    steps = navigation or 'Откройте бота магазина, раздел каталога, выберите этот сервис и тариф, затем способ оплаты.'
    text = (f'{title}: {price} ₽\n\n{steps}\n'
            'После оплаты откройте сообщение заказа и следуйте инструкции. '
            'Оплата сама по себе не активирует подписку.')
    return {'reply': text, 'summary': 'Тариф выбран из показанного списка. Цена взята из каталога.',
            'handoff': False, 'source_ids': [],
            'evidence': {'status': 'catalog_direct', 'sources': [],
                         'diagnostic': 'Этап: выбор тарифа. id=' + str(row.get('id')) + '; цена из каталога.'}}


def narrowed_question(candidates):
    from .situation import format_offer
    if not candidates:
        return None
    lines = [f"{format_offer(row.get('service'), row.get('plan'))}: {row.get('price')} ₽" for row in candidates]
    text = 'Подходят такие тарифы:\n' + '\n'.join(lines) + '\n\nКакой из этих тарифов выбрать?'
    return {'reply': text, 'summary': 'Список сужен до тарифов, которые ещё подходят. Новые тарифы не добавлены.',
            'handoff': False, 'source_ids': [],
            'evidence': {'status': 'clarification', 'sources': [],
                         'diagnostic': 'Этап: выбор тарифа. В списке осталось ' + str(len(candidates)) + '.'}}


def safe_client_reply(answer):
    if not isinstance(answer, dict):
        return answer
    if not is_internal_client_text(answer.get('reply')):
        return answer
    replaced = dict(answer)
    replaced['reply'] = 'Напишите, пожалуйста, название тарифа из последнего списка или задайте вопрос заново.'
    replaced['summary'] = 'Служебная формулировка клиенту не отправлена.'
    return replaced
