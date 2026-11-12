"""One structured reading of the current turn.

The model decides intent, service and tariff. Code only checks those ids against
the catalogue, checks prices in the draft, and decides whether the draft states
a result that the owner must confirm. Keyword hints are not decisions.
"""
import re

from .dialog_state import load, save
from .reply_assertion import asserts_outcome, collects_case_data


INTENTS = ('price', 'how_to', 'list_options', 'choose_tariff', 'capabilities',
           'payment_issue', 'other')
CLASSES = ('collect', 'instruct', 'catalog_price', 'outcome', 'promise', 'unverified')
VPN_SENTENCE = ('Для доступа к сервисам лучше включить VPN и по возможности всегда '
                'использовать одну и ту же локацию.')
UNDERSTANDING_INSTRUCTIONS = (
    'Разбери текущую реплику клиента по истории, состоянию диалога, глоссарию и спискам тарифов и номиналов. '
    'glossary — разговорные названия и слова тарифов. Это подсказка смысла. '
    'intent — тип вопроса. service_id и tariff_id только из tariffs, иначе пустая строка. '
    'Короткая реплика без нового сервиса остаётся на dialog_state.service_id. '
    'topic_changed=true только если клиент сам назвал другой сервис. '
    'Сумма или номинал в таком продолжении относится к текущему сервису и региону, а не к другому товару с похожей цифрой. '
    'region — код или название региона из nominals, если речь о пополнении. '
    'Если клиент назвал регион, отвечай только его номиналами. Если регион уже есть в dialog_state.region и нового региона нет, оставь его. '
    'Нет такого номинала в этом регионе — так и скажи и предложи номиналы из списка, не переключайся на другой сервис. '
    'confidence=high, только когда сервис или тариф понятен однозначно. '
    'matched_tariff_ids — все id тарифов текущего сервиса, которые подходят под слова клиента. '
    'Если их два или три, confidence=low, clarify_ids равны только им, tariff_id пустой, '
    'reply — один короткий вопрос ровно с этими вариантами. Остальные тарифы сервиса не перечисляй. '
    'Обычное Pro без слова про год — месячный тариф, если в списке есть и месяц, и год. '
    'Если выбран один тариф, не перечисляй остальные. '
    'questions — все отдельные вопросы этого обращения, коротко и с названием из каталога, если оно известно. '
    'На каждый вопрос ответь отдельно, с пустой строкой между мыслями. В обычном ответе не нумеруй пункты 1. 2. 3. '
    'Нумеруй только настоящую инструкцию, как оплатить или что сделать после оплаты, и только шагами из payment. '
    'Пиши как живой продавец в личке: коротко, тепло, по-человечески. '
    'Короткие фразы, между мыслями пустая строка. Не канцелярия и не сплошной абзац. '
    'Точку ставь там, где фраза правда кончилась. Скобка или один эмодзи уместны редко, не в каждой реплике. '
    'Не переспрашивай факты из known_facts. Не утверждай, что оплата прошла, подписка активирована или возврат сделан. '
    'Цены в reply только из tariffs.price и nominals.price. '
    'payment_asked=true, если клиент спрашивает, как или где оплатить. '
    'Тогда шаги оплаты бери из payment: кнопки и способы оттуда. '
    'Что нужно после оплаты, бери из payment.modes для этого сервиса. '
    'Кнопку «Я оплатил - написать Александру» не называй. '
    'Токен сессии, Organization ID и Perplexity User ID — это данные после оплаты, не способ оплаты. '
    'Для нового аккаунта ChatGPT Plus кнопки активации нет. '
    'Нужен новый аккаунт в любом сервисе или нет почты для регистрации — тема new_account_email: сначала gmail.com без прежнего аккаунта в сервисе, иначе новая outlook.com от нас. '
    'setup_email, setup_password и setup_recovery заполняй только тем, что клиент уже написал: '
    'адрес нового ящика outlook.com, пароль для него и настоящая почта клиента для восстановления. Иначе пустая строка. '
    'fulfillment_choice для Z.ai: email, link или пусто, если способ ещё не выбран. '
    'Если клиент сообщает об оплате, попроси в одном сообщении номер заказа из бота или FreeKassa и чек из банка (скриншот или PDF), если их ещё нет в последних сообщениях. '
    'Если receipt_attached=true, чек уже есть: не проси его снова и не описывай содержимое файла. '
    'order_number — только цифры номера, который клиент уже назвал, иначе пусто. '
    'receipt_amount_unverified — сумма с чека, если ты её разобрал, иначе пусто. Чек сам по себе оплату не подтверждает. '
    'advisories_already_said — советы, которые уже прозвучали в этом разговоре. Не повторяй их. '
    'vpn_advice=true только если вопрос про страну, доступ, соединение или VPN и vpn_location ещё нет в advisories_already_said. '
    'Тогда напиши, что лучше включить VPN и по возможности всегда одна и та же локация. '
    'Если клиент снова прямо спрашивает про этот совет, добавь его id в reask_advisories и можно повторить. '
    'Иначе vpn_advice=false и reask_advisories пустой. '
    'Изображение смотри вместе с текстом. Это может быть скрин ошибки или чек. Чек оплату не подтверждает. '
    'Голос уже расшифрован в тексте сообщения. '
    'Пиши о себе только в мужском роде: «рад помочь», «проверил», «уточнил», «готов». '
    'К клиенту только на «вы», даже если он пишет на «ты». Не заканчивай ответ фразами «Рад помочь», «Обращайтесь», «Если хотите, могу…». '
    'Цены клиенту только в рублях (₽): из каталога, а если тарифа в каталоге нет — пересчёт из official_prices по правилу. Доллары, тенге и цену за 100 кредитов в ответе не пиши. '
    'Если в сообщении несколько вопросов (цена и как оформить, цена и что входит), ответь на каждый. '
    'Перечисляя тарифы сервиса, у каждого называй цену в ₽ из каталога. '
    'Рассказывая о тарифах, моделях и сервисах, не давай ссылок на источники и не пиши «по официальным данным», «данные официального сайта», «проверено»: это и так понятно. '
    'Работает ли SuperGrok с Grok Bot — тема supergrok_grokbot. '
    'Если клиент оплатил и ждёт дольше обычного (вчера, несколько часов, «до сих пор») — тема activation_overdue, а не срок активации. '
    'Нужен ли VPN из России — тема vpn_needed: да, нужен для всех сервисов, включая ChatGPT. Продлить CapCut на своём аккаунте — capcut_renewal. '
    'Оплата из-за границы, Kaspi, иностранная или белорусская карта, PayPal — тема foreign_payment, не card_payment. '
    'На вопрос о текущих моделях сервиса отвечай только по official_research: назови точные названия и версии моделей оттуда, самую новую первой, и в каком тарифе она доступна; своих не добавляй. Ссылки на источники клиенту не давай. Не предлагай «подготовить таблицу». Если данных нет — скажи, что уточнит Александр. '
    'conflict: none — спокойный тон, annoyed — раздражён без угрозы, '
    'high — злится, угрожает возвратом, чарджбеком или жалобой, либо тот же вопрос уже повторяется второй раз и дольше без решения. '
    'conflict_reason — короткая причина для владельца, пусто если conflict не high. '
    'Не ставь high из-за одной неуверенности или пробела в данных: тогда ответь по известному и уточни недостающее. '
    'Сообщения history с authority=owner — слова Александра. Они главнее черновика: '
    'не противоречь им, не повторяй их дословно и не спрашивай заново то, что он уже спросил. '
    'Продолжай с его последней реплики. owner_alignment: follows, contradicts, repeats или reasks. '
    'Если таких сообщений нет, owner_alignment=follows. '
    'faq — ответы владельца. Если known=true, передай смысл answer. Дословно копировать необязательно. '
    'Рядом можно назвать цену и тариф из tariffs или nominals. Это не новое условие. '
    'beyond_owner_fact=true только если в ответе есть срок, гарантия или другое условие, которого нет в answer и нет среди цен каталога. '
    'Если у темы есть collect, это сбор сведений, а не текст обещания. '
    'Для возврата и любой другой темы с collect заполни refund_order (номер заказа), refund_service и refund_reason только тем, что уже есть в последних сообщениях, иначе пустая строка. '
    'Если у темы с collect есть answer, передай его смысл и попроси недостающее из collect. Не обещай результат. '
    'В таком ответе больше ничего не добавляй: ни инструкции по оплате, ни шагов активации. '
    'Если на вопрос отвечает тема из faq, отвечай по ней, а не товарами каталога. Сроки из других тем к ней не добавляй. '
    'owner_topic выбирай, только если клиент спрашивает именно об этой теме. '
    '«Как оформить», «как купить», «как оплатить», «что нужно кроме оплаты» до оплаты — это payment_asked=true и owner_topic=none, а не тема про кнопку активации. '
    'Пополнение App Store или iCloud: пока неизвестны регион Apple ID и сумма, сначала спроси регион и сумму одним вопросом, номиналы не перечисляй. '
    'Просьба посоветовать VPN или помочь с ним — тема vpn_help: только два VPN из ответа владельца, по настройке VPN не подсказываем. '
    'Скачать приложение на телефон — тема app_download. Блокировка аккаунта Claude — claude_account_blocked. '
    'Бонусная или подарочная подписка от курса, почта с курса — course_bonus. '
    'Пополнение кредитов ChatGPT — отдельный товар каталога: service_id бери у строки «Пополнение кредитов ChatGPT», не у ChatGPT. '
    'Срок подписки, можно ли на год — subscription_term: год только если такой тариф есть в tariffs. Риск блокировки Claude — claude_block_risk. Claude просит документ или номер — claude_verification. '
    'Ошибки: оплата в боте не проходит — payment_error; вход в аккаунт, коды, сеанс — login_problem; непонятная ошибка в сервисе — service_error; пропала функция — feature_missing; установка и настройка программ — tech_setup; Claude проверяет запрос — claude_review_pending. Ошибка активации — по-прежнему activation_failed. '
    'Сервиса нет в каталоге — off_catalog: refund_service = название сервиса, refund_reason = тариф, регион аккаунта и срок, только если клиент их написал. '
    'Пополнить DeepSeek или другой API, которого нет в каталоге, — deepseek_topup. Ссылка на оплату не в долларах — linkpay_currency; если клиент не может сменить страну в VPN — linkpay_no_vpn. '
    'Продлить сервис, где оформляли по почте и паролю, — renewal_same_account. Gemini или Google One — gemini_google. Приложение ChatGPT для компьютера — chatgpt_desktop_app. '
    'Сервис просит номер телефона (ChatGPT, Codex, Claude) — phone_number; Claude просит документ — claude_verification. Нужен ли VPN за границей — vpn_abroad. '
    'Клиент пишет не по-русски — отвечай на его языке. '
    'Codex — тема codex. Готовый аккаунт Claude — claude_ready_account. Как активировать код App Store или iCloud — appstore_redeem. '
    'Почта клиента в тексте заменена на [email скрыт]: значит, клиент прислал почту. '
    'Монетки: начисляются только по реферальной программе — 10% с первой покупки каждого приглашённого; ссылка в @neirosetkaa_bot, главное меню → «Мой профиль» → «Пригласить друга»; тратятся на оплату подписки в боте. Других способов нет, не выдумывай задания или бонусы за покупки. '
    'Промокод только по прямому запросу: NAVSEX или CHATIK10, скидка 10%. '
    'Генерация фото или видео: всегда скажи, что в нашем боте @neirosetkaa_bot это тоже есть — в главном меню разделы «Изображение», «Видео», «Редактировать...» и «Анимировать...», кредиты — «Купить кредиты». '
    'Модели, их стоимость в кредитах и пакеты кредитов бери только из bot_generation; помогай сравнить модели по note, quality и кредитам. Если bot_generation недоступен, цифр не называй. '
    'Розыгрышей сейчас нет; к Новому году могут появиться — следите за каналом https://t.me/AleksandrOii. '
    'Предложения рекламы, сотрудничества, партнёрства с вашим сервисом — тема partnership: ответ даёт Александр. Вопрос клиента про партнёрскую или реферальную ссылку — это реферальная программа. '
    'Что входит в тариф, чем тарифы или сервисы отличаются, что выбрать — только из service_guide, своими словами и коротко. '
    'Для выбора между ChatGPT и Claude опирайся на service_guide.choose. '
    'Описывай клиенту каждый тариф по service_guide на русском, помогай сравнить тарифы и сервисы между собой. '
    'Наша цена: если тариф есть в tariffs — только цена оттуда. Если тарифа в tariffs нет — пересчитай цену в $ по usd_to_rub.prices (правило: $ × 100 − 10, например $19 → 1890 ₽) и бери число только из этой таблицы. '
    'official_prices в service_guide — цена на сайте сервиса в $; называй её, только если клиент спрашивает о цене на сайте или сравнивает, и прямо говори, что это официальная цена сервиса. '
    'Чего нет в service_guide, не утверждай: скажи, что точные детали уточнит Александр. '
    'Версии моделей называй только из official_research со status=checked: перескажи коротко, ссылки на источники клиенту не давай. '
    'Если official_research нет или status=unavailable, версий не называй и скажи, что актуальный список уточнит Александр. '
    'Тариф Claude Max 20× теперь называется Ultra, Max 5× — Max: если в tariffs старое название, называй его и поясни новое. '
    'Спроси только недостающее. Возврат не обещай. '
    'owner_topic — id такой темы, либо none. Отсутствие номинала в каталоге — не owner_topic. '
    'documented_facts — уже записанные правила. Их можно сказать. Не добавляй к ним новых сроков и гарантий. '
    'Не утверждай, что оплата прошла, подписка активирована или возврат сделан.'
)

