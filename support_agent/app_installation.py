"""Installation help: owner-provided alternative APK and verified Apple steps.

No guarantees about a third-party binary, fake billing identity or region access.
"""
import re
import time

ANDROID='https://chatgpt.ru.uptodown.com/android'
PLAY='https://play.google.com/store/apps/details?id=com.openai.chatgpt'
IOS='https://apps.apple.com/us/app/chatgpt/id6448311069'
APPLE='https://support.apple.com/en-ca/118283'
DESKTOP='https://chatgpt.com/ru-RU/download/'
MIDJOURNEY='https://www.midjourney.com/'
PLATFORM_QUESTION='Подскажу пошагово) У вас Android, iPhone или компьютер?'
_LEGACY_PLATFORM_QUESTION='Подскажу пошагово) У вас Android или iPhone?'


def policy_facts():
    return {'source':'owner_instructions_and_primary_store_pages_2026_11_05',
            'android_alternative':ANDROID,'alternative_is_third_party':True,
            'apk_signature_verified':False,'official_android':PLAY,'official_ios':IOS,
            'official_desktop':DESKTOP,'desktop_source_checked_on':'2026-11-07',
            'apple_region_source':APPLE,
            'none_payment_option_guaranteed':False,'billing_details_must_be_valid':True,
            'region_change_prerequisites':['spend_remaining_balance','cancel_blocking_subscriptions_and_wait_until_period_end',
                'clear_pending_activity','check_family_sharing','valid_new_region_payment_method_may_be_required'],
            'purchase_subscription_again_in_mobile_app':False,
            'installation_does_not_confirm_subscription_or_regional_access':True}


def _install_intent(text):
    return bool(re.search(r'(?:скача|установ)[а-яё]*[^.!?\n]{0,50}(?:прилож|chat\s*gpt|чат\s*гпт|гпт|апк|apk)|'
        r'(?:прилож|chat\s*gpt|чат\s*гпт)[^.!?\n]{0,45}(?:скача|установ)|как[^.!?\n]{0,35}(?:сменить|изменить)[^.!?\n]{0,30}регион',text,re.I))


def _platforms(text):
    found=set()
    if re.search(r'android|андроид|\bapk\b|апк',text,re.I):found.add('android')
    if re.search(r'iphone|ipad|\bios\b|айфон|айпад|apple\s*(?:id|аккаунт)|эпл|app\s*store|регион',text,re.I):found.add('ios')
    if re.search(r'компьютер|ноутбук|виндо[ув]с|винд[аеуы]|\b(?:pc|windows|macos|mac|пк)\b',text,re.I):found.add('desktop')
    return found


def _platform_slot(text):
    return bool(re.fullmatch(r'\s*(?:(?:у\s+меня|нужно\s+на|на|это|а)\s+)?(?:android|андроид[а-яё]*|iphone|айфон[а-яё]*|ipad|айпад[а-яё]*|ios|компьютер[а-яё]*|ноутбук[а-яё]*|пк|pc|windows|macos|mac)[.!?\s]*',text,re.I))


def _identity(text,catalog):
    """Only the customer's product words establish the app, never a default."""
    from .catalog_context import _families,_words,service_mentions
    families=_families(_words(text))
    names=service_mentions(text,catalog)
    # A reply to the app question can name ChatGPT together with another known
    # app.  That is still an installation turn, not an implicit tariff query.
    # Keep it distinct from a single ChatGPT request so later platform answers
    # retain both requested apps.
    if 'chatgpt' in families and len(families-{'appstore'})>1:return 'multiple'
    if families-{'chatgpt','appstore'}:return 'other'
    if any(_families(_words(name))-{'chatgpt','appstore'} or not _families(_words(name))
           for name in names.values()):return 'other'
    allowed={'chatgpt','chat','gpt','android','apk','iphone','ipad','ios','apple','id','app','store',
             'windows','pc','mac','macos','openai'}
    if any(word.casefold() not in allowed for word in re.findall(r'[a-zA-Z][a-zA-Z0-9_-]*',text)):
        return 'other'
    target=re.search(r'(?:скачать|установить)\s+(?:приложение\s+)?([а-яёa-z][а-яёa-z0-9_-]*)',text,re.I)
    if (target and target[1].casefold() not in {'приложение','на','в','его','мне','себе','чат','гпт','chatgpt'}
            and _families(_words(target[1]))!={'chatgpt'}):
        return 'other'
    return 'chatgpt' if 'chatgpt' in families else None


