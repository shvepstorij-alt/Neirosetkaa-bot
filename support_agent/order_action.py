"""Owner 21.11.2026: any order reference → check this client's order → act on its real state.

The client wrote «Заказ #9974», «freekassa 346894753» or sent a screenshot with the number.
The assistant reads the order (owned by this Telegram user only) and answers from the shop's state:

  not found / not paid        → Alexander checks (handoff), no promise
  activation window open      → press «Активировать подписку» in the bot
  window closed, never activated, ChatGPT/Claude/Perplexity
                              → the assistant itself issues a repeat activation (shop action),
                                falls back to «Александр выдаст» if the shop refuses
  activation already happened → re-login after 10–15 minutes, then a screenshot
  manual fulfilment           → Alexander continues here
No codes, tokens or links are ever produced here.
"""
import logging
import re

_REF = re.compile(
    r'(?i)(?:заказ\w*|order)\s*(?:№|#|n\.?|номер)?\s*:?\s*#?\s*(\d{3,12})'
    r'|#\s*(\d{3,12})\b'
    r'|номер\s+заказа\s*:?\s*#?\s*(\d{3,12})'
    r'|(?:free\s*kassa|фри\s*касс\w*|fk)\s*(?:№|#|номер|id|intid)?\s*:?\s*(\d{5,20})')
_CONTEXT = re.compile(r'(?i)оплат|оплачен|заплат|актив|подписк|заказ|freekassa|кнопк|не\s+пришл|не\s+работ')
_PRO_PLAN = re.compile(r'(?i)\bpro\s*(?:5|20|50|100|200)|про\s*(?:5|20|50)')
AUTO_SERVICES = ('chatgpt', 'gpt', 'claude', 'perplexity')
REACTIVATION_DONE = ('Заказ #{n} вижу, он оплачен. Окно активации уже закрылось, поэтому я выдал повторную активацию: '
                     'в боте @neirosetkaa_bot пришло новое сообщение с кнопкой «Активировать подписку». Нажмите её — '
                     'кнопка действует 2 часа. Если что-то не получится, пришлите скриншот.')
REACTIVATION_BY_OWNER = ('Заказ #{n} вижу, он оплачен. Окно активации уже закрылось — Александр выдаст повторную активацию, '
                         'и в боте придёт новое сообщение с кнопкой «Активировать подписку».')
WINDOW_OPEN = ('Заказ #{n} вижу, он оплачен. В боте @neirosetkaa_bot открыта кнопка «Активировать подписку» в сообщении после оплаты — '
               'нажмите её. Если сообщения не видно, пролистайте чат с ботом чуть выше или ниже.')
ALREADY_ACTIVATED = ('Заказ #{n} вижу: активация по нему уже проходила. Если подписки не видно, выйдите из аккаунта и войдите снова '
                     'через 10–15 минут, с VPN одной и той же страны. Если не появится — пришлите скриншот.')
MANUAL = 'Заказ #{n} вижу, он в ручном оформлении — Александр продолжит здесь.'
PAID_OTHER = 'Заказ #{n} вижу, он оплачен. Александр проверит активацию и ответит здесь.'


def find_reference(text):
    """The first order reference in the client's own words, or ''. Needs a payment/activation context."""
    value = str(text or '')
    match = _REF.search(value)
    if not match or not _CONTEXT.search(value):
        return ''
    return next(group for group in match.groups() if group)


def _service_key(row):
    return str((row or {}).get('service_key') or '').casefold()


