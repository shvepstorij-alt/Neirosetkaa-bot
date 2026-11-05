"""Deterministic consultation about authenticated customer's own shop orders.

No provider requests, receipt approval, payment mutation or fulfilment here.
Recent-order samples are never evidence that an arbitrary order is absent.
"""
import re
from decimal import Decimal, InvalidOperation


def is_status_request(question):
    text=str(question or '').casefold().replace('ё','е')
    explicit=r'статус\w*\s+(?:мо\w*\s+)?(?:заказ\w*|оплат\w*|платеж\w*)|(?:провер\w*|найд\w*|посмотр\w*)[^.!?\n]{0,35}(?:заказ\w*|оплат\w*|платеж\w*)|(?:прош\w*\s+ли|дош\w*\s+ли|подтверд\w*\s+ли)[^.!?\n]{0,25}(?:оплат\w*|платеж\w*|деньг\w*)|(?:оплат\w*|платеж\w*|заказ\w*)[^.!?\n]{0,25}(?:не\s+(?:вид\w*|подтверд\w*|прош\w*|зачисл\w*)|pending)'
    claim=bool(re.search(r'\b(?:я\s+|уже\s+)?оплатил(?:а)?\b|деньги\s+списал\w*|средства\s+списал\w*',text))
    return bool(re.search(explicit,text)) or claim


def requested_reference(question, context=''):
    """Public order numbers only; ambiguous bare IDs stay ambiguous by design."""
    text=str(question or '')
    matches=[]
    patterns=[('fk_intid',r'(?:free\s*kassa|фри\s*касс\w*|freekassa|\bintid\b|\bfk\b)\s*(?:номер\s*)?[:#№\s-]*(\d{1,18})\b'),
              ('shop_num',r'заказ\w*\s*[#№]\s*(\d{1,18})\b'),
              ('ambiguous',r'(?:номер\s+заказа|номер\s+платежа)\s*[:#№\s-]*(\d{1,18})\b')]
    spans=[]
    for kind,pattern in patterns:
        for match in re.finditer(pattern,text,re.I):
            value=str(int(match.group(1)))
            if value=='0':continue
            if any(a<=match.start()<b for a,b in spans):continue
            matches.append({'kind':kind,'value':value});spans.append(match.span())
    for match in re.finditer(r'[#№]\s*(\d{1,18})\b',text):
        if any(a<=match.start()<b for a,b in spans):continue
        value=str(int(match.group(1)))
        if value!='0':matches.append({'kind':'shop_num' if text[match.start()]=='#' else 'ambiguous','value':value})
    if not matches and re.fullmatch(r'\s*\d{1,18}\s*',text) and is_status_request(context):
        value=str(int(text.strip()))
        if value!='0':matches.append({'kind':'ambiguous','value':value})
    unique=[]
    for item in matches:
        if item not in unique:unique.append(item)
    if len(unique)==2:
        numbered=[item for item in unique if item['kind']=='shop_num']
        other=[item for item in unique if item['kind'] in ('ambiguous','fk_intid')]
        if len(numbered)==1 and len(other)==1:
            return {'kind':'pair','num':numbered[0]['value'],
                    'fk_intid' if other[0]['kind']=='fk_intid' else 'untyped':other[0]['value']}
    if len(unique)>1:return {'kind':'multiple','count':len(unique)}
    return unique[0] if unique else None


def _answer(reply,reason,handoff=False):
    return {'reply':reply,'summary':'Статус оплаты проверяется отдельно от активации; чек не подтверждает зачисление автоматически.',
            'handoff':handoff,'research':{'topic':'none','products':[]},'source_ids':[],
            'evidence':{'status':'order_status','diagnostic':reason,
                        'handoff_reason':reason if handoff else ''}}


def _amount(value):
    if isinstance(value,bool):return None
    try:number=Decimal(str(value))
    except (InvalidOperation,ValueError,TypeError):return None
    if not number.is_finite() or number<0 or number>100_000_000:return None
    return format(number,'f').rstrip('0').rstrip('.') if number!=number.to_integral() else str(int(number))


