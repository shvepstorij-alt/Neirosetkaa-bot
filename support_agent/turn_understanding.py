"""One structured reading of the current turn.

The model decides intent, service and tariff. Code only checks those ids against
the catalogue, checks prices in the draft, and decides whether the draft states
a result that the owner must confirm. Keyword hints are not decisions.
"""
import re

from .dialog_state import load, save
from .reply_assertion import asserts_outcome, collects_case_data


INTENTS = ('price', 'how_to', 'list_options', 'choose_tariff', 'capabilities',
           'payment_issue', 'other')
CLASSES = ('collect', 'instruct', 'catalog_price', 'outcome', 'promise', 'unverified')
_RUBLE = re.compile(r'(\d{2,7})\s*(?:₽|руб(?:лей|ля|ль)?)', re.I)
_PROMISE = re.compile(r'(?i)обещаю|гарантирую|точно\s+активир|возврат\s+одобрен|уже\s+оформил')
_INTERNAL = re.compile(
    r'(?i)уже\s+известно|недостаточно\s+без|результат\s+операции\s+пока\s+не\s+подтвержд|'
    r'часть\s+условий\s+пока\s+не\s+подтверждена|этап\s*:')


def burst_and_window(db, chat, limit=10):
    rows = db.execute(
        '''SELECT mid, role, text FROM messages
           WHERE chat=? AND role IN ('client','assistant','owner')
           ORDER BY mid DESC LIMIT ?''', (chat, limit)).fetchall()
    window = [{'role': row['role'], 'text': row['text'] or ''} for row in reversed(rows)]
    burst = []
    for message in reversed(window):
        if message['role'] in ('assistant', 'owner') and burst:
            break
        if message['role'] == 'client':
            burst.append(message['text'])
    burst.reverse()
    return '\n'.join(part for part in burst if part).strip(), window


def catalog_index(facts, limit=120):
    rows = []
    for row in (facts or {}).get('catalog') or []:
        if not isinstance(row, dict):
            continue
        price = row.get('catalog_price_rub')
        if type(price) is not int or price <= 0:
            continue
        rows.append({'id': str(row.get('service_key') or '') + '|' + str(row.get('plan') or ''),
                     'service_id': str(row.get('service_key') or ''),
                     'service': str(row.get('service') or ''),
                     'plan': str(row.get('plan') or ''),
                     'price': price})
        if len(rows) >= limit:
            break
    return rows


def nominal_index(facts, limit=80):
    rows = []
    for region in ((facts or {}).get('appstore') or {}).get('regions') or []:
        if not isinstance(region, dict):
            continue
        for denom in region.get('denoms') or []:
            price = denom.get('price_rub') if isinstance(denom, dict) else None
            if type(price) is not int or price <= 0:
                continue
            rows.append({'region': str(region.get('name') or ''),
                         'nominal': str(denom.get('nominal') or ''),
                         'price': price})
            if len(rows) >= limit:
                return rows
    return rows


def hint_services(text, catalog):
    """Untrusted hints only. The model may ignore them."""
    from .catalog_context import service_mentions
    found = service_mentions(text or '', catalog or [])
    return [{'service_id': key, 'label': label} for key, label in found.items()]


def build_context(db, chat, facts):
    burst, window = burst_and_window(db, chat)
    state = load(db, chat)
    catalog = (facts or {}).get('catalog') or []
    tariffs = catalog_index(facts)
    from .payment_case import receipt_files
    return {'burst': burst,
            'receipt_attached': bool(receipt_files(db, chat)),
            'history': window,
            'dialog_state': {'service_id': state.get('service_key') or '',
                             'service': state.get('service_label') or '',
                             'tariff': state.get('option_label') or '',
                             'collected': state.get('collected') or {},
                             'shown': state.get('offered') or []},
            'tariffs': tariffs,
            'nominals': nominal_index(facts),
            'hints': hint_services(burst, catalog)}


def _allowed_prices(tariffs, nominals, service_id, tariff_id):
    if tariff_id:
        return {row['price'] for row in tariffs if row['id'] == tariff_id}
    nominal_prices = {row['price'] for row in nominals}
    if service_id in ('', 'appstore'):
        service_prices = {row['price'] for row in tariffs if row['service_id'] == service_id} if service_id else set()
        return nominal_prices | service_prices or nominal_prices or {row['price'] for row in tariffs}
    return {row['price'] for row in tariffs if row['service_id'] == service_id}


