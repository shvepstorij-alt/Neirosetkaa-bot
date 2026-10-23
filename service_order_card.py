"""Claude/Perplexity order cards. Rendering never starts a supplier activation."""
import asyncio
import datetime as dt
import hashlib
import html
import json
import logging
import re
from types import SimpleNamespace

from gpt_order_card import setting, save, attention_text, phase

PREFIX = 'servicecard:'
SERVICES = ('claude', 'perplexity')
_sent = {}
_alert_sent = set()


def plain(text):
    # Supplier errors may contain angle brackets that are not HTML tags.
    return html.unescape(re.sub(r'</?(?:b|i|u|s|a|code|pre|strong|em|blockquote|tg-emoji|span)(?:\s[^>]*)?>', '', str(text or '')))


async def initialize():
    from db import get_pool
    async with (await get_pool()).acquire() as conn:
        await conn.execute("INSERT INTO settings (key,value) VALUES ('servicecard_since', LOCALTIMESTAMP::text) ON CONFLICT (key) DO NOTHING")


async def snapshot(conn, oid):
    o = await conn.fetchrow('SELECT f.*, u.username, u.full_name FROM fk_orders f LEFT JOIN users u ON u.user_id=f.user_id WHERE f.order_id=$1', oid)
    parts = str((o or {}).get('pack') or '').split(':', 3)
    if not o or len(parts) < 3 or parts[0] != 'shop' or parts[1] not in SERVICES:
        return None
    service = parts[1]
    exists = await setting(conn, PREFIX + oid)
    if not exists and o['status'] != 'paid':
        return None
    # Table names come exclusively from the allowlist above.
    p = await conn.fetchrow(f'SELECT * FROM {service}_pending_activations WHERE order_id=$1 AND user_id=$2', oid, o['user_id'])
    codes = [dict(c) for c in await conn.fetch(f'SELECT * FROM {service}_codes WHERE order_id=$1 ORDER BY used_at DESC NULLS LAST, id DESC', oid)]
    done = next((c for c in codes if c.get('used_by') == o['user_id'] and c.get('used_at')), None)
    completed = bool(done) or await setting(conn, 'order_done:' + oid) == '1'
    watched = bool(p and (p.get('org_id') or p.get('bpa_order_id')))
    if service == 'claude':
        for code in {c['code'] for c in codes} | ({p['code']} if p else set()):
            if await setting(conn, 'claudecheck:' + code.upper()):
                watched = True
        marks = await conn.fetch("SELECT key,value FROM settings WHERE key LIKE 'claudecheck:%' AND split_part(value,'|',2)=$1", oid)
        watched = watched or bool(marks)
    state, title = phase(o, p, completed, watched, False, dt.datetime.now(dt.timezone.utc))
    card = json.loads(exists or '{}')
    from common import _fin_db_offset, _fin_from_db
    offset = await _fin_db_offset(conn)
    dates = {k: _fin_from_db(o[k], offset).strftime('%d.%m.%Y %H:%M:%S') for k in ('created_at','paid_at','fulfilled_at') if o.get(k)}
    import common as c
    if service == 'claude' and not completed and o['status'] == 'paid':
        ref = c._claude_chain_active.get(o['user_id'])
        if ref:
            state, title = 'running', '⏳ Активация выполняется'
    elif service == 'perplexity' and not completed and o['status'] == 'paid' and o['user_id'] in c._px_inflight:
        state, title = 'running', '⏳ Активация выполняется'
    code = (done or p or {}).get('code', '') or card.get('last_code', '')
    provider = (done or p or {}).get('provider') or card.get('last_provider', 'bpa')
    operation = {}
    if service == 'claude' and code:
        key = 'claudeop:' + hashlib.sha256((provider + ':' + code.strip().upper()).encode()).hexdigest()
        candidate = json.loads(await setting(conn, key, '{}'))
        if candidate.get('order_id') == oid:
            operation = candidate
    return dict(order=dict(o), service=service, pending=dict(p or {}), codes=codes,
                code=code, done_code=done, state=state,
                title=title, dates=dates, card=card, operation=operation)


