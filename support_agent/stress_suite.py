"""Offline dialog stress check. The stand-in model only reads the payload it is given.

Expected prices come from SHOP_CATALOG in config.py. A business fact that is not
there is a hold, unless faq.yaml already contains the owner's answer.
"""
import asyncio
import io
import re
import tempfile
import time
from pathlib import Path

from PIL import Image

from support_agent.dialog_probe import NoTelegram
from support_agent.faq import collect_fields, load_faq, usable_answer
from support_agent.glossary import load_glossary
from support_agent.service import Service
from support_agent.storage import Store
from support_agent.turn_understanding import enforce_turn_policy, package, states_price


def _fold(value):
    return ' '.join(str(value or '').casefold().replace('ё', 'е').split())


def shop_catalog_from_config():
    """Plans and prices exactly as written in config.py. No extra products."""
    config_path = Path(__file__).resolve().parents[1] / 'config.py'
    with config_path.open(encoding='utf-8') as source:
        text = source.read()
    start = text.find('SHOP_CATALOG = {')
    if start < 0:
        raise RuntimeError('SHOP_CATALOG missing')
    i = start + len('SHOP_CATALOG = ')
    depth = 0
    end = None
    for j, ch in enumerate(text[i:], i):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = j + 1
                break
    blob = text[i:end]
    parts = re.split(r'\n    "([a-z0-9_.]+)": \{', blob)
    catalog = []
    for key, body in zip(parts[1::2], parts[2::2]):
        name = re.search(r'"name":\s*"([^"]+)"', body)
        plans = re.findall(r'\{"name":\s*"([^"]+)",\s*"price":\s*(\d+)', body)
        for plan, price in plans:
            catalog.append({'service': name.group(1) if name else key, 'service_key': key,
                            'plan': plan, 'catalog_price_rub': int(price), 'fulfillment': 'not_verified'})
    return catalog


def _phrase_in(text, token):
    folded = _fold(text)
    needle = _fold(token)
    return bool(needle) and (folded == needle or (' ' + needle + ' ') in (' ' + folded + ' '))


def match_service(text, glossary):
    best = None
    best_len = 0
    for row in glossary.get('services') or []:
        for name in [row.get('name'), row.get('id'), *(row.get('aliases') or [])]:
            if _phrase_in(text, name) and len(_fold(name)) > best_len:
                best = row['id']
                best_len = len(_fold(name))
    return best


def match_plan(text, tariffs, service_id):
    rows = [row for row in tariffs if row.get('service_id') == service_id]
    folded = _fold(text)
    if not rows:
        return None
    if 'подешевл' in folded or 'дешевл' in folded:
        shown = rows
        return min(shown, key=lambda row: row['price'])
    wanted = None
    if 'pro+' in folded or 'про+' in folded:
        wanted = 'pro+'
    elif 'premier' in folded or 'премьер' in folded:
        wanted = 'premier'
    elif 'heavy' in folded or 'хеви' in folded:
        wanted = 'heavy'
    elif 'max 20' in folded or '20' in folded and 'макс' in folded:
        wanted = 'max 20'
    elif 'max 5' in folded or ('5' in folded and 'макс' in folded):
        wanted = 'max 5'
    elif 'pro 5' in folded or 'про 5' in folded or '5×' in folded or '5x' in folded:
        wanted = 'pro 5'
    elif 'плюс' in folded or re.search(r'(?<![a-z])plus(?![a-z])', folded):
        wanted = 'plus'
    elif 'basic' in folded or 'бейсик' in folded or 'базов' in folded:
        wanted = 'basic'
    elif 'standard' in folded or 'стандарт' in folded:
        wanted = 'standard'
    elif 'creator' in folded or 'креатор' in folded:
        wanted = 'creator'
    elif 'starter' in folded or 'стартер' in folded:
        wanted = 'starter'
    elif re.search(r'(?<![а-я])го(?![а-я])', folded) or re.search(r'(?<![a-z])go(?![a-z])', folded):
        wanted = 'go'
    elif re.search(r'(?<![а-я])про(?![а-я])', folded) or re.search(r'(?<![a-z])pro(?![a-z])', folded):
        wanted = 'pro'
    ordered = sorted(rows, key=lambda item: len(item['plan']), reverse=True)
    if wanted == 'pro':
        exact = [row for row in rows if row['plan'].casefold() == 'pro']
        return exact[0] if exact else None
    if wanted:
        for row in ordered:
            if wanted in row['plan'].casefold():
                return row
    for row in ordered:
        if _phrase_in(text, row['plan']) or _fold(row['plan']) in folded:
            return row
    if 'годов' in folded and not any('год' in row['plan'].casefold() for row in rows):
        return None
    if len(rows) == 1 and 'годов' not in folded:
        return rows[0]
    return None


class _Telegram(NoTelegram):
    def __init__(self, fixtures):
        super().__init__(fixtures)
        self.sent = []

    async def call(self, method, **kwargs):
        self.sent.append((method, kwargs.get('text', '')))
        return {'message_id': 1}


class _Shop:
    def __init__(self, catalog):
        self.catalog = catalog
        self.extra = []
        self.appstore = {'status': 'not_connected', 'regions': []}

    async def facts(self, uid):
        return {'catalog': list(self.catalog) + list(self.extra or []), 'catalog_status': 'available',
                'appstore': self.appstore,
                'bot_navigation': {'status': 'available', 'username': 'neirosetkaa_bot', 'catalog_webapp': True}}

    async def order_facts(self, uid, reference):
        return {'orders_status': 'not_found', 'ownership_scope': 'authenticated_user', 'orders': []}


