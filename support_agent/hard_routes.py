"""Hard rules: a small deterministic layer applied to EVERY final answer (called at the end of playbook.tidy).

The cheap model sometimes picks the wrong topic for the same situations again and again. These rules
fix the final reply by what the client actually wrote, regardless of the chosen topic.
Each rule is (name, detector, action); a rule that raises is skipped, the reply is never left empty.
"""
import re

UNKNOWN_PHRASE = 'К сожалению, точно подсказать не могу. Можете уточнить ваш вопрос или перефразировать его?'
PAYMENT_LINE = 'Оплатить можно только по СБП или картой «Мир» в боте @neirosetkaa_bot.'
RESELLER_REPLY = 'Передам ваш вопрос Александру, он ответит здесь.'
RESELLER_REASON = 'Перекупщик/опт — отвечает Александр.'


def _faq(topic_id):
    try:
        from .turn_understanding import _faq_answer
        return _faq_answer(topic_id) or ''
    except Exception:
        return ''


def _client_text(context):
    """The client's current messages: the burst plus the trailing client messages of the history."""
    context = context or {}
    parts = [str(context.get('burst') or '')]
    history = list(context.get('history') or [])
    tail = []
    while history and isinstance(history[-1], dict) and history[-1].get('role') == 'client':
        tail.append(str(history.pop().get('text') or ''))
    parts.extend(reversed(tail))
    return '\n'.join(p for p in parts if p.strip())


def _tidy_text(text):
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


# --- 1. reseller / wholesale -------------------------------------------------------------------
_RESELLER = re.compile(
    r'(?i)\bопт(?:ом|ов\w*)?\b|перепрод|перекуп|\bplati\b|plati\.?market|плати\s*маркет|плати\.?ру'
    r'|партн[её]рск\w*\s+цен|цен\w*\s+для\s+партн|wholesale|\bresell|\bre-sell'
    r'|на\s+постоянк\w*[^\n]{0,80}(?:цен\w*\s+ниже|дешевл|ниже\s+цен)')


def _reseller_detect(burst, context, answer):
    return bool(_RESELLER.search(burst))


def _reseller_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence['handoff_reason'] = RESELLER_REASON
    return dict(answer, reply=RESELLER_REPLY, handoff=True, evidence=evidence)


# --- 2. ChatGPT Business ----------------------------------------------------------------------
_BUSINESS = re.compile(r'(?i)бизнес|business|\bteam\b|\bтим\b')
_GPT = re.compile(r'(?i)gpt|гпт|жпт|чат\s*гпт|chat\s*gpt|джипити|openai')
_OTHER_SERVICE = re.compile(
    r'(?i)canva|канв|claude|клод|gemini|гемин|google|гугл|zoom|зум|notion|ноушн|slack|linkedin|microsoft|copilot'
    r'|cursor|курсор|github|figma|adobe|perplexity|перплекс|midjourney|миджорн|grok|грок|capcut|spotify|youtube'
    r'|apple|telegram|телеграм|heygen|elevenlabs|gamma|manus|genspark|make\.com|runway|krea|freepik|suno|kling')
_GREETING = re.compile(r'(?i)^(?:здравствуйте|добрый\s+(?:день|вечер|утро)|доброе\s+утро|привет|приветствую)\b')


def _business_detect(burst, context, answer):
    if not _BUSINESS.search(burst):
        return False
    if not _GPT.search(burst) and _OTHER_SERVICE.search(burst):
        return False
    text = _faq('chatgpt_business')
    reply = str(answer.get('reply') or '')
    return bool(text) and text not in reply


def _business_act(answer, burst, context):
    text = _faq('chatgpt_business')
    reply = str(answer.get('reply') or '').strip()
    first = reply.split('\n', 1)[0].strip()
    head = ''
    if _GREETING.search(first):
        # keep only the greeting itself, not the rest of the wrong-topic sentence
        sentence = re.split(r'(?<=[.!?])\s+', first, maxsplit=1)[0]
        head = sentence if len(sentence) <= 60 else ''
    evidence = dict(answer.get('evidence') or {})
    evidence['owner_text_used'] = True
    return dict(answer, reply=(head + '\n\n' + text) if head else text, evidence=evidence)


