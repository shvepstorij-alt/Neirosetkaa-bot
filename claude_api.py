"""Durable Claude code operations. No production imports at module load time."""
import hashlib
import json
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps

_connection = ContextVar('claude_operation_connection', default=None)


@asynccontextmanager
async def operation_guard(provider, code):
    """Session lock, not a transaction: intent must commit BEFORE the HTTP call."""
    key = operation_key(provider, code)
    active = _connection.get()
    if active and active[0] == key:
        yield
        return
    from db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        locked = await conn.fetchval('SELECT pg_try_advisory_lock(734021, hashtext($1))', key)
        if not locked:
            raise RuntimeError('Операция уже проверяется или изменяется; повторите позже.')
        token = _connection.set((key, conn))
        try:
            yield
        finally:
            _connection.reset(token)
            await conn.execute('SELECT pg_advisory_unlock(734021, hashtext($1))', key)


def serialized(fn):
    @wraps(fn)
    async def wrapped(provider, code, *args, **kwargs):
        async with operation_guard(provider, code):
            return await fn(provider, code, *args, **kwargs)
    return wrapped


async def recover_markers():
    """Recover watch records using the same per-code lock as retry/release."""
    from db import get_pool
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT value FROM settings WHERE key LIKE 'claudeop:%' "
                                "AND key NOT LIKE 'claudeop:%:%'")
    for row in rows:
        op = json.loads(row['value'])
        if op.get('state') not in ('released', 'rejected_input'):
            try:
                await recover_one(op['provider'], op['code'])
            except RuntimeError:
                continue  # Another worker owns this operation; next tick will retry.


@serialized
async def recover_one(provider, code):
    op = await read(provider, code)
    if not op or op.get('state') in ('released', 'rejected_input'):
        return
    conn = _connection.get()[1]
    pending = await conn.fetchrow(
        "SELECT p.* FROM claude_pending_activations p "
        "JOIN claude_codes c ON c.code=p.code "
        "WHERE p.code=$1 AND p.order_id=$2 AND c.used_by IS NULL AND c.is_used=TRUE "
        "AND NOT EXISTS (SELECT 1 FROM settings s WHERE s.key='claudecheck:' || UPPER(p.code)) "
        "AND NOT EXISTS (SELECT 1 FROM activation_claims a WHERE a.key='claude:' || p.user_id::text "
        "AND a.claimed_at > NOW() - INTERVAL '22 minutes')", op['code'], op['order_id'])
    if pending:
        fields = [pending['user_id'], op['order_id'], int(time.time()), op['provider'],
                  op['provider'], op.get('ref') or '', op['org'], pending['plan_name'], pending['plan']]
        value = '|'.join(str(x).replace('|', '/').replace('\n', ' ').strip() for x in fields)
        await put_setting('claudecheck:' + op['code'].strip().upper(), value)


async def put_setting(key, value):
    active = _connection.get()
    if active:
        await active[1].execute('INSERT INTO settings (key,value) VALUES ($1,$2) '
                               'ON CONFLICT (key) DO UPDATE SET value=$2', key, value)
    else:
        from db import set_setting
        await set_setting(key, value)


def operation_key(provider, code):
    return 'claudeop:' + hashlib.sha256((provider + ':' + code.strip().upper()).encode()).hexdigest()


async def read(provider, code):
    active = _connection.get()
    if active:
        raw = await active[1].fetchval('SELECT value FROM settings WHERE key=$1', operation_key(provider, code))
    else:
        from db import get_setting
        raw = await get_setting(operation_key(provider, code), '')
    return json.loads(raw) if raw else None


async def save(op):
    op['updated_at'] = time.time()
    await put_setting(operation_key(op['provider'], op['code']), json.dumps(op, ensure_ascii=False))


