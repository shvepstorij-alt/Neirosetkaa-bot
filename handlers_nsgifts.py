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
    WEBSHARE_PROXY, dp, fk_payment_url,
)
from runtime_state import (
    rt,
)
from states import (
    AdmNsgState,
)
from db import (
    fk_save_order, get_pool, set_setting,
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
        f"<i>Курс: 1 USD = {usd_rate:.0f} ₽ · Код придёт сразу после оплаты</i>"
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
    pay_url = fk_payment_url(order_id, price_rub, uid)

    parts_name = service.get("service_name", "").split("|")
    nominal    = parts_name[-1].strip() if parts_name else service.get("service_name", "")

    text = (
        f"🍎 <b>App Store / iCloud</b>\n\n"
        f"📦 <b>{service.get('service_name', nominal)}</b>\n"
        f"💵 Цена: <b>{price_rub:,} ₽</b>\n\n".replace(",", " ") +
        f"После оплаты код придёт <b>автоматически</b> в этот чат.\n"
        f"🆔 Заказ: <code>{order_id}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"💳 Оплатить {price_rub:,} ₽".replace(",", " "),
            url=pay_url
        )],
        [InlineKeyboardButton(
            text="🔄 Проверить оплату",
            callback_data=f"nsg_check:{order_id}"
        )],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"nsg_cat:{cat_id}")],
    ])
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

@dp.callback_query(F.data == "adm_nsgifts")
async def adm_nsgifts_menu(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await cb.answer()  # сразу убираем «Загрузка…», иначе кнопка кажется нерабочей

    balance_str = "—"
    if rt.nsgifts_client:
        try:
            # короткий таймаут — чтобы меню не подвисало, если API/прокси медленный
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
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_nsg_rate")
async def adm_nsg_rate_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.set_state(AdmNsgState.waiting_rate)
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
    await state.clear()
    await message.answer(f"✅ Курс обновлён: <b>{val:.0f} ₽/USD</b>", parse_mode="HTML")


@dp.callback_query(F.data == "adm_nsg_markup")
async def adm_nsg_markup_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.set_state(AdmNsgState.waiting_markup)
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
    await state.clear()
    await message.answer(f"✅ Наценка обновлена: <b>{val:.0f}%</b>", parse_mode="HTML")


@dp.callback_query(F.data == "adm_nsg_threshold")
async def adm_nsg_threshold_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.set_state(AdmNsgState.waiting_threshold)
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
    await state.clear()
    await message.answer(f"✅ Порог обновлён: <b>${val:.0f}</b>", parse_mode="HTML")


@dp.callback_query(F.data == "adm_nsg_refresh")
async def adm_nsg_refresh_cache(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    from ns_gifts import invalidate_stock_cache, get_stock_cached, get_apple_categories
    invalidate_stock_cache()
    stock = await get_stock_cached(rt.nsgifts_client) if rt.nsgifts_client else {}
    cats  = get_apple_categories(stock)
    await cb.answer(f"✅ Кеш сброшен. Категорий Apple: {len(cats)}", show_alert=True)


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