# --- 3. order number already given --------------------------------------------------------------
_ORDER_PATTERNS = (
    re.compile(r'(?i)номер\w*\s+заказа\s*[:№#]?\s*№?\s*#?\s*(\d{3,})'),
    re.compile(r'(?i)заказ\w*\s*(?:№|#|N|n)\s*(\d{3,})'),
    re.compile(r'(?i)заказ\w*\s+(\d{3,})'),
    re.compile(r'#\s?(\d{3,})'),
)
_FREEKASSA = re.compile(r'(?i)(?:free\s*kassa|фри\s*касс\w*)\s*[:№#]?\s*№?\s*#?\s*(\d{3,})')
_ASKS_ORDER = re.compile(r'(?i)номер\w*\s+заказа')
_VERB = r'(?:Подскажите|Напишите|Пришлите|Уточните|Сообщите|Укажите|Отправьте|подскажите|напишите|пришлите|уточните|сообщите|укажите|отправьте)'


def _order_refs(text):
    clean = _FREEKASSA.sub(' ', text)
    order = ''
    for pattern in _ORDER_PATTERNS:
        m = pattern.search(clean)
        if m:
            order = m.group(1)
            break
    fk = _FREEKASSA.search(text)
    return order, (fk.group(1) if fk else '')


def _order_detect(burst, context, answer):
    order, fk = _order_refs(_client_text(context) or burst)
    return bool(order or fk) and bool(_ASKS_ORDER.search(str(answer.get('reply') or '')))


def _drop_order_request(reply):
    order_ = r'номер\w*\s+заказа(?:\s+из\s+бота)?'
    # «Подскажите, пожалуйста, номер заказа из бота и пришлите скриншот…» → «Пришлите, пожалуйста, скриншот…»
    def lead(m):
        nxt = m.group('next')
        tail = (', пожалуйста' if m.group('comma') else ', пожалуйста,') if m.group('pl') else ''
        return nxt[:1].upper() + nxt[1:] + tail + (m.group('comma') or '')
    reply = re.sub(r'\b' + _VERB + r',?\s*(?P<pl>пожалуйста,?\s*)?' + order_ + r'\s+и\s+(?P<next>[а-яё]+те)\b(?P<comma>,)?',
                   lead, reply)
    # «пришлите номер заказа и скриншот» → «пришлите скриншот»
    reply = re.sub(r'(?i)' + order_ + r'\s*,?\s+и\s+', '', reply)
    # «пришлите скриншот и номер заказа» → «пришлите скриншот»
    reply = re.sub(r'(?i),?\s+и\s+' + order_, '', reply)
    # whole sentences that still ask for the order number («Если номера заказа нет, пришлите чек…»)
    pieces = re.split(r'(?<=[.!?])(\s+)', reply)
    out = []
    for piece in pieces:
        if _ASKS_ORDER.search(piece) and not re.search(r'(?i)вижу', piece):
            continue
        out.append(piece)
    return _tidy_text(''.join(out))


def _order_act(answer, burst, context):
    order, fk = _order_refs(_client_text(context) or burst)
    reply = str(answer.get('reply') or '')
    rest = _drop_order_request(reply)
    head = ('Заказ #' + order + ' вижу.') if order else ('Номер FreeKassa ' + fk + ' вижу.')
    greet = re.match(r'(?i)((?:здравствуйте|добрый\s+(?:день|вечер)|доброе\s+утро|привет)[^.!?\n]{0,30}[.!?]?)\s*', rest)
    if greet:
        new = greet.group(1).strip() + ' ' + head + ' ' + rest[greet.end():]
    else:
        new = (head + ' ' + rest) if rest else head
    return dict(answer, reply=_tidy_text(new))