@serialized
async def start(provider, code, org, order_id):
    """Persist intent before any external mutation; ambiguous submissions never replay."""
    from config import CLAUDE_PROVIDERS
    import agent9527 as a9
    op = await read(provider, code)
    if op and op.get('state') not in ('released', 'rejected_input'):
        if op['order_id'] != order_id or op['org'] != org:
            return {'ok': False, 'err_kind': 'unclear', 'err_msg': 'Код связан с другой сохранённой операцией.'}
        if op.get('ref'):
            return {'ok': True, 'ref': op['ref']}
        return {'ok': False, 'err_kind': 'unclear', 'err_msg': op.get('reason') or 'Предыдущая отправка не подтверждена; повтор заблокирован.'}
    if provider != 'bpa':
        q = await a9.card_query(code, 'claude')
        if not q.get('ok'):
            return {'ok': False, 'err_kind': 'unclear', 'err_msg': str(q.get('code') or '') + ' ' + str(q.get('msg') or '')}
        v = a9.verdict(q.get('card'), q.get('order'))
        conflict = a9.product_conflict('claude', 'pro', v.get('project') or '')
        if conflict or not a9.code_free(v):
            return {'ok': False, 'err_kind': 'unclear', 'err_msg': conflict or 'Код не подтверждён свободным: ' + v['state']}
    op = dict(provider=provider, code=code, org=org, order_id=order_id,
              state='submitting', ref='', started_at=time.time(), reason='Запрос подготовлен; результат ещё не подтверждён.')
    op['idem'] = a9.idem_key('claude-operation', provider, order_id, code, org, uuid.uuid4().hex)
    op['client_order'] = 'cl-' + hashlib.sha256((order_id + ':' + code + ':' + org + ':' + op['idem']).encode()).hexdigest()[:48]
    if provider == 'bpa':
        op['body'] = {'code': code, 'org_id': org}
    else:
        op['body'] = {'cardCode': code, 'target': {'type': 'organization_id', 'value': org}, 'clientOrderNo': op['client_order']}
    await save(op)  # Fail closed if persistence is unavailable.
    try:
        if provider == 'bpa':
            import aiohttp
            base = CLAUDE_PROVIDERS[provider]['base']
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
                async with session.post(base + '/api/activate', json=op['body'], allow_redirects=False) as response:
                    op['http'] = response.status
                    try:
                        data = await response.json()
                    except Exception:
                        data = {}
                    if not isinstance(data, dict):
                        data = {}
                    op['reason'] = str(data.get('detail') or 'HTTP ' + str(response.status))
                    if response.status == 201 and data.get('order_id'):
                        op['ref'] = str(data['order_id'])
                        op['state'] = 'pending'
                        op['reason'] = 'Поставщик принял операцию.'
                    elif response.status == 409 and 'out of stock' in op['reason'].lower():
                        op['state'] = 'rejected_stock'
                    elif response.status == 400 and ('org_id' in op['reason'].lower() or 'uuid' in op['reason'].lower()):
                        op['state'] = 'rejected_input'
        else:
            result = await a9.redeem(code, 'organization_id', org, op['idem'], op['client_order'], svc='claude')
            op['reason'] = str(result.get('code') or '') + ' ' + str(result.get('msg') or '')
            if result.get('ok') and (result.get('order') or {}).get('orderNo'):
                op['ref'] = str(result['order']['orderNo'])
                op['state'] = 'pending'
                op['reason'] = str(result['order'].get('reason') or 'Поставщик принял операцию.')
    except Exception as exc:
        op['reason'] = 'Ответ не подтверждён: ' + type(exc).__name__
    await save(op)
    if op['ref']:
        return {'ok': True, 'ref': op['ref']}
    if op['state'] == 'rejected_input':
        return {'ok': False, 'err_kind': 'bad_org', 'err_msg': op['reason']}
    return {'ok': False, 'err_kind': 'unclear', 'err_msg': op['reason']}