class StressModel:
    """Reads glossary, tariffs, state and the turn note. Does not invent a price."""

    def __init__(self):
        self.turn = {}
        self.transcript = ''

    async def transcribe(self, data):
        return self.transcript or 'сколько стоит суно про'

    async def understand_turn(self, context, images=None):
        turn = self.turn or {}
        glossary = context.get('glossary') or {}
        tariffs = context.get('tariffs') or []
        state = context.get('dialog_state') or {}
        burst = context.get('burst') or ''
        service_id = turn.get('service') or match_service(burst, glossary) or ''
        if not service_id and turn.get('use_state'):
            service_id = state.get('service_id') or ''
        plan = None
        if turn.get('cheaper') or 'подешевл' in _fold(burst):
            shown = state.get('shown') or [row for row in tariffs if row.get('service_id') == service_id]
            plan = min(shown, key=lambda row: row['price']) if shown else None
            service_id = plan['service_id'] if plan else service_id
        elif turn.get('use_state') and state.get('service_id') and not match_service(burst, glossary):
            service_id = state.get('service_id')
            shown = state.get('shown') or []
            named = match_plan(burst, tariffs, service_id)
            plan = named or (shown[0] if len(shown) == 1 else None)
        else:
            plan = match_plan(burst, tariffs, service_id) if service_id else None
        topic = turn.get('owner_topic') or 'none'
        if turn.get('reply'):
            reply = turn['reply']
        elif topic != 'none':
            if turn.get('naive'):
                reply = turn['naive']
            else:
                written = usable_answer(topic, context.get('faq'))
                fields = collect_fields(topic, context.get('faq'))
                got = turn.get('collected') or {}
                missing = [key for key in fields if not str(got.get(key) or '').strip()]
                labels = {'order_number': 'номер заказа', 'service': 'какой сервис', 'reason': 'причину возврата'}
                if missing:
                    reply = 'Пришлите, пожалуйста: ' + ', '.join(labels[key] for key in missing if key in labels) + '. Решение по возврату я не обещаю.'
                elif written:
                    reply = written
                else:
                    reply = 'Данные для возврата собраны.'
        elif images:
            reply = 'Вижу изображение. Напишите, пожалуйста, что на нём не получается, без паролей и данных карты.'
        elif 'годов' in _fold(burst) and service_id:
            reply = 'Годового тарифа в текущем снимке каталога нет.'
        elif plan:
            reply = '%s %s: %s ₽.' % (plan['service'], plan['plan'], plan['price'])
        elif service_id:
            reply = 'Цены этого названия нет в текущем снимке каталога.'
        else:
            reply = 'Уточните, пожалуйста, о каком сервисе речь.'
        questions = turn.get('questions') or [burst or 'вопрос']
        return {'intent': turn.get('intent') or ('price' if plan else 'other'),
                'service_id': service_id or '',
                'tariff_id': plan['id'] if plan else '',
                'confidence': 'high', 'clarify_ids': [],
                'topic_changed': bool(turn.get('topic_changed') or (
                    service_id and state.get('service_id') and service_id != state.get('service_id'))),
                'known_facts': [], 'missing_facts': [], 'questions': questions,
                'vpn_advice': bool(turn.get('vpn')),
                'payment_asked': bool(turn.get('payment_asked')),
                'fulfillment_choice': turn.get('fulfillment_choice') or '',
                'setup_email': str((turn.get('setup') or {}).get('setup_email') or ''),
                'setup_password': str((turn.get('setup') or {}).get('setup_password') or ''),
                'setup_recovery': str((turn.get('setup') or {}).get('setup_recovery') or ''),
                'reask_advisories': list(turn.get('reask') or []),
                'matched_tariff_ids': list(turn.get('matched') or []),
                'region': turn.get('region') or '',
                'conflict': turn.get('conflict') or 'none',
                'conflict_reason': turn.get('conflict_reason') or '',
                'owner_alignment': turn.get('alignment') or 'follows',
                'owner_topic': topic,
                'beyond_owner_fact': bool(turn.get('beyond')),
                'refund_order': str((turn.get('collected') or {}).get('order_number') or ''),
                'refund_service': str((turn.get('collected') or {}).get('service') or ''),
                'refund_reason': str((turn.get('collected') or {}).get('reason') or ''),
                'risk': 'instruct', 'order_number': turn.get('order_number') or '',
                'receipt_amount_unverified': '',
                'reply': reply, 'summary': 'Проверка диалога ' + str(turn.get('id') or '')}


def _jpeg():
    buffer = io.BytesIO()
    Image.new('RGB', (12, 12), (250, 250, 250)).save(buffer, 'JPEG')
    return buffer.getvalue()