def validate(understanding, context):
    """Drop ids that are not in the catalogue. Do not invent a price."""
    tariffs = context.get('tariffs') or []
    by_tariff = {row['id']: row for row in tariffs}
    services = {row['service_id'] for row in tariffs}
    service_id = str((understanding or {}).get('service_id') or '')
    tariff_id = str((understanding or {}).get('tariff_id') or '')
    if service_id not in services:
        service_id = ''
    chosen = by_tariff.get(tariff_id)
    if not chosen:
        tariff_id = ''
    elif service_id and chosen['service_id'] != service_id:
        tariff_id = ''
        chosen = None
    elif not service_id and chosen:
        service_id = chosen['service_id']
    return {'intent': understanding.get('intent') if understanding.get('intent') in INTENTS else 'other',
            'service_id': service_id,
            'tariff_id': tariff_id,
            'tariff': chosen,
            'topic_changed': bool(understanding.get('topic_changed')),
            'known_facts': [str(item) for item in understanding.get('known_facts') or [] if isinstance(item, str)][:12],
            'missing_facts': [str(item) for item in understanding.get('missing_facts') or [] if isinstance(item, str)][:12],
            'allowed_prices': _allowed_prices(tariffs, context.get('nominals') or [], service_id, tariff_id)}


def classify_draft(reply, allowed_prices):
    """Safe or hold, from what the draft asserts. Client wording is not an input."""
    text = str(reply or '')
    if not text.strip() or _INTERNAL.search(text):
        return {'send': False, 'class': 'unverified',
                'reason': 'Ответ пустой или содержит служебную формулировку.'}
    if _PROMISE.search(text) or asserts_outcome(text):
        return {'send': False, 'class': 'outcome',
                'reason': 'Ответ утверждает результат оплаты, активации, возврата или обещает его.'}
    stated = {int(amount) for amount in _RUBLE.findall(text)}
    if stated - set(allowed_prices or ()):
        return {'send': False, 'class': 'unverified',
                'reason': 'В ответе есть цена, которой нет в переданных ценах каталога или номиналов.'}
    if collects_case_data(text):
        return {'send': True, 'class': 'collect', 'reason': 'Ответ запрашивает сведения и не сообщает результат.'}
    if stated:
        return {'send': True, 'class': 'catalog_price', 'reason': 'Цены в ответе есть в переданных фактах.'}
    return {'send': True, 'class': 'instruct', 'reason': 'Ответ объясняет порядок и не сообщает результат операции.'}


def remember_turn(db, chat, validated, context):
    state = load(db, chat)
    if validated.get('topic_changed'):
        state['collected'] = {}
        state['offered'] = []
        state['option_label'] = ''
    if validated.get('service_id'):
        state['service_key'] = validated['service_id']
        match = next((row for row in context.get('tariffs') or [] if row['service_id'] == validated['service_id']), None)
        state['service_label'] = match['service'] if match else state.get('service_label') or ''
    if validated.get('tariff'):
        state['option_label'] = validated['tariff']['plan']
        state['offered'] = [validated['tariff']]
    elif validated.get('service_id') and validated.get('intent') in ('price', 'list_options'):
        state['offered'] = [row for row in context.get('tariffs') or []
                            if row['service_id'] == validated['service_id']][:12]
    state['active'] = bool(state.get('service_key') or state.get('offered'))
    state['pending_slot'] = ''
    save(db, chat, state)


def package(understanding, context, db=None, chat=None):
    validated = validate(understanding or {}, context)
    reply = str((understanding or {}).get('reply') or '').strip()
    if not reply:
        from .handoff import handoff_text
        reply = handoff_text()
        verdict = {'send': False, 'class': 'unverified', 'reason': 'Модель не дала текст ответа.'}
    else:
        verdict = classify_draft(reply, validated['allowed_prices'])
    if db is not None and chat is not None:
        remember_turn(db, chat, validated, context)
    summary = str((understanding or {}).get('summary') or verdict['reason'])[:700]
    return {'reply': reply or 'Александр будет на связи в течение получаса-часа. Пока могу помочь с другими вопросами)',
            'summary': summary,
            'handoff': not verdict['send'],
            'source_ids': [],
            'evidence': {'status': 'catalog_direct' if verdict['send'] else 'owner_policy',
                         'sources': [],
                         'reply_class': verdict['class'],
                         'turn_understanding': {'intent': validated['intent'],
                                                'service_id': validated['service_id'],
                                                'tariff_id': validated['tariff_id'],
                                                'topic_changed': validated['topic_changed'],
                                                'known_facts': validated['known_facts'],
                                                'missing_facts': validated['missing_facts']},
                         'diagnostic': verdict['reason'],
                         'handoff_reason': '' if verdict['send'] else verdict['reason']}}
