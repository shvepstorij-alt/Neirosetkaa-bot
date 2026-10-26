"""Highlight exact catalogue matches without turning missing rows into absence.

The complete catalogue remains available for contextual references. This module
does not invent products, prices or fulfilment rules and performs no network I/O.
"""
import re


ALIASES = {
    'krea': ('krea', 'креа'),
    'appstore': ('appstore', 'app store', 'apple id', 'эпл айди', 'эппл айди',
                 'аппстор', 'апп стор', 'эпл стор', 'эппл стор', 'айтюнс', 'itunes'),
    'chatgpt': ('chatgpt', 'chat gpt', 'чат гпт', 'чатгпт'),
    'claude': ('claude', 'клод', 'клода'),
    'perplexity': ('perplexity', 'перплексити'),
    'zoom': ('zoom', 'зум'),
    'notion': ('notion', 'ноушен', 'ноушн'),
    'motion': ('motion', 'моушен', 'моушн'),
    'higgsfield': ('higgsfield', 'higgsfield ai', 'хиггсфилд', 'хиггсфиелд'),
}


def _words(value):
    return tuple(re.findall(r'[^\W_]+', str(value or '').casefold(), flags=re.UNICODE))


def _contains(words, phrase):
    return bool(phrase) and any(words[i:i+len(phrase)] == phrase
                               for i in range(len(words)-len(phrase)+1))


def _families(words):
    return {name for name,aliases in ALIASES.items()
            if any(_contains(words,_words(alias)) for alias in aliases)}


def annotate(facts, latest, products=()):
    """Copy facts, adding request-relevant rows; never mark an item unavailable.

    `products` may be canonical names from the current research plan. It must not
    contain old history topics. Only catalogue rows supply prices and sale names.
    """
    result=dict(facts)
    request=_words(latest)
    product_words=[_words(p) for p in products if isinstance(p,str)]
    requests=[request,*product_words]
    wanted=set().union(*(_families(words) for words in requests))
    # A unique branded first word handles renamed rows (e.g. «Runway ML»)
    # without maintaining an exhaustive list. Generic/ambiguous prefixes do not.
    prefixes={}
    for row in facts.get('catalog',[]):
        if not isinstance(row,dict):continue
        name=_words(row.get('service'))
        if name and len(name[0])>=4 and name[0] not in ('premium','подписка','пополнение','доступ','account','credits'):
            prefixes.setdefault(name[0],set()).add(str(row.get('service_key') or row.get('service')))
    matched=[];services=[];seen=set()
    for row in facts.get('catalog',[]):
        if not isinstance(row,dict):continue
        key=_words(row.get('service_key'))
        name=_words(row.get('service'))
        family=_families(key)|_families(name)
        direct=any(_contains(words,key) or _contains(words,name) for words in requests)
        if name and len(prefixes.get(name[0],()))==1:
            direct=direct or any(_contains(words,(name[0],)) for words in requests)
        alias=bool(family&wanted)
        if not (direct or alias):continue
        matched.append(dict(row))
        identity=(str(row.get('service_key','')),str(row.get('service','')))
        if identity not in seen:
            seen.add(identity)
            services.append({'service_key':identity[0],'service':identity[1],
                             'match_basis':'name_or_key' if direct else 'explicit_alias'})
    result['requested_catalog']=matched
    result['catalog_selection']={
        'status':'matched' if matched else 'no_explicit_match',
        'matched_services':services,
        'mentioned_known_services':sorted(wanted),
        'absence_confirmed':False,
        'note':('Совпавшие строки — предложения магазина; используй их рублёвые цены '
                'вместо официальных цен производителя. Пустой результат сопоставления '
                'не доказывает отсутствие товара. Полный каталог также приложен. '
                'Не утверждай «нет в каталоге» или «не продаём» без отдельного подтверждения.')}
    return result


def false_absence(reply, facts):
    """Conservative guard for explicit catalogue denials of matched offers."""
    for clause in re.split(r'[.!?;\n]',reply):
        if not re.search(r'нет\s+(?:в\s+(?:нашем\s+)?каталоге|прямого\s+предложения)|в\s+(?:нашем\s+)?каталоге\s+нет|не\s+прода[её]м|отсутствует\s+в\s+каталоге',clause,re.I):
            continue
        if annotate({'catalog':facts.get('requested_catalog',[])},clause)['requested_catalog']:
            return True
    return False


