"""Payment steps taken from the shop bot, and what the client sends after payment.

The model may draft around this card. The card itself is the text we send when
the client asks how to pay, so a session token is never described as payment.
"""
from pathlib import Path

import yaml

from .faq import usable_answer
from .new_account_guide import _new_plus_plan


_PATH = Path(__file__).with_name('data') / 'payment_guide.yaml'
_AUTO = {'plus', 'go'}


def load_guide():
    raw = yaml.safe_load(_PATH.read_text(encoding='utf-8')) or {}
    return raw if isinstance(raw, dict) else {}


def pending_todos():
    guide = load_guide()
    return [str(item).strip() for item in guide.get('todos') or [] if str(item).strip()]


def for_context(facts):
    """Compact card for the drafting call. Todos stay out of the client text."""
    guide = load_guide()
    nav = (facts or {}).get('bot_navigation') if isinstance(facts, dict) else {}
    nav = nav if isinstance(nav, dict) else {}
    entry = guide.get('entry') if isinstance(guide.get('entry'), dict) else {}
    webapp = entry.get('webapp') if isinstance(entry.get('webapp'), dict) else {}
    chat = entry.get('chat') if isinstance(entry.get('chat'), dict) else {}
    return {
        'username': nav.get('username') if nav.get('status') == 'available' else '',
        'catalog_webapp': bool(nav.get('catalog_webapp')),
        'menu_button': webapp.get('menu_button') if nav.get('catalog_webapp') else chat.get('menu_button'),
        'pay_buttons': list(webapp.get('pay_buttons') or []) if nav.get('catalog_webapp') else list(chat.get('pay_buttons') or []),
        'after': guide.get('after') if isinstance(guide.get('after'), dict) else {},
        'appstore': guide.get('appstore') if isinstance(guide.get('appstore'), dict) else {},
        'note': 'Токен сессии, Organization ID и Perplexity User ID нужны после оплаты, это не способ оплаты.',
    }


def _plan_key(name):
    import re
    return re.sub(r'[^a-z0-9]+', '', str(name or '').casefold())


def _kind(service_id, plan_name):
    if service_id == 'appstore':
        return 'appstore'
    if service_id == 'chatgpt' and _new_plus_plan(plan_name or ''):
        return 'chatgpt_new_account'
    if service_id == 'chatgpt' and _plan_key(plan_name) in _AUTO:
        return 'chatgpt_auto'
    if service_id == 'chatgpt' and plan_name:
        return 'chatgpt_manual'
    if service_id == 'claude':
        return 'claude'
    if service_id == 'perplexity':
        return 'perplexity'
    return 'other'


def _delivery_line(already):
    if 'delivery_time' in (already or []):
        return ''
    text = usable_answer('delivery_time')
    if not text:
        return ''
    return text.replace('–', '-').replace('—', '-')


