"""ChatGPT install instructions with backward-compatible iPhone settings.

These helpers do not write settings or access external services. The old
gpt_install_text remains the iPhone source until an explicit new value exists.
"""

ANDROID_SETTING='gpt_install_android_text'
IPHONE_SETTING='gpt_install_iphone_text'
DESKTOP_SETTING='gpt_install_desktop_text'
LEGACY_IPHONE_SETTING='gpt_install_text'
MAX_GUIDE_UNITS=3500

ANDROID_DEFAULT=(
    '🤖 <b>Установка ChatGPT на Android</b>\n\n'
    'Ссылка для Android: '
    '<a href="https://chatgpt.ru.uptodown.com/android">ChatGPT на Uptodown</a>.\n'
    'Uptodown — сторонний каталог, а не сайт OpenAI. Мы не можем гарантировать безопасность каждого APK.\n\n'
    '1. Откройте ссылку в браузере Android и проверьте название и разработчика приложения.\n'
    '2. Если выбрали установку APK, следуйте подсказкам Android. Не отключайте Play Protect или другую защиту устройства. '
    'Если устройство блокирует файл, остановитесь и пришлите скриншот без личных данных.\n'
    '3. Откройте приложение и войдите в тот же аккаунт ChatGPT, для которого оформляете подписку. '
    'Не передавайте данные доступа сторонним установщикам.\n\n'
    'Если приложение не устанавливается, можно пользоваться chatgpt.com в браузере.\n\n'
    'Установка приложения сама по себе не оформляет и не активирует платную подписку.'
)

IPHONE_DEFAULT=(
    '🍎 <b>Установка ChatGPT на iPhone</b>\n\n'
    'Откройте App Store и найдите приложение ChatGPT от OpenAI. Если оно недоступно для страны вашего Apple ID, '
    'можно пользоваться chatgpt.com в браузере.\n\n'
    '<b>Если планируете сменить страну Apple ID</b>\n'
    '1. Откройте «Настройки» → ваше имя → «Медиаматериалы и покупки» → «Просмотреть» → «Страна/регион».\n'
    '2. Выберите новую страну, прочитайте условия и следуйте требованиям Apple к способу оплаты и платёжному адресу. '
    'Пункт «Нет» доступен не во всех случаях; не используйте вымышленные данные.\n'
    '3. После изменения страны снова откройте App Store и проверьте доступность ChatGPT от OpenAI.\n\n'
    '<b>Перед изменением страны</b>\n'
    'Потратьте остаток баланса. Могут потребоваться отмена мешающих смене подписок и ожидание окончания их периода, '
    'завершение ожидающих операций и выход из семейной группы. Некоторые покупки и приложения могут быть недоступны в новой стране.\n\n'
    '<a href="https://support.apple.com/en-us/118283">Правила смены страны Apple Account</a>.\n\n'
    'После установки войдите в нужный аккаунт ChatGPT. Установка приложения не активирует подписку автоматически.'
)

DESKTOP_PLAIN=(
    'Как установить ChatGPT на компьютер\n\n'
    'Windows\n\n'
    'Нужна Windows 10 версии 17763.0 или новее (x64 или arm64).\n\n'
    '1. Откройте официальную страницу: https://chatgpt.com/download/\n'
    '2. Выберите загрузку для Windows и установите ChatGPT из Microsoft Store.\n'
    '3. Откройте приложение и войдите в тот аккаунт ChatGPT, для которого оформляете подписку.\n\n'
    'macOS\n\n'
    'Нужна macOS 14 или новее и Mac с процессором Apple Silicon (M1 или новее) либо Intel.\n\n'
    '1. Откройте официальную страницу: https://chatgpt.com/download/\n'
    '2. Скачайте приложение для Mac — страница подберёт установщик под ваш процессор.\n'
    '3. Установите приложение, откройте его и войдите в нужный аккаунт ChatGPT.\n\n'
    'Если приложение не подходит или не устанавливается, можно пользоваться https://chatgpt.com в браузере)\n\n'
    'Установка приложения сама по себе не оформляет и не активирует платную подписку.'
)

DESKTOP_DEFAULT=(
    '💻 <b>Как установить ChatGPT на компьютер</b>\n\n'
    '<b>Windows</b>\n\n'
    'Нужна Windows 10 версии 17763.0 или новее (x64 или arm64).\n\n'
    '1. Откройте официальную страницу: <a href="https://chatgpt.com/download/">https://chatgpt.com/download/</a>\n'
    '2. Выберите загрузку для Windows и установите ChatGPT из Microsoft Store.\n'
    '3. Откройте приложение и войдите в тот аккаунт ChatGPT, для которого оформляете подписку.\n\n'
    '<b>macOS</b>\n\n'
    'Нужна macOS 14 или новее и Mac с процессором Apple Silicon (M1 или новее) либо Intel.\n\n'
    '1. Откройте официальную страницу: <a href="https://chatgpt.com/download/">https://chatgpt.com/download/</a>\n'
    '2. Скачайте приложение для Mac — страница подберёт установщик под ваш процессор.\n'
    '3. Установите приложение, откройте его и войдите в нужный аккаунт ChatGPT.\n\n'
    'Если приложение не подходит или не устанавливается, можно пользоваться https://chatgpt.com в браузере)\n\n'
    'Установка приложения сама по себе не оформляет и не активирует платную подписку.'
)


def validate_guide(value):
    if not isinstance(value,str):raise ValueError('Этап: сохранение инструкции. Текст должен быть строкой.')
    if len(value.encode('utf-16-le'))//2>MAX_GUIDE_UNITS:
        raise ValueError('Этап: сохранение инструкции. Текст превышает лимит 3500 символов Telegram; сократите инструкцию.')
    return value.strip()


async def load_guides(get_setting):
    """Read without mutation. Explicit empty new value clears legacy fallback."""
    iphone=await get_setting(IPHONE_SETTING,None)
    if iphone is None:iphone=await get_setting(LEGACY_IPHONE_SETTING,'')
    android=await get_setting(ANDROID_SETTING,'')
    desktop=await get_setting(DESKTOP_SETTING,'')
    return {'iphone':str(iphone or '').strip() or IPHONE_DEFAULT,
            'android':str(android or '').strip() or ANDROID_DEFAULT,
            'desktop':str(desktop or '').strip() or DESKTOP_DEFAULT}


async def settings_payload(get_setting):
    guides=await load_guides(get_setting)
    return {'gptInstall':guides['iphone'],'gptInstallIphone':guides['iphone'],
            'gptInstallAndroid':guides['android'],'gptInstallDesktop':guides['desktop']}


def setting_key(kind):
    return {'gpt_install':IPHONE_SETTING,'gpt_install_iphone':IPHONE_SETTING,
            'gpt_install_android':ANDROID_SETTING,'gpt_install_desktop':DESKTOP_SETTING}.get(kind)
