"""Owner-confirmed retail durations, not provider billing/renewal policy."""
import re


def enrich(facts):
    from .catalog_context import _families,_words,_credit_product
    def row(item):
        if not isinstance(item,dict):return item
        item=dict(item)
        if _credit_product(item) or item.get('duration') or item.get('billing_period'):return item
        plan=str(item.get('plan',''))
        if re.search(r'год|year|annual|12\s*(?:мес|month)',plan,re.I):return item
        families=_families(_words(str(item.get('service',''))+' '+str(item.get('service_key',''))))
        normalized=re.sub(r'[\s_-]+','',plan.casefold())
        normalized=re.sub(r'(?<=\d)[×хx]','',normalized)
        valid=('chatgpt' in families and normalized in ('go','plus','pro','pro5','pro20','pro50','promax')
               or 'claude' in families and normalized in ('pro','max','max5','max20')
               or 'krea' in families and normalized in ('basic','pro'))
        if valid:
            item.update(duration='1 month',duration_source='owner_confirmed_2026_11_04')
        return item
    result=dict(facts)
    for field in ('catalog','requested_catalog'):
        if isinstance(facts.get(field),list):result[field]=[row(r) for r in facts[field]]
    result['owner_offer_terms']={'confirmed_at':'2026-11-04',
        'scope':'ChatGPT Go/Plus/Pro, Claude Pro/Max, Krea Basic/Pro: один месяц; явно годовые и отдельно заданные сроки не заменяются.',
        'limits':'Срок предоставляемой подписки не означает автопродление, ежемесячное списание, гарантию доступа или срок активации.'}
    return result
