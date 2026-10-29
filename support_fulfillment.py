"""Pure, customer-safe route projection. No order creation or supplier calls."""
import html
import re
METHODS=frozenset({'manual','checkout_link','account_details','code_activation_conditional'})
REQUIRED_FIELDS=frozenset({'account_email','account_password'})
LINKPAY_STEPS=('open_service','login_own_account','open_plans','select_plan',
               'open_checkout','copy_checkout_url','send_checkout_url')


def project(catalog, flags, plan_key, auto_plans=('plus','go')):
    result={}
    for key,service in catalog.items():
        for index,plan in enumerate(service.get('plans') or []):
            if not isinstance(plan,dict):continue
            name=str(plan.get('name') or '')
            # Production manual selection resolves duplicate names to the first.
            first=next(i for i,p in enumerate(service['plans'])
                       if isinstance(p,dict) and str(p.get('name') or '').strip()==name.strip())
            kind={'chatgpt':'chatgpt','gpt':'chatgpt','claude':'claude','perplexity':'perplexity'}.get(str(key).strip().lower())
            keys=('manual:'+key+':'+str(first),'linkpay:enabled:'+key,'creds:enabled:'+key)
            if any(flags.get(k,'0') not in ('0','1') for k in keys):continue
            if (flags.get(keys[0])=='1' or 'week' in name.lower() or 'недел' in name.lower()
                    or kind=='chatgpt' and plan_key(name) not in auto_plans):
                method='manual'
            elif kind:method='code_activation_conditional'
            elif flags.get(keys[1])=='1':method='checkout_link'
            elif flags.get(keys[2])=='1':method='account_details'
            else:method='manual'
            result[(key,index)]=method
    return result


def _instruction_identity(value):
    return re.sub(r'\s+',' ',html.unescape(re.sub(r'<[^>]*>','',str(value or '')))).strip()


def _custom_creds_fields(text):
    if not isinstance(text,str) or not 0<len(text)<=2000:return False
    plain=_instruction_identity(text)
    if re.search(r'\b(?:не|нет|без|никогда|если|либо|или)\b',plain,re.I):return False
    if re.search(r'https?://|www\.|[\w.+-]+@[\w.-]+\.\w+|\d{5,}|[A-Za-z0-9_=-]{24,}',text,re.I):return False
    # Only an affirmative request of both fields in the same clause is evidence.
    # "Send email. Change password" must not become a password request.
    request=r'\b(?:пришлите|пришли|отправьте|отправь|предоставьте|передайте|укажите|нужны|потребуются|требуются|необходимо\s+предоставить|send|provide|required)\b'
    for clause in re.split(r'[.!?;\n]',plain):
        match=re.search(request,clause,re.I)
        if not match:continue
        fields=clause[match.end():]
        email=r'(?:email|e-mail|почт[ауы])';password=r'(?:пароль|password)'
        pair=rf'\b(?:{email}\s*(?:и|,|/|&)\s*{password}|{password}\s*(?:и|,|/|&)\s*{email})\b'
        if re.search(pair,fields,re.I):
            return True
    return False


def project_details(catalog, flags, plan_key, auto_plans=('plus','go'), *, default_creds_instruction,
                    default_linkpay_instruction=None):
    """Project only proven field identities, never raw instructions or credentials.

    The caller passes the same live default used by _send_creds_instructions.
    Arbitrary custom instructions are deliberately not interpreted using guesses,
    a model, or partial keyword matches (including negated password requests).
    """
    routes=project(catalog,flags,plan_key,auto_plans)
    default=_instruction_identity(default_creds_instruction)
    # Prove the expected runtime default before attaching its field identities.
    standard=('Для оформления подписки нужны email и пароль от твоего аккаунта сервиса. '
              'Нажми кнопку ниже и пришли их по очереди. После оформления рекомендуем сменить пароль.')
    link_default=_instruction_identity(default_linkpay_instruction)
    link_standard=('1. Зайди на сайт сервиса и авторизуйся в СВОЙ аккаунт. '
                   '2. Открой раздел тарифов / подписки. '
                   '3. Выбери нужный тариф (и регион, если требуется). '
                   '4. Дойди до страницы оплаты и СКОПИРУЙ ссылку на оплату. '
                   '5. Нажми кнопку ниже и пришли эту ссылку.')
    result={}
    for (key,index),method in routes.items():
        value={'method':method,'required_fields_status':'not_verified','required_fields':[],
               'workflow_steps_status':'not_verified','workflow_steps':[]}
        if method=='account_details':
            custom=flags.get('creds:instructions:'+key,'')
            default_applies=isinstance(custom,str) and (not custom or _instruction_identity(custom)==default)
            if ((default_applies and default==standard)
                    or (isinstance(custom,str) and custom and not default_applies and _custom_creds_fields(custom))):
                value.update(required_fields_status='available',
                             required_fields=['account_email','account_password'],
                             required_fields_source='live_shop_creds_instructions')
        if method=='checkout_link':
            custom=flags.get('linkpay:instructions:'+key,'')
            if isinstance(custom,str) and link_default==link_standard and (not custom or _instruction_identity(custom)==link_default):
                value.update(workflow_steps_status='available',workflow_steps=list(LINKPAY_STEPS),
                             workflow_steps_source='live_shop_linkpay_instructions')
        result[(key,index)]=value
    return result
