"""Retain an explicit expired activation issue across a service clarification."""
import re
import time

_SERVICE=re.compile(r'\s*(?:(?:это|сервис|для)\s+)?(?:chat\s*gpt|чат\s*гпт|гпт|gpt)[.!?\s]*',re.I)
_ACTIVATION=re.compile(r'активир|активац',re.I)
_EXPIRED=re.compile(
    r'(?:кнопка|ссылка)\s+(?:уже\s+)?(?:истекла|закончилась|недействительна)\b|'
    r'действие\s+(?:кнопки|ссылки)\s+(?:уже\s+)?(?:истекло|закончилось)\b|'
    r'срок\s+действия\s+(?:кнопки|ссылки)\s+(?:уже\s+)?(?:ист[её]к|закончился)\b|'
    r'(?:истекла|закончилась)\s+(?:кнопка|ссылка)\b|'
    r'(?:ист[её]к|закончился)\s+срок\s+действия\s+(?:кнопки|ссылки)\b',re.I)
_NEGATED_EXPIRY=re.compile(r'\bне\s+(?:ист[её]к|законч|истеч|является\s+недействител)',re.I)
_CLARIFICATION=re.compile(r'(?:какой|какого|уточните|назовите|о каком)[^.!?\n]{0,60}сервис',re.I)


def reply(current,previous_client,previous_assistant,now=None):
    """Handle only an immediate answer to our fresh service question."""
    if not _SERVICE.fullmatch(str(current or '')):return None
    if not previous_client or not previous_assistant:return None
    if previous_assistant.get('role', 'assistant') != 'assistant':return None
    now=time.time() if now is None else now
    for row in (previous_client,previous_assistant):
        at=row.get('at')
        if type(at) not in (int,float) or not 0<=now-at<=14400:return None
    if not previous_client.get('mid',0)<previous_assistant.get('mid',0):return None
    if previous_client['at']>previous_assistant['at']:return None
    source=str(previous_client.get('text') or '')
    from .privacy import sensitive
    if sensitive(source):return None
    if not _ACTIVATION.search(source) or not _EXPIRED.search(source) or _NEGATED_EXPIRY.search(source):return None
    from .catalog_context import _families, _words
    if _families(_words(source)) - {'chatgpt'}:return None
    if not _CLARIFICATION.search(str(previous_assistant.get('text') or '')):return None
    body=('Понял вопрос об истечении действия при активации ChatGPT. '
          'Напишите, пожалуйста, номер заказа и что показывает бот. Если есть ошибка, пришлите её текст. '
          'Пароли, коды и платёжные данные присылать не нужно.')
    from .auto_policy import consultation_evidence
    return {'reply':body,'summary':'Вопрос об истечении действия при активации; запрошены данные для проверки, факт истечения, статус заказа и причина не установлены.',
            'handoff':False,'source_ids':[],
            'evidence':consultation_evidence(body,'activation',diagnostic='Этап: уточнение вопроса об истечении действия при активации. Сервис указан клиентом; факт истечения, статус заказа и причина не проверены.')}