DOCUMENTED_FACTS = (
    'Обычная подписка ChatGPT: токен сессии вставляется в окно активации в боте, не в эту переписку.',
    'Россия не входит в официальный перечень доступа Claude. VPN и одна и та же локация — совет владельца. '
    'Это не делает страну официально поддерживаемой.',
    'Гарантия есть на все сервисы, кроме Claude. Других условий гарантии владелец не записал.',
    'Claude может заблокировать аккаунт, гарантии на Claude нет, при блокировке деньги не возвращаются. На остальные сервисы блокировка входит в гарантию. Срок замены не записан.',
    'Активация занимает 5–30 минут. У сервисов с автоактивацией — 2–5 минут.',
    'Возврат не обещать. Собрать номер заказа, сервис и причину. Когда все три есть, отвечает Александр.',
    'Оплата в боте по СБП без комиссии. По карте оплата часто не проходит, но попробовать можно. Если не прошла или бот завис: /start в боте и новый заказ.',
    'Подписка всегда на месяц, автосписаний нет. Продление — новая покупка в боте, лучше когда подписка закончилась и аккаунт перешёл на бесплатный тариф.',
    'После активации подписку нельзя перенести на другой аккаунт. Купить подписку для другого человека можно.',
    'Чек не обязателен. При вопросах по заказу нужны чек из банка и номер заказа FreeKassa.',
    'Кнопка «Активировать подписку» находится в боте в сообщении после оплаты и действует 2 часа. Повторную активацию выдаёт Александр по номеру заказа.',
    'Активировать подписку можно, когда аккаунт на бесплатном тарифе. Оплата при этом сохраняется.',
    'После активации подписку нельзя перенести на другой аккаунт, возврат за активированную подписку не оформляется.',
    'Самостоятельная активация по кнопке есть только у ChatGPT, Claude и Perplexity.',
    'Описание возможностей сервиса и тарифов — только по актуальным официальным источникам, не по памяти.',
)
_RUBLE = re.compile(r'(\d{2,7})\s*(?:₽|руб(?:лей|ля|ль)?)', re.I)
_RUBLE_SUB = _RUBLE
# "не обещаю" refuses a promise. A bare "обещаю" still blocks the draft.
_PROMISE = re.compile(r'(?i)(?<!не )обещаю|(?<!не )гарантирую|точно\s+активир|возврат\s+одобрен|уже\s+оформил')
_INTERNAL = re.compile(
    r'(?i)уже\s+известно|недостаточно\s+без|результат\s+операции\s+пока\s+не\s+подтвержд|'
    r'часть\s+условий\s+пока\s+не\s+подтверждена|этап\s*:')


def burst_and_window(db, chat, limit=10):
    rows = db.execute(
        '''SELECT mid, role, text FROM messages
           WHERE chat=? AND role IN ('client','assistant','owner')
           ORDER BY mid DESC LIMIT ?''', (chat, limit)).fetchall()
    window = [{'role': row['role'],
               'authority': 'owner' if row['role'] == 'owner' else 'untrusted',
               'text': row['text'] or ''} for row in reversed(rows)]
    burst = []
    for message in reversed(window):
        if message['role'] in ('assistant', 'owner') and burst:
            break
        if message['role'] == 'client':
            burst.append(message['text'])
    burst.reverse()
    return '\n'.join(part for part in burst if part).strip(), window


def catalog_index(facts, limit=400):
    rows = []
    for row in (facts or {}).get('catalog') or []:
        if not isinstance(row, dict):
            continue
        price = row.get('catalog_price_rub')
        if type(price) is not int or price <= 0:
            continue
        rows.append({'id': str(row.get('service_key') or '') + '|' + str(row.get('plan') or ''),
                     'service_id': str(row.get('service_key') or ''),
                     'service': str(row.get('service') or ''),
                     'plan': str(row.get('plan') or ''),
                     'price': price})
        if len(rows) >= limit:
            break
    return rows


def nominal_index(facts, limit=400):
    rows = []
    for region in ((facts or {}).get('appstore') or {}).get('regions') or []:
        if not isinstance(region, dict):
            continue
        for denom in region.get('denoms') or []:
            price = denom.get('price_rub') if isinstance(denom, dict) else None
            if type(price) is not int or price <= 0:
                continue
            rows.append({'region': str(region.get('name') or ''),
                         'nominal': str(denom.get('nominal') or ''),
                         'price': price})
            if len(rows) >= limit:
                return rows
    return rows


def hint_services(text, catalog):
    """Untrusted hints only. The model may ignore them."""
    from .catalog_context import service_mentions
    found = service_mentions(text or '', catalog or [])
    return [{'service_id': key, 'label': label} for key, label in found.items()]


def build_context(db, chat, facts):
    burst, window = burst_and_window(db, chat)
    state = load(db, chat)
    catalog = (facts or {}).get('catalog') or []
    tariffs = catalog_index(facts)
    from .payment_case import receipt_files
    from .glossary import for_context
    from .faq import for_context as faq_context
    from .payment_guide import for_context as payment_context
    try:
        glossary = for_context(facts)
    except Exception:
        glossary = {'services': [], 'phrases': [], 'tariff_words': [], 'load_error': True}
    try:
        faq = faq_context()
    except Exception:
        faq = []
    try:
        payment = payment_context(facts)
    except Exception:
        payment = {}
    region = active_region(burst, state)
    return {'burst': burst,
            'receipt_attached': bool(receipt_files(db, chat)),
            'glossary': glossary,
            'history': window,
            'dialog_state': {'service_id': state.get('service_key') or '',
                             'service': state.get('service_label') or '',
                             'tariff': state.get('option_label') or '',
                             'region': region,
                             'collected': state.get('collected') or {},
                             'shown': state.get('offered') or []},
            'tariffs': tariffs,
            'nominals': nominal_index(facts),
            'owner_statements': [item['text'] for item in window if item.get('authority') == 'owner' and item.get('text')],
            'faq': faq,
            'payment': payment,
            'bot_navigation': (facts or {}).get('bot_navigation') or {},
            'advisories_already_said': advisories_already(window, state),
            'documented_facts': list(DOCUMENTED_FACTS),
            'service_guide': _service_guide(burst, state.get('service_key') or ''),
            'usd_to_rub': _usd_context(burst, state.get('service_key') or ''),
            'bot_generation': _bot_generation(facts, burst),
            'hints': hint_services(burst, catalog)}


def _verbatim(topic):
    try:
        from .faq import verbatim
        return verbatim(topic)
    except Exception:
        return False


_GENERATION = re.compile(r'(?i)генер|нарис|картин|изображен|(?:сделать|создать|оживить)\s+(?:фото|видео)|ролик|анимир|анимац|оживи|редактир|отредакт|'
                         r'нейросет\w*\s+для|кредит\w*\s+(?:в|для)\s+бот|модел|nano\s*banana|flux|ideogram|kling|клинг|veo|'
                         r'sora|seedance|imagine|midjourney|миджорни')


def _bot_generation(facts, burst):
    """The bot's own photo/video sections: only when the client talks about generating."""
    data = (facts or {}).get('generation') if isinstance((facts or {}).get('generation'), dict) else {}
    if not _GENERATION.search(str(burst or '')):
        return {'status': 'not_requested'}
    return data or {'status': 'not_connected'}


_MODELS_Q = re.compile(r'(?i)модел|верси[яюи]|какой\s+(?:gpt|гпт|клод|claude|джемини|gemini|грок|grok)\s+(?:сейчас|стоит|внутри|последн)|'
                       r'что\s+нового|последн\w+\s+(?:модел|верси|обновлен)|\bgpt-?\d|\bopus\b|\bsonnet\b|\bhaiku\b')
_RESEARCH_ALIASES = {'chatgpt': ('chatgpt', 'gpt', 'гпт', 'чатгпт', 'openai'), 'codex': ('codex', 'кодекс'),
                     'claude': ('claude', 'клод', 'клауд', 'anthropic', 'opus', 'sonnet'), 'gemini': ('gemini', 'джемини', 'гемини'),
                     'supergrok': ('grok', 'грок'), 'perplexity': ('perplexity', 'перплекс'), 'cursor': ('cursor', 'курсор'),
                     'midjourney': ('midjourney', 'миджорни', 'мидджорни'), 'kling': ('kling', 'клинг'),
                     'suno': ('suno', 'суно'), 'runway': ('runway', 'ранвей'), 'heygen': ('heygen', 'хейген')}
