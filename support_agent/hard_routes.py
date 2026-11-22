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
        if _ASKS_ORDER.search(piece) and not re.search(r'(?i)вижу|получил', piece):
            continue
        out.append(piece)
    return _tidy_text(''.join(out))


def _order_act(answer, burst, context):
    order, fk = _order_refs(_client_text(context) or burst)
    reply = str(answer.get('reply') or '')
    rest = _drop_order_request(reply)
    # Only the shop lookup may say «вижу»; here the number came from the client's own words.
    head = ('Номер заказа #' + order + ' получил.') if order else ('Номер FreeKassa ' + fk + ' получил.')
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


# --- 7. payment fails right now → the owner's answer (21.11.2026) -----------------------------------
_PAY_FAIL = re.compile(r'(?i)не\s+(?:могу|получается|выходит)\s+оплат|оплат\w*\s+не\s+(?:проход|прош)|не\s+проходит\s+оплат'
                       # «не принимает» only about payment: «российский номер не принимает» is a phone question (bench 10)
                       r'|(?:карт\w*|сбп|оплат\w*|qr|qr-код\w*|банк\w*|плат[её]ж\w*)[^.?!\n]{0,40}не\s+принима\w*'
                       r'|не\s+принима\w*[^.?!\n]{0,30}(?:карт|сбп|оплат|qr|банк|плат[её]ж)|по\s+(?:этим\s+)?кнопкам\s+никак|карт\w*\s+заблокир|заблокировал\w*\s+карт'
                       r'|ошибк\w*\s+(?:при\s+)?оплат|оплата\s+не\s+идёт|платёж\s+не\s+проход')
_PAID_ALREADY = re.compile(r'(?i)оплатил\w*|оплачен\w*|списал\w*|деньги\s+ушли')


def _payfail_detect(burst, context, answer):
    reply = str(answer.get('reply') or '')
    return bool(_PAY_FAIL.search(burst)) and not _PAID_ALREADY.search(burst) and 'какую ошибку' not in reply


def _payfail_act(answer, burst, context):
    text = _faq('payment_error')
    if not text:
        return answer
    evidence = dict(answer.get('evidence') or {})
    evidence['owner_text_used'] = True
    reply = str(answer.get('reply') or '')
    greet = re.match(r'(?i)((?:здравствуйте|добрый\s+(?:день|вечер)|доброе\s+утро|привет)[^.!?\n]{0,30}[.!?]?)', reply.strip())
    return dict(answer, reply=((greet.group(1) + ' ') if greet else '') + text, evidence=evidence)


# --- 8. «оплатил, не работает»: only the owner's own text goes out by itself (21.11.2026) ------------
_PAID_ISSUE = re.compile(r'(?i)(?:оплатил\w*|оплачен\w*)[^\n]{0,120}(?:не\s+(?:работа|пришл|активир|появ|отобража|вид)|нет\s+подписк)'
                         r'|(?:не\s+(?:работа|пришл|активир|появ|отобража)\w*)[^\n]{0,120}(?:оплатил\w*|оплачен\w*)')


def _paidissue_detect(burst, context, answer):
    evidence = answer.get('evidence') if isinstance(answer.get('evidence'), dict) else {}
    return (bool(_PAID_ISSUE.search(burst)) and not evidence.get('owner_text_used')
            and not evidence.get('order_action') and not answer.get('handoff'))


def _paidissue_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence['situation_hold'] = True
    evidence.setdefault('diagnostic', 'Оплатил, не работает: свой текст модели уходит только черновиком.')
    return dict(answer, evidence=evidence)


# --- 9. angry / complaint / threat → calm short reply + urgent alert (owner 21.11.2026) ----------------
_CONFLICT = re.compile(r'(?i)(?<!не\s)(?<!не\s\s)\bмошенник|обман\w*|(?:буду|напишу|подам)\s+жалоб\w*|буду\s+жаловат\w*|пожалуюсь|развод\w*\s+(?:на\s+деньги|людей)|кидал|'
                       r'спор\w*\s+в\s+банк|оспор\w*\s+(?:платёж|платеж|операци)|chargeback|чарджбэк|'
                       r'(?:напишу|оставлю)\s+(?:плохой\s+|негативн\w*\s+)?отзыв|в\s+полици|заявлени\w*\s+в|в\s+суд|роспотребнадзор|прокуратур')
CONFLICT_REPLY = 'Понимаю вас. Передал Александру — он разберётся и ответит здесь при первой возможности.'


def _conflict_detect(burst, context, answer):
    # The model's own «high conflict» verdict gets the same calm reply (owner 21.11.2026: «спокойно + сразу вам»).
    if _CONFLICT.search(burst):
        return True
    model_only = bool((answer.get('evidence') or {}).get('conflict_hold'))
    return model_only and not (_REFUND.search(burst) and not _REFUND_QUESTION.search(burst))