# --- 4. unknown phrase never glued to a real answer ----------------------------------------------
def _glued_detect(burst, context, answer):
    reply = str(answer.get('reply') or '')
    if UNKNOWN_PHRASE not in reply:
        return False
    rest = reply.replace(UNKNOWN_PHRASE, '').strip()
    return len(rest) > 40


def _glued_act(answer, burst, context):
    reply = str(answer.get('reply') or '').replace(UNKNOWN_PHRASE, '')
    return dict(answer, reply=_tidy_text(reply))


# --- 5. payment method questions never get the unknown phrase --------------------------------------
_PAY_METHOD = re.compile(
    r'(?i)крипт|crypto|usdt|tether|биткоин|bitcoin|btc|paypal|пэйпал|пейпал|зарубежн\w*\s+карт|иностранн\w*\s+карт'
    r'|visa|виза\b|mastercard|мастеркард|перевод\w*\s+на\s+карт|переве\w*\s+на\s+карт|номер\w*\s+карт|на\s+карту\s+скин')


def _pay_detect(burst, context, answer):
    if not _PAY_METHOD.search(burst):
        return False
    reply = str(answer.get('reply') or '')
    return UNKNOWN_PHRASE in reply and len(reply.replace(UNKNOWN_PHRASE, '').strip()) <= 40


def _pay_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence['owner_text_used'] = True
    return dict(answer, reply=_faq('card_payment') or PAYMENT_LINE, evidence=evidence)


# --- 6. Claude verification: no driving licence --------------------------------------------------
_LICENCE = re.compile(r'(?i)водительск\w*\s+прав\w*|водительск\w*\s+удостоверени\w*')


def _licence_detect(burst, context, answer):
    return bool(_LICENCE.search(str(answer.get('reply') or '')))


def _licence_act(answer, burst, context):
    reply = str(answer.get('reply') or '')
    lic = r'(?:водительск\w*\s+прав\w*|водительск\w*\s+удостоверени\w*)'
    reply = re.sub(r'(?i)\s*(?:,|\s)\s*(?:или|либо|и)\s+' + lic, '', reply)
    reply = re.sub(r'(?i)' + lic + r'\s*(?:,|\s)\s*(?:или|либо|и)\s+', '', reply)
    reply = re.sub(r'(?i),\s*' + lic, '', reply)
    if _LICENCE.search(reply):
        # the licence is the subject of its own sentence — drop that sentence
        parts = re.split(r'(?<=[.!?])(\s+)', reply)
        reply = ''.join(p for p in parts if not _LICENCE.search(p))
    return dict(answer, reply=_tidy_text(reply))


RULES = (
    ('reseller', _reseller_detect, _reseller_act),
    ('chatgpt_business', _business_detect, _business_act),
    ('known_order_number', _order_detect, _order_act),
    ('unknown_phrase_glued', _glued_detect, _glued_act),
    ('payment_method_unknown', _pay_detect, _pay_act),
    ('claude_no_driving_licence', _licence_detect, _licence_act),
)
STOP_AFTER = {'reseller'}


def apply(answer, context):
    """Run every hard rule in order on the final answer; never raises, never returns an empty reply."""
    if not isinstance(answer, dict):
        return answer
    try:
        original = str(answer.get('reply') or '')
        burst = str((context or {}).get('burst') or '')
        fired = []
        current = answer
        for name, detect, act in RULES:
            try:
                if not detect(burst, context, current):
                    continue
                result = act(current, burst, context)
                if not isinstance(result, dict) or not str(result.get('reply') or '').strip():
                    continue
                current = result
                fired.append(name)
                if name in STOP_AFTER:
                    break
            except Exception:
                continue
        if not fired:
            return answer
        if not str(current.get('reply') or '').strip():
            current = dict(current, reply=original)  # rule 7: a filter never leaves the client an empty answer
        evidence = dict(current.get('evidence') or {}) if isinstance(current.get('evidence'), dict) else {}
        evidence['hard_routes'] = list(evidence.get('hard_routes') or []) + fired
        return dict(current, evidence=evidence)
    except Exception:
        return answer
