"""Keep retail prices out of free-form feature generation."""
import re
import unicodedata


# Currency notation from public provider pages must not enter feature-only
# prose, even when the provider uses words or a currency other than USD/EUR.
# Keep codes paired with amounts to avoid treating CAD file support, ALL image
# models, or English "try" as a monetary claim.
_CURRENCY_CODES = '''AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN
 BHD BIF BMD BND BOB BOV BRL BSD BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP
 CNY COP COU CRC CUC CUP CVE CZK DJF DKK DOP DZD EGP ERN ETB EUR FJD FKP GBP
 GEL GHS GIP GMD GNF GTQ GYD HKD HNL HRK HTG HUF IDR ILS INR IQD IRR ISK JMD
 JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD MDL
 MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD
 OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD
 SHP SLE SLL SOS SRD SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS
 UAH UGX USD USN UYI UYU UYW UZS VED VES VND VUV WST XAF XAG XAU XBA XBB XBC
 XBD XCD XDR XOF XPD XPF XPT XSU XUA YER ZAR ZMW ZWL BTC ETH USDT USDC'''.split()
_CODE = r'(?:'+'|'.join(_CURRENCY_CODES)+r')'
_AMOUNT = r'\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)?'
_CODE_AMOUNT = re.compile(r'(?<!\w)(?:'+_CODE+r'\s*'+_AMOUNT+r'|'+_AMOUNT+r'\s*'+_CODE+r')(?!\w)', re.I)
_MONEY_WORDS = re.compile(
    r'(?<!\w)(?:руб(?:ль|ля|лей|ли|лю|лях|лями|ле|лем|лём)?|руб\.|'
    r'доллар(?:а|ов|ы|у|ами|ах|е|ом)?|долл\.?|бакс(?:а|ов|ы)?|евро|'
    r'юан(?:ь|я|ей|и|ю|ями|ях|е|ем)|[ий]ен(?:а|ы|у|е|ой|ами|ах)?|'
    r'тенге|дирхам(?:а|ов|ы|у|ами|ах|е|ом)?|лир(?:а|ы|у|е|ой|ами|ах)?|'
    r'фунт(?:а|ов|ы|у|ами|ах|е|ом)?|стерлинг(?:ов|а|и)?|'
    r'франк(?:а|ов|и|у|ами|ах|е|ом)?|песо|рупи(?:я|и|й|ю|ями|ях|ей)|'
    r'реал(?:а|ов|ы|у|ами|ах|е|ом)?|крон(?:а|ы|у|е|ой|ами|ах)?|'
    r'злот(?:ый|ых|ого|ые|ыми)|грив(?:на|ны|ен|ну|не|ной|нами|нах)|'
    r'dollars?|euros?|yuan|yen|rupees?|sterling|francs?|pesos?)(?!\w)', re.I)


def _has_money(text):
    if any(unicodedata.category(char) == 'Sc' for char in text):
        return True
    if (_MONEY_WORDS.search(text) or _CODE_AMOUNT.search(text)
            or re.search(r'(?<!\w)'+_AMOUNT+r'\s*р\.?(?!\w)',text,re.I)):
        return True
    # Standalone currency codes only in an explicit monetary context. This
    # also catches "расчёт в CAD" while preserving "экспорт CAD-файлов".
    return bool(re.search(r'(?<!\w)(?:цен[а-яё]*|стоимост[а-яё]*|валют[а-яё]*|'
                          r'расч[её]т[а-яё]*|price|currency)\b[^.!?\n]{0,40}(?<!\w)'+_CODE+r'(?!\w)', text, re.I))


