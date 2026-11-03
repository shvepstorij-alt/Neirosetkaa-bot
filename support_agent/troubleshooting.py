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


def visual_failure(reason,request=''):
    from .privacy import scrub
    policy_question=(bool(re.search(r'возврат',request,re.I)) and
        bool(re.search(r'одобрен|подтвержд[её]н|согласован|approved',request,re.I)) and
        not re.search(r'ошибк|вход|блокиров|не\s+могу|не\s+получается',request,re.I))
    text=('По одному скриншоту нельзя подтвердить одобрение возврата. Решение по возврату принимаю после ручной проверки заказа и обстоятельств. По фото возврат автоматически не оформляется.' if policy_question else
          'Пока не получилось подготовить проверенный разбор по вашему фото. Чтобы не подсказать неверное действие, этот случай нужно проверить вручную. Повторно отправлять то же фото не нужно.')
    return {'reply':text,
            'summary':'Визуальный ответ не подтверждён; требуется ручная проверка изображения и ситуации.',
            'handoff':True,'source_ids':[],
            'evidence':{'status':'visual_review','sources':[],
                'diagnostic':'Этап: проверка ответа по изображению. '+scrub(reason)[:1500]}}
