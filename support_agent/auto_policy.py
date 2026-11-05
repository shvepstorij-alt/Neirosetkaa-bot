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
_CONSULTATION_KINDS={'order','course','workflow','activation','external','creative'}
_PUBLIC_SESSION_URLS={
    'https://chatgpt.com/api/auth/session',
    'https://www.perplexity.ai/api/auth/session',
}


def _consultation_text(text):
    from .response_policy import natural_reply
    text=natural_reply(str(text or '')).replace('\r\n','\n').strip()
    return re.sub(r'^\s*(?:Здравствуйте|Приветствую|Добрый день|Добрый вечер|Доброе утро|Привет)[!.,:)\s]*',
                  '',text,count=1,flags=re.I).strip()


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
    pieces=[]
    for part in re.split(r'(https?://[^\s<>]+)',str(text)):
        if part.startswith(('https://','http://')):
            url=part.rstrip('.,;)')
            pieces.append(part[len(url):] if url in _PUBLIC_SESSION_URLS else part)
        else:
            pieces.append(re.sub(r'(?<!\w)(?:парол[ьяеию]?|пароли|password|токен[а-яё]*|token|'
                          r'сесси[яиюей]+|session|cookie|cvv|код\s+входа)(?!\w)','[поле]',part,flags=re.I))
    return ''.join(pieces)


def _reason(condition):
    return 'Этап: допуск автоматического ответа. Причина: '+condition


