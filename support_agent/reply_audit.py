"""Deterministic catalogue checks; never use manufacturer terms to infer shop duration."""
import re
from decimal import Decimal

MONEY = re.compile(r'(\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d{1,2})?)\s*(?:₽|руб(?:лей|ля|ль)?\b)', re.I)


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
            candidates = [r for r in rows if (not current_service or str(r.get('service', '')).casefold() == current_service)
                          and str(r.get('plan', '')).strip()
                          and re.search(r'(?<!\w)' + re.escape(str(r['plan']).casefold()) + r'(?!\w)', lower)]
            # Prefer the longest plan name: "Pro на год" must not match "Pro".
            if candidates:
                longest = max(len(str(r['plan'])) for r in candidates)
                candidates = [r for r in candidates if len(str(r['plan'])) == longest]
            if len({r.get('service') for r in candidates})>1:
                continue
            prices = {r.get('catalog_price_rub', r.get('customer_price_rub',r.get('base_price_rub'))) for r in candidates}
            prices = {Decimal(str(p)) for p in prices if type(p) in (int, float) and p > 0}
            if prices:
                checked += 1
                if amount not in prices:
                    issues.append('Этап: привязка цены к тарифу. Тариф=' + '/'.join(sorted({str(r['plan']) for r in candidates}))
                                  + f'; цена ответа={amount}; допустимые цены=' + ','.join(map(str, sorted(prices))))
            period = 'month' if re.search(r'в\s+месяц|/\s*мес|ежемесяч', lower) else (
                'year' if re.search(r'на\s+год|в\s+год|/\s*год|годов', lower) else '')
            if period and candidates:
                values = ' '.join(str(r.get(k, '')) for r in candidates for k in ('plan', 'billing_period', 'duration')).casefold()
                confirmed = bool(re.search(r'месяц|месяч|monthly|\bmonth\b', values)) if period == 'month' else bool(
                    re.search(r'на\s+год|годов|year|annual', values))
                if not confirmed:
                    issues.append('Этап: срок товара магазина. Тариф=' + '/'.join(sorted({str(r['plan']) for r in candidates}))
                                  + f'; заявлен период={period}; в названии и полях billing_period/duration он не подтверждён.')
    return {'checked_prices': checked, 'issues': list(dict.fromkeys(issues))}
