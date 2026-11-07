"""Read the open chat before answering.

Every reply starts from the last 10 messages (client, assistant and owner, in
order) plus the saved dialog state. A fact said anywhere in that window stays
known: the latest message does not erase a region, amount or tariff given
earlier. Verifiable prices are quoted from the catalogue. A question is asked
only for what is still missing, all of it in one message, and not twice in a row.
"""
import re

from .dialog_state import load


_REGION_CURRENCY = {'RU': 'RUB', 'US': 'USD', 'DE': 'EUR', 'FR': 'EUR', 'TR': 'TRY', 'GB': 'GBP'}
_ASK_HINT = re.compile(r'\?|подскажите|уточните|какой|какая|какое|какие|какую|на какую', re.I)


def window(db, chat, limit=10):
    rows = db.execute(
        '''SELECT mid, role, text FROM messages
           WHERE chat=? AND role IN ('client','assistant','owner')
           ORDER BY mid DESC LIMIT ?''',
        (chat, limit)).fetchall()
    return [{'mid': row['mid'], 'role': row['role'], 'text': row['text'] or ''} for row in reversed(rows)]


def question_targets(text):
    """Slots a reply asks the client to provide. A stated fact is not a question."""
    targets = set()
    for chunk in re.split(r'(?<=[.!?])\s+', str(text or '')):
        if not chunk.strip() or not _ASK_HINT.search(chunk):
            continue
        if re.search(r'регион', chunk, re.I):
            targets.add('region')
        if re.search(r'сумм|номинал|на какую', chunk, re.I):
            targets.add('amount')
        if re.search(r'валют', chunk, re.I):
            targets.add('currency')
        if re.search(r'какой\s+сервис|о каком\s+сервисе|какой\s+сервис\s+вас', chunk, re.I):
            targets.add('service')
        if re.search(r'тариф|какой\s+из\s+этих', chunk, re.I):
            targets.add('option')
    return targets


def _normalize(text):
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]+', ' ', str(text or '').casefold().replace('ё', 'е'))).strip()


def _asking_sentence(text):
    chunks = [chunk.strip() for chunk in re.split(r'(?<=[.!?])\s+', str(text or '')) if chunk.strip()]
    asking = [chunk for chunk in chunks if _ASK_HINT.search(chunk) and question_targets(chunk)]
    return asking[-1] if asking else ''


def _same_question(text, previous):
    current = _normalize(_asking_sentence(text))
    prior = _normalize(_asking_sentence(previous))
    return bool(current and prior and current == prior)


def _service(text, catalog):
    from .catalog_context import service_mentions
    found = service_mentions(text, catalog or [])
    if len(found) == 1:
        key, label = next(iter(found.items()))
        return key, label
    return '', ''


def _client_amount(text):
    from .apple_matching import amounts
    found = [(str(amount), unit) for amount, unit in amounts(text)]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        return found[-1]
    matches = re.findall(
        r'(?:хочу\s+на|пополн\w*\s+на|на)\s*(\d{2,6})\b|(?<!\d)(\d{2,6})\s*(?:руб(?:лей|ля|ль)?|₽|р\b)',
        str(text or ''), re.I)
    if matches:
        number = next(piece for piece in matches[-1] if piece)
        return number, ''
    bare = re.fullmatch(r'\s*(?:на\s+|а\s+)?(\d{2,6})\s*[?.!]?\s*', str(text or ''), re.I)
    if bare:
        return bare.group(1), ''
    return '', ''


_LIST = re.compile(
    r'(?:все|каких|какие|список|перечень|назови(?:те)?|покажи(?:те)?|перечисли(?:те)?)'
    r'.{0,40}(?:номинал|тариф|вариант|цен)', re.I)
_HOWTO = re.compile(r'как\s+(?:мне\s+|нам\s+)?(?:оформить|купить|оплатить|пополнить|подключить|заказать)', re.I)
_CAPABILITIES = re.compile(
    r'что\s+(?:ты|вы)\s+мож(?:ешь|ете)|чем\s+(?:ты|вы)\s+мож(?:ешь|ете)|кто\s+(?:ты|вы)\b|что\s+умеешь', re.I)
_PRICE = re.compile(r'сколько|стоит|стоим|цен[аыуе]', re.I)


