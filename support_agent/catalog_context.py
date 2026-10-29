"""Highlight exact catalogue matches without turning missing rows into absence.

The complete catalogue remains available for contextual references. This module
does not invent products, prices or fulfilment rules and performs no network I/O.
"""
import re


ALIASES = {
    'krea': ('krea', 'креа', 'крее', 'креи', 'крею'),
    'appstore': ('appstore', 'app store', 'icloud', 'айклауд', 'apple id', 'эпл айди', 'эппл айди',
                 'аппстор', 'апп стор', 'эпл стор', 'эппл стор', 'эп стор', 'эп стора',
                 'апп стора', 'эпл стора', 'эппл стора', 'апстор', 'апстора', 'айтюнс', 'itunes'),
    'chatgpt': ('chatgpt', 'chat gpt', 'чат гпт', 'чатгпт', 'чате гпт', 'чата гпт'),
    'claude': ('claude', 'клод', 'клода', 'клоде', 'клоду'),
    'perplexity': ('perplexity', 'перплексити'),
    'zoom': ('zoom', 'зум', 'зуме', 'зума', 'зуму'),
    'notion': ('notion', 'ноушен', 'ноушн'),
    'motion': ('motion', 'моушен', 'моушн'),
    'higgsfield': ('higgsfield', 'higgsfield ai', 'хиггсфилд', 'хиггсфиелд'),
}


from .apple_matching import ALIASES as APPLE_REGION_ALIASES, region_codes, exact_quote


def apple_region_codes(text):
    return region_codes(text,stock=True)


def apple_selection(apple, text):
    requested=region_codes(text)
    regions=apple.get('regions',[]) if isinstance(apple,dict) else []
    matched=[r for r in regions if isinstance(r,dict) and requested & apple_region_codes(r.get('name',''))]
    return {'requested_codes':sorted(requested),'matched_regions':matched,
            'absence_confirmed':False,
            'note':'Пустое сопоставление не доказывает отсутствие региона. Не утверждай, что регион или номиналы отсутствуют. Если регион уже назван, не проси назвать его повторно; уточни сумму покупки и текущий баланс.'}


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
    if 'appstore' in wanted:
        result['appstore_selection']=apple_selection(facts.get('appstore',{}),latest)
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
    # Dynamic Apple stock is a separate, possibly partial catalogue. Its omission
    # from regular offers is never proof of absence of a region/denomination.
    apple_related=('appstore' in _families(_words(reply)) or
                   'appstore' in facts.get('catalog_selection',{}).get('mentioned_known_services',[]))
    if apple_related and facts.get('appstore') and re.search(
            r'(?:номинал[а-яё]*|регион[а-яё]*|код[а-яё]*)[^.!?\n]{0,130}(?:нет\b|отсутств|не\s+(?:указан|представлен|доступн))|'
            r'(?:нет\b|отсутств|не\s+(?:указан|представлен|доступн))[^.!?\n]{0,130}(?:номинал|регион|код)',reply,re.I):
        return True
    for clause in re.split(r'[.!?;\n]',reply):
        if not re.search(r'нет\s+(?:в\s+(?:нашем\s+)?каталоге|прямого\s+предложения)|в\s+(?:нашем\s+)?каталоге\s+нет|не\s+прода[её]м|отсутствует\s+в\s+каталоге',clause,re.I):
            continue
        if annotate({'catalog':facts.get('requested_catalog',[])},clause)['requested_catalog']:
            return True
    return False


def customer_facts(facts):
    """Consultations quote the public catalogue; checkout owns personal pricing."""
    result=dict(facts)
    for field in ('catalog','requested_catalog'):
        if field not in facts:continue
        result[field]=[]
        for original in facts[field]:
            row=dict(original)
            price=row.get('catalog_price_rub')
            if type(price) is int and price>0:
                for key in ('base_price_rub','customer_price_rub','price_rub'):
                    row.pop(key,None)
                row['price_scope']='public_catalog_before_personal_adjustments'
            result[field].append(row)
    return result


