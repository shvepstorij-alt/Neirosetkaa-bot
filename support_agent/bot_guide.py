"""Customer navigation verified against the shop's menus, not model memory."""
import re


def navigation_facts(facts):
    identity=facts.get('bot_navigation',{})
    username=identity.get('username','')
    verified=identity.get('status')=='available' and isinstance(username,str) and bool(re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{4,31}',username))
    menu='Каталог' if identity.get('catalog_webapp') is True else 'Магазин'
    return {'source':'verified_local_shop_code','identity_status':'available' if verified else 'not_confirmed',
            'username':username if verified else '', 'shop_menu':menu,
            'subscription_steps':['Открыть бота магазина','/start',menu,'Выбрать сервис','Выбрать тариф','Выбрать доступный способ оплаты','Оплатить и следовать инструкции конкретного заказа'],
            'apple_steps':[menu,'iCloud / App Store','Выбрать регион Apple ID','Выбрать номинал','Оплатить выбранный номинал'],
            'generation_menu':{'image':'Изображение','video':'Видео','edit':'Редактировать...',
                               'animate':'Анимировать...','chat':'Консультант AI'},
            'miniapp_generation_steps':['Каталог','Создать','Фото / Видео / Редактировать / Анимация','Выбрать модель и параметры','Заполнить описание или загрузить исходное фото','Проверить стоимость в кредитах перед запуском'],
            'orders_path':['Каталог','Кабинет','Мои заказы'],
            'generation_history_path':['Каталог','Создать','Мои генерации'],
            'info_command':'/info',
            'credits_menu':'Купить кредиты','profile_menu':'Мой профиль',
            'limits':'Текущие модели, стоимость генерации и способы оплаты показываются в интерфейсе. Кредиты генераций отличаются от товара пополнения кредитов ChatGPT. Не обещать активацию по статусу оплаты.'}


def purchase_navigation(facts,apple=False):
    nav=navigation_facts(facts)
    if not nav['username']:return ''
    path=f"Откройте @{nav['username']}, нажмите /start, затем «{nav['shop_menu']}». "
    if apple:
        return path+'Выберите iCloud / App Store, регион вашего Apple ID и нужный номинал, затем способ оплаты.'
    return path+'Выберите нужный сервис и тариф, затем доступный способ оплаты.'


def navigation_reply(facts,latest):
    """Route pure navigation requests only; do not replace service consultations."""
    text=latest.casefold()
    if re.search(r'ошиб|не работает|возврат|закуп|поставщик|18\s*\+|порно',text):return None
    if re.search(r'оформ|подписк|app\s*store|пополнени.*apple',text):return None
    if re.search(r'кредит',text) and re.search(r'chatgpt|чат\s*гпт|openai',text):return None
    if facts.get('requested_catalog') and not re.search(r'в (?:вашем )?боте|через (?:ваш )?бот',text):return None
    nav=navigation_facts(facts)
    if not nav['username']:return None
    prefix=f"Откройте @{nav['username']} и нажмите /start. "
    body=None
    if re.search(r'фото|изображени|картинк|видео',text):
        from .catalog_context import _words
        allowed=set('как мне можно сгенерировать создать сделать фото фотографию изображение картинку видео в вашем боте через ваш бот пожалуйста подскажите хочу нарисовать'.split())
        if set(_words(text))-allowed:return None
    if re.search(r'как.*(?:сгенерировать|создать|сделать).*(?:фото|изображени|картинк)',text):
        body=prefix+'Нажмите «Изображение», выберите модель и следуйте её настройкам, затем отправьте описание нужной картинки. Стоимость смотрите перед запуском в боте.'
    elif re.search(r'как.*(?:сгенерировать|создать|сделать).*видео',text):
        body=prefix+'Нажмите «Видео», выберите модель и следуйте её настройкам. Если хотите оживить готовую фотографию, выберите «Анимировать...». Стоимость смотрите перед запуском.'
    elif re.search(r'как.*(?:купить|пополнить).*кредит',text) and not re.search(r'chatgpt|чат\s*гпт|openai',text):
        from .catalog_context import _words
        allowed=set('как мне купить пополнить кредиты кредитов баланс для генераций в вашем боте через бот пожалуйста подскажите'.split())
        if set(_words(text))-allowed:return None
        body=prefix+'Нажмите «Купить кредиты», выберите пакет и доступный способ оплаты. Эти кредиты используются для работы внутри бота.'
    elif re.search(r'где.*(?:мой баланс|баланс кредитов|профиль)',text):
        body=prefix+'Откройте «Мой профиль», чтобы посмотреть свои данные и баланс.'
    elif re.search(r'как.*(?:пользоваться ботом|работает ваш бот|устроен ваш бот)',text):
        body=prefix+f"Для подписок откройте «{nav['shop_menu']}». Для генераций есть «Изображение», «Видео», «Редактировать...» и «Анимировать...». Для общения с нейросетью нажмите «Консультант AI». Что хотите попробовать?)"
    if body is None:return None
    return {'reply':body,'summary':'Навигация по подтверждённым разделам бота; username получен из работающего магазина.',
            'handoff':False,'source_ids':[],'evidence':{'status':'catalog_direct','sources':[]}}
