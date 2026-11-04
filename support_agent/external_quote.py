"""Owner-approved estimates for non-catalogue services, never an order or firm offer."""
import json
import re
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from urllib.parse import urlsplit


POLICY={'source':'owner_confirmed_2026_11_04','rub_per_usd':100,
        'rounding':'ceil_to_100_rub_minus_10','final_owner_approval_required':True,
        'absence_from_catalog_does_not_mean_unavailable':True}


def public_sites(text):
    """Persist only public homepage hosts, never checkout paths, query or credentials."""
    from .service_discovery import official_domain
    result=[]
    for url in re.findall(r'https://[^\s<>"\x27]+',str(text)):
        parts=urlsplit(url.rstrip('.,)'))
        # Account/checkout/session links are not public research input.
        if parts.query or parts.fragment or re.search(r'checkout|session|auth|login|account|token|invoice|pay/',parts.path,re.I):continue
        host=official_domain(url.rstrip('.,)'))
        if host and host not in result:result.append(host)
    return result[:2]


def intake_reply(facts,question):
    if facts.get('requested_catalog') or facts.get('public_service_sites'):return None
    if re.search(r'(?:сервис|сайт)[^.!?\n]{0,55}(?:нет|отсутств|не\s+наш[её]л)[^.!?\n]{0,25}каталог|'
                 r'(?:нет|отсутств)[^.!?\n]{0,25}каталог[^.!?\n]{0,45}(?:оформ|оплат)|'
                 r'(?:оформ|оплат)[^.!?\n]{0,40}(?:другой|любой|неизвестный)\s+сервис',question,re.I):
        return _answer('Можно рассмотреть индивидуальное оформление) Пришлите ссылку на сайт и нужный тариф. Проверю стоимость; окончательные сумму и возможность оформления подтвержу отдельно.',
                       'Этап: индивидуальный запрос вне каталога. Публичный сайт не указан; сведения о цене и доступности не выдумывались.')
    return None


def estimate(usd):
    try:value=Decimal(str(usd))
    except InvalidOperation:raise ValueError('USD amount invalid') from None
    if not value.is_finite() or value<=0 or value>10000:raise ValueError('USD amount outside estimate range')
    return max(90,int((value*100/100).to_integral_value(rounding=ROUND_CEILING)*100)-10)


def _answer(text,reason,sources=()):
    return {'reply':text,'summary':'Запрос услуги вне каталога; окончательное оформление и сумма не подтверждены.',
            'handoff':True,'source_ids':[],'research':{'topic':'none','products':[]},
            'evidence':{'status':'catalog_review','sources':list(sources),'diagnostic':reason,
                        'handoff_reason':'Возможность оформления и окончательную сумму подтверждает владелец.'}}