def verified_catalog_reply(facts, reason):
    """Owner-review fallback built from fields, never from a rejected model reply."""
    from .privacy import scrub
    rows=facts.get('requested_catalog',[])
    groups={}
    for row in rows:
        if not isinstance(row,dict):continue
        if 'appstore' in (_families(_words(row.get('service_key')))|_families(_words(row.get('service')))):
            continue  # A denomination without a region is not a usable quote.
        price=row.get('catalog_price_rub',row.get('customer_price_rub'))
        if type(price) is not int or price<=0:continue
        name=scrub(str(row.get('service','')))[:100]
        plan=scrub(str(row.get('plan','')))[:100]
        if not name or not plan:continue
        groups.setdefault((name,plan),set()).add(price)
    lines=[f'{name} · {plan} — {next(iter(prices))} ₽' for (name,plan),prices in groups.items() if len(prices)==1]
    all_lines=lines
    lines=[]
    used=0
    apple_requested=('appstore' in facts.get('catalog_selection',{}).get('mentioned_known_services',[])
                     or bool(facts.get('appstore_selection'))
                     or any('appstore' in (_families(_words(row.get('service_key')))|_families(_words(row.get('service'))))
                            for row in rows if isinstance(row,dict)))
    # Reserve space for the independently priced regional stock and questions.
    line_budget=800 if apple_requested else 1350
    for line in all_lines:
        if len(lines)>=8 or used+len(line)+1>line_budget:break
        lines.append(line);used+=len(line)+1
    parts=[]
    from .workflow_policy import policies_for
    policies=policies_for(facts)
    if lines:
        parts.append('Тарифы магазина:\n'+'\n'.join(lines)+'\nЦены до применения промокода и списания монет.')
        if len(lines)<len(all_lines):
            parts.append('Есть и другие тарифы. Подскажите, какой сервис и план разобрать подробнее?')
    for policy in policies:
        name={'krea':'Krea','suno':'Suno','zoom':'Zoom','openrouter':'Openrouter','spotify':'Spotify','youtube':'YouTube'}[policy['service']]
        if policy['service']=='suno' and any(r.get('fulfillment')!='account_details' for r in rows if 'suno' in str(r.get('service','')).casefold()):
            parts.append('Для оформления Suno нужны почта и пароль от аккаунта. Перед оплатой нужно согласовать порядок оформления заказа.')
        else:
            parts.append(f'Для оформления {name} нужны почта и пароль от вашего аккаунта. После выбора тарифа и оплаты пришлите их сюда, в эту переписку. Дальше потребуется ручное оформление.')
    if apple_requested:
        selection=facts.get('appstore_selection',{})
        known=selection.get('requested_codes',[])
        apple=facts.get('appstore',{})
        quotes=[];quote_size=0
        if apple.get('status') in ('available','truncated'):
            for region in selection.get('matched_regions',[]):
                name=scrub(str(region.get('name','')))[:60]
                for entry in region.get('denoms',[]):
                    price=entry.get('price_rub')
                    nominal=scrub(str(entry.get('nominal','')))[:60]
                    if type(price) is not int or price<=0 or not nominal:continue
                    line=f'{name} · {nominal} — {price} ₽'
                    if len(quotes)>=4 or quote_size+len(line)>350:break
                    quotes.append(line);quote_size+=len(line)+1
        if quotes:
            parts.append('Пополнение App Store:\n'+'\n'.join(quotes)+'\nЭто часть доступных номиналов. Цены до промокода и списания монет.')
        elif known:
            parts.append('Регион Apple ID: '+', '.join(known)+'. Актуальную цену конкретного номинала пока не удалось подтвердить.')
        question=('Подскажите стоимость нужной покупки в валюте аккаунта и текущий баланс Apple ID.' if known else
                  'Для подбора пополнения App Store подскажите регион Apple ID, стоимость нужной покупки в валюте аккаунта и текущий баланс.')
        parts.append(question)
    if not parts:parts=['Уточните, пожалуйста, о каком сервисе или заказе идёт речь — хочу проверить именно ваш вопрос.']
    reply='\n\n'.join(parts)
    return {'reply':reply,'summary':'Свободный ответ не прошёл проверку. Сохранена только подтверждённая часть каталога и уточняющие вопросы. Остальные части обращения требуют проверки владельца.',
            'handoff':True,'source_ids':[],'research':{'topic':'none','products':[]},
            'evidence':{'status':'catalog_review','sources':[],
                        'diagnostic':scrub(str(reason))[:1100]
                        +'\nДанные: обычных тарифов='+str(len(rows))
                        +'; Apple status='+str(facts.get('appstore',{}).get('status','not_connected'))
                        +'; регионов='+str(len(facts.get('appstore',{}).get('regions',[])))
                        +'; выбранные регионы='+','.join(facts.get('appstore_selection',{}).get('requested_codes',[]))}}


