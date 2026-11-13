"""«Приветствую! Оплатил заказ #10502 / Сервис / Тариф / Номер FreeKassa / ID» —
the message the shop bot prepares for the client after payment (owner 13.11.2026).

The assistant answers with the next step for that service and tariff: send e-mail and
password, send the payment link, press the activation button, or wait for the account.
It never says the payment is confirmed on the client's word: the order is looked up in
the shop for this Telegram user, and an order that is not paid goes to Alexander.
"""
import re

_HEAD = re.compile(r'(?i)оплатил\w*\s+заказ\s*(#?\s*[\w-]{1,40})')
_FIELD = {
    'service': re.compile(r'(?im)^\s*Сервис:\s*(.{1,80})$'),
    'plan': re.compile(r'(?im)^\s*Тариф:\s*(.{0,80})$'),
    'fk': re.compile(r'(?im)^\s*Номер\s+(?:FreeKassa|заказа):\s*(\d{3,18})\s*$'),
}


def parse(text):
    """The fields of the shop's paid-order message, or None for any other text."""
    value = str(text or '')
    head = _HEAD.search(value)
    service = _FIELD['service'].search(value)
    if not head or not service:
        return None
    number = re.sub(r'\D', '', head.group(1)) if '#' in head.group(1) or head.group(1).strip().isdigit() else ''
    plan = _FIELD['plan'].search(value)
    fk = _FIELD['fk'].search(value)
    return {'order': number, 'service': ' '.join(service.group(1).split()),
            'plan': ' '.join(plan.group(1).split()) if plan else '',
            'fk': fk.group(1) if fk else ''}


def _flat(value):
    return re.sub(r'[^a-zа-яё0-9]+', '', str(value or '').casefold())


def match_row(found, tariffs):
    """The catalog row for the service and tariff named in the message."""
    name, plan = _flat(found.get('service')), _flat(found.get('plan'))
    rows = [row for row in tariffs or [] if _flat(row.get('service')) == name]
    if not rows:
        rows = [row for row in tariffs or [] if name and (name in _flat(row.get('service')) or _flat(row.get('service')) in name)]
    exact = [row for row in rows if _flat(row.get('plan')) == plan]
    return (exact or rows or [None])[0], bool(exact)


def next_step(found, tariffs):
    """What the client does now, in the owner's wording from the payment guide."""
    from .payment_guide import _after, kind_for, load_guide
    row, _exact = match_row(found, tariffs)
    service_id = str((row or {}).get('service_id') or '')
    service_name = str((row or {}).get('service') or found.get('service') or '')
    kind = kind_for(service_id, found.get('plan') or '', tariffs or [], service_name)
    text = _after(load_guide(), kind, '', None, service_name)
    text = re.sub(r'^После оплаты в магазине\s+', '', text)
    text = re.sub(r'^После оплаты:?\s+', '', text)
    text = re.sub(r'(?m)^Дальше активация идёт в боте магазина\.$',
                  'Александр оформит подписку и напишет здесь, если понадобятся данные.', text)
    return kind, text[:1].upper() + text[1:]


_SELF = {'chatgpt_auto', 'claude_auto', 'perplexity', 'appstore'}


def compose(found, tariffs):
    kind, step = next_step(found, tariffs)
    label = found['service'] + (', тариф ' + found['plan'] if found.get('plan') else '')
    head = 'Здравствуйте! Заказ ' + ('#' + found['order'] + ' ' if found.get('order') else '') + 'вижу: ' + label + '.'
    return kind, head + '\n\n' + step


def owner_note(found, kind, status):
    wait = 'клиент активирует сам по кнопке в боте' if kind in _SELF else 'нужны ваши действия после ответа клиента'
    lines = ['🧾 Клиент сообщил об оплате.',
             'Заказ: ' + ('#' + found['order'] if found.get('order') else 'номер не указан'),
             'Сервис: ' + found['service'] + (' · ' + found['plan'] if found.get('plan') else '')]
    if found.get('fk'):
        lines.append('FreeKassa: ' + found['fk'])
    lines.append('Статус в базе магазина: ' + status)
    lines.append('Помощник написал следующий шаг; ' + wait + '.')
    return '\n'.join(lines)
