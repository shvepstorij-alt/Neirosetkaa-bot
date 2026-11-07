"""Owner-approved facts only. No historical prices, runtime snapshots or secrets."""
import re


NEW_RULES={
    'canva':{'display_name':'Canva','pattern':r'canva|канва|канве|канву','required_fields':['account_email']},
    'cursor':{'display_name':'Cursor','pattern':r'cursor|курсор|курсоре|курсора','required_fields':['account_email']},
    'capcut':{'display_name':'CapCut','pattern':r'cap\s*cut|капкат|капкут|капкате',
              'required_fields':[],'provided_fields':['account_email','account_password'],
              'account_ownership':'new_provided_account','duration':'month'},
    'midjourney':{'display_name':'Midjourney','pattern':r'midjourney|миджорни|миджерни','required_fields':['account_email','account_password']},
    'telegram_premium':{'display_name':'Telegram Premium','pattern':r'telegram\s*premium|телеграм\s*премиум|тг\s*премиум',
                        'required_fields':['telegram_username']},
    'github':{'display_name':'GitHub','pattern':r'git\s*hub|гитхаб|гитхабе','required_fields':['account_email','account_password']},
    'gemini':{'display_name':'Gemini','pattern':r'gemini|джемини|гемини','required_fields':['account_email','account_password']},
    'supergrok':{'display_name':'SuperGrok','pattern':r'super\s*grok|супер\s*грок','required_fields':['account_email','account_password']},
    'genspark':{'display_name':'Genspark','pattern':r'genspark|дженспарк|генспарк',
                'required_fields':['checkout_url'],'expected_fulfillment':'checkout_link',
                'compatible_fulfillment':['checkout_link']},
    'zai':{'display_name':'Z.ai','pattern':r'z\s*\.\s*ai|\bzai\b|зи\s*ай',
           'required_fields':[],
           'field_alternatives':[['account_email','account_password'],['checkout_url']],
           'method_options':['account_details','checkout_link'],
           'compatible_fulfillment':['manual','account_details','checkout_link']},
}

CHECKOUT_STEPS=['open_service','login_own_account','open_plans','select_plan','open_checkout','copy_checkout_url','send_checkout_url']
for _id,_name,_pattern in (
    ('elevenlabs','ElevenLabs',r'eleven\s*labs|элевенлабс|илевенлабс'),
    ('heygen','HeyGen',r'heygen|хейген|хейджен'),
    ('higgsfield','Higgsfield AI',r'higgsfield(?:\s+ai)?|хиггсфилд|хиггсфиелд'),
    ('kimi','Kimi',r'kimi|кими'),
    ('kling','Kling AI',r'kling(?:\s+ai)?|клинг'),
    ('lovable','Lovable',r'lovable|ловабл|лавабл'),
    ('make','Make',r'make|мейк'),
    ('manus','Manus',r'manus|манус'),
    ('runway','Runway',r'runway|ранвей|ранвэй')):
    NEW_RULES[_id]={'display_name':_name,'pattern':_pattern,'required_fields':['checkout_url'],
                    'expected_fulfillment':'checkout_link','compatible_fulfillment':['checkout_link'],
                    'workflow_steps':list(CHECKOUT_STEPS),'workflow_steps_source':'owner_confirmed',
                    'workflow_steps_status':'available'}
for _id in ('genspark','zai'):
    NEW_RULES[_id].update(workflow_steps=list(CHECKOUT_STEPS),workflow_steps_source='owner_confirmed',
                          workflow_steps_status='available')


_EXTRA_TEXT_SERVICES=(
    ('krea',r'krea|креа'),
    ('suno',r'suno|суно'),
    ('zoom',r'zoom|зум|зуме|зума'),
    ('openrouter',r'openrouter|open router|опенроутер'),
    ('spotify',r'spotify|спотифай'),
    ('youtube',r'youtube|you tube|ютуб|ютубе'),
)
_PURCHASE_CUE=re.compile(
    r'подписк|тариф|купить|куплю|купит|оплат|оформ|аккаунт|почт|'
    r'сколько\s+стоит|стоим|цен[аыуе]|premium|премиум|\bplus\b|\bpro\b|'
    r'что\s+(?:нужно|потребуется)|какие\s+данные|продл|пополн|заказать|'
    r'хочу|нужен|нужна|нужно|подключ',re.I)