def _conflict_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    # Owner 21.11.2026: the calm reply is sent automatically and the owner gets an urgent alert.
    evidence.update(urgent=True, owner_alert=True, owner_text_used=True, conflict_hold=False, hard_route_auto=True)
    return dict(answer, reply=CONFLICT_REPLY, handoff=False, evidence=evidence)


# --- 10. «позовите Александра / вы бот?» → honest, and keep helping (owner 21.11.2026) -----------------
_HUMAN = re.compile(r'(?i)позов\w*\s+(?:александра|сашу|человека|оператора|живого)|хочу\s+(?:с\s+)?(?:человек|живым|оператор|александр)'
                    r'|живой\s+человек|\bвы\s+бот\b|\bты\s+бот\b|это\s+бот\b|с\s+ботом\s+общаюсь|нейросеть\s+отвечает')
HUMAN_LINE = 'Я ИИ-помощник Александра. Александр ответит при первой возможности, а пока я могу помочь'


def _human_detect(burst, context, answer):
    return bool(_HUMAN.search(burst)) and 'ИИ-помощник' not in str(answer.get('reply') or '')


def _human_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence['owner_alert'] = True
    rest = _HUMAN.sub('', burst)
    reply = str(answer.get('reply') or '')
    reply = re.sub(r'(?i)(?:^|\n)[^\n]*(?:это\s+александр|я\s+александр|александр\s+на\s+связи)[^\n]*', '', reply).strip()
    if re.search(r'\?|(?i:как|сколько|можно|какой|где|почему)', rest) and len(reply) > 40:
        return dict(answer, reply=HUMAN_LINE + '.\n\n' + reply, evidence=evidence)
    return dict(answer, reply=HUMAN_LINE + ' — что подсказать?', evidence=evidence, handoff=False)


# --- 11. calm refund request → reason and order number, the owner decides (owner 21.11.2026) ----------
_REFUND = re.compile(r'(?i)верн\w*\s+(?:мне\s+)?(?:деньг|средств|оплат)|возврат\w*\s+(?:денег|средств|оплаты)|хочу\s+возврат|оформ\w*\s+возврат|refund')
REFUND_ASK = ('Подскажите, пожалуйста, номер заказа и что именно не подошло — Александр посмотрит и ответит здесь '
              'при первой возможности.')
REFUND_TAKEN = 'Спасибо, передал Александру — он посмотрит и ответит здесь при первой возможности.'


_REFUND_QUESTION = re.compile(r'(?i)\bесли\b|можно\s+ли|возмож\w*\s+ли|бывает\s+ли|а\s+можно|можно\s+(?:будет\s+)?вернуть|'
                              r'какие\s+условия|условия\s+возврат|когда\s+(?:деньги\s+)?при(?:дут|йдут)|оформили')


def _refund_detect(burst, context, answer):
    # A question about the refund policy or timing is not a refund request: it keeps its FAQ answer.
    return bool(_REFUND.search(burst)) and not _REFUND_QUESTION.search(burst)


def _refund_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_alert=True, owner_text_used=True, refund_hold=False, conflict_hold=False, hard_route_auto=True)
    history = ' '.join(str(m.get('text') or '') for m in (context or {}).get('history') or () if isinstance(m, dict))
    has_order = bool(re.search(r'#?\b\d{4,}\b', burst + ' ' + history))
    has_reason = len(_REFUND.sub('', burst).strip(' ,.!?')) > 15
    reply = REFUND_TAKEN if (has_order and has_reason) else REFUND_ASK
    return dict(answer, reply=reply, handoff=False, evidence=evidence)


# --- 12. Claude: the client agreed to the risks → next step (owner 21.11.2026) --------------------------
_AGREE = re.compile(r'(?i)^\W*(?:да\b|согла\w*|ок\b|окей|хорошо|понял\w*|принима\w*|готов\w*)')
CLAUDE_EMAIL_ASK = 'Спасибо! Пришлите, пожалуйста, почту вашего аккаунта Claude — дальше Александр продолжит оформление.'


def _claude_consent_detect(burst, context, answer):
    history = [m for m in (context or {}).get('history') or () if isinstance(m, dict)]
    last_ours = next((str(m.get('text') or '') for m in reversed(history)
                      if m.get('role') in ('assistant', 'owner')), '')
    return 'согласны с этими рисками' in last_ours and bool(_AGREE.search(burst)) \
        and bool(re.search(r'(?i)\b(?:max|ultra|макс|ультра)\b', last_ours))


def _claude_consent_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_alert=True, owner_text_used=True, hard_route_auto=True)
    return dict(answer, reply=CLAUDE_EMAIL_ASK, handoff=False, evidence=evidence)


# --- 13. paid the wrong plan/service → order number and the needed plan, the owner decides (21.11.2026) --
_WRONG_PLAN = re.compile(r'(?i)(?:оплатил\w*|купил\w*|выбрал\w*|взял\w*)\s+не\s+(?:тот|ту|то)\b|ошибл\w*\s+(?:с\s+)?(?:тариф|сервис)|'
                         r'(?:поменять|сменить|заменить|изменить)\s+(?:мне\s+)?(?:тариф|сервис|заказ)')
