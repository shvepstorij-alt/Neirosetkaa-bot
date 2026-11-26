"""Narrow authenticated action for the support assistant: repeat activation.

POST /api/support/resend_activation — the assistant may re-send the activation
button for ONE paid ChatGPT/Claude/Perplexity order of ONE client, only when the
activation window has closed and nothing shows that activation happened. It is
the same action as the admin button «Отправить кнопку активации ещё раз»
(common.gpt_resend_activation), limited to once per order per 24 hours.

All IO is injected by the shop (fetch/claim/resend/notify); this module never
imports common/db, so the eligibility logic and auth are testable without a DB.
The response never carries codes, tokens, links or emails.
"""
import asyncio
import hmac
import html
import json
import logging
import os
import re

TOKEN_ENV = 'SUPPORT_SHOP_ACTION_TOKEN'
MAX_BODY = 2048
# Owner 21.11.2026: a repeat request is re-checked in the shop every time; a new activation is issued when the
# previous one was not used. The claim only covers the activation window (ACTIVATION_WINDOW_MIN = 120).
COOLDOWN_SECONDS = 2 * 3600
MAX_ORDER_AGE_DAYS = 30
SERVICES = ('chatgpt', 'claude', 'perplexity')
CLAIM_PREFIX = 'support_resend:'

# Mirrors support_agent/clients.py ShopReader._activation_signals (read only).
_TABLES = {'chatgpt': ('gpt_pending_activations', 'gpt_codes', True),
           'claude': ('claude_pending_activations', 'claude_codes', False),
           'perplexity': ('perplexity_pending_activations', 'perplexity_codes', False)}
_LINKPAY_OPEN = ('awaiting_link', 'awaiting_payment', 'awaiting_creds', 'awaiting_setup')
_NSG_STATES = ('pending', 'success', 'delivered', 'done')
# activation_help phases that still allow a fresh button: the client's own
# input was rejected and the attempt is over. Everything else is a live or
# reviewed attempt and must not be overridden.
_ACTHELP_RETRYABLE = ('input', 'confirm')
_ORDER_ID = re.compile(r'shop_\d{1,20}_[A-Za-z0-9]{1,40}')
_DIGITS = re.compile(r'\d{1,20}')

MSG = {
    'not_found': 'Заказ с таким номером у этого клиента не найден.',
    'ambiguous': 'По этому номеру найдено несколько заказов — нужен точный номер заказа.',
    'not_paid': 'Заказ не в статусе «оплачен» — повторную активацию не выдаю.',
    'not_supported': 'Повторная активация выдаётся только для ChatGPT, Claude и Perplexity.',
    'manual_plan': 'Для этого тарифа самостоятельная активация не предусмотрена — заказ выполняется вручную.',
    'manual_order': 'Заказ оформляется вручную — повторная кнопка не нужна.',
    'unavailable': 'Не удалось проверить состояние активации — повторно не выдаю, передай владельцу.',
    'already_activated': 'По заказу уже записана активация или он отмечен выполненным — повторно не выдаю.',
    'in_progress': 'Активация по заказу уже запускалась — повторно не выдаю, результат проверит владелец.',
    'window_open': 'Окно активации ещё открыто — клиенту достаточно нажать кнопку активации в чате с ботом.',
    'already_sent': 'Повторная активация по этому заказу уже выдавалась менее 2 часов назад — прежняя кнопка ещё действует.',
    'too_old': 'Заказ оплачен больше 30 дней назад — повторную активацию выдаёт только владелец.',
    'read_error': 'Не удалось прочитать заказ — повторно не выдаю, передай владельцу.',
    'busy': 'Сервис занят, повтори запрос через минуту.',
    'resend_failed': 'Повторная активация не выдана.',
}
OK_MSG = 'Новая кнопка активации отправлена клиенту в чат с ботом.'


def _fail(code, msg=None):
    return {'ok': False, 'error': code, 'msg': msg or MSG[code]}


# ── Pure parts ────────────────────────────────────────────────────────────────

