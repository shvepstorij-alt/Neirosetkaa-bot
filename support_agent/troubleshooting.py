"""Ask for evidence of a concrete problem, never diagnose from a complaint alone."""
import re


def requires_owner(text):
    return bool(re.search(r'возврат|верн(?:ите|уть)\s+деньги|(?:позови|позовите|свяжи|свяжите|поговорить\s+с)[^.!?\n]{0,30}(?:Александр|оператор|человек)',str(text),re.I))


def screenshot_request(text,has_images=False):
    if has_images:return None
    text=str(text or '')
    if re.search(r'закуп|поставщик|себестоим|18\s*\+|порно|эротическ',text,re.I):return None
    # General risk/feature questions are not an incident report.
    hypothetical=bool(re.search(r'что\s+(?:делать|будет)\s+(?:при|если)|бывают\s+ли|могут\s+ли|'
                               r'каков[а-я]*\s+риск|как\s+избежать',text,re.I))
    concrete=bool(re.search(r'у\s+меня[^.!?\n]{0,60}(?:ошибк|блок|не\s+работ|не\s+вход)|'
        r'(?:мой\s+)?аккаунт[^.!?\n]{0,35}заблок|заблокир(?:овали|ован)[а-я]*[^.!?\n]{0,35}аккаунт|'
        r'не\s+(?:могу|могу\s+никак)\s+(?:войти|зайти|активировать|открыть|оплатить)|'
        r'не\s+получается\s+(?:войти|зайти|активировать|оформить|оплатить)|'
        r'(?:выда[её]т|показывает|пишет|вылезает)\s+(?:мне\s+)?ошибк|'
        r'не\s+пускает|не\s+активируется|меня\s+заблокировали|'
        r'подписк[а-я]*\s+не\s+(?:появ|активир|работ)|'
        r'^\s*(?:ошибка|меня\s+заблокировали)[!.?\s]*$',text,re.I))
    if not concrete or hypothetical:return None
    message=('Пришлите, пожалуйста, скриншот ошибки целиком, чтобы были видны её текст и экран сервиса. '
             'Так будет понятнее, на каком этапе возникла проблема. '
             'Перед отправкой скройте пароли, коды входа и платёжные данные. '
             'Если скриншот сделать не получается, скопируйте текст ошибки.')
    handoff=requires_owner(text)
    return {'reply':message,'summary':'Сообщение о конкретной проблеме без изображения. Запрошен скриншот либо точный текст; причина, блокировка и состояние оплаты не установлены.',
            'handoff':handoff,'source_ids':[],
            'evidence':{'status':'clarification','sources':[]}}


def low_detail_reply(records,request=''):
    flagged=[r for r in records if r['severely_low_detail']]
    if not flagged:return None
    indices=', '.join(str(r['index']) for r in flagged)
    target='На этом фото' if len(records)==1 else 'На фото №'+indices
    return {'reply':target+' не хватает различимых деталей, чтобы надёжно прочитать текст. '
                'Пришлите, пожалуйста, более чёткий снимок или увеличенный фрагмент с ошибкой. '
                'Можно также скопировать её текст сюда. По этому изображению я не буду угадывать сообщение.',
            'summary':'Локально обнаружена крайне низкая детализация фото. Распознавание моделью не запускалось; текст и причина ошибки не установлены.',
            'handoff':requires_owner(request),'source_ids':[],
            'evidence':{'status':'visual_clarification','sources':[],
                'handoff_reason':'В сообщении есть запрос человека или решения по возврату.' if requires_owner(request) else '',
                'diagnostic':'Этап: локальная проверка детализации изображения. '+
                    '; '.join(f"фото={r['index']}, highpass={r['mean_highpass']}, доля границ={r['strong_edge_fraction']}" for r in flagged)
                    +'. Порог: highpass<0.02 и доля границ<0.0001. Это не универсальная проверка читаемости; нужен чёткий фрагмент.'}}


def _refund_approval_question(question):
    """Narrow authority question; never swallow mixed technical incidents."""
    text=str(question or '')
    return (bool(re.search(r'возврат|\brefund\b|верн(?:ут|ули|уть|[её]те)\s+деньги',text,re.I))
        and bool(re.search(r'одобрен|одобрил|одобрите|подтвержд[её]н|подтвердил|согласован|approved',text,re.I))
        and not re.search(r'ошибк|вход|блокиров|заблок|забан|не\s+могу|не\s+получается|'
                          r'не\s+работ|не\s+актив|не\s+появ|не\s+пришл|'
                          r'тариф|сравни|возможност|закуп|поставщик|себестоим|18\s*\+|порно|эротическ',text,re.I))


def refund_decision_reply(question,facts,has_images=False):
    """Apply owner policy without reading a picture as proof of refund authority.

    The latest-order list does not identify this refund, so it must not be used
    to assume a payment method or disclose an unrelated payment identifier.
    """
    if not has_images or not _refund_approval_question(question):return None
    return {'reply':'По одному скриншоту нельзя подтвердить одобрение возврата. '
                'Решение по возврату принимаю лично после ручной проверки заказа и обстоятельств. '
                'По фото возврат автоматически не оформляется.\n\n'
                'Для проверки напишите номер заказа, если ещё не указывали. '
                'Если оплачивали через FreeKassa, подойдёт номер платежа из квитанции. '
                'Пароли и данные карты присылать не нужно.',
            'summary':'Вопрос об одобрении возврата по изображению. Применена политика владельца: '
                'снимок не подтверждает решение; нужна ручная проверка заказа. '
                'Само изображение этим ответом не распознавалось. Одобрение или отказ не установлены.',
            'handoff':True,'source_ids':[],
            'evidence':{'status':'owner_policy','sources':[],
                'handoff_reason':'Решение о возврате принимает владелец после ручной проверки заказа.',
                'diagnostic':'Этап: применение политики решения по возврату. '
                    'Подтверждение решения по заказу отсутствует; изображение не устанавливает полномочия '
                    'и не заменяет ручную проверку владельца. Метод оплаты не предполагался. '
                    'Содержание изображения этим ответом не проверялось.'}}


def visual_failure(reason,request=''):
    from .privacy import scrub
    policy=refund_decision_reply(request,{},has_images=True)
    text=(policy['reply'] if policy else
          'Пока не получилось подготовить проверенный разбор по вашему фото. Чтобы не подсказать неверное действие, этот случай нужно проверить вручную. Повторно отправлять то же фото не нужно.')
    return {'reply':text,
            'summary':'Визуальный ответ не подтверждён; требуется ручная проверка изображения и ситуации.',
            'handoff':True,'source_ids':[],
            'evidence':{'status':'visual_review','sources':[],
                'diagnostic':'Этап: проверка ответа по изображению. '+scrub(reason)[:1500]}}