def render(data, card, *, event=True):
    from config import SHOP_CATALOG
    from keyboards import tg_emoji
    o, p = data['order'], data['pending']
    service = data['service']
    parts = str(o['pack']).split(':', 3)
    # Saved plan name is authoritative; catalog positions can change.
    plan = parts[3] if len(parts) > 3 else p.get('plan_name') or 'Тариф из заказа'
    name = SHOP_CATALOG.get(service, {}).get('name') or service.title()
    e = lambda value: html.escape(str(value or '—'))
    who = '@' + o['username'] if o.get('username') else o.get('full_name') or 'Клиент'
    method = str(o.get('payment_method') or '').lower()
    payment = ('Telegram Stars' if method == 'stars' else str(o.get('amount_rub') or 0) + ' ₽'
               + (f" + {o['coins_spent']} монеток" if o.get('coins_spent') else ''))
    lines = [f"{tg_emoji(SHOP_CATALOG.get(service, {}))} <b>{e(name)} {e(plan)} · заказ #{e(o.get('num'))}</b>",
             '<b>' + data['title'] + '</b>', '', f"👤 {e(who)} · <code>{o['user_id']}</code>",
             f"🆔 <code>{e(o['order_id'])}</code>",
             '💳 Оплата: ' + e(payment),
             'Статус оплаты: ' + e({'paid':'оплачен','pending':'ожидает оплаты','cancelled':'отменён','refunded':'возвращён'}.get(o['status'],o['status']))]
    if method in ('card','sbp') and o.get('amount_rub'):
        lines.append('Способ: ' + {'card':'Банковская карта','sbp':'СБП'}[method])
    if o.get('fk_intid'):
        lines.append(f"🧾 FreeKassa: <code>{e(o['fk_intid'])}</code>")
    for key, label in [('created_at','Создан'), ('paid_at','Оплачен')]:
        if data['dates'].get(key):
            lines.append(label + ': ' + data['dates'][key] + ' (UTC+5)')
    if o.get('promo_code'):
        lines.append('🎟 Промокод: ' + e(o['promo_code']))
    if data['code']:
        label = '🔑 Код: ' if data.get('done_code') or p else '🔑 Последний код (резерва сейчас нет): '
        lines.append(label + '<code>' + e(data['code']) + '</code>')
    account = (data.get('done_code') or p).get('org_id') or card.get('last_account')
    if account:
        lines.append(('🏢 Organization ID: ' if service == 'claude' else '🆔 Аккаунт: ') + '<code>' + e(account) + '</code>')
    provider = (data.get('done_code') or p).get('provider') or card.get('last_provider', 'bpa')
    if data['code']:
        lines.append('🌐 Поставщик: ' + e({'bpa':'bypriceactivate.pro','aichongz':'9527.mobi','root':'rootchatgptplus.com'}.get(provider, provider)))
    if p.get('bpa_order_id'):
        lines.append('Заказ поставщика: <code>' + e(p['bpa_order_id']) + '</code>')
    operation = data.get('operation') or {}
    if operation:
        lines.append('Статус операции поставщика: ' + e(operation.get('state')))
        if operation.get('ref'):
            lines.append('Номер у поставщика: <code>' + e(operation['ref']) + '</code>')
        if data['state'] not in ('done', 'closed') and operation.get('reason'):
            lines.append('Причина / ответ сайта: ' + e(str(operation['reason'])[:500]))
    if card.get('delivery'):
        lines.append('📨 ' + e(card['delivery']))
    if event and card.get('event') and (data['state'] not in ('done', 'closed') or card.get('support')):
        # Store full text in history; the main card remains within Telegram limits.
        lines.extend(['', '<b>Последнее событие · ' + e(card.get('event_at')) + '</b>', e(plain(card['event'])[:1000])])
    lines.extend(['', '📜 Полные данные, причины ошибок и журнал — по кнопке ниже.'])
    return '\n'.join(lines)


def keyboard(data, card):
    from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as K
    number = str(data['order']['num'])
    rows = [[B(text='🔄 Обновить карточку', callback_data='scard:refresh:' + number)]]
    if data['state'] not in ('done', 'closed'):
        rows.append([B(text='📨 Отправить активацию повторно', callback_data='scard:retry:' + number)])
        # Preserve supplier-specific decisions, including their existing safety checks.
        for row in card.get('actions', []):
            buttons = [B(**b) for b in row if b.get('callback_data') != 'noop' and not b.get('callback_data', '').startswith(('scard:', 'adm_resend:'))]
            if buttons:
                rows.append(buttons)
        if data['code']:
            provider = (data.get('done_code') or data['pending']).get('provider') or card.get('last_provider', 'bpa')
            sites = {'bpa':'https://bypriceactivate.pro', 'aichongz':'https://9527.mobi', 'root':'https://rootchatgptplus.com', 'vip666':'https://vip666ai.com', 'ipiap':'https://ipiap.com'}
            if provider in sites:
                rows.append([B(text='🌐 Открыть сайт поставщика', url=sites[provider])])
    rows.append([B(text='📜 История и данные', callback_data='scard:history:' + number)])
    return K(inline_keyboard=rows)


