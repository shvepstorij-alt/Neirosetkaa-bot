"""Order-scoped ChatGPT admin cards. Never activate or release codes here."""
import asyncio
import datetime as dt
import hashlib
import html
import json
import logging
import re
import uuid
from types import SimpleNamespace

PREFIX = 'gptcard:'
_sent = {}  # Known Telegram receipts if the subsequent database write failed.
_alert_sent = set()


async def setting(conn, key, default=''):
    value = await conn.fetchval('SELECT value FROM settings WHERE key=$1', key)
    return default if value is None else value


async def save(conn, key, value):
    await conn.execute('INSERT INTO settings (key,value) VALUES ($1,$2) '
                       'ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value', key, value)


async def initialize():
    from db import get_pool
    async with (await get_pool()).acquire() as conn:
        await conn.execute("INSERT INTO settings (key,value) VALUES ('gptcard_since', LOCALTIMESTAMP::text) "
                           'ON CONFLICT (key) DO NOTHING')


def plain(text):
    return html.unescape(re.sub(r'<[^>]*>', '', str(text or '')))


def attention_text(text):
    """Keep the event, including supplier errors at its end, within Telegram limits."""
    value = str(text or '')
    raw = value.encode('utf-16-le')
    if len(raw) <= 7000:
        return value
    # Long attempt histories must not push the final reason out of the alert.
    head = raw[:2400].decode('utf-16-le', errors='ignore')
    tail = raw[-4400:].decode('utf-16-le', errors='ignore')
    return head + '\n… Полная история — в карточке заказа …\n' + tail


def chatgpt_emoji():
    from config import SHOP_CATALOG
    from keyboards import tg_emoji
    return tg_emoji(SHOP_CATALOG['chatgpt'])


def branded_event(text):
    # Stored events are not trusted HTML. Restore only our own product emoji.
    safe = html.escape(plain(text))
    return re.sub(r'(?:✨\s*)?ChatGPT', lambda _: chatgpt_emoji() + ' ChatGPT', safe)


def phase(order, pending, completed, watched, job_running, now):
    """Positive completion evidence wins over any late error or old worker."""
    if completed:
        return 'done', '✅ Подписка активирована'
    if order['status'] != 'paid':
        return 'closed', '⛔ Заказ не оплачен / отменён'
    if watched:
        return 'unknown', '⏳ Ожидаем подтверждения поставщика'
    if job_running:
        return 'running', '⏳ Активация выполняется'
    if pending and pending.get('activating_at'):
        return 'unknown', '🟠 Активация запускалась — нужно проверить результат'
    if pending and pending.get('expires_at') and pending['expires_at'] <= now:
        return 'expired', '🟠 Окно активации истекло'
    if pending:
        return 'waiting', '🕒 Ожидаем действий клиента'
    return 'attention', '🟠 Нет активной выдачи — требуется проверка'


