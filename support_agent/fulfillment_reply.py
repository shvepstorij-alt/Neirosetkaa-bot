"""Customer workflow explanations from verified shop fields, without model guesses."""
import re


def _code_help(name):
    """Steps verified in our three activation mini-apps, not inferred from a plan.

    Only called for the confirmed conditional code route. Never collect session
    data in this chat or imply that payment has already issued a usable code.
    """
    from .catalog_context import _families,_words
    families=_families(_words(name))
    if 'chatgpt' in families:
        return ('Если в вашем оплаченном заказе доступна кнопка активации, откройте её. '
                'Войдите в нужный аккаунт на chatgpt.com. В том же браузере откройте '
                'https://chatgpt.com/api/auth/session и скопируйте весь текст JSON. '
                'Вставьте его только в поле мини-приложения активации и нажмите активацию. '
                'Пароль и данные сессии сюда в переписку присылать не нужно.')
    if 'claude' in families:
        return ('Если в вашем оплаченном заказе доступна кнопка «Активировать Claude», откройте её. '
                'Войдите в нужный аккаунт на claude.ai, откройте https://claude.ai/settings/account '
                'и скопируйте Organization ID. Вставьте его в поле мини-приложения активации '
                'и нажмите активацию. Пароль от аккаунта для этой кодовой процедуры не нужен.')
    if 'perplexity' in families:
        return ('Если в вашем оплаченном заказе доступна кнопка «Активировать Perplexity», откройте её. '
                'Войдите в нужный аккаунт Perplexity и откройте в том же браузере '
                'https://www.perplexity.ai/api/auth/session. Скопируйте значение поля id '
                'и вставьте в поле мини-приложения активации, затем нажмите активацию. '
                'Пароль и данные сессии сюда в переписку присылать не нужно.')
    return ''


def _selected_plan_rows(items,latest):
    """Prefer explicitly named complete plan labels, not unrelated variants."""
    from .catalog_context import _words
    words=_words(latest)
    hits=[]
    for index,item in enumerate(items):
        plan=_words(item.get('plan',''))
        if not plan:continue
        for start in range(len(words)-len(plan)+1):
            if words[start:start+len(plan)]==plan:hits.append((start,start+len(plan),index))
    # Pro inside the same occurrence of "Pro на год" is not a separate request.
    selected={index for start,end,index in hits if not any(
        other_start<=start and end<=other_end and other_end-other_start>end-start
        for other_start,other_end,_ in hits)}
    return [item for index,item in enumerate(items) if index in selected] if selected else items


def _service_plan_scope(name,groups,latest):
    """Bind an explicitly named plan to its own service, not another brand.

    A bare shared plan label in a multi-service question remains unresolved.
    Only client text is inspected; no workflow or plan identity is invented.
    """
    from .catalog_context import ALIASES,_words,_families
    anchors=[]
    for service,items in groups.items():
        names={_words(service)}
        families=set()
        for item in items:
            for field in ('service','service_key'):
                words=_words(item.get(field,''))
                if words:names.add(words)
                families.update(_families(words))
        for family in families:
            names.update(_words(alias) for alias in ALIASES[family])
        for words in names:
            if not words:continue
            pattern=r'(?<!\w)'+r'[\W_]+'.join(re.escape(word) for word in words)+r'(?!\w)'
            for match in re.finditer(pattern,latest,re.I):
                anchors.append((match.start(),match.end(),service))
    # Keep the full name over shorter aliases at the same occurrence.
    anchors=[anchor for anchor in anchors if not any(
        other[2]==anchor[2] and other[0]<=anchor[0] and anchor[1]<=other[1]
        and other[1]-other[0]>anchor[1]-anchor[0] for other in anchors)]
    anchors=sorted(set(anchors))
    def preceding_plan(start,service):
        matches=[]
        for item in groups[service]:
            words=_words(item.get('plan',''))
            if not words:continue
            pattern=(r'(?<!\w)'+r'[\W_]+'.join(re.escape(word) for word in words)
                     +r'\s+(?:для|у|на)\s*$')
            found=re.search(pattern,latest[:start],re.I)
            if found and not any(a>=found.start() for a,_,_ in anchors if a<start):
                matches.append(found)
        return min(matches,key=lambda match:match.start()) if matches else None
    scopes=[]
    for start,end,service in anchors:
        if service!=name:continue
        if any(other!=service and a<end and start<b for a,b,other in anchors):
            continue  # An alias matches multiple products: do not guess.
        following=[anchor for anchor in anchors if anchor[0]>=end]
        stop=following[0][0] if following else len(latest)
        if following:
            next_start,_,next_service=following[0]
            next_prefix=preceding_plan(next_start,next_service)
            if next_prefix and next_prefix.start()>=end:stop=next_prefix.start()
        scoped=latest[start:stop]
        # A later stand-alone "хочу Pro" after a comma/new line can refer to
        # the whole service list. Do not attach it to the last brand by order.
        scoped=re.split(r'[,;\n]|[.!?]\s+',scoped,maxsplit=1)[0]
        # Also support "Pro для Claude" / "Plus у ChatGPT", without taking
        # an earlier service's plan from "ChatGPT Plus, Claude Pro".
        prefix=preceding_plan(start,service)
        if prefix:scoped=prefix[0]+' '+scoped
        scopes.append(scoped)
    return '\n'.join(scopes)