def check_auth(header, token):
    """None when authorised, else (http_status, payload)."""
    if not isinstance(token, str) or len(token) < 32:
        return 503, {'ok': False, 'error': 'disabled'}
    supplied = header if isinstance(header, str) else ''
    if len(supplied) > 512 or not hmac.compare_digest(supplied.encode(), ('Bearer ' + token).encode()):
        return 401, {'ok': False, 'error': 'auth'}
    return None


def parse_body(raw):
    """(user_id, order_ref) or raises ValueError(code) with code in body/user_id/order."""
    try:
        body = json.loads(bytes(raw))
    except (ValueError, UnicodeDecodeError):
        raise ValueError('body')
    if not isinstance(body, dict):
        raise ValueError('body')
    uid = body.get('user_id')
    if type(uid) is not int or not 0 < uid < 2 ** 63:
        raise ValueError('user_id')
    ref = body.get('order')
    if type(ref) is int and 0 < ref < 10 ** 20:
        ref = str(ref)
    if not isinstance(ref, str):
        raise ValueError('order')
    ref = ref.strip().lstrip('#')
    if not (_DIGITS.fullmatch(ref) or _ORDER_ID.fullmatch(ref)):
        raise ValueError('order')
    if ref.isdigit() and int(ref) == 0:
        raise ValueError('order')
    return uid, ref


def order_clause(ref):
    """WHERE fragment for $2. Digits match num / fk_intid exactly (as ShopReader
    'untyped'), a full shop order id matches order_id. user_id=$1 is always added."""
    if ref.isdigit():
        return '(num::text=$2 OR fk_intid=$2)'
    return 'order_id=$2'


def service_of(pack):
    parts = str(pack or '').split(':')
    if len(parts) >= 3 and parts[0] == 'shop' and parts[1] in SERVICES:
        return parts[1]
    return ''


def eligibility(order, signals):
    """Decide from already-read facts. Returns {'ok':True,...} or a failure dict.

    order: None or dict with status, pack; signals: dict (see fetch_facts).
    `fulfilled` (fk_orders.fulfilled_at) is deliberately NOT a blocker here: the
    shop sets it as soon as the activation BUTTON is delivered, so it does not
    mean the subscription was activated.
    """
    signals = signals if isinstance(signals, dict) else {}
    if signals.get('ambiguous'):
        return _fail('ambiguous')
    if not order:
        return _fail('not_found')
    if str(order.get('status') or '').casefold() != 'paid':
        return _fail('not_paid')
    service = service_of(order.get('pack'))
    if not service:
        return _fail('not_supported')
    if signals.get('lookup') != 'ok':
        return _fail('unavailable')
    if signals.get('recently_sent'):
        return _fail('already_sent')
    if signals.get('code_used') or signals.get('marked_done'):
        return _fail('already_activated')
    if signals.get('activation_started'):
        return _fail('in_progress')
    phase = signals.get('acthelp_phase')
    if phase and phase not in _ACTHELP_RETRYABLE:
        return _fail('in_progress')
    if signals.get('pending_open'):
        return _fail('window_open')
    if signals.get('linkpay_status') or signals.get('nsgifts_status'):
        return _fail('manual_order')
    if service == 'chatgpt' and signals.get('auto_plan') is False:
        return _fail('manual_plan')
    if signals.get('too_old'):
        return _fail('too_old')
    return {'ok': True, 'error': None, 'msg': '', 'service': service}


_TAGS = re.compile(r'<[^>]*>')