WRONG_PLAN_ASK = 'Подскажите, пожалуйста, номер заказа и какой тариф нужен — Александр поменяет при первой возможности.'
WRONG_PLAN_TAKEN = 'Спасибо, передал Александру — он поменяет при первой возможности.'


def _wrong_plan_detect(burst, context, answer):
    return bool(_WRONG_PLAN.search(burst))


def _wrong_plan_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_alert=True, owner_text_used=True, hard_route_auto=True)
    has_order = bool(re.search(r'#?\b\d{4,}\b', burst))
    reply = WRONG_PLAN_TAKEN if has_order and re.search(r'(?i)\bна\s+\w+', burst) else WRONG_PLAN_ASK
    return dict(answer, reply=reply, handoff=False, evidence=evidence)


# --- 14. «я оплатила Zoom» without the bot's notice → the order number first (owner 21.11.2026, round 5) --------
# live 6: «хочется оплаченный текущий профиль» is a wish, not a payment — only the client's own «оплатил(а)/купил(а)».
_PAID_FREE = re.compile(r'(?i)(?:^|[\s,.!])(?:я\s+)?(?:уже\s+)?(?:оплатил|оплатила|оплатили|купил|купила|купили)\b(?!\s*\?)')
_NOT_YET = re.compile(r'(?i)\bесли\b|\bкак\b[^\n]{0,20}оплат|можно\s+(?:ли\s+)?оплат|хочу\s+оплат|хочется|чтобы\s+оплат|не\s+(?:могу|получа\w*|вышло)\s+оплат|ещё\s+не|еще\s+не')
ORDER_FIRST = 'Подскажите, пожалуйста, номер заказа из бота — проверю и подскажу следующий шаг.'


def _paid_free_detect(burst, context, answer):
    evidence = answer.get('evidence') or {}
    if not _PAID_FREE.search(burst) or _NOT_YET.search(burst) or _PAID_ISSUE.search(burst):
        return False
    if _WRONG_PLAN.search(burst) or _REFUND.search(burst) or re.search(r'(?m)^\s*#?\d{3,}\s*$', burst):
        return False  # another rule owns it, or the client is answering with a number
    asked = [m for m in (context or {}).get('history') or () if isinstance(m, dict) and m.get('role') in ('assistant', 'owner')]
    if asked and re.search(r'(?i)номер\w*\s+заказ', str(asked[-1].get('text') or '')):
        return False
    if evidence.get('order_action') or evidence.get('paid_notice') or evidence.get('owner_step_ack') or evidence.get('continue_step'):
        return False
    # bench 11 (21.11.2026): a service-specific owner answer («Оплатил Claude Max» → почта, «где кнопка» → кнопка в боте)
    # already names the next step; the order number is asked only when the reply is the generic «что дальше».
    topic = str((evidence.get('turn_understanding') or {}).get('owner_topic') or '')
    if topic not in ('', 'none', 'what_next', 'unknown_answer'):
        return False
    if re.search(r'(?i)\b(?:где|как|куда)\b', burst):
        return False
    if (re.search(r'(?i)скрин|\[клиент\s+прислал|чек', burst) or (context or {}).get('images')) and not _paid_service_step(burst, context):
        return False  # a screenshot may be the order itself: the normal flow reads it
    history = ' '.join(str(m.get('text') or '') for m in (context or {}).get('history') or () if isinstance(m, dict))
    if re.search(r'(?i)#\s?\d{4,}|заказ\w*\s*(?:№|номер)?\s*\d{4,}|free\s*kassa|\b\d{9}\b', burst + ' ' + history):
        return False
    # bench 15: the generic «что дальше» text itself asks for the order number — a known service step still wins.
    return 'номер заказа' not in str(answer.get('reply') or '') or bool(_paid_service_step(burst, context))


def _paid_service_step(burst, context):
    """Live 6 (#D-48): «Я оплатила zoom» → what Zoom needs now («почта и пароль»), as Alexander answered."""
    try:
        from .payment_guide import _after, kind_for, load_guide
    except Exception:
        return ''
    rows = _asked_rows(burst, context)
    if len({str(r.get('service_id') or '') for r in rows}) != 1:
        return ''
    row = rows[0]
    kind = kind_for(row.get('service_id'), row.get('plan') if len(rows) == 1 else '', (context or {}).get('tariffs'), row.get('service'))
    if kind in ('shop', 'chatgpt_auto', 'chatgpt_manual', 'chatgpt_new_account', 'claude_manual', 'claude_login'):
        return ''  # these need the order itself: the button, the plan or the risks come from the order flow
    step = str(_after(load_guide(), kind, '', None, row.get('service')) or '').strip()
    step = re.sub(r'^После оплаты\s+', '', step)
    return ('Спасибо за оплату 🙂 ' + step[:1].upper() + step[1:]) if step else ''


