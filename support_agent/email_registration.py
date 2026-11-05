"""Owner-confirmed registration email FAQ, not provider eligibility assertions.

No credentials or email values are retained, echoed or sent to a model here.
The owner may later create Outlook manually; this consultation does not do it.
"""
import re

GMAIL_PROMPT='У вас есть почта gmail.com, которую хотите использовать?'
OUTLOOK_PROMPT='Могу вручную подготовить для вас новую почту outlook.com после согласования.'
POLICY={
    'source':'owner_confirmed',
    'chatgpt_claude_registration_email':'gmail.com preferred; owner-created outlook.com if unavailable',
    'russian_email_for_chatgpt_claude':'not recommended in this shop workflow; not a universal provider rule',
    'other_services':'gmail.com preferred; existing Russian mailbox may be tried without guarantee',
    'owner_can_create_outlook':True,
    'agent_creates_accounts':False,
    'passwords_processed_by_model':False,
    'recovery_email':'supplied personally in this same conversation, no values in model context',
}


def _families_for(text,catalog):
    from .catalog_context import service_mentions,_families,_words
    names=service_mentions(str(text or ''),catalog)
    return names,{family for name in names.values() for family in _families(_words(name))}


def _scope(facts,current,history):
    catalog=facts.get('catalog',[])
    mentioned,families=_families_for(current,catalog)
    if mentioned:return families or {'other'}
    # Only client-owned topic selection establishes the subject. An earlier
    # model response mentioning a brand is not evidence the client selected it.
    for row in reversed(list(history)[-30:]):
        if not isinstance(row,dict) or row.get('role')!='client':continue
        mentioned,families=_families_for(row.get('text',''),catalog)
        if mentioned:return families or {'other'}
    from .catalog_context import _families,_words
    rows=facts.get('requested_catalog',[])
    identities={str(row.get('service','')) for row in rows if isinstance(row,dict)}
    if len(identities)==1:
        found={family for name in identities for family in _families(_words(name))}
        return found or {'other'}
    return set()


def _no_gmail(text):
    text=str(text or '').casefold()
    word=r'(?:g\s*mail|джимейл|гмейл|гмайл|гмаил|гмайл|гмаил|почт[а-яё]*\s+гугл)'
    return bool(re.search(word,text) and re.search(r'нет|нету|не\s+(?:имею|создал|зав[её]л|пользуюсь)|без\s+g\s*mail',text))


def reply(facts,current,history=()):
    value=str(current or '')
    if not value.strip():return None
    # Actual access values must remain in the existing manual-intake route.
    from .privacy import sensitive
    if sensitive(value) or '@' in value or re.search(r'\[.*(?:скрыт|данные доступа)',value,re.I):return None
    if re.search(r'возврат|чардж|взлом|не\s+приходит\s+код|код\s+не\s+приходит|номер\s+заказа|free\s*kassa',value,re.I):return None
    email_topic=bool(re.search(r'почт|g\s*mail|джимейл|гмейл|гмайл|outlook|аутлук|яндекс|mail\.ru|rambler',value,re.I))
    last=next((str(r.get('text','')) for r in reversed(list(history)[-8:])
               if isinstance(r,dict) and r.get('role') in ('assistant','owner')),'')
    plain=re.sub(r'[\s!?.…,]+',' ',value.casefold()).strip()
    short_reply=GMAIL_PROMPT in last and plain in ('нет','нету','нет у меня нет','нет почты','да','есть','да есть')
    if not email_topic and not short_reply:return None
    scope=_scope(facts,value,history)
    if not scope:return None
    special=bool(scope & {'chatgpt','claude'})
    mixed=special and bool(scope-{'chatgpt','claude'})
    current_no=_no_gmail(value) or short_reply and plain.startswith(('нет','нету'))
    create=bool(re.search(r'(?:созда[а-яё]*|зав[её][а-яё]*|сдела[а-яё]*)[^.!?\n]{0,65}почт|почт[^.!?\n]{0,45}(?:созда[а-яё]*|зав[её][а-яё]*|сдела[а-яё]*)',value,re.I))
    if special:
        name='ChatGPT и Claude' if {'chatgpt','claude'}<=scope else ('ChatGPT' if 'chatgpt' in scope else 'Claude')
        lead=f'Для регистрации {name} при оформлении через нас рекомендую gmail.com. Российскую почту для этой регистрации не используем.'
        if current_no or create:
            text=(lead+'\n\n'+OUTLOOK_PROMPT+' Напишите, какой адрес хотели бы получить, и резервную почту для восстановления. '
                  'Пришлите их сюда, в эту переписку. Доступ к новой почте согласуем лично здесь. '
                  'Сейчас почта и аккаунт ещё не созданы; оформление и оплату проверю отдельно.')
            stage='outlook_consultation';action=True
        elif short_reply and plain in ('да','есть','да есть'):
            site='chatgpt.com' if scope=={'chatgpt'} else 'claude.ai' if scope=={'claude'} else 'нужного сервиса'
            text=(f'Тогда используйте эту Gmail для регистрации на сайте {site}. Выберите регистрацию с этой почтой и завершите шаги, которые показывает сервис. '
                  'Если появится ошибка, пришлите её скриншот без данных доступа, помогу разобраться)')
            stage='gmail_registration';action=False
        else:
            text=lead+'\n\n'+GMAIL_PROMPT+' Если нет, могу предложить ручное создание новой почты outlook.com.'
            stage='gmail_question';action=False
        if mixed:
            text+='\n\nДля остальных сервисов тоже сначала рекомендую Gmail. Если её нет, можно попробовать действующую российскую почту, но принятие адреса зависит от конкретного сервиса.'
    else:
        if create:
            text=(OUTLOOK_PROMPT+' Напишите желаемый адрес и резервную почту для восстановления сюда, в эту переписку. '
                  'Доступ согласуем лично; автоматически почта не создаётся.')
            stage='outlook_consultation';action=True
        else:
            text=('Для регистрации сначала рекомендую gmail.com. Если Gmail нет, в этом сервисе можно попробовать вашу действующую российскую почту. '
                  'Это не гарантия регистрации: сервис может отклонить адрес или запросить дополнительные проверки. '
                  'Если появится ошибка, пришлите скриншот без данных доступа, подскажу дальше)')
            stage='other_service_email';action=False
    return {'reply':text,'summary':'Консультация по подтверждённому правилу владельца о почте; этап='+stage+'. Почта, аккаунт и оплата не создавались и не подтверждались.',
            'handoff':False,'source_ids':[],
            'evidence':{'status':'catalog_direct','sources':[],'email_registration_policy':True,
                        'email_registration_stage':stage,'owner_action_required':action,'review_required':False,
                        'diagnostic':'Этап: выбор почты регистрации. Применено правило владельца, не универсальное ограничение провайдера. Значения почт и данные доступа не использовались.'}}