_STATE_TO_PRODUCT = {'grok': 'supergrok'}


def foreign_language(text):
    """The client writes in a non-Cyrillic language (English, Turkish, ...)."""
    text = str(text or '')
    latin = len(re.findall(r'[A-Za-zÀ-ÿĞğŞşİıÇçÖöÜü]', text))
    cyrillic = len(re.findall(r'[А-Яа-яЁё]', text))
    words = re.findall(r'[A-Za-zÀ-ÿĞğŞşİıÇçÖöÜü]{2,}', text)
    brands = {'chatgpt', 'gpt', 'claude', 'plus', 'pro', 'max', 'go', 'grok', 'gemini', 'codex', 'cursor', 'suno',
              'perplexity', 'ok', 'vpn', 'app', 'store', 'icloud', 'apple', 'id', 'premium'}
    plain = [w for w in words if w.casefold() not in brands]
    if len(re.findall(r'[ӘәҒғҚқҢңӨөҰұҮүҺһІіЎўЇїЄєҐґ]', text)) >= 2:
        return True  # Kazakh, Uzbek Cyrillic, Ukrainian: Cyrillic, but not Russian
    return cyrillic == 0 and latin >= 6 and len(plain) >= 2


def research_request(context):
    """A fixed official lookup when the client asks which models a service has now."""
    burst = str((context or {}).get('burst') or '')
    if not _MODELS_Q.search(burst):
        return None
    found = []
    for product, names in _RESEARCH_ALIASES.items():
        if any(_compact_mentions(burst, name) if len(name) >= 4 else
               re.search(r'(?i)(?<![а-яёa-z])' + re.escape(name), burst) for name in names):
            found.append(product)
    if not found:
        state = str(((context or {}).get('dialog_state') or {}).get('service_id') or '')
        state = _STATE_TO_PRODUCT.get(state, state)
        if state in _RESEARCH_ALIASES:
            found.append(state)
    if not found:
        return None
    return {'topic': 'models', 'products': found[:2]}


_COMPARE = re.compile(r'(?i)что\s+(?:лучше|выбрать|посовету|взять)|чем\s+отлича|отличи|разниц|сравн|какой\s+(?:сервис|лучше|тариф)|какую\s+нейросет|vs\b|или\s+(?:chatgpt|gpt|гпт|claude|клод|gemini|grok)')


def _service_guide(burst='', state_service=''):
    """Only the services in this conversation; the whole guide when the client compares or chooses."""
    try:
        from .service_guide import for_context
        guide = for_context()
    except Exception:
        return {}
    services = guide.get('services') or {}
    if _COMPARE.search(str(burst or '')):
        return guide
    aliases = {'chatgpt': ('gpt', 'гпт', 'чатгпт', 'chatgpt', 'codex', 'кодекс'), 'claude': ('клод', 'клауд'),
               'gemini': ('джемини', 'гемини'), 'perplexity': ('перплекс',), 'cursor': ('курсор',),
               'midjourney': ('миджорни', 'мидджорни'), 'grok': ('грок',), 'suno': ('суно',),
               'elevenlabs': ('элевен', 'eleven'), 'heygen': ('хейген',), 'runway': ('ранвей',),
               'lovable': ('лавабл',), 'gamma': ('гамма',), 'kling': ('клинг',), 'canva': ('канва',), 'zoom': ('зум',),
               'spotify': ('спотифай', 'спотик'), 'youtube': ('ютуб', 'ютюб'), 'notion': ('ноушн', 'ноушен', 'нотион'),
               'freepik': ('фрипик', 'magnific', 'магнифик'), 'krea': ('креа',), 'github': ('гитхаб', 'copilot', 'копилот'),
               'openrouter': ('опенроутер', 'deepseek', 'дипсик'), 'telegram_premium': ('телеграм премиум', 'тг премиум', 'premium'),
               'kimi': ('кими',), 'zai': ('z.ai', 'glm'), 'make': ('мейк',), 'capcut': ('капкат', 'кап кат'),
               'higgsfield': ('хиггс',), 'genspark': ('генспарк',), 'manus': ('манус',)}
    def said(name):
        if len(name) < 4:
            return bool(re.search(r'(?i)(?<![а-яёa-z])' + re.escape(name), str(burst or '')))
        return _compact_mentions(burst, name)
    wanted = {key for key in services if key == state_service or said(key)
              or any(said(alias) for alias in aliases.get(key, ()))}
    return dict(guide, services={key: value for key, value in services.items() if key in wanted},
                omitted='Сводки других сервисов не переданы: клиент о них не спрашивал.')


def _allowed_prices(tariffs, nominals, service_id, tariff_id, extra=()):
    """Every price in the facts given to the model. A number outside this set is invented."""
    prices = {row['price'] for row in tariffs if type(row.get('price')) is int}
    prices |= {row['price'] for row in nominals if type(row.get('price')) is int}
    for row in nominals:
        # «3000 ₽ — 4250 ₽»: the face value of an App Store code is a catalog fact too.
        face = re.fullmatch(r'\s*(\d{2,7})\s*(?:₽|руб\w*|rub)?\s*', str(row.get('nominal') or ''), re.I)
        if face:
            prices.add(int(face.group(1)))
    prices |= {value for value in extra or () if type(value) is int}
    return prices


_USD = re.compile(r'\$\s?(\d+(?:[.,]\d{1,2})?)|(\d+(?:[.,]\d{1,2})?)\s?(?:\$|долл|usd)', re.I)


def usd_to_rub(amount):
    """Owner 12.11.2026: a tariff not in the catalog is priced $ × 100 − 10 (19$ → 1890 ₽)."""
    value = float(str(amount).replace(',', '.')) * 100 - 10
    return int(round(value / 10.0)) * 10  # owner: round to whole tens (24.99$ → 2490 ₽)


def usd_table(guide, burst):
    """$ amounts from the official prices in this turn's guide and from the client's message, in ₽."""
    texts = [str(burst or '')]
    for row in ((guide or {}).get('services') or {}).values():
        texts += [str(value) for value in ((row or {}).get('official_prices') or {}).values()]
    table = {}
    for text in texts:
        for match in _USD.finditer(text):
            raw = match.group(1) or match.group(2)
            try:
                value = float(raw.replace(',', '.'))
            except ValueError:
                continue
            if 0 < value <= 5000:
                table['$' + raw.replace(',', '.')] = usd_to_rub(raw)
    return table


def _usd_context(burst, state_service):
    table = usd_table(_service_guide(burst, state_service), burst)
    return {'rule': 'Тарифа нет в каталоге — наша цена в ₽ = цена в $ × 100 − 10. Есть в tariffs — только цена из tariffs.',
            'prices': table}


def _context_prices(context):
    prices = list(((context or {}).get('usd_to_rub') or {}).get('prices', {}).values())
    # Credit packs of the bot's own generation are catalog prices too.
    generation = (context or {}).get('bot_generation')
    if isinstance(generation, dict):
        prices.extend(row['price_rub'] for row in generation.get('packs') or []
                      if isinstance(row, dict) and type(row.get('price_rub')) is int)
    return tuple(prices)


COURSE_RECEIVED = 'Спасибо! Александр скоро проверит информацию и ответит по оформлению бонусной подписки.'


def composed_card(answer):
    """Payment card or owner request assembled by code, not written by the model."""
    return bool(((answer or {}).get('evidence') or {}).get('composed_by_code'))


_EMAIL = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
_SETUP_LABELS = {'setup_email': 'какой адрес дать новой почте outlook.com',
                 'setup_password': 'какой пароль поставить на неё',
                 'setup_recovery': 'какую вашу почту привязать как резервную'}


def _email_in(text):
    match = _EMAIL.search(str(text or ''))
    return match.group(0) if match else ''


def _setup_missing_text(missing):
    items = [_SETUP_LABELS[key] for key in ('setup_email', 'setup_password', 'setup_recovery') if key in (missing or [])]
    if not items:
        return ''
    if len(items) == 1:
        return 'Осталось написать, ' + items[0] + '.'
    return 'Осталось написать:\n' + '\n'.join(f'{n}. {t[:1].upper() + t[1:]}.' for n, t in enumerate(items, 1))


def _setup(understanding):
    found = {}
    for key in ('setup_email', 'setup_password', 'setup_recovery'):
        value = ' '.join(str((understanding or {}).get(key) or '').split())
        if value and len(value) <= 200:
            found[key] = value
    return found


def _choice(understanding):
    value = str((understanding or {}).get('fulfillment_choice') or '').strip().casefold()
    return value if value in ('email', 'link') else ''


def _id_list(understanding, key):
    values = []
    for item in (understanding or {}).get(key) or []:
        text = str(item or '').strip()
        if text and text not in values:
            values.append(text)
        if len(values) == 4:
            break
    return values


def validate(understanding, context):
    """Drop ids that are not in the catalogue. Do not invent a price."""
    tariffs = context.get('tariffs') or []
    by_tariff = {row['id']: row for row in tariffs}
    services = {row['service_id'] for row in tariffs}
    services |= {str(row.get('id')) for row in ((context.get('glossary') or {}).get('services') or []) if row.get('id')}
    if context.get('nominals'):
        services.add('appstore')
    service_id = str((understanding or {}).get('service_id') or '')
    tariff_id = str((understanding or {}).get('tariff_id') or '')
    if service_id not in services:
        service_id = ''
    chosen = by_tariff.get(tariff_id)
    if not chosen:
        tariff_id = ''
    elif service_id and chosen['service_id'] != service_id:
        tariff_id = ''
        chosen = None
    elif not service_id and chosen:
        service_id = chosen['service_id']
    confidence = str((understanding or {}).get('confidence') or 'high')
    if confidence not in ('high', 'low'):
        confidence = 'high'
    return {'intent': understanding.get('intent') if understanding.get('intent') in INTENTS else 'other',
            'service_id': service_id,
            'tariff_id': tariff_id,
            'tariff': chosen,
            'confidence': confidence,
            'topic_changed': bool(understanding.get('topic_changed')),
            'known_facts': [str(item) for item in understanding.get('known_facts') or [] if isinstance(item, str)][:12],
            'missing_facts': [str(item) for item in understanding.get('missing_facts') or [] if isinstance(item, str)][:12],
            'questions': _texts(understanding.get('questions'), 6),
            'vpn_advice': bool((understanding or {}).get('vpn_advice')),
            'reask_advisories': _id_list(understanding, 'reask_advisories'),
            'payment_asked': bool((understanding or {}).get('payment_asked')),
            'fulfillment_choice': _choice(understanding),
            'setup': _setup(understanding),
            'region': '',
            'conflict': _conflict(understanding),
            'conflict_reason': str((understanding or {}).get('conflict_reason') or '').strip()[:300],
            'owner_alignment': _alignment(understanding, context),
            'owner_topic': _owner_topic(understanding, context),
            'beyond_owner_fact': bool((understanding or {}).get('beyond_owner_fact')),
            'refund_collected': _refund_collected(understanding),
            'allowed_prices': _allowed_prices(tariffs, context.get('nominals') or [], service_id, tariff_id,
                                              _context_prices(context))}


