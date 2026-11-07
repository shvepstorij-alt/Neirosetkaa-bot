"""Safe, code-owned continuation after rejecting a model candidate.

The rejected candidate is never rendered or used as evidence. Only current
client scope, structured catalogue facts and existing local renderers contribute
to the replacement. A missing fact is a gap, not proof of product absence.
"""
import re

from .privacy import scrub, sensitive


def _safe_label(value):
    value = str(value or '').strip()
    return value if 0 < len(value) <= 100 and scrub(value) == value and not sensitive(value) and not re.search(r'[<>\n]', value) else ''


def _context(facts, question):
    from .catalog_context import service_mentions
    turn = facts.get('conversation_turn', {})
    conversation = facts.get('conversation', {})
    current = str(turn.get('current_text') or conversation.get('latest_message') or question
                  or conversation.get('latest_client_message') or '')
    catalog = list(facts.get('catalog', [])) + list(facts.get('requested_catalog', []))
    # A newly named service overrides old scoped rows and old Apple slots.
    explicit = service_mentions(current, catalog)
    contextual = str(question or turn.get('context_text') or conversation.get('latest_client_message') or current)
    dialog = facts.get('dialog_state') or {}
    # The open topic wins over an isolated fragment that names no other service.
    if dialog.get('bound') and dialog.get('service_label') and not explicit:
        contextual = str(dialog.get('service_label')) + '\n' + contextual
    context_entities = service_mentions(contextual, catalog)
    scope = (contextual if explicit and set(context_entities) <= set(explicit) else current) if explicit else contextual
    if not explicit and _unknown_labels(current, catalog):
        scope = current
    if not service_mentions(scope, catalog) and not _unknown_labels(scope, catalog):
        selected = facts.get('catalog_selection', {})
        if 'appstore' in selected.get('mentioned_known_services', []):
            scope = 'App Store\n' + scope
        elif selected.get('matched_services'):
            scope = ', '.join(_safe_label(row.get('service')) for row in selected['matched_services']) + '\n' + scope
    if not service_mentions(scope, catalog) and turn.get('reference_labels'):
        labels = [_safe_label(label) for label in turn['reference_labels']]
        scope = ', '.join(label for label in labels if label) + '\n' + scope
    return current, scope, catalog


def _requires_owner(current):
    from .order_status import is_status_request
    if sensitive(current) or re.search(r'\[.*(?:данные доступа|токен|сообщение скрыто)', current, re.I):
        return True
    if is_status_request(current):
        return True
    return bool(re.search(
        r'(?:позови|позовите|соедините|подключите)[^.!?\n]{0,40}(?:человек|оператор|александр)|'
        r'(?:хочу|нужен|нужна)[^.!?\n]{0,35}(?:оператор|жив[а-яё]*\s+человек)|'
        r'(?:хочу|можно)[^.!?\n]{0,25}(?:поговорить|общаться)[^.!?\n]{0,25}(?:с\s+человеком|с\s+александром)|'
        r'возврат|верн[а-яё]*\s+деньг|я\s+оплатил|уже\s+оплатил|'
        r'проверь[^.!?\n]{0,35}(?:оплат|заказ)|'
        r'(?<!\w)(?:активируй(?:те)?|оформи(?:те)?|создай(?:те)?)\b', current, re.I))


def _unknown_labels(text, catalog=()):
    from .service_discovery import public_name
    blocked = {'apple', 'id', 'app', 'store', 'usd', 'rub', 'eur', 'try', 'pro', 'plus', 'basic',
               'max', 'premium', 'api', 'free', 'iphone', 'android', 'ios', 'email', 'password',
               'google','chrome','safari','firefox','edge','json','auth','session','organization','url',
               'browser', 'windows', 'macos', 'linux', 'ipad', 'web', 'desktop', 'mobile',
               'pdf', 'docx', 'xlsx', 'csv', 'svg', 'png', 'jpg', 'jpeg', 'webp', 'gif', 'mp4',
               'mp3', 'html', 'xml', 'sso', 'oauth', 'saml', 'sdk', 'mcp', 'rag', 'ocr', 'rest',
               'graphql', 'sonnet', 'opus', 'haiku', 'turbo', 'mini', 'nano', 'flash', 'vision'}
    from .catalog_context import _words
    blocked.update(word for row in catalog if isinstance(row, dict)
                   for key in ('service', 'service_key', 'plan') for word in _words(row.get(key, '')))
    return list(dict.fromkeys(label for label in re.findall(r'(?<![\w/@.])[A-Za-z][A-Za-z-]{1,40}(?![\w/@.])', text)
                             if label.casefold() not in blocked and public_name(label.casefold())
                             and _safe_label(label)))[:4]


