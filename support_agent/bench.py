"""/bench — the fixed benchmark run before every release (owner 20.11.2026).

Each case says what the reply must contain, must not contain, and whether it must stop.
Checked by rules, not by a second model, so the score is exact and cheap (one call per case).
"""
import asyncio
import pathlib
import re
import time

import yaml

from .dialog_probe import CatalogOnly, NoTelegram
from .storage import Store

PATH = pathlib.Path(__file__).with_name('data') / 'benchmark.yaml'


def load():
    return yaml.safe_load(PATH.read_text(encoding='utf-8'))['cases']


def check(case, reply, stopped):
    problems = []
    if bool(case.get('stop')) != bool(stopped):
        problems.append('должен остановиться' if case.get('stop') else 'лишняя остановка')
    if not case.get('stop'):
        for pattern in case.get('need') or []:
            if not re.search('(?im)' + pattern, reply):
                problems.append('нет: ' + pattern)
        for pattern in case.get('forbid') or []:
            if re.search('(?im)' + pattern, reply):
                problems.append('лишнее: ' + pattern)
    return problems


async def run_bench(service, mode='new'):
    from .auto_policy import block_reason
    from .privacy import scrub
    from .service import Service
    from .voice import masculine, trim_filler
    cases = load()
    # Owner 20.11.2026: «/bench» runs only new and previously failed cases; «/bench all» runs everything.
    import json
    try:
        passed_before = set(json.loads(service.store.get('bench_passed') or '[]'))
    except Exception:
        passed_before = set()
    if mode != 'all':
        cases = [case for case in cases if case['id'] not in passed_before]
        if not cases:
            await service.owner_message('Все случаи эталона уже прошли. Новых нет. Полный прогон перед релизом: /bench all.')
            return
    store = Store(':memory:', batch_seconds=300)
    isolated = Service(store, NoTelegram(), service.ai, CatalogOnly(service.shop, service.owner), 0, {-1})
    calls = 0

    def charge(reason='model'):
        nonlocal calls
        if calls >= len(cases) * 4:
            raise RuntimeError('лимит запросов проверки')
        service.charge_call()
        calls += 1
    isolated.charge_call = charge
    store.switch('draft')
    store.connection('bench', True, True)
    rows = []
    try:
        for number, case in enumerate(cases, 1):
            history = case.get('history') or []
            cid = store.ingest('bench', -number, len(history) + 1, 'client', scrub(case['client']), {}, time.time())
            for mid, (role, said) in enumerate(history, 1):
                store.db.execute('INSERT OR REPLACE INTO messages VALUES (?,?,?,?,?,?,?)',
                                 (cid, mid, 'owner' if role == 'owner' else 'client', said, '{}', time.time() - 600 + mid, 'stored'))
            store.prepare_now(cid, time.time())
            job = store.claim(time.time())
            try:
                answer = await asyncio.wait_for(isolated.generate(job), timeout=180) if job else None
            except RuntimeError:
                raise
            except Exception as exc:
                answer = {'reply': '', 'handoff': True, 'evidence': {'diagnostic': type(exc).__name__}}
            answer = answer or {'reply': '', 'handoff': True}
            reply = trim_filler(masculine(str(answer.get('reply') or '')))
            stopped = bool(answer.get('handoff')) or bool(block_reason(dict(answer, reply=reply), answer.get('evidence'), case['client']))
            if job:
                store.finish(job, reply, answer.get('summary', ''), answer.get('handoff', True))
            rows.append((case, reply, stopped, check(case, reply, stopped)))
    except Exception as exc:
        await service.owner_message('Эталонная проверка остановлена: %s.' % (str(exc) or type(exc).__name__))
    finally:
        store.db.close()
    if not rows:
        return
    passed = sum(1 for row in rows if not row[3])
    now_passed = {case['id'] for case, _, _, problems in rows if not problems}
    now_failed = {case['id'] for case, _, _, problems in rows if problems}
    service.store.put('bench_passed', json.dumps(sorted((passed_before - now_failed) | now_passed)))
    lines = ['Эталонная проверка (%s): %d из %d прошли. Запросов к модели: %d.' % ('полная' if mode == 'all' else 'новые и непрошедшие', passed, len(rows), calls), '']
    for case, reply, stopped, problems in sorted(rows, key=lambda r: not r[3]):
        if not problems:
            continue
        lines += ['✗ %s — %s' % (case['id'], '; '.join(problems)), 'Клиент: ' + ' / '.join(case['client'].splitlines()),
                  'Помощник' + (' (СТОП)' if stopped else '') + ': ' + reply, '']
    lines.append('Прошли: ' + ', '.join(case['id'] for case, _, _, problems in rows if not problems))
    text = '\n'.join(lines)
    await service.owner_message(lines[0] + ' Подробности — в файле.')
    try:
        await service.tg.send_text_file(service.owner, 'bench.txt', text, 'Эталонная проверка. Перешлите файл Claude.')
    except Exception:
        await service.owner_message(text[:3500])
