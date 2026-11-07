"""Safe order projection: payment, owned service label, activation diagnostic.

No codes, sessions, passwords, payment links or provider claims are produced.
An unreadable activation source is not a payment result.
"""
import re


_KEY=re.compile(r'[a-z0-9_]{1,40}')
_PLAN=re.compile(r'[\w .+\-]{1,80}', re.UNICODE)
_STATES={
    'not_checked','unavailable','not_applicable','pending_window','in_progress',
    'code_consumed','fulfilled_recorded','manual_pending',
}
_PAID_NOTE={
    'pending_window':'В боте ещё открыто окно активации. Это не подтверждение, что подписка уже включилась.',
    'in_progress':'Активация уже запускалась. Результат у поставщика этим не подтверждён.',
    'code_consumed':'По заказу отмечено использование кода. Включение подписки у поставщика отдельно не проверялось.',
    'fulfilled_recorded':'В магазине есть отметка выдачи. Включение подписки у поставщика отдельно не проверялось.',
    'manual_pending':'Заказ в ручном оформлении. Активация у поставщика не подтверждена.',
    'not_checked':'Локальной отметки активации нет. Это не подтверждение, что подписка не включена.',
    'unavailable':'Состояние активации сейчас проверить не удалось. Это не значит, что оплата не прошла.',
}


def parse_shop_pack(pack):
    """Read shop:key:index[:plan] stored by the shop. Unknown shapes stay empty."""
    parts=str(pack or '').split(':')
    if len(parts)<3 or parts[0]!='shop' or not _KEY.fullmatch(parts[1]):
        return {}
    result={'service_key':parts[1]}
    if parts[2].isdigit():
        result['plan_index']=int(parts[2])
    plan=parts[3].strip() if len(parts)>3 else ''
    plan=re.sub(r'\s+',' ',plan)[:80]
    if plan and _PLAN.fullmatch(plan):
        result['plan_name']=plan
    return result


def activation_from_signals(payment_status, signals):
    """Map local flags to a diagnostic. Never treat a missing source as unpaid."""
    signals=signals if isinstance(signals,dict) else {}
    payment=str(payment_status or '').casefold()
    if payment!='paid':
        status='not_applicable'
    elif signals.get('lookup')=='unavailable':
        status='unavailable'
    elif signals.get('activation_started'):
        status='in_progress'
    elif signals.get('pending_open'):
        status='pending_window'
    elif signals.get('code_used'):
        status='code_consumed'
    elif str(signals.get('linkpay_status') or '') or str(signals.get('nsgifts_status') or '')=='pending':
        status='manual_pending'
    elif signals.get('fulfilled') or str(signals.get('nsgifts_status') or '') in ('success','delivered','done'):
        status='fulfilled_recorded'
    else:
        status='not_checked'
    if status not in _STATES:
        status='unavailable'
    return {'activation_status':status,
            'activation_note':_PAID_NOTE.get(status,'') if payment=='paid' else ''}


def project_order(row, signals):
    """Payment columns plus a service label and activation diagnostic. No secrets."""
    data=dict(row)
    parsed=parse_shop_pack(data.get('pack'))
    merged=dict(signals or {})
    if 'fulfilled' not in merged:
        merged['fulfilled']=bool(data.get('fulfilled_at'))
    activation=activation_from_signals(data.get('status'), merged)
    projected={key:data.get(key) for key in
               ('num','fk_intid','status','amount_rub','coins_spent','paid_at','last_checked_at')}
    projected.update(parsed)
    projected.update(activation)
    return projected


def service_phrase(row):
    key=str(row.get('service_key') or '')
    if not _KEY.fullmatch(key):
        return ''
    phrase='Товар в заказе: '+key
    plan=str(row.get('plan_name') or '')
    if plan and _PLAN.fullmatch(plan):
        phrase+=', тариф '+plan
    return phrase+'. '


def activation_phrase(row):
    note=row.get('activation_note')
    if not isinstance(note,str) or not note:
        return ''
    if re.search(r'не\s+оплачен', note, re.I):
        return ''
    return note+' '