def render(service_id, service_name, plan_name, facts, already=()):
    """One payment instruction. Activation data is a separate thought."""
    guide = load_guide()
    nav = (facts or {}).get('bot_navigation') if isinstance(facts, dict) else {}
    nav = nav if isinstance(nav, dict) else {}
    username = str(nav.get('username') or '') if nav.get('status') == 'available' else ''
    webapp = bool(nav.get('catalog_webapp'))
    entry = (guide.get('entry') or {})
    path = (entry.get('webapp') if webapp else entry.get('chat')) or {}
    where = ('@' + username) if username else 'боте магазина'
    menu = str(path.get('menu_button') or ('Каталог' if webapp else 'Магазин'))
    choice = ' '.join(part for part in (service_name, plan_name) if part).strip() or 'нужный сервис и тариф'
    pay = ' или '.join('«' + str(item) + '»' for item in (path.get('pay_buttons') or []) if item)
    lines = ['Оплата в ' + where + '.', '']
    apple = guide.get('appstore') if isinstance(guide.get('appstore'), dict) else {}
    if service_id == 'appstore':
        steps = [
            'Откройте бота и нажмите «' + menu + '».',
            'Откройте «' + str(apple.get('menu_name') or 'App Store / iCloud') + '».',
            'Выберите регион Apple ID, затем номинал.',
        ]
    else:
        steps = [
            'Откройте бота и нажмите «' + menu + '».',
            'Выберите ' + choice + '.',
        ]
    if pay:
        coin = str(path.get('coins_note') or '').strip()
        steps.append('Способ оплаты: ' + pay + '.' + ((' ' + coin) if coin else ''))
    if webapp:
        steps.append('Нажмите «' + str(path.get('go_button') or 'Перейти к оплате') + '». '
                     'Если статус не обновился, нажмите «' + str(path.get('check_button') or 'Проверить оплату') + '».')
    elif service_id == 'appstore':
        steps.append('Нажмите «' + str(apple.get('pay_button') or 'Оплатить') + '». '
                     'Если статус не обновился, нажмите «' + str(apple.get('check_button') or 'Проверить оплату') + '».')
    else:
        steps.append('На экране подтверждения нажмите «' + str(path.get('confirm_button') or 'Оплатить') + '», '
                     'затем выбранный способ. Номер заказа бот покажет на этом экране.')
    for index, step in enumerate(steps, 1):
        lines.append(str(index) + '. ' + step)
    lines.append('')
    lines.append(_after_payment(_kind(service_id, plan_name), guide))
    delivery = _delivery_line(already) if service_id != 'appstore' else ''
    if delivery:
        lines.append('')
        lines.append(delivery)
    return '\n'.join(line for line in lines if line is not None).strip()


def _sentence(text):
    value = ' '.join(str(text or '').split())
    if not value:
        return ''
    return value[:1].upper() + value[1:]


def _after_payment(kind, guide):
    after = guide.get('after') if isinstance(guide.get('after'), dict) else {}
    apple = guide.get('appstore') if isinstance(guide.get('appstore'), dict) else {}
    if kind == 'appstore':
        return 'После оплаты. ' + _sentence(apple.get('after') or 'Код приходит в чат бота автоматически.')
    if kind == 'chatgpt_auto':
        row = after.get('chatgpt_auto') or {}
        return ('После оплаты нажмите «' + str(row.get('button') or 'Активировать подписку') + '».\n'
                'Туда вставляется ' + str(row.get('client_provides') or 'весь текст со страницы сессии')
                + ' (' + str(row.get('page') or 'chatgpt.com/api/auth/session') + ').\n'
                'Это активация, не способ оплаты. ' + _sentence(row.get('where')))
    if kind == 'chatgpt_new_account':
        row = after.get('chatgpt_new_account') or {}
        return 'После оплаты. ' + _sentence(row.get('client_provides'))
    if kind == 'chatgpt_manual':
        row = after.get('chatgpt_manual') or {}
        return ('После оплаты пришлите сюда ' + str(row.get('client_provides') or 'номер заказа и чек') + '.\n'
                'В боте для этого есть кнопка «' + str(row.get('button') or 'Написать Александру') + '».')
    if kind == 'claude':
        row = after.get('claude') or {}
        return ('После оплаты нажмите «' + str(row.get('button') or 'Активировать Claude') + '» '
                'и вставьте ' + str(row.get('client_provides') or 'Organization ID')
                + ' со страницы ' + str(row.get('page') or 'claude.ai/settings/account') + '.\n'
                + _sentence(row.get('where')))
    if kind == 'perplexity':
        row = after.get('perplexity') or {}
        return ('После оплаты нажмите «' + str(row.get('button') or 'Активировать Perplexity') + '» '
                'и вставьте ' + str(row.get('client_provides') or 'User ID')
                + ' (' + str(row.get('page') or 'perplexity.ai/api/auth/session') + ').\n'
                + _sentence(row.get('where')))
    row = after.get('other') or {}
    return 'После оплаты. ' + _sentence(row.get('client_provides') or 'Номер заказа будет в сообщении бота.')


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
