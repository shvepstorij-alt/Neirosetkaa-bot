"""Payment steps from the shop bot, and the owner's after-payment rules.

The card is the text we send when the client asks how to pay. Activation
secrets are never the payment method. The button that tells the client to
write Alexander about a finished payment is not part of the instruction.
"""
import re
from pathlib import Path

import yaml

from .new_account_guide import _new_plus_plan


_PATH = Path(__file__).with_name('data') / 'payment_guide.yaml'
_AUTO_PLANS = {'plus', 'go'}
_SETUP = (
    ('setup_email', 'почту, которую поставить на новый ящик'),
    ('setup_password', 'пароль для этого ящика'),
    ('setup_recovery', 'вашу настоящую почту для восстановления'),
)


def load_guide():
    raw = yaml.safe_load(_PATH.read_text(encoding='utf-8')) or {}
    return raw if isinstance(raw, dict) else {}


def pending_todos():
    guide = load_guide()
    return [str(item).strip() for item in guide.get('todos') or [] if str(item).strip()]


def for_context(facts):
    """Compact card for the drafting call."""
    guide = load_guide()
    nav = (facts or {}).get('bot_navigation') if isinstance(facts, dict) else {}
    nav = nav if isinstance(nav, dict) else {}
    entry = guide.get('entry') if isinstance(guide.get('entry'), dict) else {}
    webapp = entry.get('webapp') if isinstance(entry.get('webapp'), dict) else {}
    chat = entry.get('chat') if isinstance(entry.get('chat'), dict) else {}
    modes = guide.get('modes') if isinstance(guide.get('modes'), dict) else {}
    return {
        'username': nav.get('username') if nav.get('status') == 'available' else '',
        'catalog_webapp': bool(nav.get('catalog_webapp')),
        'menu_button': webapp.get('menu_button') if nav.get('catalog_webapp') else chat.get('menu_button'),
        'pay_buttons': list(webapp.get('pay_buttons') or []) if nav.get('catalog_webapp') else list(chat.get('pay_buttons') or []),
        'timing': guide.get('timing') or {},
        'modes': {key: {'services': list((row or {}).get('services') or []),
                        'timing': (row or {}).get('timing') or ''}
                  for key, row in modes.items() if isinstance(row, dict)},
        'note': ('Токен, Organization ID и Perplexity User ID нужны после оплаты. '
                 'Кнопку «Я оплатил - написать Александру» не называть.'),
    }


def _plan_key(name):
    import re
    return re.sub(r'[^a-z0-9]+', '', str(name or '').casefold())


def _mode(guide, key):
    modes = guide.get('modes') if isinstance(guide.get('modes'), dict) else {}
    row = modes.get(key)
    return row if isinstance(row, dict) else {}


def _service_ids(guide, key):
    return {str(item) for item in (_mode(guide, key).get('services') or []) if item}


def claude_is_maximum(plan_name, tariffs):
    """The owner's manual Claude tariff is the highest-priced one in the snapshot."""
    rows = [row for row in tariffs or []
            if row.get('service_id') == 'claude' and type(row.get('price')) is int]
    if not rows or not str(plan_name or '').strip():
        return False
    top = max(row['price'] for row in rows)
    key = _plan_key(plan_name)
    return any(row['price'] == top and _plan_key(row.get('plan')) == key for row in rows)


def _named(guide, key, service_name):
    name = str(service_name or '').casefold()
    return bool(name) and any(str(item).casefold() in name for item in (_mode(guide, key).get('names') or []) if item)


