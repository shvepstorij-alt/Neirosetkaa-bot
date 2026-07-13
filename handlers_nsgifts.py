# -*- coding: utf-8 -*-
# Auto-split module "handlers_nsgifts" — part of Neirosetkaa-bot (refactored from bot.py).
import asyncio, logging, os, re, uuid, base64, hashlib, hmac, json, time
import datetime
import datetime as _dt_tz
import time as _time_module
import asyncpg
import aiohttp
from aiohttp import web
import anthropic
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, ChatMemberUpdated, InlineKeyboardMarkup,
    InlineKeyboardButton, CallbackQuery,
    LabeledPrice, PreCheckoutQuery, BufferedInputFile,
    ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION, StateFilter
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import (
    ADMIN_ID, NSGIFTS_API_SECRET, NSGIFTS_LOGIN, NSGIFTS_PASSWORD, NSGIFTS_USER_ID,
    WEBSHARE_PROXY, bot, dp, fk_pay_url,
)
from runtime_state import (
    rt,
)
from states import (
    AdmNsgState,
)
from db import (
    fk_save_order, get_pool, set_setting, get_coins, deduct_coins,
)
from keyboards import (
    _eib,
)
from common import (
    _nsg_markup, _nsg_threshold, _nsg_usd_rate, check_not_blocked, fk_check_order_status, nsgifts_fulfill_after_payment,
)