async def snapshot(conn, oid):
    order = await conn.fetchrow(
        'SELECT f.*, u.username, u.full_name FROM fk_orders f '
        'LEFT JOIN users u ON u.user_id=f.user_id WHERE f.order_id=$1', oid)
    if not order or not str(order['pack'] or '').startswith('shop:chatgpt:'):
        return None
    exists = await setting(conn, PREFIX + oid)
    eligible = exists or await conn.fetchval(
        "SELECT 1 FROM fk_orders WHERE order_id=$1 AND paid_at >= "
        "(SELECT value::timestamp FROM settings WHERE key='gptcard_since')", oid)
    if not eligible:
        return None
    pending = await conn.fetchrow('SELECT * FROM gpt_pending_activations WHERE order_id=$1 AND user_id=$2',
                                  oid, order['user_id'])
    codes = await conn.fetch('SELECT * FROM gpt_codes WHERE order_id=$1 ORDER BY used_at DESC NULLS LAST, id DESC', oid)
    done_code = next((dict(c) for c in codes if c['used_by'] == order['user_id'] and c['used_at']), None)
    completed = bool(done_code) or await setting(conn, 'order_done:' + oid) == '1'
    code = (done_code or pending or {}).get('code', '')
    watched = False
    # Include old attempts: the provider may still be processing a previous code.
    for c in {str(c['code']) for c in codes} | ({code} if code else set()):
        if await setting(conn, 'gptclaim:' + c.upper()):
            watched = True
        mark = json.loads(await setting(conn, 'a9527:' + c.upper(), '{}') or '{}')
        if mark and not completed and not (mark.get('retry_until') and not mark.get('watch') and not mark.get('final')):
            # A persisted supplier operation needs explicit reconciliation before retry.
            watched = True
    from common import _gpt_job_active, _activation_jobs
    job = _activation_jobs.get(_gpt_job_active.get(order['user_id']), {})
    running = bool(pending and job and job.get('status') not in (None, 'done'))
    now = dt.datetime.now(dt.timezone.utc)
    state, title = phase(order, pending, completed, watched, running, now)
    if state == 'waiting' and job.get('status') == 'done' and job.get('success') is False:
        state, title = 'attention', '🟠 Последняя попытка не завершилась успехом'
    plan_index = str(order['pack']).split(':')[2]
    if state in ('waiting', 'attention', 'expired') and await setting(conn, 'manual:chatgpt:' + plan_index) == '1':
        state, title = 'attention', '🟠 Нужна ручная активация'
    from common import _fin_db_offset, _fin_from_db
    dates = {}
    offset = await _fin_db_offset(conn)
    for key in ('created_at', 'paid_at', 'fulfilled_at'):
        if order.get(key):
            dates[key] = _fin_from_db(order[key], offset).strftime('%d.%m.%Y %H:%M:%S')
    paid_label = dates.get('paid_at', '')
    return dict(order=dict(order), pending=dict(pending) if pending else None, paid_label=paid_label,
                code=code, done_code=done_code, codes=[dict(c) for c in codes], dates=dates, state=state, title=title,
                journal=await setting(conn, 'gptlog:' + oid),
                card=json.loads(exists or '{}'))


def render(data, card):
    from config import GPT_ROUTE_LABELS, gpt_route_for_code
    from common import _pack_plan_name, _plan_by_order, gpt_provider_name
    o, p = data['order'], data['pending'] or {}
    oid, code = o['order_id'], data['code']
    parts = o['pack'].split(':')
    index = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    plan = _plan_by_order('chatgpt', index, _pack_plan_name(o['pack']))[1].get('name') or _pack_plan_name(o['pack'])
    who = '@' + o['username'] if o.get('username') else o.get('full_name') or 'Клиент'
    e = html.escape
    status = {'paid':'оплачен', 'pending':'ожидает оплаты', 'cancelled':'отменён', 'refunded':'возвращён'}.get(o['status'], o['status'])
    lines = [f"{chatgpt_emoji()} <b>ChatGPT {e(str(plan))} · заказ #{o.get('num') or '—'}</b>",
             f"<b>{data['title']}</b>", '',
             f"👤 {e(who)} · <code>{o['user_id']}</code>",
             f"💳 Сумма заказа: {o.get('amount_rub') or 0} ₽ · статус: {e(status)}",
             f"🧾 FreeKassa: <code>{e(str(o.get('fk_intid') or '—'))}</code>",
             f"🆔 <code>{e(oid)}</code>"]
    if data.get('paid_label'):
        lines.append('Оплачено: ' + e(data['paid_label']))
    if o.get('coins_spent'):
        lines.append(f"Монетками: {o['coins_spent']}")
    if o.get('payment_method'):
        method = {'sbp':'СБП', 'card':'карта'}.get(o['payment_method'], o['payment_method'])
        lines.append('Способ оплаты: ' + e(str(method)))
    if o.get('promo_code'):
        lines.append('🎟 Промокод: <code>' + e(str(o['promo_code'])) + '</code>')
    if code:
        lines.append(f"🔑 {'Итоговый' if data['state']=='done' else 'Закреплённый'} код: <code>{e(code)}</code>")
        provider = (data['done_code'] or p).get('provider') or ''
        if provider:
            lines.append(f"🌐 {e(gpt_provider_name(provider))}")
        route = GPT_ROUTE_LABELS.get(gpt_route_for_code(code), '')
        if route:
            lines.append(f"Маршрут: {e(route)}")
    delivery = card.get('delivery')
    if delivery:
        lines += ['', '📨 ' + e(delivery)]
    elif o.get('fulfilled_at'):
        lines.append('📨 Первичная выдача клиенту отмечена в базе')
    else:
        lines.append('📨 Доставка клиенту ещё не подтверждена')
    if card.get('token_at'):
        lines.append('🔐 Полная сессия сохранена: ' + e(card['token_at']))
    if card.get('account'):
        lines.append('📧 Аккаунт из последней сессии: ' + e(card['account']))
    if card.get('event') and data['state'] not in ('done', 'closed'):
        # This is an event, not the source of the current status.
        lines += ['', 'Последнее уведомление (' + e(card.get('event_at', '')) + '):',
                  branded_event(plain(card['event'])[:1000])]
    if data['state'] == 'done':
        lines += ['', 'Подтверждённый успех. Повторная выдача закрыта.']
    lines += ['', 'История попыток и данные клиента — по кнопкам ниже.']
    return '\n'.join(lines)