def direct_apple_reply(facts, latest):
    """Plain top-up questions use stock facts, not historical region guesses."""
    words=set(_words(latest))
    if _families(_words(latest))-{'appstore'}:return None
    allowed=set('а как мне хочу пополнить пополнение баланс баланса appstore app store apple id icloud '
                'эп стора эпл апп стор аппстор айклауд у меня регион сша usa us американский '
                'турция турции tr турецкий на долларов доллары доллар usd try лир лиры '
                'сколько стоит какие варианты цены есть нужно пожалуйста подскажите будет стоить '
                'евро eur германия германии немецкий немецкого немецком регионе de germany в '
                'пополнения балансе эп сторе и или россии германии франции привет здравствуйте добрый день'.split())
    from .apple_matching import CURRENCIES, ALIASES
    allowed.update(c.lower() for c in CURRENCIES)
    allowed.update(code.lower() for code in ALIASES)
    allowed.update(w for names in ALIASES.values() for name in names for w in _words(name))
    allowed.update(('тенге','йен','иен','рублей','пополнить','хотел','бы','хотела','цену','для'))
    if any(w not in allowed and not w.isdigit() for w in words):return None
    if 'appstore' not in facts.get('catalog_selection',{}).get('mentioned_known_services',[]):return None
    selection=facts.get('appstore_selection',{})
    known=selection.get('requested_codes',[])
    if len(known)>1:
        from .apple_matching import regional_quotes
        quotes=regional_quotes(facts,latest)
        parts=[];diagnostics=[]
        for q in quotes:
            label={'DE':'Германия','RU':'Россия','FR':'Франция'}.get(q['region'],q['region'])
            if q.get('price') is not None:
                parts.append(f"{label}, {q['amount']:g} {q['currency']}: {q['price']} ₽")
            else:
                parts.append(f'{label}: нужно уточнить номинал или проверить его стоимость.')
                diagnostics.append(f"Регион={q['region']}; причина={q['reason']}")
        return {'reply':'\n'.join(parts),'summary':'Несколько регионов App Store; суммы сопоставлены по валютам номиналов, без пересчёта.',
                'handoff':bool(diagnostics),'source_ids':[],
                'evidence':{'status':'catalog_review' if diagnostics else 'catalog_direct','sources':[],
                            'diagnostic':'Этап: сопоставление нескольких регионов App Store. '+ '; '.join(diagnostics) if diagnostics else ''}}
    answer=verified_catalog_reply(facts,'')
    answer['evidence']={'status':'catalog_direct','sources':[]}
    quote=exact_quote(facts,latest)
    if quote:
        amount=quote['amount'];unit=quote['currency'];code=quote['region']
        label={'US':'США','DE':'Германии','TR':'Турции','GB':'Великобритании','RU':'России'}.get(code,code)
        if quote['price'] is not None:
            answer['reply']=f"Для Apple ID {label} есть пополнение на {amount:g} {unit} — {quote['price']} ₽. После оплаты код нужно погасить в App Store через профиль, пункт «Погасить подарочную карту или код»."
            answer['handoff']=False
        else:
            answer['reply']=f'Вы хотите пополнить Apple ID {label} на {amount:g} {unit}. Точную стоимость этого номинала сейчас подтвердить не получилось; потребуется проверка перед оформлением.'
            answer['evidence']={'status':'catalog_review','sources':[], 'diagnostic':
                f"Этап: подбор точного номинала App Store. Причина: {quote['reason']}. Регион={code}; сумма={amount:g} {unit}; статус источника={quote['status']}; совпадений={quote['matched']}; разных корректных цен={quote['price_count']}."}
    answer['summary']='Ответ по текущим региональным данным App Store. Регион не выбирался по истории модели.'
    return answer


def direct_workflow_reply(facts, latest):
    """Owner-approved transactional Krea FAQ; no manufacturer features or model."""
    from .workflow_policy import policies_for
    if not policies_for(facts):return None
    words=set(_words(latest))
    allowed=set('привет здравствуйте добрый день вечер как оформить оформления оформить подписку подписки подписка '
                'на креа krea какие тарифы тариф есть что нужно будет для от меня потребуется требуется '
                'какие данные нужны нужны ли почта пароль и сколько стоит стоят цены цена стоимость '
                'купить хочу мне у вас через магазин пожалуйста подскажите'.split())
    if words-allowed or not words&{'оформить','оформления','нужно','потребуется','данные','купить'}:
        return None
    answer=verified_catalog_reply(facts,'')
    answer.update(summary='Цены из магазина и условия Krea, прямо подтверждённые владельцем. Нужное ручное оформление выполняет Александр.',handoff=True)
    answer['evidence']={'status':'catalog_direct','sources':[]}
    return answer


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
        price=row.get('catalog_price_rub',row.get('customer_price_rub'))
        if type(price) is not int or price<=0 or not row.get('service') or not row.get('plan'):return None
        key=(row['service'],row['plan'])
        if key in quotes and quotes[key]!=price:return None
        quotes[key]=price
    allowed=set('привет здравствуйте добрый день вечер какие тарифы тариф тарифов цены цену стоит стоят стоимость сколько можно оформить купить заказать через вас у вас есть и еще ещё также хочу пополнить пополнение баланс баланса что нужно уточнить подскажите пожалуйста мне для спасибо а по на подписку подписки подписок это в во сервис'.split())
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
    services={name for name,_ in quotes}
    if len(services)==1 and 'appstore' not in facts.get('catalog_selection',{}).get('mentioned_known_services',[]):
        name=next(iter(services))
        result['reply']=f'Для {name} есть такие тарифы:\n'+'\n'.join(f'{plan}: {price} ₽' for (_,plan),price in quotes.items())
    result.update(summary='Ответ составлен из прочитанных цен магазина. Возможности тарифов и условия, не указанные в данных, не добавлялись.',handoff=False)
    result['evidence']={'status':'catalog_direct','sources':[]}
    return result