@dp.callback_query(F.data == "nsg_start")
async def nsg_start(cb: CallbackQuery):
    """Экран выбора региона App Store / iCloud."""
    await cb.answer()
    if not rt.nsgifts_client:
        await cb.message.answer("⚠️ Сервис временно недоступен. Напиши @neirosetkaalex")
        return

    from ns_gifts import get_stock_cached, get_apple_categories, region_flag
    stock = await get_stock_cached(rt.nsgifts_client)
    cats  = get_apple_categories(stock)

    if not cats:
        await cb.message.answer("⚠️ Каталог Apple Gift Card временно пуст. Попробуй позже.")
        return

    rows = []
    for cat in cats:
        flag  = region_flag(cat["category_name"])
        # Очищаем название: "Apple Gift Card Russia" → "Russia"
        short = cat["category_name"]
        for strip in ("Apple Gift Card", "App Store", "iTunes", "Gift Card"):
            short = short.replace(strip, "").strip()
        rows.append([InlineKeyboardButton(
            text=f"{flag} {short}",
            callback_data=f"nsg_cat:{cat['category_id']}"
        )])

    rows.append([InlineKeyboardButton(text="🌍 Как сменить регион Apple ID", callback_data="nsg_region_help")])
    rows.append([InlineKeyboardButton(text="⬅️ В магазин", callback_data="menu_shop")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    text = (
        "🍎 <b>App Store / iCloud — пополнение Apple ID</b>\n\n"
        "Код придёт <b>автоматически сразу</b> после оплаты.\n"
        "Подходит для App Store, iCloud+, Apple Music, Apple TV+.\n\n"
        "<b>Выбери регион пополнения:</b>"
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("nsg_cat:"))
async def nsg_cat(cb: CallbackQuery):
    """Экран выбора суммы пополнения."""
    await cb.answer()
    if not rt.nsgifts_client:
        await cb.message.answer("⚠️ Сервис временно недоступен.")
        return

    cat_id = int(cb.data.split(":")[1])

    from ns_gifts import get_stock_cached, get_apple_categories, region_flag, calc_price_rub
    stock    = await get_stock_cached(rt.nsgifts_client)
    cats     = get_apple_categories(stock)
    cat      = next((c for c in cats if c["category_id"] == cat_id), None)

    if not cat:
        await cb.message.answer("⚠️ Категория не найдена. Попробуй снова.")
        return

    usd_rate   = await _nsg_usd_rate()
    markup_pct = await _nsg_markup()

    # Фильтруем только товары в наличии, сортируем по цене
    services = sorted(
        [s for s in cat.get("services", []) if s.get("in_stock", 0) > 0],
        key=lambda s: s["price"]
    )

    if not services:
        await cb.message.answer("⚠️ Товары в этой категории закончились. Попробуй позже.")
        return

    flag  = region_flag(cat["category_name"])
    short = cat["category_name"]
    for strip in ("Apple Gift Card", "App Store", "iTunes", "Gift Card"):
        short = short.replace(strip, "").strip()

    rows = []
    for svc in services:
        price_rub = calc_price_rub(svc["price"], usd_rate, markup_pct)
        # Извлекаем номинал из названия: "Apple Gift Card | USA | 5 USD" → "5 USD"
        parts    = svc["service_name"].split("|")
        nominal  = parts[-1].strip() if parts else svc["service_name"]
        rows.append([InlineKeyboardButton(
            text=f"{nominal}  —  {price_rub:,} ₽".replace(",", " "),
            callback_data=f"nsg_svc:{svc['service_id']}:{cat_id}"
        )])

    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="nsg_start")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)

    text = (
        f"🍎 <b>App Store / iCloud — {flag} {short}</b>\n\n"
        f"Выбери сумму пополнения 👇\n\n"
        f"<i>Код придёт сразу после оплаты</i>"
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("nsg_svc:"))
async def nsg_svc(cb: CallbackQuery):
    """Экран подтверждения — показывает цену и кнопку оплаты."""
    await cb.answer()
    if not rt.nsgifts_client:
        await cb.message.answer("⚠️ Сервис временно недоступен.")
        return

    parts      = cb.data.split(":")
    service_id = int(parts[1])
    cat_id     = int(parts[2])
    uid        = cb.from_user.id

    if not await check_not_blocked(cb, uid):
        return

    from ns_gifts import get_stock_cached, get_apple_categories, calc_price_rub
    stock      = await get_stock_cached(rt.nsgifts_client)
    cats       = get_apple_categories(stock)
    cat        = next((c for c in cats if c["category_id"] == cat_id), None)
    service    = None
    if cat:
        service = next((s for s in cat.get("services", [])
                        if s["service_id"] == service_id), None)

    if not service:
        await cb.message.answer("⚠️ Товар не найден. Попробуй выбрать снова.")
        return

    usd_rate   = await _nsg_usd_rate()
    markup_pct = await _nsg_markup()
    price_rub  = calc_price_rub(service["price"], usd_rate, markup_pct)

    # Формируем order_id для FreeKassa
    import time as _t
    order_id = f"nsg_{service_id}_{uid}_{int(_t.time())}"

    # Сохраняем в nsgifts_orders (status=pending, ждём оплаты)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO nsgifts_orders
                (user_id, fk_order_id, service_id, service_name,
                 quantity, price_usd, price_rub, status)
            VALUES ($1,$2,$3,$4,1,$5,$6,'pending')
            ON CONFLICT (fk_order_id) DO NOTHING
        """, uid, order_id, service_id,
             service.get("service_name", ""), service["price"], price_rub)

    # Сохраняем в fk_orders (credits=0 — не начисляем кредиты)
    await fk_save_order(order_id, uid, 0, price_rub, pack=f"nsg:{service_id}")

    # Ссылка на оплату FreeKassa
    pay_url = fk_pay_url(price_rub, order_id)

    parts_name = service.get("service_name", "").split("|")
    nominal    = parts_name[-1].strip() if parts_name else service.get("service_name", "")

    # Монетки (бонусный баланс) — можно применить к любой оплате
    user_coins = await get_coins(uid)
    coins_used = int(min(user_coins, price_rub)) if user_coins >= 1 else 0
    rest = max(0, price_rub - coins_used)

    coins_line = ""
    if coins_used > 0:
        coins_line = f"🪙 Монетки: <b>−{coins_used} ₽</b> (баланс {int(user_coins)} ₽)\n"

    text = (
        f"🍎 <b>App Store / iCloud</b>\n\n"
        f"📦 <b>{service.get('service_name', nominal)}</b>\n"
        f"💵 Цена: <b>{price_rub:,} ₽</b>\n".replace(",", " ") +
        coins_line + "\n" +
        f"После оплаты код придёт <b>автоматически</b> в этот чат.\n"
        f"🆔 Заказ: <code>{order_id}</code>"
    )

    rows = []
    if coins_used > 0 and rest == 0:
        rows.append([InlineKeyboardButton(
            text=f"🪙 Оплатить монетками ({coins_used} ₽)",
            callback_data=f"nsg_full_coins:{order_id}")])
    elif coins_used > 0:
        rows.append([InlineKeyboardButton(
            text=f"🪙 {coins_used} ₽ монетками + СБП {rest:,} ₽".replace(",", " "),
            callback_data=f"nsg_coins_sbp:{order_id}")])
        rows.append([InlineKeyboardButton(
            text=f"💳 Оплатить всё СБП ({price_rub:,} ₽)".replace(",", " "),
            url=pay_url)])
    else:
        rows.append([InlineKeyboardButton(
            text=f"💳 Оплатить {price_rub:,} ₽".replace(",", " "),
            url=pay_url)])
    rows.append([InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"nsg_check:{order_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"nsg_cat:{cat_id}")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("nsg_check:"))
async def nsg_check_payment(cb: CallbackQuery):
    """Ручная проверка оплаты по кнопке (если FK webhook задержался)."""
    await cb.answer("Проверяем оплату…", show_alert=False)
    order_id = cb.data.split(":", 1)[1]
    uid      = cb.from_user.id

    fk_status = await fk_check_order_status(order_id)
    if fk_status and fk_status.get("status") == "paid":
        # Оплачен — выполняем заказ
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM nsgifts_orders WHERE fk_order_id=$1", order_id
            )
        if row and row["status"] == "pending":
            await nsgifts_fulfill_after_payment(order_id, uid)
        else:
            await cb.message.answer("✅ Заказ уже выполнен — код должен быть выше в чате.")
    else:
        await cb.answer(
            "Оплата пока не найдена. Если оплатил — подожди 1–2 минуты и попробуй снова.",
            show_alert=True
        )


# ──────────────────────────────────────────────────────────────────────────────
#  Выполнение заказа через NS Gifts API (вызывается после подтверждения оплаты)
# ──────────────────────────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("nsg_full_coins:"))
async def nsg_full_coins(cb: CallbackQuery):
    """Оплата App Store-заказа полностью монетками."""
    await cb.answer()
    order_id = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    pool = await get_pool()
    # Атомарно захватываем заказ (защита от двойного клика → двойного списания/закупки)
    async with pool.acquire() as conn:
        _claim = await conn.execute(
            "UPDATE nsgifts_orders SET status='paying' "
            "WHERE fk_order_id=$1 AND user_id=$2 AND status='pending'", order_id, uid)
        row = await conn.fetchrow(
            "SELECT * FROM nsgifts_orders WHERE fk_order_id=$1 AND user_id=$2", order_id, uid)
    if not row or _claim.split()[-1] == "0":
        await cb.answer("Заказ не найден или уже оплачен.", show_alert=True)
        return
    required = int(row["price_rub"])
    async def _rollback():
        try:
            async with pool.acquire() as _c:
                await _c.execute("UPDATE nsgifts_orders SET status='pending' WHERE fk_order_id=$1 AND status='paying'", order_id)
        except Exception:
            pass
    if await get_coins(uid) < required:
        await _rollback()
        await cb.answer("Недостаточно монеток.", show_alert=True)
        return
    if not await deduct_coins(uid, required):
        await _rollback()
        await cb.answer("Недостаточно монеток.", show_alert=True)
        return
    new_coins = await get_coins(uid)
    try:
        await cb.message.edit_text(
            f"🪙 <b>Оплачено монетками!</b>\n\n"
            f"📦 <b>{row['service_name']}</b>\n"
            f"🪙 Списано: <b>{required} ₽</b>\n"
            f"🪙 Остаток монеток: <b>{new_coins:.0f} ₽</b>\n\n"
            f"Получаем код — займёт пару секунд… ⏳",
            parse_mode="HTML")
    except Exception:
        pass
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🪙 <b>App Store: оплачено монетками</b>\n"
            f"👤 <code>{uid}</code>\n📦 {row['service_name']}\n"
            f"🪙 {required} ₽  💳 СБП 0 ₽\n🆔 <code>{order_id}</code>",
            parse_mode="HTML")
    except Exception:
        pass
    await nsgifts_fulfill_after_payment(order_id, uid)


@dp.callback_query(F.data.startswith("nsg_coins_sbp:"))
async def nsg_coins_sbp(cb: CallbackQuery):
    """Частичная оплата App Store-заказа: монетки + остаток СБП."""
    await cb.answer()
    order_id = cb.data.split(":", 1)[1]
    uid = cb.from_user.id
    pool = await get_pool()
    # Атомарно захватываем заказ (защита от двойного клика)
    async with pool.acquire() as conn:
        _claim = await conn.execute(
            "UPDATE nsgifts_orders SET status='paying' "
            "WHERE fk_order_id=$1 AND user_id=$2 AND status='pending'", order_id, uid)
        row = await conn.fetchrow(
            "SELECT * FROM nsgifts_orders WHERE fk_order_id=$1 AND user_id=$2", order_id, uid)
    if not row or _claim.split()[-1] == "0":
        await cb.answer("Заказ не найден или уже оплачен.", show_alert=True)
        return
    full = int(row["price_rub"])
    async def _rollback():
        try:
            async with pool.acquire() as _c:
                await _c.execute("UPDATE nsgifts_orders SET status='pending' WHERE fk_order_id=$1 AND status='paying'", order_id)
        except Exception:
            pass
    user_coins = await get_coins(uid)
    coins_used = int(min(user_coins, full))
    rest = max(0, full - coins_used)
    if coins_used <= 0:
        await _rollback()
        await cb.answer("Недостаточно монеток.", show_alert=True)
        return
    if rest <= 0:
        if not await deduct_coins(uid, full):
            await _rollback()
            await cb.answer("Недостаточно монеток.", show_alert=True)
            return
        await nsgifts_fulfill_after_payment(order_id, uid)
        return
    if not await deduct_coins(uid, coins_used):
        await _rollback()
        await cb.answer("Недостаточно монеток.", show_alert=True)
        return
    # Остаток к оплате через FK: фиксируем ожидаемую сумму = rest (валидация вебхука)
    # и записываем списанные монетки для авто-возврата, если клиент не доплатит.
    async with pool.acquire() as conn:
        await conn.execute("UPDATE fk_orders SET amount_rub=$1, coins_spent=$2 WHERE order_id=$3",
                           rest, coins_used, order_id)
    pay_url = fk_pay_url(rest, order_id)
    new_coins = await get_coins(uid)
    try:
        await cb.message.edit_text(
            f"🪙 <b>Монетки применены!</b>\n\n"
            f"📦 <b>{row['service_name']}</b>\n"
            f"🪙 Монетками: <b>{coins_used} ₽</b> (остаток {new_coins:.0f} ₽)\n"
            f"💳 Доплата СБП: <b>{rest:,} ₽</b>\n\n".replace(",", " ") +
            f"После оплаты код придёт <b>автоматически</b> в этот чат.\n"
            f"🆔 Заказ: <code>{order_id}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"💳 Доплатить {rest:,} ₽ через СБП".replace(",", " "), url=pay_url)],
                [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"nsg_check:{order_id}")],
            ]))
    except Exception:
        pass
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🪙 <b>App Store: монетки + СБП</b>\n"
            f"👤 <code>{uid}</code>\n📦 {row['service_name']}\n"
            f"🪙 {coins_used} ₽  💳 СБП {rest} ₽\n🆔 <code>{order_id}</code>",
            parse_mode="HTML")
    except Exception:
        pass


async def _nsg_menu_text_kb():
    balance_str = "—"
    if rt.nsgifts_client:
        try:
            bal = await asyncio.wait_for(rt.nsgifts_client.check_balance(), timeout=8)
            balance_str = f"${bal:.2f}"
        except Exception:
            balance_str = "не удалось загрузить"
    usd_rate   = await _nsg_usd_rate()
    markup_pct = await _nsg_markup()
    threshold  = await _nsg_threshold()
    text = (
        f"🍎 <b>App Store / iCloud — NS Gifts</b>\n\n"
        f"💰 Баланс кабинета: <b>{balance_str}</b>\n"
        f"💱 Курс USD → ₽: <b>{usd_rate:.0f}</b>\n"
        f"📈 Наценка: <b>{markup_pct:.0f}%</b>\n"
        f"🔔 Алерт при балансе &lt; <b>${threshold:.0f}</b>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💱 Изменить курс USD",      callback_data="adm_nsg_rate")],
        [InlineKeyboardButton(text="📈 Изменить наценку %",     callback_data="adm_nsg_markup")],
        [InlineKeyboardButton(text="🔔 Порог алерта баланса",   callback_data="adm_nsg_threshold")],
        [InlineKeyboardButton(text="🔄 Обновить кеш каталога",  callback_data="adm_nsg_refresh")],
        [InlineKeyboardButton(text="📊 Последние продажи",      callback_data="adm_nsg_sales")],
        [_eib("Главное меню", "back_main")],
    ])
    return text, kb


async def _nsg_refresh_menu(data: dict):
    """Обновляет сообщение-меню NS Gifts на месте после смены курса/наценки/порога."""
    mid  = data.get("nsg_msg_id")
    chat = data.get("nsg_chat")
    if not (mid and chat):
        return
    try:
        text, kb = await _nsg_menu_text_kb()
        await bot.edit_message_text(text, chat_id=chat, message_id=mid,
                                    reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


@dp.callback_query(F.data == "adm_nsgifts")
async def adm_nsgifts_menu(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await cb.answer()
    text, kb = await _nsg_menu_text_kb()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_nsg_rate")
async def adm_nsg_rate_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.set_state(AdmNsgState.waiting_rate)
    await state.update_data(nsg_msg_id=cb.message.message_id, nsg_chat=cb.message.chat.id)
    usd_rate = await _nsg_usd_rate()
    await cb.message.answer(
        f"💱 Текущий курс: <b>{usd_rate:.0f} ₽/USD</b>\n\n"
        f"Введи новый курс (только число, например <code>98</code>):",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdmNsgState.waiting_rate, F.text)
async def adm_nsg_rate_set(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        val = float(message.text.strip().replace(",", "."))
        assert 50 <= val <= 500
    except Exception:
        await message.answer("❌ Неверный формат. Введи число от 50 до 500:")
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings(key,value) VALUES('nsgifts_usd_rate',$1) "
            "ON CONFLICT(key) DO UPDATE SET value=$1", str(val)
        )
    data = await state.get_data()
    await state.clear()
    await message.answer(f"✅ Курс обновлён: <b>{val:.0f} ₽/USD</b>", parse_mode="HTML")
    await _nsg_refresh_menu(data)


@dp.callback_query(F.data == "adm_nsg_markup")
async def adm_nsg_markup_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.set_state(AdmNsgState.waiting_markup)
    await state.update_data(nsg_msg_id=cb.message.message_id, nsg_chat=cb.message.chat.id)
    markup = await _nsg_markup()
    await cb.message.answer(
        f"📈 Текущая наценка: <b>{markup:.0f}%</b>\n\n"
        f"Введи новую наценку (например <code>20</code>):",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdmNsgState.waiting_markup, F.text)
async def adm_nsg_markup_set(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        val = float(message.text.strip().replace(",", "."))
        assert 0 <= val <= 200
    except Exception:
        await message.answer("❌ Неверный формат. Введи % от 0 до 200:")
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings(key,value) VALUES('nsgifts_markup',$1) "
            "ON CONFLICT(key) DO UPDATE SET value=$1", str(val)
        )
    data = await state.get_data()
    await state.clear()
    await message.answer(f"✅ Наценка обновлена: <b>{val:.0f}%</b>", parse_mode="HTML")
    await _nsg_refresh_menu(data)


@dp.callback_query(F.data == "adm_nsg_threshold")
async def adm_nsg_threshold_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.set_state(AdmNsgState.waiting_threshold)
    await state.update_data(nsg_msg_id=cb.message.message_id, nsg_chat=cb.message.chat.id)
    thr = await _nsg_threshold()
    await cb.message.answer(
        f"🔔 Текущий порог: <b>${thr:.0f}</b>\n\n"
        f"Введи новый порог в USD (например <code>50</code>):",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdmNsgState.waiting_threshold, F.text)
async def adm_nsg_threshold_set(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    try:
        val = float(message.text.strip())
        assert 0 <= val <= 10000
    except Exception:
        await message.answer("❌ Введи число:")
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings(key,value) VALUES('nsgifts_balance_threshold',$1) "
            "ON CONFLICT(key) DO UPDATE SET value=$1", str(val)
        )
    data = await state.get_data()
    await state.clear()
    await message.answer(f"✅ Порог обновлён: <b>${val:.0f}</b>", parse_mode="HTML")
    await _nsg_refresh_menu(data)


@dp.callback_query(F.data == "adm_nsg_refresh")
async def adm_nsg_refresh_cache(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    from ns_gifts import invalidate_stock_cache, get_apple_categories
    invalidate_stock_cache()
    if not rt.nsgifts_client:
        await cb.answer("⚠️ NS Gifts не инициализирован (проверь переменные NSGIFTS_*).", show_alert=True)
        return
    await cb.answer("Проверяю NS Gifts…")
    _lines = ["🔎 <b>NS Gifts — диагностика</b>"]
    # Прямой запрос без «глушилки» кеша — чтобы увидеть реальную причину
    try:
        _stock = await rt.nsgifts_client.get_stock()
        _all = (_stock or {}).get("categories", []) or []
        _apple_named = [c for c in _all if any(
            k in (c.get("category_name", "").lower())
            for k in ("apple", "appstore", "app store", "itunes"))]
        _apple_instock = get_apple_categories(_stock)
        _lines.append(f"✅ get_stock OK · всего категорий: <b>{len(_all)}</b>")
        _lines.append(f"🍎 Apple-категорий: <b>{len(_apple_named)}</b>, в наличии: <b>{len(_apple_instock)}</b>")
        if _apple_named and not _apple_instock:
            _lines.append("⚠️ Apple-категории есть, но <b>все товары out of stock</b> (in_stock=0).")
        elif not _apple_named and _all:
            _names = ", ".join((c.get("category_name", "?")) for c in _all[:10])
            _lines.append(f"⚠️ Apple-категорий не найдено. Категории в каталоге: {_names}")
        elif not _all:
            _lines.append("⚠️ Каталог пуст (0 категорий) — вероятно, доступ/аккаунт.")
    except Exception as _e:
        _lines.append(f"❌ <b>get_stock ошибка:</b> {type(_e).__name__}: {str(_e)[:300]}")
        _lines.append("Похоже на отказ доступа (IP whitelist) или auth. "
                      "Проверь, что ВСЕ 3 статических IP в whitelist NS Gifts.")
    # баланс кабинета
    try:
        _bal = await rt.nsgifts_client.check_balance()
        _lines.append(f"💰 Баланс кабинета: <b>{_bal}$</b>")
    except Exception as _e:
        _lines.append(f"❌ check_balance: {str(_e)[:200]}")
    # диагностика доступа: какой IP видит NS Gifts + идём ли через прокси + маска логина
    _px = getattr(rt.nsgifts_client, "proxy", None)
    _lines.append(f"🔌 Прокси: <b>{'ДА → ' + str(_px) if _px else 'нет (прямое)'}</b>")
    try:
        _login = getattr(rt.nsgifts_client, "login", "") or ""
        _lmask = (_login[:3] + "***" + _login[-2:]) if len(_login) > 5 else "(задан)"
        _lines.append(f"👤 Логин: <b>{_lmask}</b> · User-Id: <b>{getattr(rt.nsgifts_client,'user_id','?')}</b>")
    except Exception:
        pass
    try:
        import aiohttp as _ah
        async with _ah.ClientSession(timeout=_ah.ClientTimeout(total=15)) as _s:
            async with _s.get("https://api.ipify.org?format=json", proxy=_px or None) as _ipr:
                _ip = (await _ipr.json()).get("ip", "?")
        _wl = {"162.220.232.250", "162.220.232.251", "152.55.176.240"}
        _ok = "✅ в whitelist" if _ip in _wl else "❌ НЕ в whitelist"
        _lines.append(f"🌐 Наш outbound IP (что видит NS Gifts): <b>{_ip}</b> — {_ok}")
    except Exception as _e:
        _lines.append(f"🌐 outbound IP: не удалось определить ({str(_e)[:80]})")
    try:
        await cb.message.answer("\n".join(_lines), parse_mode="HTML")
    except Exception:
        await cb.message.answer("\n".join(_lines))


@dp.callback_query(F.data == "adm_nsg_sales")
async def adm_nsg_sales(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, service_name, price_rub, status, created_at
            FROM nsgifts_orders
            ORDER BY created_at DESC LIMIT 20
        """)
    if not rows:
        await cb.answer("Продаж пока нет", show_alert=True)
        return
    lines = ["📊 <b>Последние 20 продаж App Store:</b>\n"]
    for r in rows:
        icon = "✅" if r["status"] == "fulfilled" else ("❌" if r["status"] == "failed" else "⏳")
        lines.append(
            f"{icon} <code>{r['user_id']}</code> · {r['service_name']} · "
            f"{r['price_rub']} ₽ · {r['status']}"
        )
    total = sum(r["price_rub"] or 0 for r in rows if r["status"] == "fulfilled")
    lines.append(f"\n💰 Сумма выполненных: <b>{total:,} ₽</b>".replace(",", " "))
    await cb.message.answer("\n".join(lines), parse_mode="HTML")
    await cb.answer()


