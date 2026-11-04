"""Small deterministic protections around conversational model output."""
import re


def current_request(messages, batch_first, latest_mid):
    """Text of the current collected batch, not every unanswered past question.

    `messages` contains mid/role/text rows in arbitrary order. Full history stays
    available separately to the model for references such as «а у второго?».
    A missing legacy batch boundary falls back to the newest message rather than
    silently reviving every earlier topic. Deleted fragments are simply absent.
    """
    first=batch_first if isinstance(batch_first,int) and 0<batch_first<=latest_mid else latest_mid
    rows=sorted((m for m in messages if m['role']=='client'
                 and first<=m['mid']<=latest_mid),key=lambda m:m['mid'])
    text='\n'.join(m['text'] or '' for m in rows)
    replacements=list(re.finditer(r'теперь\s+только|вместо\s+этого|предыдущий\s+вопрос\s+отменяю',text,re.I))
    return text[replacements[-1].start():] if replacements else text


def _catalog_public_brands(facts):
    """Derive public brands from fresh matches, never the whole catalogue."""
    from .catalog_context import _families, _words, _contains
    from .research import PRODUCTS
    from .service_discovery import public_name
    brands=[]
    for row in (facts or {}).get('requested_catalog',[]):
        if not isinstance(row,dict):continue
        name=str(row.get('service','')).strip()
        key=str(row.get('service_key','')).strip().casefold()
        families=_families(_words(name+' '+key))
        if 'appstore' in families:continue  # Dynamic gift stock, not an iCloud plan.
        known=[p for p in PRODUCTS if p in families]
        if known:
            brands.extend(known)
            continue
        family_brands=[p for p in sorted(families) if public_name(p)]
        if family_brands:
            brands.extend(family_brands)
            continue
        # A dot may be part of a displayed software brand (not a client URL).
        # Reject private/link-like labels before normalizing punctuation.
        from .privacy import scrub,sensitive
        if scrub(name)!=name or sensitive(name) or re.search(r'@|://|[/\\]',name):continue
        title=re.sub(r'\s+',' ',name.casefold().replace('.',' ')).strip()
        # An opaque/internal SKU is not a public software brand. Prefer a key
        # only if the displayed title actually contains that public label.
        compact_title=re.sub(r'[^a-z0-9]','',name.casefold())
        if public_name(key) and (_contains(_words(name),_words(key)) or key==compact_title):
            brands.append(key)
        elif public_name(title):
            brands.append(title)
    return list(dict.fromkeys(brands))[:4]


def consultation_plan(latest, plan, facts=None):
    """Require public evidence for explicit plan/features questions, not private prices.

    Only the current collected client batch is inspected. Never send its text to search.
    The model still resolves references and images; this is a conservative floor.
    """
    text=latest.lower()
    if re.search(r'закуп|поставщик|себестоим|18\s*\+|порно|эротическ', text):
        return {'topic':'none','products':[]}
    # Explicit replacement is different from a multi-message continuation.
    replacements=list(re.finditer(r'теперь\s+только|вместо\s+этого|предыдущий\s+вопрос\s+отменяю',text))
    if replacements:
        text=text[replacements[-1].start():]
    names={'chatgpt':r'chat\s*gpt|чат\s*гпт|\bopenai\b',
           'claude':r'claude|клод|\banthropic\b', 'perplexity':r'perplexity|перплексити',
           'gemini':r'gemini|джемини', 'zoom':r'\bzoom\b|\bзум\b',
           'krea':r'\bkrea\b|\bкреа\b'}
    products=[key for key,pattern in names.items() if re.search(pattern,text)]
    if products:
        # A newly named service must not inherit known brands from old turns.
        # Keep dynamically discovered names: their aliases are not in this map.
        plan=dict(plan,products=[p for p in plan.get('products',[])
                                if p not in names or p in products])
    public=bool(re.search(r'что\s+(?:в\s+них\s+)?входит|возможност|функци|лимит|отлич|сравн|какие\s+(?:сейчас\s+)?модел|какие\s+тариф|о\s+тариф|по\s+тариф|тарифы',text))
    public=public or bool(re.search(r'claude\s*code|клод\s*код|регион|стран|сингапур|поддержива|принимают.*карт|карт.*(?:проход|работ|оплат)',text))
    from .selection_help import is_selection_request
    public=public or is_selection_request(latest,facts)
    catalog_brands=_catalog_public_brands(facts) if facts is not None else []
    explicit_features=bool(re.search(r'что\s+(?:в\s+них\s+)?входит|возможност|функци|лимит|отлич|сравн|'
                                    r'какие\s+(?:сейчас\s+)?модел|ограничени|'
                                    r'официальн[а-яё]*[^.!?\n]{0,40}(?:сведен|возможност|описан|сайт)',text))
    plan_question=bool(re.search(r'какие\s+(?:есть\s+)?(?:платные\s+)?тариф|о\s+тариф|по\s+тариф|тарифы',text))
    checkout_only=bool(re.search(r'как\s+(?:оформить|оплатить|купить|активировать)|'
                                r'что\s+(?:нужно|потребуется)\s+(?:после|для)|'
                                r'какие\s+данные|куда\s+(?:прислать|отправить)',text)) and not explicit_features
    if catalog_brands and (explicit_features or plan_question or is_selection_request(latest,facts)) and not checkout_only:
        products=list(dict.fromkeys(products+catalog_brands))[:4]
        public=True
        # Preserve matching partial model routes, then cover fresh catalogue
        # brands before optional discoveries outside the catalogue.
        from .catalog_context import _families,_words,_contains
        from .research import PRODUCTS
        aligned=[];outside=[]
        for p in plan.get('products',[]):
            family=_families(_words(p))
            candidates=[brand for brand in products if brand in family or brand==p or _contains(_words(p),_words(brand))]
            if len(candidates)==1:aligned.append(candidates[0])
            elif p not in PRODUCTS:outside.append(p)
        plan=dict(plan,products=list(dict.fromkeys(aligned+products+outside))[:4])
        if plan.get('topic')=='none' and re.search(r'сравн|отлич|разниц',text):
            plan=dict(plan,topic='comparison')
    # The model router must not turn a SaaS plan question into a model-release
    # search simply because the requested product also offers AI features.
    if public and plan.get('topic')=='models' and not re.search(r'модел|\bmodels?\b|claude\s*code|клод\s*код',text):
        plan=dict(plan,topic='plans' if re.search(r'тариф|план|подписк',text) else 'features')
    if not products or not public:
        return plan
    requested=list(dict.fromkeys(list(plan.get('products',[]))+products))[:4]
    topic=plan.get('topic','none')
    if topic=='none':
        topic='plans' if re.search(r'тариф|подписк|входит',text) else 'features'
    return {'topic':topic,'products':requested}


