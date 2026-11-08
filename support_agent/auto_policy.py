"""Conservative content gate for the explicitly selected AUTO pilot.

This does not authorize a Telegram send. The caller must independently check
the persisted global/chat modes, selected test UID, revisions, permissions,
latest message, age and atomic send reservation immediately before dispatch.
No network, customer text in diagnostics, financial or account actions here.
"""
import math
import time
import re
import hashlib
import hmac
import secrets


# These proofs are produced only by deterministic local renderers. They are
# not an LLM field or confidence score. An old process's ready answers are not
# auto-replayed after restart; its proofs deliberately cannot authorize a new
# process. Nothing here is an external credential or configuration variable.
_CONSULTATION_KEY=secrets.token_bytes(32)
_CONSULTATION_KINDS={'order','course','workflow','activation','external','creative','installation'}
_PUBLIC_SESSION_URLS={
    'https://chatgpt.com/api/auth/session',
    'https://www.perplexity.ai/api/auth/session',
}


def _consultation_text(text):
    """Body the seal covers. A later greeting must not look like a different answer."""
    from .response_policy import natural_reply
    from .conversation_style import INTRO
    text=natural_reply(str(text or '')).replace('\r\n','\n').strip()
    if text.startswith(INTRO):
        text=text[len(INTRO):].strip()
    text=re.sub(r'^\s*(?:Здравствуйте|Приветствую|Добрый день|Добрый вечер|Доброе утро|Привет)[!.,:)\s]*',
                '',text,count=1,flags=re.I).strip()
    # The send path joins lines after the seal. Wording, not spacing, is the proof.
    return re.sub(r'\s+', ' ', text).strip()


def consultation_evidence(text,kind,**metadata):
    """Seal an immutable consultation, never a performed action or model prose."""
    if kind not in _CONSULTATION_KINDS:raise ValueError('Unknown local consultation renderer')
    payload=(kind+'\n'+_consultation_text(text)).encode('utf-8')
    signature=hmac.new(_CONSULTATION_KEY,payload,hashlib.sha256).hexdigest()
    return {**metadata,'status':kind+'_consultation','sources':[],
            'review_required':False,'consultation_contract':{'kind':kind,'proof':signature}}


def _local_consultation(text,evidence):
    contract=evidence.get('consultation_contract')
    if not isinstance(contract,dict) or set(contract)!={'kind','proof'}:return False
    kind=contract.get('kind');proof=contract.get('proof')
    if kind not in _CONSULTATION_KINDS or evidence.get('status')!=kind+'_consultation':return False
    if not isinstance(proof,str) or not re.fullmatch(r'[0-9a-f]{64}',proof):return False
    expected=consultation_evidence(text,kind)['consultation_contract']['proof']
    return hmac.compare_digest(proof,expected)


