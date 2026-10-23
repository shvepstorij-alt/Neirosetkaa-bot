"""Client activation guidance. Never redeem, retry, release or replace a code."""
import asyncio
import hashlib
import html
import json
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

PREFIX = 'acthelp:'
SERVICES = {'chatgpt': 'gpt', 'claude': 'claude'}
WAIT = ('Подтверждение ещё не получено. Подожди до 30 минут с начала активации: '
        'подписка может появиться автоматически. Оплата сохранена. '
        'Повторять активацию и оплачивать заново не нужно. Результат придёт в чат бота.')
LATE = ('Подтверждение пока не получено. Заказ передан на проверку. '
        'Писать отдельно в личку и повторять активацию не нужно. Ответ придёт в чат бота.')


def key(service, oid):
    return PREFIX + hashlib.sha256((service + ':' + oid).encode()).hexdigest()


async def read(conn, k):
    raw = await conn.fetchval('SELECT value FROM settings WHERE key=$1', k)
    return json.loads(raw) if raw else None


async def write(conn, k, record):
    await conn.execute('INSERT INTO settings (key,value) VALUES ($1,$2) '
                       'ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value',
                       k, json.dumps(record, ensure_ascii=False))


@asynccontextmanager
async def locked(k):
    from db import get_pool
    async with (await get_pool()).acquire() as conn:
        await conn.execute('SELECT pg_advisory_lock(734024, hashtext($1))', k)
        try:
            yield conn
        finally:
            await conn.execute('SELECT pg_advisory_unlock(734024, hashtext($1))', k)


async def track(service, uid, oid, code, ref, client_code=''):
    """Persist before launching the job. Reopening a screen never resets the clock."""
    k = key(service, oid)
    async with locked(k) as conn:
        r = await read(conn, k) or {}
        started = r.get('started') if r.get('phase') not in ('input', 'confirm') else None
        r.update(service=service, uid=int(uid), oid=oid, code=code, ref=str(ref),
                 started=started or time.time(), phase='pending', error='', finished=False)
        r['client_code'] = client_code or code
        await write(conn, k, r)


def classify(job):
    if not job:
        return 'pending'
    if job.get('need_force'):
        return 'confirm'
    if job.get('manual'):
        return 'manual'
    if job.get('pending') or job.get('watching'):
        return 'pending'
    if job.get('success') is True:
        return 'success'
    if job.get('status') in ('done', 'failed'):
        return 'input' if job.get('kind') in ('bad_org', 'has_plan', 'token') or job.get('token_invalid') else 'review'
    return 'pending'


async def observe(service, oid, ref, job):
    k = key(service, oid)
    async with locked(k) as conn:
        r = await read(conn, k)
        if r and r.get('ref') == str(ref):
            r['phase'] = classify(job)
            r['error'] = str((job or {}).get('error') or '')[:1500]
            await write(conn, k, r)


async def run(service, oid, ref, coroutine):
    try:
        return await coroutine
    finally:
        try:
            import common as c
            jobs = c._activation_jobs if service == 'chatgpt' else c._claude_job_results
            await observe(service, oid, ref, jobs.get(ref))
        except Exception:
            logging.exception('activation help: could not persist job outcome')


async def snapshot(conn, r):
    import common as c
    table = SERVICES[r['service']]
    order = await conn.fetchrow('SELECT * FROM fk_orders WHERE order_id=$1 AND user_id=$2', r['oid'], r['uid'])
    if not order or order['status'] != 'paid' or not str(order['pack'] or '').startswith('shop:' + r['service'] + ':'):
        return {'state': 'closed', 'message': 'Заказ недоступен для активации.'}
    done = await conn.fetchval(f'SELECT 1 FROM {table}_codes WHERE order_id=$1 AND used_by=$2 AND used_at IS NOT NULL LIMIT 1', r['oid'], r['uid'])
    manual = await conn.fetchval("SELECT value FROM settings WHERE key=$1", 'order_done:' + r['oid'])
    if done or manual == '1':
        return {'state': 'success', 'message': 'Заказ отмечен как выполненный. Открой сервис и проверь аккаунт.'}
    jobs = c._activation_jobs if r['service'] == 'chatgpt' else c._claude_job_results
    job = jobs.get(r.get('ref'))
    phase = classify(job) if job else r.get('phase', 'pending')
    # Only the durable delivery record can confirm completion to the client.
    if phase == 'success':
        phase = 'pending'
    if phase in ('input', 'confirm'):
        return {'state': phase, 'message': str((job or {}).get('error') or r.get('error') or 'Проверь данные аккаунта на экране активации.')}
    if r.get('admin_sent'):
        return {'state': 'review', 'message': LATE}
    if phase in ('manual', 'review'):
        return {'state': 'manual', 'message': 'Для заказа требуется проверка. Повторять активацию не нужно.'}
    return {'state': 'pending', 'message': WAIT, 'started_at': r.get('started'),
            'review_due': bool(r.get('started') and time.time() >= r['started'] + 1800)}