def natural_reply(reply):
    """Plain customer punctuation; retain identifiers, prices and line breaks."""
    reply=re.sub(r'^Тарифы магазина:\s*\n','',reply)
    reply=re.sub(r'^([^\n·]+) · ([^\n]+?) — (\d[^\n]*₽)\s*$',r'\1 \2: \3',reply,flags=re.M)
    reply=re.sub(r'(?<=\S)[ \t]+·[ \t]+(?=\S)',' ',reply)
    return reply.replace('—','-').replace('–','-')


def continuing_reply(reply, history):
    reply=natural_reply(reply)
    if not any(m.get('role') in ('assistant','owner') for m in history):
        return reply
    # Only remove an opening salutation, never quoted or embedded client text.
    cleaned=re.sub(r'^\s*(?:здравствуйте|здравствуй|приветствую|привет|добрый\s+(?:день|вечер)|доброе\s+утро)\b[!.,:)\s]*',
                   '',reply,count=1,flags=re.I)
    return cleaned.strip() or 'Чем могу помочь?'


def missing_shop_reply(latest, facts, has_images=False):
    """Public subscription prices must not substitute for an unavailable shop catalog."""
    text=latest.lower()
    procurement=bool(re.search(r'закуп|поставщик|себестоим',text))
    adult=bool(re.search(r'18\s*\+|порно|эротическ',text))
    if procurement or adult:
        parts=[]
        if procurement:
            parts.append('Закупочные условия и поставщиков не раскрываю.')
        if adult:
            parts.append('С созданием контента 18+ не помогаю.')
        return {'reply':' '.join(parts)+' По подпискам и возможностям сервисов подскажу)',
                'summary':'Последний запрос затрагивает закрытые закупочные сведения или контент 18+. Дан краткий отказ без возврата к прежним темам.',
                'handoff':False}
    # A missing retail catalog must not block an independently verifiable
    # explanation of named services. Private prices remain unconfirmed.
    if consultation_plan(latest,{'topic':'none','products':[]},facts)['topic']!='none':
        return None
    catalog_missing=facts.get('source')=='unavailable' or facts.get('catalog_status') in ('read_error','not_connected')
    if has_images or not catalog_missing:
        return None
    retail=bool(re.search(r'у\s+вас|через\s+вас|у\s+тебя|через\s+тебя|ваш\w*\s+(?:магазин|тариф|цен)',text))
    purchase=bool(re.search(r'цен|стоим|тариф|оформ|купить|покуп|подписк',text))
    if not (retail and purchase):
        return None
    return {'reply':'Подскажите, какой сервис и тариф вас интересуют? Точную стоимость и условия уточню перед оформлением.',
            'summary':'Запрос об оформлении или тарифах нашего магазина. Каталог недоступен: цены не подтверждены. Нужна проверка владельца; официальные цены сервисов вместо наших не использованы.',
            'handoff':True}
