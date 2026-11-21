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


async def run_dialogs(service, part, cases=None, label=None):
    from .privacy import scrub, sensitive
    from .service import Service
    from .voice import masculine, trim_filler
    if cases is None:
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
            now = time.time()
            timeline = _timeline(case, now)
            parts = timeline['burst']
            first_mid = len(history) + 1

            def said(value):
                return '[Сообщение скрыто: возможны данные доступа; нужна ручная проверка]' if sensitive(value) else scrub(value)

            cid = store.ingest('test-only', -number, first_mid, 'client', said(parts[0][0]),
                               {'kind': 'sensitive'} if sensitive(parts[0][0]) else {}, parts[0][1])
            for mid, ((role, value), at) in enumerate(zip(history, timeline['history']), 1):
                store.db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?)',
                                 (cid, mid, role if role in ('owner', 'assistant') else 'client', scrub(str(value)), '{}', at, 'stored'))
            for offset, (value, at) in enumerate(parts[1:], 1):
                store.ingest('test-only', -number, first_mid + offset, 'client', said(value),
                             {'kind': 'sensitive'} if sensitive(value) else {}, at)
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
            from .auto_policy import real_stop_reason
            gate = None if stop else real_stop_reason(dict(answer, reply=reply), answer.get('evidence'), text)
            stopped = bool(stop or gate)
            store.finish(job, reply, answer.get('summary', ''), answer.get('handoff', True))
            ev = answer.get('evidence') or {}
            reason = str(ev.get('handoff_reason') or gate or ev.get('auto_block_reason') or ev.get('diagnostic') or '')[:200]
            try:
                charge('grade')
                grade = await asyncio.wait_for(service.ai.grade_reply(grade_case(case, reply), reply, stopped, getattr(isolated, 'last_tariffs', None) or []), timeout=90)
            except RuntimeError:
                raise
            except Exception as exc:
                grade = {'verdict': 'error', 'category': 'other', 'problem': type(exc).__name__, 'fix': ''}
            auto, auto_why = auto_verdict(answer, reply, stopped, reason, text, hidden)
            rows.append({'n': number, 'case': case, 'reply': reply, 'stopped': stopped, 'reason': reason, 'grade': grade,
                         'auto': auto, 'auto_why': auto_why})
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
        await _report(service, part, rows, calls, label)


GRADE_NOTE = '(Эталон — первый ответ Александра; факты, которых нет в истории, помощник знать не мог)'
GRADE_PAYLOAD = 5600
MANUAL_MARKERS = ('[клиент прислал вложение]', '[клиент прислал текстовый файл]')


def _timeline(case, now):
    """Real relative timing (21.11.2026): at = now - (case_last_at - original_at).

    Cases from /live carry real times ('times' for history, 'burst' for the client's lines);
    the archived /dialogs cases have none and keep the old synthetic times.
    """
    history = case.get('history') or []
    times, burst = case.get('times'), case.get('burst')
    try:
        parts = [(str(value), float(at)) for value, at in burst] if burst else []
        stamps = [float(at) for at in times] if isinstance(times, list) and len(times) == len(history) else None
    except (TypeError, ValueError):
        parts, stamps = [], None
    if not parts:
        start = now - 600
        return {'history': [start + mid for mid in range(1, len(history) + 1)], 'burst': [(str(case.get('client') or ''), now)]}
    last = parts[-1][1]

    def shift(at):
        return now - max(0.0, last - at)
    if stamps is None:
        first = parts[0][1]
        stamps = [first - 600 + mid for mid in range(1, len(history) + 1)]
    return {'history': [shift(at) for at in stamps], 'burst': [(value, shift(at)) for value, at in parts]}


def gap_label(seconds):
    seconds = max(0, float(seconds or 0))
    if seconds >= 86400:
        return 'через %d дн.' % round(seconds / 86400)
    if seconds >= 3600:
        return 'через %d ч' % round(seconds / 3600)
    if seconds >= 600:
        return 'через %d мин' % round(seconds / 60)
    return ''


def _dated_history(case):
    history = case.get('history') or []
    times = case.get('times')
    if not isinstance(times, list) or len(times) != len(history):
        return [[role, str(said)] for role, said in history]
    out, previous = [], None
    for (role, said), at in zip(history, times):
        label = gap_label(at - previous) if previous is not None and at is not None else ''
        out.append([role, ('[' + label + '] ' if label else '') + str(said)])
        previous = at if at is not None else previous
    return out


