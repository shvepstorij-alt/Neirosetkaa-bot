"""Officially grounded, narrow Suno creation guidance."""
import re

SOURCE_AUDIO='https://help.suno.com/en/articles/6141569'
SOURCE_LYRICS='https://help.suno.com/en/articles/2415873'


def reply(current):
    text=str(current or '')
    if not re.search(r'\bsuno\b|суно',text,re.I):
        return None
    if re.search(r'сколько|цен[аы]|стоим|тариф|план|оплат|куп|оформ|активир',text,re.I):
        return None
    audio=bool(re.search(r'мелоди|голос|аудио|запис|аранжир|минусов|бит|музык',text,re.I))
    lyrics=bool(re.search(r'текст|слов[ао]|лирик|lyrics',text,re.I))
    if not (audio or lyrics):
        return None
    body=('Да, в Suno можно начать со своего аудио: записать фрагмент или загрузить файл, '
          'а затем продолжить создание на его основе. Свой текст можно добавить в режиме Custom.\n\n'
          'Откройте Create → загрузите или запишите аудио → при необходимости выберите Custom, добавьте текст и описание стиля → Create. '
          'Для результата лучше пробовать на коротком фрагменте и редактировать запрос: качество и совпадение с замыслом не гарантируются.\n\n'
          'Используйте только аудио и текст, на которые у вас есть права.')
    return {'reply':body,
            'summary':'Подтверждённые возможности Suno: работа со своим аудио и текстом без обещаний качества, цены или тарифа.',
            'handoff':False,'source_ids':[],
            'evidence':{'status':'official_guidance','sources':[SOURCE_AUDIO,SOURCE_LYRICS],
                        'diagnostic':'Этап: создание музыки в Suno. Архивная цепочка 917901→917924 подтверждает тему вопроса и ответ владельца; официальная документация Suno проверена 11.11.2026: загрузка/запись аудио и собственный текст поддерживаются. Тарифы, лимиты, качество и результат не утверждались.'}}
