"""/check N — the owner's check run: fixed questions through the real pipeline, compact report.

Isolated memory store, catalog only, no Telegram client calls: no client receives anything.
"""
import asyncio
import html
import time

from .check_cases import PARTS
from .dialog_probe import CatalogOnly, NoTelegram
from .storage import Store

MAX_CALLS = 90


def _reason(answer):
    evidence = answer.get('evidence') or {}
    return str(evidence.get('diagnostic') or evidence.get('handoff_reason') or answer.get('summary') or '')[:300]


async def run_check(service, part):
    from .auto_policy import block_reason
    from .privacy import scrub, sensitive
    from .service import Service
    from .voice import masculine
    dialogs = PARTS[part]
    revision = service.store.get('revision')
    calls = 0

    def charge(reason='model'):
        nonlocal calls
        if service.store.get('revision') != revision:
            raise RuntimeError('остановлено: режим изменён')
        if calls >= MAX_CALLS:
            raise RuntimeError('лимит запросов этой части')
        service.charge_call()
        calls += 1

    store = Store(':memory:', batch_seconds=300)
    isolated = Service(store, NoTelegram(), service.ai, CatalogOnly(service.shop, service.owner), 0, {-1})
    isolated.charge_call = charge
    store.switch('draft')
    store.connection('test-only', True, True)
    blocks, sent, held, number = [], 0, 0, 0

    async def flush(final=False):
        nonlocal blocks
        if blocks and (final or len(blocks) >= 5):
            await service.owner_message('\n\n'.join(blocks))
            blocks = []

    try:
        for index, turns in enumerate(dialogs):
            cid, mid = None, 0
            for step, fragments in enumerate(turns, 1):
                number += 1
                question = '\n'.join(fragments)
                for fragment in fragments:
                    mid += 1
                    hidden = sensitive(fragment)
                    cid = store.ingest('test-only', -index - 1, mid, 'client',
                                       '[Сообщение скрыто: возможны данные доступа; нужна ручная проверка]' if hidden else scrub(fragment),
                                       {'kind': 'sensitive'} if hidden else {}, time.time())
                store.prepare_now(cid, time.time())
                job = store.claim(time.time())
                if not job:
                    raise RuntimeError('очередь не выдала задание')
                answer = await asyncio.wait_for(isolated.generate(job), timeout=180)
                if not answer:
                    raise RuntimeError('ответ не получен')
                reply = masculine(str(answer.get('reply') or ''))
                stop = bool(answer.get('handoff'))
                gate = None if stop else block_reason(dict(answer, reply=reply), answer.get('evidence'), question)
                store.finish(job, reply, answer.get('summary', ''), answer.get('handoff', True))
                mid += 1
                store.db.execute('INSERT INTO messages VALUES (?,?,?,?,?,?,?)',
                                 (cid, mid, 'assistant', reply, '{}', time.time(), 'stored'))
                if stop or gate:
                    held += 1
                else:
                    sent += 1
                label = ('СТОП: ' + _reason(answer)) if stop else ('СТОП АВТО: ' + str(gate)[:300]) if gate else 'ОТПРАВИЛ БЫ'
                head = '#%d.%d' % (part, number) + (' (диалог, шаг %d)' % step if len(turns) > 1 else '')
                blocks.append(head + '\nВ: ' + question + '\n[' + label + ']\nО: ' + reply.replace('\n\n', '\n'))
                await flush()
        await flush(final=True)
        await service.owner_message('Часть %d готова: вопросов %d, отправил бы %d, остановок %d, запросов к модели %d. '
                                    'Клиентам ничего не отправлено. Перешлите эти сообщения Claude.' % (part, number, sent, held, calls))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        try:
            await flush(final=True)
        except Exception:
            pass
        known = isinstance(exc, (RuntimeError, ValueError, TimeoutError))
        await service.owner_message('Часть %d остановлена на вопросе %d: %s. Запросов к модели: %d.' % (
            part, number, (str(exc) or type(exc).__name__) if known else type(exc).__name__, calls))
    finally:
        store.db.close()