_NON_PRODUCT={
    'cursor':re.compile(r'пропал|исчез|мышь|мышк|указател|мига|не\s+двига|на\s+экране|в\s+тексте|в\s+строке|ввода|выдел',re.I),
    'youtube':re.compile(r'видео|ролик|залить|выложить|вылож|снять|снят|монтаж|превью|шортс|shorts|контент|клип|для\s+(?:ютуб\w*|youtube)|на\s+(?:ютуб\w*|youtube)',re.I),
}


def _service_patterns():
    patterns=list(_EXTRA_TEXT_SERVICES)
    for service,rule in NEW_RULES.items():
        patterns.append((service,rule['pattern']))
    return patterns


def _sentence_kind(service, sentence):
    non=_NON_PRODUCT.get(service)
    if service in _NON_PRODUCT and ((non and non.search(sentence)) or not _PURCHASE_CUE.search(sentence)):
        return 'ambiguous'
    return 'mention'


def classify_service_mentions(text):
    """purchase, mention, or ambiguous for each service keyword in the text.

    Cursor and YouTube are ambiguous when the sentence is about a pointer or
    a video, or when the word appears without a purchase cue. Other services
    keep a plain mention so an obvious catalogue question still resolves.
    """
    found={}
    sentences=re.split(r'[.!?\n]+',str(text or '')) or ['']
    for service,pattern in _service_patterns():
        compiled=re.compile(r'(?<!\w)(?:'+pattern+r')(?!\w)',re.I)
        kinds=set()
        for sentence in sentences:
            if not compiled.search(sentence):continue
            kind=_sentence_kind(service,sentence)
            if kind=='mention' and service in _NON_PRODUCT and _PURCHASE_CUE.search(sentence):
                kind='purchase'
            kinds.add(kind)
        if not kinds:continue
        if 'purchase' in kinds:
            found[service]='purchase'
        elif 'ambiguous' in kinds:
            found[service]='ambiguous'
        else:
            found[service]='mention'
    return found


def ambiguous_service_keys(text):
    return {service for service,kind in classify_service_mentions(text).items() if kind=='ambiguous'}


def ambiguity_reply(text):
    """Ask which service was meant. Never request account data from one word."""
    mentions=classify_service_mentions(text)
    ambiguous=sorted(service for service,kind in mentions.items() if kind=='ambiguous')
    if not ambiguous or any(kind=='purchase' for kind in mentions.values()):
        return None
    if ambiguous==['cursor']:
        question='Уточните, пожалуйста: речь про подписку на редактор Cursor или про указатель на экране?'
    elif ambiguous==['youtube']:
        question='Уточните, пожалуйста: вам нужна подписка YouTube или помощь с видео для YouTube?'
    else:
        labels=', '.join({'cursor':'Cursor','youtube':'YouTube'}.get(service,service) for service in ambiguous)
        question='Уточните, пожалуйста, вы про оформление '+labels+' или о чём-то другом? Данные аккаунта пока не нужны.'
    return {'reply':question,
            'summary':'Совпадение по слову не принято за покупку. Задан уточняющий вопрос, данные доступа не запрошены.',
            'handoff':False,'source_ids':[],
            'evidence':{'status':'clarification','sources':[],
                        'diagnostic':'Этап: уточнение сервиса. Одно совпавшее слово не запускает запрос почты или пароля.'}}


