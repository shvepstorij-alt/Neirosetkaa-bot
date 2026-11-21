"""Short owner-confirmed answers that must not wait for the model to invent them."""
import re

from .handoff import handoff_text

NAVSEX = ('Промокоды NAVSEX и CHATIK10 дают скидку 10%. Других скидок нет. '
          'Введите один из них при оплате заказа в боте)')
REFUND = ('Напишите номер заказа FreeKassa и причину возврата или какая ошибка произошла. '
          'Возврат заранее не обещаю.')
COINS = ('Монетки начисляются только за приглашённых друзей: 10% с первой покупки каждого друга. '
         'Реферальную ссылку возьмите в @neirosetkaa_bot: главное меню → «Мой профиль» → «Пригласить друга». '
         'Потратить монетки можно на оплату подписки. Других способов получить или потратить монетки нет.')
REFERRAL = ('Да, реферальная программа есть. Ссылку возьмите в @neirosetkaa_bot: главное меню → «Мой профиль» → «Пригласить друга». '
            'С первой покупки каждого приглашённого вы получаете 10% монетками, ими можно оплатить подписку в боте.')
GIVEAWAY = ('Сейчас розыгрышей нет, но к Новому году могут появиться. '
            'Следите за нашим каналом: https://t.me/AleksandrOii')
GENERATION = ('Фото и видео можно сделать в боте: «Изображение», «Видео», «Редактировать...» и «Анимировать...».\n\n'
              'Стоимость и модель смотрите на экране перед запуском. Если нужен отдельный сервис, напишите задачу, подберу вариант)')


# Owner 12.11.2026: one honest answer to «бот или человек», «девушка или парень».
WHO = 'Я ИИ-помощник Александра. Подскажу по сервисам, тарифам и оплате; сложные вопросы Александр решает сам.'


def _who(text):
    return bool(re.search(
        r'(?:вы|ты|это)\s+(?:точно\s+)?(?:бот|робот|человек|живой|нейросеть|ии\b|ai\b)|бот\s+или\s+человек|человек\s+или\s+бот|'
        r'(?:вы|ты)\s+(?:девушка|парень|мужчина|женщина)|с\s+кем\s+я\s+(?:говорю|общаюсь|переписываюсь)|'
        r'(?:я\s+)?(?:говорю|общаюсь)\s+с\s+(?:ботом|человеком|роботом)', text, re.I))


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


def _referral(text):
    return bool(re.search(r'реферал|рефк|партн[её]рск\w*\s+(?:программ|ссылк)|пригласи\w*\s+друз|пригласить\s+друга|за\s+друзей', text, re.I))


_WISH = re.compile(r'(?i)(хорошего|отличного|прекрасного|приятного|доброго)\s+(дня|вечера|утра|выходных|отдыха)|спокойной\s+ночи|доброй\s+ночи')


def closing(text):
    """Owner 20.11.2026: answer a wish with the same wish; otherwise «Рад помочь, обращайтесь!»."""
    wish = _WISH.search(str(text or ''))
    if wish:
        return 'Рад помочь! И вам ' + wish.group(0).casefold() + '!'
    return 'Рад помочь, обращайтесь!'


# Owner 21.11.2026: the assistant never claims to be Alexander and never promises when he answers.
HUMAN = 'Я ИИ-помощник Александра. Александр ответит при первой возможности, а пока я могу помочь — что подсказать?'
STEP_PING = 'Я ИИ-помощник Александра. Александр продолжит здесь при первой возможности.'
_CALLS_OWNER = re.compile(r'(?i)^\W*(?:александр|саша|алекс)\W*(?:(?:вы|ты)\s+(?:тут|здесь|на\s+связи)|на\s+связи|ау)?\W*$')
_HERE = re.compile(r'(?i)(?:вы|ты)\s+(?:тут|здесь|на\s+связи|на\s+месте|онлайн)\s*\??\W*$|^\W*(?:ау|алло|есть\s+кто)\W*$')
# «спасибо, пошла искать», «спс, щас попробую» — the client is still doing the step, not closing the talk.
_STILL_DOING = re.compile(r'(?i)пош[её]л|пошла|пойду|сейчас|щас|попроб|поищ|ищу|посмотр|жду|подожд|найду|скину|пришлю|'
                          r'отправлю|напишу|проверю|сделаю|займусь|ок\s*,?\s*(?:понял|поняла)|понял|поняла')
_STEP_CLOSED = re.compile(r'(?i)подписка\s+успешно\s+активирована|обращайтесь|хорошего\s+(?:дня|вечера)|рад\s+помочь')
_DONE = re.compile(r'(?i)вс[её]\s+(?:хорошо|получилось|работает|ок)|подписка\s+(?:есть|появилась)|получилось|заработал|работает')


def mid_step(context):
    """Alexander wrote here in the last 12 h and his step is not closed («подписка активирована», «обращайтесь»)."""
    if not isinstance(context, dict) or not context.get('history'):
        return False
    try:
        from .turn_understanding import current_step, ongoing_dialog
        if not ongoing_dialog(context):
            return False
        step = current_step(context.get('history'))
    except Exception:
        return False
    if not step:
        return False
    last = (step.get('owner_messages') or [''])[-1]
    return not _STEP_CLOSED.search(last) and not any(_STEP_CLOSED.search(t) for t in step.get('assistant_after') or [])