def _paid_free_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_text_used=True, hard_route_auto=True)
    return dict(answer, reply=_paid_service_step(burst, context) or ORDER_FIRST, handoff=False, evidence=evidence)


# --- 15. a price question gets the price (owner 21.11.2026, @Vmiti: «сколько стоит pro на чат gpt» got only payment steps)
_PRICE_Q = re.compile(r'(?i)\bсколько\b(?![^.?!\n]{0,40}(?:дн|времен|ждать|жду|минут|час|сут|лимит|сообщен|запрос|генерац|токен|прид[её]т|активир|оформ\w*\s+после|после\s+оплат))|\bскок\b|\bцен[аыуе]\b|стоимост\w*|почём|почем|прайс|'
                      r'how\s+much|\bprice')
_PLAN_WORDS = ('pro', 'plus', 'go', 'max', 'ultra', 'premium', 'business', 'team', 'basic', 'standard', 'плюс', 'про', 'макс')
_PLAN_RU = {'плюс': 'plus', 'про': 'pro', 'макс': 'max'}


def _asked_rows(burst, context):
    from .fit_answer import _asked, _services
    tariffs = [r for r in (context or {}).get('tariffs') or () if isinstance(r, dict) and type(r.get('price')) is int]
    names = _asked(burst, _services())
    if not names and re.search(r'(?i)\bплюс\w*\b|\bplus\b', burst):
        # «сколько плюс стоит?» — the service of this dialog, otherwise ChatGPT Plus (bench 15)
        state = str(((context or {}).get('dialog_state') or {}).get('service') or '')
        names = {state or 'ChatGPT'}
    flat = lambda v: re.sub(r'[^a-zа-яё0-9]+', '', str(v or '').casefold())
    def same(name, row):
        # bench 14: the catalogue may say «Suno AI» or «Google Gemini» — a name inside the other is the same service.
        a, b, c = flat(name), flat(row.get('service')), flat(row.get('service_id'))
        return bool(a) and (a == b or a == c or (len(a) >= 4 and (a in b or a in c)) or (len(b) >= 4 and b in a))
    rows = [r for r in tariffs if any(same(n, r) for n in names)]
    rows = [r for r in rows if not _NOT_OFFERED.search(str(r.get('service') or ''))]
    words = {(_PLAN_RU.get(w, w)) for w in re.findall(r'[a-zа-яё]+', burst.casefold()) if w in _PLAN_WORDS}
    if words:
        narrowed = [r for r in rows if any(re.search(r'(?i)(?<![a-z])' + w, str(r.get('plan') or '')) for w in words)]
        rows = narrowed or rows
    return rows


_TROUBLE = re.compile(r'(?i)оформил\w*|оплатил\w*|купил\w*|не\s+(?:работа|отража|появ|приш|активир|получа|грузит|открыва)\w*|'
                      r'слетел\w*|пропал\w*|отключил\w*|ошибк\w*|проблем\w*|просит\s+оформить|не\s+видн\w*')
_BUY_Q = re.compile(r'(?i)\bхочу\b|\bнуж(?:ен|на|но)\b|купить|куплю|оформить|возьму|приобрест|подключить')


_NOT_OFFERED = re.compile(r'(?i)supergrok')  # owner: Grok is sold through Cursor's unified plans


def _missing_rows(burst, context, reply):
    """Rows of the asked services whose price the reply does not give."""
    flat = lambda v: re.sub(r'[^a-zа-яё0-9]+', '', str(v or '').casefold())
    said = flat(reply)
    rows = _asked_rows(burst, context)
    if '₽' not in reply:
        return rows
    return [r for r in rows if flat(r.get('service')) not in said and not _NOT_OFFERED.search(str(r.get('service') or ''))]


def latin_only(burst):
    """A client writing in another language (English, Kazakh, Ukrainian…) gets the model's reply in his language:
    no Russian template is added (owner 22.11.2026: «на казахском — на казахском»)."""
    text = str(burst or '')
    if re.search(r'[әғқңөұүһ]', text, re.I) or re.search(r'[іїєґ]', text, re.I):
        return True  # Kazakh or Ukrainian letters
    return not re.search(r'[а-яё]', text, re.I) and bool(re.search(r'[a-z]{3}', text, re.I))


def _price_detect(burst, context, answer):
    evidence = answer.get('evidence') or {}
    if latin_only(burst):
        return False
    reply = str(answer.get('reply') or '')
    if answer.get('handoff') or evidence.get('continue_step') or evidence.get('owner_step_ack') or evidence.get('order_action'):
        return False
    if _PAID_FREE.search(burst) or _PAID_ISSUE.search(burst):
        return False
    if _TROUBLE.search(burst):
        return False  # live 6 (#D-60): «оформила Go, но не отражается» is a problem, not a purchase
    priced = bool(_PRICE_Q.search(burst))
    buying = bool(_BUY_Q.search(burst)) and '₽' not in reply  # «мидджорни хочу купить» gets the prices too
    if not (priced or buying):
        return False
    return 0 < len(_missing_rows(burst, context, reply)) <= 12


