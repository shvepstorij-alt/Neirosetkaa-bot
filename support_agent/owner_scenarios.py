"""Short owner-confirmed answers that must not wait for the model to invent them."""
import re

from .handoff import handoff_text

NAVSEX = ('Промокод NAVSEX даёт скидку 10%. Других скидок нет. '
          'Введите его при оплате заказа в боте)')
REFUND = ('Напишите номер заказа FreeKassa и причину возврата или какая ошибка произошла. '
          'Возврат заранее не обещаю.')
COINS = ('Монетки — отдельный баланс. Ими можно оплатить подписку в магазине.\n\n'
         'Подтверждённый способ получить их: 10% с первой покупки каждого приглашённого друга. '
         'Это не кредиты для генераций. Другие способы начисления здесь не называю.')
GIVEAWAY = 'Активных розыгрышей сейчас нет.'
GENERATION = ('Фото и видео можно сделать в боте: «Изображение», «Видео», «Редактировать...» и «Анимировать...».\n\n'
              'Стоимость и модель смотрите на экране перед запуском. Если нужен отдельный сервис, напишите задачу, подберу вариант)')


def _human(text):
    return bool(re.search(
        r'(?:позовите|позови|соедините|подключите)[^.!?\n]{0,45}(?:человек|оператор|александр)|'
        r'(?:хочу|можно)[^.!?\n]{0,25}(?:поговорить|общаться)[^.!?\n]{0,25}(?:с\s+человеком|с\s+александром)|'
        r'(?:хочу|нужен|нужна)[^.!?\n]{0,25}(?:оператор|жив[а-яё]*\s+человек)',
        text, re.I))


def _promo(text):
    if re.search(r'без\s+скидк|не\s+нужн\w*\s+скидк', text, re.I):
        return False
    return bool(re.search(
        r'промокод|(?:есть|дай(?:те)?|хочу|нужн\w*|можно|кака[яе])\s+[^.!?\n]{0,24}скидк|скидк\w*\s*\?',
        text, re.I))


def _refund(text):
    return bool(re.search(r'возврат|верните\s+деньги|вернуть\s+деньги', text, re.I))


def _coins(text):
    return bool(re.search(r'монет', text, re.I) and re.search(
        r'как|что\s+так|работа|получ|потрат|спис|потратить|копить', text, re.I))


def _giveaway(text):
    return bool(re.search(r'розыгрыш', text, re.I))


def _generation(text):
    if re.search(r'посовету|какой\s+сервис|рекоменд', text, re.I):
        return False
    return bool(re.search(
        r'(?:как|где)[^.!?\n]{0,40}(?:сгенерир|создать|редактир)[^.!?\n]{0,40}(?:фото|видео|изображ)',
        text, re.I))


def _pack(reply, summary, diagnostic):
    return {'reply': reply, 'summary': summary, 'handoff': False, 'source_ids': [],
            'evidence': {'status': 'clarification', 'sources': [], 'diagnostic': diagnostic}}


def reply(text, now=None):
    """One owner rule per message. Mixed shop purchases stay on the normal path."""
    text = str(text or '')
    if _human(text) and not re.search(r'тариф|оплат|активир|установ', text, re.I):
        return _pack(handoff_text(now),
                     'Клиент просит человека. Назван срок и сохранена готовность помочь с другими вопросами.',
                     'Этап: передача Александру. Режим чата не выключен.')
    if _promo(text):
        return _pack(NAVSEX, 'Клиент прямо просит промокод или скидку. Выдан только NAVSEX 10%.',
                     'Этап: промокод. Другие скидки не предлагались.')
    if _refund(text) and not re.search(r'как\s+(?:оформить|купить|установить)', text, re.I):
        return _pack(REFUND, 'Собраны номер FreeKassa и причина. Возврат не обещан.',
                     'Этап: возврат. Обещание возврата не давалось.')
    if _coins(text):
        return _pack(COINS, 'Объяснены подтверждённые правила монеток. Неподтверждённые способы не добавлены.',
                     'Этап: монетки. Источник: реферальное начисление и оплата подписки монетками.')
    if _giveaway(text):
        return _pack(GIVEAWAY, 'Сообщено, что активных розыгрышей нет.',
                     'Этап: розыгрыши. Новые условия не выдуманы.')
    if _generation(text):
        return _pack(GENERATION, 'Подсказан путь генерации в боте. Отдельный сервис не назначался.',
                     'Этап: генерация в боте. Названия разделов взяты из меню магазина.')
    return None