def _refund_collected(understanding):
    raw = (understanding or {}).get('refund_collected')
    if not isinstance(raw, dict):
        raw = {'order_number': (understanding or {}).get('refund_order'),
               'service': (understanding or {}).get('refund_service'),
               'reason': (understanding or {}).get('refund_reason')}
    cleaned = {}
    for key in ('order_number', 'service', 'reason'):
        value = ' '.join(str((raw or {}).get(key) or '').split())
        if value and len(value) <= 200:
            cleaned[key] = value
    return cleaned


def _owner_topic(understanding, context):
    value = str((understanding or {}).get('owner_topic') or 'none').strip()
    if value == 'none':
        return 'none'
    known = {row.get('id') for row in (context or {}).get('faq') or [] if isinstance(row, dict)}
    if not known:
        from .faq import load_faq
        try:
            known = {row['id'] for row in load_faq()['topics']}
        except Exception:
            known = set()
    return value if value in known else 'none'


def _conflict(understanding):
    value = str((understanding or {}).get('conflict') or 'none')
    value = value if value in ('none', 'annoyed', 'high') else 'none'
    # Owner 12.11.2026: the owner is called only for a named reason, not on a vague feeling.
    if value == 'high' and not CONFLICT_REASON.search(str((understanding or {}).get('conflict_reason') or '')):
        return 'annoyed'
    return value


# Reasons the model may call the owner for: refund, chargeback, complaint, threat,
# insults, «позовите человека», a problem left unsolved after several replies.
CONFLICT_REASON = re.compile(
    r'(?i)возврат|верн[иу]|чарджбек|chargeback|оспор|жалоб|пожалу|\bсуд|прокурат|роспотреб|полици|угрож|'
    r'мошенн|обман|развод|кидал|оскорб|\bмат\b|груб|челове|оператор|александр|'
    r'не\s+работа|не\s+реш|без\s+решен|третий\s+раз|повторя|снова|опять|refund|scam|complain')


OWNER_FOLLOWUP = 'Остальное по вашему вопросу уточнит Александр и ответит здесь.'


def _alignment(understanding, context):
    owners = [item for item in (context or {}).get('history') or [] if item.get('authority') == 'owner' and item.get('text')]
    if not owners:
        return 'follows'
    value = str((understanding or {}).get('owner_alignment') or 'follows')
    return value if value in ('follows', 'contradicts', 'repeats', 'reasks') else 'follows'


def _topic_known(topic, context):
    from .faq import usable_answer
    return bool(usable_answer(topic, (context or {}).get('faq')))


def _flat(text):
    return ' '.join(str(text or '').casefold().split())


def _repeats_assistant(reply, context):
    """The same text was already sent twice. One repeat is allowed: clients do ask again."""
    current = _flat(reply)
    if len(current) < 12:
        return False
    said = 0
    for item in (context or {}).get('history') or []:
        if item.get('role') == 'assistant' and _flat(item.get('text')).endswith(current):
            said += 1
    return said >= 2


_PAY_HOW = re.compile(
    r'(?i)как\s+(?:мне\s+|нам\s+|его\s+|е[её]\s+|это\s+|можно\s+|тогда\s+|теперь\s+)*'
    r'(?:оформ|купи|оплат|оплач|приобр|заказа|подключи)|'
    r'инструкци\w*\s+(?:по\s+|для\s+)?(?:оплат|покупк|оформлен)|где\s+(?:оплат|купи|оформ)|'
    r'что\s+(?:ещ[её]\s+)?(?:нужно|надо|потребуется|понадобится)\s+(?:будет\s+)?(?:кроме|помимо|после)\s+оплат|'
    r'что\s+(?:нужно|надо)\s+(?:будет\s+)?для\s+(?:оформлен|покупк|оплат)')
_ALREADY_PAID = re.compile(r'(?i)\bоплатил[аи]?\b|\bоплачено\b|оплата\s+прошла|после\s+оплаты\s+(?:не|нет|ничего)')


def asks_how_to_pay(text):
    """The client asks how to buy or what is needed besides paying. Not a report of a payment."""
    value = str(text or '')
    return bool(_PAY_HOW.search(value)) and not _ALREADY_PAID.search(value)


def states_price(reply):
    """The draft names a ruble amount. This does not look at the client's words."""
    return bool(_RUBLE.search(str(reply or '')))


def _texts(values, limit):
    return [str(item).strip() for item in values or [] if isinstance(item, str) and str(item).strip()][:limit]


def clarify_options(understanding, context):
    """Keep only catalogue ids the model offered. Do not invent a third choice."""
    tariffs = context.get('tariffs') or []
    by_id = {row['id']: row for row in tariffs}
    by_service = {}
    for row in tariffs:
        by_service.setdefault(row['service_id'], row)
    found = []
    seen = set()
    for raw in (understanding or {}).get('clarify_ids') or []:
        item = str(raw or '')
        if item in by_id:
            row = by_id[item]
            label = (row['service'] + ' ' + row['plan']).strip()
            key = row['id']
        elif item in by_service:
            row = by_service[item]
            label = row['service']
            key = row['service_id']
        else:
            continue
        if key in seen or not label:
            continue
        seen.add(key)
        found.append({'id': key, 'label': label})
        if len(found) == 3:
            break
    return found


def _clarify_reply(reply, options):
    labels = [item['label'] for item in options]
    folded = str(reply or '').casefold()
    if '?' in str(reply or '') and all(label.casefold() in folded for label in labels):
        return str(reply).strip()
    if len(labels) == 2:
        body = labels[0] + ' или ' + labels[1]
    else:
        body = ', '.join(labels[:-1]) + ' или ' + labels[-1]
    return 'Уточните, пожалуйста: ' + body + '?'


def _answer_blocks(reply):
    parts = re.split(r'(?m)^\s*\d+[.)]\s+', str(reply or ''))
    numbered = [part.strip() for part in parts[1:] if part.strip()]
    if len(numbered) >= 2:
        return numbered
    return [part.strip() for part in re.split(r'\n+', str(reply or '')) if part.strip()]


def _catalog_tokens(context):
    tokens = set()
    for row in context.get('tariffs') or []:
        for part in (row.get('service'), row.get('plan'), row.get('service_id')):
            for word in re.findall(r'[A-Za-zА-Яа-яЁё0-9]{3,}', str(part or '')):
                tokens.add(word.casefold().replace('ё', 'е'))
    return tokens


def questions_addressed(reply, questions, context):
    """A multi-question turn needs a separate line or point per question."""
    items = [item for item in questions or [] if item]
    if len(items) < 2:
        return True
    blocks = _answer_blocks(reply)
    if len(blocks) < len(items):
        return False
    known = _catalog_tokens(context)
    folded = str(reply or '').casefold().replace('ё', 'е')
    for question in items:
        words = re.findall(r'[A-Za-zА-Яа-яЁё0-9]{3,}', question)
        needed = [word.casefold().replace('ё', 'е') for word in words if word.casefold().replace('ё', 'е') in known]
        if any(word not in folded for word in needed):
            return False
    return True


def _with_vpn(reply, needed):
    if not needed:
        return reply
    folded = reply.casefold()
    if ('vpn' in folded or 'впн' in folded) and re.search(r'локац|одн\w+ и т\w+ же|одного и того же|одну и ту же', folded):
        return reply
    return (reply.rstrip() + '\n\n' + VPN_SENTENCE).strip()


def classify_draft(reply, allowed_prices):
    """Safe or hold, from what the draft asserts. Client wording is not an input."""
    text = str(reply or '')
    if not text.strip() or _INTERNAL.search(text):
        return {'send': False, 'class': 'unverified',
                'reason': 'Ответ пустой или содержит служебную формулировку.'}
    if _PROMISE.search(text) or asserts_outcome(text):
        return {'send': False, 'class': 'outcome',
                'reason': 'Ответ утверждает результат оплаты, активации, возврата или обещает его.'}
    stated = {int(amount) for amount in _RUBLE.findall(text)}
    if stated - set(allowed_prices or ()) and not snap_prices(text, allowed_prices)[1]:
        return {'send': False, 'class': 'unverified',
                'reason': 'В ответе есть цена, которой нет в переданных ценах каталога или номиналов.'}
    if collects_case_data(text):
        return {'send': True, 'class': 'collect', 'reason': 'Ответ запрашивает сведения и не сообщает результат.'}
    if stated:
        return {'send': True, 'class': 'catalog_price', 'reason': 'Цены в ответе есть в переданных фактах.'}
    return {'send': True, 'class': 'instruct', 'reason': 'Ответ объясняет порядок и не сообщает результат операции.'}


def snap_prices(text, allowed_prices):
    """A ₽ amount a few rubles off a known price is a typo, not an invented price.

    Returns (fixed_text, ok). ok is False when some amount has no single close known price.
    """
    allowed = sorted(set(allowed_prices or ()))
    ok = True

    def fix(match):
        nonlocal ok
        amount = int(match.group(1))
        if amount in allowed:
            return match.group(0)
        near = [p for p in allowed if abs(p - amount) <= max(20, p * 0.02)]
        if len(near) != 1:
            ok = False
            return match.group(0)
        return match.group(0).replace(match.group(1), str(near[0]), 1)

    fixed = _RUBLE_SUB.sub(fix, str(text or ''))
    return fixed, ok


def active_region(burst, state, model_region=''):
    """Region slot: one region named now, otherwise the region already stored."""
    from .apple_matching import ALIASES, region_codes
    named = region_codes(burst or '')
    if len(named) == 1:
        return next(iter(named))
    if len(named) > 1:
        return ''
    saved = str(((state or {}).get('collected') or {}).get('region') or '')
    if saved not in ALIASES:
        saved = str((state or {}).get('region') or '')
    if saved in ALIASES:
        return saved
    model = region_codes(model_region or '')
    if len(model) == 1:
        return next(iter(model))
    return ''


_ADVISORIES = ('vpn_location', 'activation_shop', 'activation_auto')


def _has_number(text, number):
    return bool(re.search(r'(?<!\d)' + str(number) + r'(?!\d)', text))


def advisory_present(advisory_id, text):
    low = str(text or '').casefold().replace('ё', 'е')
    if advisory_id == 'vpn_location':
        return ('vpn' in low or 'впн' in low) and bool(re.search(r'локац|одн\w+ и т\w+ же|одного и того же|одну и ту же', low))
    if advisory_id == 'activation_shop':
        return _has_number(low, 5) and _has_number(low, 30) and 'минут' in low
    if advisory_id == 'activation_auto':
        return _has_number(low, 2) and _has_number(low, 5) and 'минут' in low and 'авто' in low
    return False


def advisories_already(history, state):
    found = {str(item) for item in ((state or {}).get('advisories') or []) if item}
    for item in history or []:
        for advisory_id in _ADVISORIES:
            if advisory_present(advisory_id, (item or {}).get('text')):
                found.add(advisory_id)
    return sorted(found)


