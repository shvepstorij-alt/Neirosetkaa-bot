"""Per-chat dialog state.

The assistant remembers the service, option, open question and facts it already
collected. A short later message is read against that state. Catalogue aliases
and price rows stay the source of names and prices; this module does not keep a
list of client slang.
"""
import json
import re
from decimal import Decimal


_VERSION_CLAIM = re.compile(
    r'(?i)(?<![a-zа-яё])(?:gpt|opus|sonnet|haiku|gemini|claude)\s*[-–]?\s*\d+(?:[.,]\d+)?')
_FOLD = str.maketrans({
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'e', 'ж': 'zh',
    'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o',
    'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'c',
    'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
})
_ASK = re.compile(r'(?i)пришлите|напишите|укажите|сообщите|назовите')
_SECRET_ASK = re.compile(
    r'(?i)(?:пришлите|напишите|укажите|сообщите|назовите|отправьте)[^.!?\n]{0,80}'
    r'(?:парол|токен|сесси|код\s+(?:входа|подтверждения)|данные\s+(?:для\s+)?входа)')
_GRANT = re.compile(
    r'(?i)бонус[^.!?\n]{0,60}(?:одобрен|выдан|активирован|уже\s+доступен|оформлен)|'
    r'(?:одобрил[аи]?|активировал[аи]?|выдал[аи]?)[^.!?\n]{0,40}бонус')


def empty():
    return {'version': 1, 'active': False, 'bound': False, 'service_key': '', 'service_label': '',
            'option_label': '', 'topic': '', 'pending_slot': '', 'pending_question': '',
            'collected': {}, 'offered': []}


def _ensure(db):
    db.execute('CREATE TABLE IF NOT EXISTS dialog_state (chat INTEGER PRIMARY KEY, data TEXT)')


def load(db, chat):
    _ensure(db)
    row = db.execute('SELECT data FROM dialog_state WHERE chat=?', (chat,)).fetchone()
    if not row:
        return empty()
    try:
        data = json.loads(row['data'] if hasattr(row, 'keys') else row[0])
    except (TypeError, ValueError):
        return empty()
    state = empty()
    if isinstance(data, dict):
        state.update({key: data.get(key, state[key]) for key in state})
    if not isinstance(state.get('collected'), dict):
        state['collected'] = {}
    if not isinstance(state.get('offered'), list):
        state['offered'] = []
    state['active'] = bool(state.get('service_label') or state.get('pending_slot') or state.get('topic'))
    state['bound'] = False
    return state


def save(db, chat, state):
    _ensure(db)
    stored = empty()
    stored.update({key: state.get(key, stored[key]) for key in stored})
    stored['active'] = bool(stored.get('service_label') or stored.get('pending_slot') or stored.get('topic'))
    stored['bound'] = False
    db.execute('INSERT OR REPLACE INTO dialog_state VALUES (?,?)',
               (chat, json.dumps(stored, ensure_ascii=False, default=str)))
    if getattr(db, 'isolation_level', None) is not None:
        db.commit()
    return stored


def _fold_tokens(text):
    folded = str(text or '').casefold().replace('ё', 'е').translate(_FOLD)
    return [token for token in re.findall(r'[a-z0-9]+', folded) if len(token) >= 2]


def is_short(text):
    raw = str(text or '').strip()
    if not raw or len(raw) > 120:
        return False
    return 0 < len(re.findall(r'\w+', raw, flags=re.UNICODE)) <= 12


def _numeric_reply(text):
    return bool(re.fullmatch(
        r'\s*(?:а|и|ну)?\s*\d{2,7}(?:[.,]\d{1,2})?\s*'
        r'(?:руб(?:лей|ля|ль)?|₽|rub|usd|eur|try|\$|€|доллар(?:ов|а)?|евро|лир(?:ы)?)?\s*[?.!]*\s*',
        str(text or ''), re.I))


def _question_slot(question):
    if not question:
        return ''
    if re.search(r'какой\s+сервис', question, re.I):
        return 'service'
    if re.search(r'тариф|вариант|какой\s+из', question, re.I):
        return 'option'
    if re.search(r'регион', question, re.I):
        return 'region'
    if re.search(r'сумм|номинал|пополн', question, re.I):
        return 'amount'
    if re.search(r'почт', question, re.I):
        return 'email'
    if re.search(r'стоимость|порядок\s+оформления|возможност', question, re.I):
        return 'goal'
    return 'open'


