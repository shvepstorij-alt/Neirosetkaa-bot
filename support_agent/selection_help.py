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
    r'проект[а-яё]*|управлени[а-яё]*|планировани[а-яё]*|автоматизац[а-яё]*|интеграц[а-яё]*|'
    r'api|sso|scim|saml|webhook|экспорт[а-яё]*|офлайн[а-яё]*|offline|'
    r'хранилищ[а-яё]*|запис[ьи][а-яё]*|субтитр[а-яё]*|презентац[а-яё]*)(?!\w)', re.I)


def is_selection_request(text, facts=None):
    """Explicit plan/product advice, not every price or checkout question."""
    text = str(text or '')
    if re.search(r'закуп|поставщик|себестоим|18\s*\+|порно|эротическ', text, re.I):
        return False
    if _UI_PROBLEM.search(text):return False
    # Selecting an entry in the UI is not asking which plan fits one's tasks.
    navigation = re.search(r'(?:где|как)[^.!?\n]{0,45}выбрать[^.!?\n]{0,45}(?:в\s+боте|в\s+каталоге|кнопк)', text, re.I)
    advice = re.search(r'посовет|порекоменду|подойд[её]т|лучше|определиться|подобрать', text, re.I)
    if navigation and not advice:return False
    # Freshly annotated rows allow "Krea: что посоветуете?" for every catalogue
    # brand without a second brand registry. Do not pass stale whole-chat rows.
    object_present = bool(_SUPPORT_OBJECT.search(text) or (facts or {}).get('requested_catalog'))
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
    if _TASKS.search(text):return True
    if re.search(r'\d{1,5}\s*(?:участник[а-яё]*|сотрудник[а-яё]*|пользовател[а-яё]*|человек|мест|seat[s]?)', text, re.I):
        return True
    # An explicit intended action can be meaningful even for a service whose
    # task vocabulary is not in the examples above. No feature is inferred.
    return bool(re.search(r'чтобы\s+(?:я\s+)?(?:создавать|делать|писать|вести|хранить|управлять|'
                          r'готовить|собирать|отслеживать|организовать|организовывать|проводить|'
                          r'запускать|слушать|смотреть|настраивать)\b', text, re.I))


def selection_reply(facts, text):
    """Return one friendly question only while the purpose is still missing."""
    if not is_selection_request(text, facts) or has_selection_purpose(text):return None
    rows = facts.get('requested_catalog', [])
    count = len({str(row.get('service_key') or row.get('service')) for row in rows if isinstance(row, dict)})
    return {
        'reply': 'Конечно, помогу подобрать) Для каких задач планируете пользоваться сервисом? Можно коротко описать, что хотите делать.',
        'summary': 'Клиент просит помочь с выбором тарифа, но конкретные задачи или требования ещё не указаны. Задан один вопрос о цели; тариф, функции и способ оформления не предложены.',
        'handoff': False,
        'source_ids': [],
        'evidence': {'status': 'clarification', 'sources': [],
                     'diagnostic': 'Этап: уточнение цели выбора. Конкретная задача не обнаружена в текущем запросе с относящимся к нему контекстом; сопоставлено сервисов='+str(count)+'. Цены и оформление не обещаны.'}}