def _price_act(answer, burst, context):
    from .turn_understanding import tariff_question
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_text_used=True, hard_route_auto=True)
    rest = str(answer.get('reply') or '').strip()
    if '₽' in rest:
        # Prices of some asked services are there, others are missing («клод про и перплексити, сколько за оба»).
        extra = _missing_rows(burst, context, rest)
        lines = ['• ' + (str(r.get('service') or '') + ' ' + str(r.get('plan') or '')).strip() + ' — ' + str(r['price']) + ' ₽'
                 for r in sorted(extra, key=lambda r: r['price'])]
        return dict(answer, reply=(rest + '\n\n' + '\n'.join(lines)).strip(), handoff=False, evidence=evidence)
    prices = tariff_question(_asked_rows(burst, context))
    # The list already asks which tariff: the model's own «Уточните: Basic, Standard или Pro?» would repeat it.
    rest = '\n\n'.join(p for p in re.split(r'\n\s*\n', rest)
                         if not re.fullmatch(r'(?is)\s*(?:уточните|какой\s+(?:тариф|из)|выберите|подскажите,?\s+(?:пожалуйста,?\s+)?какой)[^\n]{0,200}\?\s*', p))
    # The reply keeps whatever else it answered («сколько стоит зум и как оплатить»); a «no price» line goes.
    rest = '\n'.join(l for l in rest.split('\n') if not re.search(r'(?i)цен\w*\s+(?:сейчас\s+)?не\s+вижу|цены\s+этого|не\s+вижу\s+цен|точн\w+\s+цен\w*\s+не', l)).strip()
    if re.search(r'Откройте бота и нажмите', rest) and not re.search(r'(?i)как\s+(?:мне\s+)?(?:оплат|купит|оформ|приобр)', burst):
        rest = ''  # only the price was asked: the tariff list already says how to pay in one line
    from .playbook import dedupe
    return dict(answer, reply=dedupe((prices + ('\n\n' + rest if rest else '')).strip()), handoff=False, evidence=evidence)


# --- 16. App Store region: never «нет» while the catalogue has it (owner 21.11.2026: «регион есть, а помощник говорит нет»)
_REGION_DENIED = re.compile(r'(?i)(?:регион\w*|для\s+\w+|пополнени\w*|номинал\w*|карт\w*)[^.!?\n]{0,60}(?:\bнет\b|недоступ\w*|не\s+прода[её]\w*|не\s+можем|отсутству\w*)'
                            r'|(?:\bнет\b|недоступ\w*)[^.!?\n]{0,40}(?:регион\w*|пополнени\w*|номинал\w*)')


def _region_rows(burst, context):
    try:
        from .apple_matching import region_codes
    except Exception:
        return []
    asked = region_codes(burst)
    if not asked:
        return []
    return [r for r in (context or {}).get('nominals') or () if isinstance(r, dict)
            and asked & region_codes(str(r.get('region') or ''), stock=True) and type(r.get('price')) is int]


def _region_detect(burst, context, answer):
    reply = str(answer.get('reply') or '')
    return bool(_REGION_DENIED.search(reply)) and bool(_region_rows(burst, context))


def _region_act(answer, burst, context):
    rows = _region_rows(burst, context)
    title = str(rows[0].get('region') or '').split('|')[0].strip()
    lines = [str(r.get('nominal') or '') + ': ' + str(r['price']) + ' ₽' for r in rows]
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_text_used=True, hard_route_auto=True)
    text = ('Да, для региона ' + title + ' пополнение есть 🙂 Сейчас доступны такие номиналы:\n\n' + '\n'.join(lines)
            + '\n\nОформить можно в боте @neirosetkaa_bot: «Каталог» → «App Store / iCloud» → регион → номинал. '
            'Код придёт в чат бота сразу после оплаты. Какой номинал нужен?')
    return dict(answer, reply=text, handoff=False, evidence=evidence)


# --- 17. a year or several months at once: honest answer from the catalogue (owner 21.11.2026) ---------------
_YEARLY = re.compile(r'(?i)на\s+(?:целый\s+)?год|годов\w*|на\s+(?:\d+|два|три|четыре|пять|шесть|несколько)\s+месяц\w*|полгода|'
                     r'сразу\s+на\s+(?:\d+|несколько)|12\s+месяц|year|annual')
_YEAR_PLAN = re.compile(r'(?i)год|годов|year|annual|12\s*мес')


def _yearly_detect(burst, context, answer):
    return bool(_YEARLY.search(burst)) and not _PAID_FREE.search(burst)