@serialized
async def status(provider, code, ref, org):
    """Return a conservative state and retain the supplier's diagnostic text."""
    op = await read(provider, code)
    if not op:
        return None  # Caller must use the legacy transport for historical operations.
    if op['org'] != org:
        return 'mismatch'
    if ref and op.get('ref') and str(ref) != str(op['ref']):
        return 'mismatch'
    if op['state'] == 'released':
        return 'unknown'
    if op['state'] == 'success':
        return 'success'
    ref = op.get('ref') or ref
    state = 'unknown'
    reason = ''
    try:
        if provider == 'bpa':
            if not ref:
                # An interrupted POST may have succeeded. Recover by code, never repost.
                from common import _claude_bpa_code_status
                state = await _claude_bpa_code_status(code, org, op.get('started_at', 0))
                # An unused snapshot after an uncertain submission is not final proof.
                if state == 'free' and op['state'] != 'rejected_stock':
                    state = 'unknown'
                op['last_status'] = state
                if state == 'success':
                    op['state'] = 'success'
                op['checked_at'] = time.time()
                await save(op)
                return state
            import aiohttp
            from config import CLAUDE_PROVIDERS
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get(CLAUDE_PROVIDERS[provider]['base'] + '/api/activate/' + str(ref), allow_redirects=False) as response:
                    data = await response.json()
                    if response.status != 200 or not isinstance(data, dict):
                        raise ValueError('Invalid status response')
            reason = str(data.get('error') or data.get('status') or 'Причина не сообщена')
            if str(data.get('org_id') or '').lower() != org.lower() or str(data.get('order_id') or '') != str(ref):
                state = 'mismatch'
            elif not str(data.get('product') or '').startswith('claude_pro'):
                state = 'mismatch'
            else:
                state = {'done': 'success', 'failed': 'failed', 'running': 'pending', 'queued': 'pending'}.get(data.get('status'), 'unknown')
        else:
            import agent9527 as a9
            result = await a9.card_query(code, 'claude')
            if not result.get('ok'):
                reason = str(result.get('code') or '') + ' ' + str(result.get('msg') or '')
            else:
                order = result.get('order') or {}
                v = a9.verdict(result.get('card'), order, expect_order_no=str(ref or ''))
                reason = v.get('reason') or 'Поставщик не сообщил причину.'
                op['supplier'] = {k: order.get(k) for k in ('status', 'isFinal', 'autoRetryState', 'autoRetryCount', 'canRetry', 'retryCount', 'updatedAt')}
                if op.get('retry_pending') and str(order.get('orderNo') or '') == str(ref or ''):
                    if int(order.get('retryCount') or 0) > op.get('retry_before', 0):
                        op['retry_pending'] = False
                if not ref and order.get('clientOrderNo') == op['client_order'] and order.get('orderNo'):
                    op['ref'] = ref = str(order['orderNo'])
                if not ref or str(order.get('orderNo') or '') != str(ref):
                    state = 'unknown'
                elif v.get('target_org') and v['target_org'].lower() != org.lower():
                    state = 'mismatch'
                else:
                    state = {'success': 'success', 'pending': 'pending', 'review': 'review', 'failed': 'failed'}.get(v['state'], 'unknown')
                    if op.get('retry_pending') and state != 'success':
                        state = 'pending'
    except Exception as exc:
        reason = 'Проверка недоступна: ' + type(exc).__name__
    op['last_status'] = state
    if state == 'success':
        op['state'] = 'success'
    op['checked_at'] = time.time()
    op['reason'] = reason
    await save(op)
    return state


