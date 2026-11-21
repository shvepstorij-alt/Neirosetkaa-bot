"""The minimum of a consultation: what a reply on a frequent topic must tell the client.

Owner 21.11.2026 (bench 12): «помощник должен больше подсказывать клиентам». The replies were true but thin:
«VPN в каталоге нет» without the owner's VPN recommendations, «по внутреннему паспорту не стоит» without
«лучше иностранные документы или загранпаспорт РФ», «продление — новая покупка» without where to buy.

Each rule: when the client's message is about the topic and the reply misses the key fact, the owner's own text
for that fact is added. Runs last on every reply (playbook.tidy), after the hard routes. Hand-overs, conflicts,
the owner's step and paid-order flows are left alone: there the short answer is the right one.
"""
import re


def _faq(topic, first_paragraphs=None):
    try:
        from .faq import usable_answer
        text = str(usable_answer(topic) or '').strip()
    except Exception:
        return ''
    if first_paragraphs:
        text = '\n\n'.join(re.split(r'\n\s*\n', text)[:first_paragraphs]).strip()
    return text


RENEW_LINE = ('Продлить можно в боте @neirosetkaa_bot: «Каталог» → сервис → тариф, оплата по СБП или картой «Мир». '
              'Продление оформляется как новая покупка; после оплаты подскажу, что понадобится.')

_VPN_ASK = re.compile(r'(?i)(?:впн|vpn)')
_VPN_TROUBLE = re.compile(r'(?i)настро|не\s+(?:работа|подключа|включа)|ошибк|сменить\s+стран|поменять\s+стран|в\s+рубл')
_KYC = re.compile(r'(?i)верификац|kyc|паспорт|документ\w*\s+(?:для|на)\s+клод|права\b')
_KYC_BY_US = re.compile(r'(?i)kyc|за\s+(?:меня|вас|нас)|проходите\s+вы|прохождени\w*\s+верификац')
_RENEW = re.compile(r'(?i)продл\w*|продолжить\s+подписк|ещё\s+на\s+месяц|еще\s+на\s+месяц')
_PROBLEM = re.compile(r'(?i)проблем\w*|не\s+(?:получа|работа|прош|приход|активир|отража)\w*|ошибк\w*|слетел\w*|снова\s+как')
_DISCOUNT = re.compile(r'(?i)скидк\w*|дешевле|промокод\w*|подешевле|акци[ия]')
_GAMES = re.compile(r'(?i)robux|робукс\w*|roblox|роблокс\w*|ps\s*plus|пс\s*плюс|game\s*pass|гейм\s*пасс|steam|стим\w*|nintendo|нинтендо|'
                    r'пополн\w*\s+(?:пс|ps|playstation|xbox|иксбокс)|гифт|подарочн\w*\s+карт')
_NOT_IN_CATALOG = re.compile(r'(?i)[^.!?\n]*(?:в\s+каталоге\s+(?:сейчас\s+)?нет|нет\s+в\s+каталоге|не\s+прода[её]м)[^.!?\n]*[.!?]?')
_PARTNER = re.compile(r'(?i)пиар|реклам\w*|сотруднич\w*|партн[её]р\w*|коллаб\w*|взаимн\w*\s+(?:пиар|реклам|продвиж)|бартер')


def _handover(answer):
    evidence = answer.get('evidence') or {}
    reply = str(answer.get('reply') or '')
    routes = set(evidence.get('hard_routes') or [])
    return bool(answer.get('handoff') or evidence.get('urgent') or evidence.get('owner_step_ack')
                or evidence.get('order_action') or evidence.get('paid_notice') or evidence.get('continue_step')
                or routes & {'conflict', 'refund_request', 'wrong_plan', 'reseller', 'paid_without_order',
                             'paid_issue_draft_only', 'claude_consent'}
                or not reply.strip())


_NEEDS = re.compile(r'(?i)что\s+(?:от\s+меня\s+)?(?:нужно|надо|понадобится|потребуется|требуется)|что\s+нужно\s+для|как\s+(?:оформить|подключить|активировать)|'
                    r'можно\s+(?:ли\s+)?(?:на\s+(?:мой|свой)|оформить|купить|подключить)|хочу\s+(?:купить|оформить|подключить)')
GENERIC_AFTER = 'После оплаты подскажу, что понадобится для активации.'


def _after_pay(burst, context):
    """«Что от меня нужно для Claude Max?» → what this service needs after payment (owner's payment guide)."""
    try:
        from .hard_routes import _asked_rows
        from .payment_guide import _after, kind_for, load_guide
    except Exception:
        return ''
    rows = _asked_rows(burst, context)
    services = {str(r.get('service_id') or '') for r in rows}
    if len(services) != 1 or len(rows) > 3:
        return ''
    row = rows[0]
    kind = kind_for(row.get('service_id'), row.get('plan') if len(rows) == 1 else '', (context or {}).get('tariffs'), row.get('service'))
    if kind == 'shop':
        return ''
    text = str(_after(load_guide(), kind, '', None, row.get('service')) or '').strip()
    return ('После оплаты: ' + text[:1].lower() + text[1:]) if text and not text.startswith('После оплаты') else text