async def edit_message(message, text='', **kwargs):
    """Route legacy supplier button edits through the persistent card renderer."""
    from db import get_pool
    from config import ADMIN_ID
    if message.chat.id == ADMIN_ID:
        async with (await get_pool()).acquire() as conn:
            oid = await conn.fetchval("SELECT order_id FROM fk_orders WHERE admin_msg_id=$1 AND (pack LIKE 'shop:claude:%' OR pack LIKE 'shop:perplexity:%')", message.message_id)
        if oid:
            return await update(oid, text, kwargs.get('reply_markup'))
    if text:
        return await message.edit_text(text, **kwargs)
    return await message.edit_reply_markup(**kwargs)


async def handle(cb):
    from config import ADMIN_ID, bot
    from db import get_pool, claim_activation, release_activation, activation_cooldown
    from aiogram.types import BufferedInputFile
    if cb.from_user.id != ADMIN_ID:
        return await cb.answer('Нет доступа', show_alert=True)
    _, action, number = cb.data.split(':', 2)
    if not number.isdigit():
        return await cb.answer('Некорректный номер', show_alert=True)
    async with (await get_pool()).acquire() as conn:
        oid = await conn.fetchval('SELECT order_id FROM fk_orders WHERE num=$1', int(number))
        data = await snapshot(conn, oid) if oid else None
    if not data:
        return await cb.answer('Заказ не найден', show_alert=True)
    await cb.answer('Обрабатываю…')
    if action == 'refresh':
        await update(oid)
    elif action == 'retry':
        lock = 'servicecard_retry:' + oid
        if not await claim_activation(lock, stale_minutes=5):
            return
        try:
            async with (await get_pool()).acquire() as conn:
                data = await snapshot(conn, oid)
            if data['state'] in ('done','closed','unknown','running'):
                return await cb.answer('Повтор недоступен: заказ завершён либо результат активации ещё проверяется.', show_alert=True)
            if await activation_cooldown(lock, seconds=60):
                return await cb.answer('Кнопка недавно отправлялась. Подожди минуту.', show_alert=True)
            from common import claude_resend_activation, perplexity_resend_activation
            fn = claude_resend_activation if data['service'] == 'claude' else perplexity_resend_activation
            ok, message = await fn(oid)
            await update(oid, message, delivery='Кнопка повторно отправлена клиенту' if ok else 'Повторная выдача не выполнена: ' + plain(message))
        finally:
            await release_activation(lock)
    elif action == 'history':
        async with (await get_pool()).acquire() as conn:
            history = await setting(conn, 'servicecard_history:' + oid)
            attempts = await setting(conn, 'claudelog:' + oid) if data['service'] == 'claude' else ''
        report = plain(render(data, data['card'], event=False))
        report += '\n\nСвязанные коды:\n' + '\n'.join(str(c['code']) + ' · ' + str(c.get('org_id') or '') for c in data['codes'])
        report += '\n\nПопытки Claude (код | сайт | результат | повторы):\n' + attempts if attempts else ''
        report += '\n\nПолный журнал уведомлений:\n' + history
        if data.get('operation'):
            # Operation bodies/credentials are intentionally excluded from exports.
            op = data['operation']
            report += '\n\nОперация поставщика:\n' + '\n'.join(k + ': ' + str(op.get(k) or '') for k in ('provider','code','org','ref','state','reason'))
        footer = '\n\n📄 Полные данные заказа и история — в этом файле.'
        lines = []
        for line in render(data, data['card'], event=False).splitlines():
            if len(plain('\n'.join(lines + [line]) + footer).encode('utf-16-le')) <= 2048:
                lines.append(line)
        await bot.send_document(ADMIN_ID, BufferedInputFile(report.encode(), filename=data['service'] + '_order_' + number + '.txt'), caption='\n'.join(lines) + footer, parse_mode='HTML')


async def loop():
    from db import get_pool
    await initialize()
    while True:
        try:
            async with (await get_pool()).acquire() as conn:
                rows = await conn.fetch("SELECT f.order_id FROM fk_orders f LEFT JOIN settings s ON s.key='servicecard:'||f.order_id WHERE (f.pack LIKE 'shop:claude:%' OR f.pack LIKE 'shop:perplexity:%') AND (s.value IS NOT NULL OR f.paid_at >= (SELECT value::timestamp FROM settings WHERE key='servicecard_since')) AND (s.value IS NULL OR s.value::jsonb->>'dirty'='true' OR COALESCE(s.value::jsonb->>'state','') NOT IN ('done','closed')) ORDER BY f.paid_at DESC")
            for row in rows:
                try:
                    await update(row['order_id'])
                except Exception:
                    logging.exception('Service card refresh failed: %s', row['order_id'])
        except Exception:
            logging.exception('Service card worker failed')
        await asyncio.sleep(30)


