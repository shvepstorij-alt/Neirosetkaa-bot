"""Narrow guidance for a presentation draft in Gamma."""
import re


SOURCE='https://help.gamma.app/en/articles/7838093-how-do-i-create-a-new-presentation-document-or-webpage-in-gamma'
_BLOCK=r'сколько|цен[аы]|стоим|тариф|план|оплат|куп|активир|лимит|подпис|гарант|сравн|\bvs\b|регион|vpn|впн|доступ|аккаунт|войти|логин|парол|код|ошиб|блокиров|возврат|ваш\s+бот|в\s+боте|каталог|кредит'


def reply(current):
    text=str(current or '')
    if re.search(_BLOCK,text,re.I):
        return None
    if re.search(r'оформ[^.!?\n]{0,35}(?:тариф|подпис|план)|(?:тариф|подпис|план)[^.!?\n]{0,35}оформ',text,re.I):
        return None
    named=bool(re.search(r'\bgamma\b|гамма',text,re.I))
    presentation=bool(re.search(r'презентац|слайд',text,re.I))
    structure=bool(re.search(r'собра[тьн]|структур|оформ|текст|информац|документ',text,re.I))
    if not (named and presentation) and not (presentation and structure):
        return None
    body=('Для первого черновика презентации можно попробовать Gamma. Опишите тему и аудиторию, '
          'а если текст уже есть — вставьте его в сервис: Gamma может собрать структуру и оформить материал.\n\n'
          'Перед отправкой проверьте факты, цифры, ссылки и итоговый дизайн: черновик лучше доработать под свою задачу.')
    return {'reply':body,'summary':'Узкая навигация Gamma для черновой презентации по теме или своему тексту без тарифов и обещаний качества.',
            'handoff':False,'source_ids':[],
            'evidence':{'status':'official_guidance','sources':[SOURCE],
                        'diagnostic':'Этап: создание черновой презентации. Архивная цепочка 909409→909501 подтверждает рекомендацию Gamma для сбора информации и оформления текстов; результата клиент не подтвердил. Официальная справка Gamma проверена 11.11.2026: доступны Generate для темы и Paste для своего текста. Цены, лимиты и качество не утверждались.'}}
