"""Task intent facts, not manufacturer capability or plan recommendations.

Only inspect the current context-resolved request. Never scan arbitrary old
turns or infer that a named product can meet the requested task.
"""
import re

_VISUAL_ANALYSIS = re.compile(
    r'(?:расшифр|распозна|объясн|определ|анализ|понять)[^.!?\n]{0,95}'
    r'(?:знач[ое]к|значки|панел|прибор|что[^.!?\n]{0,20}(?:фото|изображени))|'
    r'(?:фото|снимок)[^.!?\n]{0,90}(?:приборн|что[^.!?\n]{0,15}горит|распозна|расшифр)|'
    r'(?:приборн[а-яё]*\s+панел|знач[ое]к[^.!?\n]{0,25}горит)', re.I)
_MEDIA = re.compile(r'фото|изображени|картинк|видео|ролик|анимац', re.I)
_CREATE = re.compile(r'генерац|генерир|созда[вт]|сделать|делать|ожив|анимир', re.I)
_DISCOVERY = re.compile(r'сервис|нейросет|инструмент|помог[^.!?\n]{0,30}выбрать|'
                        r'подскаж|посовет|подойд[её]т|порекоменду', re.I)


def context(text, facts=None):
    text = str(text or '')
    if re.search(r'закуп|поставщик|себестоим|18\s*\+|порно|эротическ', text, re.I):
        return {'active': False}
    if _VISUAL_ANALYSIS.search(text):
        return {
            'active': True, 'kind': 'visual_understanding',
            'task_text': text[-1800:],
            'instructions': [
                'Клиенту нужно понимание содержимого изображения, а не генерация картинки. Не подменять задачу Midjourney, Gamma или другим генератором без проверенного подтверждения нужной функции.',
                'Учитывать уже названные задачи и выбранный сервис. Проверить актуальную возможность загрузки и анализа фото и лимиты подходящего тарифа по официальным сведениям; рублёвые цены брать только из магазина.',
                'По автомобильному индикатору не обещать точную диагностику неисправности или безопасность движения. Можно объяснить символ по фото и рекомендовать свериться с руководством автомобиля; при необходимости уточнить марку/модель.'
            ]}
    if not (_MEDIA.search(text) and _CREATE.search(text)):
        return {'active': False}
    # Explicit creative production is meaningful even when no product is named.
    # This includes the continuation "нужно делать короткие 15-30 секундные видео".
    durations = re.findall(r'\b\d{1,3}(?:\s*[-–—]\s*\d{1,3})?\s*(?:секунд[а-яё]*|сек\.?|минут[а-яё]*)', text, re.I)
    from .bot_guide import navigation_facts
    nav = navigation_facts(facts or {})
    return {
        'active': True, 'kind': 'creative_generation', 'task_text': text[-1800:],
        'durations_requested': durations,
        'discovery_requested': bool(_DISCOVERY.search(text)),
        'shop_generation_navigation': {
            'identity_status': nav['identity_status'], 'username': nav['username'],
            'main_menu': nav['generation_menu'],
            'miniapp_steps': nav['miniapp_generation_steps']},
        'instructions': [
            'Клиент просит подобрать способ создания фото/видео. Не требовать назвать сервис: выбор сервиса является частью вопроса. Не отвечать одним списком цен предыдущего сервиса.',
            'Сначала предложить генерацию внутри бота магазина через подтверждённые разделы. Если нужна отдельная подписка, сравнить несколько подходящих сервисов по официальным актуальным возможностям; цены и доступные планы брать из магазина.',
            'Учитывать уже указанную длительность, задачу и формат. Не спрашивать это повторно. Если данных мало, дать полезную отправную точку и задать один конкретный оставшийся вопрос, например о генерации с нуля или оживлении готового фото.',
            'Для 15-30 секунд не обещать один непрерывный клип или конкретный лимит модели без проверки. Отличать длительность одного поколения от итогового ролика, который может потребовать нескольких сцен и монтажа.',
            'Не выдумывать модели, лимиты, качество, безлимитность, сроки и стоимость генераций. Текущие модели, параметры и стоимость генерации клиент проверяет в интерфейсе до запуска.'
        ]}


def fallback(facts, reason):
    """Useful verified navigation if public recommendations could not be checked.

    Never turn an evidence failure into a price dump for the previous topic.
    Remaining requested services are explicitly unresolved, not silently omitted.
    """
    intent=facts.get('consultation_intent',{})
    if intent.get('kind')!='creative_generation':return None
    nav=intent.get('shop_generation_navigation',{})
    if nav.get('identity_status')!='available' or not nav.get('username'):return None
    body=('Начать можно с генераций в @'+nav['username']+')\n\n'
          'В главном меню есть «Изображение», «Видео», «Редактировать...» и «Анимировать...». '
          'Выберите нужный раздел: доступные модели, параметры и стоимость в кредитах будут показаны перед запуском.\n\n')
    if intent.get('durations_requested'):
        body+='Для ролика на '+', '.join(intent['durations_requested'])+' проверьте длительность одного клипа в настройках выбранной модели: итоговый ролик может потребовать нескольких сцен и монтажа.\n\n'
    body+='Сравнение отдельных подписок и остальные условия вашего вопроса пока не удалось проверить. Не буду советовать тариф без подтверждённых возможностей.'
    from .privacy import scrub
    return {'reply':body,'summary':'Сохранена полезная подтверждённая навигация генераций. Сравнение внешних подписок и остальные пункты не подтверждены; нужна проверка владельца.',
            'handoff':True,'source_ids':[],'evidence':{'status':'catalog_review','sources':[],
            'diagnostic':'Этап: подбор сервиса по задаче. '+scrub(str(reason))[:1200],
            'handoff_reason':'Публичные возможности для рекомендации и остальные пункты запроса не проверены.'}}
