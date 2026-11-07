"""Narrow field-name permission for the accepted free-answer review path.

Only Service's post-check_answer path calls attach_reviewed_workflow. A model's
review flag cannot create the process-local proof. This is not a delivery or
action permission: auto_policy still checks the ordinary reviewed-answer gates.
"""
import hashlib
import hmac
import json
import re
import secrets

_KEY=secrets.token_bytes(32)
_CONTRACT='reviewed_workflow_contract'
_FIELDS={'version','products','detection','source_ids','proof'}
_SECURITY_FIELDS=('status','content_review','review_required','manual_secret_intake','handoff_reason',
                  'sources','checked_at','owner_action_required','new_account_stage',
                  'feature_projection','response_decision','consultation_contract')
_TERMS=re.compile(r'(?<!\w)(парол[а-яё]*|почт[а-яё]*|e-?mail)(?!\w)',re.I)
_REQUIREMENT=re.compile(r'пришлите|присылайте|отправьте|отправить|отправляйте|передайте|передать|'
                        r'сообщите|предоставьте|нуж(?:ен|на|ны|но)|потребуются|потребуется|'
                        r'понадобятся|понадобится|необходим[а-яё]*|требуются|требуется',re.I)
_WORDS=set(('после до согласования согласуйте оплаты оплатить выбора выбранного тарифа тариф подписки '
            'для оформления активации подключения пришлите присылайте отправьте отправить отправляйте '
            'передайте передать сообщите предоставьте нужны нужен нужна нужно потребуется потребуются '
            'понадобится понадобятся необходимо необходима необходимы требуется требуются '
            'сюда здесь в эту этой же переписку переписке лично александру владельцу '
            'почта почту почты email mail пароль пароля паролю паролем от аккаунта аккаунту '
            'адрес электронной электронную электронный электронного вашей вашего e '
            'дальнейшей настройки настройку подключения настройке дальнейшего '
            'вашего вашему ваш свой своего своему существующего текущего сервиса и а также '
            'пожалуйста будет будут чтобы подключить оформить подписку').split())


def _canonical(text):
    from .conversation_style import decorate_reply
    return decorate_reply(str(text),{'continuing':True}).replace('\r\n','\n').strip()


