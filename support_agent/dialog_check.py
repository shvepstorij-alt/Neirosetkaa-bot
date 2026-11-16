"""/dialogs N — the wide check (owner 16.11.2026).

Real dialog fragments from the owner's chats (28.07–28.10.2026, personal data removed):
the assistant sees the same history, answers the client's message, and a second model
compares the answer with what Alexander really wrote. The owner gets a summary of the
problems and one file with every case. Nothing is sent to any client.
"""
import asyncio
import collections
import json
import pathlib
import time

from .dialog_probe import CatalogOnly, NoTelegram
from .storage import Store

PART_SIZE = 100
MAX_CALLS = 380
_CASES = pathlib.Path(__file__).with_name('data') / 'dialog_cases.json'

VERDICTS = {'good': 'хорошо', 'partial': 'частично', 'wrong': 'неверно', 'harmful': 'вредно',
            'needless_stop': 'лишний стоп', 'right_stop': 'верный стоп', 'error': 'сбой проверки'}
CATEGORIES = {'none': '', 'wrong_service': 'не тот сервис или тема', 'missed_question': 'упустил вопрос',
              'wrong_steps': 'неверные шаги', 'invented_fact': 'выдумал факт', 'price': 'цена',
              'too_vague': 'слишком общо', 'too_long': 'слишком длинно', 'lost_context': 'потерял нить разговора',
              'should_ask': 'надо было уточнить', 'should_handoff': 'надо было передать Александру',
              'needless_stop': 'лишняя остановка', 'tone': 'тон', 'language': 'язык', 'other': 'другое'}


def load_cases():
    return json.loads(_CASES.read_text(encoding='utf-8'))


def parts():
    total = len(load_cases())
    return (total + PART_SIZE - 1) // PART_SIZE


async def run_dialogs(service, part):
    from .auto_policy import block_reason
    from .privacy import scrub, sensitive
    from .service import Service
    from .voice import masculine, trim_filler
    cases = load_cases()[(part - 1) * PART_SIZE: part * PART_SIZE]
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
    rows, done = [], 0
    try:
        for index, case in enumerate(cases):
            number = (part - 1) * PART_SIZE + index + 1
            history = case.get('history') or []
            text = str(case.get('client') or '')
            hidden = sensitive(text)
            cid = store.ingest('test-only', -number, len(history) + 1, 'client',
                               '[Сообщение скрыто: возможны данные доступа; нужна ручная проверка]' if hidden else scrub(text),
                               {'kind': 'sensitive'} if hidden else {}, time.time())
            start = time.time() - 600
            for mid, (role, said) in enumerate(history, 1):
                store.db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?)',
                                 (cid, mid, 'owner' if role == 'owner' else 'client', scrub(said), '{}', start + mid, 'stored'))
            store.prepare_now(cid, time.time())
            job = store.claim(time.time())
            if not job:
                raise RuntimeError('очередь не выдала задание')
            try:
                answer = await asyncio.wait_for(isolated.generate(job), timeout=180)
            except asyncio.CancelledError:
                raise
            except RuntimeError:
                raise
            except Exception as exc:
                answer = {'reply': '', 'handoff': True, 'evidence': {'diagnostic': 'сбой: ' + type(exc).__name__}}
            answer = answer or {'reply': '', 'handoff': True, 'evidence': {'diagnostic': 'ответ не получен'}}
            reply = trim_filler(masculine(str(answer.get('reply') or '')))
            stop = bool(answer.get('handoff'))
            gate = None if stop else block_reason(dict(answer, reply=reply), answer.get('evidence'), text)
            stopped = bool(stop or gate)
            store.finish(job, reply, answer.get('summary', ''), answer.get('handoff', True))
            ev = answer.get('evidence') or {}
            reason = str(ev.get('auto_block_reason') or ev.get('handoff_reason') or ev.get('diagnostic') or gate or '')[:200]
            try:
                charge('grade')
                grade = await asyncio.wait_for(service.ai.grade_reply(case, reply, stopped), timeout=90)
            except RuntimeError:
                raise
            except Exception as exc:
                grade = {'verdict': 'error', 'category': 'other', 'problem': type(exc).__name__, 'fix': ''}
            rows.append({'n': number, 'case': case, 'reply': reply, 'stopped': stopped, 'reason': reason, 'grade': grade})
            done += 1
            if done % 25 == 0:
                await service.owner_message('Проверка диалогов, часть %d: готово %d из %d.' % (part, done, len(cases)))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await service.owner_message('Часть %d остановлена после %d диалогов: %s.' % (part, done, str(exc) or type(exc).__name__))
    finally:
        store.db.close()
    if rows:
        await _report(service, part, rows, calls)


def _block(row):
    case, grade = row['case'], row['grade']
    lines = ['#D%d  [%s%s]' % (row['n'], VERDICTS.get(grade['verdict'], grade['verdict']),
                               (' · ' + CATEGORIES.get(grade['category'], grade['category'])) if grade.get('category') not in (None, 'none') else '')]
    for role, said in (case.get('history') or [])[-4:]:
        lines.append(('  Клиент: ' if role == 'client' else '  Александр: ') + ' '.join(str(said).split())[:220])
    lines.append('В: ' + case.get('client', ''))
    lines.append('Александр ответил: ' + case.get('owner', ''))
    lines.append('Помощник' + (' (СТОП: ' + row['reason'] + ')' if row['stopped'] else '') + ': ' + row['reply'])
    if grade.get('problem'):
        lines.append('Проблема: ' + grade['problem'])
    if grade.get('fix'):
        lines.append('Как надо: ' + grade['fix'])
    return '\n'.join(lines)


async def _report(service, part, rows, calls):
    verdicts = collections.Counter(row['grade']['verdict'] for row in rows)
    cats = collections.Counter(row['grade']['category'] for row in rows
                               if row['grade']['verdict'] not in ('good', 'right_stop') and row['grade']['category'] != 'none')
    stops = sum(1 for row in rows if row['stopped'])
    order = ['harmful', 'wrong', 'needless_stop', 'partial', 'error', 'right_stop', 'good']
    rows.sort(key=lambda row: (order.index(row['grade']['verdict']) if row['grade']['verdict'] in order else 9, row['n']))
    summary = ['Проверка диалогов, часть %d: %d диалогов, запросов к модели %d. Клиентам ничего не отправлено.' % (part, len(rows), calls),
               '', 'Оценки:']
    summary += ['• %s — %d' % (VERDICTS[key], verdicts[key]) for key in order if verdicts.get(key)]
    summary.append('Остановок всего: %d' % stops)
    if cats:
        summary += ['', 'Главные причины проблем:'] + ['• %s — %d' % (CATEGORIES.get(k, k), v) for k, v in cats.most_common(8)]
    text = '\n'.join(summary)
    model = str(getattr(service.ai, 'model', '') or '')
    body = 'Модель: ' + model + '\n\n' + text + '\n\n' + '=' * 40 + '\n\n' + '\n\n'.join(_block(row) for row in rows)
    try:
        await service.tg.send_text_file(service.owner, 'dialogs_%d.txt' % part, body,
                                        'Все диалоги части %d, сначала проблемные. Перешлите файл Claude.' % part)
    except Exception:
        pass
    await service.owner_message(text)
