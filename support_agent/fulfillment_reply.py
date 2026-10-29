"""Customer workflow explanations from verified shop fields, without model guesses."""
import re


def workflow_reply(facts,latest):
    lines=[line.strip() for line in latest.splitlines() if line.strip()]
    questions=[line for line in lines if re.search(r'оформ|кроме оплаты|что.*прислать|какие.*данные',line,re.I)]
    if not questions:return None
    if re.search(r'функци|лимит|входит|отлич|возврат|ошиб|не работает|закуп|поставщик|себестоим|18\s*\+|порно|эротическ',latest,re.I):return None
    from .catalog_context import _words,_families
    from .workflow_policy import policies_for
    rows=facts.get('requested_catalog',[])
    if not rows:return None
    groups={}
    for row in rows:groups.setdefault(row.get('service',''),[]).append(row)
    if not all(groups):return None
    krea=bool(policies_for(facts));parts=[];diagnostics=[];handoff=False
    for name,items in groups.items():
        if 'appstore' in _families(_words(name)):return None
        if re.search(r'тариф|цен|стоим',latest,re.I):
            prices=[]
            for item in items:
                price=item.get('catalog_price_rub')
                if type(price) is int and price>0:prices.append(f"{item.get('plan','')}: {price} ₽")
            if prices:parts.append(name+'\n'+'\n'.join(dict.fromkeys(prices)))
        if krea and 'krea' in _families(_words(name)):
            parts.append(name+': выберите тариф и оплатите заказ. После оплаты пришлите сюда, в эту переписку, почту и пароль от аккаунта.')
            handoff=True
            continue
        methods={item.get('fulfillment','not_verified') for item in items}
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
            parts.append(name+': оформление по платёжной ссылке сервиса. Выберите тариф в магазине; дальше потребуется ссылка для оплаты.')
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
    return {'reply':'\n\n'.join(parts),'summary':'Объяснение оформления по данным магазина; сроки, отсутствие входа и успех не обещаны.',
            'handoff':handoff,'source_ids':[],
            'evidence':{'status':'catalog_review' if diagnostics else 'catalog_direct','sources':[],
                        'diagnostic':'Этап: проверка инструкции оформления. '+'; '.join(diagnostics) if diagnostics else ''}}
