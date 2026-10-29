"""Pure, customer-safe route projection. No order creation or supplier calls."""
METHODS=frozenset({'manual','checkout_link','account_details','code_activation_conditional'})


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
