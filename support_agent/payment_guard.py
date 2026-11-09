"""Narrow intake guard for unverified payment data and a mismatched item."""
import re


def reply(text):
    value = str(text or '')
    # Address risk only when payment context and a request to invent or use
    # arbitrary details are both explicit. A different legitimate address is
    # not itself proof of falsification.
    address = bool(re.search(r'адрес|индекс|billing\s*address|postal\s*code', value, re.I))
    payment = bool(re.search(r'оплат|плат[её]ж|карт[а-яё]*|checkout|billing', value, re.I))
    fabricated = bool(re.search(r'придум|выдум|фальшив|фиктив|несуществ|любо[йе]|произвольн|поддельн', value, re.I))
    # Ask for evidence only where the client explicitly reports a mismatch;
    # the mere mention of two products is insufficient.
    item = bool(re.search(r'товар|позиц|сч[её]т|заказ|оплат|плат[её]ж', value, re.I))
    mismatch = bool(re.search(r'(?:друго[еий]|не\s+(?:то|тот|та|совпада|похож)|отлича|расхожд|несовпад|неверн|неправильн)[^.!?\n]{0,40}(?:названи|товар|позици|продукт)|(?:названи|товар|позици|продукт)[^.!?\n]{0,50}(?:друго[еий]|не\s+(?:то|тот|та|совпада|похож)|отлича|расхожд|несовпад|неверн|неправильн)', value, re.I))
    if re.search(r'(?:выбрать|выберу|выбираю|хочу|нужен)\s+друг(?:ой|ую|ое)\s+(?:товар|продукт|тариф)', value, re.I) and not re.search(r'несовпад|расхожд|не\s+совпад|неверн|неправильн', value, re.I):
        mismatch = False
    if not (address and payment and fabricated or item and mismatch):
        return None
    blocks=[]
    if address and payment and fabricated:
        blocks.append('Не подскажу выдуманный платёжный адрес. Укажите данные, которые платёжная форма требует и которые вы можете подтвердить. Полный адрес и данные карты сюда присылать не нужно.')
    if item and mismatch:
        blocks.append('Название позиции отличается от ожидаемого. Пока соответствие не проверено, не оплачивайте её. Нужна проверка конкретной позиции и заказа владельцем.')
    if re.search(r'активир|активац', value, re.I):
        blocks.append('Оплата сама по себе не подтверждает активацию. Состояние подписки нужно проверить отдельно по конкретному заказу и аккаунту.')
    # Retain other questions for manual review instead of silently claiming
    # complete coverage of a mixed request.
    handoff = bool(item and mismatch or (value.count('?') > 2 or (value.count('?') > 1 and not re.search(r'активир|активац', value, re.I))))
    if handoff:
        blocks.append('Остальные детали обращения уточнит Александр.')
    return {'reply':'\n\n'.join(blocks), 'summary':'Требуется проверка платёжных данных или соответствия позиции; данные и статус оплаты не подтверждены.',
            'handoff':handoff, 'source_ids':[],
            'evidence':{'status':'clarification','sources':[],
                        'diagnostic':'Этап: проверка вопроса перед оплатой. Достоверность платёжных данных и соответствие позиции источником не подтверждены.'}}
