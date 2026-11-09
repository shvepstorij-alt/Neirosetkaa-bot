"""Stable ascending retail prices in customer lists, without mutating facts."""

import re


def ordered_rows(rows):
    indexed=[]
    for index,row in enumerate(rows):
        if not isinstance(row,dict):continue
        value=row.get('catalog_price_rub',row.get('customer_price_rub'))
        price=value if type(value) is int and value>0 else float('inf')
        indexed.append((price,index,row))
    return [entry[2] for entry in sorted(indexed,key=lambda entry:entry[:2])]


_PRICE_LINE=re.compile(r'^\s*(?!\d+[.)]\s)[^\n]{1,120}?\b(\d[\d ]{0,10})\s*₽\s*$')


def sort_price_lines(reply):
    """Sort plain consecutive ruble quote lines; leave prose and steps intact."""
    lines=str(reply).split('\n')
    start=0
    while start<len(lines):
        match=_PRICE_LINE.match(lines[start])
        if not match:
            start+=1
            continue
        end=start
        priced=[]
        while end<len(lines):
            found=_PRICE_LINE.match(lines[end])
            if not found:break
            priced.append((int(found.group(1).replace(' ','')),end-start,lines[end]))
            end+=1
        if len(priced)>1:
            lines[start:end]=[line for _,_,line in sorted(priced)]
        start=end
    return '\n'.join(lines)
