"""Exact Apple region and denomination identities, without price conversion."""
import re
from decimal import Decimal

# Names help customers; codes and currency identities always come from stock rows.
REGIONS = {
 'US':'сша|америка|американский|американского|американском|usa|united states',
 'GB':'великобритания|великобритании|британия|британский|британского|uk|united kingdom',
 'TR':'турция|турции|турцию|турецкий|турецкого|турецком|turkey|türkiye',
 'RU':'россия|россии|российский|российского|russia',
 'DE':'германия|германии|германию|немецкий|немецкого|немецком|germany',
 'AE':'оаэ|эмираты|эмиратах|эмиратов|united arab emirates|uae',
 'AT':'австрия|австрии|австрийский|австрийского|austria',
 'AU':'австралия|австралии|австралийский|австралийского|australia',
 'BE':'бельгия|бельгии|бельгийский|бельгийского|belgium',
 'BR':'бразилия|бразилии|бразильский|бразильского|brazil',
 'CA':'канада|канады|канаде|канадский|канадского|canada',
 'ES':'испания|испании|испанский|испанского|spain',
 'FR':'франция|франции|французский|французского|france',
 'IE':'ирландия|ирландии|ирландский|ирландского|ireland',
 'IN':'индия|индии|индийский|индийского|india',
 'IT':'италия|италии|итальянский|итальянского|italy',
 'JP':'япония|японии|японский|японского|japan',
 'KZ':'казахстан|казахстана|казахстане|казахстанский|kazakhstan',
 'LU':'люксембург|люксембурга|люксембурге|luxembourg',
 'NL':'нидерланды|нидерландах|нидерландов|голландия|голландии|netherlands',
 'PL':'польша|польши|польше|польский|польского|poland',
 'SA':'саудовская аравия|саудовской аравии|saudi arabia',
 'CH':'швейцария|швейцарии|швейцарский|switzerland',
 'SG':'сингапур|сингапура|сингапуре|singapore',
 'HK':'гонконг|гонконга|гонконге|hong kong',
 'NZ':'новая зеландия|новой зеландии|new zealand',
 'MX':'мексика|мексики|мексике|mexico',
 'KR':'южная корея|южной кореи|south korea',
 'SE':'швеция|швеции|sweden','NO':'норвегия|норвегии|norway',
 'DK':'дания|дании|denmark','FI':'финляндия|финляндии|finland',
 'PT':'португалия|португалии|portugal','ZA':'юар|south africa',
}
ALIASES={code:tuple(names.split('|')) for code,names in REGIONS.items()}
CURRENCIES=('USD','EUR','TRY','GBP','AED','AUD','BRL','CAD','INR','JPY','KZT','PLN',
            'SAR','RUB','CHF','SGD','HKD','NZD','MXN','KRW','SEK','NOK','DKK','ZAR')
TOKEN=r'(?:'+ '|'.join(CURRENCIES)+r'|\$|€|₺|£|евро|доллар(?:ов|а|ы)?|лир(?:ы)?|тенге|иен(?:ы)?|йен(?:ы)?)'
NUMBER=r'\d+(?:[.,]\d{1,2})?'
PAIR=re.compile(r'(?<![\w.,])('+NUMBER+r')\s*('+TOKEN+r')(?!\w)|(?<!\w)('+TOKEN+r')\s*('+NUMBER+r')(?![\d.,])',re.I)

def words(text):return tuple(re.findall(r'[^\W_]+',str(text).casefold()))

def region_codes(text, stock=False):
    w=words(text);found=set()
    for code,names in ALIASES.items():
        for name in names:
            p=words(name)
            if any(w[i:i+len(p)]==p for i in range(len(w)-len(p)+1)):
                found.add(code)
    # Uppercase explicit codes are supported without interpreting ordinary
    # English words "in", "at", "be", "it" as country requests.
    for token in re.findall(r'\b[A-Z]{2,3}\b',str(text)):
        canonical={'UK':'GB','USA':'US'}.get(token,token)
        if canonical in REGIONS:found.add(canonical)
    for token in re.findall(r'\bрегион(?:е|а)?\s+([a-z]{2,3})\b',str(text),re.I):
        canonical={'UK':'GB','USA':'US'}.get(token.upper(),token.upper())
        if canonical in REGIONS:found.add(canonical)
    tail=str(text).split('|')[-1].strip().upper()
    if stock and re.fullmatch(r'[A-Z]{2,3}',tail):
        found.add({'UK':'GB','USA':'US'}.get(tail,tail))
    return found

def canonical_currency(token):
    t=token.casefold()
    if t=='евро' or t=='€':return 'EUR'
    if t=='₺' or t.startswith('лир'):return 'TRY'
    if t=='£':return 'GBP'
    if t.startswith('доллар') or t=='$':return '$'
    if t=='тенге':return 'KZT'
    if t.startswith(('иен','йен')):return 'JPY'
    return t.upper()

def amounts(text):
    return {(Decimal((a or d).replace(',','.')),canonical_currency(b or c)) for a,b,c,d in PAIR.findall(str(text))}

def nominal(text):
    """Allow harmless product label suffixes, never extra numbers or bundles."""
    text=str(text).strip()
    m=PAIR.match(text)
    if not m:return None
    suffix=text[m.end():].strip()
    if suffix and not re.fullmatch(r'(?:Apple\s+)?(?:gift\s*card|подарочн[а-яё]*\s+карт[а-яё]*)',suffix,re.I):return None
    return next(iter(amounts(m.group())))

def exact_quote(facts,text):
    selection=facts.get('appstore_selection',{})
    codes=selection.get('requested_codes',[]);wanted=amounts(text)
    if len(codes)!=1 or len(wanted)!=1:return None
    amount,unit=next(iter(wanted));entries=[]
    for region in selection.get('matched_regions',[]):
        for row in region.get('denoms',[]):
            identity=nominal(row.get('nominal',''))
            if identity:entries.append((identity,row.get('price_rub')))
    # A bare dollar symbol is ambiguous across dollar currencies. Resolve only
    # within the explicitly selected region, using its actual denomination rows.
    if unit=='$':
        units={u for (_,u),_ in entries if u in ('USD','CAD','AUD','NZD','HKD','SGD','MXN','$')}
        if len(units)==1:unit=next(iter(units))
        elif codes==['US']:unit='USD'
    matched=[p for (a,u),p in entries if a==amount and (u==unit or u=='$' and unit=='USD' and codes==['US'])]
    prices={p for p in matched if type(p) is int and p>0}
    status=facts.get('appstore',{}).get('status','not_connected')
    reason=('источник недоступен' if status not in ('available','truncated') else
            'получены разные цены одного номинала' if len(prices)>1 else
            'у совпавшего номинала нет корректной цены' if matched and not prices else
            'в полученной выборке нет точного совпадения номинала; отсутствие товара не подтверждено' if not matched else '')
    return {'region':codes[0],'amount':amount,'currency':unit,'price':next(iter(prices)) if not reason else None,
            'reason':reason,'status':status,'matched':len(matched),'price_count':len(prices)}