def _service_of(text, catalog):
    from .catalog_context import service_mentions
    found = service_mentions(text, catalog or [])
    if len(found) == 1:
        key, label = next(iter(found.items()))
        return key, label
    return '', ''


def _offers_from_facts(facts, service_key, service_label):
    rows = []
    for row in (facts or {}).get('requested_catalog') or []:
        if not isinstance(row, dict):
            continue
        price = row.get('catalog_price_rub')
        if type(price) is not int or price <= 0:
            continue
        key, label = _service_of(str(row.get('service', '')) + ' ' + str(row.get('service_key', '')), [])
        if service_key and key != service_key:
            continue
        plan = str(row.get('plan') or '').strip()
        name = str(row.get('service') or service_label or label).strip()
        if name and plan:
            rows.append({'service': name, 'service_key': key or service_key, 'plan': plan, 'price': price})
    return rows


def _offers_from_reply(reply, service_key, service_label):
    rows = []
    for label, price in re.findall(r'([^\n:]{2,80}?):\s*(\d+)\s*₽', str(reply or '')):
        name = label.strip()
        if re.search(r'этап|причина|регион|диагност', name, re.I):
            continue
        plan = name
        if service_label and name.casefold().startswith(service_label.casefold()):
            plan = name[len(service_label):].strip() or name
        rows.append({'service': service_label or name, 'service_key': service_key,
                     'plan': plan, 'price': int(price)})
    return rows


def _remember_apple(state, reply):
    text = str(reply or '')
    found = re.search(
        r'Apple ID\s+([^\s:.,]+).*?(\d+(?:[.,]\d+)?)\s*(RUB|USD|EUR|TRY|GBP)\D{0,16}(\d+)\s*₽',
        text, re.I | re.S)
    if not found:
        found = re.search(
            r'(?:App Store|Apple ID)[^.!\n]{0,80}?(\d+(?:[.,]\d+)?)\s*(RUB|USD|EUR|TRY|GBP)[^.!\n]{0,40}?(\d+)\s*₽',
            text, re.I)
        if not found:
            return state
        amount, unit, _price = found.groups()
        region_text = text
    else:
        region_text, amount, unit, _price = found.groups()
    from .apple_matching import region_codes
    codes = region_codes(region_text)
    state['service_key'] = state.get('service_key') or 'appstore'
    state['service_label'] = state.get('service_label') or 'App Store'
    state['topic'] = 'appstore'
    collected = dict(state.get('collected') or {})
    if len(codes) == 1:
        collected['region'] = next(iter(codes))
    collected['amount'] = str(Decimal(amount.replace(',', '.')))
    collected['currency'] = unit.upper()
    state['collected'] = collected
    return state


def record_outgoing(db, chat, reply, facts=None):
    """Update state from the reply we actually produced and the rows behind it."""
    facts = facts if isinstance(facts, dict) else {}
    catalog = list(facts.get('catalog') or []) + list(facts.get('requested_catalog') or [])
    state = load(db, chat)
    key, label = _service_of(reply, catalog)
    if key:
        if state.get('service_key') and state['service_key'] != key:
            state['option_label'] = ''
            state['collected'] = {}
            state['offered'] = []
        state['service_key'] = key
        state['service_label'] = label
    question = ''
    questions = re.findall(r'[^?\n]*\?', str(reply or ''))
    if questions:
        question = questions[-1].strip()
    slot = _question_slot(question)
    if slot == 'service' and state.get('service_label'):
        slot = state.get('pending_slot') or 'open'
        question = state.get('pending_question') or question
    state['pending_question'] = question
    state['pending_slot'] = slot
    offered = _offers_from_facts(facts, state.get('service_key', ''), state.get('service_label', ''))
    if not offered:
        offered = _offers_from_reply(reply, state.get('service_key', ''), state.get('service_label', ''))
    if offered:
        state['offered'] = offered[:12]
        if not state.get('topic'):
            state['topic'] = 'price'
        if len(offered) == 1:
            state['option_label'] = offered[0]['plan']
    state = _remember_apple(state, reply)
    if slot == 'email':
        collected = dict(state.get('collected') or {})
        collected['email_requested'] = True
        state['collected'] = collected
    if slot == 'region':
        parsed = _amount_of(reply, '')
        collected = dict(state.get('collected') or {})
        if parsed and not collected.get('amount'):
            collected['amount'], collected['currency'] = parsed
            state['collected'] = collected
            state['topic'] = 'appstore'
    state['active'] = bool(state.get('service_label') or state.get('pending_slot') or state.get('topic'))
    return save(db, chat, state)