def format_offer(service, plan):
    """Show the tariff once when its name already ends with that plan."""
    service = str(service or '').strip()
    plan = str(plan or '').strip()
    if not service:
        return plan
    folded_service = _normalize(service).split()
    folded_plan = _normalize(plan).split()
    visible = plan.split()
    if not plan or _normalize(service).endswith(_normalize(plan)):
        return service
    while folded_service and folded_plan and visible and folded_service[-1] == folded_plan[0]:
        folded_service.pop()
        folded_plan.pop(0)
        visible.pop(0)
    if not visible:
        return service
    return f'{service} {" ".join(visible)}'


def classify_intent(text):
    """Question type of the current burst. Product names are not the classifier."""
    if _CAPABILITIES.search(text or ''):
        return 'capabilities'
    if _LIST.search(text or ''):
        return 'list_nominals'
    if _HOWTO.search(text or ''):
        return 'how_to'
    if _PRICE.search(text or ''):
        return 'price'
    return 'continuation'


def _burst_text(messages):
    burst = []
    for message in reversed(messages):
        role = message.get('role')
        if role in ('assistant', 'owner') and burst:
            break
        if role == 'client':
            burst.append(message.get('text') or '')
    burst.reverse()
    return '\n'.join(part for part in burst if part).strip()


def _offers_in(text, service_label):
    rows = []
    for label, price in re.findall(r'([^\n:]{2,80}?):\s*(\d+)\s*₽', str(text or '')):
        name = label.strip()
        if re.search(r'этап|причина|регион|диагност', name, re.I):
            continue
        plan = name
        if service_label and name.casefold().startswith(service_label.casefold()):
            plan = name[len(service_label):].strip() or name
        rows.append({'service': service_label or name, 'plan': plan, 'price': int(price)})
    return rows


def _match_option(text, offered):
    from .dialog_state import _match_offers
    return _match_offers(text, offered)


def _fill_catalog_offers(situation, catalog):
    from .catalog_context import service_mentions
    rows = []
    for row in catalog or []:
        if not isinstance(row, dict):
            continue
        price = row.get('catalog_price_rub')
        if type(price) is not int or price <= 0 or not row.get('plan'):
            continue
        mentioned = service_mentions(str(row.get('service', '')) + ' ' + str(row.get('service_key', '')), [])
        if situation.get('service_key') not in mentioned:
            continue
        rows.append({'service': row.get('service') or situation.get('service_label') or '',
                     'plan': row.get('plan'), 'price': price})
    if rows:
        situation['offered'] = rows