def _yearly_act(answer, burst, context):
    from .fit_answer import _asked, _services
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_text_used=True, hard_route_auto=True)
    service_rows = [r for r in _asked_rows(re.sub(r'(?i)\b(?:pro|plus|max|ultra|про|плюс|макс)\b', '', burst), context)]
    names = sorted({str(r.get('service') or '') for r in service_rows if r.get('service')})
    if not names:
        state = str(((context or {}).get('dialog_state') or {}).get('service') or '')
        names = [state] if state else []
        service_rows = [r for r in (context or {}).get('tariffs') or () if isinstance(r, dict) and str(r.get('service') or '') == state]
    if not names:
        text = ('Подписки оформляются на месяц, автосписаний нет 🙂 Годовые тарифы есть не на все сервисы — '
                'подскажите, какой сервис нужен, проверю по каталогу.')
    else:
        yearly = [r for r in service_rows if _YEAR_PLAN.search(str(r.get('plan') or '') + ' ' + str(r.get('service') or ''))
                  and type(r.get('price')) is int]
        if yearly:
            lines = ['• ' + (str(r.get('service') or '') + ' ' + str(r.get('plan') or '')).strip() + ' — ' + str(r['price']) + ' ₽'
                     for r in sorted(yearly, key=lambda r: r['price'])]
            text = ('Да, годовой тариф есть 🙂\n' + '\n'.join(lines)
                    + '\n\nОформить можно в боте @neirosetkaa_bot: «Каталог» → сервис → тариф, оплата по СБП или картой «Мир».')
        else:
            text = ('Годового тарифа на ' + ', '.join(names) + ' в каталоге сейчас нет — подписка оформляется на месяц. '
                    'Автосписаний нет, а продлить можно в боте в любой момент, когда текущий срок закончится 🙂')
    rest = str(answer.get('reply') or '').strip()
    rest = '\n'.join(l for l in rest.split('\n') if not _YEARLY.search(l) and not re.search(r'(?i)на\s+месяц|только\s+на\s+1', l)).strip()
    from .playbook import dedupe
    return dict(answer, reply=dedupe(text + ('\n\n' + rest if rest else '')), handoff=False, evidence=evidence)


# --- 18. documents or payment from a company → the owner (owner 21.11.2026) -------------------------------------
_INVOICE = re.compile(r'(?i)юр\.?\s*лиц\w*|юрлиц\w*|\bип\b|\bооо\b|\bоао\b|\bзао\b|счёт\s+на\s+оплату|счет\s+на\s+оплату|закрывающ\w*|\bдоговор\w*|безнал\w*|'
                      r'от\s+(?:компании|организации|фирмы)|с\s+р/?с|расч[её]тн\w+\s+сч[её]т|\bупд\b|акт\w*\s+выполненных')


def _invoice_detect(burst, context, answer):
    return bool(_INVOICE.search(burst))


def _invoice_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_alert=True, owner_text_used=True, hard_route_auto=True)
    return dict(answer, reply=_faq('invoice_legal') or answer.get('reply'), handoff=False, evidence=evidence)


# --- 19. a receipt but no order in the bot → Alexander checks the payment (owner 21.11.2026, answer 19) -------
_RECEIPT_NO_ORDER = re.compile(r'(?i)(?:чек|квитанц|скрин\w*\s+оплат)[^.!?\n]{0,60}(?:заказ\w*\s+(?:нет|не\s+(?:вижу|видно|появил|приш))|нет\s+заказ|не\s+(?:появил|приш|вижу))'
                               r'|(?:заказ\w*\s+(?:нет|не\s+(?:вижу|видно|появил))|нет\s+заказ)[^.!?\n]{0,60}(?:чек|квитанц)')
RECEIPT_TAKEN = 'Чек получил — Александр проверит оплату и ответит здесь при первой возможности 🙂'


def _receipt_detect(burst, context, answer):
    return bool(_RECEIPT_NO_ORDER.search(burst))


def _receipt_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_alert=True, owner_text_used=True, hard_route_auto=True)
    return dict(answer, reply=RECEIPT_TAKEN, handoff=False, evidence=evidence)


# --- 20. link-pay service, the client cannot get the payment link → email and password (owner 21.11.2026, answer 5)
_NO_LINK = re.compile(r'(?i)(?:ссылк\w*)[^.!?\n]{0,50}(?:не\s+(?:могу|получа|выход|найт|открыва)\w*|никак)|не\s+(?:могу|получа\w*)\s+(?:\w+\s+){0,3}ссылк')


def _nolink_detect(burst, context, answer):
    if not _NO_LINK.search(burst):
        return False
    try:
        from .payment_guide import kind_for
    except Exception:
        return False
    rows = _asked_rows(burst, context)
    return bool(rows) and kind_for(rows[0].get('service_id'), '', (context or {}).get('tariffs'), rows[0].get('service')) == 'link_pay'


def _nolink_act(answer, burst, context):
    name = str(_asked_rows(burst, context)[0].get('service') or '').strip()
    paid = bool(_PAID_FREE.search(burst))
    text = ('Ничего страшного, оформим сами 🙂 ' + ('Пришлите' if paid else 'После оплаты в боте @neirosetkaa_bot пришлите')
            + ' сюда почту и пароль от аккаунта ' + name + '. Если на почту придёт код подтверждения — его тоже пришлите сюда.')
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_text_used=True, hard_route_auto=True)
    return dict(answer, reply=text, handoff=False, evidence=evidence)


