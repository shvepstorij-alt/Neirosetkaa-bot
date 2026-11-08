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
    'conflict: none — спокойный тон, annoyed — раздражён без угрозы, '
    'high — злится, угрожает возвратом, чарджбеком или жалобой, либо тот же вопрос уже повторяется второй раз и дольше без решения. '
    'conflict_reason — короткая причина для владельца, пусто если conflict не high. '
    'Сообщения history с authority=owner — слова Александра. Они главнее черновика: '
    'не противоречь им, не повторяй их дословно и не спрашивай заново то, что он уже спросил. '
    'Продолжай с его последней реплики. owner_alignment: follows, contradicts, repeats или reasks. '
    'Если таких сообщений нет, owner_alignment=follows. '
    'faq — ответы владельца. Если known=true, ответ клиенту должен содержать answer дословно и ничего сверх него. '
    'Если клиент просит срок гарантии или другие условия, которых нет в answer, beyond_owner_fact=true. '
    'Если у темы есть collect, это сбор сведений, а не текст обещания. '
    'Для возврата заполни refund_order, refund_service и refund_reason только тем, что уже есть в последних сообщениях, иначе пустая строка. '
    'Спроси только недостающее. Возврат не обещай. '
    'owner_topic — id такой темы, либо none. '
    'documented_facts — уже записанные правила. Их можно сказать. Не добавляй к ним новых сроков и гарантий. '
    'Не утверждай, что оплата прошла, подписка активирована или возврат сделан.'
)

DOCUMENTED_FACTS = (
    'Обычная подписка ChatGPT: токен сессии вставляется в окно активации в боте, не в эту переписку.',
    'Россия не входит в официальный перечень доступа Claude. VPN и одна и та же локация — совет владельца. '
    'Это не делает страну официально поддерживаемой.',
    'Гарантия есть на все сервисы, кроме Claude. Других условий гарантии владелец не записал.',
    'Claude может заблокировать аккаунт, гарантии на Claude нет. На остальные сервисы блокировка входит в гарантию. Срок замены не записан.',
    'Подписка оформляется в течение 30–60 минут.',
    'Возврат не обещать. Собрать номер заказа, сервис и причину. Когда все три есть, отвечает Александр.',
)
_RUBLE = re.compile(r'(\d{2,7})\s*(?:₽|руб(?:лей|ля|ль)?)', re.I)
# "не обещаю" refuses a promise. A bare "обещаю" still blocks the draft.
_PROMISE = re.compile(r'(?i)(?<!не )обещаю|(?<!не )гарантирую|точно\s+активир|возврат\s+одобрен|уже\s+оформил')
_INTERNAL = re.compile(
    r'(?i)уже\s+известно|недостаточно\s+без|результат\s+операции\s+пока\s+не\s+подтвержд|'
    r'часть\s+условий\s+пока\s+не\s+подтверждена|этап\s*:')


def burst_and_window(db, chat, limit=10):
    rows = db.execute(
        '''SELECT mid, role, text FROM messages
           WHERE chat=? AND role IN ('client','assistant','owner')
           ORDER BY mid DESC LIMIT ?''', (chat, limit)).fetchall()
    window = [{'role': row['role'],
               'authority': 'owner' if row['role'] == 'owner' else 'untrusted',
               'text': row['text'] or ''} for row in reversed(rows)]
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
    from .faq import for_context as faq_context
    try:
        glossary = for_context(facts)
    except Exception:
        glossary = {'services': [], 'phrases': [], 'tariff_words': [], 'load_error': True}
    try:
        faq = faq_context()
    except Exception:
        faq = []
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
            'owner_statements': [item['text'] for item in window if item.get('authority') == 'owner' and item.get('text')],
            'faq': faq,
            'documented_facts': list(DOCUMENTED_FACTS),
            'hints': hint_services(burst, catalog)}


def _allowed_prices(tariffs, nominals, service_id, tariff_id):
    """Every price in the facts given to the model. A number outside this set is invented."""
    prices = {row['price'] for row in tariffs if type(row.get('price')) is int}
    prices |= {row['price'] for row in nominals if type(row.get('price')) is int}
    return prices


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
            'conflict': _conflict(understanding),
            'conflict_reason': str((understanding or {}).get('conflict_reason') or '').strip()[:300],
            'owner_alignment': _alignment(understanding, context),
            'owner_topic': _owner_topic(understanding, context),
            'beyond_owner_fact': bool((understanding or {}).get('beyond_owner_fact')),
            'refund_collected': _refund_collected(understanding),
            'allowed_prices': _allowed_prices(tariffs, context.get('nominals') or [], service_id, tariff_id)}


def _refund_collected(understanding):
    raw = (understanding or {}).get('refund_collected')
    if not isinstance(raw, dict):
        raw = {'order_number': (understanding or {}).get('refund_order'),
               'service': (understanding or {}).get('refund_service'),
               'reason': (understanding or {}).get('refund_reason')}
    cleaned = {}
    for key in ('order_number', 'service', 'reason'):
        value = ' '.join(str((raw or {}).get(key) or '').split())
        if value and len(value) <= 200:
            cleaned[key] = value
    return cleaned


