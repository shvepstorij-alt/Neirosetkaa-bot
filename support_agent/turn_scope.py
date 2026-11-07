"""Separate current intent from client-derived reference context for all brands."""
import re


def _scoped_plans(groups, catalog, resolved, current):
    """Resolve plan labels with their client-authored service scope.

    A bare choice can continue the last single-service focus. It cannot apply
    another service's plan just because both products offer a plan named Pro.
    """
    from .catalog_context import service_mentions
    from .fulfillment_reply import _selected_plan_rows, _service_plan_scope
    identities={name:set(service_mentions(' '.join(dict.fromkeys(
        str(row.get(field,'')) for row in items for field in ('service','service_key'))),catalog))
        for name,items in groups.items()}
    chosen={};focus=set(groups) if len(groups)==1 else set()
    # Reapply current independently: it may be absent from reconstructed text,
    # and is always authoritative over older client selections.
    turns=[*str(resolved).splitlines(),*str(current).splitlines()]
    for line in (part for turn in turns for part in re.split(r'[.!?](?:\s+|$)',turn) if part.strip()):
        mentioned=set(service_mentions(line,catalog))
        named={name for name,ids in identities.items() if ids & mentioned}
        if mentioned:
            focus=named
        shared=bool(re.search(r'\b(?:обоим|обоих|оба|обе|всем|всех)\b',line,re.I)
                    and re.search(r'выбира|бер[уё]|давайте|хочу|тариф|план',line,re.I))
        if shared and not mentioned:
            focus=set(groups)
        for name,items in groups.items():
            if mentioned:
                if name not in named:continue
                local=line if len(mentioned)==1 and len(named)==1 else _service_plan_scope(name,groups,line)
                if shared:local=line
            elif name in focus and (len(focus)==1 or shared):
                local=line
            else:
                # An unqualified new plan after multiple services is a pending
                # choice, not confirmation of any earlier plans for that list.
                if not mentioned and name in focus and _selected_plan_rows(items,line,require_match=True):
                    chosen.pop(name,None)
                continue
            match=_selected_plan_rows(items,local,require_match=True)
            if match:chosen[name]=match
    return [row for name,items in groups.items() for row in chosen.get(name,items)]


def scope(facts, resolved, current, discovered=False):
    from .catalog_context import annotate,service_mentions
    from .consultation_intent import context
    from .request_context import is_reminder
    rows=facts.get('requested_catalog',[])
    # Latest explicit exclusions/exclusive selection override earlier clauses
    # within the same collected batch, across every catalogue brand.
    if re.search(r'\bне\s+(?:нуж|буду|оформ)|\b(?:пока|хочу|нужен|нужна|нужно|теперь)\s+только',current,re.I):
        from .session_context import _active
        active,_=_active([{'mid':1,'text':current}],facts.get('catalog',[]))
        ids={entry['identity'] for entry in active}
        if ids:
            rows=[r for r in rows if set(service_mentions(r.get('service',''),facts.get('catalog',[])))&ids]
            facts=dict(facts,requested_catalog=rows)
        elif re.search(r'\bне\s+(?:нуж|буду|оформ)',current,re.I):
            removed=set(service_mentions(current,facts.get('catalog',[])))
            if removed:
                rows=[r for r in rows if not set(service_mentions(r.get('service',''),facts.get('catalog',[])))&removed]
                facts=dict(facts,requested_catalog=rows,cancelled_entities=sorted(removed))
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
        selected=_scoped_plans(groups,facts.get('catalog',[]),resolved,current)
        facts=dict(facts,requested_catalog=selected)
        rows=selected
    labels=list(dict.fromkeys(str(r.get('service','')) for r in rows if r.get('service')))
    # Dynamic Apple matching already maintains independently verified region and
    # denomination slots. Its scoped text must not lose a split amount reply.
    apple='appstore' in facts.get('catalog_selection',{}).get('mentioned_known_services',[])
    if apple or is_reminder(current):return facts,resolved
    query=(', '.join(labels)+'\n' if labels else '')+current
    from .selection_help import is_selection_request,has_selection_purpose
    # A short request for another recommendation still refers to the client's
    # supplied task. Pass only task-bearing client lines, not old prices/actions.
    if is_selection_request(current,facts) and not has_selection_purpose(current):
        task_lines=[line for line in resolved.splitlines() if has_selection_purpose(line)
                    and not re.search(r'как[^.!?\n]{0,30}(?:оплат|оформ)|\b(?:стоимость|цена|тарифы)\b',line,re.I)]
        if task_lines:query+='\n'+'\n'.join(task_lines[-3:])
    # Selected plan identity lives in structured rows, not an extra invented
    # sentence which a shortcut might mistake for another client question.
    from .workflow_policy import route_choice
    if route_choice(current) and re.search(r'оформ|что[^.!?\n]{0,30}нуж|данные',resolved,re.I):
        query+='\nЧто нужно для оформления?'
    from .turn_contract import entity_reference,intent_questions
    if entity_reference(current,facts.get('catalog',[])) and resolved.strip()!=current.strip():
        from .request_goals import goals
        for line in reversed(resolved.splitlines()):
            previous_goals=goals(line,facts)
            questions=intent_questions(previous_goals)
            if questions:
                query+='\n'+questions;break
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