# --- 21. an older owner line superseded by his newer rule is not a «clash» (bench 9-14: driving licence) -----------
def _superseded_detect(burst, context, answer):
    evidence = answer.get('evidence') or {}
    if not evidence.get('owner_alignment_hold'):
        return False
    owner = ' '.join(str(m.get('text') or '') for m in (context or {}).get('history') or ()
                     if isinstance(m, dict) and m.get('role') == 'owner')
    reply = str(answer.get('reply') or '')
    # Owner 20.11.2026: Claude verification — driving licences are not accepted any more (his old line said they are).
    return bool(re.search(r'(?i)водительск', owner)) and bool(re.search(r'(?i)загран|паспорт|верификац', reply))


def _superseded_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_alignment_hold=False, superseded_owner_line=True)
    reply = str(answer.get('reply') or '')
    if not re.search(r'(?i)загран', reply):
        reply = (reply + '\n\n' + _faq('claude_verification').split('\n\n')[0]).strip()
    return dict(answer, reply=reply, handoff=False, evidence=evidence)


# --- 22. owner 22.11.2026: FKwallet QR, Claude blocked + money back, team plan, «где мой заказ» -----------------
_FKWALLET = re.compile(r'(?i)fk\s*wallet|фк\s*валл?ет|кошел[её]к\w*\s+(?:скачать|просит|установ)|(?:скачать|установить)\s+(?:какой-то\s+)?кошел')
FKWALLET_REPLY = ('Этот QR — для оплаты через приложение FKwallet, ставить его не обязательно 🙂 Рядом должен быть способ '
                  'оплаты просто по СБП — выберите его. Если его нет, пропишите в боте @neirosetkaa_bot /start и оформите новый заказ.')
_CLAUDE_BLOCK_MONEY = re.compile(r'(?i)(?:клод|claude)[^.!?\n]{0,80}(?:заблок|бан|блокир)|(?:заблок|бан|блокир)[^.!?\n]{0,80}(?:клод|claude)')
CLAUDE_BLOCK_REPLY = ('Понимаю, очень неприятно. К сожалению, гарантии на Claude нет: при блокировке аккаунта деньги не возвращаются — '
                      'мы предупреждаем об этом до оплаты. Передал Александру, он посмотрит вашу ситуацию и ответит здесь при первой возможности.')
_TEAM = re.compile(r'(?i)на\s+команд\w*|для\s+команд\w*|на\s+\d+\s+(?:человек|сотрудник|пользовател)|для\s+сотрудник\w*|\bteam\b')  # «Business» itself stays with the owner's chatgpt_business rule
_WHERE_ORDER = re.compile(r'(?i)где\s+(?:мой\s+|мой\s+оплаченный\s+)?заказ|заказ\w*\s+(?:так\s+и\s+)?не\s+(?:приш|появил|пришёл)|'
                          r'оплатил\w*[^.!?\n]{0,40}(?:ничего\s+не\s+(?:приш|пришло)|ничего\s+нет|тишина)')


def _fkwallet_detect(burst, context, answer):
    return bool(_FKWALLET.search(burst))


def _fkwallet_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_text_used=True, hard_route_auto=True)
    return dict(answer, reply=FKWALLET_REPLY, handoff=False, evidence=evidence)


def _claude_block_detect(burst, context, answer):
    return bool(_CLAUDE_BLOCK_MONEY.search(burst)) and bool(re.search(r'(?i)верн|возврат|деньг', burst))


def _claude_block_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_alert=True, owner_text_used=True, hard_route_auto=True, conflict_hold=False, refund_hold=False)
    return dict(answer, reply=CLAUDE_BLOCK_REPLY, handoff=False, evidence=evidence)


def _team_detect(burst, context, answer):
    return bool(_TEAM.search(burst)) and bool(re.search(r'(?i)гпт|gpt|чат|chatgpt|джипити', burst))


def _team_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_text_used=True, hard_route_auto=True)
    rows = [r for r in (context or {}).get('tariffs') or () if isinstance(r, dict) and type(r.get('price')) is int
            and re.search(r'(?i)chat\s*gpt', str(r.get('service') or '') + str(r.get('service_id') or ''))
            and re.search(r'(?i)business|team|бизнес|команд', str(r.get('plan') or ''))]
    if rows:
        from .turn_understanding import tariff_question
        return dict(answer, reply='Для команды есть такой вариант 🙂\n\n' + tariff_question(rows), handoff=False, evidence=evidence)
    evidence['owner_alert'] = True
    return dict(answer, reply='Понял вас! По подписке ChatGPT на команду подскажет Александр — он ответит здесь при первой возможности 🙂',
                handoff=False, evidence=evidence)


