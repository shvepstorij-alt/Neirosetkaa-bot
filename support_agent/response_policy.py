"""Small deterministic protections around conversational model output."""
import re


def continuing_reply(reply, history):
    if not any(m.get('role') in ('assistant','owner') for m in history):
        return reply
    # Only remove an opening salutation, never quoted or embedded client text.
    cleaned=re.sub(r'^\s*(?:здравствуйте|здравствуй|приветствую|привет|добрый\s+(?:день|вечер)|доброе\s+утро)\b[!.,:)\s]*',
                   '',reply,count=1,flags=re.I)
    return cleaned.strip() or 'Чем могу помочь?'


def missing_shop_reply(latest, facts, has_images=False):
    """Public subscription prices must not substitute for an unavailable shop catalog."""
    text=latest.lower()
    procurement=bool(re.search(r'закуп|поставщик|себестоим',text))
    adult=bool(re.search(r'18\s*\+|порно|эротическ',text))
    if procurement or adult:
        parts=[]
        if procurement:
            parts.append('Закупочные условия и поставщиков не раскрываю.')
        if adult:
            parts.append('С созданием контента 18+ не помогаю.')
        return {'reply':' '.join(parts)+' По подпискам и возможностям сервисов подскажу)',
                'summary':'Последний запрос затрагивает закрытые закупочные сведения или контент 18+. Дан краткий отказ без возврата к прежним темам.',
                'handoff':False}
    catalog_missing=facts.get('source')=='unavailable' or facts.get('catalog_status') in ('read_error','not_connected')
    if has_images or not catalog_missing:
        return None
    retail=bool(re.search(r'у\s+вас|через\s+вас|у\s+тебя|через\s+тебя|ваш\w*\s+(?:магазин|тариф|цен)',text))
    purchase=bool(re.search(r'цен|стоим|тариф|оформ|купить|покуп|подписк',text))
    if not (retail and purchase):
        return None
    return {'reply':'Подскажите, какой сервис и тариф вас интересуют? Точную стоимость и условия уточню перед оформлением.',
            'summary':'Запрос об оформлении или тарифах нашего магазина. Каталог недоступен: цены не подтверждены. Нужна проверка владельца; официальные цены сервисов вместо наших не использованы.',
            'handoff':True}