async def decide(service, job, number, context=None, source='text'):
    """An answer built from the order's real state, or None when the shop cannot be read (normal flow continues)."""
    number = re.sub(r'\D', '', str(number or ''))
    if not number:
        return None
    try:
        lookup = await service.shop.order_facts(job['uid'], {'untyped': number})
    except Exception:
        return None
    if not isinstance(lookup, dict):
        return None
    status = str(lookup.get('orders_status') or '')
    orders = lookup.get('orders') if isinstance(lookup.get('orders'), list) else []
    if status in ('read_error', 'not_connected', 'invalid_identity', 'invalid_reference') or status == 'ambiguous':
        return None
    row = orders[0] if status == 'available' and len(orders) == 1 and isinstance(orders[0], dict) else None
    shown = str((row or {}).get('num') or number)
    note = 'Заказ #' + shown + ' · ' + (_service_key(row) or '?') + ' ' + str((row or {}).get('plan_name') or '') + \
           ' · оплата: ' + str((row or {}).get('status') or status) + ' · активация: ' + str((row or {}).get('activation_status') or '—')
    if not row or str(row.get('status') or '').casefold() != 'paid':
        from .handoff import handoff_text
        return _answer(handoff_text(), True, note + '. Заказ не найден или не оплачен — нужен Александр.', number, row, 'not_paid')
    activation = str(row.get('activation_status') or 'not_checked')
    key = _service_key(row)
    if key.startswith(('chatgpt', 'gpt')) and _PRO_PLAN.search(str(row.get('plan_name') or '')) \
            and activation not in ('in_progress', 'code_consumed'):
        # Owner 24.11.2026: Pro 5× and above have no activation button — the client sends the token in one text file.
        from .paid_notice import TOKEN_STEPS_MANUAL
        return _answer('Заказ #' + shown + ' вижу, он оплачен. ' + TOKEN_STEPS_MANUAL.format(), False,
                       note + '. Pro: запрошен токен файлом.', number, row, 'manual', owner_alert=True)
    if activation == 'pending_window':
        return _answer(WINDOW_OPEN.format(n=shown), False, note, number, row, 'window_open')
    if activation == 'code_consumed':
        return _answer(ALREADY_ACTIVATED.format(n=shown), False, note, number, row, 'already_activated')
    # Owner 25.11.2026: «in_progress» = the activation was started but not confirmed (mini-app did not open,
    # client gave up). Treat it as not activated: ask the shop for a repeat activation; the shop refuses if it went through.
    if activation == 'manual_pending':
        return _answer(MANUAL.format(n=shown), False, note, number, row, 'manual')
    # «fulfilled_recorded» is set as soon as the shop SENDS the button, so an expired, never-used order looks
    # like that too. The shop re-checks the exact signals (code used, done mark, activation started) and decides.
    if activation in ('not_checked', 'fulfilled_recorded', 'in_progress') and any(key.startswith(s) for s in AUTO_SERVICES):
        result = {'ok': False, 'error': 'not_connected', 'msg': ''}
        resend = getattr(service.shop, 'resend_activation', None)
        if resend:
            try:
                result = await resend(job['uid'], shown)
            except Exception as exc:
                logging.warning('Repeat activation call failed: %s', type(exc).__name__)
                result = {'ok': False, 'error': 'call_failed', 'msg': ''}
        if isinstance(result, dict) and result.get('ok'):
            return _answer(REACTIVATION_DONE.format(n=shown), False,
                           note + '. 🤖 Помощник выдал повторную активацию.', number, row, 'reactivated')
        if isinstance(result, dict) and result.get('error') == 'window_open':
            return _answer(WINDOW_OPEN.format(n=shown), False, note, number, row, 'window_open')
        if isinstance(result, dict) and result.get('error') in ('already_activated', 'in_progress'):
            return _answer(ALREADY_ACTIVATED.format(n=shown), False, note, number, row, 'already_activated')
        if isinstance(result, dict) and result.get('error') in ('manual_order', 'manual_plan', 'not_supported', 'too_old'):
            return _answer(PAID_OTHER.format(n=shown), False,
                           note + '. Магазин не выдаёт повторную автоматически: ' + str(result.get('msg') or result.get('error'))[:200],
                           number, row, 'paid_other', owner_alert=True)
        if isinstance(result, dict) and result.get('error') == 'already_sent':
            return _answer(WINDOW_OPEN.format(n=shown), False,
                           note + '. Повторная активация уже выдавалась за последние сутки.', number, row, 'already_sent')
        reason = str((result or {}).get('msg') or (result or {}).get('error') or 'магазин не выполнил выдачу')
        return _answer(REACTIVATION_BY_OWNER.format(n=shown), False,
                       note + '. Нужна повторная активация — выдать не удалось: ' + reason[:200], number, row,
                       'reactivation_needed', owner_alert=True)
    return _answer(PAID_OTHER.format(n=shown), False, note, number, row, 'paid_other', owner_alert=True)


def _answer(reply, handoff, note, number, row, outcome, owner_alert=False):
    evidence = {'status': 'catalog_direct', 'sources': [], 'reply_class': 'instruct', 'owner_text_used': True,
                'owner_approved_wording': True, 'order_action': outcome, 'order_number': number,
                'diagnostic': note, 'owner_alert': bool(owner_alert)}
    if handoff:
        evidence.update(status='owner_policy', reply_class='outcome', handoff_reason=note)
    return {'reply': reply, 'handoff': handoff, 'source_ids': [],
            'summary': 'Проверен заказ клиента: ' + note, 'evidence': evidence}