def verified_catalog_reply(facts, reason):
    """Owner-review fallback built from fields, never from a rejected model reply."""
    from .privacy import scrub
    rows=facts.get('requested_catalog',[])
    groups={}
    for row in rows:
        if not isinstance(row,dict):continue
        if 'appstore' in (_families(_words(row.get('service_key')))|_families(_words(row.get('service')))):
            continue  # A denomination without a region is not a usable quote.
        price=row.get('customer_price_rub',row.get('catalog_price_rub'))
        if type(price) is not int or price<=0:continue
        name=scrub(str(row.get('service','')))[:100]
        plan=scrub(str(row.get('plan','')))[:100]
        if not name or not plan:continue
        groups.setdefault((name,plan),set()).add(price)
    lines=[f'{name} · {plan} — {next(iter(prices))} ₽' for (name,plan),prices in groups.items() if len(prices)==1]
    lines=lines[:8]
    parts=[]
    if lines:
        parts.append('Тарифы магазина:\n'+'\n'.join(lines)+'\nЦены до применения промокода и списания монет.')
    if 'appstore' in facts.get('catalog_selection',{}).get('mentioned_known_services',[]):
        parts.append('Для подбора пополнения App Store подскажите регион Apple ID, стоимость нужной покупки в валюте аккаунта и текущий баланс.')
    if not parts:parts=['Уточню условия по вашему вопросу.']
    reply='\n\n'.join(parts)
    if len(reply)>1800:reply='\n\n'.join(parts[-1:])
    return {'reply':reply,'summary':'Свободный ответ не прошёл проверку. Сохранена только подтверждённая часть каталога и уточняющие вопросы. Остальные части обращения требуют проверки владельца.',
            'handoff':True,'source_ids':[],'research':{'topic':'none','products':[]},
            'evidence':{'status':'catalog_review','sources':[],
                        'diagnostic':scrub(str(reason))[:500]}}


def direct_catalog_reply(facts, latest):
    """Narrow price-only route; unknown words/tasks go to normal consultation.

    No feature claims, numeric calculations, existing orders or guessed brands.
    """
    words=_words(latest)
    if not set(words)&{'тарифы','цены','цену','стоят','стоит','стоимость','сколько'}:return None
    rows=facts.get('requested_catalog',[])
    regular=[r for r in rows if 'appstore' not in (_families(_words(r.get('service_key')))|_families(_words(r.get('service'))))]
    if not regular or len(regular)>8:return None
    quotes={}
    for row in regular:
        price=row.get('customer_price_rub',row.get('catalog_price_rub'))
        if type(price) is not int or price<=0 or not row.get('service') or not row.get('plan'):return None
        key=(row['service'],row['plan'])
        if key in quotes and quotes[key]!=price:return None
        quotes[key]=price
    allowed=set('привет здравствуйте добрый день вечер какие тарифы тариф тарифов цены цену стоит стоят стоимость сколько можно оформить купить заказать через вас у вас есть и еще ещё также хочу пополнить пополнение баланс баланса что нужно уточнить подскажите пожалуйста мне для спасибо а по на подписку подписки подписок это'.split())
    for row in rows:
        allowed.update(_words(row.get('service_key')));allowed.update(_words(row.get('service')))
        families=_families(_words(row.get('service_key')))|_families(_words(row.get('service')))
        for family in families:
            for alias in ALIASES[family]:allowed.update(_words(alias))
    # App Store is an independent dynamic section, not necessarily a regular row.
    if facts.get('appstore',{}).get('status')=='available':
        for alias in ALIASES['appstore']:allowed.update(_words(alias))
    if any(word not in allowed for word in words):return None
    result=verified_catalog_reply(facts,'')
    if not result['reply'].startswith('Тарифы магазина:'):return None
    result.update(summary='Ответ составлен из прочитанных цен магазина. Возможности тарифов и условия, не указанные в данных, не добавлялись.',handoff=False)
    result['evidence']={'status':'catalog_direct','sources':[]}
    return result
