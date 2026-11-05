"""Code-owned guidance for the existing ChatGPT mini-app, never an SLA."""
import re


def reply(facts, text, current=None):
    from .catalog_context import _families, _words
    selected=facts.get('catalog_selection',{}).get('mentioned_known_services',[])
    if set(selected)!={'chatgpt'}:return None
    from .fulfillment_reply import _selected_plan_rows
    rows=_selected_plan_rows(facts.get('requested_catalog',[]),text)
    if not rows or not any(r.get('fulfillment')=='code_activation_conditional' for r in rows):return None
    if facts.get('live_shop_status')!='available':return None
    # Other questions in a collected batch must not disappear behind these steps.
    current=text if current is None else current
    from .request_goals import covers
    token_question=bool(re.search(r'(?:откуда|где|как|не могу|не получается|не удается)[^.!?\n]{0,65}(?:токен|сесси)|(?:токен|сесси)[^.!?\n]{0,40}(?:взять|получить|найти)',current,re.I))
    if not covers(current,{'activation','problem'} if token_question else {'activation'},facts):return None
    if re.search(r'возврат|блокиров|другие сервис|генерац|выбрать|тарифы|сравн|сколько|как оплатить|как купить',current,re.I):return None
    explanation=token_question or bool(re.search(r'что значит[^.!?\n]{0,35}авториз|как[^.!?\n]{0,35}войти|не открывается[^.!?\n]{0,40}(?:текст|сесси|страниц)',current,re.I))
    activation=bool(re.search(r'как[^.!?\n]{0,40}активир|не понимаю[^.!?\n]{0,40}активир|помог[а-яё]*\s+активир',current,re.I))
    if not explanation and not activation:return None
    if explanation:
        body=('Если в вашем оплаченном заказе есть кнопка «Активировать подписку», данные сессии нужны для формы этой активации. Авторизоваться значит войти в свой аккаунт ChatGPT на chatgpt.com.\n\n'
              'Откройте сайт в Chrome или Safari и войдите в нужный аккаунт. Затем в этом же браузере откройте https://chatgpt.com/api/auth/session.\n\n'
              'Если страница пустая или не открывается, попробуйте другой браузер: сначала войдите там в аккаунт, затем откройте страницу сессии заново.\n\n'
              'Если не получится, пришлите скриншот ошибки без текста сессии. Сам текст сессии сюда не отправляйте: его нужно вставить только в форму активации в мини-приложении.')
    else:
        body=('Если в сообщении вашего оплаченного заказа есть кнопка «Активировать подписку», откройте её.\n\n'
              '1. В Chrome или Safari откройте chatgpt.com и войдите в нужный аккаунт.\n'
              '2. В этом же браузере откройте https://chatgpt.com/api/auth/session.\n'
              '3. Скопируйте весь текст страницы.\n'
              '4. Вернитесь в мини-приложение активации, вставьте текст в предназначенное поле и отправьте.\n\n'
              'Текст сессии не присылайте в переписку. Оплата и завершение активации - разные этапы: результат смотрите в мини-приложении. Если кнопки нет или появилась ошибка, пришлите скриншот без данных сессии, помогу разобраться)')
    from .auto_policy import consultation_evidence
    return {'reply':body,'summary':'Пошаговая помощь по подтверждённой кодовой ветке ChatGPT; факт оплаты и успешной активации не утверждался, срок не обещан.',
            'handoff':False,'source_ids':[], 'evidence':consultation_evidence(body,'activation',
            diagnostic='Этап: инструкция кодовой активации. Сервис=ChatGPT; fulfillment=code_activation_conditional; живой экспорт=available. Платёж и результат активации не проверялись.')}
