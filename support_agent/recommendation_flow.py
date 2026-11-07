"""Reviewed task advice with code-owned retail facts and private instructions."""
import re

from .privacy import scrub


def _generation_intro(facts):
    if facts.get('consultation_intent',{}).get('kind')!='creative_generation':return ''
    from .bot_guide import navigation_facts
    nav=navigation_facts(facts)
    if not nav.get('username'):return ''
    return (f'Начать можно с генераций в @{nav["username"]}) В главном меню есть '
            '«Изображение», «Видео», «Редактировать...» и «Анимировать...». '
            'Модели, параметры и стоимость смотрите перед запуском.')


def _private_part(facts,current):
    from .request_goals import goals
    if 'purchase' not in goals(current,facts):return None
    from .fulfillment_reply import workflow_reply
    from .catalog_context import annotate
    # These are only explicitly/contextually selected client products. Newly
    # proposed public candidates must never solicit unrelated account details.
    names=list(dict.fromkeys(r.get('service','') for r in facts.get('requested_catalog',[]) if r.get('service')))
    apple=facts.get('appstore_selection',{})
    if 'appstore' in facts.get('catalog_selection',{}).get('mentioned_known_services',[]) and not names:
        names=['App Store']
    if not names:return None
    question='Как оформить '+', '.join(names)+'? Что потребуется?'
    if apple.get('requested_codes'):
        question+='\nРегион Apple ID: '+', '.join(apple['requested_codes'])
        from .apple_matching import exact_quote
        quote=exact_quote(facts,current)
        if quote:question+='\n'+str(quote['amount'])+' '+quote['currency']
    prepared=annotate(facts,question)
    prepared['requested_catalog']=facts.get('requested_catalog',[])
    if apple:prepared['appstore_selection']=apple
    return workflow_reply(prepared,question)