def _payload(answer,evidence,contract):
    return json.dumps({'reply':_canonical(answer['reply']),'handoff':answer.get('handoff'),
                       'source_ids':contract['source_ids'],
                       'evidence':{k:evidence[k] for k in _SECURITY_FIELDS if k in evidence},
                       'contract':{k:v for k,v in contract.items() if k!='proof'}},
                      ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def _approved_fields(row):
    from .workflow_policy import policies_for
    policies=policies_for({'requested_catalog':[row]})
    if policies:
        fields=set()
        for policy in policies:
            if policy.get('source')=='owner_confirmed':fields.update(policy.get('required_fields',[]))
        return fields
    return set(row.get('required_fields',[])) if row.get('required_fields_status')=='available' else set()


def _prepare(text,facts):
    from .catalog_context import service_mentions,_words
    from .answer_check import _code_credential_issue
    from .privacy import redaction_categories
    rows=[r for r in facts.get('requested_catalog',[]) if isinstance(r,dict)]
    if _code_credential_issue(text,facts):return None
    if set(redaction_categories(text)) & {'access_secret','credential_line','email','voucher_code'}:return None
    # Such fields are never enabled by this contract, even if an upstream row
    # erroneously advertises them. No payload/placeholder becomes a field name.
    original=text
    if facts.get('public_research',{}).get('sources'):
        from .public_metrics import detection_copy
        text=detection_copy(text)
    if re.search(r'токен|token|сесси|session|cookie|cvv|'
                 r'код(?:ы)?\s+(?:входа|подтверждения)|(?:одноразов|резервн)[а-яё]*\s+код|'
                 r'\botp\b|\[[^\]]*(?:скры|token|код|парол)',text,re.I):return None
    if not rows and _TERMS.search(text):return None
    fragments=re.split(r'((?<=[.!?])\s+|\n+)',text)
    # Removing a field name must not hide the association with an unlabelled
    # pure-letter secret on another line or after the requirement sentence.
    if any(re.fullmatch(r'\s*[A-Za-z0-9!@$%^&*_.+\-]+\s*',part)
           for part in fragments if part.strip()):return None
    if re.search(r'\b(?:вот|значение|данные)\s+[A-Za-z0-9!@$%^&*_.+\-]+',text,re.I):return None
    catalog=facts.get('catalog',rows)
    identities={id(row):set(service_mentions(str(row.get('service',''))+' '+str(row.get('service_key','')),catalog)) for row in rows}
    permitted={id(row):_approved_fields(row) for row in rows}
    active=rows;products={};out=[];changed=text!=original
    for part in fragments:
        mentions=set(service_mentions(part,catalog))
        matched=[row for row in rows if identities[id(row)] & mentions]
        if mentions:active=matched
        terms=list(_TERMS.finditer(part))
        if not terms:
            out.append(part);continue
        if not _REQUIREMENT.search(part):return None
        fields={'account_password' if term[0].casefold().startswith('парол') else 'account_email' for term in terms}
        if not active or any(fields-permitted[id(row)] for row in active):return None
        # Validate all words in the requirement clause, including words after
        # «пароль». Unknown alphabetic values cannot disappear with the label.
        public_names=set()
        from .catalog_context import ALIASES,_families
        for row in active:
            for key in ('service','service_key'):public_names.update(_words(row.get(key,'')))
            for family in _families(_words(str(row.get('service',''))+' '+str(row.get('service_key','')))):
                for alias in ALIASES[family]:public_names.update(_words(alias))
        if set(_words(part)) - _WORDS - public_names:return None
        if re.search(r'парол[а-яё]*\s+(?!(?:от|для|и|сюда|здесь|нужен|нужны|нужно|потребуется|требуется|будет)\b)\w+',part,re.I):return None
        if re.search(r'\d|[={}\[\]"\'`@]|(?:парол[а-яё]*|почт[а-яё]*|e-?mail)\s*:',part,re.I):return None
        out.append(_TERMS.sub('[поле оформления]',part));changed=True
        for row in active:
            key=(str(row.get('service_key','')),str(row.get('service','')),str(row.get('plan','')))
            products[key]={'service_key':key[0],'service':key[1],'plan':key[2],
                           'fields':sorted(fields|set(products.get(key,{}).get('fields',[])))}
    return {'products':list(products.values()),'detection':''.join(out)} if changed else None


def attach_reviewed_workflow(answer,facts):
    """Return a copy; call only after the actual positive check_answer result.

    Sources, status and review must already be attached. No handoff flag is
    changed, and an unprovable requirement simply receives no exemption.
    """
    if not isinstance(answer,dict) or not isinstance(answer.get('evidence'),dict):return answer
    result=dict(answer);evidence=dict(answer['evidence']);evidence.pop(_CONTRACT,None)
    result['evidence']=evidence
    if (answer.get('handoff') is not False or not isinstance(answer.get('reply'),str)
            or evidence.get('status') not in ('checked','catalog_checked')
            or evidence.get('owner_action_required') is True
            or not isinstance(evidence.get('content_review'),dict)
            or evidence['content_review'].get('supported') is not True):return result
    prepared=_prepare(_canonical(answer['reply']),facts)
    if not prepared:return result
    contract={'version':1,'source_ids':answer.get('source_ids',[]),**prepared}
    try:contract['proof']=hmac.new(_KEY,_payload(result,evidence,contract),hashlib.sha256).hexdigest()
    except (TypeError,ValueError):return result
    evidence[_CONTRACT]=contract
    return result


def validate_reviewed_workflow(answer,evidence=None):
    if not isinstance(answer,dict):return None
    evidence=answer.get('evidence') if evidence is None else evidence
    if not isinstance(evidence,dict):return None
    contract=evidence.get(_CONTRACT)
    if not isinstance(contract,dict) or set(contract)!=_FIELDS or contract.get('version')!=1:return None
    # Stored drafts reconstruct reply/handoff separately; source IDs live in
    # evidence. Bind them in the sealed contract and reject explicit changes.
    if 'source_ids' in answer and answer['source_ids']!=contract['source_ids']:return None
    proof=contract.get('proof')
    if not isinstance(proof,str) or not re.fullmatch(r'[0-9a-f]{64}',proof):return None
    try:expected=hmac.new(_KEY,_payload(answer,evidence,contract),hashlib.sha256).hexdigest()
    except (TypeError,ValueError,KeyError):return None
    return contract if hmac.compare_digest(proof,expected) else None


def detection_copy(answer,evidence=None):
    contract=validate_reviewed_workflow(answer,evidence)
    return contract['detection'] if contract else None
