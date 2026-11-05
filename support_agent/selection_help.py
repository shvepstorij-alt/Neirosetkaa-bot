"""A bounded clarification for plan choice, independent of service/price names.

The caller passes the current request enriched with relevant client context,
not catalogue descriptions or an unscoped whole history. A known task is handed
back to the normal consultation/research pipeline; this module never recommends
a plan, claims a feature, or invents a purchase/activation route.
"""
import re


_SUPPORT_OBJECT = re.compile(r'тариф|план|подписк|пакет|сервис|нейросет|инструмент', re.I)
_UI_PROBLEM = re.compile(r'ошибк|не\s+работает\s+кнопк|не\s+получается\s+(?:нажать|выбрать|оплатить)|'
                         r'не\s+могу\s+(?:нажать|оплатить)|не\s+открывается', re.I)
_TASKS = re.compile(
    r'(?<!\w)(?:код(?:а|ом|е)?|программирован[а-яё]*|разработк[а-яё]*|скрипт[а-яё]*|'
    r'документ[а-яё]*|pdf|docx|xlsx|таблиц[а-яё]*|текст[а-яё]*|стат[ьья][а-яё]*|'
    r'пис(?:ать|ем|ьма)|перевод[а-яё]*|исследован[а-яё]*|анализ[а-яё]*|'
    r'уч[её]б[а-яё]*|обучени[а-яё]*|студент[а-яё]*|курсов[а-яё]*|диплом[а-яё]*|'
    r'видео[а-яё]*|изображени[а-яё]*|картин[а-яё]*|фото[а-яё]*|дизайн[а-яё]*|'
    r'анимац[а-яё]*|монтаж[а-яё]*|музык[а-яё]*|пес(?:ни|ен|ню)|озвуч[а-яё]*|голос[а-яё]*|'
    r'встреч[а-яё]*|созвон[а-яё]*|конференц[а-яё]*|вебинар[а-яё]*|звонк[а-яё]*|'
    r'консультант[а-яё]*|консультир[а-яё]*|проект[а-яё]*|управлени[а-яё]*|планировани[а-яё]*|автоматизац[а-яё]*|интеграц[а-яё]*|'
    r'api|sso|scim|saml|webhook|экспорт[а-яё]*|офлайн[а-яё]*|offline|'
    r'хранилищ[а-яё]*|запис[ьи][а-яё]*|субтитр[а-яё]*|презентац[а-яё]*)(?!\w)', re.I)

# A task is an intended activity, not the brand of the proposed tool. Keep the
# same recognition available to the downstream finite research projection.
_DEVELOPMENT_TASK = re.compile(
    r'(?<!\w)(?:сайт[а-яё]*|веб[ -]?(?:сайт|приложени)[а-яё]*|'
    r'приложени[а-яё]*|(?:telegram|телеграм)[ -]?бот[а-яё]*|бот(?:ов|ы|а)?|'
    r'чат[ -]?бот[а-яё]*|лендинг[а-яё]*|фронтенд[а-яё]*|бэкенд[а-яё]*|'
    r'frontend|backend|debugging|refactoring)(?!\w)', re.I)


def development_purpose(text):
    value=str(text or '')
    action=r'созда[а-яё]*|дела[а-яё]*|пиш[а-яё]*|писать|напис[а-яё]*|собира[а-яё]*|собрать|разраб[а-яё]*|исправ[а-яё]*|отлад[а-яё]*|дораб[а-яё]*|поддерж[а-яё]*|build|develop|debug|refactor'
    for match in _DEVELOPMENT_TASK.finditer(value):
        prefix=re.split(r'[.!?;\n,]',value[:match.start()])[-1][-70:]
        suffix=re.split(r'[.!?;\n,]',value[match.end():])[0][:60]
        # Using a product IN an app/ON a website is not creating that app/site.
        if re.search(r'(?:^|\s)(?:в|во|на|через|из|внутри)\s+(?:(?:этом|этом\s+же|мобильном|своём|своем)\s+)?$',prefix,re.I):continue
        if re.search(r'(?<!\w)(?:'+action+r')(?!\w)',prefix,re.I):return True
        if re.match(r'\s*(?:,?\s*(?:я|мы)\s+)?(?:'+action+r')(?!\w)',suffix,re.I):return True
    return False


