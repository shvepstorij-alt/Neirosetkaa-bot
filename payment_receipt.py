"""One persistent purchase confirmation per paid order, independent of fulfillment."""
import asyncio
import datetime as dt
import html
import json
import logging
import hashlib
from gpt_order_card import setting, save

_sent = {}


async def initialize():
    from db import get_pool
    async with (await get_pool()).acquire() as conn:
        await conn.execute("INSERT INTO settings (key,value) VALUES ('receipt_since', LOCALTIMESTAMP::text) ON CONFLICT (key) DO NOTHING")


def render(o, *, product='', paid_at='', stars=None):
    from config import SHOP_CATALOG
    e = lambda x: html.escape(str(x))
    pack = str(o.get('pack') or '')
    parts = pack.split(':', 3)
    if parts[0] == 'shop' and len(parts) >= 3:
        catalog = SHOP_CATALOG.get(parts[1], {})
        service = catalog.get('name') or parts[1]
        plan = parts[3] if len(parts) > 3 else 'Тариф из заказа'
        product = service + ' · ' + plan
    elif not product:
        product = ('Кредиты бота · ' + str(o.get('credits') or 0) + ' кредитов') if o.get('credits') else pack or 'Товар из заказа'
    amount = o.get('amount_rub') or 0
    coins = o.get('coins_spent') or 0
    method = str(o.get('payment_method') or '').lower()
    lines = ['✅ <b>Оплата получена</b>', '', '🧾 <b>Заказ #' + e(o.get('num') or o['order_id']) + '</b>',
             '📦 ' + e(product), '🆔 <code>' + e(o['order_id']) + '</code>']
    if method == 'stars':
        lines.append('⭐ Оплачено: ' + (e(stars) + ' Stars' if stars is not None else 'Telegram Stars (сумма в квитанции Telegram)'))
    else:
        lines.append('💰 Сумма заказа: <b>' + e(amount + coins) + ' ₽</b>')
        if amount:
            label = {'card':'Банковская карта', 'sbp':'СБП'}.get(method, 'Платёж через FreeKassa')
            lines.append('💳 ' + label + ': ' + e(amount) + ' ₽')
        if coins:
            lines.append('🪙 Монетками: ' + e(coins) + ' ₽')
    if paid_at:
        lines.append('🕒 Оплачено: ' + e(paid_at) + ' (UTC+5)')
    if o.get('fk_intid'):
        lines.append('Платёж FreeKassa: <code>' + e(o['fk_intid']) + '</code>')
    if o.get('promo_code'):
        lines.append('🎟 Промокод: ' + e(o['promo_code']))
    if int(o.get('credits') or 0) > 0:
        lines.append('💎 Начислено: <b>' + e(o['credits']) + ' кредитов бота</b>')
    else:
        lines.append('Статус: <b>оплачен</b>. Инструкция и результат выдачи — отдельными сообщениями в этом чате.')
    lines.extend(['', 'Сохрани номер заказа — по нему можно найти покупку.'])
    return '\n'.join(lines)


async def send(oid):
    from db import get_pool
    from config import bot
    from common import _fin_db_offset, _fin_from_db
    async with (await get_pool()).acquire() as conn:
        await conn.execute('SELECT pg_advisory_lock(734026, hashtext($1))', oid)
        try:
            o = await conn.fetchrow('SELECT * FROM fk_orders WHERE order_id=$1', oid)
            if not o or o['status'] != 'paid':
                return
            key = 'receipt:' + oid
            state = json.loads(await setting(conn, key, '{}'))
            if state.get('message_id'):
                return
            if oid in _sent:
                await save(conn, key, json.dumps({'message_id': _sent[oid]}))
                _sent.pop(oid, None)
                return
            # A crash/network timeout after dispatch has an ambiguous result. Do not
            # send another confirmation blindly on restart.
            if state.get('dispatching'):
                logging.warning('Payment receipt delivery uncertain: %s', oid)
                return
            product = ''
            if str(o.get('pack') or '').startswith('nsg:'):
                product = await conn.fetchval('SELECT service_name FROM nsgifts_orders WHERE fk_order_id=$1', oid) or ''
            stars = await setting(conn, 'receipt_stars:' + oid)
            date = _fin_from_db(o['paid_at'], await _fin_db_offset(conn)).strftime('%d.%m.%Y %H:%M:%S') if o.get('paid_at') else ''
            text = render(dict(o), product=product, paid_at=date, stars=stars or None)
            await save(conn, key, json.dumps({'dispatching': True}))
            try:
                msg = await bot.send_message(o['user_id'], text, parse_mode='HTML')
            except Exception as exc:
                # Telegram rejected before delivery; retry only rate-limited requests.
                if type(exc).__name__ == 'TelegramRetryAfter':
                    await save(conn, key, '{}')
                raise
            _sent[oid] = msg.message_id
            await save(conn, key, json.dumps({'message_id': msg.message_id}))
            _sent.pop(oid, None)
        finally:
            await conn.execute('SELECT pg_advisory_unlock(734026, hashtext($1))', oid)


async def safe_send(oid):
    """Notification failure cannot interrupt payment fulfillment."""
    try:
        await send(oid)
    except Exception:
        logging.exception('Payment receipt failed: %s', oid)


async def safe_stars_receipt(charge, credits, name):
    """Historical Stars credit invoices use their existing payment record, not a
    second fk_orders entry that would double-count financial reports."""
    from db import get_pool
    from config import bot, _BOT_TZ
    oid = 'Stars-' + hashlib.sha256(charge.encode()).hexdigest()[:16]
    try:
        async with (await get_pool()).acquire() as conn:
            await conn.execute('SELECT pg_advisory_lock(734026, hashtext($1))', oid)
            try:
                key = 'receipt:' + oid
                if await setting(conn, key):
                    return
                row = await conn.fetchrow('SELECT * FROM stars_payments WHERE charge_id=$1', charge)
                if not row or not row.get('delivered_at'):
                    return
                o = dict(order_id=oid, num=oid, pack='', credits=credits, payment_method='stars')
                text = render(o, product=name, paid_at=row['created_at'].astimezone(_BOT_TZ).strftime('%d.%m.%Y %H:%M:%S'), stars=row['amount'])
                await save(conn, key, json.dumps({'dispatching':True}))
                msg = await bot.send_message(row['user_id'], text, parse_mode='HTML')
                await save(conn, key, json.dumps({'message_id':msg.message_id}))
            finally:
                await conn.execute('SELECT pg_advisory_unlock(734026, hashtext($1))', oid)
    except Exception:
        logging.exception('Stars receipt failed: %s', oid)


async def loop():
    from db import get_pool
    await initialize()
    while True:
        try:
            async with (await get_pool()).acquire() as conn:
                rows = await conn.fetch("SELECT f.order_id FROM fk_orders f LEFT JOIN settings s ON s.key='receipt:'||f.order_id WHERE f.status='paid' AND f.paid_at >= (SELECT value::timestamp FROM settings WHERE key='receipt_since') AND (s.value IS NULL OR s.value='{}') ORDER BY f.paid_at LIMIT 100")
            for r in rows:
                await safe_send(r['order_id'])
        except Exception:
            logging.exception('Payment receipt worker failed')
        await asyncio.sleep(30)