def _labels(names, mentions, text, catalog):
    """Keep every scoped service, deduplicating its display name and aliases."""
    from .catalog_context import service_mentions
    labels = []
    seen_ids = set()
    seen_names = set()
    for label in [*names, *mentions.values(), *_unknown_labels(text, catalog)]:
        label = _safe_label(label)
        if not label:
            continue
        identities = set(service_mentions(label, catalog)) or {'unverified:' + label.casefold()}
        if identities & seen_ids or label.casefold() in seen_names:
            continue
        labels.append(label)
        seen_ids.update(identities)
        seen_names.add(label.casefold())
    return labels


def _finish(body, reason, *, status='clarification', diagnostic=''):
    if not body or len(body) > 1800:
        return None
    return {'reply': body, 'summary': 'Автономное продолжение по подтверждённым данным; отклонённый кандидат не использован.',
            'handoff': False, 'source_ids': [], 'research': {'topic': 'none', 'products': []},
            'evidence': {'status': status, 'sources': [],
                         'rejected_candidate_review': {'supported': False, 'reason': scrub(str(reason))[:1000]},
                         'diagnostic': 'Этап: автономное продолжение. ' + diagnostic}}


def _apple(facts, text, reason):
    from .apple_matching import amounts, region_codes, exact_quote, alternatives, quote_diagnostic
    from .catalog_context import apple_selection
    normalized = re.sub(r'(?<=\d)\s*р\.?\b', ' RUB', text, flags=re.I)
    normalized = re.sub(r'(?<=\d)\s*руб\.?\b', ' RUB', normalized, flags=re.I)
    current_codes = region_codes(normalized)
    original = facts.get('appstore_selection', {})
    codes = sorted(current_codes or original.get('requested_codes', []))
    apple = facts.get('appstore', {})
    selection = apple_selection(apple, normalized) if current_codes else original
    local = dict(facts, appstore_selection=selection)
    wanted = amounts(normalized)
    code_text = ', '.join(codes)
    values = ', '.join(f'{value:g} {unit}' for value, unit in sorted(wanted))
    prefix = ('Apple ID, регион ' + code_text if codes else 'Apple ID')
    if values:
        prefix += ': сумма ' + values
    elif re.search(r'\d', normalized):
        # Echo only a narrow numeric amount phrase, never arbitrary client text.
        bare = re.search(r'(?<!\w)\d+(?:[.,]\d+)?(?:\s*(?:тысяч[аиу]?|тыс\.?)(?!\w))?', normalized, re.I)
        if bare:
            prefix += ': сумма ' + bare[0]
    if not codes:
        prompt = 'Какой регион у вашего Apple ID?' if wanted else 'Подскажите регион Apple ID и валюту указанной суммы.' if re.search(r'\d', normalized) else 'Какой регион Apple ID и на какую сумму хотите пополнить баланс?'
        return _finish(prefix + '. ' + prompt, reason, diagnostic='Регион не подтверждён; сумму клиента не проверяли как цену магазина.')
    if not wanted:
        prompt = ('Это сумма пополнения в валюте аккаунта или бюджет покупки кода? Уточните валюту.'
                  if re.search(r'\d', normalized) else 'На какую сумму хотите пополнить баланс?')
        return _finish(prefix + '. ' + prompt, reason, diagnostic='Регион=' + code_text + '; сумма/валюта номинала не определена.')
    if re.search(r'\bбюджет', normalized, re.I) and not re.search(r'номинал|пополн[а-яё]*[^.!?\n]{0,25}\bна\s+\d', normalized, re.I):
        return _finish('Apple ID, регион ' + code_text + ': бюджет покупки кода ' + values
                       + '. На какую сумму хотите пополнить баланс в валюте Apple ID?', reason,
                       diagnostic='Сумма обозначена как бюджет покупки; она не сопоставлялась с номиналом и продажной ценой.')
    if len(codes) != 1 or len(wanted) != 1:
        return _finish(prefix + '. Уточните соответствие суммы и валюты каждому региону.', reason,
                       diagnostic='Регионов=' + str(len(codes)) + '; денежных сумм=' + str(len(wanted)) + '.')
    quote = exact_quote(local, normalized)
    if quote and quote.get('price') is not None:
        body = f"App Store, регион {quote['region']}: номинал {quote['amount']:g} {quote['currency']} стоит {quote['price']} ₽."
        return _finish(body, reason, status='catalog_direct', diagnostic='Точная региональная цена подтверждена. ' + quote_diagnostic(quote))
    body = prefix + '. Точную стоимость этого номинала сейчас получить не удалось; отсутствие товара не установлено.'
    other = alternatives(local, quote) if quote else []
    if other:
        body += '\n\nПодтверждённые варианты этого региона:\n' + '\n'.join(
            f"{row['amount']:g} {row['currency']}: {row['price']} ₽" for row in other)
        body += '\nПодойдёт другой номинал или нужен именно указанный?'
    else:
        body += ' Можно продолжить по карточке нужного номинала: пришлите её скриншот без личных и платёжных данных.'
    return _finish(body, reason, status='catalog_direct' if other else 'clarification',
                   diagnostic='Источник=' + str(apple.get('status', 'not_connected')) + '; регион=' + code_text
                   + '; регионов получено=' + str(len(apple.get('regions', []))) + '. '
                   + (quote_diagnostic(quote) if quote else 'Точное сопоставление суммы не получено.'))


