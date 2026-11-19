"""Situation playbooks (owner 17–19.11.2026): one place that says what each kind of client
message must get. The model classifies the situation; finalize() checks the reply has the
required part and completes it from data/playbook.yaml. Replaces word-triggered patches."""
import functools
import pathlib
import re

import yaml

PATH = pathlib.Path(__file__).with_name('data') / 'playbook.yaml'
HARD_STOP = {'refund_complaint'}


@functools.lru_cache(maxsize=1)
def load():
    return yaml.safe_load(PATH.read_text(encoding='utf-8'))['situations']


def names():
    return list(load())


def prompt_block():
    lines = ['Ситуации (поле situation; для каждого вопроса клиента — в situations):']
    for key, row in load().items():
        lines.append(f'- {key}: {row["title"]}. {row["rule"]}')
    return '\n'.join(lines)


def _has(reply, patterns):
    return all(re.search('(?i)' + p, reply) for p in patterns or [])


def _tariffs(context):
    from .turn_understanding import _named_services, _service_rows, tariff_question
    for service in sorted(_named_services(context or {})):
        rows = _service_rows(context, service)
        if rows:
            return tariff_question(rows)
    return ''


def finalize(answer, understanding, context):
    """Complete the reply for each situation the model reported. Never turns an answer into a stop."""
    if not isinstance(answer, dict) or answer.get('handoff'):
        return answer
    book = load()
    u = understanding if isinstance(understanding, dict) else {}
    situations = [s for s in [u.get('situation')] + list(u.get('situations') or []) if s in book]
    if not situations:
        return answer
    from .rescue import ORDER_NO
    from .turn_understanding import BUY_STEPS
    reply = str(answer.get('reply') or '').strip()
    burst = str((context or {}).get('burst') or '')
    window = ' '.join(str(r.get('text') or '') for r in (context or {}).get('window') or [] if isinstance(r, dict))
    added = []
    for name in dict.fromkeys(situations):
        row = book[name]
        if _has(reply, row.get('need')):
            continue
        if row.get('skip_if_order_known') and ORDER_NO.search(burst + ' ' + window):
            continue
        part = ''
        if row.get('tariffs'):
            part = _tariffs(context)
            if row.get('add_buy_steps') and '@neirosetkaa_bot' not in reply + part:
                part += '\n\n' + BUY_STEPS
        elif row.get('add'):
            part = row['add']
        if part and part not in reply:
            added.append(part)
            reply = (reply + '\n\n' + part).strip()
    if not added:
        return answer
    evidence = dict(answer.get('evidence') or {})
    evidence['playbook'] = situations
    return dict(answer, reply=reply, evidence=evidence,
                summary=(str(answer.get('summary') or '') + ' Дополнено по сценарию: ' + ', '.join(situations) + '.').strip())
