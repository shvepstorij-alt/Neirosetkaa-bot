"""Paid owner-triggered smoke conversations through Service.generate, never Telegram clients."""
import asyncio
import html
import time
from .storage import Store
from .conversation_style import decorate_reply, session_info


class NoTelegram:
    async def call(self, *args, **kwargs):
        raise RuntimeError('Test isolation: Telegram client operation forbidden')


class CatalogOnly:
    def __init__(self, shop, owner):
        self.shop, self.owner = shop, owner

    async def facts(self, uid):
        # No client ID is queried. Do not put owner order history into the model.
        facts = await self.shop.facts(self.owner)
        allowed = ('catalog', 'catalog_status', 'catalog_complete', 'appstore', 'bot_navigation',
                   'live_shop_status', 'workflow_status', 'catalog_observed_at_unix', 'note', 'source')
        return {k: v for k, v in facts.items() if k in allowed}


SCENARIOS = (
    ('Оформление и смена темы', (
        ('Привет!', 'Как оформить Krea? Какие тарифы есть и что нужно кроме оплаты?'),
        ('А какие тарифы в зуме?',),
        ('Что потребуется для оформления обоих сервисов?',))),
    ('App Store и уточнение', (
        ('Как пополнить App Store? У меня регион Германия.',),
        ('Сколько стоит 10 евро?',))),
    ('Настоящий поиск и сравнение', (
        ('Какие возможности у платных тарифов Zoom и Krea? Сравни по официальным сведениям, а цены назови из магазина.',),)),
)


async def run_probe(service, max_calls=20):
    from .service import Service
    revision = service.store.get('revision')
    calls = 0
    def charge():
        nonlocal calls
        if service.store.get('revision') != revision:
            raise RuntimeError('Test stopped: owner changed mode')
        if calls >= max_calls:
            raise RuntimeError('Test API call limit reached')
        service.charge_call()
        calls += 1

    store = Store(':memory:', batch_seconds=300)
    isolated = Service(store, NoTelegram(), service.ai,
                       CatalogOnly(service.shop, service.owner), 0, {-1})
    isolated.charge_call = charge
    store.switch('draft')
    store.connection('test-only', True, True)
    stage = 'инициализация'
    try:
        for index, (name, turns) in enumerate(SCENARIOS):
            cid = None
            mid = 0
            for turn, fragments in enumerate(turns, 1):
                stage = f'сценарий {index+1}, шаг {turn}, подготовка ответа'
                if service.store.get('revision') != revision:
                    raise RuntimeError('Test stopped: owner changed mode')
                for fragment in fragments:
                    mid += 1
                    cid = store.ingest('test-only', -index-1, mid, 'client', fragment, {}, time.time())
                store.prepare_now(cid, time.time())
                job = store.claim(time.time())
                if not job:
                    raise RuntimeError('Test queue: synthetic batch not claimed')
                answer = await asyncio.wait_for(isolated.generate(job), timeout=180)
                if not answer:
                    raise RuntimeError('Test generation: no current answer returned')
                first = store.chat(cid)['batch_first']
                reply = decorate_reply(answer['reply'], session_info(store.db, cid, first, mid))
                store.finish(job, reply, answer.get('summary', ''), answer.get('handoff', True))
                # Simulate a delivered reply only in isolated memory, no send API.
                mid += 1
                store.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                                 (cid, mid, 'assistant', reply, '{}', time.time(), 'stored'))
                label = 'НУЖНА ПРОВЕРКА' if answer.get('handoff') else 'ПОДГОТОВЛЕН (не гарантия точности)'
                report = (f'Тест {index+1}, шаг {turn}: {name}\n{label}\n\n'
                          +'Вопрос:\n'+'\n'.join(fragments)+'\n\nОтвет:\n'+reply
                          +'\n\nРазбор:\n'+str(answer.get('summary', '')))
                await service.owner_message(html.escape(report), parse_mode='HTML')
        await service.owner_message(f'Прогон завершён. Учтённых этапов API: {calls}/{max_calls}. '
                                    'Часть ответов могла быть подготовлена без модели. '
                                    'Клиентам ничего не отправлено. Семантическую точность нужно оценить по отчёту.')
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        # Do not expose arbitrary exception messages, credentials or raw payloads.
        known = ('Test stopped: owner changed mode', 'Test API call limit reached',
                 'Test queue: synthetic batch not claimed', 'Test generation: no current answer returned',
                 'Daily API request limit reached')
        reason = str(exc) if str(exc) in known else ('превышено время этапа 180 секунд' if isinstance(exc, TimeoutError)
                 else 'первопричина не установлена; произвольный текст исключения скрыт')
        await service.owner_message(f'Прогон остановлен. Этап: {stage}; причина: {reason}; '
                                    f'тип: {type(exc).__name__}; учтённых этапов API: {calls}/{max_calls}. '
                                    'Полный прогон не завершён. Первопричину смотрите в диагностике приложения.')
    finally:
        store.db.close()