def policies_for(facts):
    rows=facts.get('requested_catalog',[])
    names=' '.join(str(r.get(k,'')) for r in rows if isinstance(r,dict)
                   for k in ('service_key','service'))
    current=str(facts.get('conversation',{}).get('latest_client_message',''))
    mentions=classify_service_mentions(current)
    def _matched(service,pattern):
        if mentions.get(service)=='ambiguous':return False
        found=re.search(r'(?<!\w)(?:'+pattern+r')(?!\w)',names+' '+current,re.I)
        if not found:return False
        # A free-text hit without a catalogue row still needs a real purchase
        # cue for the services that are easy to confuse with ordinary words.
        in_names=bool(re.search(r'(?<!\w)(?:'+pattern+r')(?!\w)',names,re.I))
        if service in _NON_PRODUCT and not in_names and mentions.get(service)!='purchase':
            return False
        return True
    services=[service for service,pattern in _EXTRA_TEXT_SERVICES if _matched(service,pattern)]
    policies=[{'service':service,'source':'owner_confirmed','confirmed_on':'2026-10-29',
             'expected_fulfillment':'account_details',
             'required_fields':['account_email','account_password'],
             'delivery_channel':'personally_to_alexander_telegram',
             'customer_instruction':'После оплаты пришлите необходимые данные сюда, в эту переписку. Не направлять клиента в другой чат. Дальше требуется ручное оформление владельцем.',
             'agent_can_login':False,'agent_can_pay':False,
             'agent_collects_credentials':False,
             'google_sign_in':({'required_fields':['google_account_email','google_account_password'],
                                'condition':'only_when_customer_uses_google_sign_in',
                                'never_request':['one_time_code','backup_code','session_token']}
                               if service in ('suno','zoom','openrouter','spotify','youtube') else None),
             'duration':'not_confirmed','success_criterion':'not_confirmed'} for service in services]
    for service,rule in NEW_RULES.items():
        if not _matched(service,rule['pattern']):continue
        policy={k:v for k,v in rule.items() if k!='pattern'}
        policy.update(service=service,source='owner_confirmed',confirmed_on='2026-11-04',
                      delivery_channel='personally_to_alexander_telegram',
                      customer_instruction='Данные передаются сюда, в эту же переписку. Оформление выполняет владелец вручную.',
                      agent_can_login=False,agent_can_pay=False,agent_collects_credentials=False,
                      required_fields_status='owner_confirmed',required_fields_source='owner_confirmed',
                      google_sign_in=({'required_fields':['google_account_email','google_account_password'],
                                       'condition':'only_when_customer_uses_google_sign_in',
                                       'never_request':['one_time_code','backup_code','session_token']}
                                      if service=='midjourney' else None),
                      success_criterion='not_confirmed')
        policy.setdefault('expected_fulfillment','manual')
        policy.setdefault('compatible_fulfillment',['manual'] if service=='capcut' else ['manual','account_details'])
        policy.setdefault('duration','not_confirmed')
        policies.append(policy)
    return policies


def route_choice(text):
    """Explicit latest route choice only; never select a route by price or order."""
    for line in reversed(str(text).splitlines()):
        if line.startswith(('Сервисы текущего обращения:','Сервисы:')):continue
        if re.search(r'или',line,re.I) and re.search(r'ссылк',line,re.I) and re.search(r'почт|парол',line,re.I):return None
        choices={}
        patterns={
            'checkout_link':r'по\s+(?:плат[её]жной\s+)?ссылк[а-яё]*|(?:выбираю|выберу|давайте?|хочу|пришлю|передам|отправлю)[^.!?,\n]{0,15}ссылк[а-яё]*|как\s+(?:получить|скопировать|найти)[^.!?,\n]{0,25}ссылк[а-яё]*',
            'account_details':r'по\s+(?:почт[еуы]|данным\s+аккаунта)|(?:выбираю|выберу|давайте?|хочу|пришлю|передам|отправлю)[^.!?,\n]{0,15}(?:почт[а-яё]*|парол[а-яё]*|данны[ме])'}
        candidates=False
        for kind,pattern in patterns.items():
            for match in re.finditer(pattern,line,re.I):
                candidates=True
                prefix=re.split(r'[,;.!?]',line[:match.start()])[-1]
                if re.search(r'(?<!\w)(?:не|без)(?!\w)',prefix,re.I):continue
                choices[kind]=True
        if candidates:return next(iter(choices)) if len(choices)==1 else None
    return None