def _match_offers(text, offered):
    tokens = set(_fold_tokens(text))
    if not tokens:
        return []
    hits = []
    for row in offered:
        plan_tokens = set(_fold_tokens(row.get('plan', '')))
        if tokens & plan_tokens:
            hits.append(row)
    return hits


def _amount_of(text, previous):
    from .apple_matching import amounts
    found = amounts(text)
    if len(found) == 1:
        amount, unit = next(iter(found))
        return str(amount), unit
    match = re.search(r'(\d{2,7}(?:[.,]\d{1,2})?)', str(text or ''))
    if match and (_numeric_reply(text) or previous):
        return str(Decimal(match.group(1).replace(',', '.'))), previous or 'RUB'
    return None


def _region_of(text):
    from .apple_matching import region_codes
    codes = region_codes(text)
    if len(codes) == 1:
        return next(iter(codes))
    return ''


def interpret(state, text, catalog=()):
    """Bind a short message to the open topic. A named other service starts a new one."""
    state = dict(state or empty())
    state['collected'] = dict(state.get('collected') or {})
    state['offered'] = list(state.get('offered') or [])
    state['bound'] = False
    message = str(text or '').strip()
    from .request_context import is_acknowledgement
    if not state.get('active') or not is_short(message) or is_acknowledgement(message):
        return state
    key, _label = _service_of(message, catalog)
    if key and state.get('service_key') and key != state['service_key'] and not _numeric_reply(message):
        return state
    state['bound'] = True
    state['client_reply'] = message
    if key and not state.get('service_key'):
        state['service_key'] = key
        state['service_label'] = _label
    region = _region_of(message)
    if region and (state.get('topic') == 'appstore' or state.get('service_key') == 'appstore'
                   or state.get('pending_slot') == 'region'):
        state['collected']['region'] = region
        state['topic'] = 'appstore'
    amount = _amount_of(message, state['collected'].get('currency', ''))
    if amount and (state.get('topic') == 'appstore' or state.get('service_key') == 'appstore'
                   or state.get('pending_slot') == 'amount'):
        state['collected']['amount'], state['collected']['currency'] = amount
        state['topic'] = 'appstore'
    hits = _match_offers(message, state.get('offered') or [])
    state['matched_offers'] = hits
    if len(hits) == 1:
        state['option_label'] = hits[0]['plan']
    label_tokens = set(_fold_tokens(state.get('service_label', '')))
    if key and key == state.get('service_key'):
        label_tokens |= set(_fold_tokens(_label))
    state['restate'] = bool(key and key == state.get('service_key')
                            and not [token for token in _fold_tokens(message) if token not in label_tokens])
    if state.get('pending_slot') == 'goal':
        if re.search(r'стоим|цен', message, re.I):
            state['topic'] = 'price'
            state['answered_price'] = True
        elif re.search(r'оформ|куп', message, re.I):
            state['topic'] = 'purchase'
        elif re.search(r'возмож|функц', message, re.I):
            state['topic'] = 'features'
    if state.get('pending_slot') == 'email':
        state['collected']['email_requested'] = True
    return state


def continue_text(state, message):
    if not state.get('bound'):
        return ''
    parts = [state.get('service_label') or '', state.get('option_label') or '']
    collected = state.get('collected') or {}
    if collected.get('region'):
        parts.append(str(collected['region']))
    if collected.get('amount'):
        parts.append((str(collected['amount']) + ' ' + str(collected.get('currency') or '')).strip())
    parts.append(str(message or '').strip())
    return '\n'.join(part for part in parts if part)


