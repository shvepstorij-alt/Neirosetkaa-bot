"""Pure, customer-safe route projection. No order creation or supplier calls."""
import html
import re
METHODS=frozenset({'manual','checkout_link','account_details','code_activation_conditional'})
REQUIRED_FIELDS=frozenset({'account_email','account_password'})


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


def project_details(catalog, flags, plan_key, auto_plans=('plus','go'), *, default_creds_instruction):
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
    result={}
    for (key,index),method in routes.items():
        value={'method':method,'required_fields_status':'not_verified','required_fields':[]}
        if method=='account_details':
            custom=flags.get('creds:instructions:'+key,'')
            if isinstance(custom,str) and default==standard and (not custom or _instruction_identity(custom)==default):
                value.update(required_fields_status='available',
                             required_fields=['account_email','account_password'],
                             required_fields_source='live_shop_creds_instructions')
        result[(key,index)]=value
    return result