def analyze(messages, state=None, catalog=()):
    """Fold the window into one situation. Earlier facts survive a short reply."""
    state = state or {}
    collected = dict(state.get('collected') or {})
    situation = {
        'service_key': state.get('service_key') or '',
        'service_label': state.get('service_label') or '',
        'region': str(collected.get('region') or ''),
        'amount': str(collected.get('amount') or ''),
        'currency': str(collected.get('currency') or ''),
        'option_label': state.get('option_label') or '',
        'offered': list(state.get('offered') or []),
        'matched_offers': [],
        'latest_intent': '',
        'previous_question': '',
        'asked_slots': [],
    }
    for message in messages[-10:]:
        text = message.get('text') or ''
        role = message.get('role')
        if role in ('assistant', 'owner'):
            question = _asking_sentence(text)
            if question:
                situation['previous_question'] = question
                situation['asked_slots'] = sorted(question_targets(question))
            if not situation['service_key']:
                key, label = _service(text, catalog)
                if key:
                    situation['service_key'], situation['service_label'] = key, label
            if situation['service_label']:
                listed = _offers_in(text, situation['service_label'])
                if listed:
                    situation['offered'] = listed
            continue
        key, label = _service(text, catalog)
        if key and key != situation['service_key']:
            situation.update(service_key=key, service_label=label, region='', amount='',
                             currency='', option_label='', offered=[], matched_offers=[])
        elif key:
            situation['service_key'], situation['service_label'] = key, label
        from .apple_matching import region_codes
        regions = region_codes(text)
        if len(regions) == 1 and (situation['service_key'] in ('', 'appstore') or 'region' in situation['asked_slots']
                                  or situation['service_key'] == 'appstore'):
            # A country in an App Store topic is the account region, not a new shop service.
            if situation['service_key'] in ('', 'appstore') or 'region' in (situation['asked_slots'] or []):
                situation['region'] = next(iter(regions))
                if not situation['service_key']:
                    situation['service_key'], situation['service_label'] = 'appstore', 'App Store'
        amount, currency = _client_amount(text)
        if amount and (situation['service_key'] == 'appstore' or 'amount' in situation['asked_slots']
                       or situation['region']):
            situation['amount'] = amount
            if currency:
                situation['currency'] = currency
            if not situation['service_key']:
                situation['service_key'], situation['service_label'] = 'appstore', 'App Store'
        situation['latest_intent'] = text.strip()
        situation['_last_client'] = text
    _fill_catalog_offers(situation, catalog)
    if situation.get('_last_client'):
        hits = _match_option(situation['_last_client'], situation['offered'])
        if len(hits) == 1:
            situation['option_label'] = hits[0]['plan']
            situation['matched_offers'] = hits
        elif hits:
            situation['matched_offers'] = hits
            situation['option_label'] = ''
    situation.pop('_last_client', None)
    if situation['amount'] and not situation['currency']:
        situation['currency'] = _REGION_CURRENCY.get(situation['region'], '')
    situation['want'] = ('appstore_topup' if situation['service_key'] == 'appstore'
                         else 'tariff' if situation['service_key'] else 'unknown')
    known = []
    if situation['service_key']:
        known.append('service')
    if situation['region']:
        known.append('region')
    if situation['amount']:
        known.append('amount')
    if situation['currency']:
        known.append('currency')
    if situation['option_label']:
        known.append('option')
    missing = []
    if situation['want'] == 'appstore_topup':
        for slot in ('region', 'amount', 'currency'):
            if slot not in known:
                missing.append(slot)
    elif situation['want'] == 'unknown':
        missing.append('service')
    elif situation['want'] == 'tariff' and 'option' in situation['asked_slots'] and 'option' not in known:
        missing.append('option')
    situation['burst'] = _burst_text(messages)
    situation['intent'] = classify_intent(situation['burst'] or situation.get('latest_intent') or '')
    # A new question does not inherit an amount or tariff the client is not asking about.
    if situation['intent'] in ('list_nominals', 'capabilities'):
        situation['amount'] = ''
        situation['currency'] = ''
        situation['option_label'] = ''
        situation['matched_offers'] = []
    elif situation['intent'] == 'how_to':
        situation['option_label'] = ''
        situation['matched_offers'] = []
        if situation['want'] != 'appstore_topup':
            situation['amount'] = ''
            situation['currency'] = ''
            situation['region'] = ''
    known = [slot for slot in known if {
        'service': situation['service_key'], 'region': situation['region'], 'amount': situation['amount'],
        'currency': situation['currency'], 'option': situation['option_label'],
    }.get(slot)]
    missing = []
    if situation['intent'] == 'list_nominals' and situation['want'] == 'appstore_topup':
        if 'region' not in known:
            missing.append('region')
    elif situation['intent'] in ('capabilities', 'how_to') and situation['want'] != 'appstore_topup':
        missing = []
    elif situation['intent'] == 'how_to' and situation['want'] == 'appstore_topup':
        for slot in ('region', 'amount', 'currency'):
            if slot not in known:
                missing.append(slot)
    elif situation['want'] == 'appstore_topup' and situation['intent'] in ('price', 'continuation'):
        for slot in ('region', 'amount', 'currency'):
            if slot not in known:
                missing.append(slot)
    elif situation['want'] == 'unknown' and situation['intent'] == 'continuation':
        missing.append('service')
    elif situation['want'] == 'tariff' and situation['intent'] in ('price', 'continuation') and 'option' in situation['asked_slots'] and 'option' not in known:
        missing.append('option')
    situation['known'] = known
    situation['missing'] = missing
    return situation


