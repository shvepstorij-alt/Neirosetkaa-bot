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
    from .catalog_context import _families,_words
    # Regional gift cards have their own region/nominal checker. Never bind
    # their RUB prices to an ordinary subscription merely by numeric equality.
    rows = [row for row in customer_facts(facts).get('requested_catalog', [])
            if 'appstore' not in (_families(_words(row.get('service','')))
                                  | _families(_words(row.get('service_key',''))))]
    issues = []
    checked = 0
    current_service = ''
    names = {str(r.get('service', '')).casefold() for r in rows} - {''}
    anchors=[(re.compile(r'(?<!\w)'+re.escape(name)+r'(?!\w)',re.I),name) for name in names]
    anchors.append((re.compile(r'app\s*store|appstore|apple(?:\s*id)?|icloud|эп\s*стор|апп\s*стор|айклауд',re.I),'__apple__'))
    for line in reply.splitlines():
        lower = line.casefold()
        found=[(m.start(),m.end(),name) for pattern,name in anchors for m in pattern.finditer(lower)]
        # A longer service title can contain another title. Prefer the complete
        # title at that occurrence; do not mistake it for two product anchors.
        found=sorted(anchor for anchor in found if not any(
            other[0]<=anchor[0] and anchor[1]<=other[1] and other[1]-other[0]>anchor[1]-anchor[0]
            for other in found))
        monies=list(MONEY.finditer(line))
        last_price_end=0
        for money in monies:
            amount = Decimal(re.sub(r'[ \u00a0\u202f]', '', money[1]).replace(',', '.'))
            preceding=[anchor for anchor in found if anchor[1]<=money.start()]
            service=preceding[-1][2] if preceding else current_service
            if service=='__apple__' or not rows:
                last_price_end=money.end()
                continue
            # Bind the price to a nearby preceding label, never to a plan
            # mentioned later in a feature description ("unlike Basic...").
            # A previous monetary quote is a hard boundary: its plan must not
            # accidentally own the next amount in the same sentence or list.
            prefix=lower[last_price_end:money.start()].replace('**','').replace('__','')
            matches=[];labels=[]
            for row in rows:
                if service and str(row.get('service','')).casefold()!=service:continue
                label=str(row.get('plan','')).strip().casefold()
                if not label:continue
                variants=[label]
                if label.endswith(' на год'):variants.append(label[:-7])
                for variant in variants:
                    for match in re.finditer(r'(?<!\w)'+re.escape(variant)+r'(?!\w)',prefix):
                        labels.append((match.start(),match.end(),label))
                        tail=prefix[match.end():]
                        year=bool(re.search(r'на\s+год|годов|annual|year',tail))
                        if variant!=label and not year:continue
                        clean=re.sub(r'\b(?:на\s+год|годов[а-яё]*|месячн[а-яё]*|месяц|тариф|план|стоит|цена|за|в|annual|yearly|monthly)\b',' ',tail)
                        if re.search(r'[\w]',clean):continue
                        if year and variant==label and not label.endswith(' на год'):
                            if any(str(r.get('plan','')).casefold()==label+' на год' and r.get('service')==row.get('service') for r in rows):continue
                        matches.append((match.start(),match.end(),row,tail))
            visible_labels=[label for label in set(labels) if not any(
                other[0]<=label[0] and label[1]<=other[1] and other[1]-other[0]>label[1]-label[0]
                for other in labels)]
            ambiguous_labels=any(left[2]!=right[2] and left[1]<=right[0]
                and re.search(r'\bи\b|\bили\b|[,/+]',prefix[left[1]:right[0]])
                and not re.search(r'[.!?;]',prefix[left[1]:right[0]])
                for left in visible_labels for right in visible_labels)
            local_anchors=[a for a in preceding if a[0]>=last_price_end]
            if len({a[2] for a in local_anchors})>1:
                between=lower[local_anchors[0][1]:local_anchors[-1][0]]
                ambiguous_labels=ambiguous_labels or bool(re.search(r'\bи\b|\bили\b|[,/+]',between)
                    and not re.search(r'[.!?;]',between))
            if ambiguous_labels:
                issues.append(f'Этап: привязка цены к тарифу. Цена ответа={amount}; несколько сервисов или тарифов перечислены перед одной суммой. Укажи отдельную рублёвую цену для каждой пары сервис/тариф.')
                last_price_end=money.end()
                continue
            if matches:
                nearest=max((start,end) for start,end,_,_ in matches)
                matches=[m for m in matches if m[:2]==nearest]
            candidates=[m[2] for m in matches]
            if len({r.get('service') for r in candidates})>1:
                issues.append(f'Этап: привязка цены к тарифу. Цена ответа={amount}; один ярлык соответствует нескольким сервисам. Укажи сервис и тариф непосредственно перед каждой рублёвой ценой.')
                last_price_end=money.end()
                continue
            prices = {r.get('catalog_price_rub', r.get('customer_price_rub',r.get('base_price_rub'))) for r in candidates}
            prices = {Decimal(str(p)) for p in prices if type(p) in (int, float) and p > 0}
            if len(prices)>1:
                issues.append('Этап: привязка цены к тарифу. Тариф='+'/'.join(sorted({str(r['plan']) for r in candidates}))
                    +f'; цена ответа={amount}; выбранный тариф имеет несколько разных цен в данных магазина: '+','.join(map(str,sorted(prices)))+'. Единственная подтверждённая цена не установлена.')
            elif prices:
                checked += 1
                if amount not in prices:
                    issues.append('Этап: привязка цены к тарифу. Тариф=' + '/'.join(sorted({str(r['plan']) for r in candidates}))
                                  + f'; цена ответа={amount}; допустимые цены=' + ','.join(map(str, sorted(prices))))
            else:
                issues.append(f'Этап: привязка цены к тарифу. Цена ответа={amount}; не удалось однозначно сопоставить предшествующий ярлык подтверждённому тарифу. Сервис={service or "не определён"}. Укажи сервис и тариф перед каждой рублёвой ценой; совпадение суммы с общим набором цен недостаточно.')
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
            last_price_end=money.end()
        if found:current_service=found[-1][2]
    return {'checked_prices': checked, 'issues': list(dict.fromkeys(issues))}