def is_selection_request(text, facts=None):
    """Explicit plan/product advice, not every price or checkout question."""
    text = str(text or '')
    if re.search(r'закуп|поставщик|себестоим|18\s*\+|порно|эротическ', text, re.I):
        return False
    if _UI_PROBLEM.search(text):return False
    from .consultation_intent import context
    if context(text, facts).get('active'):return True
    # Selecting an entry in the UI is not asking which plan fits one's tasks.
    navigation = re.search(r'(?:где|как)[^.!?\n]{0,45}выбрать[^.!?\n]{0,45}(?:в\s+боте|в\s+каталоге|кнопк)', text, re.I)
    advice = re.search(r'посовет|порекоменду|подойд[её]т|лучше|определиться|подобрать', text, re.I)
    if navigation and not advice:return False
    # Freshly annotated rows allow "Krea: что посоветуете?" for every catalogue
    # brand without a second brand registry. Do not pass stale whole-chat rows.
    object_present = bool(_SUPPORT_OBJECT.search(text) or (facts or {}).get('requested_catalog'))
    if not object_present:
        from .official_services import registry
        from .catalog_context import ALIASES
        object_present=any(re.search(r'(?<!\w)'+re.escape(alias)+r'(?!\w)',text,re.I)
                           for product in registry() for alias in ALIASES.get(product,(product,)))
    strong = re.search(r'помог(?:и|ите)[^.!?\n]{0,65}(?:выбрать|подобрать|определиться|с\s+выбором)|'
                       r'(?:не\s+могу|сложно|трудно)[^.!?\n]{0,35}определиться|'
                       r'(?:хочу|нужно)[^.!?\n]{0,25}подобрать', text, re.I)
    question = re.search(r'(?:какой|какую|какие|что)[^.!?\n]{0,80}'
                         r'(?:выбрать|лучше|подойд[её]т|посовету|порекоменду|предпочесть)', text, re.I)
    imperative = re.search(r'(?:подбери(?:те)?|посовету(?:й|йте)|порекомендуй(?:те)?)[^.!?\n]{0,60}'
                           r'(?:тариф|план|подписк|пакет|сервис|нейросет)', text, re.I)
    bare_followup = re.fullmatch(r'\s*(?:(?:а|ну|тогда)\s+)?(?:помог(?:и|ите)\s+(?:выбрать|определиться)|'
                                r'что\s+(?:мне\s+)?(?:выбрать|посоветуете)|'
                                r'какой\s+(?:мне\s+)?(?:лучше|подойд[её]т))\s*[!?.)]*\s*', text, re.I)
    return bool(imperative or object_present and (strong or question) or bare_followup)


def has_selection_purpose(text):
    """Recognize concrete tasks/requirements, not just 'для работы/для себя'."""
    text = str(text or '')
    # A request to be asked about tasks is not a supplied task in itself.
    text = re.sub(r'(?:для\s+каких|какие|под\s+какие)\s+задач[а-яё]*', '', text, flags=re.I)
    text = re.sub(r'не\s+знаю[^.!?\n]{0,25}задач[а-яё]*', '', text, flags=re.I)
    if _TASKS.search(text) or development_purpose(text):return True
    if re.search(r'отвечать\s+(?:на\s+вопросы\s+)?клиент[а-яё]*|'
                 r'(?:повседневн[а-яё]*|рабоч[а-яё]*)[^.!?\n]{0,25}вопрос[а-яё]*', text, re.I):return True
    if re.search(r'\d{1,5}\s*(?:участник[а-яё]*|сотрудник[а-яё]*|пользовател[а-яё]*|человек|мест|seat[s]?)', text, re.I):
        return True
    # An explicit intended action can be meaningful even for a service whose
    # task vocabulary is not in the examples above. No feature is inferred.
    if re.search(r'(?:моя\s+задача|у\s+меня\s+задача|мне\s+нужно|планирую|хочу)\s+'
                 r'(?:созда[а-яё]*|делать|вести|собирать|обрабатывать|проверять|настраивать)\s+'
                 r'(?!тариф|подписк|выбор|покупк|оплат)[а-яёa-z0-9-]{3,}',text,re.I):return True
    return bool(re.search(r'чтобы\s+(?:я\s+)?(?:создавать|делать|писать|вести|хранить|управлять|'
                          r'готовить|собирать|отслеживать|организовать|организовывать|проводить|'
                          r'запускать|слушать|смотреть|настраивать)\b', text, re.I))


def selection_reply(facts, text):
    """Return one friendly question only while the purpose is still missing."""
    from .consultation_intent import context
    if context(text, facts).get('active'):return None
    if not is_selection_request(text, facts) or has_selection_purpose(text):return None
    rows = facts.get('requested_catalog', [])
    count = len({str(row.get('service_key') or row.get('service')) for row in rows if isinstance(row, dict)})
    prompt='Конечно, помогу подобрать) Для каких задач планируете пользоваться сервисом? Можно коротко описать, что хотите делать.'
    # Asking a useful clarification must not discard an independent price
    # question. Only render exact selected store rows; no model conversion.
    if re.search(r'сколько[^.!?\n]{0,40}сто(?:ит|ят)|цен[аыуе]|стоимост',str(text),re.I):
        from .catalog_order import ordered_rows
        prices=[]
        for row in ordered_rows(rows):
            price=row.get('catalog_price_rub')
            if type(price) is int and price>0 and row.get('service') and row.get('plan'):
                line=f'{row["service"]} {row["plan"]}: {price} ₽'
                if line not in prices:prices.append(line)
        if prices:
            price_text='\n'.join(prices[:8])
            if len(prices)>8:price_text+='\nЭто часть тарифов: ещё '+str(len(prices)-8)+'. После уточнения задачи помогу сузить выбор.'
            prompt=price_text+'\n\n'+prompt
    return {
        'reply': prompt,
        'summary': 'Клиент просит помочь с выбором тарифа, но конкретные задачи или требования ещё не указаны. Задан один вопрос о цели; тариф, функции и способ оформления не предложены.',
        'handoff': False,
        'source_ids': [],
        'evidence': {'status': 'clarification', 'sources': [],
                     'diagnostic': 'Этап: уточнение цели выбора. Конкретная задача не обнаружена в текущем запросе с относящимся к нему контекстом; сопоставлено сервисов='+str(count)+'. Цены и оформление не обещаны.'}}