def build_cases(catalog):
    by_service = {}
    for row in catalog:
        by_service.setdefault(row['service_key'], []).append(row)
    glossary = load_glossary()['services']
    cases = []

    def add(case_id, turns):
        cases.append({'id': case_id, 'turns': turns})

    for row in glossary:
        key = row['id']
        alias = next((item for item in row['aliases'] if _fold(item) != _fold(row['name'])), row['aliases'][0])
        plans = by_service.get(key) or []
        if plans:
            plan = sorted(plans, key=lambda item: item['catalog_price_rub'])[0]
            add('price-' + key, [{'role': 'client', 'text': 'скока стоит ' + alias + ' ' + plan['plan'],
                                  'expect': {'hold': False, 'service': key, 'price': plan['catalog_price_rub'],
                                             'forbid': (['вернём', 'этап:'] if key == 'claude' else ['гарант', 'вернём', 'этап:'])}}])
            add('yearly-' + key, [
                {'role': 'client', 'text': 'хочу ' + alias, 'expect': {'hold': False, 'service': key}},
                {'role': 'client', 'text': 'а годовая есть?', 'use_state': True,
                 'expect': {'hold': False, 'service': key, 'forbid_price': not any('год' in item['plan'].casefold() for item in plans)}},
            ])
        else:
            add('name-' + key, [{'role': 'client', 'text': 'как оформить ' + alias,
                                 'expect': {'hold': False, 'forbid_price': True,
                                            'forbid': (['вернём', 'этап:'] if key == 'claude' else ['гарант', 'вернём', 'этап:'])}}])

    add('follow-yes', [
        {'role': 'client', 'text': 'сколько миджорни basic',
         'expect': {'hold': False, 'service': 'midjourney', 'price': 1000}},
        {'role': 'client', 'text': 'да', 'use_state': True,
         'expect': {'hold': False, 'service': 'midjourney', 'price': 1000, 'forbid': ['5500']}},
    ])
    add('follow-how-much', [
        {'role': 'client', 'text': 'хочу ловабл',
         'expect': {'hold': False, 'service': 'lovable', 'price': 2300}},
        {'role': 'client', 'text': 'а сколько?', 'use_state': True,
         'expect': {'hold': False, 'service': 'lovable', 'price': 2300}},
    ])
    add('cheaper', [
        {'role': 'client', 'text': 'какие цены миджа', 'intent': 'list_options',
         'expect': {'hold': False, 'service': 'midjourney'}},
        {'role': 'client', 'text': 'тот что подешевле', 'cheaper': True,
         'expect': {'hold': False, 'service': 'midjourney', 'price': 1000, 'forbid': ['5500', '3000']}},
    ])
    add('switch-topic', [
        {'role': 'client', 'text': 'миджа про сколько',
         'expect': {'hold': False, 'service': 'midjourney', 'price': 5500}},
        {'role': 'client', 'text': 'а суно про сколько', 'topic_changed': True,
         'expect': {'hold': False, 'service': 'suno', 'price': 1000, 'forbid': ['5500']}},
    ])
    add('two-questions', [{
        'role': 'client', 'text': 'сколько стоит канва и как её оплатить?',
        'questions': ['Сколько стоит Canva Pro?', 'Как оплатить Canva Pro?'],
        'reply': '1. Canva Pro: 1200 ₽.\n2. Оплата Canva Pro проходит в боте.',
        'service': 'canva',
        'expect': {'hold': False, 'price': 1200, 'need': ['1.', '2.', 'Canva']},
    }])
    add('three-questions', [{
        'role': 'client', 'text': 'гамма плюс сколько, как оплатить и где активировать?',
        'questions': ['Сколько стоит Gamma Plus?', 'Как оплатить Gamma?', 'Где активация Gamma?'],
        'reply': '1. Gamma Plus: 1000 ₽.\n2. Оплата Gamma идёт в боте.\n3. Дальнейший шаг Gamma смотрите в сообщении заказа в боте.',
        'service': 'gamma',
        'expect': {'hold': False, 'need': ['1.', '2.', '3.', 'Gamma'], 'price': 1000},
    }])
    add('missed-question', [{
        'role': 'client', 'text': 'сколько курсор и как активировать?',
        'questions': ['Сколько стоит Cursor Pro?', 'Как активировать Cursor?'],
        'reply': 'Cursor Pro: 2300 ₽.',
        'service': 'cursor',
        'expect': {'hold': True},
    }])
    add('payment-ask', [{
        'role': 'client', 'text': 'я оплатил, а активации нет',
        'intent': 'payment_issue',
        'reply': 'Пришлите номер заказа из бота или номер платежа FreeKassa и чек об оплате из банка: скриншот или PDF.',
        'expect': {'hold': False, 'need': ['номер', 'чек'], 'forbid': ['активирована', 'прошла']},
    }])
    add('payment-unknown-order', [{
        'role': 'client', 'text': 'заказ 555 оплатил',
        'intent': 'payment_issue', 'order_number': '555',
        'reply': 'Сверю заказ 555.',
        'expect': {'hold': True},
    }])
    add('payment-asserted', [{
        'role': 'client', 'text': 'деньги точно дошли, включите подписку',
        'reply': 'Оплата прошла. Подписка активирована.',
        'expect': {'hold': True},
    }])
    add('refund-collect', [
        {'role': 'client', 'text': 'хочу вернуть оплату',
         'owner_topic': 'refund_terms',
         'expect': {'hold': False, 'need': ['номер заказа', 'какой сервис', 'причину возврата'],
                    'forbid': ['вернём', 'одобрен']}},
        {'role': 'client', 'text': 'номер 555',
         'owner_topic': 'refund_terms', 'collected': {'order_number': '555'},
         'expect': {'hold': False, 'need': ['какой сервис', 'причину возврата'], 'forbid': ['номер заказа']}},
        {'role': 'client', 'text': 'суно, не пришло',
         'owner_topic': 'refund_terms',
         'collected': {'order_number': '555', 'service': 'Suno', 'reason': 'не пришло'},
         'expect': {'hold': True, 'owner_has': ['555', 'Suno', 'не пришло']}},
    ])
    add('refund-invented', [{
        'role': 'client', 'text': 'когда именно вернёте деньги',
        'owner_topic': 'refund_terms', 'fixed_by': 'owner_fact',
        'naive': 'Вернём деньги в течение суток на карту.',
        'expect': {'hold': True},
    }])
    add('guarantee-suno', [{
        'role': 'client', 'text': 'на суно гарантия есть?',
        'owner_topic': 'guarantee',
        'reply': 'Гарантия есть на все сервисы, кроме Claude. На Suno она есть.',
        'expect': {'hold': False, 'need': ['Гарантия есть на все сервисы, кроме Claude', 'Suno'],
                   'forbid': ['месяц', 'суток', 'дней']},
    }])
    add('guarantee-claude', [{
        'role': 'client', 'text': 'а на клод гарантия есть, если заблокирует?',
        'owner_topic': 'account_ban',
        'expect': {'hold': False, 'need': ['заблокировать', 'гарантии на Claude нет']},
    }])
    add('guarantee-terms', [{
        'role': 'client', 'text': 'на сколько месяцев эта гарантия?',
        'owner_topic': 'guarantee', 'beyond': True,
        'reply': 'Гарантия есть на все сервисы, кроме Claude. Она действует 12 месяцев.',
        # Owner 12.11.2026: send the rule, the rest goes to Alexander, no stop.
        'expect': {'hold': False, 'need': ['уточнит Александр'], 'forbid': ['12 месяцев']},
    }])
    add('delivery-time', [{
        'role': 'client', 'text': 'когда я её получу',
        'owner_topic': 'delivery_time',
        'expect': {'hold': False, 'need': ['5', '30', '2', 'автоактивац']},
    }])
    add('delivery-invented', [{
        'role': 'client', 'text': 'через сколько придёт kling после оплаты',
        'owner_topic': 'delivery_time', 'fixed_by': 'owner_fact',
        'naive': 'Kling придёт за 10 минут.',
        # The invented term is replaced by the owner's own text, not sent and not held.
        'expect': {'hold': False, 'need': ['5–30 минут']},
    }])
    add('chatgpt-activation', [{
        'role': 'client', 'text': 'как активировать чат гпт плюс',
        'reply': 'Для обычной подписки ChatGPT токен сессии вставляется в окно активации в боте, не в эту переписку. Обычно это 2–5 минут, вручную до получаса. Это не гарантия.',
        'service': 'chatgpt',
        'expect': {'hold': False, 'need': ['окно активации в боте', 'не в эту переписку'],
                   'forbid': ['гарантир'], 'context': 'окно активации в боте'},
    }])
    add('ban-timing', [{
        'role': 'client', 'text': 'если аккаунт забанят, за сколько дадите новый?',
        'owner_topic': 'account_ban', 'beyond': True,
        'naive': 'Если аккаунт забанят, заменим его в течение часа.',
        'expect': {'hold': False, 'need': ['уточнит Александр'], 'forbid': ['в течение часа']},
    }])
    add('off-catalog', [{
        'role': 'client', 'text': 'можно купить нетфликс и сколько он стоит',
        'owner_topic': 'off_catalog', 'fixed_by': 'owner_fact',
        'naive': 'Netflix оформим за 1500 ₽.',
        'expect': {'hold': False, 'need': ['какой тариф', 'регион', 'срок']},
    }])
    add('claude-region', [{
        'role': 'client', 'text': 'клод работает в рф?',
        'vpn': True,
        'reply': 'Россия не входит в официальный перечень доступа Claude. Для доступа лучше включить VPN и по возможности всегда использовать одну и ту же локацию. Это не гарантия.',
        'service': 'claude',
        'expect': {'hold': False, 'need': ['VPN', 'локац'], 'forbid': ['гарантия есть'],
                   'context': 'официальный перечень доступа Claude'},
    }])
    add('region-invented', [{
        'role': 'client', 'text': 'в любой стране точно откроется?',
        'owner_topic': 'region_policy', 'fixed_by': 'owner_fact',
        'naive': 'Сервис официально открыт в любой стране, ограничений нет.',
        'expect': {'hold': True},
    }])
    add('angry', [{
        'role': 'client', 'text': 'верните всё немедленно, я буду жаловаться',
        'conflict': 'high', 'conflict_reason': 'Клиент требует немедленный возврат и жалуется.',
        'reply': 'Сейчас разберусь и всё верну.',
        'expect': {'hold': True, 'owner_alert': True},
    }])
    add('annoyed-ok', [{
        'role': 'client', 'text': 'ну сколько в итоге суно про',
        'conflict': 'annoyed',
        'expect': {'hold': False, 'service': 'suno', 'price': 1000},
    }])
    add('owner-leads', [
        {'role': 'owner', 'text': 'Токен вставляется в окно бота, цену клиенту не меняйте.'},
        {'role': 'client', 'text': 'а что дальше', 'expect_paused': True},
        {'role': 'clear_pause'},
        {'role': 'client', 'text': 'а что дальше', 'use_state': True,
         'reply': 'Продолжаю от реплики Александра: токен вставляется в окно бота.',
         'alignment': 'follows',
         'expect': {'hold': False, 'need': ['окно бота'], 'forbid': ['999']}},
    ])
    add('owner-contradict', [
        {'role': 'owner', 'text': 'Токен вставляется в окно бота, не в эту переписку.'},
        {'role': 'clear_pause'},
        {'role': 'client', 'text': 'так куда вставлять',
         'reply': 'Нет, пришлите токен сюда в чат.',
         'alignment': 'contradicts',
         'expect': {'hold': True}},
    ])
    add('repeat-assistant', [
        {'role': 'client', 'text': 'как открыть хейген',
         'reply': 'HeyGen Creator смотрите в каталоге бота, повторно цену не называю.',
         'expect': {'hold': False, 'service': 'heygen'}},
        # One repeat is allowed: clients do ask the same thing again. The third identical text goes to the owner.
        {'role': 'client', 'text': 'ещё раз то же самое',
         'reply': 'HeyGen Creator смотрите в каталоге бота, повторно цену не называю.',
         'expect': {'hold': False}},
        {'role': 'client', 'text': 'и ещё раз',
         'reply': 'HeyGen Creator смотрите в каталоге бота, повторно цену не называю.',
         'fixed_by': 'repeat',
         'expect': {'hold': True}},
    ])
    add('debug-leak', [{
        'role': 'client', 'text': 'что с заказом',
        'reply': 'Этап: разбор хода. Service_id=chatgpt.',
        'expect': {'hold': True},
    }])
    add('photo-error', [{
        'role': 'photo', 'text': 'вот ошибка на экране',
        'expect': {'hold': False, 'need': ['изображение'], 'forbid_price': True, 'forbid': ['error 500', 'гарант']},
    }])
    add('voice-suno', [{
        'role': 'voice', 'text': '', 'transcript': 'сколько стоит суно про',
        'expect': {'hold': False, 'service': 'suno', 'price': 1000},
    }])
    add('repeat-question', [
        {'role': 'client', 'text': 'где мой заказ',
         'reply': 'Напишите номер заказа из бота.',
         'expect': {'hold': False, 'need': ['номер заказа']}},
        {'role': 'client', 'text': 'где мой заказ', 'conflict': 'high',
         'conflict_reason': 'Тот же вопрос второй раз без решения.',
         'reply': 'Напишите номер заказа из бота.',
         'expect': {'hold': True}},
    ])
    add('mixed-two-prices', [{
        'role': 'client', 'text': 'сколько клод про и сколько суно про',
        'questions': ['Сколько стоит Claude Pro?', 'Сколько стоит Suno Pro?'],
        'reply': '1. Claude Pro: 2000 ₽.\n2. Suno Pro: 1000 ₽.',
        'expect': {'hold': False, 'need': ['2000', '1000', 'Claude', 'Suno']},
    }])
    add('chatgpt-go', [{
        'role': 'client', 'text': 'а гпт го сколько',
        'expect': {'hold': False, 'service': 'chatgpt', 'price': 1000, 'forbid': ['9000']},
    }])
    add('cursor-pro-plus', [{
        'role': 'client', 'text': 'курсор про+ цена',
        'expect': {'hold': False, 'service': 'cursor', 'price': 5500, 'forbid': ['2300']},
    }])
    add('eleven-starter', [{
        'role': 'client', 'text': 'элевен стартер сколько',
        'expect': {'hold': False, 'service': 'elevenlabs', 'price': 600},
    }])
    add('kling-standard-then-pro', [
        {'role': 'client', 'text': 'клинг стандарт цена',
         'expect': {'hold': False, 'service': 'kling', 'price': 900}},
        {'role': 'client', 'text': 'а про?', 'use_state': True,
         'expect': {'hold': False, 'service': 'kling', 'price': 2700, 'forbid': ['900']}},
    ])
    add('runway-pro', [{
        'role': 'client', 'text': 'ранвей про сколько стоит',
        'expect': {'hold': False, 'service': 'runway', 'price': 3700},
    }])
    add('thanks', [{
        'role': 'client', 'text': 'ок спасибо',
        'reply': 'Пожалуйста. Если появится вопрос по каталогу, напишите.',
        'expect': {'hold': False, 'forbid_price': True},
    }])
    add('perplexity-pro', [{
        'role': 'client', 'text': 'перплекс про сколько',
        'expect': {'hold': False, 'service': 'perplexity', 'price': 2000},
    }])
    add('coins-unknown', [{
        'role': 'client', 'text': 'сколько процентов дают монетки',
        'reply': 'Процента монеток в этом снимке каталога нет. Его подтверждает Александр.',
        'expect': {'hold': False, 'forbid_price': True, 'forbid': ['%', '10', '20']},
    }])
    add('casual-suno-timing', [{
        'role': 'client', 'text': 'суно про сколько и когда будет',
        'owner_topic': 'delivery_time', 'beyond': True,
        'questions': ['Сколько стоит Suno Pro?', 'Когда будет Suno?'],
        'reply': 'Suno Pro: 1000 ₽.\n\nАктивация занимает 5–30 минут. У сервисов с автоактивацией — 2–5 минут.',
        'expect': {'hold': False, 'service': 'suno', 'price': 1000, 'casual': True,
                   'need': ['1000', '5', '30', '2']},
    }])
    add('vpn-once', [
        {'role': 'client', 'text': 'клод работает в рф?', 'vpn': True, 'service': 'claude',
         'reply': 'Россия не входит в официальный перечень доступа Claude.',
         'expect': {'hold': False, 'need': ['VPN', 'локац'], 'service': 'claude'}},
        {'role': 'client', 'text': 'а тариф дальше', 'vpn': True, 'use_state': True,
         'reply': 'Дальше подскажу тариф.\n\nДля доступа лучше включить VPN и одну и ту же локацию.',
         'expect': {'hold': False, 'need': ['тариф'], 'forbid': ['VPN', 'локац'], 'casual': True}},
        {'role': 'client', 'text': 'а про vpn ещё раз', 'vpn': True, 'reask': ['vpn_location'],
         'reply': 'Да. Для доступа лучше включить VPN и всегда одну и ту же локацию.',
         'expect': {'hold': False, 'need': ['VPN', 'локац']}},
    ])
    add('vpn-owner-said', [
        {'role': 'owner', 'text': 'Клиенту уже сказал: включите VPN и держите одну локацию.'},
        {'role': 'clear_pause'},
        {'role': 'client', 'text': 'сколько клод про', 'vpn': True,
         'expect': {'hold': False, 'service': 'claude', 'price': 2000, 'forbid': ['VPN', 'локац']}},
    ])
    add('icloud-region-followup', [
        {'role': 'client', 'text': 'Хочу пополнить айклауд на регион США',
         'expect': {'hold': False, 'service': 'appstore', 'region': 'US', 'casual': True,
                    'need': ['5 USD', '450', 'США'], 'forbid': ['Росси', '640']}},
        {'role': 'client', 'text': 'На 30$ есть?', 'use_state': True, 'service': 'chatgpt',
         'reply': '1. Да, есть пополнение ChatGPT на 30$.\n2. Стоимость 3090 ₽.',
         'expect': {'hold': False, 'service': 'appstore', 'region': 'US', 'casual': True,
                    'need': ['нет', '5 USD'], 'forbid': ['3090', 'ChatGPT']}},
        {'role': 'client', 'text': 'А Айклауд?', 'use_state': True,
         'expect': {'hold': False, 'service': 'appstore', 'region': 'US', 'casual': True,
                    'need': ['5 USD'], 'forbid': ['3090']}},
    ])
    add('plus-two-options', [
        {'role': 'client', 'text': 'Хочу чат гпт',
         'expect': {'hold': False, 'service': 'chatgpt'}},
        # Owner 11.11.2026: plain «плюс» is plain Plus; the new-account plan only when the client asks for it.
        {'role': 'client', 'text': 'Тариф плюс', 'use_state': True,
         'reply': 'ChatGPT Plus: 2000 ₽.',
         'expect': {'hold': False, 'service': 'chatgpt', 'casual': True,
                    'need': ['Plus'], 'forbid_plans': ['Go'],
                    'forbid': ['1000', 'Новый аккаунт']}},
        {'role': 'client', 'text': 'А на новый аккаунт plus?', 'use_state': True,
         'reply': 'Новый аккаунт с подпиской Plus: 2090 ₽.',
         'expect': {'hold': False, 'service': 'chatgpt', 'need': ['Новый аккаунт']}},
    ])
    add('pay-chatgpt-plus', [
        {'role': 'client', 'text': 'Хочу чат гпт плюс',
         'expect': {'hold': False, 'service': 'chatgpt', 'price': 2000, 'casual': True}},
        {'role': 'client', 'text': 'как оплатить?', 'use_state': True, 'payment_asked': True,
         'reply': 'Для оплаты тарифа ChatGPT Plus используйте токен сессии в окне активации бота.',
         'expect': {'hold': False, 'service': 'chatgpt',
                    'need': ['СБП', 'Картой', 'Активировать подписку', 'После оплаты', '2-5'],
                    'forbid': ['используйте токен сессии в окне активации', '5-30', '30-60',
                               'Я оплатил']}},
    ])
    add('pay-link', [{
        # Owner 12.11.2026: the tariff first (cheapest first, with prices), then the card.
        'role': 'client', 'text': 'как оплатить клинг',
        'payment_asked': True,
        'expect': {'hold': False, 'service': 'kling',
                   'need': ['Standard', 'Pro', 'какой тариф оформить'],
                   'forbid': ['Я оплатил']}},
        {'role': 'client', 'text': 'Standard',
         'payment_asked': True,
         'expect': {'hold': False, 'service': 'kling',
                    'need': ['ссылк', 'не плат', 'не нужны', '5-30'],
                    'forbid': ['Я оплатил', '2-5']}},
    ])
    add('pay-suno-password', [{
        'role': 'client', 'text': 'как оплатить суно',
        'payment_asked': True,
        'expect': {'hold': False, 'service': 'suno', 'need': ['Pro', 'Premier', 'какой тариф оформить']}},
        {'role': 'client', 'text': 'Pro', 'payment_asked': True,
         'expect': {'hold': False, 'service': 'suno',
                   'need': ['почт', 'парол', '5-30'],
                   'forbid': ['пришлите ссылку', 'Я оплатил']}},
    ])
    add('pay-zai-choice', [
        {'role': 'client', 'text': 'как оплатить z.ai',
         'payment_asked': True,
         'expect': {'hold': False, 'service': 'zai',
                    'need': ['почт', 'ссылк', 'не нужны'],
                    'forbid': ['СБП', 'Я оплатил']}},
        {'role': 'client', 'text': 'давайте по ссылке', 'use_state': True,
         'payment_asked': True, 'fulfillment_choice': 'link',
         'expect': {'hold': False, 'service': 'zai',
                    'need': ['СБП', 'не плат', '5-30'],
                    'forbid': ['Я оплатил']}},
    ])
    add('pay-telegram-username', [{
        'role': 'client', 'text': 'как оплатить телеграм премиум',
        'payment_asked': True,
        'expect': {'hold': False, 'service': 'telegram_premium',
                   'need': ['username', '5-30'],
                   'forbid': ['пришлите пароль', 'Я оплатил']}},
    ])
    add('pay-capcut-new', [{
        'role': 'client', 'text': 'как оплатить капкат',
        'payment_asked': True,
        'expect': {'hold': False, 'service': 'capcut',
                   'need': ['новый аккаунт', 'Александр', '5-30'],
                   'forbid': ['пришлите пароль', 'Я оплатил']}},
    ])
    add('pay-claude-max', [{
        'role': 'client', 'text': 'как оплатить клод макс 20',
        'payment_asked': True,
        'expect': {'hold': False, 'service': 'claude',
                   'need': ['Александр', 'почт', 'парол', '5-30'],
                   'forbid': ['Organization', 'Активировать Claude', '2-5', 'Я оплатил']}},
    ])
    add('pay-claude-pro-auto', [{
        'role': 'client', 'text': 'как оплатить клод про',
        'payment_asked': True,
        'expect': {'hold': False, 'service': 'claude',
                   'need': ['Активировать Claude', 'Organization', '2-5'],
                   'forbid': ['парол', '5-30', 'Я оплатил']}},
    ])
    add('pay-chatgpt-pro5', [{
        'role': 'client', 'text': 'как оплатить чат гпт про 5',
        'payment_asked': True,
        'expect': {'hold': False, 'service': 'chatgpt',
                   'need': ['токен', 'Александр', '5-30'],
                   'forbid': ['Активировать подписку', 'почту и пароль', 'Я оплатил']}},
    ])
    add('new-account-plus', [
        {'role': 'client', 'text': 'Как оплатить новый аккаунт с подпиской Plus',
         'service': 'chatgpt', 'payment_asked': True,
         'expect': {'hold': False, 'service': 'chatgpt',
                    'need': ['Активировать', 'почту, которую поставить', 'пароль для этого ящика',
                             'настоящую почту', '5-30'],
                    'forbid': ['Активировать подписку', 'Я оплатил']}},
        {'role': 'client', 'text': 'Почта для ящика newbox@example.com',
         'service': 'chatgpt', 'use_state': True,
         'setup': {'setup_email': 'newbox@example.com'},
         'expect': {'hold': False, 'need': ['пароль для этого ящика', 'настоящую почту'],
                    'forbid': ['newbox@example.com', 'Активировать подписку']}},
        {'role': 'client', 'text': 'Пароль Secret1 и восстановление real@example.com',
         'service': 'chatgpt', 'use_state': True,
         'setup': {'setup_password': 'Secret1', 'setup_recovery': 'real@example.com'},
         'expect': {'hold': True, 'owner_has': ['newbox@example.com', 'Secret1', 'real@example.com']}},
    ])
    add('chosen-plan-not-relisted', [
        {'role': 'client', 'text': 'суно про сколько',
         'expect': {'hold': False, 'service': 'suno', 'price': 1000}},
        {'role': 'client', 'text': 'беру этот', 'use_state': True,
         'reply': 'Suno Pro: 1000 ₽.\nSuno Premier: 3000 ₽.\nКакой из них?',
         'expect': {'hold': False, 'service': 'suno', 'need': ['1000'],
                    'forbid': ['3000', 'Premier'], 'casual': True}},
    ])
    by_id = {case['id']: case for case in cases}
    by_id['icloud-region-followup']['extra_catalog'] = [{
        'service': 'App Store / iCloud', 'service_key': 'appstore', 'plan': '',
        'catalog_price_rub': 0, 'fulfillment': 'not_verified',
    }]
    by_id['icloud-region-followup']['appstore'] = {
        'status': 'available',
        'regions': [
            {'name': 'США | US', 'denoms': [
                {'nominal': '5 USD', 'price_rub': 450},
                {'nominal': '10 USD', 'price_rub': 910},
                {'nominal': '15 USD', 'price_rub': 1365},
            ]},
            {'name': 'Россия | RU', 'denoms': [
                {'nominal': '500 RUB', 'price_rub': 640},
                {'nominal': '1000 RUB', 'price_rub': 1280},
            ]},
        ],
    }
    # Цена 2090 в этом прогоне взята из живого ответа на скриншоте, в config.py тарифа нет.
    # Ответ уточнения цену не называет.
    def _bare(service_id, service):
        return {'service': service, 'service_key': service_id, 'plan': '',
                'catalog_price_rub': 0, 'fulfillment': 'not_verified'}

    by_id['pay-zai-choice']['extra_catalog'] = [_bare('zai', 'Z.ai')]
    by_id['pay-telegram-username']['extra_catalog'] = [_bare('telegram_premium', 'Telegram Premium')]
    by_id['pay-capcut-new']['extra_catalog'] = [_bare('capcut', 'CapCut')]
    by_id['new-account-plus']['extra_catalog'] = [{
        'service': 'ChatGPT', 'service_key': 'chatgpt', 'plan': 'Новый аккаунт с подпиской Plus',
        'catalog_price_rub': 2090, 'fulfillment': 'not_verified',
    }]
    by_id['plus-two-options']['extra_catalog'] = [{
        'service': 'ChatGPT', 'service_key': 'chatgpt', 'plan': 'Новый аккаунт с подпиской Plus',
        'catalog_price_rub': 2090, 'fulfillment': 'not_verified',
    }]
    return cases