def _matches_reference(row,reference):
    num=str(row.get('num',''));fk=str(row.get('fk_intid') or '')
    kind=reference.get('kind');value=reference.get('value')
    if kind=='pair':
        return (num==reference.get('num')
                and ('fk_intid' not in reference or fk==reference['fk_intid'])
                and ('untyped' not in reference or reference['untyped'] in (num,fk)))
    return (kind=='shop_num' and num==value or kind=='fk_intid' and fk==value
            or kind=='ambiguous' and value in (num,fk))


def status_reply(facts, question, reference=None):
    """Render only an exact owned lookup. Never search by supplied email/name.

    Root plumbing supplies `order_lookup` with ownership_scope set by its own
    authenticated-UID query. Raw model output or a recent-five sample is not
    accepted as exact-order evidence.
    """
    if not is_status_request(question) and reference is None:return None
    reference=reference or requested_reference(question)
    if not reference:
        return _answer('Пришлите номер заказа из сообщения бота или номер платежа FreeKassa. Проверю оплату именно вашего заказа)',
                       'Этап: выбор заказа для проверки оплаты. Номер заказа или FreeKassa не указан. Последние заказы не выбраны вместо него.')
    if reference.get('kind')=='multiple':
        return _answer('Пришлите, пожалуйста, один номер заказа или платежа FreeKassa, который нужно проверить.',
                       'Этап: выбор заказа. Получено несколько номеров='+str(reference.get('count',0))+'. Проверка разных заказов не смешивалась.')
    lookup=facts.get('order_lookup',{})
    if not isinstance(lookup,dict):lookup={}
    state=lookup.get('status','not_connected')
    own=lookup.get('ownership_scope')=='authenticated_user'
    bound=lookup.get('reference')==reference
    if not own or not bound:
        return _answer('Сейчас не получилось проверить оплату этого заказа. Номер получил, потребуется ручная проверка.',
                       'Этап: проверка доступности данных заказа. Точный авторизованный поиск не подтвержден; ownership_scope='+('authenticated_user' if own else 'not_confirmed')+'; привязка номера='+('совпадает' if bound else 'не подтверждена')+'. Последняя выборка заказов не доказывает отсутствие заказа.',True)
    if state=='not_found':
        return _answer('По этому номеру не удалось найти заказ среди покупок вашего Telegram-аккаунта. Проверьте номер и пришлите сообщение о заказе из бота. Если покупали с другого аккаунта, напишите с него.',
                       'Этап: точный поиск собственного заказа. Статус=not_found; авторизованное совпадение=0. Существование заказа у других клиентов не проверялось и не раскрывается.',True)
    if state!='available':
        safe=state if state in ('read_error','not_connected','timeout','ambiguous','invalid_response') else 'unknown'
        detail=lookup.get('diagnostic','')
        from .privacy import scrub
        detail=scrub(detail)[:500] if isinstance(detail,str) else ''
        return _answer('Сейчас не получилось надежно проверить оплату заказа. Проверю его вручную; неподтвержденный статус называть не буду.',
                       'Этап: чтение собственного заказа. Статус источника='+safe+'. Зачисление и активация не подтверждены. '+detail,True)
    rows=lookup.get('orders')
    if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict):
        return _answer('Не получилось однозначно сопоставить номер с заказом. Проверю вручную.',
                       'Этап: однозначность собственного заказа. Требуется ровно одна строка; получено='+str(len(rows) if isinstance(rows,list) else 'invalid')+'.',True)
    row=rows[0]
    # Validate the query contract again before rendering any order facts.
    num=str(row.get('num',''));fk=str(row.get('fk_intid') or '')
    matches=_matches_reference(row,reference)
    if not matches:
        return _answer('Не получилось сопоставить ответ проверки с указанным номером. Проверю вручную.',
                       'Этап: привязка результата к номеру заказа. Строка авторизованного поиска не соответствует запрошенному номеру.',True)
    label='Заказ №'+num if re.fullmatch(r'\d{1,18}',num) else 'Ваш заказ'
    amount=_amount(row.get('amount_rub'))
    amount_text=(' на '+amount+' ₽') if amount is not None else ''
    status=str(row.get('status','')).casefold()
    if status=='paid':
        return _answer(label+amount_text+': оплата отмечена в магазине как полученная. Это еще не подтверждение активации или выдачи подписки.',
                       'Этап: чтение статуса собственного заказа. status=paid; источник=авторизованное чтение магазина. Активация, выдача и прямой запрос к платежному провайдеру этим не проверены.')
    if status=='pending':
        return _answer(label+amount_text+': магазин пока ожидает подтверждение оплаты. Если деньги уже списались, пришлите скриншот банковского чека, скрыв номер карты, счет и другие лишние личные данные. Чек поможет разобраться, но сам по себе не подтверждает зачисление платежа в магазине. Повторно оплачивать сейчас не нужно.',
                       'Этап: чтение статуса собственного заказа. status=pending; источник=авторизованное чтение магазина. Чек не проверяет зачисление и не меняет статус.',True)
    if status in ('cancelled','canceled','expired','failed'):
        return _answer(label+': заказ отмечен как отмененный, истекший или неуспешный. Если деньги списались, пришлите банковский чек со скрытыми платежными данными, проверю ситуацию. Возврат или повторную оплату заранее не обещаю.',
                       'Этап: чтение статуса собственного заказа. status='+status+'; получение денег и основания возврата отдельно не подтверждены.',True)
    return _answer('У заказа получен статус, по которому нельзя надежно подтвердить оплату. Проверю вручную.',
                   'Этап: интерпретация статуса собственного заказа. Поддерживаемый статус оплаты не получен; первопричина неизвестна. Необработанное значение скрыто.',True)


