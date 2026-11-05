"""Owner-confirmed manual new-account ChatGPT Plus guide, no account actions.

Exact SKU scope; customer statements never confirm payment or fulfillment.
History is used only for bounded dialogue choices, not as product facts.
"""
import re

GMAIL_QUESTION='У вас уже есть почта Gmail, которую хотите использовать для нового аккаунта ChatGPT?'
REGISTRATION_PROMPT='Когда аккаунт будет создан, напишите сюда.'
OWNER_FIELDS_PROMPT='Напишите желаемый адрес новой почты и резервную почту для восстановления.'
OWNER_DETAILS_ACK='Данные для согласования указаны в этой переписке; потребуется моя ручная проверка.'
OWNER_SESSION_PROMPT='Если новый аккаунт ChatGPT уже зарегистрирован, дальнейшее оформление согласуем здесь вручную.'


def _new_plus_plan(value):
    # Labels may use parentheses, a dash, or put the account qualifier last.
    # These are spelling/layout variants of an existing catalogue SKU, not a
    # licence to turn ordinary Plus into a new-account purchase.
    from .catalog_context import _words
    words=_words(value)
    return (bool(re.search(r'нов[а-яё]*\W+аккаунт|аккаунт\W+нов[а-яё]*|new\W+account|account\W+new',str(value),re.I))
            and any(w in ('plus','плюс') for w in words)
            and not any(w in ('pro','go','го','max','макс') for w in words))


def _only_chatgpt_mentions(mentioned):
    # service_mentions returns product-scoped IDs when a brand also sells
    # credits: catalog:chatgpt:chatgpt is not the bare family ID "chatgpt".
    # Compare the canonical public identities, not the implementation keys.
    from .catalog_context import _families,_words
    return bool(mentioned) and all(_families(_words(name))=={'chatgpt'}
                                 and not re.search(r'кредит|\bcredits?\b',name,re.I)
                                 for name in mentioned.values())


def _current_sku(facts,current):
    """Recover an explicitly requested SKU from fresh catalogue rows.

    Generic upstream plan grouping is not proof of a different selection. An
    explicit current new-account request can be resolved independently, while
    a bare yes/no must still have our own prompt and a client-owned SKU source.
    """
    from .catalog_context import _families,_words,_credit_product
    if _families(_words(current))!={'chatgpt'} or not _new_plus_plan(current):return facts
    if re.search(r'кредит|\bcredits?\b',str(current),re.I):return facts
    catalog=facts.get('catalog',[])
    candidates=[r for r in catalog if isinstance(r,dict)
                and _families(_words(str(r.get('service',''))+' '+str(r.get('service_key',''))))=={'chatgpt'}
                and not _credit_product(r) and _new_plus_plan(r.get('plan',''))]
    # Distinct product keys/plans are not silently merged, even at equal prices.
    identities={(str(r.get('service_key','')),str(r.get('service','')),str(r.get('plan',''))) for r in candidates}
    if len(identities)!=1:return facts
    return dict(facts,requested_catalog=candidates)


def selected_new_account(facts):
    from .catalog_context import _families,_words,_credit_product
    rows=facts.get('requested_catalog',[])
    if not rows or any(not isinstance(r,dict) for r in rows):return None
    for row in rows:
        identity=' '.join(str(row.get(k,'')) for k in ('service','service_key'))
        plan=str(row.get('plan',''))
        if _families(_words(identity))!={'chatgpt'} or _credit_product(row):return None
        if not _new_plus_plan(plan):return None
    if len({(r.get('service'),r.get('plan')) for r in rows})!=1:return None
    prices={r.get('catalog_price_rub') for r in rows}
    price=next(iter(prices)) if len(prices)==1 else None
    return dict(rows[0],catalog_price_rub=price if type(price) is int and price>0 else None)


