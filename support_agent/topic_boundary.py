"""Owner's topical boundaries, before catalog shortcuts and public research.

The decision uses client intent, never a model's previous recommendation.  A
short anaphoric follow-up can inherit a blocked intent, but a new named task
does not stay blocked merely because the history once contained that topic.
"""
import re
import unicodedata


def _text(value):
    text=unicodedata.normalize('NFKC',str(value or '')).casefold().replace('ё','е')
    return re.sub(r'\s+',' ',text).strip()


def _category(text):
    # Product safety/age restrictions may be explained neutrally. They do not
    # permit advice about finding or generating the prohibited material.
    neutral=bool(re.search(r'запрещ\w*|ограничени\w*|правил\w*|разрешен\w*|допуска\w*',text))
    action=bool(re.search(r'созда\w*|генер\w*|сдела\w*|нарис\w*|разде\w*|покаж\w*|посовет\w*|порекоменд\w*|подбер\w*|какой\s+сервис|где\s+можно',text))
    sexual=bool(re.search(r'сексуальн\w*|\bсекс\b|интим\w*|флирт\w*|порно\w*|эрот\w*|обнажен\w*|гол(?:ая|ые|ую|ых|ый)\b|\bnsfw\b|\bnudes?\b|\bporn\w*|\berotic\w*|\bsexual\b|18\s*\+',text))
    if sexual and (not neutral or action):return 'sexual_content'
    if re.search(r'взлом\w*|взломать|украсть\w*|фишинг\w*|краж\w*\s+(?:данн\w*|парол\w*)|доступ\w*\s+к\s+чуж\w*\s+(?:аккаунт\w*|данн\w*)|поддел\w*\s+(?:паспорт\w*|документ\w*|чек\w*)|отмы\w*\s+деньг\w*|обман\w*\s+(?:клиент\w*|банк\w*|покупател\w*)|\bphishing\b|hack\s+(?:someone|an?\s+account)',text):
        # Reporting a compromised personal account is support, not a request
        # for hacking instructions. Mixed offensive requests still refuse.
        victim=bool(re.search(r'(?:мой|меня|мне|моего)\b',text))
        request=bool(re.search(r'как\s+взлом|помоги\w*\s+взлом|хочу\s+взлом|чуж\w*\s+аккаунт|фишинг|поддел',text))
        if not victim or request:return 'illegal_assistance'
    if re.search(r'политик\w*|политическ\w*|выбор\w*\s+президент\w*|за\s+кого\s+голосовать|кого\s+поддерживаешь|религи\w*|кто\s+прав\s+в\s+войне|агитаци\w*|пропаганд\w*',text):
        # Privacy policies and service region/payment policies are on topic.
        if not re.search(r'политик\w*\s+(?:конфиденциальност\w*|возврат\w*|оплат\w*|обработк\w*|сервис\w*|использован\w*)',text):
            return 'off_topic_discussion'
    return None


def boundary_reply(current, context=''):
    """Return a safe fixed answer or None; no API, prices or generation advice.

    `context` should contain resolved, relevant *client* text, not an assistant
    answer. Only a genuinely referential current message may use it.
    """
    text=_text(current)
    category=_category(text)
    inherited=False
    if not category and re.search(r'\b(?:такое|такие|этот|это|этим|так)\b',text):
        referential=bool(re.search(r'генер\w*|сдела\w*|созда\w*|можно|можете|умеет|подойдет|разрешено|позволя\w*',text))
        # A self-contained new task must not revive a blocked earlier topic.
        new_task=bool(re.search(r'документ\w*|код\w*|программ\w*|товар\w*|реклам\w*|презентац\w*|встреч\w*|подписк\w*|оплат\w*|тариф\w*',text))
        if referential and not new_task:
            category=_category(_text(context))
            inherited=bool(category)
    if not category:return None
    replies={
        'sexual_content':'С созданием сексуального контента 18+ и подбором инструментов для него не помогаю. По обычным задачам, подпискам и возможностям сервисов подскажу)',
        'illegal_assistance':'С незаконными действиями, обманом и доступом к чужим данным не помогаю. По оформлению подписок и работе сервисов подскажу)',
        'off_topic_discussion':'Политические и религиозные дискуссии здесь не веду. По подпискам и возможностям сервисов помогу)',
    }
    return {
        'reply':replies[category],
        'summary':'Применена граница тематики владельца. Рекомендации сервисов и генераций по запрещённой цели не выдавались.',
        'handoff':False,
        'evidence_status':'topic_boundary',
        'diagnostic':('Этап: проверка тематики обращения. Категория='+category+
                      '; основание='+('ссылочное уточнение к запрещённой цели клиента' if inherited else 'явный текущий запрос')+
                      '. Поиск, подбор тарифов и генераций не запускались. Текст клиента не включён в диагностику.'),
    }
