"""Narrow, non-transactional CapCut captions guidance."""
import re


SOURCE='https://www.capcut.com/help/how-to-recognise-subtitles'
_BLOCK=r'сколько|цен[аы]|стоим|тариф|план|оплат|куп|оформ|активир|лимит|подпис|аккаунт|войти|логин|парол|код|гарант|сравн|\bvs\b|регион|vpn|впн|доступ|ошиб|блокиров|возврат|ваш\s+бот|в\s+боте|каталог|кредит'


def reply(current):
    text=str(current or '')
    if re.search(_BLOCK,text,re.I):
        return None
    capcut=bool(re.search(r'\bcap\s*cut\b|капкат|капкут',text,re.I))
    short_video=bool(re.search(r'рилс|reels|коротк[а-яё]*\s+видео|ролик|видео',text,re.I))
    captions=bool(re.search(r'субтитр|подпис[ьи]|текст.*(?:видео|ролик)|видео.*текст',text,re.I))
    if not (capcut or (short_video and captions)):
        return None
    if not (captions or re.search(r'монтаж|оформ|уже\s+(?:снят|есть).*?(?:видео|ролик)',text,re.I)):
        return None
    if re.search(r'с\s+нуля|полностью[^.!?\n]{0,30}(?:ии|нейросет)|генерир[^.!?\n]{0,30}(?:видео|ролик)',text,re.I):
        return None
    body=('Для уже снятого короткого ролика с субтитрами можно попробовать CapCut. Откройте проект, '
          'найдите раздел Text или Captions → Auto Captions, выберите язык и после генерации вручную проверьте текст и тайминги.\n\n'
          'Названия кнопок и доступность функций могут отличаться на телефоне, компьютере и в веб-версии. Автосубтитры лучше перепроверять, особенно при шуме или быстрой речи.')
    return {'reply':body,'summary':'Узкая навигация CapCut для субтитров в уже снятом коротком видео без цен, аккаунтов и обещания результата.',
            'handoff':False,'source_ids':[],
            'evidence':{'status':'official_guidance','sources':[SOURCE],
                        'diagnostic':'Этап: монтаж уже снятого короткого видео и субтитры. Архивные цепочки 905941→905973→905976→905977 и 942932→943011→943021 подтверждают предложение CapCut как варианта; готовый результат клиент не подтвердил. Официальная справка CapCut проверена 11.11.2026: Auto Captions поддерживается с ручной правкой, а доступность зависит от платформы. Тарифы, версия и качество не утверждались.'}}