def _previous_installation(facts,current,history):
    """Fresh original client intent, with only our own exact slot prompt usable."""
    from .context_validity import validate_client_rows
    from .request_context import is_acknowledgement
    from .auto_policy import _consultation_text
    rows=[r for r in list(history)[-40:] if isinstance(r,dict)]
    clients=[r for r in rows if r.get('role')=='client']
    context=validate_client_rows(reversed(clients),now=time.time(),max_messages=40)
    valid={r['mid'] for r in context.rows}
    last_prompt=next((r for r in reversed(rows) if r.get('role') in ('assistant','owner')),None)
    prompt=bool(last_prompt and any(_consultation_text(last_prompt.get('text')).endswith(q) for q in (PLATFORM_QUESTION,_LEGACY_PLATFORM_QUESTION))
                and type(last_prompt.get('at')) in (int,float)
                and 0<=time.time()-last_prompt['at']<=14400)
    platform_hint=set()
    for row in reversed(clients):
        if row.get('mid') not in valid:break
        text=str(row.get('text',''))
        if text==current:continue  # Service history includes the current batch.
        identity=_identity(text,facts.get('catalog',[]))
        if identity=='other':break
        if _install_intent(text) and identity in ('chatgpt', 'multiple', None):
            return text,prompt,platform_hint or _platforms(text)
        if _platform_slot(text):
            if not platform_hint:platform_hint=_platforms(text)
        elif not is_acknowledgement(text):break
    return '',False,set()


def _accepted_install_offer(current,history):
    """A short acceptance refers only to the last, recent explicit app offer."""
    if not re.fullmatch(r'\s*(?:да|ага|хорошо|помогите|да[,.!\s]+помогите|да[,.!\s]+подскажите)[.!\s]*',str(current),re.I):
        return set()
    rows=[row for row in list(history)[-40:] if isinstance(row,dict)]
    previous=next((row for row in reversed(rows) if row.get('role') in ('assistant','owner')),None)
    if not previous or type(previous.get('at')) not in (int,float) or not 0<=time.time()-previous['at']<=14400:
        return set()
    offer=str(previous.get('text') or '')
    if not re.search(r'помогу\s+с\s+установк[а-яё]*\s+приложени[а-яё]*\s+(?:ChatGPT|чат\s*гпт)',offer,re.I):
        return set()
    platforms=_platforms(offer)
    return platforms if len(platforms)==1 else set()