def _consultation_detection_copy(text):
    # Only these complete public URLs are instructional, never a private
    # session value. A query, fragment, credentials or a suffix is NOT removed.
    # The sealed renderer can explain field names and instruct where to put
    # them. Strip only bare field words in this detection copy. Credential
    # assignments/JWTs/API keys are rejected before this function is called;
    # unlabelled mixed values and unknown session URLs remain detectable.
    # A signed renderer may state that a field is not required. Recognise the
    # whole negative requirement, rather than erasing code words everywhere:
    # positive requests and field/value pairs must retain their secret trigger.
    code_qualifier=r'(?:одноразов|резервн)(?:ый|ые|ого|ых)'
    code_field=(r'(?:'+code_qualifier+r'(?:\s*(?:,|и)\s*'+code_qualifier+r')*\s+код(?:ы|а|ов)?|'
                r'код(?:ы)?\s+(?:для\s+)?(?:входа|подтверждения))')
    field=r'(?:пароль|пароли|password|токен(?:ы)?|token|сессия|session|cookie|cvv|'+code_field+r')'
    # Public labels are presentation names, not an unrestricted value slot.
    # Payload punctuation, all-capital identifiers, numbers and bare lowercase
    # strings do not qualify. The label stays in the sensitive detection copy.
    label=r'(?-i:(?:[A-Z][a-z]+(?:[A-Z][a-z]+)*|[А-ЯЁ][а-яё]+(?:[А-ЯЁ][а-яё]+)*)(?:\s+ID)?)'
    negative=re.compile(r'\s*'+field+r'(?:\s*(?:,|и)\s*'+field+r')*'
        r'(?:\s+(?:(?:от|для)\s+)?'+label+r')?\s+'
        r'(?:(?:присылать|отправлять|сообщать|передавать)\s+)?не\s+'
        r'(?:нужен|нужна|нужны|нужно|требуется|требуются|надо)'
        r'(?:\s+(?:присылать|отправлять|сообщать|передавать))?\s*',re.I)
    pieces=[]
    for part in re.split(r'(https?://[^\s<>]+)',str(text)):
        if part.startswith(('https://','http://')):
            url=part.rstrip('.,;)')
            pieces.append(part[len(url):] if url in _PUBLIC_SESSION_URLS else part)
        else:
            fragments=re.split(r'([.!?](?:\s+|$)|\n)',part)
            bare_value=any(re.fullmatch(r'\s*[A-Za-z0-9!@$%^&*_.+\-]{5,}\s*',fragment)
                           for fragment in fragments if fragment.strip())
            if not bare_value:
                part=''.join(re.sub(code_field,'[поле]',fragment,flags=re.I)
                             if negative.fullmatch(fragment) else
                             re.sub(code_field,'код входа',fragment,flags=re.I) for fragment in fragments)
            else:
                part=re.sub(code_field,'код входа',part,flags=re.I)
            pieces.append(re.sub(r'(?<!\w)(?:парол[ьяеию]?|пароли|password|токен[а-яё]*|token|'
                          r'сесси[яиюей]+|session|cookie|cvv)(?!\w)','[поле]',part,flags=re.I))
    return ''.join(pieces)


def _reason(condition):
    return 'Этап: допуск автоматического ответа. Причина: '+condition


def owner_reason(value):
    """Safe diagnostic body, with presentation headings owned by the UI only."""
    from .privacy import scrub,sensitive
    value=str(value or '')
    value=re.sub(r'(?im)^\s*(?:🛑\s*)?(?:<b>|\*\*)?Почему этот ответ не отправлен автоматически(?:</b>|\*\*)?\s*[:\n]*','',value).strip()
    safe=scrub(value)
    detection=re.sub(r'(?<!\w)(?:account_email|account_password|checkout_url|course_registration_email|required_fields|workflow_steps|provided_fields|account_ownership)(?!\w)','[имя поля]',safe,flags=re.I)
    # Only machine-owned status/count fields, not arbitrary key=value payloads.
    detection=re.sub(r'(?i)(?:живой экспорт|источник|статус источника|полученные ветки|ветки|source|live_shop_status|fulfillment)\s*=\s*(?:http_[45]\d\d|not_verified|not_connected|unavailable|available|account_details|checkout_link|manual|code_activation_conditional)(?=$|[\s,;.])','[статус источника]',detection)
    detection=re.sub(r'(?i)(?<!\w)(?:HTTP|Код Telegram)\s*=\s*[1-5]\d\d(?=$|[\s,;.])','[код протокола]',detection)
    # Public diagnostic counters can include Latin source names (Apple=0).
    # They are not mixed alphanumeric credentials. Recognise only complete,
    # bounded numeric fields in the detection copy; keep their visible values.
    detection=re.sub(r'(?i)(?<!\w)(?:релевантных регионов Apple|Apple|Сумма ответа|'
                     r'обычных тарифов|регионов в проверке|регионов|совпадений|'
                     r'разных корректных цен)\s*=\s*\d{1,9}(?:[.,]\d{1,2})?(?=$|[\s;.])',
                     '[числовое поле диагностики]',detection)
    detection=re.sub(r'(?i)(?:поле|поля|полей)\s+(?:пароля|токена|сессии)(?=[\s.,;:]|$)','[имя поля]',detection)
    detection=re.sub(r'(?i)(\[имя поля\]\s*[/,]\s*)(?:пароля|токена|сессии)(?=$|[.,;!?]|\s+(?:не|подтвержден|проверен|отсутств|нуж|треб))',r'\1[имя поля]',detection)
    if not sensitive(detection):return safe[:1400]
    stage=re.search(r'Этап:\s*([А-Яа-яЁё ]{3,90})\.',safe)
    return (('Этап: '+stage[1]+'. ' if stage else '')+'Остальные детали скрыты: возможны данные доступа.')