def selection_diagnostic(facts,current):
    """Bounded public SKU/route trace; no client text, identifiers or secrets."""
    from .privacy import scrub,sensitive
    relevant=bool(_new_plus_plan(current))
    recovered=_current_sku(facts,current)
    rows=facts.get('requested_catalog',[])
    if not relevant:return ''
    def label(row):
        values=[]
        for key in ('service','plan','fulfillment'):
            value=str(row.get(key,''))
            values.append(key+'='+('[скрыто]' if sensitive(value) else scrub(value)[:90]))
        return '; '.join(values)
    safe_rows=[label(r) for r in rows[:3] if isinstance(r,dict)]
    selected=selected_new_account(recovered)
    return ('Этап: выбор товара нового аккаунта. Исходных выбранных строк='+str(len(rows))
            +'; точный SKU после явного выбора='+('да' if selected else 'нет')
            +'; каталог='+str(facts.get('catalog_status','unknown'))
            +'; строки (не более 3): '+' | '.join(safe_rows)
            +'. Это трассировка выбора, а не установленная первопричина предыдущего отказа.')


def _gmail_answer(text,question_before=False):
    value=re.sub(r'\s+',' ',str(text).casefold()).strip(' !?.,)')
    gmail=bool(re.search(r'gmail|джимейл|гмейл|почт[а-яё]*\s+гугл',value,re.I))
    if gmail:
        if re.search(r'нет|не\s+(?:имею|заводил|создавал)|без\s+gmail',value,re.I):return False
        if re.search(r'есть|имею|использую|у\s+меня|уже\s+(?:создал|завёл|завел)',value,re.I):return True
    if question_before:
        value=re.sub(r'[,;]+',' ',value)
        value=re.sub(r'\s+',' ',value).strip()
        if value in ('да','да есть','есть','да у меня есть','имеется'):return True
        if value in ('нет','нету','нет нету','нет у меня нет','нет почты'):return False
    return None


def _gmail_state(current,history):
    # Retain no arbitrary customer string, email, password or model summary.
    # Only a canonical yes/no choice from the latest bounded conversation.
    rows=[r for r in history[-20:] if isinstance(r,dict) and isinstance(r.get('text'),str)]
    state=None;last_question=False
    for row in rows:
        role=row.get('role')
        text=row['text']
        if role in ('assistant','owner'):
            last_question=GMAIL_QUESTION in text
            continue
        if role!='client':continue
        answer=_gmail_answer(text,last_question)
        if answer is not None:state=answer
        last_question=False
    answer=_gmail_answer(current,last_question)
    return state if answer is None else answer




def _registration_claim(text):
    match=re.search(r'зарегистрировал[а-яё]*|создал[а-яё]*\s+(?:новый\s+)?аккаунт|аккаунт\s+(?:уже\s+)?(?:создан|готов)',str(text),re.I)
    if not match:return None
    return not bool(re.search(r'(?<!\w)(?:не|нет)(?!\w)',str(text)[max(0,match.start()-20):match.start()],re.I))


def _reported_registration(history,facts):
    for row in reversed(list(history)[-20:]):
        if not isinstance(row,dict) or row.get('role')!='client':continue
        text=row.get('text','')
        from .catalog_context import service_mentions
        mentioned=service_mentions(text,facts.get('catalog',[]))
        if mentioned and not _only_chatgpt_mentions(mentioned):return False
        claim=_registration_claim(text)
        if claim is not None:return claim
        if re.search(r'нов[а-яё]*\s+аккаунт|new\s+account',str(text),re.I):break
    return False