def _owner_topic(understanding, context):
    value = str((understanding or {}).get('owner_topic') or 'none').strip()
    if value == 'none':
        return 'none'
    known = {row.get('id') for row in (context or {}).get('faq') or [] if isinstance(row, dict)}
    if not known:
        from .faq import load_faq
        try:
            known = {row['id'] for row in load_faq()['topics']}
        except Exception:
            known = set()
    return value if value in known else 'none'


def _conflict(understanding):
    value = str((understanding or {}).get('conflict') or 'none')
    return value if value in ('none', 'annoyed', 'high') else 'none'


def _alignment(understanding, context):
    owners = [item for item in (context or {}).get('history') or [] if item.get('authority') == 'owner' and item.get('text')]
    if not owners:
        return 'follows'
    value = str((understanding or {}).get('owner_alignment') or 'follows')
    return value if value in ('follows', 'contradicts', 'repeats', 'reasks') else 'follows'


def _topic_known(topic, context):
    from .faq import usable_answer
    return bool(usable_answer(topic, (context or {}).get('faq')))


def _flat(text):
    return ' '.join(str(text or '').casefold().split())


def _repeats_assistant(reply, context):
    current = _flat(reply)
    if len(current) < 12:
        return False
    for item in (context or {}).get('history') or []:
        if item.get('role') == 'assistant' and _flat(item.get('text')) == current:
            return True
    return False


def states_price(reply):
    """The draft names a ruble amount. This does not look at the client's words."""
    return bool(_RUBLE.search(str(reply or '')))


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
                         'vpn_advice': validated['vpn_advice'],
                         'conflict': validated['conflict'],
                         'owner_alignment': validated['owner_alignment'],
                         'owner_topic': validated['owner_topic']},
                         'diagnostic': verdict['reason'],
                         'handoff_reason': '' if verdict['send'] else verdict['reason']}}


def enforce_turn_policy(answer, understanding, context, freshness=None):
    """Hold a draft the model marked as conflict, a clash with the owner, or a stale price."""
    if not isinstance(answer, dict):
        return answer
    validated = validate(understanding or {}, context or {})
    holds = []
    if validated['conflict'] == 'high':
        holds.append('Модель отметила высокий конфликт. Клиенту оставлена фраза ожидания.')
    if validated['owner_alignment'] != 'follows':
        holds.append('Черновик расходится с репликой владельца в этом чате.')
    if (freshness or {}).get('status') == 'refresh_failed' and states_price(answer.get('reply')):
        holds.append('Каталог старше порога, повторное чтение не удалось. Цену клиенту не отправляю.')
    topic = validated['owner_topic']
    faq_rows = (context or {}).get('faq') or []
    from .faq import collect_fields, usable_answer
    answer_text = usable_answer(topic, faq_rows) if topic != 'none' else ''
    collecting = collect_fields(topic, faq_rows) if topic != 'none' else []
    refund_ready = False
    beyond = bool(validated.get('beyond_owner_fact'))
    if topic != 'none' and collecting:
        needed = [key for key in ('order_number', 'service', 'reason') if key in collecting]
        got = validated.get('refund_collected') or {}
        missing = [key for key in needed if not got.get(key)]
        reply = str(answer.get('reply') or '')
        if not missing:
            refund_ready = True
            holds.append('Данные для возврата собраны. Клиенту оставлена фраза ожидания.')
        elif asserts_outcome(reply) or _PROMISE.search(reply) or not re.search(r'(?i)пришлите|напишите|укажите|\?', reply):
            holds.append('По возврату нельзя обещать результат, пока собраны не все сведения.')
    elif topic != 'none' and not answer_text:
        holds.append('Для этой темы нет записанного ответа владельца. Клиенту оставлена фраза ожидания.')
    elif answer_text:
        reply = str(answer.get('reply') or '')
        if beyond or _flat(answer_text) not in _flat(reply):
            holds.append('Ответ выходит за записанный факт владельца.')
    if _repeats_assistant(answer.get('reply'), context):
        holds.append('Черновик повторяет уже сказанное в этом чате.')
    evidence = dict(answer.get('evidence') or {})
    turn = dict(evidence.get('turn_understanding') or {})
    turn.update(conflict=validated['conflict'], owner_alignment=validated['owner_alignment'],
                owner_topic=validated['owner_topic'])
    evidence['turn_understanding'] = turn
    evidence['catalog_freshness'] = dict(freshness or {})
    if holds:
        evidence.update(status='owner_policy', conflict_hold=validated['conflict'] == 'high',
                        owner_alignment_hold=validated['owner_alignment'] != 'follows',
                        stale_price_hold=(freshness or {}).get('status') == 'refresh_failed' and states_price(answer.get('reply')),
                        owner_fact_hold=bool(topic != 'none' and not collecting and (
                            (not answer_text) or beyond or (answer_text and _flat(answer_text) not in _flat(answer.get('reply'))))),
                        refund_hold=refund_ready,
                        refund_collected=validated.get('refund_collected') or {},
                        repeat_hold=_repeats_assistant(answer.get('reply'), context),
                        diagnostic=' '.join(holds), handoff_reason=' '.join(holds))
        answer = dict(answer, handoff=True, evidence=evidence)
        return answer
    answer = dict(answer, evidence=evidence)
    return answer
