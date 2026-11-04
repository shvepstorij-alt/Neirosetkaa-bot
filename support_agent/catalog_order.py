"""Stable ascending retail prices within each service, without mutating facts."""


def ordered_rows(rows):
    groups={}
    indexed=[]
    for index,row in enumerate(rows):
        if not isinstance(row,dict):continue
        identity=(row.get('service'),row.get('service_key'))
        group=groups.setdefault(identity,len(groups))
        value=row.get('catalog_price_rub',row.get('customer_price_rub'))
        price=value if type(value) is int and value>0 else float('inf')
        indexed.append((group,price,index,row))
    return [entry[3] for entry in sorted(indexed,key=lambda entry:entry[:3])]
