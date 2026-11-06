# -*- coding: utf-8 -*-
"""Compatibility adapter for the explicitly reviewed description snapshot.

No model versions, pricing guesses or claims of live freshness are generated.
The root application applies this only by owner action/versioned migration.
"""
from catalog_descriptions import describe, REVIEW_VERSION, CHECKED_DATE


def build_descriptions(catalog):
    result={}
    for key,service in catalog.items():
        if not isinstance(service,dict):continue
        name=service.get('name','')
        result[key]={'service':describe(key,name)['text'],
                     'plans':{str(p.get('name','')):describe(key,name,p.get('name',''))['text']
                              for p in service.get('plans',[]) if isinstance(p,dict)}}
    return result


# Legacy /apply_desc imports this value after the runtime catalogue has loaded.
from config import SHOP_CATALOG
DESCRIPTIONS = build_descriptions(SHOP_CATALOG)