def _apple_fact(facts, situation):
    from .apple_matching import ALIASES, alternatives, exact_quote
    from .catalog_context import annotate
    region = situation['region']
    names = ALIASES.get(region) or ()
    word = names[0] if names else region
    lookup = f"app store {word} {situation['amount']} {situation['currency']} сколько стоит"
    priced = annotate(dict(facts or {}), lookup)
    quote = exact_quote(priced, lookup)
    if not quote:
        return None
    label = {'US': 'США', 'DE': 'Германии', 'TR': 'Турции', 'GB': 'Великобритании', 'RU': 'России'}.get(region, region)
    amount = quote['amount']
    unit = quote['currency']
    if quote.get('price') is not None:
        text = f"Для Apple ID {label} есть пополнение на {amount:g} {unit} — {quote['price']} ₽."
        status = 'catalog_direct'
        diagnostic = 'Этап: разбор диалога. Регион и сумма взяты из последних сообщений, цена из остатков.'
    else:
        text = (f"Вы хотите пополнить Apple ID {label} на {amount:g} {unit}. "
                'Точную стоимость этого номинала сейчас подтвердить не получилось.')
        other = alternatives(priced, quote)
        if other:
            text += '\n\nПодтверждённые номиналы этого региона:\n' + '\n'.join(
                f"{row['amount']:g} {row['currency']}: {row['price']} ₽" for row in other)
        status = 'catalog_direct' if other else 'catalog_review'
        diagnostic = 'Этап: разбор диалога. Регион и сумма уже названы; точной цены этого номинала в остатках нет.'
    return {'reply': text, 'summary': 'Цена App Store по сведениям, уже есть в диалоге. Повторно регион и сумма не запрашивались.',
            'handoff': False, 'source_ids': [],
            'evidence': {'status': status, 'sources': [], 'diagnostic': diagnostic}}


def _tariff_fact(facts, situation):
    from .catalog_context import service_mentions
    label = situation.get('option_label') or ''
    if not label:
        hits = situation.get('matched_offers') or []
        if len(hits) == 1:
            label = hits[0]['plan']
        elif len(hits) > 1:
            return _one_question(situation, hits, facts)
    if not label:
        return None
    price = None
    service = situation.get('service_label') or ''
    for row in (facts or {}).get('catalog') or []:
        if not isinstance(row, dict):
            continue
        mentioned = service_mentions(str(row.get('service', '')) + ' ' + str(row.get('service_key', '')), [])
        if situation['service_key'] not in mentioned:
            continue
        if _normalize(row.get('plan')) == _normalize(label) or label.casefold() in str(row.get('plan', '')).casefold():
            value = row.get('catalog_price_rub')
            if type(value) is int and value > 0:
                price = value
                service = row.get('service') or service
                label = row.get('plan') or label
                break
    if price is None:
        for row in situation.get('offered') or []:
            if _normalize(row.get('plan')) == _normalize(label):
                price = row.get('price')
                service = row.get('service') or service
                break
    if type(price) is not int:
        return None
    return {'reply': (f'{format_offer(service, label)}: {price} ₽\n\n'
                      'Это цена из каталога. Оформить можно в боте: выберите этот сервис, тариф и способ оплаты. '
                      'Оплата сама по себе не активирует подписку.'),
            'summary': 'Тариф выбран по открытому диалогу, цена взята из каталога.',
            'handoff': False, 'source_ids': [],
            'evidence': {'status': 'catalog_direct', 'sources': [],
                         'diagnostic': 'Этап: разбор диалога. Тариф сопоставлен с уже названным сервисом и каталогом.'}}


def _missing_sentence(situation, options=()):
    missing = list(situation.get('missing') or [])
    if options and 'option' in missing:
        lines = [f"{format_offer(row['service'], row['plan'])}: {row['price']} ₽" for row in options]
        return 'Подходят несколько тарифов:\n' + '\n'.join(lines) + '\n\nКакой из этих тарифов выбрать?'
    if situation.get('want') == 'appstore_topup':
        if missing == ['region', 'amount', 'currency'] or set(missing) >= {'region', 'amount'}:
            return 'Подскажите, пожалуйста, регион Apple ID и сумму пополнения.'
        if missing == ['region']:
            return 'Какой регион у вашего Apple ID?'
        if missing == ['amount'] or set(missing) <= {'amount', 'currency'}:
            return 'На какую сумму хотите пополнить баланс Apple ID?'
        if missing == ['currency']:
            return 'Уточните, пожалуйста, валюту суммы пополнения.'
    if missing == ['service']:
        return 'Какой сервис вас интересует?'
    if missing == ['option']:
        offered = situation.get('offered') or []
        if offered:
            lines = [f"{format_offer(row['service'], row['plan'])}: {row['price']} ₽" for row in offered]
            return 'Цены из каталога:\n' + '\n'.join(lines) + '\n\nКакой тариф выбрать?'
        return 'Какой тариф вас интересует?'
    names = {'region': 'регион', 'amount': 'сумму', 'currency': 'валюту', 'service': 'сервис', 'option': 'тариф'}
    readable = [names[item] for item in missing if item in names]
    if not readable:
        return ''
    return 'Напишите, пожалуйста, сразу: ' + ' и '.join(readable) + '.'


