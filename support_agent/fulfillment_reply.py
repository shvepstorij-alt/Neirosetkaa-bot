"""Customer workflow explanations from verified shop fields, without model guesses."""
import re


def workflow_reply(facts,latest):
    lines=[line.strip() for line in latest.splitlines() if line.strip()]
    questions=[line for line in lines if re.search(r'оформ|кроме оплаты|что.*прислать|какие.*данные|как.*(?:получить|скопировать|найти).*ссылк',line,re.I)]
    if not questions:return None
    if re.search(r'функци|лимит|входит|отлич|возврат|ошиб|не работает|закуп|поставщик|себестоим|18\s*\+|порно|эротическ',latest,re.I):return None
    from .catalog_context import _words,_families
    from .request_context import _service_names
    known_words={word for name in _service_names(facts.get('catalog',[])) for word in name}
    ordinary=set('что нужно потребуется данные почта пароль оплата оплаты оформить оформление оформления выбрать тариф тарифы регион сумма пополнение баланс после для и или как куда спасибо пожалуйста сервис сервисы apple id store app почту аккаунт аккаунта'.split())
    for row in facts.get('requested_catalog',[]):
        known_words.update(_words(row.get('service','')));known_words.update(_words(row.get('plan','')))
    for line in lines:
        if line.startswith(('Сервисы текущего обращения:','Сервисы:','Регион Apple ID:')):continue
        for tail in re.findall(r'\bи\s+([A-Za-zА-Яа-яЁё][\w-]*)',line,re.I):
            if tail.casefold() not in known_words|ordinary:return None
    from .workflow_policy import policies_for
    rows=facts.get('requested_catalog',[])
    apple_requested='appstore' in facts.get('catalog_selection',{}).get('mentioned_known_services',[])
    if not rows and not apple_requested:return None
    groups={}
    for row in rows:
        if 'appstore' not in (_families(_words(row.get('service','')))|_families(_words(row.get('service_key','')))):
            groups.setdefault(row.get('service',''),[]).append(row)
    if not all(groups):return None
    parts=[];diagnostics=[];handoff=False
    from .bot_guide import purchase_navigation
    navigation=purchase_navigation(facts,apple=apple_requested and not groups)
    if navigation:parts.append(navigation)
    for name,items in groups.items():
        if re.search(r'тариф|цен|стоим',latest,re.I):
            prices=[]
            for item in items:
                price=item.get('catalog_price_rub')
                if type(price) is int and price>0:prices.append(f"{item.get('plan','')}: {price} ₽")
            if prices:parts.append(f'Для {name} есть такие тарифы:\n'+'\n'.join(dict.fromkeys(prices)))
        owner_policies=policies_for({'requested_catalog':items})
        methods={item.get('fulfillment','not_verified') for item in items}
        if any(p['service']=='suno' for p in owner_policies) and methods!={'account_details'}:
            parts.append(name+': для оформления нужны почта и пароль от аккаунта. Перед оплатой нужно согласовать порядок оформления; платёжную ссылку от сервиса для этого способа присылать не нужно.')
            diagnostics.append(name+': конфликт способов оформления. Владелец подтвердил account_details (почта и пароль), живой каталог передаёт '+','.join(sorted(methods))+'. Нужно согласовать настройку товара; метод в магазине не изменён автоматически.')
            handoff=True
            continue
        if owner_policies:
            parts.append(name+': выберите тариф и оплатите заказ. После оплаты пришлите сюда, в эту переписку, почту и пароль от аккаунта.')
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
            diagnostics.append(name+': кодовая ветка подтверждена; наличие и выдача кода этим источником не подтверждаются.')
            handoff=True
        else:
            parts.append(name+': способ оформления зависит от выбранного тарифа. Какой тариф вы хотите оформить?')
            diagnostics.append(name+': полученные ветки оформления='+','.join(sorted(methods)))
            handoff=True
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
            parts.append('Для App Store указано несколько регионов. Подскажите, какую сумму хотите пополнить для каждого аккаунта.')
    return {'reply':'\n\n'.join(parts),'summary':'Объяснение оформления по данным магазина; сроки, отсутствие входа и успех не обещаны.',
            'handoff':handoff,'source_ids':[],
            'evidence':{'status':'catalog_review' if diagnostics else 'catalog_direct','sources':[],
                        'diagnostic':'Этап: проверка инструкции оформления. '+'; '.join(diagnostics) if diagnostics else ''}}
