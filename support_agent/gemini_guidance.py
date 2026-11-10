"""Narrow, current Gemini image-creation guidance.

This is intentionally not a tariff, model-choice, limit, or comparison reply.
"""
import re


SOURCE='https://support.google.com/gemini/answer/14286560'


def reply(current):
    text=str(current or '')
    if not re.search(r'\bgemini\b|джемини|гемини',text,re.I):
        return None
    # These questions need a current catalogue, account-specific check, or a
    # comparison dialogue; do not let a generic feature answer override them.
    if re.search(r'сколько|цен[аы]|стоим|тариф|план|оплат|куп|оформ|активир|лимит|подпис|лучше|сравн|против|\bvs\b|chat\s*gpt|чат\s*гпт|модель|banana',text,re.I):
        return None
    images=bool(re.search(r'фото|фотк|картин|изображени|рисунк|нарис|сгенерир|созда[тй]|редактир',text,re.I))
    if not images:
        return None
    body=('Да, Gemini умеет создавать и редактировать изображения по текстовому запросу. '
          'Можно также загрузить изображение и попросить изменить его.\n\n'
          'Откройте чат Gemini → опишите, что хотите получить, '
          'либо прикрепите исходное изображение и напишите, что изменить.\n\n'
          'Доступность функции зависит от страны, языка и типа аккаунта. Используйте только изображения, '
          'на которые у вас есть права.')
    return {'reply':body,
            'summary':'Подтверждённая навигация Gemini для создания и редактирования изображений без обещаний сравнения, тарифа или лимитов.',
            'handoff':False,'source_ids':[],
            'evidence':{'status':'official_guidance','sources':[SOURCE],
                        'diagnostic':'Этап: создание/редактирование изображений в Gemini. Архивная цепочка 941137–941138→941245, с подтверждением понимания 941261, подтверждает потребность клиента в работе с фото. Официальная справка Google проверена 11.11.2026: Gemini Apps поддерживает создание и редактирование изображений, при этом доступность зависит от поддерживаемых стран, языков и типа аккаунта. Тарифы, лимиты, модели и сравнение с ChatGPT не утверждались.'}}