async def update(oid, text='', reply_markup=None, delivery=None, force_attention=False):
    """Serialized read/merge/render; only a proven missing message is recreated."""
    from db import get_pool
    from config import bot, ADMIN_ID, _BOT_TZ
    if not oid:
        return None
    async with (await get_pool()).acquire() as conn:
        await conn.execute('SELECT pg_advisory_lock(734025, hashtext($1))', str(oid))
        try:
            data = await snapshot(conn, oid)
            if data is None:
                return None
            card = data['card']
            if data['code']:
                card['last_code'] = data['code']
            current = data.get('done_code') or data['pending']
            if current.get('org_id'):
                card['last_account'] = current['org_id']
            if current.get('provider'):
                card['last_provider'] = current['provider']
            stamp = dt.datetime.now(_BOT_TZ).strftime('%d.%m.%Y %H:%M:%S')
            changed_event = text and text != card.get('event')
            if changed_event:
                card['event'], card['event_at'] = text, stamp
                card['support'] = force_attention
                if force_attention or (data['state'] not in ('done', 'closed') and any(word in plain(text).lower() for word in
                        ('ошиб', 'неуда', 'не прош', 'вручн', 'истек', 'нет мест', 'неяс', 'не удалось', 'проверь у продавца', 'проверка', 'неясен', 'failed', 'timeout'))):
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    if card.get('alert') != digest:
                        card['pending_alert'] = {'digest': digest, 'text': attention_text(plain(text))}
            if data['state'] in ('done', 'closed') and not card.get('support'):
                card.pop('pending_alert', None)
            if changed_event:
                # Full operational history in a separate per-order setting.
                history = await setting(conn, 'servicecard_history:' + oid)
                await save(conn, 'servicecard_history:' + oid, history + '\n' + stamp + '\n' + plain(text) + '\n')
            if reply_markup is not None:
                card['actions'] = reply_markup.model_dump(exclude_none=True)['inline_keyboard']
            if delivery is not None:
                card['delivery'] = delivery
            content = render(data, card)
            kb = keyboard(data, card)
            signature = hashlib.sha256((content + kb.model_dump_json(exclude_none=True)).encode()).hexdigest()
            mid = card.get('message_id') or _sent.get(oid) or data['order'].get('admin_msg_id')
            # Persist desired state first, so a failed Telegram edit is retried by the worker.
            card['state'] = data['state']
            card['dirty'] = True
            await save(conn, PREFIX + oid, json.dumps(card, ensure_ascii=False))
            if card.get('rendered') != signature:
                if mid:
                    try:
                        await bot.edit_message_text(chat_id=ADMIN_ID, message_id=mid,
                                                    text=content, parse_mode='HTML', reply_markup=kb)
                    except Exception as exc:
                        error = str(exc).lower()
                        if 'message is not modified' in error:
                            pass
                        elif 'message to edit not found' in error:
                            mid = None
                        else:
                            raise
                if not mid:
                    msg = await bot.send_message(ADMIN_ID, content, parse_mode='HTML', reply_markup=kb)
                    mid = msg.message_id
                    _sent[oid] = mid
                card.update(message_id=mid, rendered=signature, dirty=True)
                await save(conn, PREFIX + oid, json.dumps(card, ensure_ascii=False))
                _sent.pop(oid, None)
            else:
                card['dirty'] = True
                await save(conn, PREFIX + oid, json.dumps(card, ensure_ascii=False))
            # Repair the order reference even if Telegram already has identical
            # content: the previous DB write may have failed after the edit.
            await conn.execute('UPDATE fk_orders SET admin_msg_id=$1 WHERE order_id=$2', mid, oid)
            card['dirty'] = bool(card.get('pending_alert'))
            await save(conn, PREFIX + oid, json.dumps(card, ensure_ascii=False))
            # New actionable information gets an alert; edits alone are silent in Telegram.
            alert = card.get('pending_alert')
            if alert:
                from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
                digest = alert['digest']
                if (oid, digest) not in _alert_sent:
                    await bot.send_message(ADMIN_ID,
                        f"⚠️ Заказ #{data['order'].get('num')}: требуется внимание.\n" + html.escape(attention_text(alert['text']))
                        + '\n\n↗️ Нажми на цитату сверху, чтобы перейти к карточке заказа.',
                        parse_mode='HTML', reply_to_message_id=mid,
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                            InlineKeyboardButton(text='Обновить карточку', callback_data='scard:refresh:' + str(data['order']['num']))]]))
                    _alert_sent.add((oid, digest))
                card['alert'] = digest
                card.pop('pending_alert', None)
                card['dirty'] = False
                await save(conn, PREFIX + oid, json.dumps(card, ensure_ascii=False))
                _alert_sent.discard((oid, digest))
            return SimpleNamespace(message_id=mid)
        finally:
            await conn.execute('SELECT pg_advisory_unlock(734025, hashtext($1))', str(oid))