def _check(answer, turn, context):
    expect = turn.get('expect') or {}
    errors = []
    if 'hold' in expect and bool(answer.get('handoff')) != bool(expect['hold']):
        errors.append('hold got %s' % answer.get('handoff'))
    turn_info = (answer.get('evidence') or {}).get('turn_understanding') or {}
    if 'service' in expect and turn_info.get('service_id') != expect['service']:
        errors.append('service got %s' % turn_info.get('service_id'))
    reply = str(answer.get('reply') or '')
    if expect.get('price') is not None and str(expect['price']) not in reply:
        errors.append('price')
    if expect.get('forbid_price') and states_price(reply):
        errors.append('price invented')
    for word in expect.get('need') or []:
        if word.casefold() not in reply.casefold():
            errors.append('need ' + word)
    for word in expect.get('forbid') or []:
        if word.casefold() in reply.casefold():
            errors.append('forbid ' + word)
    if not answer.get('handoff') and re.search(r'этап\s*:|service_id\s*=', reply, re.I):
        errors.append('debug')
    needle = expect.get('context')
    if needle and needle.casefold() not in '\n'.join(context.get('documented_facts') or []).casefold():
        errors.append('context fact missing')
    if 'region' in expect and (turn_info.get('region') or '') != expect['region']:
        errors.append('region got %s' % turn_info.get('region'))
    if expect.get('casual'):
        if re.search(r'(?m)^\s*\d+[.)]\s+\S', reply):
            errors.append('numbered list')
        if len(reply) > int(expect.get('max_chars') or 500):
            errors.append('too long')
        if len(reply) > 220 and '\n' not in reply:
            errors.append('dense block')
    for name in expect.get('forbid_plans') or []:
        if name.casefold() in reply.casefold():
            errors.append('relisted ' + name)
    return errors