def history_report(data):
    from config import _BOT_TZ
    from common import gpt_provider_name
    e = html.escape
    o, p, card = data['order'], data['pending'] or {}, data['card']
    text = render(data, card).replace('История попыток и данные клиента — по кнопкам ниже.', '')
    lines = [text, '', '<b>Дополнительные данные заказа</b>',
             'Имя клиента: ' + e(str(o.get('full_name') or 'не сохранено'))]
    for field, label in (('created_at','Заказ создан'),('fulfilled_at','Первичная выдача')):
        lines.append(label + ': ' + e(data.get('dates', {}).get(field) or 'не отмечено'))
    for field, label in (('expires_at','Срок окна активации'),('activating_at','Начало активации')):
        value = p.get(field)
        label_value = value.astimezone(_BOT_TZ).strftime('%d.%m.%Y %H:%M:%S') if value else 'не отмечено'
        lines.append(label + ': ' + e(label_value))
    lines.append('Часовой пояс дат: Кызылорда (UTC+5)')
    lines += ['', '<b>Коды, связанные с заказом</b>']
    codes = list(data.get('codes') or [])
    if p.get('code') and not any(c['code'] == p['code'] for c in codes):
        codes.insert(0, dict(p, used_by=None, used_at=None))
    for c in codes:
        code = str(c['code'])
        label = 'активация записана' if c.get('used_by') == o['user_id'] and c.get('used_at') else (
            'текущий резерв' if code == p.get('code') else 'связан с заказом, успех не подтверждён')
        lines.append('• <code>' + e(code) + '</code> — ' + label)
        if c.get('provider'):
            lines.append('  Сайт: ' + e(gpt_provider_name(c['provider'])))
        if c.get('check_status'):
            lines.append('  Последний статус проверки кода: ' + e(str(c['check_status'])))
        if c.get('used_at'):
            lines.append('  Запись использования: ' + e(c['used_at'].astimezone(_BOT_TZ).strftime('%d.%m.%Y %H:%M:%S')))
    if not codes:
        lines.append('В базе нет связанных кодов.')
    lines += ['', '<b>Журнал попыток</b>',
              'Итоги отдельных попыток не заменяют текущий статус заказа.']
    if data.get('journal'):
        for index, row in enumerate(data['journal'].splitlines(), 1):
            fields = row.split('|')
            if len(fields) >= 3:
                lines.append(e(f'{index}. Код: {fields[0]} · сайт: {gpt_provider_name(fields[1])} · результат: {fields[2]}'
                               + (f' · повторов: {fields[3]}' if len(fields)>3 else '')))
            else:
                lines.append(e(row))
    else:
        lines.append('Попытки активации ещё не записаны.')
    lines += ['', 'Полный журнал уведомлений приложен файлом. Полная сессия клиента доступна отдельно по кнопке токена.']
    return '\n'.join(lines)


def history_caption(data):
    """One document caption, within Telegram's 1024-character caption limit.

    Keep complete HTML lines; the attached document retains every detail.
    Event history is excluded from the caption to prioritize order identity.
    """
    card = dict(data['card'])
    card.pop('event', None)
    text = render(data, card).replace('История попыток и данные клиента — по кнопкам ниже.', '')
    footer = '📄 Полные данные и история попыток — в этом файле.'
    lines = []
    for line in text.splitlines():
        candidate = '\n'.join(lines + [line, '', footer])
        if len(plain(candidate).encode('utf-16-le')) // 2 <= 1024:
            lines.append(line)
    return '\n'.join(lines).rstrip() + '\n\n' + footer