def grade_case(case, reply=''):
    """What the grader sees. Live cases: only the first reply is the reference, with the fairness note,
    gaps shown in the history, and the newest history kept when the payload is too long."""
    if not case.get('burst'):
        return case
    owner = GRADE_NOTE + '\n' + str(case.get('owner') or '')
    client = str(case.get('client') or '')[:1500]
    times = case.get('times') or []
    burst = case.get('burst') or []
    if times and burst and times[-1] is not None:
        label = gap_label(float(burst[0][1]) - float(times[-1]))
        if label:
            client = '[' + label + '] ' + client
    budget = GRADE_PAYLOAD - len(owner) - len(client) - len(str(reply or ''))
    kept = []
    for role, said in reversed(_dated_history(case)):
        said = said[:400]
        budget -= len(said) + 20
        if budget < 0:
            break
        kept.insert(0, [role, said])
    return dict(case, history=kept, client=client, owner=owner)


def auto_verdict(answer, reply, stopped, reason, text, hidden=False):
    """Would this reply have gone to the client by itself? Same gates as the real auto mode."""
    if stopped:
        return False, 'стоп: ' + (reason or 'передано Александру')
    if hidden:
        return False, 'данные доступа в сообщении'
    if any(marker in str(text or '') for marker in MANUAL_MARKERS):
        return False, 'вложение для ручного разбора'
    from .auto_policy import block_reason
    try:
        block = block_reason(dict(answer or {}, reply=reply), (answer or {}).get('evidence'), text)
    except Exception as exc:
        block = 'сбой проверки: ' + type(exc).__name__
    if block:
        return False, ' '.join(str(block).split())[:200]
    return True, 'все проверки пройдены'


def _bad_auto(row):
    return bool(row.get('auto')) and row['grade'].get('verdict') in ('harmful', 'wrong')


def _block(row):
    case, grade = row['case'], row['grade']
    lines = ['#D%d  [%s%s]' % (row['n'], VERDICTS.get(grade['verdict'], grade['verdict']),
                               (' · ' + CATEGORIES.get(grade['category'], grade['category'])) if grade.get('category') not in (None, 'none') else '')]
    names = {'client': '  Клиент: ', 'assistant': '  Помощник: '}
    for role, said in _dated_history(case)[-4:]:
        lines.append(names.get(role, '  Александр: ') + ' '.join(str(said).split())[:220])
    lines.append('В: ' + case.get('client', ''))
    lines.append('Александр ответил' + (' (первый ответ)' if case.get('burst') else '') + ': ' + case.get('owner', ''))
    if case.get('later'):
        lines.append('Позже Александр писал (%d сообщ., не эталон): %s' % (case.get('later_count') or 0, case['later']))
    lines.append('Помощник' + (' (СТОП: ' + row['reason'] + ')' if row['stopped'] else '') + ': ' + row['reply'])
    if 'auto' in row:
        lines.append('Автоотправка: %s (%s)' % ('да' if row['auto'] else 'нет', row.get('auto_why') or ''))
    if grade.get('problem'):
        lines.append('Проблема: ' + grade['problem'])
    if grade.get('fix'):
        lines.append('Как надо: ' + grade['fix'])
    return '\n'.join(lines)


async def _report(service, part, rows, calls, label=None):
    verdicts = collections.Counter(row['grade']['verdict'] for row in rows)
    cats = collections.Counter(row['grade']['category'] for row in rows
                               if row['grade']['verdict'] not in ('good', 'right_stop') and row['grade']['category'] != 'none')
    stops = sum(1 for row in rows if row['stopped'])
    order = ['harmful', 'wrong', 'needless_stop', 'partial', 'error', 'right_stop', 'good']
    rows.sort(key=lambda row: (not _bad_auto(row), order.index(row['grade']['verdict']) if row['grade']['verdict'] in order else 9, row['n']))
    bad_auto = sum(1 for row in rows if _bad_auto(row))
    auto_total = sum(1 for row in rows if row.get('auto'))
    summary = ['Плохих ответов, которые ушли бы сами (вредно/неверно при автоотправке): %d из %d ушедших бы сами' % (bad_auto, auto_total),
               (label or 'Проверка диалогов, часть %d' % part) + ': %d диалогов, запросов к модели %d. Клиентам ничего не отправлено.' % (len(rows), calls),
               '', 'Оценки:']
    summary += ['• %s — %d' % (VERDICTS[key], verdicts[key]) for key in order if verdicts.get(key)]
    summary.append('Остановок всего: %d' % stops)
    if cats:
        summary += ['', 'Главные причины проблем:'] + ['• %s — %d' % (CATEGORIES.get(k, k), v) for k, v in cats.most_common(8)]
    text = '\n'.join(summary)
    model = str(getattr(service.ai, 'model', '') or '')
    body = 'Модель: ' + model + '\n\n' + text + '\n\n' + '=' * 40 + '\n\n' + '\n\n'.join(_block(row) for row in rows)
    try:
        await service.tg.send_text_file(service.owner, ('live.txt' if label else 'dialogs_%d.txt' % part), body,
                                        'Все диалоги части %d, сначала проблемные. Перешлите файл Claude.' % part)
    except Exception:
        pass
    await service.owner_message(text)