_SERVICE_TITLES = {'capcut': 'CapCut', 'suno': 'Suno', 'zoom': 'Zoom', 'openrouter': 'OpenRouter', 'spotify': 'Spotify',
                   'youtube': 'YouTube', 'gemini': 'Gemini', 'grok': 'Grok', 'midjourney': 'Midjourney',
                   'github': 'GitHub', 'krea': 'Krea', 'canva': 'Canva', 'cursor': 'Cursor'}


def _state_plan(validated, context):
    """The remembered tariff belongs to the remembered service only."""
    state = (context or {}).get('dialog_state') or {}
    if str(state.get('service_id') or '') != str(validated.get('service_id') or ''):
        return ''
    return str(state.get('tariff') or '')


def _compact(text):
    return re.sub(r'[^a-zа-яё0-9]+', '', str(text or '').casefold())


def _compact_mentions(burst, name):
    """«кап ката», «капкатом», «Cap-Cut» all mention CapCut. Short names need an exact word."""
    key = _compact(name)
    if len(key) < 4:
        return False
    return key in _compact(burst)


def _named_services(context):
    found = set()
    raw_burst = context.get('burst') or ''
    burst = ' ' + _flat(raw_burst) + ' '
    for item in context.get('hints') or []:
        if isinstance(item, dict) and item.get('service_id'):
            found.add(str(item['service_id']))
    for row in ((context.get('glossary') or {}).get('services') or []):
        names = [row.get('name'), row.get('id'), *(row.get('aliases') or [])]
        for name in names:
            token = _flat(name)
            if token and ((' ' + token + ' ') in burst or burst.strip() == token or _compact_mentions(raw_burst, name)):
                found.add(str(row.get('id') or ''))
    for row in context.get('tariffs') or []:
        if _compact_mentions(raw_burst, row.get('service')) or _compact_mentions(raw_burst, row.get('service_id')):
            found.add(str(row.get('service_id') or ''))
    return {item for item in found if item}


def _names_unknown_service(context):
    """The client names a service we know from the payment rules but it is not the open one."""
    try:
        from .payment_guide import load_guide
        modes = (load_guide().get('modes') or {})
    except Exception:
        modes = {}
    burst = (context or {}).get('burst') or ''
    names = set()
    for row in modes.values():
        if isinstance(row, dict):
            names.update(str(item) for item in (row.get('services') or []) + (row.get('names') or []) if item)
    extra = {'capcut': ('капкат', 'кап кат'), 'suno': ('суно',), 'zoom': ('зум',), 'grok': ('грок',),
             'midjourney': ('миджорни', 'мидджорни'), 'gemini': ('джемини', 'гемини'), 'cursor': ('курсор',),
             'canva': ('канва',), 'openrouter': ('опенроутер',), 'spotify': ('спотифай',)}
    hits = set()
    for name in names:
        if _compact_mentions(burst, name) or any(_compact_mentions(burst, alias) for alias in extra.get(name, ())):
            hits.add(name)
    return hits


def _clear_tariff_if_other(validated, service_id):
    tariff = validated.get('tariff')
    if tariff and tariff.get('service_id') != service_id:
        validated['tariff'] = None
        validated['tariff_id'] = ''


def bind_service(validated, context):
    """Keep the open service when a short follow-up does not name a new one.

    An empty service_id stays empty. The glossary is not a router.
    """
    state_id = str((context.get('dialog_state') or {}).get('service_id') or '')
    named = _named_services(context)
    proposed = validated.get('service_id') or ''
    other = {name for name in _names_unknown_service(context) if name != state_id}
    if other and state_id not in named and not (state_id and _compact_mentions(context.get('burst') or '', state_id)):
        # The client moved to another service. Never answer about the previous one.
        if proposed == state_id or not proposed:
            validated['service_id'] = ''
            validated['tariff'] = None
            validated['tariff_id'] = ''
            validated['topic_changed'] = True
            validated['_other_service'] = sorted(other)[0]
        return validated
    if not proposed:
        if state_id and (not named or state_id in named):
            validated['service_id'] = state_id
            validated['topic_changed'] = False
        return validated
    if proposed in named or (not named and not state_id):
        return validated
    if not named and state_id and proposed != state_id:
        validated['service_id'] = state_id
        validated['topic_changed'] = False
        _clear_tariff_if_other(validated, state_id)
        return validated
    if named and proposed not in named and len(named) == 1:
        only = next(iter(named))
        validated['service_id'] = only
        validated['topic_changed'] = bool(state_id and only != state_id)
        _clear_tariff_if_other(validated, only)
    return validated


def attach_region(validated, understanding, context):
    if validated.get('service_id') != 'appstore':
        validated['region'] = ''
        return validated
    state = context.get('dialog_state') or {}
    validated['region'] = active_region(context.get('burst') or '', state, (understanding or {}).get('region') or '')
    return validated


def _plan_tokens(text):
    return set(re.findall(r'[a-zа-яё0-9+]+', _flat(text)))


def _asked_tariff_before(context):
    last = next((str(row.get('text') or '') for row in reversed((context or {}).get('history') or [])
                 if isinstance(row, dict) and row.get('role') in ('assistant', 'owner')), '')
    return 'Это разные варианты' in last


def _closest_option(options, burst):
    words = _plan_tokens(burst)
    scored = sorted(((len(_plan_tokens(row.get('plan') or '') - words), row) for row in options), key=lambda x: x[0])
    if len(scored) >= 2 and scored[0][0] < scored[1][0]:
        return scored[0][1]
    return None


def _bind_credits(validated, context):
    """«Пополнить кредиты ChatGPT» is its own catalog product, not the ChatGPT subscription."""
    burst = str((context or {}).get('burst') or '')
    last = next((str(row.get('text') or '') for row in reversed((context or {}).get('history') or [])
                 if isinstance(row, dict) and row.get('role') in ('assistant', 'owner')), '')
    state_service = str(((context or {}).get('dialog_state') or {}).get('service_id') or '')
    about_gpt = bool(re.search(r'(?i)gpt|гпт|джипити|codex|кодекс|openai', burst)) or 'chatgpt' in state_service \
        or 'кредит' in state_service or validated.get('service_id') == 'chatgpt'
    other_brand = bool(re.search(r'(?i)\b(?!chatgpt|gpt|codex|openai|credits?\b)[a-z][a-z.]{2,}', burst)) or bool(
        validated.get('_other_service')) or bool(_names_unknown_service(context))
    asked_now = bool(re.search(r'(?i)кредит|credit', burst)) and (about_gpt or not other_brand)
    follow_up = bool(re.search(r'(?i)кредит', last)) and not validated.get('tariff')
    if not (asked_now or follow_up) or validated.get('service_id') not in ('', 'chatgpt', None):
        return
    if (validated.get('tariff') or {}).get('service_id') == 'chatgpt' and not asked_now:
        return
    row = next((r for r in (context or {}).get('tariffs') or [] if 'кредит' in str(r.get('service') or '').casefold()), None)
    if not row:
        return
    if validated.get('service_id') == row['service_id']:
        return
    validated['service_id'] = row['service_id']
    tariff = validated.get('tariff') or {}
    if tariff and tariff.get('service_id') != row['service_id']:
        validated['tariff'] = None
        validated['tariff_id'] = ''


def matching_tariffs(context, service_id):
    """Tariffs of the open service whose names fit the client's words."""
    if not service_id:
        return []
    burst_tokens = _plan_tokens(context.get('burst') or '')
    if not burst_tokens:
        return []
    forms = []
    for spec in ((context.get('glossary') or {}).get('tariff_words') or []):
        words = [_flat(item) for item in spec.get('words') or [] if _flat(item)]
        if any(word in burst_tokens for word in words):
            forms.extend(words)
    rows = [row for row in context.get('tariffs') or [] if row.get('service_id') == service_id]
    scored = []
    for row in rows:
        tokens = _plan_tokens(row.get('plan') or '')
        score = len(tokens & burst_tokens)
        if forms and tokens & set(forms):
            score = max(score, 1)
        if score:
            scored.append((score, row))
    if not scored:
        return []
    burst_flat = _flat(context.get('burst') or '')
    padded = ' ' + burst_flat + ' '
    exact = []
    for _score, row in scored:
        name = _flat(row.get('plan') or '')
        if name and (' ' + name + ' ') in padded:
            exact.append(row)
    if len(exact) == 1 and len(scored) > 1:
        # «новый аккаунт plus» names the longer plan even though «plus» alone matches exactly.
        known = burst_tokens | set(forms)
        rows_ = [row for _score, row in scored]
        shared = set.intersection(*[_plan_tokens(row.get('plan') or '') for row in rows_])
        named = [row for row in rows_ if (_plan_tokens(row.get('plan') or '') - shared) & known]
        if len(named) == 1:
            return named
    if len(exact) == 1:
        return exact
    best = max(score for score, _row in scored)
    winners = [row for score, row in scored if score == best]
    if len(winners) >= 2:
        # «Plus» means plain Plus; «Новый аккаунт с подпиской Plus» only when the client says so.
        known = burst_tokens | set(forms)
        shared = set.intersection(*[_plan_tokens(row.get('plan') or '') for row in winners])
        named = [row for row in winners if (_plan_tokens(row.get('plan') or '') - shared) & known]
        if len(named) == 1:
            return named
        if not named:
            plain = [row for row in winners if not (_plan_tokens(row.get('plan') or '') - known)]
            if len(plain) == 1:
                return plain
    if 'год' not in burst_flat and 'year' not in burst_flat:
        plain = [row for row in winners if 'год' not in _flat(row.get('plan')) and 'year' not in _flat(row.get('plan'))]
        if len(plain) == 1:
            return plain
        if plain:
            winners = plain
    return winners


_ASKS_PRICE = re.compile(r'(?i)сколько\s+(?:это\s+|он[аи]?\s+|будет\s+)?(?:стоит|стоят|стоить)|цен[аыу]\b|\bстоимост|\bпоч[ёе]м\b|\bprice|how much|\bcost')


def _with_price_line(card, context, service_id, tariff, kind):
    """«Perplexity Pro цена? Как активировать?» — the card alone skips the price."""
    if kind == 'appstore' or not _ASKS_PRICE.search(str((context or {}).get('burst') or '')) or '₽' in card:
        return card
    if isinstance(tariff, dict) and type(tariff.get('price')) is int:
        label = ' '.join(part for part in (str(tariff.get('service') or ''), str(tariff.get('plan') or '')) if part)
        return label + ' — ' + str(tariff['price']) + ' ₽.\n\n' + card
    priced = [row for row in (context or {}).get('tariffs') or [] if type(row.get('price')) is int]
    rows = [row for row in priced if row.get('service_id') == service_id]
    if not rows and len(str(service_id or '')) >= 4:
        key = re.sub(r'[^a-zа-яё0-9]+', '', str(service_id).casefold())
        rows = [row for row in priced
                if key in re.sub(r'[^a-zа-яё0-9]+', '', (str(row.get('service')) + str(row.get('service_id'))).casefold())]
    if not rows or len(rows) > 8:
        return card
    lines = ['• ' + str(row.get('plan') or row.get('service') or '') + ' — ' + str(row['price']) + ' ₽' for row in rows]
    return 'Цены ' + str(rows[0].get('service') or '') + ':\n' + '\n'.join(lines) + '\n\n' + card