def enrich_facts(facts,current,history=()):
    """Restore only an explicit client's new-account SKU after our own prompt.

    Caller must supply recent same-chat delivered history. Prompt text proves
    only which yes/no field was asked; SKU identity comes from client text and
    current catalogue, never from assistant output or model summaries.
    """
    from .catalog_context import annotate,service_mentions
    value=str(current or '')
    catalog=facts.get('catalog',[])
    if service_mentions(value,catalog):return facts
    rows=[r for r in list(history)[-40:] if isinstance(r,dict) and isinstance(r.get('text'),str)]
    index=next((i for i in range(len(rows)-1,-1,-1) if rows[i].get('role') in ('assistant','owner')),None)
    if index is None:return facts
    prompt=rows[index]['text']
    asked=next((p for p in (GMAIL_QUESTION,REGISTRATION_PROMPT,OWNER_FIELDS_PROMPT,OWNER_DETAILS_ACK,OWNER_SESSION_PROMPT) if p in prompt),None)
    if asked is None:return facts
    plain=re.sub(r'[\s!?.…,]+',' ',value.casefold()).strip()
    candidate=(_gmail_answer(value,asked==GMAIL_QUESTION) is not None
               or asked==REGISTRATION_PROMPT and (plain in ('готово','сделал','сделала','зарегистрировался','зарегистрировалась') or _registration_claim(value) is not None)
               or asked==OWNER_FIELDS_PROMPT and '[email скрыт]' in value
               or asked==OWNER_SESSION_PROMPT and bool(re.search(r'токен|сесси|как\s+(?:получить|найти|скопировать)',value,re.I))
               or asked==OWNER_DETAILS_ACK and plain in ('хорошо','спасибо','жду','понял','поняла','что дальше','как дальше'))
    if not candidate:return facts
    # A new explicit product after the prompt cancels its yes/no interpretation.
    for row in rows[index+1:]:
        if row.get('role')=='client' and service_mentions(row['text'],catalog):return facts
    for row in reversed(rows[:index]):
        if row.get('role')!='client':continue
        source=row['text'];mentions=service_mentions(source,catalog)
        if not mentions:continue
        if not _only_chatgpt_mentions(mentions) or not _new_plus_plan(source):return facts
        scoped=_current_sku(annotate(facts,source),source)
        new_rows=[r for r in scoped.get('requested_catalog',[]) if selected_new_account({'requested_catalog':[r]})]
        scoped=dict(scoped,requested_catalog=new_rows)
        if selected_new_account(scoped) is None:return facts
        return scoped
    return facts

