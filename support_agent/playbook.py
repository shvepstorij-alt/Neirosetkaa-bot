"""Situation playbooks (owner 17–19.11.2026): one place that says what each kind of client
message must get. The model classifies the situation; finalize() checks the reply has the
required part and completes it from data/playbook.yaml. Replaces word-triggered patches."""
import functools
import pathlib
import re

import yaml

PATH = pathlib.Path(__file__).with_name('data') / 'playbook.yaml'
HARD_STOP = {'refund_complaint'}
_ASKS_DATA = re.compile(r'(?i)(?:пришлите|отправьте|нужн[ыа])[^.\n]{0,80}(?:почт\w*\s+и\s+парол|парол|sessionkey|токен|резервн\w+\s+код)')
AFTER_PAY = 'Оплатить можно в боте @neirosetkaa_bot: «Каталог» → сервис → тариф, по СБП или картой «Мир». После оплаты подскажу, что понадобится для активации.'


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
    rows = []
    for service in sorted(_named_services(context or {})):
        rows += _service_rows(context, service) or []  # owner 20.11.2026: every named service, not the first
    return tariff_question(rows) if rows else ''


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
    stripped = False
    if set(situations) & {'buy', 'price', 'compare_choose'} and not set(situations) & {'paid_not_working', 'activation_step', 'owner_step'}:
        # Owner 20.11.2026 (bench): activation data is asked only after payment.
        kept = [p for p in re.split(r'\n\s*\n', reply) if not _ASKS_DATA.search(p)]
        if len(kept) != len(re.split(r'\n\s*\n', reply)):
            reply = '\n\n'.join(kept + [AFTER_PAY]).strip()
            stripped = True
    window = ' '.join(str(r.get('text') or '') for r in (context or {}).get('window') or [] if isinstance(r, dict))
    added = []
    for name in dict.fromkeys(situations):
        row = book[name]
        if _has(reply, row.get('need')):
            continue
        if row.get('skip_if_order_known') and ORDER_NO.search(burst + ' ' + window):
            continue
        if row.get('skip_if_order_known'):
            known = order_status_reply(context)
            if known:
                if known not in reply and not re.search(r'#\d{3,}', reply):
                    added.append(known)
                    reply = (reply + '\n\n' + known).strip() if not _has(reply, ['номер заказа']) else known
                continue
        part = ''
        if row.get('tariffs'):
            part = _tariffs(context)
            if part and row.get('add_buy_steps') and '@neirosetkaa_bot' not in reply + part:
                part += '\n\n' + BUY_STEPS
        elif row.get('add'):
            part = row['add']
        if part and part not in reply:
            added.append(part)
            reply = (reply + '\n\n' + part).strip()
    if not added and not stripped:
        return answer
    evidence = dict(answer.get('evidence') or {})
    evidence['playbook'] = situations
    return dict(answer, reply=reply, evidence=evidence,
                summary=(str(answer.get('summary') or '') + ' Дополнено по сценарию: ' + ', '.join(situations) + '.').strip())


_GPT_LINE = re.compile(r'\n?• Если это ChatGPT — [^\n]*')
_CLAUDE_LINE = re.compile(r'\n?• Если это Claude — [^\n]*')


def tidy(answer, context):
    """Owner 19.11.2026 (dialogs): keep only the bullet for the client's service; no empty gaps."""
    if not isinstance(answer, dict):
        return answer
    reply = str(answer.get('reply') or '')
    if '• Если это ChatGPT' in reply and '• Если это Claude' in reply:
        try:
            from .turn_understanding import _named_services
            named = set(_named_services(context or {}))
        except Exception:
            named = set()
        named.add(str(((context or {}).get('dialog_state') or {}).get('service') or ''))
        if 'chatgpt' in named and 'claude' not in named:
            reply = _CLAUDE_LINE.sub('', reply).replace('• Если это ChatGPT — пришлите ещё', 'Ещё пришлите')
        elif 'claude' in named and 'chatgpt' not in named:
            reply = _GPT_LINE.sub('', reply).replace('• Если это Claude — пришлите', 'Ещё пришлите')
    if re.search(r'(?i)(?:напиш|обрат)\w*[^.\n]{0,80}@?neirosetkaalex', reply):
        # Owner 20.11.2026: this chat IS @Neirosetkaalex — never send the client to write here.
        from .turn_understanding import _faq_answer
        reply = _faq_answer('course_bonus_start') or reply
    reply = re.sub(r'\n{3,}', '\n\n', reply).strip()
    return dict(answer, reply=reply) if reply != answer.get('reply') else answer


def order_status_reply(context):
    """Owner 19.11.2026: when the shop shows this client's paid order, say what it shows instead of asking."""
    orders = ((context or {}).get('client_orders') or {}).get('orders') or []
    if not orders:
        return ''
    row = orders[0]
    label = ' '.join(x for x in (row.get('service'), row.get('plan')) if x).strip()
    head = 'Вижу заказ ' + (row.get('order') or '') + (', ' + label if label else '')
    code = row.get('state_code')
    if not row.get('paid'):
        return head + ', но оплата по нему пока не прошла. Если деньги списались, пришлите, пожалуйста, чек из банка — Александр проверит.'
    if code == 'pending_window':
        return head + ', он оплачен. В боте открыта активация: нажмите «Активировать подписку» в сообщении после оплаты и следуйте инструкции.'
    if code in ('code_consumed', 'fulfilled_recorded', 'in_progress'):
        return (head + ', активация по нему уже проходила. Выйдите из аккаунта и зайдите снова через 10–15 минут, с VPN одной страны. '
                'Если подписка так и не появится, пришлите скриншот настроек аккаунта — Александр проверит.')
    if code == 'manual_pending':
        return head + ', он оплачен и сейчас в ручном оформлении у Александра — он напишет здесь.'
    return head + ', он оплачен. Если кнопка активации больше не работает, Александр выдаст повторную активацию — напишите, что именно не получается, или пришлите скриншот.'


@functools.lru_cache(maxsize=1)
def business_profile():
    """Owner 20.11.2026: the whole business, so no question about a product line is a surprise."""
    try:
        return (PATH.parent / 'business_profile.md').read_text(encoding='utf-8').strip()
    except OSError:
        return ''