def instruction_for(policy,name,state='matched',google=False,style='direct',choice=None):
    """Render each confirmed data requirement without inheriting another service."""
    if style=='fallback' and policy['service'] not in NEW_RULES:
        if state=='conflict':
            return f'Для оформления {name} нужны почта и пароль от аккаунта. Перед оплатой нужно согласовать порядок оформления заказа.'
        if state=='unavailable':
            return f'Для оформления {name} нужны почта и пароль от аккаунта. Их нужно будет прислать сюда, в эту переписку. Порядок оформления проверю перед оплатой.'
        return f'Для оформления {name} нужны почта и пароль от вашего аккаунта. После выбора тарифа и оплаты пришлите их сюда, в эту переписку. Дальше потребуется ручное оформление.'
    fields=policy['required_fields']
    if policy.get('field_alternatives'):
        if choice in policy.get('method_options',[]):
            selected=dict(policy)
            selected.pop('field_alternatives')
            selected['required_fields']=['checkout_url'] if choice=='checkout_link' else ['account_email','account_password']
            return instruction_for(selected,name,state,google,style)
        return (name+': можно оформить по почте и паролю от аккаунта либо по ссылке на оплату выбранного тарифа. '
                'Какой вариант вам удобнее? Для оформления достаточно данных выбранного способа, оба набора присылать не нужно. '
                'Данные выбранного способа нужно прислать сюда, в эту переписку. '
                +('Порядок оформления согласуем перед оплатой заказа.' if state!='matched' else ''))
    if fields==['checkout_url']:
        if policy.get('workflow_steps_status')!='available':
            return (name+': пришлите сюда, в эту переписку, ссылку на оплату выбранного тарифа сервиса. Самостоятельно по ней не оплачивайте. Если не получается найти ссылку, пришлите скриншот страницы тарифа без платёжных данных.'
                    +(' Порядок оформления согласуем перед оплатой заказа.' if state!='matched' else ''))
        text=(name+': оформление по ссылке на оплату выбранного тарифа. Получить её можно так:\n'
              '1. Откройте сайт сервиса и войдите в свой аккаунт.\n'
              '2. Откройте тарифы или подписку и выберите нужный план.\n'
              '3. Перейдите на страницу оплаты, но самостоятельно не оплачивайте.\n'
              '4. Скопируйте ссылку этой страницы и пришлите сюда, в эту переписку.\n'
              'Пароль от аккаунта для этого способа не нужен.')
        if state!='matched':text+=' Перед оплатой заказа согласуем текущий способ оформления в магазине.'
        return text
    if policy.get('account_ownership')=='new_provided_account':
        text=(name+': доступен только новый аккаунт с подпиской на месяц. '
              'Я выдам почту и пароль от нового аккаунта. Подписку на существующий аккаунт клиента этим способом не оформляем; '
              'ваши почта и пароль для этого не нужны.')
        if state!='matched':text+=' Порядок выдачи согласуем перед оплатой.'
        else:text+=' Выберите нужный товар в магазине и оплатите заказ.'
        return text
    label={('account_email',):'почта от вашего аккаунта',
           ('telegram_username',):'ник Telegram-аккаунта, на который нужна подписка',
           ('account_email','account_password'):'почта и пароль от аккаунта'}[tuple(fields)]
    sent={('account_email',):'почту от вашего аккаунта',
          ('telegram_username',):'ник Telegram-аккаунта, на который нужна подписка',
          ('account_email','account_password'):'почту и пароль от аккаунта'}[tuple(fields)]
    if state=='matched':
        text=name+': выберите тариф и оплатите заказ. После оплаты пришлите сюда, в эту переписку, '+sent+'.'
    else:
        verb='нужна' if fields==['account_email'] else 'нужен' if fields==['telegram_username'] else 'нужны'
        text=name+': для оформления '+verb+' '+label+'. Перед оплатой нужно согласовать порядок оформления. После согласования и оплаты пришлите сюда, в эту переписку, '+sent+'.'
        if state=='conflict' and 'account_password' in fields:
            text+=' Платёжную ссылку от сервиса для этого способа присылать не нужно.'
        if state=='unavailable':text+=' Порядок оформления проверю перед оплатой.'
    if fields==['account_email']:text+=' Пароль не нужен.'
    if fields==['telegram_username']:text+=' Пароль и коды входа Telegram не нужны.'
    if google and policy.get('google_sign_in'):
        text+=' Если в этот сервис вы входите через Google, для оформления нужны почта и пароль от соответствующего Google-аккаунта. Одноразовые и резервные коды присылать не нужно.'
    return text


def enrich(facts):
    return dict(facts,owner_workflow_policies=policies_for(facts))


def policy_scope_issue(policy,rows):
    if policy.get('account_ownership')!='new_provided_account':return ''
    for row in rows:
        label=' '.join(str(row.get(k,'')) for k in ('plan','duration','billing_period'))
        if re.search(r'на\s+год|годов|\byear(?:ly)?\b|\bannual(?:ly)?\b|\b12\s*(?:month|месяц)',label,re.I):
            return 'Владелец подтвердил только новый аккаунт на месяц; выбранный товар содержит другой срок. Нельзя подменять годовой товар месячным.'
    return ''


def route_evidence(service,rows):
    """Distinguish a missing projection from an explicitly different route."""
    selected=[row for row in rows if service in {
        policy['service'] for policy in policies_for({'requested_catalog':[row]})}]
    methods={str(row.get('fulfillment','not_verified')) for row in selected}
    explicit=methods & {'manual','checkout_link','account_details','code_activation_conditional'}
    policies=policies_for({'requested_catalog':selected})
    policy=next((p for p in policies if p['service']==service),{})
    if policy_scope_issue(policy,selected):return 'conflict',methods
    compatible=set(policy.get('compatible_fulfillment',['account_details']))
    if explicit-compatible:return 'conflict',methods
    if not selected or not methods<=compatible:return 'unavailable',methods
    return 'matched',methods