def block_reason(answer,evidence=None,current_text=''):
    """Return a safe owner-only reason, or None for content eligible to send.

    Handoff and semantic review are necessary signals, never sufficient alone.
    Unknown evidence contracts remain manual; this pilot deliberately permits
    fewer responses than the owner may approve after reading a draft.
    """
    if not isinstance(answer,dict):return _reason('ответ отсутствует или имеет неверный тип.')
    if type(answer.get('handoff')) is not bool:
        return _reason('флаг участия владельца отсутствует или имеет неверный тип.')
    if answer['handoff']:
        meta=evidence if isinstance(evidence,dict) else answer.get('evidence',{})
        detail=''
        if isinstance(meta,dict):
            detail=meta.get('diagnostic') or meta.get('handoff_reason') or ''
            if not detail and isinstance(meta.get('content_review'),dict) and meta['content_review'].get('supported') is False:
                detail=meta['content_review'].get('reason','')
        if not isinstance(detail,str):detail=''
        if detail:
            from .privacy import sensitive,scrub
            safe=scrub(detail)
            detection=re.sub(r'(?<!\w)(?:account_email|account_password|checkout_url|course_registration_email|'
                             r'required_fields|workflow_steps|provided_fields|account_ownership)(?!\w)',
                             '[имя поля]',safe,flags=re.I)
            # An explicitly named schema field is not its value. Only the
            # detection copy changes; actual assignments/JWTs stay redacted.
            detection=re.sub(r'(?i)(?:поле|поля|полей)\s+(?:пароля|токена|сессии)(?=[\s.,;:]|$)',
                             '[имя поля]',detection)
            detection=re.sub(r'(?i)(\[имя поля\]\s*[/,]\s*)(?:пароля|токена|сессии)'
                             r'(?=$|[.,;!?]|\s+(?:не|подтвержден|проверен|отсутств|нуж|треб))',
                             r'\1[имя поля]',detection)
            if sensitive(detection):
                stage=re.match(r'^Этап:\s*([А-Яа-яЁё ]{3,90})\.',safe)
                fields=sorted(set(re.findall(r'(?<!\w)(?:account_email|account_password|checkout_url|course_registration_email)(?!\w)',safe)))
                detail=(('Этап: '+stage[1]+'. ' if stage else '')
                        +('В диагностике указаны поля: '+', '.join(fields)+'. ' if fields else '')
                        +'Остальные детали скрыты: возможны данные доступа.')
            else:detail=safe[:1000]
        return _reason('ответ требует участия владельца; автоматическая отправка не разрешена.'
                       +(' Основание: '+detail if detail else ' Конкретная причина в сохранённых подтверждениях отсутствует.'))
    text=answer.get('reply')
    if not isinstance(text,str) or not text.strip():return _reason('текст ответа пуст или имеет неверный тип.')
    if len(text)>1800:return _reason('текст превышает проверенный лимит 1800 символов.')
    if evidence is None:evidence=answer.get('evidence')
    if not isinstance(evidence,dict):return _reason('сохранённые подтверждения ответа отсутствуют.')
    status=evidence.get('status')
    consultation=_local_consultation(text,evidence)
    if status in {kind+'_consultation' for kind in _CONSULTATION_KINDS} and not consultation:
        return _reason('кодовая консультация не имеет действующего подтверждения неизменённого текста; нужна новая подготовка.')
    if evidence.get('review_required') is True:
        return _reason('сохранено явное требование проверки содержания; действие по оформлению и проверка текста различаются.')
    if evidence.get('manual_secret_intake'):
        return _reason('предусмотрена ручная обработка данных доступа; требуется владелец.')
    if evidence.get('handoff_reason'):
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
    if sensitive(current):return _reason('обращение может содержать данные доступа; нужен ручной разбор.')
    if consultation and evidence['consultation_contract']['kind']=='workflow' and re.search(r'\[email скрыт\]|\[ссылка скрыта\]',current,re.I):
        return _reason('получены данные или ссылка для оформления; их обработку выполняет владелец.')
    # A short follow-up such as "да" may not repeat the financial/account
    # subject. Do not let a falsely optimistic review approve action claims or
    # collection of secrets merely because the current fragment looks benign.
    if not consultation and re.search(r'(?:пришлите|отправьте|передайте|сообщите|укажите)[^.!?\n]{0,120}'
                 r'(?:парол|токен|текст\s+сесси|данные\s+(?:для\s+)?входа|код\s+входа)',text,re.I):
        return _reason('ответ запрашивает данные доступа; сбор этих данных оставлен владельцу.')
    # Mentioning that no password is needed is not providing its value. Strip
    # only this exact code-owned public sentence from the detection copy;
    # secrets anywhere else, including the next sentence, remain detectable.
    from .privacy import redaction_categories
    if consultation and ({'access_secret','credential_line'} & set(redaction_categories(text))
            or re.search(r'(?i)(?:парол[ья]?|password|токен|token|session|cookie|cvv)\s*[:=]\s*\S',text)):
        return _reason('кодовая консультация содержит значение доступа или присваивание; автоматическая отправка запрещена.')
    detection=(_consultation_detection_copy(text) if consultation else
               re.sub(r'(?<!\w)Пароль от аккаунта для этого способа не нужен\.(?=\s|$)','',text))
    if sensitive(detection):return _reason('ответ может содержать данные доступа; автоматическая отправка запрещена.')
    if consultation:
        # Local renderers describe checked observations or the next question;
        # this does not execute payment/refund/activation or grant eligibility.
        if re.search(r'возврат[^.!?\n]{0,40}(?:одобрен|оформлен)|'
                     r'подписк[а-яё]*\s+(?:уже\s+)?активирован[а-яё]*|'
                     r'аккаунт\s+(?:уже\s+)?создан',text,re.I):
            return _reason('даже кодовая консультация не может утверждать выполнение возврата или оформления.')
        return None
    if re.search(r'(?:возврат[^.!?\n]{0,40}(?:одобрен|оформлен|подтвержд[её]н)|'
                 r'(?:одобрил|оформил|выполнил)[а-яё]*[^.!?\n]{0,25}возврат|'
                 r'подписк[а-яё]*\s+(?:уже\s+)?активирован[а-яё]*|'
                 r'аккаунт\s+(?:уже\s+)?создан|оплат[а-яё]*\s+(?:уже\s+)?(?:прошла|подтверждена))',text,re.I):
        return _reason('ответ утверждает завершение финансового действия или оформления; в пилоте нужен владелец.')
    from .order_status import is_status_request
    if is_status_request(current):
        return _reason('обращение касается проверки оплаты или заказа; в пилоте требуется ручная проверка.')
    if re.search(r'возврат|верн[а-яё]*\s+деньг|чарджб[еэ]к|chargeback|'
                 r'чек\b|квитанци|резервн[а-яё]*\s+код|одноразов[а-яё]*\s+код',current,re.I):
        return _reason('обращение касается возврата, платёжного подтверждения или кодов доступа; нужен владелец.')
    if re.search(r'бонусн[а-яё]*\s+подписк|куратор|из\s+(?:обучения|курса)|завод[а-яё]*\s+нейроагент',current,re.I):
        return _reason('доступность бонуса курса подтверждает владелец; автоматический ответ не разрешён.')
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
    if status not in ('checked','catalog_checked'):
        return _reason('статус подтверждений='+status_safe(status)+'; этот тип ответа не разрешён в автоматическом пилоте.')
    review=evidence.get('content_review')
    if not isinstance(review,dict) or review.get('supported') is not True:
        return _reason('нет сохранённого положительного результата проверки содержания.')
    projection=answer.get('feature_projection') or evidence.get('feature_projection')
    if isinstance(projection,dict) and projection.get('incomplete_target_ids'):
        return _reason('сравнение содержит неподтверждённые цели; требуется проверка владельца.')
    from .response_policy import public_question
    sources=evidence.get('sources')
    public=(status=='checked' or public_question(current) or bool(answer.get('source_ids')))
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
           'order_consultation','course_consultation','workflow_consultation','activation_consultation','external_consultation','creative_consultation'}
    return value if value in known else 'unknown'