def kind_for(service_id, plan_name, tariffs=None, service_name=''):
    guide = load_guide()
    service_id = str(service_id or '')
    plan_name = str(plan_name or '')
    if not service_name:
        row = next((r for r in tariffs or [] if r.get('service_id') == service_id), None)
        service_name = (row or {}).get('service') or ''
    if 'кредит' in str(service_name).casefold() or 'кредит' in service_id.casefold() or 'credit' in service_id.casefold():
        return 'chatgpt_credits'
    if service_id in _service_ids(guide, 'appstore') or service_id == 'appstore':
        return 'appstore'
    if service_id in _service_ids(guide, 'link_pay'):
        return 'link_pay'
    if service_id in _service_ids(guide, 'zai'):
        return 'zai'
    if service_id in _service_ids(guide, 'suno'):
        return 'suno'
    if service_id in _service_ids(guide, 'telegram_premium'):
        return 'telegram_premium'
    if service_id in _service_ids(guide, 'capcut'):
        return 'capcut'
    if service_id in _service_ids(guide, 'credentials_email_code') or _named(guide, 'credentials_email_code', service_name):
        return 'credentials_email_code'
    if service_id in _service_ids(guide, 'credentials') or _named(guide, 'credentials', service_name):
        return 'credentials'
    if service_id in _service_ids(guide, 'email_only') or _named(guide, 'email_only', service_name):
        return 'email_only'
    if service_id == 'chatgpt' and _new_plus_plan(plan_name):
        return 'chatgpt_new_account'
    if service_id == 'chatgpt' and (_plan_key(plan_name) in _AUTO_PLANS
                                    or re.match(r'\s*(?:plus|go)\b', plan_name.casefold())):
        return 'chatgpt_auto'
    if service_id == 'chatgpt' and plan_name:
        return 'chatgpt_manual'
    if service_id == 'claude' and claude_is_maximum(plan_name, tariffs):
        return 'claude_manual'
    if service_id == 'claude':
        return 'claude_auto'
    if service_id == 'perplexity':
        return 'perplexity'
    return 'shop'


def merged_setup(incoming, collected):
    found = {}
    incoming = incoming or {}
    collected = collected or {}
    for key, _label in _SETUP:
        value = ' '.join(str(incoming.get(key) or collected.get(key) or '').split())
        if value and len(value) <= 200:
            found[key] = value
    return found


def missing_setup(found):
    return [key for key, _label in _SETUP if not (found or {}).get(key)]


def setup_ready(found):
    return bool(found) and not missing_setup(found)


def setup_ask(missing):
    labels = [label for key, label in _SETUP if key in (missing or [])]
    if not labels:
        return ''
    if len(labels) == 1:
        body = labels[0]
    else:
        body = ', '.join(labels[:-1]) + ' и ' + labels[-1]
    return ('Пришлите сюда ' + body + '.\n\n'
            'Кнопки «Активировать» у этого тарифа нет. Когда всё будет, оформит Александр.')


def _sentence(text):
    value = ' '.join(str(text or '').split())
    if not value:
        return ''
    return value[:1].upper() + value[1:]


def _timing_line(guide, kind, already):
    row = _mode(guide, kind)
    slot = str(row.get('timing') or '')
    if not slot:
        return ''
    advisory = 'activation_auto' if slot == 'auto' else 'activation_shop'
    if advisory in (already or []):
        return ''
    timing = guide.get('timing') if isinstance(guide.get('timing'), dict) else {}
    return str(timing.get(slot) or '').strip()


def _after(guide, kind, choice, have, service_name=''):
    row = _mode(guide, kind)
    if kind in ('credentials', 'credentials_email_code', 'email_only'):
        return _sentence(str(row.get('after') or '').replace('{name}', str(service_name or 'сервиса')))
    if kind == 'appstore':
        apple = guide.get('appstore') if isinstance(guide.get('appstore'), dict) else {}
        text = str(apple.get('after') or '').strip()
        return 'После оплаты: ' + text[:1].lower() + text[1:]
    if kind == 'zai':
        if choice == 'link':
            return _sentence(row.get('link_after'))
        if choice == 'email':
            return _sentence(row.get('email_after'))
        return _sentence(row.get('choose'))
    if kind == 'chatgpt_auto':
        return ('После оплаты нажмите «' + str(row.get('button') or 'Активировать подписку') + '».\n'
                'Туда вставляется весь текст со страницы ' + str(row.get('page') or 'chatgpt.com/api/auth/session') + '.\n'
                'Это активация, не способ оплаты. ' + _sentence(row.get('where')))
    if kind == 'chatgpt_new_account':
        parts = [_sentence(row.get('after'))]
        ask = setup_ask(missing_setup(have))
        if ask:
            parts.append(ask)
        return '\n\n'.join(part for part in parts if part)
    if kind in ('claude_auto', 'perplexity'):
        return ('После оплаты нажмите «' + str(row.get('button') or '') + '» и вставьте '
                + str(row.get('client_provides') or '') + ' (' + str(row.get('page') or '') + ').\n'
                + _sentence(row.get('where')))
    return _sentence(row.get('after'))