@serialized
async def transfer_allowed(provider, code, org, expected_order=''):
    """A failed purchase is not proof of an unspent code; only explicit release permits transfer."""
    op = await read(provider, code)
    if not op or op.get('org') != org or op.get('state') in ('released', 'success') or op.get('retry_pending'):
        return False
    if expected_order and op.get('order_id') != expected_order:
        return False
    if provider == 'bpa':
        if op.get('state') != 'rejected_stock':
            return False
        # A code may have been redeemed manually since the original rejection.
        from chatgpt_activation import bpa_query_codes
        current = await bpa_query_codes([code])
        return str((current.get(code.strip().upper()) or {}).get('status') or '').lower() == 'unused'
    if not op.get('ref'):
        return False  # A released historical order cannot settle an ambiguous new POST.
    import agent9527 as a9
    q = await a9.card_query(code, 'claude')
    if not q.get('ok'):
        return False
    card, order = q.get('card') or {}, q.get('order') or {}
    if str(order.get('orderNo') or '') != str(op['ref']):
        return False
    v = a9.verdict(card, order, expect_order_no=op.get('ref') or '')
    return bool(card.get('previousOrderReleased') and a9.code_free(v))


@serialized
async def record_release(provider, code, org='', expected_order=''):
    op = await read(provider, code)
    if op:
        if not await transfer_allowed(provider, code, org, expected_order):
            raise RuntimeError('Поставщик больше не подтверждает безопасный перенос.')
        await put_setting(operation_key(provider, code) + ':' + op['idem'], json.dumps(op, ensure_ascii=False))
        op['state'] = 'released'
        await save(op)
    else:
        raise RuntimeError('Исходная операция не найдена.')


@serialized
async def retry_original(provider, code, org, expected_order=''):
    """Explicit admin retry, never a new redemption or account change."""
    if provider != 'aichongz':
        return False, 'Этот поставщик не поддерживает такой повтор.'
    import agent9527 as a9
    op = await read(provider, code)
    if not op or not op.get('ref') or op.get('org') != org or op['state'] in ('released', 'success'):
        return False, 'Исходная операция не подтверждена.'
    if expected_order and op.get('order_id') != expected_order:
        return False, 'Код связан с другим заказом.'
    if op.get('retry_pending'):
        return False, 'Результат предыдущего запроса повтора ещё уточняется.'
    if time.time() < op.get('retry_not_before', 0):
        return False, 'Поставщик ограничил частоту запросов. Повторите позже.'
    q = await a9.card_query(code, 'claude')
    if not q.get('ok'):
        return False, 'Не удалось проверить исходную операцию.'
    order = q.get('order') or {}
    v = a9.verdict(q.get('card'), order, expect_order_no=op['ref'])
    if v['state'] != 'failed' or not order.get('canRetry') or str(order.get('orderNo') or '') != op['ref']:
        return False, 'Поставщик не разрешил повтор исходного заказа.'
    if v.get('target_org') and v['target_org'].lower() != org.lower():
        return False, 'Organization ID не совпадает.'
    op['retry_pending'] = True
    op['retry_before'] = int(order.get('retryCount') or 0)
    op['retry_idem'] = a9.idem_key('claude-retry', op['ref'], uuid.uuid4().hex)
    op['reason'] = 'Запрошен повтор исходного заказа; результат уточняется.'
    await save(op)
    from urllib.parse import quote
    result = await a9.post(a9.PREFIX + '/redemptions/' + quote(op['ref'], safe='') + '/retry',
                           a9.body_bytes({}), idem=op['retry_idem'], tries=1, timeout=60, svc='claude')
    op['reason'] = str(result.get('code') or '') + ' ' + str(result.get('msg') or '')
    returned = (result.get('data') or {}).get('order') or {}
    if result.get('ok') and str(returned.get('orderNo') or '') == op['ref']:
        op['reason'] = 'Поставщик принял повтор исходного заказа.'
    else:
        op['reason'] = 'Результат повтора уточняется. ' + op['reason']
    if result.get('http') == 429 or result.get('code') == 'RATE_LIMITED':
        op['retry_pending'] = False
        op['retry_not_before'] = time.time() + max(result.get('retry_after') or 5, 1)
        op['reason'] = 'Повтор не принят из-за лимита запросов. Код не отправлялся повторно.'
    # A stale query must not enable another retry until the counter changes.
    await save(op)
    return True, op['reason']
