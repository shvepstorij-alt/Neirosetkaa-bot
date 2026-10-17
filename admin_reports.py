"""Daily Telegram summary and stock alerts. No provider calls or stock mutations."""
import asyncio
import datetime as dt
import json
import logging

TZ = dt.timezone(dt.timedelta(hours=5), 'Asia/Qyzylorda')
STOCK = {
    'go': ('ChatGPT Go', 10),
    'plus_ios': ('ChatGPT Plus · iOS', 20),
    'plus_ph': ('ChatGPT Plus · Филиппины', 20),
    'claude': ('Claude · все тарифы', 10),
    'perplexity': ('Perplexity · все тарифы', 5),
}

# Receipts survive a transient settings write error within the running process.
# Telegram and PostgreSQL cannot commit one transaction: a process crash before
# persisting Telegram's response can still cause a duplicate on restart.
_delivery_receipts = {}


async def deliver(key, text):
    from config import bot, ADMIN_ID
    from db import get_setting, set_setting
    receipt = _delivery_receipts.get(key)
    if receipt is None:
        receipt = json.loads(await get_setting(key, '{}') or '{}')
    if receipt.get('text') != text:
        if receipt.get('message_id'):
            try:
                await bot.edit_message_text(chat_id=ADMIN_ID,
                    message_id=receipt['message_id'], text=text, parse_mode='HTML')
            except Exception as exc:
                if 'message is not modified' not in str(exc).lower():
                    raise  # Never send a duplicate when editing fails.
        else:
            message = await bot.send_message(ADMIN_ID, text, parse_mode='HTML')
            receipt['message_id'] = message.message_id
        receipt['text'] = text
    _delivery_receipts[key] = receipt
    # Retrying the DB write does not repeat the Telegram operation.
    for attempt in range(3):
        try:
            await set_setting(key, json.dumps(receipt, ensure_ascii=False))
            _delivery_receipts.pop(key, None)
            return
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(0.2)


async def daily_reports(conn, now, counts):
    from db import get_setting, set_setting
    now = now.astimezone(TZ)
    cursor_key = 'admin_daily_report_closed_day'
    cursor = await get_setting(cursor_key, '')
    if not cursor:
        legacy = await get_setting('admin_daily_report_day', '')
        # Old reports ended before midnight. Recalculate the last old day fully.
        base = dt.date.fromisoformat(legacy) if legacy else now.date()
        cursor = (base - dt.timedelta(days=1)).isoformat()
        await set_setting(cursor_key, cursor)
    next_day = dt.date.fromisoformat(cursor) + dt.timedelta(days=1)
    # Recover oldest days first, bounded to avoid monopolizing the worker.
    for _ in range(3):
        if next_day >= now.date():
            break
        start = dt.datetime.combine(next_day, dt.time(), TZ)
        end = start + dt.timedelta(days=1)
        key = 'admin_daily_card:' + next_day.isoformat()
        await deliver(key, await build_report(conn, start, end, counts))
        await set_setting(cursor_key, next_day.isoformat())
        next_day += dt.timedelta(days=1)
    # A preview at the requested time; midnight finalizes that same message.
    if now.time() >= dt.time(23, 59):
        key = 'admin_daily_card:' + now.date().isoformat()
        if key not in _delivery_receipts and await get_setting(key, ''):
            return
        start = dt.datetime.combine(now.date(), dt.time(), TZ)
        text = await build_report(conn, start, now, counts)
        await deliver(key, text + '\n\n⏳ После полуночи эта сводка обновится итогами полного дня.')


def report_window(now):
    """Latest due report: today at 23:59, or yesterday after a restart."""
    now = now.astimezone(TZ)
    day = now.date() if now.time() >= dt.time(23, 59) else now.date() - dt.timedelta(days=1)
    start = dt.datetime.combine(day, dt.time(), TZ)
    end = min(now, start + dt.timedelta(days=1))
    return day.isoformat(), start, end


def stock_level(count, threshold):
    return 'empty' if count == 0 else ('low' if count < threshold else 'ok')


def stock_text(counts):
    lines = []
    for key, (name, threshold) in STOCK.items():
        count = counts[key]
        level = stock_level(count, threshold)
        icon = {'empty': '🔴', 'low': '🟠', 'ok': '🟢'}[level]
        lines.append(f'{icon} {name}: <b>{count}</b>' + (' — пополнить' if level != 'ok' else ''))
    return '\n'.join(lines)


async def read_stock(conn):
    from config import GPT_ENABLED_PROVIDERS, CLAUDE_PROVIDER_ORDER, AICHONGZ_READY
    gpt = await conn.fetch(
        "SELECT plan, COALESCE(route,'') AS route, COUNT(*) AS n FROM gpt_codes "
        "WHERE NOT is_used AND provider=ANY($1::text[]) "
        "AND COALESCE(check_status,'unchecked') NOT IN ('used','invalid') "
        "GROUP BY plan, COALESCE(route,'')", list(GPT_ENABLED_PROVIDERS))
    counts = dict.fromkeys(STOCK, 0)
    for row in gpt:
        if row['plan'] == 'go':
            counts['go'] += int(row['n'])
        elif row['plan'] == 'plus' and row['route'] in ('ios', 'ph'):
            counts['plus_' + row['route']] += int(row['n'])
    providers = [p for p in CLAUDE_PROVIDER_ORDER if p != 'aichongz' or AICHONGZ_READY]
    counts['claude'] = int(await conn.fetchval(
        "SELECT COUNT(*) FROM claude_codes WHERE NOT is_used AND provider=ANY($1::text[]) "
        "AND COALESCE(check_status,'unchecked') NOT IN ('used','invalid')", providers))
    counts['perplexity'] = int(await conn.fetchval(
        "SELECT COUNT(*) FROM perplexity_codes WHERE NOT is_used "
        "AND COALESCE(check_status,'unchecked') NOT IN ('used','invalid')"))
    return counts