def render(service_id, service_name, plan_name, facts, already=(), tariffs=None, choice='', have=None):
    """One payment instruction. Z.ai asks for the method before the shop steps."""
    guide = load_guide()
    kind = kind_for(service_id, plan_name, tariffs, service_name)
    if kind == 'zai' and choice not in ('email', 'link'):
        return _after(guide, kind, '', have)
    nav = (facts or {}).get('bot_navigation') if isinstance(facts, dict) else {}
    nav = nav if isinstance(nav, dict) else {}
    username = str(nav.get('username') or '') if nav.get('status') == 'available' else ''
    webapp = bool(nav.get('catalog_webapp'))
    entry = guide.get('entry') or {}
    path = (entry.get('webapp') if webapp else entry.get('chat')) or {}
    where = ('@' + username) if username else 'боте магазина'
    menu = str(path.get('menu_button') or ('Каталог' if webapp else 'Магазин'))
    choice_name = ' '.join(part for part in (service_name, plan_name) if part).strip() or 'нужный сервис и тариф'
    if kind == 'chatgpt_credits':
        choice_name = '«' + (service_name or 'Пополнение кредитов ChatGPT') + '»' + (', затем ' + plan_name if plan_name else '')
    pay = ' или '.join('«' + str(item) + '»' for item in (path.get('pay_buttons') or []) if item)
    apple = guide.get('appstore') if isinstance(guide.get('appstore'), dict) else {}
    if kind == 'appstore':
        steps = [
            'Откройте бота и нажмите «' + menu + '».',
            'Откройте «' + str(apple.get('menu_name') or 'App Store / iCloud') + '».',
            'Выберите регион Apple ID, затем номинал.',
        ]
    else:
        steps = [
            'Откройте бота и нажмите «' + menu + '».',
            'Выберите ' + choice_name + '.',
        ]
    if pay:
        coin = str(path.get('coins_note') or '').strip()
        steps.append('Способ оплаты: ' + pay + '.' + ((' ' + coin) if coin else ''))
    if webapp:
        steps.append('Нажмите «' + str(path.get('go_button') or 'Перейти к оплате') + '». '
                     'Если статус не обновился, нажмите «' + str(path.get('check_button') or 'Проверить оплату') + '».')
    elif kind == 'appstore':
        steps.append('Нажмите «' + str(apple.get('pay_button') or 'Оплатить') + '». '
                     'Если статус не обновился, нажмите «' + str(apple.get('check_button') or 'Проверить оплату') + '».')
    else:
        steps.append('На экране подтверждения нажмите «' + str(path.get('confirm_button') or 'Оплатить') + '», '
                     'затем выбранный способ. Номер заказа бот покажет на этом экране.')
    lines = ['Оплата в ' + where + '.', '']
    lines.extend(str(index) + '. ' + step for index, step in enumerate(steps, 1))
    lines.append('')
    lines.append(_after(guide, kind, choice, have, service_name))
    timing = _timing_line(guide, kind, already)
    if timing:
        lines.append('')
        lines.append(timing)
    return '\n'.join(line for line in lines if line is not None).strip()


def mixes_activation_into_payment(reply):
    """A payment sentence that tells the client to pay with an activation secret."""
    import re
    for part in re.split(r'(?<=[.!?])\s+|\n+', str(reply or '')):
        low = part.casefold().replace('ё', 'е')
        if 'оплат' not in low:
            continue
        if 'после оплат' in low:
            continue
        if any(token in low for token in ('токен', 'organization id', 'user id')):
            return True
    return False