async def notify(conn, k, r, reason, tell_client=False):
    """Serialize and save each delivery receipt separately; retry failed delivery only."""
    from config import bot, ADMIN_ID
    import common as c
    r['requested'] = r.get('requested') or reason
    r['finished'] = False
    await write(conn, k, r)
    if not r.get('admin_sent'):
        context = (await c._adm_gpt_ctx(r['oid'], code=r['code'], uid=r['uid'])
                   if r['service'] == 'chatgpt' else
                   await c._adm_claude_ctx(order_id=r['oid'], code=r['code'], uid=r['uid']))
        who = await c._who_user(r['uid'])
        text = ('🆘 <b>Проверка активации ' + ('ChatGPT' if r['service'] == 'chatgpt' else 'Claude')
                + '</b>\n\n👤 ' + who + '\n' + context + '\n\n' + html.escape(r['requested']))
        if r['service'] == 'chatgpt':
            from gpt_order_card import send
            receipt = await send(r['oid'], ADMIN_ID, text, parse_mode='HTML')
        else:
            from service_order_card import update
            receipt = await update(r['oid'], text, force_attention=True)
        r['admin_sent'] = int(receipt.message_id)
        await write(conn, k, r)
    if tell_client and not r.get('client_sent'):
        label = await c._order_ref_line(r['oid'])
        message = ('Обращение зарегистрировано. Данные заказа переданы Александру. '
                   'Ответ придёт сюда; писать отдельно в личку не нужно.' if r.get('user_report') else LATE)
        receipt = await bot.send_message(r['uid'], '⏳ <b>Проверка активации</b>\n\n' + message + '\n\n' + label, parse_mode='HTML')
        r['client_sent'] = int(receipt.message_id)
        await write(conn, k, r)


async def locate(conn, service, uid, code, ref=''):
    """Bind the exact supplied code to its owner's order; never select 'latest order'."""
    table = SERVICES[service]
    if ref:
        rows = await conn.fetch("SELECT value FROM settings WHERE key LIKE 'acthelp:%'")
        for row in rows:
            r = json.loads(row['value'])
            if r.get('service') == service and r.get('uid') == uid and r.get('ref') == ref:
                return r['oid']
        return None
    row = await conn.fetchrow(f'SELECT order_id FROM {table}_pending_activations WHERE user_id=$1 AND UPPER(code)=UPPER($2)', uid, code)
    if not row:
        row = await conn.fetchrow(f'SELECT order_id FROM {table}_codes WHERE used_by=$1 AND UPPER(code)=UPPER($2)', uid, code)
    if row and row['order_id']:
        return row['order_id']
    # The original code can change during a provider switch; persisted ownership remains.
    rows = await conn.fetch("SELECT value FROM settings WHERE key LIKE 'acthelp:%'")
    for row in rows:
        r = json.loads(row['value'])
        if (r.get('service') == service and r.get('uid') == uid and
                code.upper() in (str(r.get('code') or '').upper(), str(r.get('client_code') or '').upper())):
            return r['oid']
    return None


