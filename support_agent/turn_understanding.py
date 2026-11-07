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
VPN_SENTENCE = ('Для доступа к сервисам лучше включить VPN и по возможности всегда '
                'использовать одну и ту же локацию.')
UNDERSTANDING_INSTRUCTIONS = (
    'Разбери текущую реплику клиента по истории, состоянию диалога, глоссарию и спискам тарифов и номиналов. '
    'glossary — разговорные названия и слова тарифов. Это подсказка смысла. '
    'intent — тип вопроса. service_id и tariff_id только из tariffs, иначе пустая строка. '
    'confidence=high, только когда сервис или тариф понятен. '
    'confidence=low, если есть сомнение: service_id и tariff_id оставь пустыми, '
    'clarify_ids — два или три id из tariffs (id или service_id), '
    'reply — короткий вопрос ровно с этими вариантами, без выбора одного из них. '
    'topic_changed=true, если реплика про другой сервис или другой тип вопроса. '
    'Обычное Pro без слова про год — месячный тариф, если в списке есть и месяц, и год. '
    'questions — все отдельные вопросы этого обращения, коротко и с названием из каталога, если оно известно. '
    'reply отвечает на каждый вопрос отдельным коротким пунктом: 1. 2. 3. '
    'Не переспрашивай факты из known_facts. Не утверждай, что оплата прошла, подписка активирована или возврат сделан. '
    'Цены в reply только из tariffs.price и nominals.price. '
    'Если клиент сообщает об оплате, попроси в одном сообщении номер заказа из бота или FreeKassa и чек из банка (скриншот или PDF), если их ещё нет в последних сообщениях. '
    'Если receipt_attached=true, чек уже есть: не проси его снова и не описывай содержимое файла. '
    'order_number — только цифры номера, который клиент уже назвал, иначе пусто. '
    'receipt_amount_unverified — сумма с чека, если ты её разобрал, иначе пусто. Чек сам по себе оплату не подтверждает. '
    'Если вопрос про работу сервиса в стране клиента, доступ, соединение или обычное использование сервисов, '
    'поставь vpn_advice=true и напиши, что нужен VPN и лучше всегда одна и та же локация. '
    'Изображение смотри вместе с текстом. Это может быть скрин ошибки или чек. Чек оплату не подтверждает. '
    'Голос уже расшифрован в тексте сообщения. '
    'Не утверждай, что оплата прошла, подписка активирована или возврат сделан.'
)
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
    from .glossary import for_context
    try:
        glossary = for_context(facts)
    except Exception:
        glossary = {'services': [], 'phrases': [], 'tariff_words': [], 'load_error': True}
    return {'burst': burst,
            'receipt_attached': bool(receipt_files(db, chat)),
            'glossary': glossary,
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
    confidence = str((understanding or {}).get('confidence') or 'high')
    if confidence not in ('high', 'low'):
        confidence = 'high'
    return {'intent': understanding.get('intent') if understanding.get('intent') in INTENTS else 'other',
            'service_id': service_id,
            'tariff_id': tariff_id,
            'tariff': chosen,
            'confidence': confidence,
            'topic_changed': bool(understanding.get('topic_changed')),
            'known_facts': [str(item) for item in understanding.get('known_facts') or [] if isinstance(item, str)][:12],
            'missing_facts': [str(item) for item in understanding.get('missing_facts') or [] if isinstance(item, str)][:12],
            'questions': _texts(understanding.get('questions'), 6),
            'vpn_advice': bool((understanding or {}).get('vpn_advice')),
            'allowed_prices': _allowed_prices(tariffs, context.get('nominals') or [], service_id, tariff_id)}


def _texts(values, limit):
    return [str(item).strip() for item in values or [] if isinstance(item, str) and str(item).strip()][:limit]


def clarify_options(understanding, context):
    """Keep only catalogue ids the model offered. Do not invent a third choice."""
    tariffs = context.get('tariffs') or []
    by_id = {row['id']: row for row in tariffs}
    by_service = {}
    for row in tariffs:
        by_service.setdefault(row['service_id'], row)
    found = []
    seen = set()
    for raw in (understanding or {}).get('clarify_ids') or []:
        item = str(raw or '')
        if item in by_id:
            row = by_id[item]
            label = (row['service'] + ' ' + row['plan']).strip()
            key = row['id']
        elif item in by_service:
            row = by_service[item]
            label = row['service']
            key = row['service_id']
        else:
            continue
        if key in seen or not label:
            continue
        seen.add(key)
        found.append({'id': key, 'label': label})
        if len(found) == 3:
            break
    return found


def _clarify_reply(reply, options):
    labels = [item['label'] for item in options]
    folded = str(reply or '').casefold()
    if '?' in str(reply or '') and all(label.casefold() in folded for label in labels):
        return str(reply).strip()
    if len(labels) == 2:
        body = labels[0] + ' или ' + labels[1]
    else:
        body = ', '.join(labels[:-1]) + ' или ' + labels[-1]
    return 'Уточните, пожалуйста: ' + body + '?'


def _answer_blocks(reply):
    parts = re.split(r'(?m)^\s*\d+[.)]\s+', str(reply or ''))
    return [part.strip() for part in parts[1:] if part.strip()]


def _catalog_tokens(context):
    tokens = set()
    for row in context.get('tariffs') or []:
        for part in (row.get('service'), row.get('plan'), row.get('service_id')):
            for word in re.findall(r'[A-Za-zА-Яа-яЁё0-9]{3,}', str(part or '')):
                tokens.add(word.casefold().replace('ё', 'е'))
    return tokens


def questions_addressed(reply, questions, context):
    """A multi-question turn needs one numbered point per question the model listed."""
    items = [item for item in questions or [] if item]
    if len(items) < 2:
        return True
    blocks = _answer_blocks(reply)
    if len(blocks) < len(items):
        return False
    known = _catalog_tokens(context)
    folded = str(reply or '').casefold().replace('ё', 'е')
    for question in items:
        words = re.findall(r'[A-Za-zА-Яа-яЁё0-9]{3,}', question)
        needed = [word.casefold().replace('ё', 'е') for word in words if word.casefold().replace('ё', 'е') in known]
        if any(word not in folded for word in needed):
            return False
    return True


def _with_vpn(reply, needed):
    if not needed:
        return reply
    folded = reply.casefold()
    if ('vpn' in folded or 'впн' in folded) and 'локац' in folded:
        return reply
    return (reply.rstrip() + '\n\n' + VPN_SENTENCE).strip()


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
    from .handoff import handoff_text
    if validated['confidence'] == 'low':
        options = clarify_options(understanding, context)
        validated['service_id'] = ''
        validated['tariff_id'] = ''
        validated['tariff'] = None
        validated['allowed_prices'] = _allowed_prices(
            context.get('tariffs') or [], context.get('nominals') or [], '', '')
        if len(options) < 2:
            reply = handoff_text()
            verdict = {'send': False, 'class': 'unverified',
                       'reason': 'Уверенность низкая, а двух вариантов из каталога модель не назвала.'}
        else:
            reply = _clarify_reply(reply, options)
            verdict = classify_draft(reply, validated['allowed_prices'])
    elif not reply:
        reply = handoff_text()
        verdict = {'send': False, 'class': 'unverified', 'reason': 'Модель не дала текст ответа.'}
    else:
        verdict = classify_draft(reply, validated['allowed_prices'])
    if verdict['send']:
        reply = _with_vpn(reply, validated['vpn_advice'])
        if len(reply) > 1800:
            verdict = {'send': False, 'class': 'unverified', 'reason': 'Ответ с правилом VPN длиннее 1800 символов.'}
    if verdict['send'] and not questions_addressed(reply, validated['questions'], context):
        verdict = {'send': False, 'class': 'unverified',
                   'reason': 'В ответе нет отдельного пункта на каждый вопрос из разбора.'}
    if db is not None and chat is not None:
        remember_turn(db, chat, validated, context)
    summary = str((understanding or {}).get('summary') or verdict['reason'])[:700]
    return {'reply': reply or handoff_text(),
            'summary': summary,
            'handoff': not verdict['send'],
            'source_ids': [],
            'evidence': {'status': 'catalog_direct' if verdict['send'] else 'owner_policy',
                         'sources': [],
                         'reply_class': verdict['class'],
                         'turn_understanding': {'intent': validated['intent'],
                                                'service_id': validated['service_id'],
                                                'tariff_id': validated['tariff_id'],
                                                'confidence': validated['confidence'],
                                                'topic_changed': validated['topic_changed'],
                                                'known_facts': validated['known_facts'],
                                                'missing_facts': validated['missing_facts'],
                                                'questions': validated['questions'],
                                                'vpn_advice': validated['vpn_advice']},
                         'diagnostic': verdict['reason'],
                         'handoff_reason': '' if verdict['send'] else verdict['reason']}}
