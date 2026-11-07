"""Payment follow-up after the model has called the turn a payment issue.

The lookup is always limited to this Telegram user. A receipt is forwarded,
never treated as proof that the shop was paid.
"""
import json
import logging
import re

from .handoff import handoff_text
from .order_activation import activation_phrase, service_phrase


def order_number(understanding):
    raw = str((understanding or {}).get('order_number') or '').strip()
    if re.fullmatch(r'\d{1,18}', raw) and raw != '0':
        return raw
    return ''


def receipt_amount_unverified(understanding):
    raw = str((understanding or {}).get('receipt_amount_unverified') or '').strip()
    if re.fullmatch(r'\d{1,7}', raw):
        return int(raw)
    return None


def receipt_files(db, chat):
    """File ids only. Captions and image bytes are not copied."""
    rows = db.execute(
        '''SELECT media FROM messages WHERE chat=? AND role='client'
           ORDER BY mid DESC LIMIT 10''', (chat,)).fetchall()
    found = []
    for row in rows:
        try:
            media = json.loads(row['media'] or '{}')
        except (TypeError, ValueError):
            continue
        if not isinstance(media, dict):
            continue
        kind = media.get('kind')
        file_id = str(media.get('file_id') or '')
        if kind in ('photo', 'document') and file_id and len(file_id) <= 128:
            found.append({'kind': kind, 'file_id': file_id})
    return found


def _summary(number, lookup, row, receipts, mismatch):
    status = (lookup or {}).get('orders_status') or 'unknown'
    pay = str((row or {}).get('status') or '')
    amount = (row or {}).get('amount_rub')
    amount_text = str(amount) if type(amount) is int else 'нет в ответе базы'
    lines = [
        'Проверка своего заказа.',
        'Номер: ' + number,
        'Статус чтения: ' + str(status),
        'Статус оплаты в базе: ' + (pay or 'нет строки'),
        'Сумма, ₽: ' + amount_text,
        'Чек: ' + ('есть, содержимое в лог не писалось' if receipts else 'в последних сообщениях нет'),
    ]
    if mismatch:
        lines.append('Сумма, которую модель увидела на чеке, не совпала с базой. Чек не подтверждает оплату.')
    return '\n'.join(lines)


async def notify_owner(service, job, number, lookup, row, receipts, mismatch):
    text = service.chat_identity(job['chat']) + '\n' + _summary(number, lookup, row, receipts, mismatch)
    try:
        await service.owner_message(text)
    except Exception:
        logging.warning('Owner payment note was not delivered')
    for item in receipts:
        method = 'sendPhoto' if item['kind'] == 'photo' else 'sendDocument'
        try:
            await service.tg.call(method, chat_id=service.owner, **{
                'photo' if item['kind'] == 'photo' else 'document': item['file_id']})
        except Exception:
            logging.warning('Owner receipt forward failed')


def _paid(row, lookup):
    return (lookup or {}).get('orders_status') == 'available' and isinstance(row, dict) and str(row.get('status') or '').casefold() == 'paid'


def next_step(row):
    """Documented client step for a paid order. A receipt is not an input."""
    key = str((row or {}).get('service_key') or '')
    plan = str((row or {}).get('plan_name') or '').casefold()
    if key == 'chatgpt' and 'аккаунт' in plan and 'нов' in plan:
        return ''
    if key == 'chatgpt':
        from .activation_guidance import _STEPS
        return ('Для обычной подписки ChatGPT токен сессии вставляется в окно активации в боте, не в эту переписку. '
                + _STEPS)
    note = (service_phrase(row or {}) + activation_phrase(row or {})).strip()
    if note:
        return note + ' Откройте сообщение этого заказа в боте и выполните указанный там шаг.'
    return 'Откройте сообщение этого заказа в боте и выполните указанный там шаг. Включение подписки этим сообщением не подтверждается.'


async def resolve_payment(service, job, understanding, context):
    """Return an answer when this turn is a payment issue. None leaves the draft as-is."""
    if (understanding or {}).get('intent') != 'payment_issue':
        return None
    number = order_number(understanding)
    receipts = receipt_files(service.store.db, job['chat'])
    if not number:
        return None
    try:
        lookup = await service.shop.order_facts(job['uid'], {'untyped': number})
    except Exception:
        lookup = {'orders_status': 'read_error', 'ownership_scope': 'authenticated_user', 'orders': []}
    if not isinstance(lookup, dict):
        lookup = {'orders_status': 'read_error', 'orders': []}
    orders = lookup.get('orders') if isinstance(lookup.get('orders'), list) else []
    row = orders[0] if lookup.get('orders_status') == 'available' and len(orders) == 1 and isinstance(orders[0], dict) else None
    claimed = receipt_amount_unverified(understanding)
    mismatch = type(claimed) is int and type((row or {}).get('amount_rub')) is int and claimed != row['amount_rub']
    paid = _paid(row, lookup) and not mismatch
    step = next_step(row) if paid else ''
    if not paid or receipts or not step:
        await notify_owner(service, job, number, lookup, row, receipts, mismatch)
    from .turn_understanding import package
    if not paid:
        answer = {'reply': handoff_text(),
                  'summary': _summary(number, lookup, row, receipts, mismatch),
                  'handoff': True, 'source_ids': [],
                  'evidence': {'status': 'owner_policy', 'sources': [], 'reply_class': 'outcome',
                               'diagnostic': _summary(number, lookup, row, receipts, mismatch),
                               'handoff_reason': 'Статус заказа не позволяет подтвердить оплату или чек расходится с базой.'}}
        return answer
    if not step:
        answer = {'reply': handoff_text(),
                  'summary': _summary(number, lookup, row, receipts, mismatch),
                  'handoff': True, 'source_ids': [],
                  'evidence': {'status': 'owner_policy', 'sources': [], 'reply_class': 'outcome',
                               'diagnostic': 'Для этого товара следующий шаг выполняет владелец. Чек оплату не подтверждает.',
                               'handoff_reason': 'Отдельный товар с новым аккаунтом оформляет владелец.'}}
        return answer
    facts = {'order_number': number, 'status': 'paid', 'amount_rub': row.get('amount_rub'),
             'service_key': row.get('service_key') or '', 'plan_name': row.get('plan_name') or '',
             'activation_status': row.get('activation_status') or '',
             'activation_note': (service_phrase(row) + activation_phrase(row)).strip(),
             'next_step': step,
             'receipt_missing': not receipts,
             'receipt_is_not_proof': True}
    draft = {}
    if hasattr(service.ai, 'draft_with_facts'):
        try:
            draft = await service.ai.draft_with_facts(facts)
        except Exception:
            draft = {}
    merged = dict(understanding)
    if isinstance(draft, dict) and draft.get('reply'):
        merged['reply'] = draft['reply']
        merged['summary'] = draft.get('summary') or merged.get('summary') or ''
        if draft.get('service_id'):
            merged['service_id'] = draft['service_id']
    else:
        merged['reply'] = step
    merged['intent'] = 'payment_issue'
    answer = package(merged, context, service.store.db, job['chat'])
    if not answer.get('handoff'):
        return answer
    # A paid lookup already happened. A draft that asserts activation is replaced
    # by the documented step; the receipt still does not prove payment.
    fallback = step if receipts else step + '\n\nПришлите также чек об оплате из банка: скриншот или PDF.'
    merged['reply'] = fallback
    merged['summary'] = 'Следующий шаг по оплаченному заказу. Чек оплату не подтверждает.'
    return package(merged, context, service.store.db, job['chat'])
