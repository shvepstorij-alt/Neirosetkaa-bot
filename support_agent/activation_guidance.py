"""Code-owned guidance for the existing ChatGPT mini-app, never an SLA."""
import re


_TIMING=('Автоматическая активация через бота обычно занимает 2–5 минут. '
          'Ручная активация — в течение получаса. Кнопка активации действует 2 часа.')
_BROWSERS='Подойдут Google, Chrome, Safari, Opera и Яндекс. Лучше всего Google.'
_STEPS=('1. Зайдите на сайт ChatGPT.com\n'
        '2. Авторизуйтесь в ваш аккаунт\n'
        '3. Перейдите по ссылке в этом же браузере https://chatgpt.com/api/auth/session\n'
        '4. Скопируйте весь текст и вставьте его в окно активации в боте, не в эту переписку.')


def _chatgpt(facts, text):
    selected=set(facts.get('catalog_selection',{}).get('mentioned_known_services') or [])
    if selected-{'chatgpt'}:
        return False
    if selected=={'chatgpt'}:
        return True
    if re.search(r'chatgpt|чат\s*гпт|чатгпт', text, re.I):
        return True
    return bool(re.search(r'нов\w*\s+аккаунт', text, re.I) and re.search(r'\bplus\b|плюс', text, re.I))


def reply(facts, text, current=None):
    current=text if current is None else current
    if not _chatgpt(facts, current):return None
    if re.search(r'возврат|другие сервис|генерац|выбрать|тарифы|сравн|сколько|как оплатить|как купить',current,re.I):return None
    expired=bool(re.search(r'(?:ист[её]к|протух)[^.!?\n]{0,20}токен|токен[^.!?\n]{0,20}(?:ист[её]к|протух)',current,re.I))
    paste=bool(re.search(r'не\s+вставля[^.!?\n]{0,30}сесс',current,re.I))
    new_plus=bool(re.search(r'нов\w*\s+аккаунт',current,re.I) and re.search(r'plus|плюс',current,re.I))
    overlay=bool(re.search(r'активир\w*\s+поверх|уже\s+есть\s+подписк',current,re.I))
    token_question=bool(re.search(r'(?:откуда|где|как|не могу|не получается|не удается)[^.!?\n]{0,65}(?:токен|сесси)|(?:токен|сесси)[^.!?\n]{0,40}(?:взять|получить|найти)',current,re.I))
    activation=bool(re.search(r'как[^.!?\n]{0,40}активир|не понимаю[^.!?\n]{0,40}активир|помог[а-яё]*\s+активир',current,re.I))
    if not any((expired,paste,new_plus,overlay,token_question,activation)):return None
    from .request_goals import covers
    if not (expired or paste or new_plus or overlay) and not covers(current,{'activation','problem'} if token_question else {'activation'},facts):return None
    rows=facts.get('requested_catalog') or []
    methods={r.get('fulfillment') for r in rows if isinstance(r,dict)}
    if methods and 'code_activation_conditional' not in methods and not (expired or paste or new_plus or overlay):
        return None
    if new_plus:
        body=('Для товара «Новый аккаунт Plus» кнопки самостоятельной активации нет. Пришлите токен аккаунта сюда, в эту переписку.')
    elif expired:
        body=('Похоже, токен истёк. Сначала полностью выйдите из аккаунта ChatGPT и войдите заново в том же браузере, затем получите новый токен и повторите активацию.\n\n'
              'Если не помогло, попробуйте другой браузер. '+_BROWSERS+'\n\n'+_STEPS+'\n\n'+_TIMING)
    elif paste:
        body=('Если сессия не вставляется с компьютера, вставьте текст с клавиатуры через Ctrl+V.\n\n'+_STEPS)
    elif overlay:
        body=('Можно нажать «Активировать поверх». Старая подписка сгорит, начнётся новый оплаченный месяц. Новая подписка оплачивается полностью.')
    else:
        body=('Самостоятельная активация идёт в окне бота, не в этой переписке.\n\n'+_STEPS+'\n\n'+_BROWSERS+'\n\n'+_TIMING+
              '\n\nЕсли токен истёк, полностью выйдите из аккаунта и войдите заново в том же браузере, затем возьмите новый токен. Если не помогло — другой браузер.')
    from .auto_policy import consultation_evidence
    return {'reply':body,'summary':'Подсказка по активации ChatGPT по правилам владельца. Оплата и успешная активация не утверждались.',
            'handoff':False,'source_ids':[], 'evidence':consultation_evidence(body,'activation',
            diagnostic='Этап: инструкция активации ChatGPT. Платёж и результат у поставщика не проверялись.')}