def reply(facts,current,history=()):
    text=str(current or '')
    from .privacy import sensitive
    if sensitive(text):return None
    # Midjourney's documented supported entry points are its website and
    # Discord.  Do not turn a request for an app into a recommendation for an
    # APK or an unrelated product with a similar name.
    if re.search(r'midjourney|миджорни|миджерни',text,re.I) and re.search(r'приложени|скача|установ',text,re.I):
        from .auto_policy import consultation_evidence
        body=('По официальной документации Midjourney начать работу можно на официальном сайте:\n'+MIDJOURNEY+
              '\n\nMidjourney также работает через Discord: при необходимости подключите свой Discord-аккаунт в настройках Midjourney. '
              'Не рекомендую скачивать APK или приложение из неофициального источника.')
        return {'reply':body,'summary':'Для Midjourney предложены подтверждённые официальный сайт и Discord без рекомендации стороннего приложения.',
                'handoff':False,'source_ids':[],
                'evidence':consultation_evidence(body,'installation',diagnostic='Этап: установка/доступ Midjourney. Официальная документация Midjourney проверена 10.11.2026: описаны работа на midjourney.com и через подключённый Discord; приложение как единственный или отсутствующий способ доступа не утверждается. Неофициальный APK не рекомендован.')}
    identity=_identity(text,facts.get('catalog',[]))
    if identity=='other':return None
    prior,prompt,prior_platforms=_previous_installation(facts,text,history)
    accepted_platforms=_accepted_install_offer(text,history)
    slot=_platform_slot(text)
    rows=[r for r in list(history)[-40:] if isinstance(r,dict)]
    last=next((r for r in reversed(rows) if r.get('role') in ('assistant','owner')),None)
    from .auto_policy import _consultation_text
    fresh_last=bool(last and type(last.get('at')) in (int,float) and 0<=time.time()-last['at']<=14400)
    app_slot=bool(prior and identity in ('chatgpt','multiple') and fresh_last and _consultation_text(last.get('text')).endswith('Какое приложение хотите установить?'))
    multi_platform_slot=bool(fresh_last and re.search(r'ChatGPT\s+и\s+Claude\).*?(?:Android|iPhone|компьютер)',
                                                       _consultation_text(last.get('text')),re.I))
    unavailable=bool(prior and fresh_last and 'App Store' in str(last.get('text','')) and re.search(r'chat\s*gpt|чат\s*гпт',str(last.get('text','')),re.I) and re.fullmatch(r'\s*(?:у\s+меня\s+)?(?:он|оно|приложение)\s+недоступен(?:о)?[.!?\s]*',text,re.I))
    if unavailable:
        from .auto_policy import consultation_evidence
        body='Что именно недоступно: приложение ChatGPT в App Store или вход в него? Если магазин показывает сообщение, пришлите его текст или скриншот.'
        return {'reply':body,'summary':'Уточнение недоступности приложения без предположения о причине.','handoff':False,'source_ids':[],'evidence':consultation_evidence(body,'installation',diagnostic='Этап: уточнение проблемы установки; причина недоступности не установлена.')}
    short_install=bool(re.fullmatch(r'\s*(?:а\s+)?(?:как|где)\s+(?:его\s+)?(?:скачать|установить)(?:\s+приложение)?[.!?\s]*',text,re.I))
    install=_install_intent(text) or short_install
    if identity is None and multi_platform_slot and (install or slot):
        identity='multiple'
    if not install and not (slot and prompt and prior) and not (slot and multi_platform_slot) and not accepted_platforms and not app_slot:return None
    from .request_goals import covers
    if not covers(text,{'installation'},facts):return None
    if re.search(r'сколько|тариф|как[^.!?\n]{0,35}оплат|возврат|верн[а-я]*\s+деньг|что\s+входит',text,re.I):return None
    platforms=_platforms(text)
    if not platforms and accepted_platforms:
        platforms=accepted_platforms
        identity='chatgpt'
    if not platforms and prior:platforms=prior_platforms
    if len(platforms)>1:return None
    android='android' in platforms;ios='ios' in platforms;desktop='desktop' in platforms
    apple_region=bool(re.search(r'как[^.!?\n]{0,35}(?:сменить|изменить)[^.!?\n]{0,30}регион',text,re.I)
                      and re.search(r'apple\s*(?:id|account|аккаунт)|app\s*store|эпл',text,re.I))
    prior_identity=_identity(prior,facts.get('catalog',[])) if prior else None
    if identity is None and prior_identity=='multiple':identity='multiple'
    if identity=='multiple' and not platforms:
        body='Понял, нужна установка ChatGPT и Claude) На чём хотите установить: Android, iPhone или компьютер?'
    elif identity=='multiple' and (android or ios):
        store='Google Play' if android else 'App Store'
        chatgpt_link=PLAY if android else IOS
        body=(f'Для ChatGPT откройте официальную страницу OpenAI в {store}:\n{chatgpt_link}\n\n'
              f'Для Claude откройте {store}, найдите приложение «Claude» и проверьте, что разработчик указан как Anthropic. '
              'После установки войдите в нужные аккаунты. Установка приложений сама по себе не оформляет подписки.')
    elif identity=='multiple' and desktop:
        body=('Для ChatGPT на компьютере используйте официальную страницу загрузки:\n'+DESKTOP+
              '\n\nClaude можно открыть в браузере на официальном сайте сервиса. Установка и вход не оформляют подписки автоматически.')
    elif identity!='chatgpt' and prior_identity!='chatgpt' and not apple_region:
        body='Какое приложение хотите установить?'
    elif desktop:
        from install_guides import DESKTOP_PLAIN
        body=DESKTOP_PLAIN
    elif android:
        body=('Для Android можно скачать приложение здесь:\n'+ANDROID+'\n\n'
              'Это сторонний каталог APK, не сайт OpenAI. Проверку конкретного файла я не выполнял, поэтому гарантировать его безопасность не могу. '
              'Если доступен Google Play, предпочтительнее установить приложение OpenAI оттуда:\n'+PLAY+'\n\n'
              'Откройте страницу, скачайте APK и запустите файл. Если Android запросит разрешение на установку из этого источника, '
              'разрешайте его только для нужного приложения установки; после установки разрешение можно отключить. '
              'Не отключайте защиту устройства. Если появится предупреждение или ошибка, пришлите её скриншот.\n\n'
              'Войдите в тот же аккаунт ChatGPT, на который оформляется подписка. Повторно покупать подписку внутри приложения не нужно.')
    elif ios:
        body=(('Смена региона Apple Account требует проверки действующих условий.\n\n' if apple_region and identity!='chatgpt' and not prior else
               'На iPhone установите ChatGPT от OpenAI через App Store:\n'+IOS+'\n\n')+
              'Если магазин пишет, что приложение недоступно в вашем регионе, сначала проверьте страну Apple Account. '
              'Смена региона подходит не во всех случаях: нужно потратить остаток баланса, отменить мешающие подписки '
              'и дождаться окончания их периода. Незавершённые покупки, возвраты и семейная группа тоже могут мешать.\n\n'
              'Путь в настройках: ваше имя → «Медиаматериалы и покупки» → «Просмотреть» или настройки учётной записи → '
              '«Страна/регион» → изменение страны. Примите условия и укажите действительные данные для нового региона. '
              'Apple может потребовать подходящий способ оплаты; вариант «Нет» доступен не всегда. Выдумывать адрес и телефон не нужно.\n\n'
              'Некоторый контент может быть недоступен в новом регионе. После изменения платёжных данных подписки при необходимости '
              'оформляются заново, если доступны в этой стране. Не меняйте регион, пока не проверите важные подписки и iCloud.\n\n'
              'После установки войдите в тот же аккаунт ChatGPT. Повторно покупать подписку в приложении не нужно.')
        if apple_region and identity!='chatgpt' and not prior:
            body=body.removesuffix('\n\nПосле установки войдите в тот же аккаунт ChatGPT. Повторно покупать подписку в приложении не нужно.')
    else:
        body=PLATFORM_QUESTION
    from .auto_policy import consultation_evidence
    return {'reply':body,'summary':'Инструкция установки без гарантии APK, доступа по региону или обязательного варианта None. Покупка подписки повторно не предлагается.',
            'handoff':False,'source_ids':[],
            'evidence':consultation_evidence(body,'installation',
                        diagnostic='Этап: инструкция установки. Платформа='+('android' if android else 'ios' if ios else 'desktop' if desktop else 'не указана')+
                            '; APK третьей стороны не проверялся по подписи; оплата и активация не проверялись. Условия Apple сверены 05.11.2026; официальная страница загрузки для компьютера сверена 07.11.2026.')}