def _promo():
    from .owner_scenarios import NAVSEX
    return NAVSEX


def _rules(context=None):
    return (
        # «Что от меня нужно?» about one service: what is needed after payment.
        ('after_pay', lambda b, r: bool(_NEEDS.search(b)) and not re.search(r'(?i)оплатил|оплачен', b),
         lambda r: bool(re.search(r'(?i)после\s+оплаты\s+(?:пришлите|понадоб|нужн|потребу|готовый|бот|придёт|придет)', r)),
         lambda: _after_pay((context or {}).get('burst') or '', context)),
        # VPN: the owner's recommended VPNs whenever a client asks about getting one (not about fixing one).
        ('vpn', lambda b, r: bool(_VPN_ASK.search(b)) and not _VPN_TROUBLE.search(b),
         lambda r: 'DureVpnBot' in r, lambda: _faq('vpn_help', 2)),
        # Claude verification: always which documents work.
        ('claude_documents', lambda b, r: bool(_KYC.search(b)) and not _KYC_BY_US.search(b)
         and bool(re.search(r'(?i)клод|claude|верификац|паспорт', b + ' ' + r)),
         lambda r: bool(re.search(r'(?i)загран', r) and re.search(r'(?i)иностран', r)), lambda: _faq('claude_verification', 1)),
        # Renewal: where and how to buy it.
        # A discount request always gets the owner's promo codes.
        ('discount', lambda b, r: bool(_DISCOUNT.search(b)), lambda r: 'NAVSEX' in r, _promo),
        ('renewal_where', lambda b, r: bool(_RENEW.search(b)) and not _PROBLEM.search(b),
         lambda r: bool(re.search(r'(?i)@neirosetkaa_bot|«каталог»|каталог', r)), lambda: RENEW_LINE),
        # Games, gift cards, console subscriptions: the bot section.
        ('games_section', lambda b, r: bool(_GAMES.search(b)),
         lambda r: bool(re.search(r'(?i)гифт-карты\s+и\s+игры|другой\s+сервис', r)), lambda: _faq('games_consoles', 1),
         _NOT_IN_CATALOG),
        # Partnership: the owner asks for details.
        ('partnership', lambda b, r: bool(_PARTNER.search(b)),
         lambda r: bool(re.search(r'(?i)подробн', r)), lambda: _faq('partnership', 1)),
    )


BOT = '@neirosetkaa_bot'
_IN_BOT = re.compile(r'(?i)\b((?:в|через)\s+(?:нашем\s+|наш\s+)?бот(?:е|а)?)(?:\s+магазина)?\b(?!\s*@)')


def link_the_bot(reply):
    """Owner 21.11.2026: «в боте» always names the bot, so the client can open it and pay at once."""
    text = str(reply or '')
    if not text or BOT in text:
        return text
    return _IN_BOT.sub(lambda m: m.group(1) + ' ' + BOT, text, count=1)


def complete(answer, context):
    """Add the owner's key fact a frequent-topic reply left out; «в боте» always gets the bot's name."""
    if isinstance(answer, dict) and answer.get('reply'):
        linked = link_the_bot(answer['reply'])
        if linked != answer['reply']:
            answer = dict(answer, reply=linked)
    if not isinstance(answer, dict) or _handover(answer):
        return answer
    burst = str((context or {}).get('burst') or '')
    from .hard_routes import latin_only
    if latin_only(burst):
        return answer  # owner 21.11.2026: English stays English — the owner's Russian texts are not appended
    reply = str(answer.get('reply') or '').strip()
    added, names = [], []
    for rule in _rules(context):
        name, about, has, text = rule[:4]
        contradicts = rule[4] if len(rule) > 4 else None
        try:
            if not about(burst, reply) or has(reply + '\n' + '\n'.join(added)):
                continue
            extra = text()
        except Exception:
            continue
        if name == 'after_pay' and extra and GENERIC_AFTER in reply:
            reply = reply.replace(GENERIC_AFTER, extra)  # the specific step replaces «подскажу, что понадобится»
            names.append(name)
            continue
        if extra and extra[:60] not in reply:
            if contradicts is not None:  # the owner's fact wins over the model's contrary sentence
                reply = re.sub(r'\n{3,}', '\n\n', contradicts.sub('', reply)).strip()
            added.append(extra)
            names.append(name)
    if not added:
        return answer
    paragraphs = [p for p in re.split(r'\n\s*\n', reply) if p.strip()]
    question = paragraphs.pop() if paragraphs and paragraphs[-1].rstrip().endswith('?') else ''
    text = '\n\n'.join(paragraphs + added + ([question] if question else [])).strip()
    evidence = dict(answer.get('evidence') or {})
    evidence['completed_with'] = names
    return dict(answer, reply=link_the_bot(text), evidence=evidence)