def redact(text, limit=300):
    """Shop/admin text → safe short text: no codes, links, emails, tags."""
    s = str(text or '')
    s = re.sub(r'<code>.*?</code>', '', s, flags=re.S | re.I)
    s = html.unescape(_TAGS.sub('', s))
    s = re.sub(r'(?i)\b(?:https?://|www\.|t\.me/)\S*', '', s)
    s = re.sub(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+', '', s)
    s = re.sub(r'\((?:код|code)\b[^)]*\)', '', s, flags=re.I)
    s = '\n'.join(line for line in s.splitlines() if not re.search(r'(?i)\b(?:код|code|token|токен)\s*:', line))
    s = re.sub(r'(?i)\b(по\s+)?(код[уа]?|code)\s+[^\s,.;:—]+', lambda m: (m.group(1) or '') + m.group(2), s)
    s = re.sub(r'\b(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{8,}\b', '', s)
    s = re.sub(r'\b[A-Z0-9-]{10,}\b', '', s)
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\s*\n\s*', ' ', s).strip(' ·,;—-')
    return s[:limit]


# ── IO helpers wired by the shop ─────────────────────────────────────────────

async def fetch_facts(pool, uid, ref, *, plan_info=None):
    """Read one owned order and its activation signals. Returns (order|None, signals).

    Ownership is enforced in SQL (user_id=$1). Same signal names as
    support_agent ShopReader._activation_signals plus shop-only ones:
    marked_done (settings order_done:<id>='1'), acthelp_phase, recently_sent,
    too_old, auto_plan.
    """
    sql = ('SELECT order_id,num,fk_intid,status,pack,paid_at,fulfilled_at,'
           " (paid_at IS NOT NULL AND paid_at < NOW() - make_interval(days => " + str(int(MAX_ORDER_AGE_DAYS)) + ")) AS too_old"
           ' FROM fk_orders WHERE user_id=$1 AND ' + order_clause(ref) + ' LIMIT 2')
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, uid, ref)
        if len(rows) > 1:
            return None, {'ambiguous': True}
        if not rows:
            return None, {}
        order = dict(rows[0])
        signals = {'lookup': 'ok', 'fulfilled': bool(order.get('fulfilled_at')),
                   'too_old': bool(order.get('too_old'))}
        oid = str(order.get('order_id') or '')
        service = service_of(order.get('pack'))
        if str(order.get('status') or '').casefold() != 'paid' or not service:
            return order, signals
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,80}', oid):
            signals['lookup'] = 'unavailable'
            return order, signals
        try:
            link = await conn.fetchrow(
                'SELECT status FROM linkpay_orders WHERE fk_order_id=$1 AND user_id=$2', oid, uid)
            if link and link['status'] in _LINKPAY_OPEN:
                signals['linkpay_status'] = str(link['status'])
            gift = await conn.fetchrow(
                'SELECT status FROM nsgifts_orders WHERE fk_order_id=$1 AND user_id=$2', oid, uid)
            if gift and gift['status'] in _NSG_STATES:
                signals['nsgifts_status'] = str(gift['status'])
            pending, codes, started = _TABLES[service]
            started_sql = '(activating_at IS NOT NULL)' if started else 'FALSE'
            prow = await conn.fetchrow(
                'SELECT (expires_at>NOW()) AS open, ' + started_sql + ' AS started FROM ' + pending +
                ' WHERE order_id=$1 AND user_id=$2', oid, uid)
            if prow:
                signals['pending_open'] = bool(prow['open'])
                signals['activation_started'] = bool(prow['started'])
            # Stricter than the export: any code of this order recorded as used.
            # (A reserved code has is_used=TRUE but used_by/used_at NULL.)
            signals['code_used'] = bool(await conn.fetchval(
                'SELECT 1 FROM ' + codes + ' WHERE order_id=$1 AND (used_at IS NOT NULL OR used_by IS NOT NULL) LIMIT 1',
                oid))
            signals['marked_done'] = (await conn.fetchval(
                'SELECT value FROM settings WHERE key=$1', 'order_done:' + oid) or '').strip() == '1'
            if service in ('chatgpt', 'claude'):
                import hashlib
                raw = await conn.fetchval(
                    'SELECT value FROM settings WHERE key=$1',
                    'acthelp:' + hashlib.sha256((service + ':' + oid).encode()).hexdigest())
                if raw:
                    try:
                        signals['acthelp_phase'] = str((json.loads(raw) or {}).get('phase') or 'pending')
                    except (ValueError, AttributeError):
                        signals['acthelp_phase'] = 'pending'
            signals['recently_sent'] = bool(await conn.fetchval(
                'SELECT 1 FROM activation_claims WHERE key=$1 AND claimed_at > NOW() - make_interval(secs => $2)',
                CLAIM_PREFIX + oid, COOLDOWN_SECONDS))
        except Exception as exc:
            logging.warning(f'support resend facts {oid}: {type(exc).__name__}')
            signals['lookup'] = 'unavailable'
            return order, signals
    if plan_info is not None:
        try:
            info = plan_info(order.get('pack')) or {}
            signals['auto_plan'] = info.get('auto')
            order['label'] = str(info.get('label') or '')[:120]
        except Exception:
            signals['auto_plan'] = None
    return order, signals


