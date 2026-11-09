"""Narrow, fact-free replies for fresh account/device and Apple region questions.

The previous assistant message may identify an Apple follow-up, but its prices,
denominations and claims are never reused as facts.
"""
import re

from .apple_matching import region_codes
from .privacy import sensitive


_CHATGPT = re.compile(r'chat\s*gpt|чат\s*гпт|чатгпт|\bгпт\b', re.I)
_PHONE = re.compile(r'телефон|айфон|iphone|android|андроид', re.I)
_DESKTOP = re.compile(r'компьютер|ноутбук|\bпк\b|\bpc\b|windows|macbook', re.I)
_NOT_VISIBLE = re.compile(r'\bнет\b|не\s+(?:появил|отобража|видн|работа)|\bfree\b|бесплатн', re.I)
_APPLE = re.compile(r'айклауд|icloud|app\s*store|апп?\s*стор|эпп?\s*стор|apple\s*id|эпл\s*айди', re.I)
_TOPUP = re.compile(r'пополн|баланс|номинал|\bкод\b|сколько|цен|стоимост|\d+\s*(?:₽|рубл|руб\b)', re.I)
_MONEY_ONLY = re.compile(r'^\s*(?:(?:мне\s+)?нужно|хочу|на|бюджет|примерно|около)?\s*(?:на\s+)?\d+[\s\u00a0]*(?:₽|руб(?:лей|ля|ль)?|р)\s*[.!?\s]*$', re.I)


def _device_issue(text):
    return bool(_CHATGPT.search(text) and _PHONE.search(text) and _DESKTOP.search(text)
                and _NOT_VISIBLE.search(text) and re.search(r'подписк|плюс|plus|активир|тариф',text,re.I))


def _apple_question(text,previous_assistant):
    explicit=bool(_APPLE.search(text) and _TOPUP.search(text))
    # A short amount may inherit only the subject of a recent assistant offer.
    # It never inherits its old prices or a region not stated by the client.
    followup=bool(_MONEY_ONLY.fullmatch(text.strip()) and _APPLE.search(previous_assistant or '')
                  and re.search(r'цен|номинал|пополн|тариф',previous_assistant or '',re.I))
    return explicit or followup


def reply(current,previous_assistant=''):
    """Answer both independently stated goals; ask for only missing facts.

    A single device mismatch or a regionless Apple amount is supported too.
    Other problem types go through the normal reviewed pipeline.
    """
    text=str(current or '')
    if not text or sensitive(text):return None
    clauses=[line.strip() for line in text.splitlines() if line.strip()]
    apple=[line for line in clauses if _apple_question(line,previous_assistant)]
    remaining=[line for line in clauses if line not in apple]
    # A single Telegram message can contain a line break between the purchase
    # statement and the observed phone/desktop mismatch.
    device=(remaining if _device_issue('\n'.join(remaining)) and all(
        _CHATGPT.search(line) or _PHONE.search(line) or _DESKTOP.search(line)
        for line in remaining) else [])
    if not device and not apple:return None
    if apple and not device and not any(_MONEY_ONLY.fullmatch(line) for line in apple):
        return None  # The full Apple intake has its own catalogue-aware route.
    # Never swallow another independent question in the same collected batch.
    covered=set(device+apple)
    if any(line not in covered for line in clauses):return None
    if apple and any(region_codes(line) for line in apple):return None
    parts=[]
    if device:
        parts.append('**ChatGPT на компьютере.** Подписка может отображаться по-разному, если на устройствах выполнен вход в разные аккаунты или разными способами. На компьютере откройте ChatGPT и проверьте, что вошли в тот же аккаунт тем же способом (Apple, Google или почта), что на телефоне. Если там по-прежнему Free, напишите, какой способ входа используете на каждом устройстве. Личные данные для входа присылать не нужно.')
    if apple:
        amount=next((m.group(0).strip() for line in apple for m in [re.search(r'\d+[\s\u00a0]*(?:₽|руб(?:лей|ля|ль)?|р)',line,re.I)] if m),None)
        opening=('Вы указали бюджет '+amount+'. ' if amount else '')
        parts.append('**iCloud / App Store.** '+opening+'Для подбора пополнения сначала нужен регион вашего Apple ID: подарочный код должен соответствовать региону аккаунта. Посмотреть его можно на iPhone: «Настройки» → ваше имя → «Медиаматериалы и покупки» → «Просмотреть» → «Страна/регион». Какой регион там указан? После этого подберу доступный номинал по текущим данным магазина.')
    body='\n\n'.join(parts)
    return {'reply':body,
            'summary':'Отдельно рассмотрены проблема отображения подписки ChatGPT на компьютере и/или пополнение Apple ID. Статус подписки, регион, цена и доступность номинала не установлены.',
            'handoff':False,'source_ids':[],
            'evidence':{'status':'clarification','sources':[],
                        'diagnostic':'Этап: разбор текущих вопросов. Запросов по устройствам ChatGPT='+str(len(device))
                        +'; запросов по Apple ID без региона='+str(len(apple))
                        +'; статус подписки и регион не подтверждены; цены и номиналы не назывались.'}}