def _plan_reply(rows):
    lines = [f"{row['service']} {row['plan']}: {row['price']} ₽" for row in rows]
    if len(rows) == 1:
        row = rows[0]
        text = (f"{row['service']} {row['plan']}: {row['price']} ₽\n\n"
                'Это цена из каталога. Оформить можно в боте: выберите этот сервис, тариф и способ оплаты. '
                'Оплата сама по себе не активирует подписку.')
    else:
        text = ('Подходят несколько тарифов:\n' + '\n'.join(lines) +
                '\n\nКакой из этих тарифов выбрать?')
    return {'reply': text,
            'summary': 'Цена взята из каталога по уже открытой теме диалога. Новые условия не добавлялись.',
            'handoff': False, 'source_ids': [],
            'evidence': {'status': 'catalog_direct', 'sources': [],
                         'diagnostic': 'Этап: продолжение диалога. Цена подтверждена строкой каталога.'}}


def _apple_reply(facts, state):
    collected = state.get('collected') or {}
    region = collected.get('region') or ''
    amount = collected.get('amount') or ''
    currency = collected.get('currency') or ''
    if not region or not amount or not currency:
        return None
    from .apple_matching import ALIASES
    from .catalog_context import annotate, direct_apple_reply
    names = ALIASES.get(region) or ()
    region_word = names[0] if names else region
    lookup = f'app store {region_word} {amount} {currency} сколько стоит'
    priced = annotate(dict(facts or {}), lookup)
    return direct_apple_reply(priced, lookup, current=lookup)


def grounded_reply(facts, state):
    """Quote a price only when the open topic and the catalogue identify one."""
    if not state.get('bound'):
        return None
    if state.get('topic') == 'appstore' or state.get('service_key') == 'appstore':
        quoted = _apple_reply(facts, state)
        if quoted:
            return quoted
    hits = state.get('matched_offers') or []
    if hits:
        return _plan_reply(hits)
    offered = state.get('offered') or []
    # Repeating the service, or answering our price question, stays on the listed prices.
    if offered and state.get('restate') and state.get('pending_slot') in ('option', 'goal'):
        return _plan_reply(offered)
    if offered and state.get('answered_price'):
        return _plan_reply(offered)
    return None


def bind_turn(db, chat, text, facts):
    """Attach dialog state to this turn and quote a verified price when one exists."""
    facts = dict(facts or {})
    catalog = list(facts.get('catalog') or []) + list(facts.get('requested_catalog') or [])
    state = interpret(load(db, chat), text, catalog)
    public = {key: state.get(key) for key in empty()}
    public['active'] = bool(state.get('active'))
    public['bound'] = bool(state.get('bound'))
    public['collected'] = dict(state.get('collected') or {})
    public['offered'] = list(state.get('offered') or [])
    facts = dict(facts, dialog_state=public)
    reply = grounded_reply(facts, state) if state.get('bound') else None
    return {'facts': facts, 'grounded': reply,
            'continue_text': continue_text(state, text) if state.get('bound') and not reply else ''}


def ground_claims(reply, facts=None):
    """Drop sentences that name a numbered model the supplied facts do not contain."""
    text = str(reply or '').strip()
    if not text:
        return ''
    corpus = ''
    if isinstance(facts, dict):
        corpus = re.sub(r'\s+', '', json.dumps(facts, ensure_ascii=False, default=str).casefold())
    kept = []
    for part in re.split(r'(?<=[.!?])\s+', text):
        claims = _VERSION_CLAIM.findall(part)
        if claims and not all(re.sub(r'\s+', '', claim.casefold()) in corpus for claim in claims):
            continue
        kept.append(part.strip())
    return ' '.join(part for part in kept if part).strip()


def prepare_outgoing(reply, facts=None):
    """Return reply text and whether ungrounded claims replaced the whole answer."""
    cleaned = ground_claims(reply, facts)
    if str(reply or '').strip() and not cleaned:
        from .handoff import handoff_text
        return handoff_text(), True
    return cleaned or str(reply or ''), False


def asks_for_check(text):
    return bool(_ASK.search(str(text or '')) and re.search(r'провер', str(text or ''), re.I))


def client_collection(text):
    """A question that only asks the client for non-secret data."""
    value = str(text or '')
    if not _ASK.search(value) or _SECRET_ASK.search(value) or _GRANT.search(value):
        return False
    from .completion_claims import has_completion_claim
    return not has_completion_claim(value)


def grants_unverified_benefit(text):
    return bool(_GRANT.search(str(text or '')))