async def run(service, facts, question, current, job):
    """Return None for unsupported intent, never relax evidence validation."""
    from .recommendation import task_projection,choose_candidates
    task=task_projection(facts,question)
    if not task['labels']:return None
    from .response_policy import _catalog_public_brands
    creative=task['kind']=='creative_generation'
    products=_catalog_public_brands(facts)
    # A named public tool is still part of a comparison even if it has no
    # separate shop SKU (e.g. Codex vs Kimi). Never fabricate its retail price.
    from .official_services import registry
    from .catalog_context import ALIASES
    official=registry()
    for product in official:
        aliases=ALIASES.get(product,(product,))
        if any(re.search(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',current,re.I) for alias in aliases) and product not in products:
            products.append(product)
    from .response_policy import explicit_public_products
    explicit_products=explicit_public_products(current,facts)
    products=list(dict.fromkeys(products+explicit_products))
    products=products[:4]
    discovery=creative and (not products or bool(re.search(
        r'(?:выбрать|подобрать|подскаж|посовет|порекоменд|нужен|нужна|нужны|какой|какие|другие)'
        r'[^.!?\n]{0,45}(?:сервис|нейросет|инструмент|генератор|редактор)|'
        r'где[^.!?\n]{0,30}(?:созда|генер)|в\s+ч[её]м',current,re.I)))
    if creative and not products:
        # Do not replace an explicitly named unfamiliar brand with our own
        # suggestions. The existing name-discovery route handles that request.
        from .catalog_context import _words,ALIASES
        known={w for aliases in ALIASES.values() for a in aliases for w in _words(a)}
        known.update(w for r in facts.get('catalog',[]) for w in _words(r.get('service','')))
        technical={'ai','api','pdf','docx','xlsx','mp4','png','jpg','jpeg','webp','svg','gif','hd','4k','sso','lora'}
        foreign=[w.casefold() for w in re.findall(r'(?<!\w)[A-Za-z][A-Za-z0-9-]{1,60}(?!\w)',current)]
        if any(w not in known|technical for w in foreign):return None
        named=re.findall(r'(?:нужен|нужна|про|сервис[ае]?|подписк[ауи]\s+на)\s+[«"]?([А-ЯЁ][а-яёА-ЯЁ-]{2,60})',current)
        if any(w.casefold() not in known|{'фото','видео','изображения','ролики'} for w in named):return None
    if not products and not creative:return None
    private=_private_part(facts,current)
    from .request_goals import goals
    if 'purchase' in goals(current,facts) and private is None:return None
    intro=_generation_intro(facts)
    extras=[p for p in (intro,(private or {}).get('reply','')) if p]
    extra_text='\n\n'.join(extras)
    stage='подбор кандидатов';evidence={};public_facts=facts
    def current_job():
        return service.store.still_current(job)
    def charge():
        if not current_job():raise ValueError('Stale research job')
        service.charge_call()
    def incomplete(reason):
        body='\n\n'.join(extras+['Подходящий внешний сервис и тариф под вашу задачу пока не удалось подтвердить. Нужна дополнительная проверка.'])
        if len(body)>1800:
            body='В обращении несколько сервисов и задач. Проверенные инструкции для всех пунктов в одном ответе подготовить не получилось. Давайте разберём их по очереди: с какого сервиса начнём?'
            reason='Подтверждённые инструкции превышают лимит сообщения: символов='+str(len(extra_text))+'; лимит=1800. Инструкции не обрезаны. '+str(reason)
        return {'reply':body,'summary':'Подтверждённые инструкции сохранены. Рекомендация не прошла проверку; непроверенные характеристики не отправляются.',
                'handoff':True,'source_ids':[],'research':{'topic':'none','products':[]},
                'evidence':{'status':'catalog_review','sources':evidence.get('sources',[]),
                    'diagnostic':'Этап: '+stage+'. Причина: '+scrub(str(reason))[:1300],
                    'handoff_reason':'Не завершена проверка соответствия сервиса задаче.'}}
    try:
        if len(extra_text)>900:return incomplete('подтверждённые инструкции требуют отдельного сообщения; остальные пункты не объявлены выполненными.')
        mandatory=[]
        if discovery:
            from .catalog_context import annotate
            from .request_goals import goals
            for clause in re.split(r'[?!;\n]',current):
                if 'purchase' in goals(clause,facts):continue
                mandatory.extend(explicit_public_products(clause,facts))
                selected=annotate(facts,clause)
                mandatory.extend(_catalog_public_brands(selected))
            charge()
            chosen=await choose_candidates(service.ai,facts,question)
            if not current_job():return None
            products=list(dict.fromkeys(mandatory+chosen['products']))[:4];task=chosen['task']
        from .catalog_context import annotate
        # Match researched candidates only, without reviving unrelated previous
        # products. Subscription selection must not include credit top-ups.
        base=dict(facts,requested_catalog=[])
        selected=[]
        for product in products:
            source_rows=facts.get('requested_catalog',[]) if product in mandatory or not discovery else facts.get('catalog',[])
            selected.extend(annotate({'catalog':source_rows},product+' тарифы подписок')['requested_catalog'])
        public_facts=dict(base,requested_catalog=selected,recommendation_mode=True,
                          recommendation_task=task,recommendation_budget=min(1700,1800-len(extra_text)-4))
        if getattr(service,'_probe_capture',False):
            service._probe_facts=public_facts
        stage='поиск официальных возможностей'
        from .research import lookup,public_facets,attach_evidence,with_public_trace,failure_details
        charge()
        evidence=await lookup(service.ai,{'topic':'features','products':products},charge_extra=charge,
                              question_facets=public_facets(question))
        if not current_job():return None
        final_facts=dict(public_facts,public_research=evidence,feature_only=True,feature_claims_v2=True)
        from .feature_claims import draft_claims,review_claims
        stage='выделение проверяемых утверждений'
        charge();draft=await draft_claims(service.ai,final_facts)
        if not current_job():return None
        stage='независимая проверка соответствия задаче'
        charge();review=await review_claims(service.ai,draft,final_facts)
        if not current_job():return None
        if not review.get('supported') or not isinstance(review.get('revised_answer'),dict):
            return incomplete(review.get('reason','проверка не вернула собранный ответ'))
        answer=review['revised_answer']
        parts=extras+[answer['reply']]
        # For an automotive photo, explanation of a symbol is not a diagnosis.
        if facts.get('consultation_intent',{}).get('kind')=='visual_understanding' and re.search(r'прибор|горит|автомоб',question,re.I):
            parts.append('По фото можно разбирать обозначение индикатора, но это не диагностика неисправности. Сверяйтесь с руководством автомобиля.')
        combined='\n\n'.join(parts)
        if len(combined)>1800:return incomplete('проверенные части превышают лимит сообщения; текст не обрезан и не переписан моделью.')
        answer['reply']=combined
        answer['handoff']=bool(answer.get('handoff') or (private or {}).get('handoff'))
        stage='сохранение подтверждений'
        answer=attach_evidence(answer,evidence,allow_catalog_only=True,semantic_review=True)
        meta=answer.setdefault('evidence',{})
        meta.update(content_review={'supported':True,'reason':review['reason']},
                    atomic_review=True,atomic_diagnostics=review['revised_answer'].get('evidence',{}).get('atomic_diagnostics',[]),
                    recommendation_task=task)
        if private:
            detail=private.get('evidence',{}).get('diagnostic')
            if detail:
                meta['private_workflow_diagnostic']=detail
                meta.setdefault('atomic_diagnostics',[]).append('Оформление: '+detail)
            if private.get('handoff'):
                meta['handoff_reason']=detail or 'Оформление выполняется вручную; проверка публичных возможностей не подтверждает выдачу заказа.'
        elif answer.get('handoff'):
            meta['handoff_reason']='Не все варианты или условия задачи подтверждены; причины по утверждениям перечислены отдельно.'
        return with_public_trace(answer,evidence)
    except Exception as exc:
        if not current_job():return None
        if str(exc) in ('Stale research job','Daily API request limit reached','Test API call limit reached'):raise
        from .research import failure_details,with_public_trace
        reason=getattr(exc,'diagnostic',None) or failure_details(exc,'answer_check')
        return with_public_trace(incomplete(reason),evidence)