async def run(service,facts,question,plan,job):
    """Separate verified provider USD from deterministic approximate shop RUB."""
    if facts.get('requested_catalog') or facts.get('catalog_selection',{}).get('mentioned_known_services'):return None
    if not facts.get('public_service_sites') and not re.search(r'оформ|оплат|купить|через\s+(?:вас|нас)|сколько|стоим|цен',question,re.I):return None
    products=plan.get('products',[])
    sites=facts.get('public_service_sites',[])
    if (not products or products==['other']) and len(sites)==1:
        from .service_discovery import public_name
        candidate=sites[0].split('.')[0].replace('-',' ')
        if public_name(candidate):products=[candidate]
    if not products or products==['other']:return None
    if not sites:
        return _answer('Можно рассмотреть оформление сервиса, даже если его нет в каталоге) Пришлите ссылку на сайт и нужный тариф. Проверю условия и ориентировочную стоимость; окончательно возможность оформления и сумму подтвержу отдельно.',
                       'Этап: услуга вне каталога. Не получена публичная ссылка сервиса; отсутствие товара не означает отказ в оформлении.')
    if len(products)!=1 or len(sites)!=1:
        return _answer('Пришлите, пожалуйста, один сайт и нужный тариф, чтобы не смешать условия разных сервисов.',
                       'Этап: оценка внешней услуги. Сервисов='+str(len(products))+'; публичных сайтов='+str(len(sites))+'. Требуется однозначное сопоставление.')
    stage='проверка принадлежности сайта';evidence={}
    def charge():
        if not service.store.still_current(job):raise ValueError('Stale research job')
        service.charge_call()
    try:
        from .service_discovery import discover
        charge();identity=await discover(service.ai,products[0],charge_extra=charge)
        if not any(sites[0]==d or sites[0].endswith('.'+d) for d in identity['domains']):
            return _answer('Не удалось подтвердить, что ссылка относится к нужному сервису. Проверьте адрес сайта, пожалуйста.',
                           'Этап: проверка принадлежности сайта. Указанный домен не совпал с подтверждёнными доменами сервиса; совпадений=0.')
        from .research import lookup,failure_details
        stage='чтение официальных тарифов';charge()
        evidence=await lookup(service.ai,{'topic':'plans','products':products},charge_extra=charge,
                              _registry={products[0]:(products[0]+' consumer software subscription',tuple(identity['domains']))},
                              question_facets=('prices','plans'))
        if not service.store.still_current(job):return None
        from .feature_claims import _read
        from .privacy import scrub
        sources=evidence.get('sources',[]);ids=[s['id'] for s in sources]
        if not ids:raise ValueError('No official price citations')
        fields={'plan':{'type':'string','minLength':1,'maxLength':60},
                'usd':{'type':'string','pattern':r'^\d{1,5}(\.\d{1,2})?$'},
                'billing':{'type':'string','enum':['monthly','annual_total','annual_monthly_equivalent','one_time']},
                'scope':{'type':'string','enum':['account','seat']},
                'source_ids':{'type':'array','minItems':1,'maxItems':3,'items':{'type':'integer','enum':ids}}}
        schema={'type':'object','properties':{'plans':{'type':'array','maxItems':4,'items':{'type':'object','properties':fields,'required':list(fields),'additionalProperties':False}}},'required':['plans'],'additionalProperties':False}
        stage='выделение официальных USD цен';charge()
        payload={'model':service.ai.model,'store':False,'max_output_tokens':1400,
                 'input':json.dumps({'sources':sources,'digest':evidence.get('digest','')},ensure_ascii=False),
                 'instructions':'Extract at most four current consumer software USD plans. No API plans, trials, discounts, free or custom pricing. Preserve billing commitment and per-seat scope. Annual monthly equivalent must be labelled annual_monthly_equivalent, never monthly. Use only cited evidence; if unsure return empty plans. No customer data or invented amounts.',
                 'text':{'format':{'type':'json_schema','name':'official_usd_quotes','strict':True,'schema':schema}}}
        extracted=_read(await service.ai.request('responses',json=payload),stage)
        plans=extracted.get('plans')
        if not isinstance(plans,list) or len(plans)>4:raise ValueError('Invalid price extraction')
        for row in plans:
            if (not isinstance(row,dict) or set(row)!=set(fields) or not isinstance(row['plan'],str)
                    or not 1<=len(row['plan'])<=60 or scrub(row['plan'])!=row['plan']
                    or row['billing'] not in fields['billing']['enum'] or row['scope'] not in ('account','seat')
                    or not isinstance(row['source_ids'],list) or not row['source_ids']
                    or any(type(i) is not int or i not in ids for i in row['source_ids'])
                    or not isinstance(row['usd'],str) or not re.fullmatch(r'\d{1,5}(?:\.\d{1,2})?',row['usd'])):raise ValueError('Invalid price row')
            estimate(row['usd'])
        if not plans:raise ValueError('No unambiguous USD consumer prices')
        stage='независимая проверка официальных цен';charge()
        review_schema={'type':'object','properties':{'accepted':{'type':'array','minItems':len(plans),'maxItems':len(plans),'items':{'type':'boolean'}}},'required':['accepted'],'additionalProperties':False}
        payload=dict(payload,input=json.dumps({'plans':plans,'sources':sources,'digest':evidence.get('digest','')},ensure_ascii=False),
                     instructions='Independently verify each proposed plan against cited evidence for that exact plan: USD amount, billing period, annual commitment, per-seat scope. Reject unsupported, stale, promotional, API, or ambiguous prices. Return one boolean in input order. Do not infer from arithmetic or memory.',
                     text={'format':{'type':'json_schema','name':'verify_usd_quotes','strict':True,'schema':review_schema}})
        review=_read(await service.ai.request('responses',json=payload),stage)
        accepted=review.get('accepted')
        if not isinstance(accepted,list) or len(accepted)!=len(plans) or any(type(v) is not bool for v in accepted):raise ValueError('Invalid price review')
        if not service.store.still_current(job):return None
        labels={'monthly':'за месяц','annual_total':'за год','annual_monthly_equivalent':'за месяц при оплате года целиком','one_time':'разово'}
        lines=[]
        for row,ok in zip(plans,accepted):
            if not ok:continue
            suffix=' за место' if row['scope']=='seat' else ''
            line=f"{row['plan']}: ${row['usd']} {labels[row['billing']]}{suffix}"
            if row['billing']=='annual_monthly_equivalent':
                line+='; это не цена отдельного месяца. Итог за год нужно проверить перед расчётом.'
            else:line+=f"; через нас ориентировочно {estimate(row['usd'])} ₽."
            lines.append(line)
        if not lines:raise ValueError('All official USD quotes rejected')
        text='На сайте сервиса подтверждены такие варианты:\n\n'+'\n\n'.join(lines)+'\n\nЭто предварительный расчёт по курсу 100 ₽ за $1 с округлением. Окончательные сумму и возможность оформления подтвержу после проверки выбранного тарифа. Какой вариант вам нужен?)'
        return _answer(text,'Этап: предварительная оценка внешней услуги. Строк='+str(len(plans))+'; подтверждено='+str(len(lines))+'. Цены проверены моделью по сводке поиска, не гарантированы. Коммерческое подтверждение владельца ещё не получено.',sources)
    except Exception as exc:
        if str(exc) in ('Stale research job','Daily API request limit reached'):raise
        if not service.store.still_current(job):return None
        from .research import failure_details
        known={'No official price citations':'официальные цитаты цен отсутствуют','Invalid price extraction':'неверная структура тарифов','Invalid price row':'нарушен контракт строки цены','No unambiguous USD consumer prices':'не получены однозначные потребительские USD цены','Invalid price review':'неверный контракт проверки цен','All official USD quotes rejected':'все цены отклонены независимой проверкой'}
        reason=known.get(str(exc)) or failure_details(exc,'search')
        return _answer('Ссылку получил. Сейчас не получилось надёжно подтвердить стоимость выбранного тарифа. Проверю её вручную перед оформлением; неподтверждённую сумму называть не буду.',
                       'Этап: '+stage+'. Причина: '+reason,evidence.get('sources',[]))
