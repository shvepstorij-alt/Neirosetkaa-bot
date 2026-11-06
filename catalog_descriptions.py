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
        return {'text':'Условия этого предложения, состав услуги и порядок оформления уточняются до оплаты.',
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
                entry=dict(base,text=base['text']+' Годовой вариант предложения магазина; период указан в названии товара.')
        if entry is None and key=='openrouter':
            entry={'text':'Пополнение баланса OpenRouter для оплаты использования моделей. Объём доступной работы зависит от выбранной модели, тарификации и фактического расхода; это не безлимитная подписка.',
                   'status':'shop_scope_only'}
        if entry is None and key=='appstore':
            entry={'text':'Номинал регионального предложения Apple. Выбирайте только для соответствующего региона Apple ID; цену и доступность нужно проверить в текущем региональном каталоге.',
                   'status':'shop_scope_only'}
        if entry is None and key=='telegram' and re.fullmatch(r'(?:3|6|12)\s+месяц(?:а|ев)',str(plan_name).strip(),re.I):
            entry={'text':'Telegram Premium на срок, указанный в названии предложения. Дополнительные функции включают расширенные лимиты, расшифровку голосовых, перевод и премиальное оформление; нужна поддерживаемая версия приложения.',
                   'status':'shop_scope_only'}
        if entry is None and key in ('other_service','heygen','make') and re.fullmatch(r'\d+(?:[.,]\d+)?\s*[$€]?',str(plan_name).strip()):
            entry={'text':'Индивидуальный вариант оплаты выбранного сервиса. Название с суммой не подтверждает отдельный публичный тариф или набор функций; состав услуги согласовывается перед оплатой.',
                   'status':'shop_scope_only'}
        if entry is None:
            entry={'text':'Состав этого варианта и доступные функции необходимо уточнить перед оплатой. Название товара магазина не подтверждает соответствие отдельному публичному тарифу сервиса.',
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