async def handler(request):
    from aiohttp import web
    from db import get_pool
    import common as c
    try:
        body = await request.json()
        if not isinstance(body, dict):
            raise ValueError()
        service, code, action = body.get('service'), str(body.get('code') or '').strip(), body.get('action', 'status')
        ref = str(body.get('job_ref') or '')
        if service not in SERVICES or not code or len(code) > 200 or len(ref) > 200 or action not in ('status', 'support', 'missing'):
            return web.json_response({'error': 'Некорректный запрос.'}, status=400)
        uid = c._verify_tg_init_data(str(body.get('init_data') or ''))
        if not uid:
            return web.json_response({'error': 'Открой активацию заново из чата бота, чтобы проверить заказ.'}, status=403)
        async with (await get_pool()).acquire() as conn:
            oid = await locate(conn, service, int(uid), code, ref)
        if not oid:
            return web.json_response({'error': 'Не удалось найти этот заказ. Открой его кнопкой из чата бота.'}, status=404)
        k = key(service, oid)
        async with locked(k) as conn:
            r = await read(conn, k) or dict(service=service, uid=int(uid), oid=oid, code=code, ref='', started=0, phase='manual')
            if r['uid'] != int(uid) or r['service'] != service:
                return web.json_response({'error': 'Заказ недоступен.'}, status=404)
            view = await snapshot(conn, r)
            if view['state'] == 'closed':
                return web.json_response(view)
            if action == 'support' or action == 'missing':
                if view['state'] == 'success' and action != 'missing':
                    return web.json_response(view)
                reason = ('Клиент сообщает: после подтверждения подписка не отображается.' if action == 'missing' else 'Клиент запросил помощь с активацией.')
                # A complaint AFTER success is a new incident, not the earlier delay alert.
                if action == 'missing' and r.get('request_kind') != 'missing':
                    r.pop('admin_sent', None)
                    r.pop('client_sent', None)
                    r.pop('requested', None)
                    r['request_kind'] = 'missing'
                r['user_report'] = True
                await notify(conn, k, r, reason)
                return web.json_response({'state': 'review', 'message': 'Обращение зарегистрировано. Данные заказа переданы Александру. Ответ придёт в чат бота; писать отдельно не нужно.'})
            if view.get('review_due'):
                await notify(conn, k, r, 'Прошло 30 минут с начала активации, подтверждения нет.', True)
                view = await snapshot(conn, r)
            return web.json_response(view)
    except Exception:
        logging.exception('activation help request failed')
        return web.json_response({'error': 'Не удалось проверить заказ или зарегистрировать обращение. Попробуй проверить статус чуть позже; активацию повторять не нужно.'}, status=503)


async def tick():
    from db import get_pool
    async with (await get_pool()).acquire() as conn:
        rows = await conn.fetch("SELECT key, value FROM settings WHERE key LIKE 'acthelp:%'")
    for row in rows:
        try:
            k = row['key']
            if json.loads(row['value']).get('finished'):
                continue
            async with locked(k) as conn:
                r = await read(conn, k)
                view = await snapshot(conn, r)
                if r.get('user_report') and r.get('requested') and view['state'] != 'closed':
                    await notify(conn, k, r, r['requested'], True)
                    r['finished'] = True
                    await write(conn, k, r)
                elif view['state'] in ('success', 'closed', 'input', 'confirm'):
                    r['finished'] = True
                    await write(conn, k, r)
                elif r.get('requested') or (r.get('started') and time.time() >= r['started'] + 1800):
                    await notify(conn, k, r, 'Прошло 30 минут с начала активации, подтверждения нет.', True)
                    r['finished'] = True
                    await write(conn, k, r)
        except Exception:
            logging.exception('activation help delivery failed; will retry')


async def lifecycle(app):
    async def loop():
        while True:
            try:
                await tick()
            except Exception:
                logging.exception('activation help background check failed')
            await asyncio.sleep(60)
    task = asyncio.create_task(loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


def install(app):
    from aiohttp import web
    async def script(request):
        return web.FileResponse(Path(__file__).with_name('activation_help.js'), headers={'Cache-Control': 'no-cache'})
    app.router.add_get('/webapp/activation-help.js', script)
    app.router.add_post('/api/activation-help', handler)
    app.cleanup_ctx.append(lifecycle)