def public_question(facts):
    """Project public intent labels; never forward arbitrary conversation text."""
    text=str(facts.get('conversation',{}).get('latest_client_message','')).strip()
    if len(text)>4000:return ''
    facets=(('API',r'\bapi\b|программ[а-яё]*\s+интерфейс'),('SSO',r'\bsso\b|един[а-яё]*\s+вход'),
        ('Длительность встреч',r'длительн|продолжительн|сколько\s+час'),
        ('Число участников',r'участник|participants'),('Хранилище и запись',r'хранилищ|запис[ьи]|storage|recording'),
        ('Изображения',r'изображен|картин[а-яё]*|\bimage'),('Видео',r'видео|\bvideo'),
        ('Документы и файлы',r'документ|файл|\bfiles?\b|pdf|таблиц'),
        ('Форматы',r'формат|\bformats?\b'),('Интеграции',r'интеграц|integrat'),
        ('Коммерческая лицензия',r'коммерческ|лицензи|commercial'),
        ('Конфиденциальность и безопасность',r'конфиденц|безопасност|privacy|security'),
        ('Модели',r'модел|\bmodels?\b'),('Лимиты использования',r'лимит|ограничен|limits?|quota'),
        ('Кредиты',r'кредит|units?|credits?'),('Качество и разрешение',r'качеств|разрешени|апскейл|upscal'),
        ('Обучение LoRA',r'lora|обучени'),('Водяные знаки',r'водян|watermark'),
        ('Мобильное приложение',r'мобил|android|ios|iphone'),('Браузер',r'браузер|browser'),
        ('Экспорт',r'экспорт|export'),('Совместная работа',r'команд|совместн|team'),
        ('Сравнение тарифов',r'сравни|отличи|разниц'),('Состав тарифа',r'возможност|функци|что\s+входит'))
    facets+= (('Доступность по странам и региональные ограничения',r'регион|стран|\bрф\b|росси'),)
    labels=[label for label,pattern in facets if re.search(pattern,text,re.I)]
    specifics=('SVG','PDF','DOCX','XLSX','CSV','PNG','JPG','JPEG','WEBP','GIF','MP4','MOV','AVI','MP3','WAV','OGG',
        'JSON','XML','HTML','PPTX','TXT','PSD','HEIC','STL','GLB','FBX','Windows','macOS','Linux','Android','iOS',
        'Chrome','Safari','Firefox','Edge','Slack','Google Drive','Dropbox','OneDrive','Excel','PowerPoint','Word',
        'LoRA','OAuth','SAML','SCIM','webhook','Zapier','MCP','RAG','OCR','SDK','REST','GraphQL','2FA','TOTP',
        '4K','8K','22K','1080p','720p')
    terms=[term for term in specifics if re.search(r'(?<!\w)'+re.escape(term)+r'(?!\w)',text,re.I)]
    if terms:labels.append('Уточнения: '+', '.join(terms))
    quantities=re.findall(r'(?<!\w)(\d{1,4})\s*(участник[а-яё]*|человек|минут[а-яё]*|час[а-яё]*|секунд[а-яё]*|мб|гб|MB|GB)(?!\w)',text,re.I)
    if quantities:labels.append('Требования: '+', '.join(number+' '+unit.lower() for number,unit in quantities[:6]))
    return 'Публичная часть запроса: '+'; '.join(labels)+'.' if labels else ''


def price_block(facts, question, topic=None):
    if topic not in ('features','comparison','plans') and not re.search(r'возможност|сравни|отлич[аи]|разниц|функци',question,re.I):return ''
    if re.search(r'как\s+(?:оформ|оплат)|что\s+(?:нужно|потребуется)|возврат|заказ\s*№',question,re.I):return ''
    from .catalog_order import ordered_rows
    rows=ordered_rows(facts.get('requested_catalog',[]))
    if not rows or len(rows)>16:return ''
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
    """Legacy block guard. Atomic mode filters whole reviewed records upstream."""
    text=answer.get('reply','')
    if _has_money(text) or re.search(r'скидк|сниженн[а-яё]*\s+ставк|discount|снижают\s+(?:месячную\s+)?стоимость|позволяют\s+сэкономить',text,re.I):
        return None,'Этап: разделение функций и цены. Модель добавила денежные сведения в блок функций; цены вставляет код из catalog_price_rub.'
    # Billing cadence is a public plan property, not a customer instruction.
    # Leave its truth/plan binding to the content audit; keep transactions out.
    if re.search(r'@|https?://|/start|owner_confirmed|парол|пришл|'
                 r'оплатите|оплачивайте|(?:после|до|для)\s+оплат|'
                 r'\b(?:API|доступ|использование)\b[^.!?\n]{0,35}опла[тч]|'
                 r'опла[тч][^.!?\n]{0,35}(?:отдельно|заказ|карт[а-яё]*)|'
                 r'(?:откройте|перейдите|выберите)[^.!?\n]{0,50}(?:каталог|магазин)',text,re.I):
        return None,'Этап: разделение функций и оформления. В блоке функций появились ник, ссылка или инструкция оформления; эти сведения нельзя генерировать в этом режиме.'
    reply=block+'\n\n'+text
    if len(reply)>1800:
        return None,'Этап: сборка ответа. Цены и описание превышают лимит 1800 символов; сократи описание функций, не удаляя сервисы.'
    return dict(answer,reply=reply),''