async def order_reply(shop,uid,current,*,facts=None,context=''):
    """Read-only exact lookup adapter for ShopReader.order_facts.

    A foreign number produces the same owned-order not-found result; this
    module never requests unscoped existence, contact email or account data.
    `order_context` is a minimal trusted signal for root's mixed-goal composer.
    """
    reference=requested_reference(current,context)
    if not is_status_request(current) and not (reference and is_status_request(context)):
        return None
    if not reference or reference.get('kind')=='multiple':
        return status_reply({},current,reference)
    if reference['kind']=='pair':
        query={k:int(v) if k=='num' else v for k,v in reference.items() if k!='kind'}
    else:
        key={'shop_num':'num','fk_intid':'fk_intid','ambiguous':'untyped'}[reference['kind']]
        query={key:int(reference['value']) if key=='num' else reference['value']}
    try:
        lookup=await shop.order_facts(uid,query)
    except Exception:
        # No provider bodies or DSN errors are returned to customers/admins.
        lookup={'orders_status':'read_error','ownership_scope':'authenticated_user','orders':[]}
    if not isinstance(lookup,dict):lookup={}
    normalized={'order_lookup':{'status':lookup.get('orders_status','invalid_response'),
                'ownership_scope':lookup.get('ownership_scope'),
                'reference':reference,'orders':lookup.get('orders'),'diagnostic':lookup.get('orders_diagnostic','')}}
    result=status_reply(normalized,current,reference)
    rows=lookup.get('orders')
    if (result and lookup.get('ownership_scope')=='authenticated_user'
            and lookup.get('orders_status')=='available'
            and isinstance(rows,list) and len(rows)==1 and isinstance(rows[0],dict)):
        candidate=rows[0]
        own_match=_matches_reference(candidate,reference)
        if own_match:
            state=str(candidate.get('status','')).casefold()
            if state in ('paid','pending','cancelled','canceled','expired','failed'):
                result['order_context']={'status':state,'ownership_scope':'authenticated_user',
                                         'activation_status':'not_checked','source':'exact_shop_order_lookup'}
    return result
