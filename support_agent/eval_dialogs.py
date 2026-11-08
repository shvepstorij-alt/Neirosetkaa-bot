"""Offline stress check, plus an optional live model pass.

Run from the repository root:

    python -m support_agent.eval_dialogs

The offline pass does not need a key and checks outcomes. A live key is optional
and is never printed.
"""
import asyncio
import os
import sys


FIXTURES = (
    'Как пополнить App Store?',
    'У меня Россия\nХочу на 3000',
    'Назовите все номиналы',
    'Как оформить подписку на Ловабл',
    'Что ты можешь',
    'Тариф про',
    'Тариф про на месяц',
    'У вас можно купить телеграм подписку?',
    'Оплатил, но не могу активировать',
    'Нужно оформить бонусную подписку, я купил обучение',
    'Верните деньги, иначе я напишу жалобу',
    'Я спрашиваю то же самое ещё раз, вы так и не ответили',
    'Сколько сейчас стоит подписка',
)


def main():
    from support_agent.stress_suite import run_suite
    report = run_suite()
    print('Офлайн-диалоги: %s, проверено реплик: %s, прошли: %s, не прошли: %s.' % (
        report['cases'], report['checked'], report['passed'], len(report['failed'])))
    print('До правки по темам владельца и повторам не проходили реплик: %s.' % report['before_failed_turns'])
    for item in report['failed']:
        print('-', item['id'], ', '.join(item['errors']))
    if report['failed']:
        return 1
    key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not key:
        print('Ключ API не задан, живой прогон модели пропущен. Примеры для него:')
        for line in FIXTURES:
            print('-', line.replace('\n', ' / '))
        return 0
    try:
        import aiohttp
    except ImportError:
        print('Нужен пакет aiohttp, чтобы вызвать API.')
        return 1
    from support_agent.clients import OpenAI

    async def run():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as session:
            client = OpenAI(session, key, os.environ.get('OPENAI_MODEL', 'gpt-4.1-mini'), 'whisper-1')
            history = []
            for line in FIXTURES:
                history.append({'role': 'client', 'text': line})
                context = {'burst': line, 'history': history[-10:], 'dialog_state': {},
                           'tariffs': [], 'nominals': [], 'hints': []}
                try:
                    result = await client.understand_turn(context)
                except Exception as exc:
                    print(line.replace('\n', ' / '), 'ОШИБКА', type(exc).__name__)
                    continue
                reply = str(result.get('reply') or '').replace('\n', ' ')
                print(line.replace('\n', ' / '))
                print(' ', result.get('intent'), result.get('service_id'), result.get('tariff_id'))
                print(' ', reply[:240])
                history.append({'role': 'assistant', 'text': reply})

    asyncio.run(run())
    return 0


if __name__ == '__main__':
    sys.exit(main())