# ──────────────────────────────────────────────────────────────────────────────
#  Вспомогательная: сохранить FK-заказ (адаптер для совместимости)
#  Скопируй или проверь что fk_save_order уже есть в bot.py.
#  Если нет — добавь:
# ──────────────────────────────────────────────────────────────────────────────



# ── Диагностика NS Gifts (только админ): /nsg_test ───────────────────────────
@dp.message(F.text == "/nsg_test", StateFilter("*"))
async def nsg_test(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    ok = lambda v: "✅" if v else "❌ пусто"
    lines = [
        "🔎 <b>NS Gifts — диагностика</b>\n",
        f"USER_ID: {ok(NSGIFTS_USER_ID)}",
        f"LOGIN: {ok(NSGIFTS_LOGIN)}",
        f"PASSWORD: {ok(NSGIFTS_PASSWORD)}",
        f"API_SECRET: {ok(NSGIFTS_API_SECRET)}",
        f"PROXY: {'задан' if WEBSHARE_PROXY else 'нет'}",
        f"client init: {'✅' if rt.nsgifts_client else '❌ НЕ инициализирован'}",
    ]
    await message.answer("\n".join(lines), parse_mode="HTML")

    if not rt.nsgifts_client:
        await message.answer(
            "⚠️ client = None → не заданы env-переменные NSGIFTS_* на Railway "
            "(нужны USER_ID, LOGIN, PASSWORD, API_SECRET). Добавь и перезапусти сервис."
        )
        return

    # Баланс (проверка авторизации/подписи/прокси)
    try:
        bal = await rt.nsgifts_client.check_balance()
        await message.answer(f"💰 Баланс NS Gifts: <b>${bal:.4f}</b>", parse_mode="HTML")
    except Exception as e:
        await message.answer(
            f"❌ check_balance упал (авторизация/подпись/прокси):\n<code>{str(e)[:500]}</code>",
            parse_mode="HTML"
        )

    # Каталог (наличие Apple-категорий с товаром)
    try:
        from ns_gifts import get_apple_categories
        stock = await rt.nsgifts_client.get_stock()
        cats_all = stock.get("categories", [])
        apple = get_apple_categories(stock)
        sample = ""
        if apple:
            c = apple[0]
            svcs = [s for s in c.get("services", []) if s.get("in_stock", 0) > 0]
            sample = f"\nПример: {c.get('category_name')} — товаров в наличии: {len(svcs)}"
        await message.answer(
            f"📦 Каталог: всего категорий {len(cats_all)}, "
            f"Apple-категорий с товаром: <b>{len(apple)}</b>{sample}",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(
            f"❌ get_stock упал:\n<code>{str(e)[:500]}</code>", parse_mode="HTML"
        )


# ── /nsg_set КУРС НАЦЕНКА — задать курс и нацен, цены пересчитаются для всех регионов ──
@dp.message(F.text.startswith("/nsg_set"), StateFilter("*"))
async def nsg_set(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "Формат: <code>/nsg_set КУРС НАЦЕНКА</code>\n"
            "Например: <code>/nsg_set 90 18</code> — курс 90 ₽/$, наценка 18%.\n\n"
            "<i>Цена клиенту считается автоматически по каждому региону:</i>\n"
            "<i>закупка_$ × курс × (1 + наценка/100), округление до красивого числа.</i>",
            parse_mode="HTML"
        )
        return
    try:
        rate   = float(parts[1].replace(",", "."))
        markup = float(parts[2].replace(",", "."))
        assert 50 <= rate <= 500 and 0 <= markup <= 100
    except Exception:
        await message.answer("❌ Неверные значения. Курс 50–500, наценка 0–100. Пример: /nsg_set 90 18")
        return
    await set_setting("nsgifts_usd_rate", str(rate))
    await set_setting("nsgifts_markup", str(markup))
    await message.answer(
        f"✅ Готово!\nКурс: <b>{rate:.0f} ₽/$</b>\nНаценка: <b>{markup:.0f}%</b>\n\n"
        f"Цены пересчитались для всех регионов автоматически. Проверь в магазине 🍎",
        parse_mode="HTML"
    )


# ── Инструкция: как сменить регион Apple ID ──────────────────────────────────
@dp.callback_query(F.data == "nsg_region_help")
async def nsg_region_help(cb: CallbackQuery):
    await cb.answer()
    text = (
        "🌍 <b>Как сменить регион Apple ID</b>\n\n"
        "Код активируется только на Apple ID <b>того же региона</b>, что и карта. "
        "Если у тебя стоит другой регион (например, Россия) — смени его:\n\n"
        "1. <b>Настройки</b> → нажми на своё <b>имя</b> вверху\n"
        "2. <b>«Медиаматериалы и покупки»</b> → <b>«Просмотреть»</b> → <b>«Страна/регион»</b>\n"
        "3. <b>«Изменить страну или регион»</b> → выбери нужную (напр. США)\n"
        "4. Прими условия, в способе оплаты выбери <b>«Нет»</b> (None), введи любой адрес и номер телефона\n"
        "5. Готово — теперь можно активировать код 🎉\n\n"
        "⚠️ <b>Перед сменой региона важно:</b>\n"
        "• Баланс старого региона нужно потратить — Apple требует баланс <b>0</b> для смены.\n"
        "• Активные подписки (Apple Music, iCloud+, подписки в приложениях) могут "
        "<b>слететь</b> — после смены их придётся оформить заново.\n"
        "• Скачанные приложения останутся, но обновления некоторых могут быть недоступны в новом регионе.\n"
        "• Семейный доступ может сброситься.\n\n"
        "💡 <b>Совет:</b> многие заводят <b>отдельный Apple ID</b> под нужный регион, "
        "чтобы не трогать основной аккаунт.\n\n"
        "Нужна помощь — пиши @neirosetkaalex 🙌"
    )
    await cb.message.answer(text, parse_mode="HTML")