def _old_policy_holds(understanding, context):
    """What the previous checker did: no owner-file hold and no repeat hold."""
    stripped = dict(understanding, owner_topic='none')
    quiet = dict(context, history=[item for item in context.get('history') or [] if item.get('role') != 'assistant'])
    answer = package(stripped, quiet)
    answer = enforce_turn_policy(answer, stripped, quiet, {'status': 'fresh'})
    return bool(answer.get('handoff'))


def run_suite():
    catalog = shop_catalog_from_config()
    cases = build_cases(catalog)
    covered = set()
    for case in cases:
        for turn in case['turns']:
            covered.add(case['id'].split('-', 1)[-1] if case['id'].startswith(('price-', 'name-')) else '')
    shop_keys = {row['service_key'] for row in catalog}
    named = {case['id'].split('-', 1)[-1] for case in cases if case['id'].startswith(('price-', 'name-'))}
    glossary_ids = {row['id'] for row in load_glossary()['services']}
    picture = _jpeg()
    voice = b'OggS' + b'\x00' * 24
    file = tempfile.NamedTemporaryFile(suffix='.sqlite')
    store = Store(file.name, batch_seconds=0)
    store.put('mode', 'auto')
    tg = _Telegram({'shot-1': picture, 'voice-1': voice})
    model = StressModel()
    shop = _Shop(catalog)
    service = Service(store, tg, model, shop, owner=1, allowed={1, 50}, daily_limit=5000)
    checked = 0
    failed = []
    before_fail = 0
    before_total = 0
    try:
        for index, case in enumerate(cases):
            conn = 'c%d' % index
            shop.extra = case.get('extra_catalog') or []
            shop.appstore = case.get('appstore') or {'status': 'not_connected', 'regions': []}
            service.catalog_cache._rows.clear()
            store.connection(conn, True, True)
            mid = 0
            last_reply = ''
            for turn in case['turns']:
                role = turn['role']
                if role == 'clear_pause':
                    cid = store.db.execute('SELECT id FROM chats WHERE connection=?', (conn,)).fetchone()[0]
                    store.db.execute('UPDATE chats SET manual_pause_until=0 WHERE id=?', (cid,))
                    continue
                if role == 'echo':
                    mid += 1
                    store.ingest(conn, 50, mid, 'assistant', last_reply, {}, time.time())
                    continue
                mid += 1
                now = time.time()
                media = {}
                text = turn.get('text') or ''
                if role == 'photo':
                    media = {'kind': 'photo', 'file_id': 'shot-1', 'file_size': len(picture)}
                    role = 'client'
                elif role == 'voice':
                    media = {'kind': 'voice', 'file_id': 'voice-1', 'file_size': len(voice), 'duration': 3}
                    model.transcript = turn.get('transcript') or ''
                    role = 'client'
                cid = store.ingest(conn, 50, mid, role, text, media, now)
                store.db.execute("UPDATE chats SET mode='auto' WHERE id=?", (cid,))
                if role != 'client':
                    continue
                store.prepare_now(cid, now)
                job = store.claim(now + 1)
                if turn.get('expect_paused'):
                    if job is not None:
                        failed.append({'id': case['id'], 'errors': ['expected pause']})
                    continue
                if job is None:
                    failed.append({'id': case['id'], 'errors': ['no job']})
                    continue
                model.turn = dict(turn, id=case['id'])
                answer = asyncio.run(service.generate(job))
                store.db.execute("UPDATE messages SET state='done' WHERE chat=? AND mid=?", (cid, job['mid']))
                last_reply = answer.get('reply') or ''
                context = service._last_turn_context or {}
                if turn.get('expect'):
                    checked += 1
                    before_total += 1
                    errors = _check(answer, turn, context)
                    understanding = {'intent': 'other', 'service_id': turn.get('service') or '',
                                     'tariff_id': '', 'confidence': 'high',
                                     'reply': turn.get('naive') or turn.get('reply') or last_reply,
                                     'summary': 'x', 'questions': turn.get('questions') or ['q'],
                                     'vpn_advice': False, 'owner_topic': 'none',
                                     'conflict': turn.get('conflict') or 'none',
                                     'owner_alignment': turn.get('alignment') or 'follows'}
                    old_hold = _old_policy_holds(understanding, context)
                    expect_hold = bool(turn['expect'].get('hold'))
                    if turn.get('fixed_by') and old_hold != expect_hold:
                        before_fail += 1
                    elif errors and not turn.get('fixed_by'):
                        before_fail += 1
                    blob = '\n'.join(text for _, text in tg.sent)
                    for bit in (turn.get('expect') or {}).get('owner_has') or []:
                        if bit not in blob:
                            errors.append('owner ' + bit)
                    if errors:
                        failed.append({'id': case['id'], 'errors': errors, 'reply': last_reply[:180]})
                mid += 1
                spoken = last_reply if case['id'] == 'repeat-assistant' and not turn.get('fixed_by') else 'ок'
                store.ingest(conn, 50, mid, 'assistant', spoken, {}, now)
    finally:
        file.close()
    return {'cases': len(cases), 'checked': checked, 'failed': failed,
            'passed': checked - len(failed), 'before_failed_turns': before_fail,
            'before_checked': before_total, 'shop_keys': sorted(shop_keys),
            'named': sorted(named), 'glossary_ids': sorted(glossary_ids)}
