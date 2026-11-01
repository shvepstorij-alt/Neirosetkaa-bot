"""Keep retail prices out of free-form feature generation."""
import re


def price_block(facts, question):
    if not re.search(r'возможност|сравни|отличи|функци',question,re.I):return ''
    if re.search(r'как\s+(?:оформ|оплат)|что\s+(?:нужно|потребуется)|возврат|заказ\s*№',question,re.I):return ''
    rows=facts.get('requested_catalog',[])
    if not rows or len(rows)>8:return ''
    quotes={}
    for row in rows:
        price=row.get('catalog_price_rub')
        name=row.get('service');plan=row.get('plan')
        if type(price) is not int or price<=0 or not name or not plan:return ''
        key=(name,plan)
        if key in quotes and quotes[key]!=price:return ''
        quotes[key]=price
    text='Цены в нашем боте:\n'+'\n'.join(f'{name} {plan}: {price} ₽' for (name,plan),price in quotes.items())
    return text if len(text)<=650 else ''


def compose(answer, block):
    """Reject an invalid feature section; never silently delete guessed claims."""
    text=answer.get('reply','')
    if re.search(r'[$€₽]|\b(?:руб(?:лей|ля|ль)?|RUB|USD|EUR|TRY|GBP)\b',text,re.I):
        return None,'Этап: разделение функций и цены. Модель добавила денежные сведения в блок функций; цены вставляет код из catalog_price_rub.'
    if re.search(r'@|https?://|/start|owner_confirmed|парол|пришл|опла[тч]|каталог|магазин',text,re.I):
        return None,'Этап: разделение функций и оформления. В блоке функций появились ник, ссылка или инструкция оформления; эти сведения нельзя генерировать в этом режиме.'
    reply=block+'\n\n'+text
    if len(reply)>1800:
        return None,'Этап: сборка ответа. Цены и описание превышают лимит 1800 символов; сократи описание функций, не удаляя сервисы.'
    return dict(answer,reply=reply),''