async def build_report(conn, start, end, counts):
    # Reuse the financial screen's currency and database timezone rules.
    from common import _fin_collect, _fin_cfg, _fin_db_offset, _fin_to_db
    offset = await _fin_db_offset(conn)
    money = (await _fin_collect(conn, start, end, offset, await _fin_cfg(conn), detail=False))['totals']
    lines = [f"📊 <b>Сводка за {start:%d.%m.%Y}</b>",
             ('По Кызылорде · полный день, 00:00–24:00'
              if end == start + dt.timedelta(days=1)
              else f"По Кызылорде · с 00:00 до {end:%H:%M}"), '',
             f"Оплаченных заказов: <b>{money['orders']}</b>",
             f"Поступления FreeKassa: <b>{money['money']:,.0f} ₽</b>", '',
             '<b>Подтверждённые активации за период</b>']
    for table, name in (('gpt_codes', 'ChatGPT'), ('claude_codes', 'Claude'),
                        ('perplexity_codes', 'Perplexity')):
        count = await conn.fetchval(
            f"SELECT COUNT(DISTINCT COALESCE(NULLIF(order_id,''), code)) FROM {table} "
            "WHERE used_by IS NOT NULL AND used_at >= $1 AND used_at < $2",
            start, end)  # used_at is TIMESTAMPTZ, unlike fk_orders.paid_at.
        lines.append(f'{name}: <b>{count}</b>')
    pending = await conn.fetchval(
        "SELECT COUNT(*) FROM fk_orders f WHERE f.status='paid' "
        "AND f.paid_at >= $1 AND f.paid_at < $2 "
        "AND split_part(f.pack, ':', 2) IN ('chatgpt','claude','perplexity') "
        "AND f.pack LIKE 'shop:%' "
        "AND NOT EXISTS (SELECT 1 FROM settings s WHERE s.key='order_done:'||f.order_id AND s.value='1') "
        "AND NOT EXISTS (SELECT 1 FROM gpt_codes g WHERE g.order_id=f.order_id AND g.used_by IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM claude_codes c WHERE c.order_id=f.order_id AND c.used_by IS NOT NULL) "
        "AND NOT EXISTS (SELECT 1 FROM perplexity_codes p WHERE p.order_id=f.order_id AND p.used_by IS NOT NULL)",
        _fin_to_db(start, offset), _fin_to_db(end, offset))
    lines += ['', f'Из оплат за день ещё без подтверждённой активации: <b>{pending}</b>',
              'Это также заказы, где клиент ещё не начал активацию.']
    lines += ['', '<b>Свободные коды сейчас</b>', stock_text(counts),
              '', 'Резервы, использованные и невалидные коды в запас не входят.',
              'Plus показан отдельно по маршрутам iOS и Филиппины.']
    return '\n'.join(lines)


async def reports_tick(now=None):
    from config import bot, ADMIN_ID
    from db import get_pool, get_setting, set_setting
    now = now or dt.datetime.now(TZ)
    pool = await get_pool()
    # A session lock avoids duplicate sends during overlapping deployments.
    # PostgreSQL releases it automatically when the connection is lost.
    async with pool.acquire() as conn:
        if not await conn.fetchval('SELECT pg_try_advisory_lock(734019, 1)'):
            return
        try:
            counts = await read_stock(conn)
            raw = await get_setting('admin_stock_alert_levels', '{}')
            try:
                previous = json.loads(raw or '{}')
            except (ValueError, TypeError):
                previous = {}
            if not isinstance(previous, dict):
                previous = {}
            state = json.loads(await get_setting('admin_stock_state_v2', '{}') or '{}')
            event = int(state.get('event', 0))
            previous = state.get('levels', previous)
            levels = {key: stock_level(counts[key], threshold)
                      for key, (_, threshold) in STOCK.items()}
            alerts = []
            for key, (name, threshold) in STOCK.items():
                level = levels[key]
                if level != 'ok' and level != previous.get(key):
                    # Empty -> low means replenished somewhat; don't send a new low alert.
                    if level == 'low' and previous.get(key) == 'empty':
                        continue
                    alerts.append(f"{'🔴 Закончились' if level == 'empty' else '🟠 Мало кодов'}: "
                                  f"{name} — <b>{counts[key]}</b> (порог: меньше {threshold})")
            if alerts:
                # A monotonically increasing event number separates real new
                # threshold crossings from retries of an already sent alert.
                await deliver(f'admin_stock_notice:{event}',
                              '📦 <b>Нужно пополнить запасы</b>\n\n' + '\n'.join(alerts))
            # Persist only after successful delivery; failed sends are retried.
            if levels != previous:
                await set_setting('admin_stock_state_v2', json.dumps(
                    {'levels': levels, 'event': event + (1 if alerts else 0)}))
            await daily_reports(conn, now, counts)
        finally:
            await conn.execute('SELECT pg_advisory_unlock(734019, 1)')


async def admin_reports_loop():
    await asyncio.sleep(30)
    while True:
        try:
            await reports_tick()
        except Exception:
            logging.exception('Admin daily report / stock alerts failed')
        await asyncio.sleep(30)
