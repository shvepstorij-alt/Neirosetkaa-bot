"""Installation help: owner-provided alternative APK and verified Apple steps.

No guarantees about a third-party binary, fake billing identity or region access.
"""
import re

ANDROID='https://chatgpt.ru.uptodown.com/android'
PLAY='https://play.google.com/store/apps/details?id=com.openai.chatgpt'
IOS='https://apps.apple.com/us/app/chatgpt/id6448311069'
APPLE='https://support.apple.com/en-ca/118283'


def policy_facts():
    return {'source':'owner_instructions_and_primary_store_pages_2026_11_05',
            'android_alternative':ANDROID,'alternative_is_third_party':True,
            'apk_signature_verified':False,'official_android':PLAY,'official_ios':IOS,
            'apple_region_source':APPLE,
            'none_payment_option_guaranteed':False,'billing_details_must_be_valid':True,
            'region_change_prerequisites':['spend_remaining_balance','cancel_blocking_subscriptions_and_wait_until_period_end',
                'clear_pending_activity','check_family_sharing','valid_new_region_payment_method_may_be_required'],
            'purchase_subscription_again_in_mobile_app':False,
            'installation_does_not_confirm_subscription_or_regional_access':True}


def reply(facts,current):
    text=str(current or '')
    install=bool(re.search(r'(?:скача|установ)[а-яё]*[^.!?\n]{0,50}(?:прилож|chat\s*gpt|чат\s*гпт|апк|apk)|'
        r'(?:прилож|chat\s*gpt|чат\s*гпт)[^.!?\n]{0,45}(?:скача|установ)|как[^.!?\n]{0,35}(?:сменить|изменить)[^.!?\n]{0,30}регион',text,re.I))
    if not install:return None
    from .request_goals import covers
    if not covers(text,{'installation'},facts):return None
    if re.search(r'сколько|тариф|как[^.!?\n]{0,35}оплат|возврат|верн[а-я]*\s+деньг|что\s+входит',text,re.I):return None
    from .catalog_context import _families,_words
    families=_families(_words(text))
    known=families or set(facts.get('catalog_selection',{}).get('mentioned_known_services',[]))
    if known-{'chatgpt','appstore'}:return None
    from .catalog_context import service_mentions
    named=service_mentions(text,facts.get('catalog',[]))
    selected=named or service_mentions(' '.join(str(r.get('service','')) for r in facts.get('requested_catalog',[])),facts.get('catalog',[]))
    if set(selected)-{'chatgpt','appstore'}:return None
    android=bool(re.search(r'android|андроид|\bapk\b|апк',text,re.I))
    ios=bool(re.search(r'iphone|ipad|ios|айфон|айпад|apple\s*(?:id|аккаунт)|эпл|app\s*store|регион',text,re.I))
    if android and ios:return None
    if android:
        body=('Для Android можно скачать приложение здесь:\n'+ANDROID+'\n\n'
              'Это сторонний каталог APK, не сайт OpenAI. Проверку конкретного файла я не выполнял, поэтому гарантировать его безопасность не могу. '
              'Если доступен Google Play, предпочтительнее установить приложение OpenAI оттуда:\n'+PLAY+'\n\n'
              'Откройте страницу, скачайте APK и запустите файл. Если Android запросит разрешение на установку из этого источника, '
              'разрешайте его только для нужного приложения установки; после установки разрешение можно отключить. '
              'Не отключайте защиту устройства. Если появится предупреждение или ошибка, пришлите её скриншот.\n\n'
              'Войдите в тот же аккаунт ChatGPT, на который оформляется подписка. Повторно покупать подписку внутри приложения не нужно.')
    elif ios:
        body=('На iPhone установите ChatGPT от OpenAI через App Store:\n'+IOS+'\n\n'
              'Если магазин пишет, что приложение недоступно в вашем регионе, сначала проверьте страну Apple Account. '
              'Смена региона подходит не во всех случаях: нужно потратить остаток баланса, отменить мешающие подписки '
              'и дождаться окончания их периода. Незавершённые покупки, возвраты и семейная группа тоже могут мешать.\n\n'
              'Путь в настройках: ваше имя → «Медиаматериалы и покупки» → «Просмотреть» или настройки учётной записи → '
              '«Страна/регион» → изменение страны. Примите условия и укажите действительные данные для нового региона. '
              'Apple может потребовать подходящий способ оплаты; вариант «Нет» доступен не всегда. Выдумывать адрес и телефон не нужно.\n\n'
              'Некоторый контент может быть недоступен в новом регионе. После изменения платёжных данных подписки при необходимости '
              'оформляются заново, если доступны в этой стране. Не меняйте регион, пока не проверите важные подписки и iCloud.\n\n'
              'После установки войдите в тот же аккаунт ChatGPT. Повторно покупать подписку в приложении не нужно.')
    else:
        body='Подскажу пошагово) У вас Android или iPhone?'
    return {'reply':body,'summary':'Инструкция установки без гарантии APK, доступа по региону или обязательного варианта None. Покупка подписки повторно не предлагается.',
            'handoff':False,'source_ids':[],
            'evidence':{'status':'owner_policy','sources':[],
                        'diagnostic':'Этап: инструкция установки. Платформа='+('android' if android else 'ios' if ios else 'не указана')+
                            '; APK третьей стороны не проверялся по подписи; оплата и активация не проверялись. Условия Apple сверены 05.11.2026.'}}