def reply(facts,current,history=()):
    facts=_current_sku(facts,current)
    row=selected_new_account(facts)
    if row is None:return None
    value=str(current or '')
    from .catalog_context import service_mentions
    mentioned=service_mentions(value,facts.get('catalog',[]))
    if mentioned and not _only_chatgpt_mentions(mentioned):return None
    # This guide must never swallow a comparison, technical error or unrelated
    # task merely because the account SKU is still selected in context.
    from .request_goals import goals
    if goals(value,facts)-{'purchase','price','activation'}:return None
    if re.search(r'функци|возможност|сравн|отлич|что\s+входит|возврат|реферал',value,re.I):return None
    if not re.search(r'оформ|аккаунт|почт|gmail|гугл|google|куп|оплат|зарег|созда|сесси|токен|дальше|продолж|\[email скрыт\]|^\s*(?:да|нет|есть|готово|сделал|сделала|хорошо|спасибо|жду|понял|поняла)\b',value,re.I):return None
    gmail=_gmail_state(value,list(history))
    done=re.sub(r'[\s!?.…,]+',' ',value.casefold()).strip() in ('готово','сделал','сделала','зарегистрировался','зарегистрировалась')
    previous_assistant=next((r.get('text','') for r in reversed(list(history)[-20:]) if isinstance(r,dict) and r.get('role') in ('assistant','owner')), '')
    current_claim=_registration_claim(value)
    registered=(current_claim if current_claim is not None else bool(done and REGISTRATION_PROMPT in previous_assistant or _reported_registration(history,facts) or OWNER_SESSION_PROMPT in previous_assistant))
    price=row.get('catalog_price_rub')
    price_question=bool(re.search(r'сколько|цен[аыу]|стоимост',value,re.I))
    price_text=(f'Новый аккаунт ChatGPT с подпиской Plus: {price} ₽.\n\n' if price_question and price else '')
    if gmail is None and not registered:
        text=price_text+GMAIL_QUESTION
        stage='gmail_question';handoff=False
        diagnostic='Этап: новый аккаунт ChatGPT Plus. Выбран точный товар нового аккаунта; наличие Gmail пока не подтверждено. Задан один вопрос; регистрация, оплата и активация не выполнялись.'
    elif gmail is True and not registered:
        text=(price_text+'Тогда сначала зарегистрируйте новый аккаунт ChatGPT самостоятельно)\n\n'
              '1. Откройте chatgpt.com в браузере.\n'
              '2. Выберите создание аккаунта и вход через Google.\n'
              '3. Выберите свою почту Gmail и завершите регистрацию на экране сервиса.\n\n'
              +REGISTRATION_PROMPT+' Пароль Google для самостоятельной регистрации присылать не нужно. '
              'Оплату заказа и ручное оформление подписки согласуем отдельно; сама регистрация подписку не включает.')
        stage='customer_registration';handoff=False
        diagnostic='Этап: новый аккаунт ChatGPT Plus. Клиент сообщил о наличии Gmail. Даны шаги самостоятельной регистрации; факт регистрации и оплаты не подтверждён.'
    elif not registered and ((gmail is False and '[email скрыт]' in value and OWNER_FIELDS_PROMPT in previous_assistant) or OWNER_DETAILS_ACK in previous_assistant):
        text=(OWNER_DETAILS_ACK+' '
              'Помощник не создаёт аккаунты и не обрабатывает пароль. После согласования отдельно проверю оплату и порядок оформления подписки.')
        stage='owner_details_received';handoff=True
        diagnostic='Этап: ручное согласование нового аккаунта ChatGPT Plus. После подтверждённого запроса желаемого адреса и резервной почты получены скрытые поля email. Значения не передаются модели; полнота, корректность, доступность адреса, оплата и создание аккаунта не подтверждены. Нужна ручная проверка владельца.'
    elif gmail is False and not registered:
        text=(price_text+'Если Gmail нет, можно согласовать ручное создание почты Outlook и нового аккаунта ChatGPT.\n\n'
              +OWNER_FIELDS_PROMPT+' Пароль для новой почты согласуем лично; помощник его не обрабатывает.\n\n'
              'Создание аккаунтов и оформление подписки выполняются вручную после согласования и проверки оплаты заказа. Автоматически аккаунт сейчас не создаётся.')
        stage='owner_registration';handoff=True
        diagnostic='Этап: новый аккаунт ChatGPT Plus. Клиент сообщил об отсутствии Gmail; требуется ручное согласование адреса, резервной почты и пароля с владельцем. Секреты не запрашиваются в подготовку модели; создание аккаунтов не выполнялось.'
    else:
        text=(price_text+OWNER_SESSION_PROMPT+'\n\n'
              'Для ручного оформления откройте в том же браузере, где вошли в свой новый аккаунт, '
              'chatgpt.com/api/auth/session. Весь текст страницы сохраните одним текстовым файлом и пришлите сюда лично для ручной обработки. '
              'Это данные доступа к сессии: помощник не должен читать файл или передавать его модели.\n\n'
              'Подписка не считается оформленной только по сообщению об оплате. Сначала нужно проверить оплату и выполнить ручное оформление.')
        stage='owner_session_intake';handoff=True
        diagnostic='Этап: новый аккаунт ChatGPT Plus. Клиент сообщил о создании аккаунта; указана подтверждённая владельцем личная передача сессии одним текстовым файлом. Файл не должен загружаться, анализироваться или передаваться модели; нужна ручная обработка. Оплата и выдача подписки не подтверждены.'
    return {'reply':text,'summary':'Подтверждённая владельцем отдельная цепочка нового аккаунта ChatGPT Plus; этап='+stage+'. Действия с аккаунтом и платежом не выполнялись.',
            'handoff':handoff,'source_ids':[],'evidence':{'status':'catalog_direct','sources':[],
            'diagnostic':diagnostic,'new_account_stage':stage,'manual_secret_intake':stage in ('owner_registration','owner_session_intake','owner_details_received'),
            'handoff_reason':diagnostic if handoff else ''}}