_ANDROID_APPS = (('Claude', r'claude|клод|клауд', 'https://claude.ru.uptodown.com/android'),
                 ('ChatGPT', r'chat\s*gpt|gpt|гпт|чат\s*джипити', 'https://chatgpt.ru.uptodown.com/android'))


def _android_install(burst, context=None):
    """Owner 12.11.2026: an Android question gets only the Android link, politely; no iPhone part."""
    text = str(burst or '')
    if not re.search(r'(?i)андро[ий]д|android|самсунг|samsung|xiaomi|сяоми|редми|redmi|huawei|хуавей|honor|хонор|pixel|\bapk\b', text) \
            or re.search(r'(?i)айфон|iphone|ios\b|ipad|айпад|app\s*store|эп\s*стор', text):
        return ''
    state = str(((context or {}).get('dialog_state') or {}).get('service_id') or '')
    apps = [row for row in _ANDROID_APPS if re.search('(?i)' + row[1], text)]
    if not apps:
        apps = [row for row in _ANDROID_APPS if row[0].casefold() in state.casefold()] or list(_ANDROID_APPS)
    if len(apps) == 1:
        return 'Вы можете установить приложение ' + apps[0][0] + ' на Android по этой ссылке: ' + apps[0][2] + ' )'
    return ('Вы можете установить приложение на Android по этим ссылкам:\n'
            + '\n'.join('• ' + name + ': ' + url for name, _pattern, url in apps) + ' )')


CLARIFY = 'Уточните, пожалуйста, о каком сервисе идёт речь и что именно нужно сделать?'
_MONEY_COMPLAINT = re.compile(r'(?i)обман|мошенн|развод|деньги\s+(?:списа|ушли|снял)|списал\w*\s+деньги|верн\w+\s+деньги|возврат')


_OFFCAT_NO = re.compile(r'(?i)^\s*(?:нет|не надо|не нужно|спасибо|ок|понял|хорошо|ладно)\b')


def _offcat_asked(context):
    """The service name when our last message was the off-catalog question, else ''."""
    history = [row for row in (context or {}).get('history') or [] if isinstance(row, dict)]
    last = next((i for i in range(len(history) - 1, -1, -1) if history[i].get('role') in ('assistant', 'owner')), None)
    text = str(history[last].get('text') or '') if last is not None else ''
    if last is None or not (re.search(r'(?i)регион\w*[^.?!\n]{0,40}аккаунт|аккаунт\w*[^.?!\n]{0,40}регион', text)
                            and re.search(r'(?i)срок', text) and re.search(r'(?i)тариф', text)):
        return ''
    asked = next((str(history[i].get('text') or '') for i in range(last - 1, -1, -1)
                  if history[i].get('role') == 'client'), '')
    return ' '.join(asked.split())[:80] or 'сервис вне каталога'


_INFO_QUESTION = re.compile(r'(?i)сколько|цен[аыу]\b|стоит|стоимост|что такое|что входит|что умеет|чем\s|разниц|отлич|'
                            r'работает ли|можно ли|какие|как[ао][йя]\s+лучше|сравн|how much|what is|difference')


def _plans_summary(rows):
    """Price and the guide's description for each plan the client named."""
    try:
        from .service_guide import for_context
        services = for_context().get('services') or {}
    except Exception:
        services = {}
    parts = []
    for row in rows[:6]:
        name = (str(row.get('service') or '') + ' ' + str(row.get('plan') or '')).strip()
        flat = re.sub(r'[^a-zа-яё0-9]+', '', (str(row.get('service')) + str(row.get('service_id'))).casefold())
        guide = next((value for key, value in services.items() if key.replace('_', '') in flat), {})
        plans = guide.get('plans') if isinstance(guide, dict) else {}
        plan_key = _flat(row.get('plan') or '')
        text = next((str(desc) for label, desc in (plans or {}).items() if _flat(label) == plan_key), '')
        parts.append(name + ' — ' + str(row.get('price')) + ' ₽.' + ((' ' + text) if text else ''))
    return '\n\n'.join(parts)


def tariff_question(rows):
    labels = []
    for row in rows:
        label = (str(row.get('service') or '') + ' ' + str(row.get('plan') or '')).strip()
        if label and label not in labels:
            labels.append(label)
    if len(labels) == 2:
        body = labels[0] + ' или ' + labels[1]
    else:
        body = ', '.join(labels[:-1]) + ' или ' + labels[-1]
    return body + '?\n\nЭто разные варианты, напишите какой из них'


def drop_other_plans(reply, validated):
    chosen = validated.get('tariff')
    if not chosen or validated.get('intent') == 'list_options':
        return reply
    lines = str(reply or '').split('\n')
    kept = []
    for line in lines:
        dropped = False
        low = line.casefold()
        for row in validated.get('_others') or []:
            plan = str(row.get('plan') or '')
            if plan and plan.casefold() in low and str(row.get('price')) in line:
                dropped = True
        if not dropped:
            kept.append(line)
    text = '\n'.join(kept).strip()
    return text or str(reply or '').strip()


def ground_nominals(reply, context, validated):
    if validated.get('service_id') != 'appstore':
        return reply, False
    code = validated.get('region') or ''
    from .apple_matching import amounts, nominal, region_codes
    rows = [row for row in context.get('nominals') or []
            if code and code in region_codes(row.get('region') or '', stock=True)]
    if not code or not rows:
        return reply, False
    title = str(rows[0].get('region') or code).split('|')[0].strip() or code
    asked = amounts(context.get('burst') or '')
    if len(asked) == 1:
        amount, unit = next(iter(asked))
        if unit == '$' and code == 'US':
            unit = 'USD'
        matched = []
        for row in rows:
            ident = nominal(str(row.get('nominal') or ''))
            if ident and ident[0] == amount and (ident[1] == unit or (unit == 'USD' and ident[1] in ('USD', '$'))):
                matched.append(row)
        if not matched:
            lines = [f"{row['nominal']}: {row['price']} ₽" for row in rows]
            text = (f"Номинала {amount:g} {unit} в регионе {title} нет.\n\n"
                    'Сейчас есть:\n' + '\n'.join(lines) + '\n\nКакой оформить?')
            return text, True
        price = matched[0]['price']
        shown = '₽' if unit in ('RUB', 'руб', '₽') else unit
        # Always the code's own sentence: the model's wording around nominals contradicted itself.
        return (f"В регионе {title} есть {amount:g} {shown}, это {price} ₽.\n\n"
                'Оформить можно в боте: «Каталог» → «App Store / iCloud» → регион → номинал. '
                'Код придёт в чат бота сразу после оплаты.'), True
    prices = {row.get('price') for row in rows}
    stated = {int(item) for item in _RUBLE.findall(str(reply or ''))}
    if stated and stated <= prices:
        return reply, False
    lines = [f"{row['nominal']}: {row['price']} ₽" for row in rows]
    text = 'Для региона ' + title + ' сейчас такие номиналы:\n\n' + '\n'.join(lines) + '\n\nКакой оформить?'
    return text, True


def shape_advisories(reply, validated, context):
    already = set(context.get('advisories_already_said') or [])
    reask = set(validated.get('reask_advisories') or [])
    # Drop only a repeated advisory sentence. Line breaks and numbered steps stay as written.
    pieces = re.split(r'((?<=[.!?])\s+|\n+)', str(reply or ''))
    kept = []
    for index in range(0, len(pieces), 2):
        part = pieces[index]
        gap = pieces[index + 1] if index + 1 < len(pieces) else ''
        if any(advisory_id in already and advisory_id not in reask and advisory_present(advisory_id, part)
               for advisory_id in _ADVISORIES):
            continue
        if part.strip():
            kept.append(part + gap)
    shaped = re.sub(r'\n{3,}', '\n\n', ''.join(kept)).strip()
    if not shaped:
        shaped = str(reply or '').strip()
    want_vpn = bool(validated.get('vpn_advice')) and ('vpn_location' not in already or 'vpn_location' in reask)
    if want_vpn:
        shaped = _with_vpn(shaped, True)
    return shaped