def keyboard(data, card):
    from aiogram.types import InlineKeyboardButton as B, InlineKeyboardMarkup as K, WebAppInfo
    from config import webapp_url
    oid = data['order']['order_id']
    rows = [[B(text='🔄 Обновить карточку', callback_data='gcard:refresh:' + str(data['order']['num']))]]
    if data['state'] not in ('done', 'closed'):
        rows.append([B(text='📨 Отправить активацию повторно', callback_data='gcard:retry:' + str(data['order']['num']))])
        if data['code']:
            rows.append([B(text='🔎 Проверить у поставщика', callback_data='gcard:check:' + str(data['order']['num']))])
        # Preserve provider-specific actions, e.g. confirming a route or manual work.
        for row in card.get('actions', []):
            buttons = [B(**b) for b in row if (b.get('callback_data') or b.get('url') or b.get('web_app')) and not
                       b.get('callback_data', '').startswith(('gptrc:', 'adm_gpt_retry:', 'gcard:'))
                       and b.get('callback_data') != 'noop']
            if buttons:
                rows.append(buttons)
    if card.get('token_key'):
        rows.append([B(text='📋 Токен целиком', web_app=WebAppInfo(url=webapp_url('/webapp/token', k=card['token_key'])))])
        rows.append([B(text='📄 Токен файлом', callback_data='gcard:token:' + str(data['order']['num']))])
    rows.append([B(text='📜 История и данные', callback_data='gcard:history:' + str(data['order']['num']))])
    return K(inline_keyboard=rows)


async def update(oid, text='', reply_markup=None, token=None, delivery=None):
    """Serialized read/merge/render; only a proven missing message is recreated."""
    from db import get_pool
    from config import bot, ADMIN_ID, _BOT_TZ
    if not oid:
        return None
    async with (await get_pool()).acquire() as conn:
        await conn.execute('SELECT pg_advisory_lock(734020, hashtext($1))', str(oid))
        try:
            data = await snapshot(conn, oid)
            if data is None:
                return None
            card = data['card']
            stamp = dt.datetime.now(_BOT_TZ).strftime('%d.%m.%Y %H:%M:%S')
            changed_event = text and text != card.get('event')
            if changed_event:
                card['event'], card['event_at'] = text, stamp
                card['actions'] = []
                if data['state'] not in ('done', 'closed') and any(word in plain(text).lower() for word in
                        ('ошиб', 'неуда', 'не прош', 'вручн', 'истек', 'нет мест', 'неяс', 'не удалось', 'проверь у продавца')):
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    if card.get('alert') != digest:
                        card['pending_alert'] = {'digest': digest, 'text': attention_text(plain(text))}
            if data['state'] in ('done', 'closed'):
                card.pop('pending_alert', None)
            if changed_event:
                # Full operational history in a separate per-order setting.
                history = await setting(conn, 'gptcard_history:' + oid)
                await save(conn, 'gptcard_history:' + oid, history + '\n' + stamp + '\n' + plain(text) + '\n')
            if reply_markup is not None:
                card['actions'] = reply_markup.model_dump(exclude_none=True)['inline_keyboard']
            if delivery is not None:
                card['delivery'] = delivery
            if token is not None and token:
                digest = hashlib.sha256(token.encode()).hexdigest()
                if card.get('token_hash') != digest:
                    key = uuid.uuid4().hex
                    await save(conn, 'admtok:' + key, json.dumps(dict(t=token, uid=data['order']['user_id'],
                        code=data['code'], order=oid, ts=int(dt.datetime.now().timestamp())), ensure_ascii=False))
                    card.update(token_key=key, token_hash=digest, token_at=stamp)
                    try:
                        payload = json.loads(token)
                        account = payload.get('user', {}).get('email', '') if isinstance(payload, dict) else ''
                        card['account'] = str(account)[:200]
                    except (ValueError, AttributeError):
                        card['account'] = ''
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
                card.update(message_id=mid, rendered=signature, dirty=bool(card.get('pending_alert')))
                await save(conn, PREFIX + oid, json.dumps(card, ensure_ascii=False))
                _sent.pop(oid, None)
                await conn.execute('UPDATE fk_orders SET admin_msg_id=$1 WHERE order_id=$2', mid, oid)
            else:
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
                            InlineKeyboardButton(text='Обновить карточку', callback_data='gcard:refresh:' + str(data['order']['num']))]]))
                    _alert_sent.add((oid, digest))
                card['alert'] = digest
                card.pop('pending_alert', None)
                card['dirty'] = False
                await save(conn, PREFIX + oid, json.dumps(card, ensure_ascii=False))
                _alert_sent.discard((oid, digest))
            return SimpleNamespace(message_id=mid)
        finally:
            await conn.execute('SELECT pg_advisory_unlock(734020, hashtext($1))', str(oid))