def block_reason(answer,evidence=None,current_text=''):
    """Return a safe owner-only reason, or None for content eligible to send.

    Handoff and semantic review are necessary signals, never sufficient alone.
    Unknown evidence contracts remain manual; this pilot deliberately permits
    fewer responses than the owner may approve after reading a draft.
    """
    if not isinstance(answer,dict):return _reason('ответ отсутствует или имеет неверный тип.')
    if type(answer.get('handoff')) is not bool:
        return _reason('флаг участия владельца отсутствует или имеет неверный тип.')
    meta=evidence if isinstance(evidence,dict) else answer.get('evidence',{})
    if isinstance(meta,dict) and (meta.get('conflict_hold') or meta.get('owner_alignment_hold')
                                  or meta.get('stale_price_hold') or meta.get('owner_fact_hold')
                                  or meta.get('repeat_hold')):
        detail=meta.get('diagnostic') or meta.get('handoff_reason') or ''
        return _reason(detail or 'черновик удержан по конфликту, реплике владельца или устаревшей цене.')
    from .response_decision import verified_atomic_decision
    decision=verified_atomic_decision(answer,meta)
    atomic_partial=bool(decision and decision['coverage_complete'] is False)
    from .dialog_state import client_collection
    collecting=client_collection(answer.get('reply'))
    if answer['handoff'] and not atomic_partial and not collecting:
        meta=evidence if isinstance(evidence,dict) else answer.get('evidence',{})
        detail=''
        if isinstance(meta,dict):
            detail=meta.get('diagnostic') or meta.get('handoff_reason') or ''
            if not detail and isinstance(meta.get('content_review'),dict) and meta['content_review'].get('supported') is False:
                detail=meta['content_review'].get('reason','')
        if not isinstance(detail,str):detail=''
        if not detail:
            detail=str(answer.get('summary') or '').strip()
        if detail:
            detail=owner_reason(detail)
        return _reason('ответ требует участия владельца; автоматическая отправка не разрешена.'
                       +(' Основание: '+detail if detail else
                         ' Основание: модель запросила владельца, отдельная диагностика не сохранена.'))
    text=answer.get('reply')
    if not isinstance(text,str) or not text.strip():return _reason('текст ответа пуст или имеет неверный тип.')
    if len(text)>1800:return _reason('текст превышает проверенный лимит 1800 символов.')
    if evidence is None:evidence=answer.get('evidence')
    if not isinstance(evidence,dict):return _reason('сохранённые подтверждения ответа отсутствуют.')
    status=evidence.get('status')
    consultation=_local_consultation(text,evidence)
    from .reviewed_workflow import detection_copy as reviewed_workflow_detection
    reviewed_detection=reviewed_workflow_detection(answer,evidence)
    if evidence.get('reviewed_workflow_contract') is not None and reviewed_detection is None:
        return _reason('подтверждение проверенных полей оформления недействительно; нужна новая проверка ответа.')
    if status in {kind+'_consultation' for kind in _CONSULTATION_KINDS} and not consultation:
        return _reason('кодовая консультация не имеет действующего подтверждения неизменённого текста; нужна новая подготовка.')
    if evidence.get('review_required') is True:
        return _reason('сохранено явное требование проверки содержания; действие по оформлению и проверка текста различаются.')
    if evidence.get('manual_secret_intake'):
        return _reason('предусмотрена ручная обработка данных доступа; требуется владелец.')
    if evidence.get('handoff_reason') and not collecting:
        return _reason('сохранена причина ручного разбора; автоматический допуск не подтверждён.')

    current=str(current_text or '')
    if re.search(r'(?:позовите|позови|подключите|соедините|свяжите)[^.!?\n]{0,45}(?:человек|оператор|александр)|'
                 r'(?:хочу|нужен|нужна|можно)[^.!?\n]{0,25}(?:жив[а-яё]*\s+человек|оператор)|'
                 r'(?:хочу|можно)[^.!?\n]{0,25}(?:поговорить|общаться)[^.!?\n]{0,25}(?:с\s+человеком|с\s+александром)',current,re.I):
        return _reason('клиент явно просит человека; дальнейшее общение передано владельцу.')
    # Never expose the matching value or whole question in an owner diagnostic.
    if re.search(r'\[\s*Сообщение скрыто|\[данные доступа скрыты\]|\[токен скрыт\]',current,re.I):
        return _reason('в обращении сохранён признак скрытых данных доступа; нужен ручной разбор.')
    from .privacy import sensitive
    from .public_plan_reference import valid as public_plan_reference
    if sensitive(current) and not public_plan_reference(current,evidence):
        return _reason('обращение может содержать данные доступа; нужен ручной разбор.')
    if (reviewed_detection is not None or consultation and evidence['consultation_contract']['kind']=='workflow') and re.search(r'\[email скрыт\]|\[ссылка скрыта\]',current,re.I):
        return _reason('получены данные или ссылка для оформления; их обработку выполняет владелец.')
    # A short follow-up such as "да" may not repeat the financial/account
    # subject. Do not let a falsely optimistic review approve action claims or
    # collection of secrets merely because the current fragment looks benign.
    if not consultation and reviewed_detection is None and re.search(r'(?:пришлите|отправьте|передайте|сообщите|укажите)[^.!?\n]{0,120}'
                 r'(?:парол|токен|текст\s+сесси|данные\s+(?:для\s+)?входа|код(?:ы)?\s+(?:входа|подтверждения)|(?:одноразов|резервн)[а-яё]*\s+код)',text,re.I):
        return _reason('ответ запрашивает данные доступа; сбор этих данных оставлен владельцу.')
    from .reply_assertion import collects_case_data, asserts_outcome
    reply_class=evidence.get('reply_class') if isinstance(evidence,dict) else ''
    if reply_class in ('collect','instruct','catalog_price') and not asserts_outcome(text):
        return None
    if reply_class in ('outcome','promise','unverified'):
        detail=evidence.get('diagnostic') if isinstance(evidence,dict) else ''
        return _reason(detail or 'ответ утверждает непроверенный результат.')
    if collects_case_data(text) and not asserts_outcome(text):
        return None
    from .order_status import is_status_request
    if is_status_request(current) and asserts_outcome(text):
        return _reason('ответ сообщает результат оплаты, активации или возврата; это проверяет владелец.')
    if re.search(r'возврат|верн[а-яё]*\s+деньг|чарджб[еэ]к|chargeback|'
                 r'чек\b|квитанци|резервн[а-яё]*\s+код|одноразов[а-яё]*\s+код',current,re.I):
        return _reason('обращение касается возврата, платёжного подтверждения или кодов доступа; нужен владелец.')
    from .completion_claims import has_completion_claim
    if collecting and has_completion_claim(text):
        return _reason('ответ одновременно запрашивает данные и утверждает, что оформление уже выполнено.')
    if collecting:
        return None
    # Mentioning that no password is needed is not providing its value. Strip
    # only this exact code-owned public sentence from the detection copy;
    # secrets anywhere else, including the next sentence, remain detectable.
    from .privacy import redaction_categories
    if consultation and ({'access_secret','credential_line'} & set(redaction_categories(text))
            or re.search(r'(?i)(?:парол[ья]?|password|токен|token|session|cookie|cvv)\s*[:=]\s*\S',text)):
        return _reason('кодовая консультация содержит значение доступа или присваивание; автоматическая отправка запрещена.')
    detection=(reviewed_detection if reviewed_detection is not None else _consultation_detection_copy(text) if consultation else
               re.sub(r'(?<!\w)Пароль от аккаунта для этого способа не нужен\.(?=\s|$)','',text))
    if consultation and evidence['consultation_contract']['kind']=='installation':
        # Only URLs emitted by this sealed renderer; a modified query, suffix
        # or fragment remains visible to the ordinary sensitive-data checks.
        from .app_installation import ANDROID,PLAY,IOS,APPLE,DESKTOP
        installer_urls={ANDROID,PLAY,IOS,APPLE,DESKTOP}
        detection=re.sub(r'https?://[^\s<>]+',lambda match:
            '[публичная страница установки]' if match[0] in installer_urls else match[0],detection)
    if decision is not None:
        from .public_metrics import detection_copy as public_metric_detection
        detection=public_metric_detection(detection)
    if sensitive(detection):return _reason('ответ может содержать данные доступа; автоматическая отправка запрещена.')
    if consultation:
        # Local renderers describe checked observations or the next question;
        # this does not execute payment/refund/activation or grant eligibility.
        from .completion_claims import has_completion_claim
        if has_completion_claim(text):
            return _reason('даже кодовая консультация не может утверждать выполнение возврата или оформления.')
        return None
    from .completion_claims import has_completion_claim
    if has_completion_claim(text):
        return _reason('ответ утверждает завершение финансового действия или оформления; в пилоте нужен владелец.')
    from .order_status import is_status_request
    if is_status_request(current) and asserts_outcome(text):
        return _reason('ответ сообщает результат оплаты, активации или возврата; это проверяет владелец.')
    if re.search(r'возврат|верн[а-яё]*\s+деньг|чарджб[еэ]к|chargeback|'
                 r'чек\b|квитанци|резервн[а-яё]*\s+код|одноразов[а-яё]*\s+код',current,re.I):
        return _reason('обращение касается возврата, платёжного подтверждения или кодов доступа; нужен владелец.')
    from .dialog_state import grants_unverified_benefit
    if grants_unverified_benefit(text):
        return _reason('ответ подтверждает выдачу бонуса или льготы; это решает владелец.')
    if evidence.get('new_account_stage') in ('owner_registration','owner_session_intake','owner_details_received'):
        return _reason('текущий этап нового аккаунта требует ручного оформления владельцем.')

    if not isinstance(status,str):return _reason('статус подтверждений отсутствует или имеет неверный тип.')
    if 'content_review' in evidence and not isinstance(evidence['content_review'],dict):
        return _reason('сохранённый результат проверки содержания имеет неверный тип.')
    if status in ('catalog_direct','clarification'):
        # These statuses are written only by deterministic routes. The rest of
        # the system must not relabel free model prose as one of these routes.
        if evidence.get('content_review',{}).get('supported') is False:
            return _reason('сохранён отказ проверки содержания ответа.')
        return None
    if status not in ('checked','catalog_checked') and not (atomic_partial and status=='insufficient'):
        return _reason('статус подтверждений='+status_safe(status)+'; этот тип ответа не разрешён в автоматическом пилоте.')
    review=evidence.get('content_review')
    if not isinstance(review,dict) or review.get('supported') is not True:
        return _reason('нет сохранённого положительного результата проверки содержания.')
    projection=answer.get('feature_projection') or evidence.get('feature_projection')
    if isinstance(projection,dict) and projection.get('incomplete_target_ids') and not atomic_partial:
        return _reason('сравнение содержит неподтверждённые цели; требуется проверка владельца.')
    from .response_policy import public_question
    sources=evidence.get('sources')
    public=(atomic_partial or status=='checked' or public_question(current) or bool(answer.get('source_ids')))
    if public:
        if not isinstance(sources,list) or not sources:
            return _reason('для публичных утверждений нет сохранённых источников.')
        from urllib.parse import urlsplit
        ids=[]
        for source in sources:
            if not isinstance(source,dict) or type(source.get('id')) is not int or source['id']<=0:
                return _reason('сохранённый источник имеет неверный идентификатор.')
            try:
                url=urlsplit(source.get('url',''))
                valid=url.scheme=='https' and bool(url.hostname) and not url.username and not url.password and url.port in (None,443)
            except (TypeError,ValueError):valid=False
            if not valid:return _reason('сохранённый источник имеет недопустимый адрес; адрес скрыт.')
            ids.append(source['id'])
        if len(ids)!=len(set(ids)):return _reason('в подтверждениях повторены идентификаторы источников.')
        checked=evidence.get('checked_at')
        if type(checked) not in (int,float) or not math.isfinite(checked) or checked<=0:
            return _reason('время проверки публичных источников отсутствует или некорректно.')
        if not -30<=time.time()-checked<=900:
            return _reason('проверка публичных источников старше 15 минут либо её время находится в будущем; нужен новый разбор.')
    return None


def status_safe(value):
    """Fixed vocabulary only; no arbitrary provider value in diagnostics."""
    known={'unavailable','insufficient','partial','catalog_review','order_status','owner_policy',
           'visual_review','visual_clarification','not_requested','clarification','catalog_direct',
           'checked','catalog_checked','topic_boundary',
           'order_consultation','course_consultation','workflow_consultation','activation_consultation','external_consultation','creative_consultation','installation_consultation'}
    return value if value in known else 'unknown'
