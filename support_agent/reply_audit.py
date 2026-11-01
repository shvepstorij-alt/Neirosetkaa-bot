"""Deterministic catalogue checks; never use manufacturer terms to infer shop duration."""
import re
from decimal import Decimal

MONEY = re.compile(r'(\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d{1,2})?)\s*(?:₽|руб(?:лей|ля|ль)?\b)', re.I)


def apple_audit(reply,facts,question):
    from .apple_matching import exact_quote
    from .catalog_context import ALIASES,_families,_words
    quote=exact_quote(facts,question)
    result={'quote':quote,'prices':[],'issues':[]}
    if not quote:return result
    names=set()
    for row in facts.get('requested_catalog',[]):
        families=_families(_words(str(row.get('service',''))+' '+str(row.get('service_key',''))))
        if 'appstore' in families:continue
        names.update(str(row.get(k,'')) for k in ('service','service_key') if row.get(k))
        for family in families:names.update(ALIASES.get(family,()))
    anchors=[(re.compile(r'(?<!\w)'+re.escape(name)+r'(?!\w)',re.I),'regular') for name in names]
    anchors.append((re.compile(r'app\s*store|apple(?:\s*id)?|icloud|эп\s*стор|апп\s*стор|айклауд',re.I),'apple'))
    scope=None if names else 'apple'
    for segment in re.split(r'[\n;]',reply):
        matches=sorted((m.start(),m.end(),kind) for pattern,kind in anchors for m in pattern.finditer(segment))
        for money in MONEY.finditer(segment):
            preceding=[m for m in matches if m[1]<=money.start()]
            local=preceding[-1][2] if preceding else scope
            if local!='apple':continue
            amount=Decimal(re.sub(r'[ \u00a0\u202f]','',money[1]).replace(',','.'))
            result['prices'].append(amount)
            if quote['price'] is None or amount!=Decimal(str(quote['price'])):
                result['issues'].append(f"Этап: проверка точной цены App Store. Регион={quote['region']}; номинал={quote['amount']} {quote['currency']}; ожидаемая цена={quote['price'] if quote['price'] is not None else 'не подтверждена'}; цена в ответе={amount}. Причина: {quote['reason'] or 'цена не соответствует выбранному региону, валюте и номиналу'}.")
        if matches:scope=matches[-1][2]
    return result


def catalogue_audit(reply, facts):
    from .catalog_context import customer_facts
    rows = customer_facts(facts).get('requested_catalog', [])
    issues = []
    checked = 0
    current_service = ''
    names = {str(r.get('service', '')).casefold() for r in rows} - {''}
    for line in reply.splitlines():
        lower = line.casefold()
        mentioned = [name for name in names if name in lower]
        if len(mentioned)>1:
            current_service=''
            continue
        if len(mentioned) == 1:
            current_service = mentioned[0]
        monies=list(MONEY.finditer(line))
        if len(monies)>1:
            continue  # Ambiguous inline lists remain for the semantic reviewer.
        for money in monies:
            amount = Decimal(re.sub(r'[ \u00a0\u202f]', '', money[1]).replace(',', '.'))
            # Bind the price to a nearby preceding label, never to a plan
            # mentioned later in a feature description ("unlike Basic...").
            prefix=lower[:money.start()].replace('**','').replace('__','')
            matches=[]
            for row in rows:
                if current_service and str(row.get('service','')).casefold()!=current_service:continue
                label=str(row.get('plan','')).strip().casefold()
                if not label:continue
                variants=[label]
                if label.endswith(' на год'):variants.append(label[:-7])
                for variant in variants:
                    for match in re.finditer(r'(?<!\w)'+re.escape(variant)+r'(?!\w)',prefix):
                        tail=prefix[match.end():]
                        year=bool(re.search(r'на\s+год|годов|annual|year',tail))
                        if variant!=label and not year:continue
                        clean=re.sub(r'\b(?:на\s+год|годов[а-яё]*|месячн[а-яё]*|месяц|тариф|план|стоит|цена|за|в|annual|yearly|monthly)\b',' ',tail)
                        if re.search(r'[\w]',clean):continue
                        if year and variant==label and not label.endswith(' на год'):
                            if any(str(r.get('plan','')).casefold()==label+' на год' and r.get('service')==row.get('service') for r in rows):continue
                        matches.append((match.start(),match.end(),row,tail))
            if matches:
                nearest=max((start,end) for start,end,_,_ in matches)
                matches=[m for m in matches if m[:2]==nearest]
            candidates=[m[2] for m in matches]
            if len({r.get('service') for r in candidates})>1:
                continue
            prices = {r.get('catalog_price_rub', r.get('customer_price_rub',r.get('base_price_rub'))) for r in candidates}
            prices = {Decimal(str(p)) for p in prices if type(p) in (int, float) and p > 0}
            if prices:
                checked += 1
                if amount not in prices:
                    issues.append('Этап: привязка цены к тарифу. Тариф=' + '/'.join(sorted({str(r['plan']) for r in candidates}))
                                  + f'; цена ответа={amount}; допустимые цены=' + ','.join(map(str, sorted(prices))))
            suffix=re.match(r'[\s*_()/-]*(?:в\s+месяц|/\s*мес|ежемесяч\w*|месячн\w*|на\s+год|в\s+год|/\s*год|годов\w*)',lower[money.end():])
            scope=(matches[0][3] if matches else '')+' '+(suffix[0] if suffix else '')
            period = 'month' if re.search(r'в\s+месяц|/\s*мес|ежемесяч|месячн', scope) else (
                'year' if re.search(r'на\s+год|в\s+год|/\s*год|годов', scope) else '')
            if period and candidates:
                values = ' '.join(str(r.get(k, '')) for r in candidates for k in ('plan', 'billing_period', 'duration')).casefold()
                confirmed = bool(re.search(r'месяц|месяч|monthly|\bmonth\b', values)) if period == 'month' else bool(
                    re.search(r'на\s+год|годов|year|annual', values))
                if not confirmed:
                    issues.append('Этап: срок товара магазина. Тариф=' + '/'.join(sorted({str(r['plan']) for r in candidates}))
                                  + f'; заявлен период={period}; в названии и полях billing_period/duration он не подтверждён.')
    return {'checked_prices': checked, 'issues': list(dict.fromkeys(issues))}