def workflow_reply(facts,latest):
    from .selection_help import is_selection_request
    if is_selection_request(latest):return None
    lines=[line.strip() for line in latest.splitlines() if line.strip()]
    questions=[line for line in lines if re.search(
        r'оформ|кроме оплаты|(?:что|какие).*(?:нужн|потреб|треб|данн|присл)|'
        r'как.*(?:получить|скопировать|найти).*ссылк|как.*(?:пополн|активир)|парол.*(?:нуж|треб)|'
        r'если.*вход.*(?:google|гугл)',line,re.I)]
    if not questions:return None
    if re.search(r'функци|лимит|входит|отлич|возврат|ошиб|не работает|закуп|поставщик|себестоим|18\s*\+|порно|эротическ',latest,re.I):return None
    ambiguities=facts.get('catalog_selection',{}).get('variant_ambiguities',[])
    if ambiguities:
        labels=list(dict.fromkeys(name for group in ambiguities for name in group['services']))
        return {'reply':'Что именно нужно оформить: '+' или '.join(labels)+'?',
                'summary':'У бренда найдены разные типы товаров; клиенту задан один вопрос для выбора конкретного товара.',
                'handoff':False,'source_ids':[],'evidence':{'status':'catalog_direct','sources':[],
                    'diagnostic':'Этап: выбор товара. Неоднозначны отдельные продукты одного бренда: '+', '.join(labels)}}
    from .catalog_context import _words,_families
    from .request_context import _service_names
    known_words={word for name in _service_names(facts.get('catalog',[])) for word in name}
    ordinary=set('что нужно потребуется данные почта пароль оплата оплаты оформить оформление оформления выбрать тариф тарифы регион сумма пополнение баланс после для и или как куда спасибо пожалуйста сервис сервисы apple id store app почту аккаунт аккаунта'.split())
    ordinary.update(('google','email','e','mail','password','если','вход'))
    ordinary.update(('почте','почтой','почты','паролю','паролем','пароля','ссылке','ссылкой','данным','данными'))
    if 'appstore' in facts.get('catalog_selection',{}).get('mentioned_known_services',[]):
        from .apple_matching import ALIASES as region_aliases
        ordinary.update(('usd','eur','try','rub','gbp','cad','aud','inr','jpy','kzt','sar','brl','pln'))
        for code in facts.get('appstore_selection',{}).get('requested_codes',[]):
            ordinary.update(_words(code))
            for alias in region_aliases.get(code,()):ordinary.update(_words(alias))
    for row in facts.get('requested_catalog',[]):
        known_words.update(_words(row.get('service','')));known_words.update(_words(row.get('plan','')))
    selected_families=set()
    for row in facts.get('requested_catalog',[]):
        selected_families.update(_families(_words(row.get('service',''))))
        selected_families.update(_families(_words(row.get('service_key',''))))
    # A known alias missing from the selected catalogue is not an instruction
    # to silently explain only the other products in the same question.
    if _families(_words(latest))-selected_families-{'appstore'}:return None
    for line in lines:
        if line.startswith(('Сервисы текущего обращения:','Сервисы:','Регион Apple ID:')):continue
        for tail in re.findall(r'(?:\bи\b|\bили\b|[,;+])\s*([A-Za-zА-Яа-яЁё][\w-]*)',line,re.I):
            if tail.casefold() not in known_words|ordinary:return None
        # Latin product names can also precede a comma/conjunction. Unknown
        # names must reach consultation/discovery instead of being omitted.
        for token in re.findall(r'(?<!\w)[A-Za-z][A-Za-z0-9_-]*(?!\w)',line):
            if set(_words(token))-(known_words|ordinary):return None
    from .workflow_policy import policies_for,route_evidence,instruction_for,policy_scope_issue,route_choice
    rows=facts.get('requested_catalog',[])
    apple_requested='appstore' in facts.get('catalog_selection',{}).get('mentioned_known_services',[])
    if not rows and not apple_requested:return None
    groups={}
    for row in rows:
        if 'appstore' not in (_families(_words(row.get('service','')))|_families(_words(row.get('service_key','')))):
            groups.setdefault(row.get('service',''),[]).append(row)
    if not all(groups):return None
    parts=[];diagnostics=[];handoff=False;shared_links=[]
    from .bot_guide import purchase_navigation
    navigation=purchase_navigation(facts,apple=apple_requested and not groups)
    # Append navigation only if the whole answer has no unresolved procedure.
    # A valid catalogue button is not a reason to solicit payment first.
    for name,items in groups.items():
        # Shared names such as Pro in a multi-brand question cannot be bound
        # to a brand by a token match alone. Keep those variants unresolved.
        if len(groups)==1:items=_selected_plan_rows(items,latest)
        else:
            scope=_service_plan_scope(name,groups,latest)
            if scope:items=_selected_plan_rows(items,scope)
        if re.search(r'тариф|цен|стоим',latest,re.I):
            prices=[]
            for item in items:
                price=item.get('catalog_price_rub')
                if type(price) is int and price>0:prices.append(f"{item.get('plan','')}: {price} ₽")
            if prices:parts.append(f'Для {name} есть такие тарифы:\n'+'\n'.join(dict.fromkeys(prices)))
        owner_policies=policies_for({'requested_catalog':items})
        methods={item.get('fulfillment','not_verified') for item in items}
        if owner_policies:
            state,projected=route_evidence(owner_policies[0]['service'],items)
            policy=owner_policies[0]
            choice=route_choice(latest if len(groups)==1 else _service_plan_scope(name,groups,latest)) if policy.get('field_alternatives') else None
            if choice and projected & set(policy.get('method_options',[])) and choice not in projected:
                state='conflict'
                diagnostics.append(name+': выбранный клиентом способ='+choice+'; текущий способ товара='+','.join(sorted(projected))+'. Владелец подтвердил оба способа, но настройка выбранного товара не изменена автоматически.')
            if policy['required_fields']==['checkout_url'] and policy.get('workflow_steps_source')=='owner_confirmed':
                shared_links.append((name,policy,state))
            else:
                parts.append(instruction_for(policy,name,state,google=bool(re.search(r'google|гугл',latest,re.I)),choice=choice))
            if policy.get('field_alternatives') and not choice:
                diagnostics.append(name+': доступны подтверждённые альтернативы; клиент ещё не выбрал способ. Оба набора данных не требуются; до выбора не направлять к оплате.')
            if state=='conflict':
                diagnostics.append(name+': конфликт способов оформления. Правило владельца='+policy['expected_fulfillment']+'; полученные ветки='+','.join(sorted(projected))+'. Настройка магазина не изменена автоматически.')
                scope_issue=policy_scope_issue(policy,items)
                if scope_issue:diagnostics.append(name+': '+scope_issue)
            elif state=='unavailable':
                diagnostics.append(name+': необходимые данные подтверждены владельцем; текущая ветка оформления не получена. Живой экспорт='+str(facts.get('live_shop_status','not_connected'))+'; полученные ветки='+','.join(sorted(projected))+'. Конфликт не установлен.')
            handoff=True
            continue
        if methods=={'account_details'}:
            fields={tuple(item.get('required_fields',[])) for item in items if item.get('required_fields_status')=='available'}
            complete=all(item.get('required_fields_status')=='available' for item in items)
            if complete and fields=={('account_email','account_password')}:
                parts.append(name+': после выбора тарифа и оплаты пришлите сюда, в эту переписку, почту и пароль от аккаунта.')
                handoff=True
            else:
                parts.append(name+': для оформления потребуются данные аккаунта. Точный список данных нужно проверить перед оформлением.')
                diagnostics.append(name+': ветка account_details подтверждена; точный список полей не подтверждён для всех выбранных тарифов.')
                handoff=True
        elif methods=={'checkout_link'}:
            expected=['open_service','login_own_account','open_plans','select_plan','open_checkout','copy_checkout_url','send_checkout_url']
            verified=all(item.get('workflow_steps_status')=='available' and item.get('workflow_steps')==expected for item in items)
            if verified:
                parts.append(name+': для оформления понадобится ссылка на оплату выбранного тарифа. Получить её можно так:\n'
                             '1. Откройте сайт сервиса и войдите в свой аккаунт.\n'
                             '2. Откройте тарифы или подписку и выберите нужный план.\n'
                             '3. Перейдите на страницу оплаты, но самостоятельно не оплачивайте.\n'
                             '4. Скопируйте ссылку этой страницы и пришлите сюда, в эту переписку.\n'
                             'Если на каком-то шаге не получается, пришлите скриншот без платёжных данных, подскажу дальше)')
            else:
                parts.append(name+': оформление по платёжной ссылке сервиса. Для подробных шагов нужно проверить действующую инструкцию этого товара.')
                diagnostics.append(name+': ветка checkout_link подтверждена; пошаговая инструкция не подтверждена для всех выбранных тарифов.')
            handoff=True
        elif methods=={'manual'}:
            parts.append(name+': оформление вручную. Перед оплатой нужно проверить необходимые данные для выбранного тарифа.')
            diagnostics.append(name+': ручная ветка подтверждена; необходимые поля в данных магазина не указаны.')
            handoff=True
        elif methods=={'code_activation_conditional'}:
            parts.append(name+': предусмотрено оформление по коду активации. Выдачу кода нужно проверить по заказу.')
            instructions=_code_help(name)
            if instructions:parts.append(instructions)
            diagnostics.append(name+': кодовая ветка подтверждена; наличие и выдача кода этим источником не подтверждаются.')
            handoff=True
        else:
            if not (methods & {'manual','checkout_link','account_details','code_activation_conditional'}):
                parts.append(name+': действующую инструкцию оформления сейчас подтвердить не получилось. Проверю необходимые шаги и данные до оплаты.')
                diagnostics.append(name+': инструкция оформления не получена; живой экспорт='+str(facts.get('live_shop_status','not_connected'))+'; полученные ветки='+','.join(sorted(str(method) for method in methods))+'. Неизвестный способ не означает зависимость от тарифа; повторный выбор не запрошен.')
            else:
                parts.append(name+': есть разные способы оформления. Уточню, какой предусмотрен для выбранного тарифа, до оплаты.')
                diagnostics.append(name+': несколько веток оформления='+','.join(sorted(str(method) for method in methods))+'. Соответствие выбранному тарифу требует проверки.')
            handoff=True
    if shared_links:
        names=', '.join(item[0] for item in shared_links)
        state='matched' if all(item[2]=='matched' for item in shared_links) else 'unavailable'
        parts.append(instruction_for(shared_links[0][1],names,state))
    if apple_requested:
        from .apple_matching import ALIASES
        codes=facts.get('appstore_selection',{}).get('requested_codes',[])
        from .apple_matching import exact_quote
        quote=exact_quote(facts,latest)
        if quote and quote.get('price') is not None:
            parts.append(f"App Store, регион {quote['region']}: {quote['amount']:g} {quote['currency']} стоит {quote['price']} ₽. После оплаты подарочный код нужно погасить в App Store.")
        elif quote:
            parts.append(f"App Store: регион {quote['region']} и сумма {quote['amount']:g} {quote['currency']} уже указаны. Стоимость нужно проверить перед оформлением.")
            diagnostics.append('App Store: '+quote['reason']);handoff=True
        elif len(codes)==1:
            label={'US':'США','RU':'Россия'}.get(codes[0],ALIASES.get(codes[0],(codes[0],))[0].capitalize())
            parts.append(f'App Store: регион {label} уже указан. Напишите, на какую сумму хотите пополнить баланс. Пароль от Apple ID для покупки подарочного кода не нужен.')
        elif not codes:
            parts.append('Для App Store нужны регион вашего Apple ID и сумма пополнения. Пароль от Apple ID для покупки подарочного кода не нужен.')
        else:
            parts.append('Для App Store указано несколько регионов. Подскажите, какую сумму хотите пополнить для каждого аккаунта. Пароль от Apple ID для покупки подарочного кода не нужен.')
        source=facts.get('appstore',{}).get('status','not_connected')
        if source not in ('available','truncated'):
            parts.append('Текущие региональные номиналы сейчас не удалось проверить. Доступность и цену нужно подтвердить до оплаты.')
            diagnostics.append('App Store: региональный источник='+str(source)+'; живой экспорт='+str(facts.get('live_shop_status','not_connected'))+'; выбранные регионы='+','.join(codes)+'. Номиналы из обычного каталога не подтверждают региональное наличие.')
            handoff=True
    if navigation and not diagnostics:parts.insert(0,navigation)
    return {'reply':'\n\n'.join(parts),'summary':'Объяснение оформления по данным магазина; сроки, отсутствие входа и успех не обещаны.',
            'handoff':handoff,'source_ids':[],
            'evidence':{'status':'catalog_review' if diagnostics else 'catalog_direct','sources':[],
                        'handoff_reason':('Нужна проверка неподтверждённых условий оформления.' if diagnostics else
                                          'Требуется действие владельца при оформлении; отказ проверки ответа не установлен.') if handoff else '',
                        'diagnostic':'Этап: проверка инструкции оформления. '+'; '.join(diagnostics) if diagnostics else ''}}