def _one_question(situation, options=(), facts=None):
    sentence = _missing_sentence(situation, options)
    if not sentence or _same_question(sentence, situation.get('previous_question')):
        return None
    text = sentence
    if (situation.get('want') == 'appstore_topup' and not situation.get('previous_question')
            and set(situation.get('missing') or []) >= {'region', 'amount'}):
        from .bot_guide import purchase_navigation
        navigation = purchase_navigation(facts or {}, apple=True)
        text = ((navigation + '\n\n') if navigation else '') + (
            'Нужен подарочный код того же региона, что и ваш Apple ID. '
            'После оплаты погасите код в App Store: профиль → «Погасить подарочную карту или код».\n\n'
            + sentence)
    return {'reply': text,
            'summary': 'В последних сообщениях не хватает сведений. Спрошено всё недостающее сразу, без повтора прошлого вопроса.',
            'handoff': False, 'source_ids': [],
            'evidence': {'status': 'clarification', 'sources': [],
                         'diagnostic': 'Этап: разбор диалога. Не хватает: ' + ', '.join(situation.get('missing') or []) + '.'}}


def _all_denoms(facts, code):
    from .apple_matching import nominal, region_codes
    found = {}
    for region in (facts or {}).get('appstore', {}).get('regions', []) or []:
        if code not in region_codes(region.get('name', ''), stock=True):
            continue
        for row in region.get('denoms', []) or []:
            identity = nominal(row.get('nominal', ''))
            price = row.get('price_rub')
            if not identity or type(price) is not int or price <= 0:
                continue
            found[identity] = price
    rows = [{'amount': amount, 'currency': unit, 'price': price} for (amount, unit), price in found.items()]
    rows.sort(key=lambda row: (row['currency'], -row['amount']))
    return rows


def _list_reply(facts, situation):
    if situation.get('service_key') == 'appstore':
        if not situation.get('region'):
            return _one_question(dict(situation, missing=['region']), facts=facts)
        rows = _all_denoms(facts, situation['region'])
        label = {'US': 'США', 'DE': 'Германии', 'TR': 'Турции', 'GB': 'Великобритании', 'RU': 'России'}.get(
            situation['region'], situation['region'])
        if not rows:
            text = f'Актуальные номиналы для Apple ID {label} сейчас не удалось загрузить.'
        else:
            text = f"Номиналы для Apple ID {label}:\n" + '\n'.join(
                f"{row['amount']:g} {row['currency']}: {row['price']} ₽" for row in rows)
        return {'reply': text, 'summary': 'Список номиналов выбранного региона. Сумма, которую клиент не называл в этом вопросе, не подставлена.',
                'handoff': False, 'source_ids': [],
                'evidence': {'status': 'catalog_direct', 'sources': [],
                             'diagnostic': 'Этап: разбор текущего вопроса. Запрошен список номиналов, а не цена старой суммы.'}}
    offered = situation.get('offered') or []
    if not offered:
        return None
    text = 'Тарифы из каталога:\n' + '\n'.join(
        f"{format_offer(row['service'], row['plan'])}: {row['price']} ₽" for row in offered)
    return {'reply': text, 'summary': 'Список тарифов по текущему вопросу.',
            'handoff': False, 'source_ids': [],
            'evidence': {'status': 'catalog_direct', 'sources': [],
                         'diagnostic': 'Этап: разбор текущего вопроса. Запрошен список тарифов.'}}