def quick_reply(text, now=None, context=None):
    """Owner rules that answer the whole short message: promo, coins, referral, a human.

    Owner 21.11.2026: history-aware — inside Alexander's open step a «спасибо, пошла искать» is not a
    closing and «Александр?)» is answered honestly as the assistant, never as Alexander.
    """
    text = str(text or '')
    if not text.strip() or len(text) > 220:
        return None
    step_open = mid_step(context)
    if _human(text) and not re.search(r'тариф|оплат|активир|установ', text, re.I):
        return _pack(STEP_PING if step_open else HUMAN, 'Клиент просит человека.',
                     'Этап: передача Александру. Режим чата не выключен.')
    if len(text) <= 40 and _CALLS_OWNER.search(text.strip()):
        return _pack(STEP_PING if step_open else HUMAN, 'Клиент зовёт Александра.', 'Этап: кто отвечает. Ответ владельца.')
    if re.fullmatch(r'(?i)\W*(?:вот\s+)?(?:чек|квитанци\w*|скрин\w*\s+оплат\w*)(?:\s+(?:об?|по|за)\s+оплат\w*|\s+оплат\w*)?\W*', text.strip()):
        # Owner 17.11.2026: a receipt sent after the order is acknowledged, not questioned.
        return _pack('Спасибо, чек получил. Александр проверит оплату и продолжит здесь.',
                     'Клиент прислал чек об оплате.', 'Этап: чек об оплате. Ответ владельца.')
    if len(text) <= 60 and _HERE.search(text.strip()):
        if step_open:
            return _pack(STEP_PING, 'Клиент спрашивает, на связи ли Александр, посреди его шага.',
                         'Этап: шаг Александра. Ответ владельца.')
        return _pack('Я ИИ-помощник Александра, на связи. Чем помочь?', 'Клиент спрашивает, на связи ли Александр.',
                     'Этап: кто отвечает. Ответ владельца.')
    if len(text) <= 160 and re.search(r'(?i)обманыва|(?:деньги|средства)\s+(?:списал|ушли|сняли)|списал\w*\s+(?:деньги|средства)', text) \
            and not re.search(r'(?i)возврат|верн\w+\s+(?:мне\s+)?(?:деньги|средства)|заказ\w*\s*#?\s*\d', text):
        # Owner 13.11.2026: ask what was charged and the order number; a refund demand still goes to him.
        return _pack('Уточните, пожалуйста, за что именно списались средства, и пришлите номер заказа из бота. '
                     'После оплаты проверьте бота @neirosetkaa_bot: туда обычно приходит инструкция, что делать дальше.',
                     'Клиент пишет, что деньги списались.', 'Этап: уточнение по списанию. Ответ владельца.')
    thanks_ok = not _STILL_DOING.search(text) and (not step_open or _DONE.search(text))
    if thanks_ok and len(text) <= 70 and re.search(r'(?i)^\W*(?:спасибо|благодарю|спс|пасиб\w*|ура|отлично|супер|всё\s+получилось|все\s+получилось|получилось|работает|заработало|прошло)\W*(?:большое|огромное|вам|\W)*(?:всё|все)?\W*(?:получилось|работает|прошло)?\W*$', text):
        return _pack(closing(text), 'Клиент благодарит.', 'Этап: благодарность. Ответ владельца.')
    if (thanks_ok and len(text) <= 90 and re.search(r'(?i)спасибо|благодар|спс\b|пасиб', text)
            and not re.search(r'(?i)\?|\bне\b|ошибк|проблем|как\s|можно|подскаж|помогите|а\s+ещ', text)):
        # Owner 20.11.2026: thanks with a wish or «всё получилось» — a short closing, the wish mirrored.
        return _pack(closing(text), 'Клиент благодарит.', 'Этап: благодарность. Ответ владельца.')
    if _who(text) and len(text) <= 80:
        return _pack(WHO, 'Клиент спрашивает, бот это или человек.', 'Этап: кто отвечает. Ответ владельца.')
    if _promo(text):
        return _pack(NAVSEX, 'Клиент прямо просит промокод или скидку. Выдан только NAVSEX 10%.',
                     'Этап: промокод. Другие скидки не предлагались.')
    if _giveaway(text):
        return _pack(GIVEAWAY, 'Розыгрышей нет; канал для новостей.', 'Этап: розыгрыш.')
    if _referral(text):
        return _pack(REFERRAL, 'Объяснена реферальная программа: ссылка в «Мой профиль», 10% с первой покупки монетками.',
                     'Этап: реферальная программа.')
    if _coins(text):
        return _pack(COINS, 'Объяснены подтверждённые правила монеток.', 'Этап: монетки.')
    return None


def _giveaway(text):
    return bool(re.search(r'розыгрыш|конкурс', text, re.I))


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
