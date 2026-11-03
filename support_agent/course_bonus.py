"""Owner-approved course bonus intake. Never grants benefits or creates orders."""
import re


POLICY = {
    'source':'owner_confirmed_2026_11_03',
    'duration':'one_month',
    'service_choices':['ChatGPT','Claude'],
    'eligibility':'manual_owner_check_required',
    'required_field':'course_registration_email',
    'delivery_channel':'same_conversation',
    'plan_tier':'not_confirmed',
    'automatic_grant':False,
}


def named_course(text):
    return bool(re.search(r'завод[а-я]*\s*нейроагент|(?:аи|ай|ai)[ -]*эксперт|ai[ -]*expert',str(text),re.I))


def registration_email_message(text):
    text=str(text)
    has_email=bool(re.search(r'\[email скрыт\]|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}',text))
    account=bool(re.search(r'аккаунт|chat\s*gpt|claude|чат\s*гпт|клод',text,re.I))
    registration=bool(re.search(r'курс|обучени|регистрац',text,re.I))
    return has_email and (not account or registration)


def exited(text):
    text=str(text)
    if re.search(r'не\s+(?:с|из)\s+(?:курса|обучения)|бонус\s+не\s+нуж|вместо\s+бонус',text,re.I):return True
    return bool(re.search(r'(?:хочу|планирую)\s+(?:купить|оплатить)|платн[а-я]*\s+подпис',text,re.I)
                and not re.search(r'бонус|курс|обучени|куратор',text,re.I))


def introduction(text):
    text=str(text).casefold().replace('ё','е')
    if re.search(r'не\s+(?:с|из)\s+(?:курса|обучения)',text):return False
    named=named_course(text)
    course=bool(re.search(r'курс|обучени|куратор',text))
    benefit=bool(re.search(r'бонус|подар|бесплат|подписк[а-я]*\s+на\s+месяц|полож[а-я]*[^.!?\n]{0,35}месяц',text))
    return (named and bool(re.search(r'подписк|бонус|подар|куратор|обучени|курс',text))) or (course and benefit)


def context(current,history):
    """Only course introductions and explicit followups; old topics do not spread."""
    text=str(current or '')
    if exited(text):return None
    direct=introduction(text)
    recent=history[-24:]
    boundaries=[i for i,m in enumerate(recent) if m.get('role')=='client' and exited(m.get('text',''))]
    if boundaries:recent=recent[boundaries[-1]+1:]
    anchors=[i for i,m in enumerate(recent) if m.get('role')=='client' and introduction(m.get('text',''))]
    prior=bool(anchors)
    follow=named_course(text) or bool(re.search(r'бонус|курс|куратор|обучени|выбира|выбрал|выбрала|сравн|отлич|лучше|'
        r'для\s+(?:работы|учебы|кода|документ)|почт|email|\[email скрыт\]|@|'
        r'chat\s*gpt|чат\s*гпт|claude|клод|как\s+(?:это\s+)?оформ|'
        r'что\s+(?:дальше|нужно|потребуется)|сколько\s+стоит',text,re.I))
    from .catalog_context import _families,_words
    if prior and any(m.get('role')=='client' and _families(_words(m.get('text','')))-{'chatgpt','claude'}
                     for m in recent[anchors[-1]+1:]):
        prior=False
    other=_families(_words(text))-{'chatgpt','claude'}
    if not direct and (not prior or not follow or other or re.search(r'теперь\s+(?:друг|не)|вместо\s+бонус|бонус\s+не\s+нуж',text,re.I)):
        return None
    compare=bool(re.search(r'сравн|отлич|лучше|(?:что|какой|помоги)\s+(?:мне\s+)?выбрать|'
                          r'посовету|возможност|функци|блокиров|\bрф\b|росси',text,re.I))
    email=registration_email_message(text)
    email_before=any(m.get('role')=='client' and registration_email_message(m.get('text',''))
                     for m in recent[anchors[-1]:]) if prior else False
    identified=named_course(text) or (prior and any(named_course(m.get('text','')) for m in recent[anchors[-1]:]))
    return dict(POLICY,comparison_requested=compare,email_already_provided=email or email_before,
                course_identity='named_program' if identified else 'unconfirmed')


def reply(policy):
    if policy.get('course_identity')=='unconfirmed':
        text=('Подскажите, пожалуйста, название курса. Условия бонуса нужно проверить именно по вашей программе. '
              +('Почту повторно отправлять не нужно.' if policy['email_already_provided'] else
                'Пришлите также почту регистрации на курсе, без пароля.'))
    elif policy['comparison_requested']:return None
    elif policy['email_already_provided']:
        text=('Спасибо! Доступность бонусной подписки нужно проверить вручную по почте регистрации на курсе. '
              'После проверки согласуем оформление ChatGPT или Claude на месяц. Повторно отправлять почту не нужно.')
    else:
        text=('По условиям курса предусмотрена бонусная подписка на месяц: ChatGPT или Claude на выбор. '
              'Пришлите сюда почту, на которую регистрировались на курсе, я проверю, доступен ли вам бонус. '
              'Пароль от почты не нужен. Если ещё не определились с сервисом, помогу сравнить их под ваши задачи)')
    return {'reply':text,'summary':'Обращение по бонусу курса. Право на бонус не подтверждено; нужна ручная проверка владельца по почте регистрации. Тариф, выдача и активация не обещаны.',
            'handoff':True,'source_ids':[],
            'evidence':{'status':'catalog_direct','sources':[],
                'handoff_reason':'Бонус курса: доступность проверяет владелец вручную. '+
                    ('В сообщениях есть почта; значение не включено в диагностику.' if policy['email_already_provided'] else 'Нужна почта регистрации на курсе.')}}
