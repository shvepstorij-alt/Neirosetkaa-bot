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