def _where_order_detect(burst, context, answer):
    evidence = answer.get('evidence') or {}
    if evidence.get('order_action') or evidence.get('conflict_hold') or re.search(r'(?i)#\s?\d{4,}|\b\d{5,}\b', burst):
        return False  # with a number the order flow checks the shop itself
    return bool(_WHERE_ORDER.search(burst))


def _where_order_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_text_used=True, hard_route_auto=True)
    return dict(answer, reply='Подскажите, пожалуйста, номер заказа из бота — проверю его статус и подскажу, что дальше 🙂',
                handoff=False, evidence=evidence)


# --- 23. a service that is not in the catalogue got the generic «Каталог → выберите сервис» (bench 15: Netflix, Duolingo)
_GENERIC_GUIDE = re.compile(r'(?i)Выберите\s+нужный\s+сервис\s+и\s+тариф|Выберите\s+сервис\s+и\s+нужный\s+тариф')
_PAY_ASK = re.compile(r'(?i)оплат\w*|купит\w*|купл\w*|приобр\w*|подписк\w*|подключ\w*|\bесть\b|можно')
_COMMON = re.compile(r'(?i)^(?:можно|можете|можешь|оплат\w*|купит\w*|купл\w*|приобр\w*|подписк\w*|подключ\w*|есть|ли|а|и|у|вас|вы|мне|мой|мою|'
                     r'нам|на|в|за|для|через|как|где|сколько|стоит|цен\w*|хочу|нужен|нужна|нужно|пожалуйста|привет|здравствуйте|добрый|'
                     r'день|вечер|утро|тариф\w*|месяц\w*|год|бот\w*|ваш\w*|это|эт\w*|тоже|ещё|еще|уже|сейчас|супер|премиум|premium|'
                     r'плюс|plus|pro|про|аккаунт\w*|сервис\w*|подскажите|скажите|интересует|возможно|сделать|оформить|оформите|'
                     r'вашем|через|рублях|рубл\w*|долл\w*|карт\w*|сбп|мир)$')


def _unknown_names(burst):
    from .fit_answer import _asked, _services
    if _asked(burst, _services()):
        return []
    words = re.findall(r'[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё.\-]{3,}', burst)
    return [w for w in words if not _COMMON.match(w.strip('.-'))]


def _offcat_detect(burst, context, answer):
    reply = str(answer.get('reply') or '')
    return (bool(_GENERIC_GUIDE.search(reply)) and bool(_PAY_ASK.search(burst)) and len(burst) < 160
            and bool(_unknown_names(burst)))


def _offcat_act(answer, burst, context):
    evidence = dict(answer.get('evidence') or {})
    evidence.update(owner_text_used=True, hard_route_auto=True)
    return dict(answer, reply=_faq('off_catalog') or answer.get('reply'), handoff=False, evidence=evidence)


RULES = (
    ('superseded_owner_line', _superseded_detect, _superseded_act),
    ('claude_blocked_money', _claude_block_detect, _claude_block_act),
    ('fkwallet_qr', _fkwallet_detect, _fkwallet_act),
    ('chatgpt_team', _team_detect, _team_act),
    ('where_is_my_order', _where_order_detect, _where_order_act),
    ('conflict', _conflict_detect, _conflict_act),
    ('receipt_no_order', _receipt_detect, _receipt_act),
    ('linkpay_no_link', _nolink_detect, _nolink_act),
    ('invoice_legal', _invoice_detect, _invoice_act),
    ('paid_without_order', _paid_free_detect, _paid_free_act),
    ('wrong_plan', _wrong_plan_detect, _wrong_plan_act),
    ('refund_request', _refund_detect, _refund_act),
    ('claude_consent', _claude_consent_detect, _claude_consent_act),
    ('reseller', _reseller_detect, _reseller_act),
    ('human_or_bot', _human_detect, _human_act),
    ('payment_failure', _payfail_detect, _payfail_act),
    ('chatgpt_business', _business_detect, _business_act),
    ('known_order_number', _order_detect, _order_act),
    ('unknown_phrase_glued', _glued_detect, _glued_act),
    ('payment_method_unknown', _pay_detect, _pay_act),
    ('claude_no_driving_licence', _licence_detect, _licence_act),
    ('yearly_plan', _yearly_detect, _yearly_act),
    ('appstore_region_exists', _region_detect, _region_act),
    ('price_without_price', _price_detect, _price_act),
    ('off_catalog_generic_guide', _offcat_detect, _offcat_act),
    ('paid_issue_draft_only', _paidissue_detect, _paidissue_act),
)
STOP_AFTER = {'claude_blocked_money', 'fkwallet_qr', 'chatgpt_team', 'where_is_my_order', 'reseller', 'conflict', 'wrong_plan', 'paid_without_order', 'receipt_no_order', 'linkpay_no_link', 'invoice_legal', 'yearly_plan', 'appstore_region_exists', 'refund_request', 'claude_consent'}


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