def reply(facts, reason, question=''):
    """Return a deterministic safe continuation, or None for actual manual work."""
    if not isinstance(facts, dict):
        return None
    current, scope, catalog = _context(facts, question)
    owner_reason = re.search(
        r'конфликт\s+(?:подтвержд[её]нных\s+)?(?:правил|политик|способов\s+оформления)|'
        r'manual_secret_intake|secret\s+intake|human\s+request|'
        r'клиент\s+(?:явно\s+)?просит\s+(?:человека|оператора)|'
        r'получены\s+(?:значения\s+)?данные\s+доступа', str(reason), re.I)
    if not scope.strip() or _requires_owner(current) or owner_reason:
        return None
    from .catalog_context import annotate, service_mentions
    scoped = annotate(dict(facts, catalog=catalog), scope)
    mentions = service_mentions(scope, catalog)
    apple = 'appstore' in mentions
    from .request_goals import goals
    intent = goals(current, facts) | goals(scope)
    public = bool(intent & {'features', 'recommendation'})
    if apple and len(mentions) == 1:
        if public and not intent & {'price', 'purchase', 'activation'}:
            return _finish('Запрошенные возможности и ограничения iCloud / App Store сейчас не подтверждены источниками. '
                           'Пришлите ссылку на официальное описание нужной функции или скриншот нужного пункта без личных данных.',
                           reason, diagnostic='Вопрос о публичных свойствах; уточнение суммы пополнения не подставлялось вместо ответа.')
        from .apple_matching import region_codes
        if not region_codes(scope) and facts.get('appstore_selection', {}).get('requested_codes'):
            scoped['appstore_selection'] = facts['appstore_selection']
        result = _apple(scoped, scope, reason)
        if result and public:
            result['reply'] += '\n\nЗапрошенные возможности и ограничения пока не подтверждены источниками. Пришлите ссылку на официальное описание нужной функции.'
        return result if result and len(result['reply']) <= 1800 else None
    from .turn_scope import scope as narrow_scope
    scoped, _ = narrow_scope(scoped, scope, current)
    from .catalog_context import _families, _words
    rows = [row for row in scoped.get('requested_catalog', [])
            if 'appstore' not in _families(_words(str(row.get('service', '')) + ' ' + str(row.get('service_key', ''))))]
    scoped = dict(scoped, requested_catalog=rows)
    names = list(dict.fromkeys(_safe_label(row.get('service')) for row in rows))
    names = [name for name in names if name]
    labels = _labels(names, mentions, scope, catalog)
    if not labels:
        dialog = facts.get('dialog_state') or {}
        if dialog.get('bound') and dialog.get('service_label'):
            labels = [_safe_label(dialog.get('service_label'))]
            labels = [label for label in labels if label]
        if not labels:
            return _finish('Какой сервис вас интересует?', reason, diagnostic='Идентичность сервиса не определена; несвязанный ассортимент не использован.')
    prices = {}
    methods = {}
    for row in rows:
        key = (_safe_label(row.get('service')), _safe_label(row.get('plan')))
        value = row.get('catalog_price_rub')
        if not all(key):
            continue
        if type(value) is int and value > 0:
            prices.setdefault(key, set()).add(value)
        method = row.get('fulfillment')
        if method:
            methods.setdefault(key, set()).add(method)
    if any(len(values) != 1 for values in prices.values()):
        return None  # Conflicting source prices remain a real manual conflict.
    if any(len(values) > 1 for values in methods.values()):
        return None  # The same exact offer has conflicting fulfillment evidence.
    parts = []
    budget = re.search(r'\bбюджет[а-яё]*\s*(?:до|около|на|:|-|—)?\s*'
                       r'(\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?)\s*'
                       r'(руб(?:лей|ля|ль)?|₽|RUB|USD|EUR|доллар(?:ов|а)?|евро)(?!\w)', current, re.I)
    if budget:
        parts.append('Ваш бюджет: ' + budget[1] + ' ' + budget[2] + '.')
    if prices:
        if len(prices) > 8:
            return _finish('Для ' + ', '.join(labels) + ' найдено несколько тарифов. Какой тариф вас интересует?', reason,
                           diagnostic='Подтверждённых тарифов=' + str(len(prices)) + '; запрошено сужение выбора.')
        parts.append('Цены магазина:\n' + '\n'.join(f'{name} {plan}: {next(iter(values))} ₽'
                                                   for (name, plan), values in prices.items()))
    workflow = None
    if intent & {'purchase', 'activation'} and rows:
        from .fulfillment_reply import workflow_reply
        workflow = workflow_reply(scoped, scope, current=current)
        if workflow and workflow.get('handoff'):
            detail = str(workflow.get('evidence', {}).get('diagnostic', ''))
            if re.search(r'конфликт|несколько веток|разные способы', detail, re.I):
                return None
        if workflow and not workflow.get('handoff'):
            parts.append(workflow['reply'])
    if public:
        parts.append('Для ' + ', '.join(labels) + ' запрошенные возможности и ограничения сейчас не подтверждены источниками. '
                     'Можно продолжить по официальному описанию нужного тарифа: пришлите ссылку на него '
                     'или скриншот нужного пункта без личных данных.')
    elif not workflow or workflow.get('handoff'):
        if not prices:
            parts.append('Точную цену и условия ' + ', '.join(labels) + ' сейчас получить не удалось. '
                         'Доступность предложения ещё не подтверждена.')
        if not rows and not mentions:
            parts.append('Пришлите ссылку на официальный сайт ' + ', '.join(labels) + '.'
                         if not facts.get('public_service_sites') else
                         'Пришлите скриншот нужного тарифа с официального сайта без личных и платёжных данных.')
        elif len({row.get('plan') for row in rows if row.get('plan')}) > 1:
            parts.append('Какой из этих тарифов вас интересует?')
        else:
            if not prices:
                from .bot_guide import navigation_facts
                nav = navigation_facts(facts)
                if nav.get('username'):
                    parts.append(f"Проверить карточку в магазине можно в @{nav['username']}: /start → «{nav['shop_menu']}». "
                                 'Если нужная карточка отображается, откройте её.')
            parts.append('Можно продолжить по карточке нужного тарифа: пришлите её скриншот без личных и платёжных данных.')
    body = '\n\n'.join(parts)
    # Re-seal a composed workflow rather than copying a proof for another text.
    if workflow and not workflow.get('handoff'):
        from .auto_policy import consultation_evidence
        result = _finish(body, reason, status='catalog_direct', diagnostic='Подтверждённые цены и локальная инструкция; непринятый текст исключён.')
        if result:
            meta = result['evidence']
            result['evidence'] = consultation_evidence(body, 'workflow', **{k: v for k, v in meta.items() if k not in ('status', 'sources')})
        return result
    return _finish(body, reason, status='catalog_direct' if prices else 'clarification',
                   diagnostic='Сервис=' + ', '.join(labels) + '; подтверждённых цен=' + str(len(prices))
                   + '; публичные свойства не утверждались; несвязанные строки исключены.')