async def send(oid, chat_id, text, **kwargs):
    from config import bot, ADMIN_ID
    if chat_id == ADMIN_ID:
        result = await update(oid, text, kwargs.get('reply_markup'))
        if result is None:
            from service_order_card import update as service_update
            result = await service_update(oid, text, kwargs.get('reply_markup'))
        if result:
            return result
    return await bot.send_message(chat_id, text, **kwargs)


async def edit(oid, text, **kwargs):
    from config import bot, ADMIN_ID
    if kwargs.get('chat_id') == ADMIN_ID:
        result = await update(oid, text, kwargs.get('reply_markup'))
        if result is None:
            from service_order_card import update as service_update
            result = await service_update(oid, text, kwargs.get('reply_markup'))
        if result:
            return result
    return await bot.edit_message_text(text, **kwargs)


async def photo(oid, chat_id, photo, **kwargs):
    from config import bot, ADMIN_ID
    if chat_id == ADMIN_ID:
        result = await update(oid, kwargs.get('caption', ''), kwargs.get('reply_markup'))
        if result is None:
            from service_order_card import update as service_update
            result = await service_update(oid, kwargs.get('caption', ''), kwargs.get('reply_markup'))
        if result:
            await bot.send_photo(chat_id, photo, caption='📸 Скриншот активации · заказ ' + oid)
            return result
    return await bot.send_photo(chat_id, photo, **kwargs)


async def capture(oid, token):
    """UI failures must not cancel the customer's activation job."""
    try:
        return await update(oid, token=token)
    except Exception:
        logging.exception('GPT card token capture failed for order %s', oid)
        return None


async def client_success(oid, *args, **kwargs):
    from common import client_success_deliver
    ok = await client_success_deliver(*args, **kwargs)
    try:
        await update(oid, delivery=('Сообщение об успехе доставлено клиенту' if ok else
                                   'Подписка активирована, но доставка сообщения об успехе не подтверждена'))
    except Exception:
        logging.exception('GPT success delivery card update failed for %s', oid)
    return ok


async def update_code(code, text='', reply_markup=None):
    from db import get_pool
    async with (await get_pool()).acquire() as conn:
        oid = await conn.fetchval(
            "SELECT order_id FROM gpt_codes WHERE UPPER(code)=UPPER($1) AND COALESCE(order_id,'')<>'' "
            "UNION ALL SELECT order_id FROM gpt_pending_activations WHERE UPPER(code)=UPPER($1) LIMIT 1", code)
    return await update(oid, text, reply_markup) if oid else None


