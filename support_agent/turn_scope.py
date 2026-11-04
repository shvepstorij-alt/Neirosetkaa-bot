"""Separate current intent from client-derived reference context for all brands."""
import re


def scope(facts, resolved, current, discovered=False):
    from .catalog_context import annotate,service_mentions
    from .fulfillment_reply import _selected_plan_rows
    from .consultation_intent import context
    from .request_context import is_reminder
    rows=facts.get('requested_catalog',[])
    creative=context(current,facts).get('kind')=='creative_generation'
    explicit=service_mentions(current,facts.get('catalog',[]))
    if creative and not explicit and not discovered:
        facts=annotate(facts,current)
        rows=facts.get('requested_catalog',[])
    # A general request for available plans deliberately releases an old plan
    # selection. Otherwise only exact client plan labels can narrow the scope.
    general=bool(re.search(r'какие[^.!?\n]{0,25}тариф|все\s+тариф|список\s+тариф',current,re.I))
    if rows and not general and not (creative and not explicit):
        groups={}
        for row in rows:groups.setdefault(row.get('service',''),[]).append(row)
        selected=[]
        for group in groups.values():
            chosen=_selected_plan_rows(group,current,require_match=True)
            if not chosen:
                for line in reversed(resolved.splitlines()):
                    chosen=_selected_plan_rows(group,line,require_match=True)
                    if chosen:break
            selected.extend(chosen or group)
        facts=dict(facts,requested_catalog=selected)
        rows=selected
    labels=list(dict.fromkeys(str(r.get('service','')) for r in rows if r.get('service')))
    # Dynamic Apple matching already maintains independently verified region and
    # denomination slots. Its scoped text must not lose a split amount reply.
    apple='appstore' in facts.get('catalog_selection',{}).get('mentioned_known_services',[])
    if apple or is_reminder(current):return facts,resolved
    query=(', '.join(labels)+'\n' if labels else '')+current
    # Selected plan identity lives in structured rows, not an extra invented
    # sentence which a shortcut might mistake for another client question.
    from .workflow_policy import route_choice
    if route_choice(current) and re.search(r'оформ|что[^.!?\n]{0,30}нуж|данные',resolved,re.I):
        query+='\nЧто нужно для оформления?'
    from .request_context import _only_service_names
    if _only_service_names(current,facts.get('catalog',[])) and resolved.strip()!=current.strip():
        from .request_goals import goals
        for line in reversed(resolved.splitlines()):
            previous_goals=goals(line,facts)
            if 'purchase' in previous_goals:
                query+='\nЧто нужно для оформления?';break
            if 'price' in previous_goals:
                query+='\nКакие тарифы есть?';break
    return facts,query


def purchase_reply(facts,current):
    """Code-owned payment/choice response; never infer activation from payment."""
    from .request_goals import covers
    from .fulfillment_reply import _selected_plan_rows
    payment=bool(re.search(r'как[^.!?\n]{0,35}оплат|где[^.!?\n]{0,35}оплат',current,re.I))
    choice=bool(re.search(r'давайте|беру|выбираю|хочу\s+(?:тариф|план)|оформим',current,re.I))
    if not (payment or choice) or not covers(current,{'purchase','price'},facts):return None
    rows=facts.get('requested_catalog',[])
    if not rows:return None
    unique={(r.get('service'),r.get('plan'),r.get('catalog_price_rub')) for r in rows}
    if len(unique)!=1:return None
    name,plan,price=next(iter(unique))
    if not name or not plan or type(price) is not int or price<=0:return None
    if any(r.get('fulfillment') not in ('manual','account_details','checkout_link','code_activation_conditional') for r in rows):return None
    from .workflow_policy import policies_for,route_evidence
    if any(route_evidence(p['service'],rows)[0]=='conflict' for p in policies_for(facts)):return None
    if choice and len(_selected_plan_rows(rows,current))!=1:return None
    from .bot_guide import navigation_facts
    nav=navigation_facts(facts)
    if not nav.get('username'):return None
    text=(f'{name} {plan}: {price} ₽\n\n'
          f'1. Откройте @{nav["username"]} и нажмите /start.\n'
          f'2. Перейдите в «{nav["shop_menu"]}» и выберите {name}.\n'
          f'3. Выберите тариф {plan}.\n'
          '4. Выберите доступный способ оплаты и оплатите заказ.\n\n'
          'После оплаты откройте сообщение заказа и следуйте инструкции оформления. Оплата сама по себе не означает, что подписка уже активирована.')
    return {'reply':text,'summary':'Выбранный клиентом тариф и цена из магазина; инструкция оплаты по подтверждённой навигации. Активация не утверждалась.',
            'handoff':False,'source_ids':[],'evidence':{'status':'catalog_direct','sources':[]}}