def fact_stated(answer, reply):
    if _flat(answer) in _flat(reply):
        return True
    folded = _flat(reply)
    numbers = re.findall(r'\d+', answer or '')
    words = re.findall(r'[a-zа-яё]{5,}', _flat(answer))
    if numbers:
        if any(not re.search(r'(?<!\d)' + number + r'(?!\d)', folded) for number in numbers):
            return False
        return not words or any(word in folded for word in words)
    if not words:
        return False
    hits = sum(1 for word in words if word in folded)
    return hits >= max(2, (len(words) + 1) // 2)


def extra_numbers_ok(answer, reply, allowed_prices, context):
    fact_numbers = set(re.findall(r'\d+', answer or ''))
    price_numbers = {str(price) for price in allowed_prices or ()}
    plan_numbers = set()
    low = str(reply or '').casefold()
    for row in (context or {}).get('tariffs') or []:
        plan = str(row.get('plan') or '')
        if plan and plan.casefold() in low:
            plan_numbers.update(re.findall(r'\d+', plan))
    for number in re.findall(r'\d+', str(reply or '')):
        if number in fact_numbers or number in price_numbers or number in plan_numbers:
            continue
        if int(number) <= 3:
            continue
        return False
    return True


def addition_is_catalog(answer, reply, context):
    text = _RUBLE.sub(' ', str(reply or ''))
    for number in re.findall(r'\d+', answer or ''):
        text = re.sub(r'(?<!\d)' + number + r'(?!\d)', ' ', text)
    for row in (context or {}).get('tariffs') or []:
        for part in (row.get('service'), row.get('plan')):
            if part and len(str(part)) >= 3:
                text = re.sub(re.escape(str(part)), ' ', text, flags=re.I)
    fact_words = re.findall(r'[A-Za-zА-Яа-яЁё]{5,}', answer or '')
    for word in re.findall(r'[A-Za-zА-Яа-яЁё]{5,}', text):
        stem = word.casefold().replace('ё', 'е')[:5]
        if any(stem == fact.casefold().replace('ё', 'е')[:5] for fact in fact_words):
            continue
        return False
    return True


def fact_gap(answer_text, reply, beyond, allowed_prices, context):
    if not answer_text:
        return False
    if not fact_stated(answer_text, reply):
        return True
    if not extra_numbers_ok(answer_text, reply, allowed_prices, context):
        return True
    if beyond and not addition_is_catalog(answer_text, reply, context):
        return True
    return False


def remember_turn(db, chat, validated, context, reply=''):
    state = load(db, chat)
    advisories = [str(item) for item in (state.get('advisories') or []) if item]
    if validated.get('topic_changed'):
        state['collected'] = {}
        state['offered'] = []
        state['option_label'] = ''
    if validated.get('service_id') and state.get('service_key') and validated['service_id'] != state.get('service_key'):
        # A new service: the old tariff and the options shown for it no longer apply.
        state['option_label'] = ''
        state['offered'] = []
    if validated.get('_other_service') and not validated.get('service_id'):
        state['service_key'] = ''
        state['service_label'] = ''
        state['option_label'] = ''
        state['offered'] = []
    if validated.get('service_id'):
        state['service_key'] = validated['service_id']
        match = next((row for row in context.get('tariffs') or [] if row['service_id'] == validated['service_id']), None)
        state['service_label'] = match['service'] if match else state.get('service_label') or ''
    if validated.get('tariff'):
        state['option_label'] = validated['tariff']['plan']
        state['offered'] = [validated['tariff']]
    elif validated.get('service_id') and validated.get('intent') in ('price', 'list_options'):
        state['offered'] = [row for row in context.get('tariffs') or []
                            if row['service_id'] == validated['service_id']][:12]
    collected = dict(state.get('collected') or {})
    if validated.get('service_id') == 'appstore' and validated.get('region'):
        collected['region'] = validated['region']
    elif validated.get('service_id') and validated['service_id'] != 'appstore':
        collected.pop('region', None)
    state['collected'] = collected
    for key, value in (validated.get('setup') or {}).items():
        if value:
            collected[key] = value
    if validated.get('owner_topic') == 'new_account_email':
        collected['setup_topic'] = 1
    if validated.get('owner_topic') == 'course_bonus':
        collected['course_topic'] = 1
    for advisory_id in _ADVISORIES:
        if advisory_present(advisory_id, reply) and advisory_id not in advisories:
            advisories.append(advisory_id)
    state['advisories'] = advisories
    state['active'] = bool(state.get('service_key') or state.get('offered'))
    state['pending_slot'] = ''
    save(db, chat, state)


def package(understanding, context, db=None, chat=None):
    validated = bind_service(validate(understanding or {}, context), context)
    validated = attach_region(validated, understanding, context)
    if (asks_how_to_pay(context.get('burst')) and validated['owner_topic'] in ('none', 'activation_where')
            and len((understanding or {}).get('questions') or []) <= 1):
        # «Как оформить», «что нужно кроме оплаты» get the payment card, not a nearby topic.
        validated['payment_asked'] = True
        validated['owner_topic'] = 'none'
    _bind_credits(validated, context)
    reply = str((understanding or {}).get('reply') or '').strip()
    from .handoff import handoff_text
    from .payment_guide import (kind_for, merged_setup, missing_setup, mixes_activation_into_payment,
                                render, setup_ask, setup_ready)
    plan_name = (validated.get('tariff') or {}).get('plan') or _state_plan(validated, context)
    kind = kind_for(validated.get('service_id') or '', plan_name, context.get('tariffs') or [])
    setup = merged_setup(validated.get('setup'), (context.get('dialog_state') or {}).get('collected'))
    composed = False
    if kind == 'chatgpt_new_account' and setup_ready(setup):
        reply = handoff_text()
        validated['questions'] = [reply]
        composed = True
        verdict = {'send': False, 'class': 'collect', 'reason': 'Данные для нового аккаунта Plus собраны.'}
    options = [] if validated.get('payment_asked') or composed else matching_tariffs(context, validated.get('service_id'))
    if len(options) >= 2 and _asked_tariff_before(context):
        # The client answered our «X или Y?» — take the option with no extra words, do not ask again.
        picked = _closest_option(options, context.get('burst') or '')
        if picked:
            validated['tariff'] = picked
            validated['tariff_id'] = picked.get('id') or ''
            validated['payment_asked'] = True
            plan_name = picked.get('plan') or ''
            kind = kind_for(validated.get('service_id') or '', plan_name, context.get('tariffs') or [])
            options = []
    if not composed and len(options) >= 2 and reply and _INFO_QUESTION.search(str(context.get('burst') or '')) \
            and not _asked_tariff_before(context):
        # «Сколько стоит Pro и что такое Max?» — answer both plans; no «X или Y?».
        bounced = len(reply) < 160 and bool(re.search(r'(?i)уточните|какой из|\bили\b[^.!\n]{0,60}\?\s*$', reply))
        if (bounced or validated['confidence'] == 'low') and len(options) <= 3:
            reply = _plans_summary(options)
            validated['questions'] = [reply]
            composed = True
            verdict = classify_draft(reply, validated['allowed_prices'])
        options = []
    if not composed and len(options) >= 2:
        reply = tariff_question(options)
        validated['tariff_id'] = ''
        validated['tariff'] = None
        validated['intent'] = 'choose_tariff'
        validated['questions'] = [reply]
        composed = True
        verdict = classify_draft(reply, validated['allowed_prices'])
    elif not composed and (validated.get('payment_asked') or mixes_activation_into_payment(reply)
            or (kind == 'chatgpt_new_account' and setup and missing_setup(setup))):
        service_id = validated.get('service_id') or validated.get('_other_service') or ''
        tariff = validated.get('tariff') or {}
        service_name = tariff.get('service') or ''
        if not service_name and service_id and service_id == validated.get('_other_service'):
            service_name = _SERVICE_TITLES.get(service_id, service_id.capitalize())
        if not service_name:
            match = next((row for row in context.get('tariffs') or [] if row.get('service_id') == service_id), None)
            service_name = match.get('service') if match else ''
        if kind == 'chatgpt_new_account' and setup and not validated.get('payment_asked') and not mixes_activation_into_payment(reply):
            reply = setup_ask(missing_setup(setup))
        else:
            reply = render(service_id, service_name, plan_name or (tariff or {}).get('plan') or '',
                           {'bot_navigation': context.get('bot_navigation') or {}},
                           context.get('advisories_already_said') or [],
                           context.get('tariffs') or [], validated.get('fulfillment_choice') or '', setup)
        reply = _with_price_line(reply, context, service_id, tariff, kind)
        if 'vpn_location' not in (validated.get('reask_advisories') or []):
            validated['vpn_advice'] = False
        validated['questions'] = [reply]
        composed = True
        verdict = classify_draft(reply, validated['allowed_prices'])
    elif not composed and validated['confidence'] == 'low':
        options = clarify_options(understanding, context)
        validated['service_id'] = ''
        validated['tariff_id'] = ''
        validated['tariff'] = None
        validated['region'] = ''
        validated['allowed_prices'] = _allowed_prices(
            context.get('tariffs') or [], context.get('nominals') or [], '', '', _context_prices(context))
        if len(options) < 2:
            # Owner 12.11.2026 (real questions): an unclear message gets one clarifying
            # question, not a stop. A complaint about money, or a second unclear message
            # in a row, still goes to Alexander.
            burst_low = str(context.get('burst') or '')
            last_said = next((str(row.get('text') or '') for row in reversed(context.get('history') or [])
                              if isinstance(row, dict) and row.get('role') in ('assistant', 'owner')), '')
            own_question = ' '.join(str(reply or '').split())
            if _MONEY_COMPLAINT.search(burst_low):
                reply = handoff_text()
                verdict = {'send': False, 'class': 'unverified',
                           'reason': 'Клиент жалуется на оплату или списание. Нужен Александр.'}
            elif CLARIFY in last_said or last_said.rstrip().endswith('?') and 'Уточните' in last_said:
                reply = handoff_text()
                verdict = {'send': False, 'class': 'unverified',
                           'reason': 'Сообщение непонятно второй раз подряд. Нужен Александр.'}
            else:
                # Low confidence used to throw the whole draft away. It only means «do not
                # name a tariff or a price»: an answer without prices that passes the
                # safety check goes out; anything else becomes one clarifying question.
                if not (len(own_question) >= 20 and '₽' not in own_question and '$' not in own_question
                        and own_question != ' '.join(handoff_text().split())
                        and classify_draft(str(reply or ''), validated['allowed_prices'])['send']):
                    reply = CLARIFY
                validated['questions'] = [reply]
                verdict = classify_draft(reply, validated['allowed_prices'])
        else:
            reply = _clarify_reply(reply, options)
            verdict = classify_draft(reply, validated['allowed_prices'])
    elif not composed:
        grounded, composed = ground_nominals(reply, context, validated)
        reply = grounded
        if not composed and validated.get('tariff'):
            others = [row for row in context.get('tariffs') or []
                      if row.get('service_id') == validated['tariff'].get('service_id')
                      and row.get('id') != validated['tariff'].get('id')]
            validated['_others'] = others
            reply = drop_other_plans(reply, validated)
        if not reply:
            reply = handoff_text()
            verdict = {'send': False, 'class': 'unverified', 'reason': 'Модель не дала текст ответа.'}
        else:
            verdict = classify_draft(reply, validated['allowed_prices'])
    if verdict['send']:
        reply = shape_advisories(reply, validated, context)
        if len(reply) > 3500:
            verdict = {'send': False, 'class': 'unverified', 'reason': 'Ответ с правилом VPN длиннее 3500 символов.'}
    # Owner 12.11.2026: a detailed answer without a separate paragraph per question is not a reason
    # to stop; only a one-line reply to several questions still is.
    if verdict['send'] and not composed and len(reply) < 160 \
            and not questions_addressed(reply, validated['questions'], context):
        verdict = {'send': False, 'class': 'unverified',
                   'reason': 'В ответе нет отдельного пункта на каждый вопрос из разбора.'}
    if db is not None and chat is not None:
        remember_turn(db, chat, validated, context, reply if verdict['send'] else '')
    summary = str((understanding or {}).get('summary') or verdict['reason'])[:700]
    return {'reply': reply or handoff_text(),
            'summary': summary,
            'handoff': not verdict['send'],
            'source_ids': [],
            'evidence': {'status': 'catalog_direct' if verdict['send'] else 'owner_policy',
                         'sources': [],
                         'composed_by_code': bool(composed),
                         'reply_class': verdict['class'],
                         'turn_understanding': {'intent': validated['intent'],
                                                'service_id': validated['service_id'],
                                                'tariff_id': validated['tariff_id'],
                                                'confidence': validated['confidence'],
                                                'topic_changed': validated['topic_changed'],
                                                'known_facts': validated['known_facts'],
                                                'missing_facts': validated['missing_facts'],
                         'questions': validated['questions'],
                         'vpn_advice': validated['vpn_advice'],
                         'region': validated.get('region') or '',
                         'conflict': validated['conflict'],
                         'owner_alignment': validated['owner_alignment'],
                         'owner_topic': validated['owner_topic']},
                         'diagnostic': verdict['reason'],
                         'handoff_reason': '' if verdict['send'] else verdict['reason']}}


def enforce_turn_policy(answer, understanding, context, freshness=None):
    """Hold a draft the model marked as conflict, a clash with the owner, or a stale price."""
    if not isinstance(answer, dict):
        return answer
    validated = validate(understanding or {}, context or {})
    if isinstance(answer.get('reply'), str) and validated.get('allowed_prices'):
        # 2489 ₽ instead of 2490 ₽ is a slip, not a reason to stop the answer.
        answer = dict(answer, reply=snap_prices(answer['reply'], validated['allowed_prices'])[0])
    holds = []
    if validated['conflict'] == 'high':
        holds.append('Модель отметила высокий конфликт. Клиенту оставлена фраза ожидания.')
    if validated['owner_alignment'] not in ('follows', 'repeats'):
        holds.append('Черновик расходится с репликой владельца в этом чате.')
    if (freshness or {}).get('status') == 'refresh_failed' and states_price(answer.get('reply')):
        holds.append('Каталог старше порога, повторное чтение не удалось. Цену клиенту не отправляю.')
    from .payment_guide import kind_for, merged_setup, missing_setup, setup_ready
    plan_name = (validated.get('tariff') or {}).get('plan') or _state_plan(validated, context or {})
    account = merged_setup(validated.get('setup'), ((context or {}).get('dialog_state') or {}).get('collected'))
    topic = validated['owner_topic']
    offcat_asked = _offcat_asked(context)
    offcat_burst = str((context or {}).get('burst') or '').strip()
    if offcat_asked and '?' not in offcat_burst and 0 < len(offcat_burst) <= 160 and not _OFFCAT_NO.search(offcat_burst):
        # «Премиум, Турция, на 3 месяца» after our question is the order, not a new catalog request.
        topic = validated['owner_topic'] = 'off_catalog'
        validated['refund_collected'] = {'service': offcat_asked, 'reason': offcat_burst[:200]}
        from .handoff import handoff_text as _wait
        answer = dict(answer, reply=_wait(), handoff=False)
    elif topic == 'off_catalog' and not offcat_asked:
        # First mention: always ask tariff, region and term before calling the owner.
        got_first = dict(validated.get('refund_collected') or {})
        got_first['reason'] = ''
        validated['refund_collected'] = got_first
    collected_state = ((context or {}).get('dialog_state') or {}).get('collected') or {}
    setup_dialog = (kind_for(validated.get('service_id') or '', plan_name, (context or {}).get('tariffs') or [])
                    == 'chatgpt_new_account' or topic == 'new_account_email' or bool(collected_state.get('setup_topic')))
    # Ready only on the turn that brought new data, so later «спасибо» does not alert again.
    account_ready = setup_dialog and setup_ready(account) and bool(validated.get('setup'))
    if account_ready:
        holds.append('Данные для новой почты outlook.com собраны. Клиенту оставлена фраза ожидания.')
    burst_text = str((context or {}).get('burst') or '')
    course_email = (_email_in(burst_text) or ('[email скрыт]' if '[email скрыт]' in burst_text else '')) if (
        topic == 'course_bonus' or collected_state.get('course_topic')) else ''
    if course_email:
        # Owner 11.11.2026: tell the client Alexander will check it; notify the owner. No hold.
        answer = dict(answer, reply=COURSE_RECEIVED, handoff=False)
    if (topic == 'activation_where' and asks_how_to_pay((context or {}).get('burst'))
            and len((understanding or {}).get('questions') or []) <= 1):
        topic = 'none'  # the payment card already answers; see package()
    faq_rows = (context or {}).get('faq') or []
    from .faq import collect_fields, usable_answer
    answer_text = usable_answer(topic, faq_rows) if topic != 'none' else ''
    collecting = collect_fields(topic, faq_rows) if topic != 'none' else []
    refund_ready = False
    owner_text_used = bool(course_email)
    owner_followup = False
    beyond = bool(validated.get('beyond_owner_fact'))
    if topic != 'none' and collecting and set(collecting) & {'setup_email', 'setup_password', 'setup_recovery', 'course_email'}:
        reply = str(answer.get('reply') or '')
        missing = missing_setup(account) if 'setup_email' in collecting else []
        if account_ready or course_email:
            pass
        elif account and missing:
            answer = dict(answer, reply=_setup_missing_text(missing), handoff=False)
            owner_text_used = True
        elif (answer.get('handoff') or asserts_outcome(reply) or _PROMISE.search(reply)
              or fact_gap(answer_text, reply, bool(validated.get('beyond_owner_fact')),
                          validated.get('allowed_prices') or _allowed_prices(
                              (context or {}).get('tariffs') or [], (context or {}).get('nominals') or [], '', '', _context_prices(context)),
                          context)):
            if answer_text and classify_draft(answer_text, validated.get('allowed_prices'))['send']:
                answer = dict(answer, reply=answer_text, handoff=False)
                owner_text_used = True
            else:
                holds.append('Для этой темы нет записанного ответа владельца. Клиенту оставлена фраза ожидания.')
    elif topic != 'none' and collecting:
        needed = [key for key in ('order_number', 'service', 'reason') if key in collecting]
        got = validated.get('refund_collected') or {}
        missing = [key for key in needed if not got.get(key)]
        reply = str(answer.get('reply') or '')
        if not missing:
            refund_ready = True
            holds.append('Данные для возврата собраны. Клиенту оставлена фраза ожидания.' if topic == 'refund_terms'
                         else 'Заказ вне каталога: сервис и детали получены. Нужен Александр. Клиенту оставлена фраза ожидания.'
                         if topic == 'off_catalog'
                         else 'Номер заказа по теме «' + topic + '» получен. Нужен Александр. Клиенту оставлена фраза ожидания.')
        elif _verbatim(topic) and answer_text and classify_draft(answer_text, validated.get('allowed_prices'))['send']:
            answer = dict(answer, reply=answer_text, handoff=False)
            owner_text_used = True
        elif asserts_outcome(reply) or _PROMISE.search(reply) or not re.search(r'(?i)пришлите|напишите|укажите|\?', reply):
            if answer_text and classify_draft(answer_text, validated.get('allowed_prices'))['send']:
                # The owner's own request for the missing details replaces a draft that promised something.
                answer = dict(answer, reply=answer_text, handoff=False)
                owner_text_used = True
            else:
                holds.append('По возврату нельзя обещать результат, пока собраны не все сведения.')
        elif answer.get('handoff') and answer_text and classify_draft(answer_text, validated.get('allowed_prices'))['send']:
            answer = dict(answer, reply=answer_text, handoff=False)
            owner_text_used = True
    elif topic != 'none' and not answer_text:
        holds.append('Для этой темы нет записанного ответа владельца. Клиенту оставлена фраза ожидания.')
    elif answer_text:
        reply = str(answer.get('reply') or '')
        prices = validated.get('allowed_prices')
        if prices is None:
            prices = _allowed_prices(context.get('tariffs') or [], context.get('nominals') or [], '', '',
                                     _context_prices(context))
        platform_text = _android_install(burst_text, context) if topic == 'app_download' else ''
        if platform_text:
            answer_text = platform_text
        if platform_text or fact_gap(answer_text, reply, beyond, prices, context) or answer.get('handoff') or _verbatim(topic):
            # A draft that drifted from the owner's fact, or was held for its wording, is replaced
            # by the owner's own text. Conflict, owner clash and stale price still hold below.
            # When the model itself marks a condition beyond the fact, the question needs the owner.
            # Owner 12.11.2026: a question wider than the rule still gets the rule;
            # the rest goes to Alexander without stopping the reply.
            if classify_draft(answer_text, prices)['send']:
                reply_text = answer_text.rstrip() + ('\n\n' + OWNER_FOLLOWUP if beyond and OWNER_FOLLOWUP not in answer_text else '')
                answer = dict(answer, reply=reply_text, handoff=False)
                owner_text_used = True
                owner_followup = beyond
            else:
                holds.append('Ответ выходит за записанный факт владельца.')
    asked_tariff = validated.get('tariff') or {}
    if not asked_tariff and _ASKS_PRICE.search(burst_text):
        named = matching_tariffs(context or {}, validated.get('service_id') or validated.get('_other_service') or '')
        if not named:
            for sid in {row.get('service_id') for row in (context or {}).get('tariffs') or []}:
                found = matching_tariffs(context or {}, sid)
                if len(found) == 1 and _compact_mentions(burst_text, str(found[0].get('service') or '')):
                    named = found
                    break
        asked_tariff = named[0] if len(named) == 1 else {}
        if not asked_tariff and named and _plan_tokens(burst_text):
            plain = [row for row in named if _plan_tokens(row.get('plan') or '') <= _plan_tokens(burst_text)]
            asked_tariff = plain[0] if len(plain) == 1 else {}
    if (not holds and not answer.get('handoff') and type(asked_tariff.get('price')) is int
            and _ASKS_PRICE.search(burst_text) and '₽' not in str(answer.get('reply') or '')
            and not composed_card(answer)):
        # «How much is Plus and how can I pay from abroad?» — the owner's rule answers the second half only.
        label = ' '.join(part for part in (str(asked_tariff.get('service') or ''), str(asked_tariff.get('plan') or '')) if part)
        answer = dict(answer, reply=label + ' — ' + str(asked_tariff['price']) + ' ₽.\n\n' + str(answer.get('reply') or ''))
    if _repeats_assistant(answer.get('reply'), context):
        holds.append('Черновик повторяет уже сказанное в этом чате.')
    evidence = dict(answer.get('evidence') or {})
    turn = dict(evidence.get('turn_understanding') or {})
    turn.update(conflict=validated['conflict'], owner_alignment=validated['owner_alignment'],
                owner_topic=validated['owner_topic'])
    evidence['turn_understanding'] = turn
    evidence['catalog_freshness'] = dict(freshness or {})
    if course_email:
        evidence.update(course_email_hold=True, course_email=course_email)
    if owner_followup and not holds:
        evidence.update(owner_followup=True, owner_followup_topic=topic)
    if composed_card(answer):
        evidence['owner_approved_wording'] = True
    if owner_text_used and not holds:
        evidence.update(status='catalog_direct', reply_class='instruct', owner_text_used=True, owner_approved_wording=True,
                        diagnostic='Черновик модели заменён записанным ответом владельца по теме «' + topic + '».',
                        handoff_reason='')
    if holds:
        evidence.update(status='owner_policy', conflict_hold=validated['conflict'] == 'high',
                        owner_alignment_hold=validated['owner_alignment'] not in ('follows', 'repeats'),
                        stale_price_hold=(freshness or {}).get('status') == 'refresh_failed' and states_price(answer.get('reply')),
                        owner_fact_hold=bool(topic != 'none' and not collecting and (
                            (not answer_text) or fact_gap(
                                answer_text, str(answer.get('reply') or ''), beyond,
                                validated.get('allowed_prices') or _allowed_prices(
                                    (context or {}).get('tariffs') or [], (context or {}).get('nominals') or [], '', '', _context_prices(context)),
                                context))),
                        refund_hold=refund_ready,
                        collect_topic=topic if collecting else '',
                        refund_collected=validated.get('refund_collected') or {},
                        account_setup_hold=account_ready,
                        account_setup=account if account_ready else {},
                        account_service=(validated.get('tariff') or {}).get('service') or validated.get('service_id')
                                        or ((context or {}).get('dialog_state') or {}).get('service_label') or '',

                        repeat_hold=_repeats_assistant(answer.get('reply'), context),
                        diagnostic=' '.join(holds), handoff_reason=' '.join(holds))
        if account_ready:
            from .handoff import handoff_text
            answer = dict(answer, reply=handoff_text(), handoff=True, evidence=evidence)
        else:
            answer = dict(answer, handoff=True, evidence=evidence)
        return answer
    answer = dict(answer, evidence=evidence)
    return answer