async def handle(cb):
    from config import bot, ADMIN_ID
    from db import get_pool, claim_activation, release_activation, activation_cooldown
    from aiogram.types import BufferedInputFile
    if cb.from_user.id != ADMIN_ID:
        await cb.answer('Нет доступа', show_alert=True)
        return
    _, action, number = cb.data.split(':', 2)
    if not number.isdigit():
        await cb.answer('Некорректный номер заказа', show_alert=True)
        return
    async with (await get_pool()).acquire() as conn:
        oid = await conn.fetchval('SELECT order_id FROM fk_orders WHERE num=$1', int(number))
        data = await snapshot(conn, oid) if oid else None
    if not data:
        await cb.answer('Карточка заказа не найдена', show_alert=True)
        return
    if action == 'open':
        # Historical alerts cannot acquire a reply target by editing them.
        await update(oid)
        await cb.answer('Карточка обновлена. Найди её в чате по номеру заказа #' + number,
                        show_alert=True)
        return
    await cb.answer('Обрабатываю…')
    if action == 'refresh':
        await update(oid)
        return
    if action == 'retry':
        lock = 'gptretrycard:' + oid
        if not await claim_activation(lock, stale_minutes=5):
            return
        try:
            async with (await get_pool()).acquire() as conn:
                data = await snapshot(conn, oid)
                other = await conn.fetchval('SELECT order_id FROM gpt_pending_activations '
                    'WHERE user_id=$1 AND order_id<>$2', data['order']['user_id'], oid)
            if data['state'] in ('done','closed','running','unknown') or other:
                await cb.answer('Повтор недоступен: заказ завершён, обрабатывается или есть другой резерв. Обнови карточку.', show_alert=True)
                return
            if await activation_cooldown('gcard_retry:' + oid, seconds=60):
                await cb.answer('Кнопка уже отправлялась недавно. Подожди минуту.', show_alert=True)
                return
            from common import gpt_resend_activation
            ok, msg = await gpt_resend_activation(oid)
            stamp = dt.datetime.now(dt.timezone(dt.timedelta(hours=5))).strftime('%d.%m %H:%M')
            await update(oid, text=msg, delivery=(('Повторная кнопка отправлена, ' + stamp)
                         if ok else 'Повторная отправка не подтверждена: ' + plain(msg)[:300]))
        finally:
            await release_activation(lock)
        return
    if action == 'check':
        if await activation_cooldown('gcard_check:' + oid, seconds=15):
            return
        from common import gpt_recheck_report
        if data['code']:
            result = await gpt_recheck_report(data['code'])
            await update(oid, text=(chatgpt_emoji() + ' ChatGPT · проверка у поставщика\n' + result) if result else '')
        else:
            await cb.answer('Нет закреплённого кода для проверки', show_alert=True)
        return
    async with (await get_pool()).acquire() as conn:
        if action == 'token':
            raw = await setting(conn, 'admtok:' + data['card'].get('token_key', ''), '{}')
            token = json.loads(raw or '{}')
            if (token.get('order') != oid or token.get('uid') != data['order']['user_id']
                    or not token.get('t') or dt.datetime.now().timestamp() - token.get('ts', 0) > 14*86400):
                await cb.answer('Полная сессия недоступна или срок хранения истёк', show_alert=True)
                return
            await bot.send_document(ADMIN_ID, BufferedInputFile(token['t'].encode('utf-8'),
                filename='chatgpt_session_' + number + '.txt'), caption='Полная сессия · заказ #' + number)
        elif action == 'history':
            history = await setting(conn, 'gptcard_history:' + oid)
            report = history_report(data)
            text = plain(report) + '\n\nПолный журнал уведомлений:\n' + history
            await bot.send_document(ADMIN_ID, BufferedInputFile(text.encode('utf-8'),
                filename='chatgpt_order_' + number + '.txt'),
                caption=history_caption(data), parse_mode='HTML')


async def loop():
    from db import get_pool
    await initialize()
    while True:
        try:
            async with (await get_pool()).acquire() as conn:
                rows = await conn.fetch("SELECT f.order_id FROM fk_orders f LEFT JOIN settings s ON s.key='gptcard:'||f.order_id "
                    "WHERE f.pack LIKE 'shop:chatgpt:%' AND f.paid_at >= "
                    "(SELECT value::timestamp FROM settings WHERE key='gptcard_since') "
                    "AND (s.value IS NULL OR s.value::jsonb->>'dirty'='true' "
                    "OR COALESCE(s.value::jsonb->>'state','') NOT IN ('done','closed')) "
                    'ORDER BY f.paid_at DESC')
            for row in rows:
                try:
                    await update(row['order_id'])
                except Exception:
                    logging.exception('GPT card refresh failed for order %s', row['order_id'])
        except Exception:
            logging.exception('GPT card worker failed')
        await asyncio.sleep(30)