async def _read_body(request):
    raw = bytearray()
    async for chunk in request.content.iter_chunked(1024):
        raw.extend(chunk)
        if len(raw) > MAX_BODY:
            return None
    return bytes(raw)


def make_support_resend_handler(*, fetch, claim, resend, notify, json_response=None):
    """fetch(uid, ref) -> (order|None, signals); claim(key, seconds) -> 0 when
    granted else seconds left (db.activation_cooldown); resend(order_id) ->
    (ok, admin_text) (common.gpt_resend_activation); notify(text) sends to owner.
    """
    if json_response is None:
        from aiohttp import web
        json_response = web.json_response
    gate = asyncio.Semaphore(2)

    def reply(payload, status=200):
        return json_response(payload, status=status, headers={'Cache-Control': 'no-store'})

    async def process(request):
        denied = check_auth(request.headers.get('Authorization', ''), os.getenv(TOKEN_ENV, ''))
        if denied:
            return denied
        try:
            raw = await _read_body(request)
        except Exception:
            return 400, {'ok': False, 'error': 'body'}
        if raw is None:
            return 413, {'ok': False, 'error': 'size'}
        try:
            uid, ref = parse_body(raw)
        except ValueError as exc:
            return 400, {'ok': False, 'error': str(exc)}
        try:
            await asyncio.wait_for(gate.acquire(), timeout=2)
        except asyncio.TimeoutError:
            return 429, _fail('busy')
        try:
            return 200, await act(uid, ref)
        finally:
            gate.release()

    async def act(uid, ref):
        try:
            order, signals = await asyncio.wait_for(fetch(uid, ref), timeout=10)
        except Exception as exc:
            logging.warning(f'support resend fetch uid={uid}: {type(exc).__name__}')
            return _fail('read_error')
        verdict = eligibility(order, signals)
        if not verdict['ok']:
            logging.info(f'support resend uid={uid} ref={ref}: refused {verdict["error"]}')
            return {'ok': False, 'error': verdict['error'], 'msg': verdict['msg']}
        oid = str(order['order_id'])
        try:
            left = await claim(CLAIM_PREFIX + oid, COOLDOWN_SECONDS)
        except Exception as exc:
            logging.warning(f'support resend claim {oid}: {type(exc).__name__}')
            return _fail('read_error')
        if left:
            return _fail('already_sent')
        raised = False
        try:
            ok, text = await resend(oid)
        except Exception as exc:
            ok, text, raised = False, f'{type(exc).__name__}: {exc}', True
            logging.error(f'support resend {oid}: {exc}', exc_info=True)
        ok = ok is True
        logging.warning(f'support resend {oid} uid={uid}: ok={ok} msg={redact(text, 200)}')
        num = order.get('num')
        label = order.get('label') or verdict['service']
        note = (f"🤖 Помощник выдал повторную активацию · заказ #{html.escape(str(num if num is not None else oid))}"
                f" · {html.escape(str(label))} · " + ('ok' if ok else 'ошибка') + f": {text}")
        try:
            await asyncio.wait_for(notify(note), timeout=10)
        except Exception as exc:
            logging.warning(f'support resend notify {oid}: {type(exc).__name__}')
        if ok:
            return {'ok': True, 'error': None, 'msg': OK_MSG}
        safe = '' if raised else redact(text)
        if 'вручную' in safe.casefold():
            return {'ok': False, 'error': 'manual_plan', 'msg': safe}
        return {'ok': False, 'error': 'resend_failed', 'msg': safe or MSG['resend_failed']}

    async def handler(request):
        status, payload = await process(request)
        return reply(payload, status)

    handler.process = process
    return handler
