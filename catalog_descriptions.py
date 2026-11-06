"""Reviewed description snapshot, not a model or live verification service.

Only desc fields may be changed. Owners apply this explicitly/versioned; do not
call after every DB load because that would erase later manual description edits.
"""
import json
import re
import unicodedata
from pathlib import Path

DATA_PATH = Path(__file__).with_suffix('.json')
_DATA = json.loads(DATA_PATH.read_text(encoding='utf-8'))
REVIEW_VERSION = _DATA['version']
CHECKED_DATE = _DATA['checked_date']


def _identity(value):
    value=unicodedata.normalize('NFKC',str(value or '')).casefold()
    return re.sub(r'[^\w]+',' ',value).strip()


def describe(service_key, service_name, plan_name=None):
    """Return a source-labelled safe description; preserve every SKU identity.

    Exact aliases only. Dollar denominations and unknown plan labels are not
    treated as official subscription tiers or granted inherited premium features.
    """
    identities={_identity(service_key),_identity(service_name)}-{''}
    matches=[(key,row) for key,row in _DATA['services'].items()
             if identities & {_identity(v) for v in [key,*row.get('aliases',[])]}]
    if len(matches)!=1:
        return {'text':'Помощь с оформлением подписки на выбранный сервис. Для индивидуального заказа нужны официальный сайт, название плана и срок подписки.',
                'source_urls':[],'status':'needs_confirmation','checked_date':CHECKED_DATE,
                'service_id':None,'limitation':'Сервис не сопоставлен однозначно с проверенным справочником.'}
    key,row=matches[0]
    entry=row['service']
    if plan_name is not None:
        entry=next((value for label,value in row.get('plans',{}).items()
                    if _identity(label)==_identity(plan_name)),None)
        # A clearly named annual SKU keeps its own name and price; only the
        # qualitative base-plan description is shared. Do not reinterpret x2
        # bundles or arbitrary amounts as annual subscriptions.
        annual=re.fullmatch(r'(.+?)\s+на\s+(?:1\s+)?год',str(plan_name).strip(),re.I)
        if entry is None and annual:
            base=next((value for label,value in row.get('plans',{}).items()
                       if _identity(label)==_identity(annual[1])),None)
            if base:
                entry=dict(base,text=base['text']+' Годовая подписка на 12 месяцев.')
        if entry is None and key in ('heygen','make','other_service','openrouter') and re.fullmatch(r'\d+(?:[.,]\d+)?\s*[$€]?',str(plan_name).strip()):
            entry=dict(row.get('default_plan',row['service']),status='shop_scope_only')
        if entry is None and isinstance(row.get('default_plan'),dict):
            entry=row['default_plan']
        if entry is None:
            entry={'text':row['service']['text'],
                   'status':'needs_confirmation'}
    return {'text':entry['text'],'source_urls':list(entry.get('source_urls',row.get('source_urls',[]))),
            'status':entry.get('status','needs_confirmation'),'checked_date':CHECKED_DATE,
            'service_id':key,'limitation':entry.get('limitation',row.get('limitation',''))}


def apply_catalog(catalog):
    """Apply the reviewed snapshot explicitly; return changed (key, plan, text)."""
    changed=[]
    for key,service in catalog.items():
        if not isinstance(service,dict):continue
        result=describe(key,service.get('name',''))
        if service.get('desc')!=result['text']:
            service['desc']=result['text'];changed.append((key,'',result['text']))
        for plan in service.get('plans',[]):
            if not isinstance(plan,dict):continue
            name=str(plan.get('name',''))
            result=describe(key,service.get('name',''),name)
            if plan.get('desc')!=result['text']:
                plan['desc']=result['text'];changed.append((key,name,result['text']))
    return changed