def respond(situation, facts):
    """Answer the current question. Background facts do not replace it."""
    if not situation:
        return None
    intent = situation.get('intent')
    if intent == 'capabilities':
        return None
    if intent == 'list_nominals':
        listed = _list_reply(facts, situation)
        if listed:
            return listed
    if intent == 'how_to':
        if situation.get('want') == 'appstore_topup' and situation.get('missing'):
            return _one_question(situation, facts=facts)
        return None
    if situation.get('want') == 'appstore_topup' and not situation.get('missing') and intent in ('price', 'continuation'):
        quoted = _apple_fact(facts, situation)
        if quoted:
            return quoted
    if situation.get('want') == 'tariff' and situation.get('offered') and not situation.get('option_label') and not situation.get('matched_offers'):
        if re.search(r'сколько|стоим|цен|тариф', situation.get('latest_intent') or '', re.I):
            lines = [f"{format_offer(row['service'], row['plan'])}: {row['price']} ₽" for row in situation['offered']]
            text = 'Цены из каталога:\n' + '\n'.join(lines)
            if not _same_question(text, situation.get('previous_question')):
                return {'reply': text, 'summary': 'Цены сервиса из каталога по уже известной теме.',
                        'handoff': False, 'source_ids': [],
                        'evidence': {'status': 'catalog_direct', 'sources': [],
                                     'diagnostic': 'Этап: разбор диалога. Сервис известен, показаны цены каталога без нового вопроса о сервисе.'}}
    if situation.get('want') == 'tariff' and ('option' in situation.get('known', []) or situation.get('matched_offers')):
        quoted = _tariff_fact(facts, situation)
        if quoted:
            return quoted
    if situation.get('missing'):
        return _one_question(situation, situation.get('matched_offers') or [], facts)
    return None


def prepare(db, chat, facts):
    state = load(db, chat)
    situation = analyze(window(db, chat), state, (facts or {}).get('catalog') or [])
    reply = respond(situation, facts)
    return {'situation': situation, 'reply': reply}


def _known_statement(situation):
    bits = []
    if situation.get('region'):
        bits.append('регион ' + situation['region'])
    if situation.get('amount'):
        bits.append(('сумма ' + situation['amount'] + ' ' + (situation.get('currency') or '')).strip())
    if situation.get('option_label'):
        bits.append('тариф ' + situation['option_label'])
    if situation.get('service_label') and 'service' in situation.get('known', []):
        bits.append(situation['service_label'])
    missing = situation.get('missing') or []
    names = {'region': 'регион', 'amount': 'сумма', 'currency': 'валюта', 'service': 'сервис', 'option': 'тариф'}
    known = ', '.join(bits)
    if missing:
        need = ', '.join(names.get(item, item) for item in missing)
        text = (f'Уже известно: {known}. ' if known else '') + f'Этого недостаточно без: {need}.'
    else:
        text = f'Уже известно: {known}.' if known else 'Повторно это не спрашиваю.'
    if _same_question(text, situation.get('previous_question')):
        text = 'Эти сведения уже есть в переписке, повторно не спрашиваю.'
    return text


def guard_reply(answer, situation, facts=None):
    """Drop a question whose answer is already known, and do not repeat the previous one."""
    if not isinstance(answer, dict) or not situation:
        return answer
    # A how-to or a capabilities question is not replaced by the previous price card.
    if situation.get('intent') in ('capabilities', 'how_to'):
        return answer
    text = str(answer.get('reply') or '')
    asked = question_targets(text)
    known = set(situation.get('known') or [])
    illegal = asked & known
    repeated = _same_question(text, situation.get('previous_question'))
    if not illegal and not repeated:
        return answer
    replacement = respond(situation, facts or {})
    if replacement and not (question_targets(replacement['reply']) & known) and not _same_question(
            replacement['reply'], situation.get('previous_question')):
        return replacement
    kept = []
    for chunk in re.split(r'(?<=[.!?])\s+', text):
        if not chunk.strip():
            continue
        targets = question_targets(chunk)
        if targets & known or (repeated and targets):
            continue
        kept.append(chunk.strip())
    cleaned = ' '.join(kept).strip()
    if not cleaned:
        cleaned = _known_statement(situation)
    guarded = dict(answer)
    guarded['reply'] = cleaned
    guarded['summary'] = 'Вопрос убран: ответ на него уже есть в последних сообщениях или он повторяет предыдущий.'
    return guarded


def remember(db, chat, situation):
    """Store every known fact. One open question must not erase the others."""
    state = load(db, chat)
    state['service_key'] = situation.get('service_key') or ''
    state['service_label'] = situation.get('service_label') or ''
    state['option_label'] = situation.get('option_label') or ''
    state['topic'] = 'appstore' if situation.get('want') == 'appstore_topup' else (situation.get('want') or '')
    state['collected'] = {
        'region': situation.get('region') or '',
        'amount': situation.get('amount') or '',
        'currency': situation.get('currency') or '',
    }
    state['offered'] = situation.get('offered') or []
    state['pending_question'] = situation.get('previous_question') or ''
    state['pending_slot'] = ''
    state['active'] = bool(state['service_key'] or state['pending_question'])
    from .dialog_state import save
    return save(db, chat, state)
