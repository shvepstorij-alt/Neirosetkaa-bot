# -*- coding: utf-8 -*-
# Auto-split module "handlers_shop" — part of Neirosetkaa-bot (refactored from bot.py).
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
    ADMIN_ID, CREDIT_PACKS, IMAGE_MODELS, PERSONAL_USERNAME, SHOP_CATALOG, SHOP_CATEGORIES,
    VIDEO_MODELS, _ref_bonus_for_count, bot, dp, fk_pay_url, pending_fk_payments,
)
from states import (
    PromoState, ShopPromoState,
)
from db import (
    add_credits_batch, check_promo_for_user, deduct_coins, fk_get_order, fk_save_order, get_coins,
    get_credits, get_pool, get_user, log_payment, redeem_promo, get_order_num,
)
from keyboards import (
    _btn_emoji_id, _eib, kb_buy, pay_btn_kwargs, tg_emoji, tg_emoji_ui,
)
from common import (
    check_not_blocked, fk_check_order_status, fk_create_order, fk_credit_paid_order, fk_monitor_order, process_referral_bonus,
)

# Старые дубликаты сервисов из БД, которые надо скрыть из магазина (по названию).
# Автоматический App Store теперь живёт под ключом 'appstore' (NS Gifts).
_HIDDEN_SHOP_NAMES = {"iCloud/AppStore", "iCloud / AppStore", "iCloud/App Store", "iCloud / App Store"}

@dp.callback_query(F.data.startswith("shop_renew:"))
async def shop_renew(cb: CallbackQuery):
    # Кнопка «Продлить» из напоминания об истечении. Напоминание могло прийти
    # несколько дней назад — редактировать старое сообщение (>48ч) Telegram НЕ даёт,
    # поэтому шлём ВСЕГДА СВЕЖИМ сообщением витрину тарифов сервиса. Так кнопка
    # не «замолкает» независимо от возраста напоминания.
    try:
        await cb.answer()          # сразу гасим «часики» на кнопке
    except Exception:
        pass
    key = cb.data.split(":")[1] if ":" in cb.data else ""
    s = SHOP_CATALOG.get(key)
    if not s and key:
        _kl = key.lower()
        for _k, _v in SHOP_CATALOG.items():
            if isinstance(_v, dict) and (_k.lower() == _kl or _kl in _v.get("name", "").lower()):
                key, s = _k, _v
                break
    _shop_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛍 Магазин", callback_data="menu_shop")]])
    try:
        if s and s.get("plans"):
            _order = sorted(range(len(s["plans"])), key=lambda i: s["plans"][i].get("price", 0))
            plans_text = ""
            for _n, i in enumerate(_order, 1):
                p = s["plans"][i]
                plans_text += (f"  {_n}. <b>{p.get('name','')} - {p.get('price',0)}₽/мес</b>\n"
                               f"     <i>{p.get('desc','')}</i>\n")
            text = (
                f"{tg_emoji(s)} <b>{s['name']}</b>\n\n"
                f"<i>{s['desc']}</i>\n\n"
                f"Доступные тарифы:\n{plans_text}\n"
                f"<b>👇 Выбери тариф для продления:</b>"
            )
            rows = []
            for i in _order:
                p = s["plans"][i]
                rows.append([InlineKeyboardButton(
                    text=f"{p.get('name','')} - {p.get('price',0)}₽/мес",
                    callback_data=f"shop_confirm:{key}:{i}")])
            rows.append([InlineKeyboardButton(text="⬅️ В магазин", callback_data="menu_shop")])
            await bot.send_message(cb.from_user.id, text, parse_mode="HTML",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
        else:
            await bot.send_message(
                cb.from_user.id,
                "🛍 Открой магазин и выбери сервис для продления 👇",
                reply_markup=_shop_kb)
    except Exception as _e:
        logging.error(f"shop_renew failed key={key!r}: {_e}")
        try:
            await bot.send_message(
                cb.from_user.id,
                "🛍 Открой магазин для продления подписки 👇",
                reply_markup=_shop_kb)
        except Exception:
            pass


def _build_shop_menu():
    """Строит (text, kb) магазина. Используется и inline-кнопкой, и нижней reply-кнопкой."""
    text = (
        "🛍 <b>Магазин подписок Neirosetka</b>\n\n"
        "<i>Оплата в рублях - по СБП, без иностранных карт.\n"
        "Активация в течение 5-30 минут после оплаты.</i>\n\n"
        "<b>👇 Выбери сервис:</b>"
    )
    # Порядок кнопок задаётся SHOP_ORDER (по ключу ИЛИ имени, регистронезависимо).
    # Всё, что не попало в SHOP_ORDER — в конец (в порядке словаря), чтобы ничего не пропало.
    try:
        from config import SHOP_ORDER as _SHOP_ORDER
    except Exception:
        _SHOP_ORDER = []

    def _shop_rank(_k, _s):
        _kl = (_k or "").lower()
        _nl = ((_s or {}).get("name", "") or "").lower()
        for _i, _tok in enumerate(_SHOP_ORDER):
            if _tok == _kl or (_tok and _tok in _nl):
                return _i
        return 10_000

    ordered_keys = [
        _k for _k, _ in sorted(SHOP_CATALOG.items(),
                               key=lambda _kv: _shop_rank(_kv[0], _kv[1]))
    ]

    rows = []
    row = []
    for key in ordered_keys:
        s = SHOP_CATALOG.get(key)
        if not s:
            continue
        # Скрываем старый ручной дубликат App Store (заменён на appstore через NS Gifts)
        if key != "appstore" and (s.get("name") or "").strip() in _HIDDEN_SHOP_NAMES:
            continue
        # NS Gifts сервисы — кастомный callback, показываем даже без plans
        if s.get("_nsgifts"):
            _eid = _btn_emoji_id(key, s)
            row.append(InlineKeyboardButton(
                text=s['name'] if _eid else f"{s['emoji']} {s['name']}",
                callback_data="nsg_start",
                **{"icon_custom_emoji_id": _eid} if _eid else {}
            ))
            if len(row) == 2:
                rows.append(row)
                row = []
            continue
        if not s.get("plans"):
            continue
        _eid = _btn_emoji_id(key, s)
        row.append(InlineKeyboardButton(
            text=s['name'] if _eid else f"{s['emoji']} {s['name']}",
            callback_data=f"shop_svc:{key}",
            **{"icon_custom_emoji_id": _eid} if _eid else {}
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(
        text="💬 Другой сервис - написать Александру",
        callback_data="shop_other"
    )])
    rows.append([_eib("Главное меню", "back_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    return text, kb


@dp.callback_query(F.data == "menu_shop")
async def menu_shop(cb: CallbackQuery):
    text, kb = _build_shop_menu()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.message(F.text.contains("Магазин"), StateFilter("*"))
async def reply_shop(message: Message, state: FSMContext):
    """Нижняя reply-кнопка «🛍️ Магазин» — открывает каталог новым сообщением."""
    await state.clear()
    text, kb = _build_shop_menu()
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "shop_other")
async def shop_other(cb: CallbackQuery):
    text = (
        "💬 <b>Другой сервис</b>\n\n"
        "Не нашёл нужный сервис в каталоге?\n"
        "Напиши Александру - оформим любую подписку:\n\n"
        "• Любой AI-сервис\n"
        "• Любой тариф\n"
        "• Оплата в рублях\n\n"
        "👇 Нажми кнопку и напиши что нужно:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✍️ Написать @neirosetkaalex",
            url=f"https://t.me/{PERSONAL_USERNAME}"
        )],
        [InlineKeyboardButton(text="⬅️ В магазин", callback_data="menu_shop")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("shop_cat:"))
async def shop_category(cb: CallbackQuery):
    # Редирект в общий магазин - категории больше не используются
    await menu_shop(cb)


@dp.callback_query(F.data == "menu_subs")
async def menu_subs(cb: CallbackQuery):
    uid = cb.from_user.id
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT service_key, service_name, plan_name, expires_at FROM user_subscriptions "
            "WHERE user_id=$1 AND is_active=TRUE AND expires_at>NOW() ORDER BY expires_at", uid)
    if not rows:
        try:
            await cb.message.edit_text(
                "📋 <b>Мои подписки</b>\n\nУ тебя пока нет активных подписок.\nОформи в разделе 🛍 Магазин.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_eib("Магазин", "menu_shop")],
                    [_eib("Главное меню", "back_main")],
                ]))
        except Exception:
            pass
        await cb.answer()
        return
    import datetime as _dt_s
    lines = ["📋 <b>Мои подписки</b>\n"]
    kb_rows = []
    for r in rows:
        try:
            # expires_at — aware (TIMESTAMPTZ). Берём aware-now, иначе вычитание падает и дни=0.
            _now_aw = _dt_s.datetime.now(r["expires_at"].tzinfo or _dt_s.timezone.utc)
            days = max(0, (r["expires_at"] - _now_aw).days)
        except Exception:
            days = 0
        nm = r["service_name"]; pl = r["plan_name"] or ""
        lines.append(f"\n• <b>{nm}{(' ' + pl) if pl else ''}</b>\n  ещё {days} дн. (до {r['expires_at'].strftime('%d.%m.%Y')})")
        scat = SHOP_CATALOG.get(r["service_key"], {}) or {}
        plans = scat.get("plans", [])
        idx = next((i for i, p in enumerate(plans) if (p.get("name") or "") == pl), 0)
        if r["service_key"] in SHOP_CATALOG:
            kb_rows.append([InlineKeyboardButton(text=f"🔄 Продлить {nm}", callback_data=f"sub_renew:{r['service_key']}:{idx}")])
    kb_rows.append([_eib("Главное меню", "back_main")])
    try:
        await cb.message.edit_text("\n".join(lines), parse_mode="HTML",
                                   reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    except Exception:
        await cb.message.answer("\n".join(lines), parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("sub_renew:"))
async def sub_renew(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    key = parts[1] if len(parts) > 1 else ""
    idx = parts[2] if len(parts) > 2 else "0"
    try:
        cb.data = f"shop_confirm:{key}:{idx}"
        await shop_confirm(cb, state)
    except Exception as _e:
        logging.error(f"sub_renew failed key={key!r} idx={idx!r}: {_e}")
        try:
            await cb.answer()
        except Exception:
            pass
        # Фолбэк: открываем витрину сервиса свежим сообщением
        try:
            cb.data = f"shop_renew:{key}"
            await shop_renew(cb)
        except Exception:
            pass


@dp.callback_query(F.data.startswith("shop_svc:"))
async def shop_service(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    s = SHOP_CATALOG.get(key)
    if not s:
        await cb.answer("Сервис не найден", show_alert=True)
        return
    if not s.get("plans"):
        logging.warning(f"shop_service: no plans for key={key!r}")
        await cb.answer("У этого сервиса пока нет тарифов. Напишите Александру.", show_alert=True)
        return
    _order = sorted(range(len(s["plans"])), key=lambda i: s["plans"][i].get("price", 0))
    plans_text = ""
    for _n, i in enumerate(_order, 1):
        p = s["plans"][i]
        plans_text += f"  {_n}. <b>{p.get('name','')} - {p.get('price',0)}₽/мес</b>\n     <i>{p.get('desc','')}</i>\n"
    text = (
        f"{tg_emoji(s)} <b>{s['name']}</b>\n\n"
        f"<i>{s['desc']}</i>\n\n"
        f"Доступные тарифы:\n{plans_text}\n"
        f"<b>👇 Выбери тариф:</b>"
    )
    rows = []
    for i in _order:
        p = s["plans"][i]
        rows.append([InlineKeyboardButton(
            text=f"{p.get('name','')} - {p.get('price',0)}₽/мес",
            callback_data=f"shop_confirm:{key}:{i}"
        )])
    back_cat = "menu_shop"
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_shop")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("shop_confirm:"))
async def shop_confirm(cb: CallbackQuery, state: FSMContext):
    """Экран подтверждения заказа - до оплаты."""
    parts = cb.data.split(":")
    key = parts[1]
    plan_idx = int(parts[2])
    s = SHOP_CATALOG.get(key)
    if not s or plan_idx >= len(s["plans"]):
        await cb.answer("Ошибка", show_alert=True)
        return
    p = s["plans"][plan_idx]
    uid = cb.from_user.id

    # Проверяем применённый промокод из состояния
    data = await state.get_data()
    promo_code = data.get(f"shop_promo_{key}_{plan_idx}")
    promo_discount = 0
    promo_text = ""
    base_price = p["price"]

    if promo_code:
        ok_p, _, promo = await check_promo_for_user(promo_code, uid)
        if ok_p and promo and promo["kind"] == "percent":
            promo_discount = promo["value"]
            promo_text = f"\n🎟 Промокод <b>{promo_code}</b>: -{promo_discount}%"

    final_price = max(1, int(base_price * (100 - promo_discount) / 100)) if promo_discount > 0 else base_price
    price_line = f"<s>{base_price}₽</s> → <b>{final_price}₽</b>/мес{promo_text}" if promo_discount > 0 else f"<b>{base_price}₽/мес</b>"

    text = (
        f"📋 <b>Подтверждение заказа</b>\n\n"
        f"{tg_emoji(s)} <b>{s['name']} {p['name']}</b>\n"
        f"💵 Стоимость: {price_line}\n\n"
        f"<b>Что входит:</b>\n<i>{p['desc']}</i>\n\n"
        f"Выбери способ оплаты:"
    )

    rows = [
        [InlineKeyboardButton(
            text=f"СБП — {final_price}₽",
            callback_data=f"shop_pay_sbp:{key}:{plan_idx}:{final_price}",
            **pay_btn_kwargs()
        )],
    ]
    if not promo_code:
        rows.append([InlineKeyboardButton(
            text="🎟 Применить промокод",
            callback_data=f"shop_promo_apply:{key}:{plan_idx}"
        )])
    else:
        rows.append([InlineKeyboardButton(
            text=f"❌ Убрать промокод ({promo_code})",
            callback_data=f"shop_promo_remove:{key}:{plan_idx}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shop_svc:{key}")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("shop_promo_apply:"))
async def shop_promo_apply(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    key, plan_idx = parts[1], parts[2]
    await state.set_state(ShopPromoState.waiting_code)
    await state.update_data(shop_promo_key=key, shop_promo_plan=plan_idx)
    await cb.message.answer(
        "🎟 <b>Введи промокод</b>\n\nОтправь код одним сообщением:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data=f"shop_confirm:{key}:{plan_idx}")]
        ])
    )
    await cb.answer()


@dp.message(ShopPromoState.waiting_code)
async def shop_promo_receive(message: Message, state: FSMContext):
    code = (message.text or "").strip().upper()
    data = await state.get_data()
    key = data.get("shop_promo_key", "")
    plan_idx = data.get("shop_promo_plan", "0")
    uid = message.from_user.id

    ok, msg_txt, promo = await check_promo_for_user(code, uid)
    if not ok:
        await message.answer(
            f"❌ {msg_txt}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shop_confirm:{key}:{plan_idx}")]
            ])
        )
        await state.set_state(None)
        return

    if promo["kind"] != "percent":
        await message.answer(
            "⚠️ Этот промокод даёт кредиты, а не скидку на подписку.\n"
            "Для оплаты подписок работают только промокоды со скидкой (%).",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shop_confirm:{key}:{plan_idx}")]
            ])
        )
        await state.set_state(None)
        return

    await state.update_data(**{f"shop_promo_{key}_{plan_idx}": code})
    await state.set_state(None)
    await message.answer(
        f"✅ Промокод <b>{code}</b> применён — скидка {promo['value']}%!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Перейти к оплате", callback_data=f"shop_confirm:{key}:{plan_idx}")]
        ])
    )


@dp.callback_query(F.data.startswith("shop_promo_remove:"))
async def shop_promo_remove(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    key, plan_idx = parts[1], parts[2]
    data = await state.get_data()
    promo_key = f"shop_promo_{key}_{plan_idx}"
    if promo_key in data:
        await state.update_data(**{promo_key: None})
    await cb.answer("Промокод убран")
    # Перерисовываем экран
    fake_cb = type("FakeCB", (), {
        "data": f"shop_confirm:{key}:{plan_idx}",
        "from_user": cb.from_user,
        "message": cb.message,
        "answer": cb.answer,
    })()
    await shop_confirm(fake_cb, state)


@dp.callback_query(F.data.startswith("shop_pay_sbp:"))
async def shop_pay_sbp(cb: CallbackQuery, state: FSMContext):
    """Оплата СБП через FreeKassa."""
    parts = cb.data.split(":")
    key = parts[1]
    plan_idx = int(parts[2])
    s = SHOP_CATALOG.get(key)
    if not s or plan_idx >= len(s.get("plans", [])):
        await cb.answer("Ошибка", show_alert=True)
        return
    p = s["plans"][plan_idx]
    uid = cb.from_user.id
    # SECURITY: цену НЕ берём из callback_data. Промокод — из state, с повторной валидацией.
    _data = await state.get_data()
    _promo_code = _data.get(f"shop_promo_{key}_{plan_idx}")
    promo_final = None
    if _promo_code:
        _ok_p, _, _promo = await check_promo_for_user(_promo_code, uid)
        if _ok_p and _promo and _promo.get("kind") == "percent":
            promo_final = max(1, int(p["price"] * (100 - _promo["value"]) / 100))
    import time as _time
    order_id = f"shop_{uid}_{int(_time.time())}"

    # Считаем итоговую сумму ДО записи в БД — чтобы сохранить правильную сумму
    user_coins = await get_coins(uid)
    # Применяем промокодную цену если передана
    price_after_promo = promo_final if promo_final and promo_final < p["price"] else p["price"]
    coins_used = 0
    if user_coins >= 1:
        coins_used = int(min(user_coins, price_after_promo))
        price_after_promo = max(0, price_after_promo - coins_used)
    final_shop_price = price_after_promo

    # Сохраняем заказ в БД с РЕАЛЬНОЙ суммой (после промокода и монеток)
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO fk_orders (order_id, user_id, credits, amount_rub, pack, promo_code)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (order_id) DO NOTHING
        """, order_id, uid, 0, final_shop_price if final_shop_price > 0 else p["price"],
            f"shop:{key}:{plan_idx}",
            (_promo_code.strip().upper() if (promo_final and promo_final < p["price"] and _promo_code) else None))
        try:
            _onum = await conn.fetchval("SELECT num FROM fk_orders WHERE order_id=$1", order_id)
        except Exception:
            _onum = None
    _onum_str = f"#{_onum}" if _onum else order_id

    pay_url = fk_pay_url(final_shop_price, order_id) if final_shop_price > 0 else None

    coins_line = f"\n🪙 Монетки: <b>−{coins_used}₽</b>" if coins_used > 0 else ""
    has_discount = promo_final and promo_final < p["price"]
    if coins_used > 0 or has_discount:
        price_line = f"<s>{p['price']}₽</s> → <b>{final_shop_price}₽</b>"
    else:
        price_line = f"<b>{p['price']}₽</b>"

    text = (
        f"🏦 <b>Оплата через СБП</b>\n\n"
        f"{tg_emoji(s)} <b>{s['name']} {p['name']}</b>\n"
        f"💵 Сумма: {price_line}{coins_line}\n\n"
        f"После оплаты отправьте чек и номер заказа Александру - он активирует подписку 👇"
    )
    shop_buttons = []
    if coins_used > 0 and final_shop_price == 0:
        # Полностью покрыто монетками
        shop_buttons.append([InlineKeyboardButton(
            text=f"✅ Оплатить монетками ({coins_used}₽)",
            callback_data=f"shop_full_coins:{key}:{plan_idx}:{coins_used}"
        )])
    elif coins_used > 0:
        # Частично монетками + остаток СБП
        shop_buttons.append([InlineKeyboardButton(
            text=f"🪙 Применить {coins_used}₽ монетками + СБП {final_shop_price}₽",
            callback_data=f"shop_coins_sbp:{key}:{plan_idx}:{coins_used}"
        )])
        shop_buttons.append([InlineKeyboardButton(text=f"Оплатить без монеток {p['price']}₽", url=fk_pay_url(p["price"], order_id), **pay_btn_kwargs())])
    else:
        shop_buttons.append([InlineKeyboardButton(text=f"Оплатить {final_shop_price}₽", url=pay_url, **pay_btn_kwargs())])

    kb = InlineKeyboardMarkup(inline_keyboard=shop_buttons + [
        [InlineKeyboardButton(
            text="✅ Я оплатил - написать Александру",
            url="https://t.me/" + PERSONAL_USERNAME + "?text=" + __import__('urllib.parse', fromlist=['quote']).quote(f'Приветствую! Оплатил заказ {_onum_str}\nСервис: {s["name"]}\nТариф: {p["name"]}\nID: {order_id}')
        )],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shop_confirm:{key}:{plan_idx}")],
    ])
    _client_pay_msg_id = None
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        _client_pay_msg_id = cb.message.message_id
    except Exception:
        try:
            _m = await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
            _client_pay_msg_id = _m.message_id
        except Exception:
            pass
    # Сохраняем id сообщения оплаты у клиента — чтобы погасить кнопки после успешной оплаты
    if _client_pay_msg_id:
        try:
            pool_cm = await get_pool()
            async with pool_cm.acquire() as conn_cm:
                await conn_cm.execute(
                    "UPDATE fk_orders SET client_msg_id=$1 WHERE order_id=$2",
                    _client_pay_msg_id, order_id)
        except Exception:
            pass

    # Уведомить Александра - одно сообщение, которое обновится при оплате
    username = cb.from_user.username or cb.from_user.full_name
    try:
        admin_msg = await bot.send_message(
            ADMIN_ID,
            f"🛍 <b>Новый заказ {_onum_str}</b>\n\n"
            f"👤 @{username} (<code>{uid}</code>)\n"
            f"📦 {tg_emoji(s)} {s['name']} {p['name']}\n"
            f"💵 Сумма: <b>{final_shop_price if final_shop_price > 0 else p['price']}₽</b>\n"
            f"💳 Способ: СБП\n"
            f"🆔 Заказ: <code>{order_id}</code>\n\n"
            f"⏳ <b>Статус: ожидает оплаты</b>",
            parse_mode="HTML"
        )
        # Сохраняем message_id в БД для последующего редактирования
        pool2 = await get_pool()
        async with pool2.acquire() as conn2:
            await conn2.execute(
                "UPDATE fk_orders SET admin_msg_id=$1 WHERE order_id=$2",
                admin_msg.message_id, order_id
            )
    except Exception:
        pass
    await cb.answer()


# ── ОПЛАТА МОНЕТКАМИ ──────────────────────────────────────────────────────────

@dp.callback_query(F.data.startswith("pay_coins:"))
async def pay_coins_credits(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    key = parts[1]
    p = CREDIT_PACKS.get(key)
    if not p:
        await cb.answer("Ошибка", show_alert=True)
        return
    uid = cb.from_user.id
    # SECURITY: остаток к доплате считаем на сервере, не из callback_data.
    _data = await state.get_data()
    _promo_code = _data.get("promo_code")
    _price = p["price"]
    if _promo_code:
        _ok_p, _, _promo = await check_promo_for_user(_promo_code, uid)
        if _ok_p and _promo and _promo.get("kind") == "percent":
            _price = max(1, int(p["price"] * (100 - _promo["value"]) / 100))
    user_coins = await get_coins(uid)
    coins_used = min(int(user_coins), _price)
    rest = max(0, _price - coins_used)

    if rest == 0:
        ok = await deduct_coins(uid, coins_used)
        if not ok:
            await cb.answer("Недостаточно монеток.", show_alert=True)
            return
        await add_credits_batch(uid, p["credits"], source="purchase", days_valid=0)
        new_cr = await get_credits(uid)
        new_coins = await get_coins(uid)
        await cb.message.edit_text(
            "\u2705 <b>\u041e\u043f\u043b\u0430\u0447\u0435\u043d\u043e \u043c\u043e\u043d\u0435\u0442\u043a\u0430\u043c\u0438!</b>\n\n"
            f"📦 {p['name']} - {p['credits']} кредитов\n"
            f"🪙 Списано: {coins_used}₽ монетками\n"
            f"💵 Баланс кредитов: <b>{new_cr} кр</b>\n"
            f"🪙 Баланс монеток: <b>{new_coins:.0f}₽</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_eib("Главное меню", "back_main")],
            ])
        )
    else:
        ok = await deduct_coins(uid, coins_used)
        if not ok:
            await cb.answer("Недостаточно монеток.", show_alert=True)
            return
        import time as _t
        order_id = f"cr_{uid}_{int(_t.time())}"
        await fk_save_order(order_id, uid, p["credits"], rest, key)
        try:
            _pool_cs = await get_pool()
            async with _pool_cs.acquire() as _c_cs:
                await _c_cs.execute("UPDATE fk_orders SET coins_spent=$1 WHERE order_id=$2", coins_used, order_id)
        except Exception:
            pass
        pay_url = fk_pay_url(rest, order_id)
        await cb.message.edit_text(
            "\U0001fa99 <b>\u041c\u043e\u043d\u0435\u0442\u043a\u0438 \u043f\u0440\u0438\u043c\u0435\u043d\u0435\u043d\u044b!</b>\n\n"
            f"📦 {p['name']} - {p['credits']} кредитов\n"
            f"🪙 Списано монетками: <b>{coins_used}₽</b>\n"
            f"💵 Осталось доплатить: <b>{rest}₽</b> через СБП",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"Доплатить {rest}₽ через СБП", url=pay_url, **pay_btn_kwargs())],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_buy")],
            ])
        )
    await cb.answer()


@dp.callback_query(F.data.startswith("shop_full_coins:"))
async def shop_full_coins(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    key, plan_idx = parts[1], int(parts[2])
    s = SHOP_CATALOG.get(key)
    if not s or plan_idx >= len(s.get("plans", [])):
        await cb.answer("Ошибка", show_alert=True)
        return
    p = s["plans"][plan_idx]
    uid = cb.from_user.id
    # SECURITY: сумму к списанию считаем на сервере, не из callback_data.
    _data = await state.get_data()
    _promo_code = _data.get(f"shop_promo_{key}_{plan_idx}")
    required = p["price"]
    if _promo_code:
        _ok_p, _, _promo = await check_promo_for_user(_promo_code, uid)
        if _ok_p and _promo and _promo.get("kind") == "percent":
            required = max(1, int(p["price"] * (100 - _promo["value"]) / 100))
    user_coins = await get_coins(uid)
    if user_coins < required:
        await cb.answer("Недостаточно монеток.", show_alert=True)
        return
    coins_used = int(required)
    ok = await deduct_coins(uid, coins_used)
    if not ok:
        await cb.answer("Недостаточно монеток.", show_alert=True)
        return
    new_coins = await get_coins(uid)
    username = cb.from_user.username or cb.from_user.full_name
    await cb.message.edit_text(
        f"\U0001fa99 <b>Оплачено монетками!</b>\n\n"
        f"{tg_emoji(s)} <b>{s['name']} {p['name']}</b>\n"
        f"\U0001fa99 Списано: <b>{coins_used}\u20bd</b>\n"
        f"\U0001fa99 Остаток монеток: <b>{new_coins:.0f}\u20bd</b>\n\n"
        f"Александр активирует подписку в течение часа \U0001f447",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u2705 Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}")],
            [_eib("Главное меню", "back_main")],
        ])
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            f"\U0001fa99 <b>Заказ оплачен монетками (магазин)</b>\n\n"
            f"\U0001f464 @{username} (ID: {uid})\n"
            f"\U0001f4e6 {tg_emoji(s)} {s['name']} {p['name']}\n"
            f"\U0001fa99 Монетки: {coins_used}\u20bd\n"
            f"\U0001f4b5 СБП: 0\u20bd",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("shop_coins_sbp:"))
async def shop_coins_sbp(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    key, plan_idx = parts[1], int(parts[2])
    s = SHOP_CATALOG.get(key)
    if not s or plan_idx >= len(s.get("plans", [])):
        await cb.answer("Ошибка", show_alert=True)
        return
    p = s["plans"][plan_idx]
    uid = cb.from_user.id
    # SECURITY: монетки и доплату считаем на сервере, не из callback_data.
    _data = await state.get_data()
    _promo_code = _data.get(f"shop_promo_{key}_{plan_idx}")
    price_after_promo = p["price"]
    if _promo_code:
        _ok_p, _, _promo = await check_promo_for_user(_promo_code, uid)
        if _ok_p and _promo and _promo.get("kind") == "percent":
            price_after_promo = max(1, int(p["price"] * (100 - _promo["value"]) / 100))
    user_coins = await get_coins(uid)
    coins_used = int(min(user_coins, price_after_promo))
    rest = max(0, price_after_promo - coins_used)
    if coins_used <= 0:
        await cb.answer("Недостаточно монеток.", show_alert=True)
        return
    ok = await deduct_coins(uid, coins_used)
    if not ok:
        await cb.answer("Недостаточно монеток.", show_alert=True)
        return
    import time as _t
    order_id = f"shop_{uid}_{int(_t.time())}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO fk_orders (order_id, user_id, credits, amount_rub, pack, coins_spent) "
            "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (order_id) DO NOTHING",
            order_id, uid, 0, rest, f"shop:{key}:{plan_idx}", coins_used
        )
    pay_url = fk_pay_url(rest, order_id)
    username = cb.from_user.username or cb.from_user.full_name
    import urllib.parse
    msg_text = urllib.parse.quote(
        f"Привет! Оплатил заказ {order_id}\n"
        f"Сервис: {s['name']}\nТариф: {p['name']}\n"
        f"Монетки: {coins_used}\u20bd + СБП: {rest}\u20bd"
    )
    await cb.message.edit_text(
        f"\U0001fa99 <b>Монетки применены!</b>\n\n"
        f"{tg_emoji(s)} <b>{s['name']} {p['name']}</b>\n"
        f"\U0001fa99 Монетками: <b>{coins_used}\u20bd</b>\n"
        f"\U0001f4b5 Доплата СБП: <b>{rest}\u20bd</b>\n\n"
        f"После оплаты напиши Александру \U0001f447",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"\U0001f3e6 Оплатить {rest}\u20bd через СБП", url=pay_url)],
            [InlineKeyboardButton(text="\u2705 Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}?text={msg_text}")],
            [InlineKeyboardButton(text="\u2b05\ufe0f Назад", callback_data=f"shop_confirm:{key}:{plan_idx}")],
        ])
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            f"\U0001fa99 <b>Заказ (монетки + СБП)</b>\n\n"
            f"\U0001f464 @{username} (ID: {uid})\n"
            f"\U0001f4e6 {tg_emoji(s)} {s['name']} {p['name']}\n"
            f"\U0001fa99 Монетки: {coins_used}\u20bd\n"
            f"\U0001f4b5 СБП: {rest}\u20bd\n"
            f"\U0001f194 Заказ: <code>{order_id}</code>",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("shop_pay_stars:"))
async def shop_pay_stars(cb: CallbackQuery):
    """Оплата Telegram Stars."""
    parts = cb.data.split(":")
    key = parts[1]
    plan_idx = int(parts[2])
    s = SHOP_CATALOG.get(key)
    if not s:
        await cb.answer("Ошибка", show_alert=True)
        return
    p = s["plans"][plan_idx]
    uid = cb.from_user.id
    username = cb.from_user.username or cb.from_user.full_name

    # Отправляем invoice Telegram Stars
    try:
        await bot.send_invoice(
            chat_id=uid,
            title=f"{s['name']} {p['name']}",
            description=p["desc"],
            payload=f"shop:{key}:{plan_idx}",
            currency="XTR",
            prices=[LabeledPrice(label=f"{s['name']} {p['name']} - 1 мес", amount=p["stars"])],
        )
        try:
            await cb.message.edit_text(
                f"⭐ <b>Оплата Telegram Stars</b>\n\n"
                f"{tg_emoji(s)} <b>{s['name']} {p['name']}</b>\n"
                f"⭐ Сумма: <b>{p.get('stars', round(p['price']/2.5))} Stars</b>\n\n"
                f"Счёт отправлен выше 👆\n"
                f"После оплаты отправьте скриншот Александру - он активирует подписку.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shop_confirm:{key}:{plan_idx}")],
                ]),
                parse_mode="HTML"
            )
        except Exception:
            pass
    except Exception as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return

    # Уведомить Александра
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🛍 <b>Заказ из магазина (Stars)</b>\n\n"
            f"👤 @{username} (ID: {uid})\n"
            f"📦 {tg_emoji(s)} {s['name']} {p['name']}\n"
            f"⭐ {p['stars']} Stars",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await cb.answer()


@dp.pre_checkout_query()
async def on_pre_checkout(pre_checkout: PreCheckoutQuery):
    """Единый обработчик - подтверждаем оплату Stars для любого payload."""
    await pre_checkout.answer(ok=True)


@dp.message(F.successful_payment)
async def on_successful_payment(message: Message):
    """Единый обработчик Stars-платежей для магазина и пакетов кредитов."""
    payload = message.successful_payment.invoice_payload
    uid = message.from_user.id
    username = message.from_user.username or message.from_user.full_name

    # === 1. Магазин подписок (shop:SERVICE:PLAN_IDX) ===
    if payload.startswith("shop:"):
        parts = payload.split(":")
        key = parts[1]
        plan_idx = int(parts[2])
        s = SHOP_CATALOG.get(key)
        if not s:
            return
        p = s["plans"][plan_idx]

        await message.answer(
            f"✅ <b>Оплата прошла успешно!</b>\n\n"
            f"{tg_emoji(s)} <b>{s['name']} {p['name']}</b> - {p['stars']} ⭐\n\n"
            f"Отправьте скриншот оплаты Александру - он активирует подписку.\n\n"
            f"👇 Напишите напрямую:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="💬 Написать @neirosetkaalex",
                    url="https://t.me/" + PERSONAL_USERNAME + "?text=" + __import__('urllib.parse', fromlist=['quote']).quote(f'Приветствую! Оплатил через Telegram Stars\nСервис: {s["name"]}\nТариф: {p["name"]}')
                )],
                [_eib("Главное меню", "back_main")],
            ]),
            parse_mode="HTML"
        )
        try:
            await bot.send_message(
                ADMIN_ID,
                f"💰 <b>Stars оплачено!</b>\n\n"
                f"👤 @{username} (ID: {uid})\n"
                f"📦 {tg_emoji(s)} {s['name']} {p['name']}\n"
                f"⭐ {p['stars']} Stars получено - активируй подписку!",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    # === 2. Пакеты кредитов (pack:KEY и stars:KEY:UID — оба ведут на пакет кредитов) ===
    if payload.startswith("pack:") or payload.startswith("stars:"):
        parts = payload.split(":")
        key = parts[1]
        p = CREDIT_PACKS.get(key)
        if not p:
            logging.warning(f"Unknown pack key in payment: {key} (payload={payload})")
            return

        await add_credits_batch(uid, p["credits"], source="purchase", days_valid=0)
        await log_payment(uid, p["credits"], p["stars"], "stars")
        await process_referral_bonus(uid)
        cr = await get_credits(uid)
        await message.answer(
            f"🎉 <b>Оплата прошла успешно!</b>\n\n"
            f"➕ Начислено: <b>{p['credits']} кредитов</b>\n"
            f"💵 Баланс: <b>{cr} кредитов</b>\n\n"
            f"Можешь начинать генерацию! 🚀",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📷 Создать фото", callback_data="menu_image")],
                [InlineKeyboardButton(text="🎬 Создать видео", callback_data="menu_video")],
                [_eib("Главное меню", "back_main")],
            ])
        )
        try:
            await bot.send_message(
                ADMIN_ID,
                f"💰 <b>Stars: пакет кредитов куплен</b>\n\n"
                f"👤 @{username} (ID: <code>{uid}</code>)\n"
                f"📦 {p['name']} - {p['credits']} кр\n"
                f"⭐ {p['stars']} Stars",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    logging.warning(f"Unknown successful_payment payload: {payload}")


@dp.callback_query(F.data == "menu_ref")
async def menu_ref(cb: CallbackQuery):
    uid = cb.from_user.id
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            total_refs = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1", uid) or 0
            paid_refs  = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1 AND ref_bonus_paid=TRUE", uid) or 0
            earned_sum = await conn.fetchval(
                "SELECT COALESCE(SUM(CAST(SPLIT_PART(SPLIT_PART(data, 'credits=', 2), ' ', 1) AS INTEGER)), 0) "
                "FROM events WHERE user_id=$1 AND kind='batch_add_referral'", uid
            ) or 0
            # Последние 5 приглашённых - с защитой если колонки нет
            try:
                recent_refs = await conn.fetch(
                    """SELECT u.full_name, u.username, u.ref_bonus_paid,
                              u.created_at::date as joined
                       FROM users u WHERE u.referred_by=$1
                       ORDER BY u.created_at DESC LIMIT 5""",
                    uid
                )
            except Exception as ref_err:
                logging.warning(f"recent_refs query failed: {ref_err}")
                recent_refs = []
    except Exception as e:
        logging.error(f"menu_ref DB error uid={uid}: {e}")
        await cb.answer("⚠️ Ошибка загрузки. Попробуй снова.", show_alert=True)
        return

    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{uid}"
    user_coins = await get_coins(uid)

    # Текущий уровень и следующий бонус
    next_bonus = _ref_bonus_for_count(paid_refs)
    # Статус уровня
    if paid_refs < 5:
        tier = "🥉 Новичок"
        next_level_msg = f"До уровня 🥈 осталось: {5 - paid_refs}"
    elif paid_refs < 10:
        tier = "🥈 Активный"
        next_level_msg = f"До уровня 🥇 осталось: {10 - paid_refs}"
    elif paid_refs < 20:
        tier = "🥇 Опытный"
        next_level_msg = f"До уровня 💎 осталось: {20 - paid_refs}"
    elif paid_refs < 50:
        tier = "💎 Эксперт"
        next_level_msg = f"До уровня 👑 осталось: {50 - paid_refs}"
    else:
        tier = "👑 Топ-реферер"
        next_level_msg = "Максимальный уровень 🔥"

    # Строим список последних приглашённых
    friends_lines = []
    for i, r in enumerate(recent_refs, 1):
        name = r["full_name"] or "Пользователь"
        username = f" (@{r['username']})" if r["username"] else ""
        status = "✅ Купил" if r["ref_bonus_paid"] else "⏳ Не купил"
        joined = r["joined"].strftime("%d.%m") if r["joined"] else ""
        friends_lines.append(f"{i}. {name}{username} · {status} · {joined}")

    friends_block = ""
    if friends_lines:
        friends_block = "\n\n👥 <b>Последние приглашённые:</b>\n" + "\n".join(friends_lines)
    elif total_refs == 0:
        friends_block = "\n\n<i>Ты ещё никого не пригласил</i>"

    text = (
        f"\U0001f91d <b>Пригласить друга</b>\n\n"
        f"<b>Твой уровень: {tier}</b>\n"
        f"<b>За друга сейчас: +{next_bonus} кредитов</b>\n\n"
        f"<b>🎖 Уровни и бонусы:</b>\n"
        f"🥉 1-4 друга · +200 кр\n"
        f"🥈 5-9 друзей · +250 кр\n"
        f"🥇 10-19 друзей · +300 кр\n"
        f"💎 20-49 друзей · +325 кр\n"
        f"👑 50+ друзей · +350 кр\n\n"
        f"❓ <b>Как работает:</b>\n"
        f"1\u20e3 Поделись своей ссылкой\n"
        f"2\u20e3 Друг регистрируется и получает +200 кр\n"
        f"3\u20e3 Друг делает первую покупку → тебе кредиты по уровню\n"
        f"    <b>+ кешбэк 10% монетками</b> 🪙 с его покупки\n"
        f"<i>Монетки можно тратить на любые покупки в боте</i>\n\n"
        f"\U0001f4ca <b>Твоя статистика:</b>\n"
        f"\U0001f465 Приглашено: <b>{total_refs}</b>\n"
        f"\U0001f4b0 Купили: <b>{paid_refs}</b>\n"
        f"\U0001f381 Кредитов заработано: <b>{earned_sum} кр</b>\n"
        f"🪙 Монеток на балансе: <b>{user_coins:.0f}₽</b>\n"
        f"<i>{next_level_msg}</i>"
        f"{friends_block}\n\n"
        f"\U0001f517 <b>Твоя ссылка:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"<i>Нажми на ссылку чтобы скопировать и отправь другу</i>"
    )
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_eib("Главное меню", "back_main")],
        ]), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_eib("Главное меню", "back_main")],
        ]), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "menu_balance")
async def menu_balance(cb: CallbackQuery):
    cr = await get_credits(cb.from_user.id)

    img_keys  = ["img_fast", "img_std", "img_ultra"]
    nano_keys = ["nb_flash", "nb_2", "nb_pro"]
    fal_img_keys = ["flux_pro", "ideogram_v3"]
    vid_keys  = ["vid_lite", "vid_fast", "vid_pro"]
    kling_keys = ["kling_turbo", "kling_pro"]

    def model_line(k, d):
        m = d[k]
        icon = "🔹" if cr >= m['credits'] else "🔸"
        return f"{icon} <b>{m['name']}</b> - <i>{m['credits']} кр</i>"

    img_lines  = [model_line(k, IMAGE_MODELS) for k in img_keys  if k in IMAGE_MODELS]
    nano_lines = [model_line(k, IMAGE_MODELS) for k in nano_keys if k in IMAGE_MODELS]
    fal_img_lines = [model_line(k, IMAGE_MODELS) for k in fal_img_keys if k in IMAGE_MODELS]
    vid_lines  = [model_line(k, VIDEO_MODELS) for k in vid_keys  if k in VIDEO_MODELS]
    kling_lines = [model_line(k, VIDEO_MODELS) for k in kling_keys if k in VIDEO_MODELS]

    text = (
        f"💵 <b>Баланс: {cr} кредитов</b>\n\n"
        f"<b>Доступные модели:</b>\n\n"
        f"🌟 <b>IMAGEN 4</b>\n" + "\n".join(img_lines) + "\n\n"
        f"🍌 <b>NANO BANANA</b>\n" + "\n".join(nano_lines) + "\n\n"
        f"🎨 <b>FLUX &amp; IDEOGRAM</b>\n" + "\n".join(fal_img_lines) + "\n\n"
        f"🎥 <b>VEO 3.1</b>\n" + "\n".join(vid_lines) + "\n\n"
        f"🎞 <b>KLING</b>\n" + "\n".join(kling_lines) + "\n\n"
        f"<i>🔹 доступно · 🔸 нужно пополнить</i>"
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb_buy(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_buy(), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "payment_issue")
async def payment_issue_handler(cb: CallbackQuery):
    """Клиент жалуется что оплатил но кредиты не пришли. 
    
    Шаги:
    1. Сразу запускаем авто-проверку pending заказов этого юзера через FK API
    2. Если нашли оплаченный - зачисляем
    3. Если не нашли - алертим админа и просим клиента подождать"""
    uid = cb.from_user.id
    await cb.answer()
    
    # Промежуточное сообщение
    waiting_msg = await cb.message.answer(
        "⏳ <b>Проверяю твои платежи...</b>\n\n"
        "<i>Это займёт несколько секунд</i>",
        parse_mode="HTML"
    )

    # 1. Ищем pending заказы этого юзера за последний час
    recovered_count = 0
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            pending_rows = await conn.fetch(
                "SELECT order_id, user_id, credits, amount_rub, payment_method, promo_code "
                "FROM fk_orders "
                "WHERE user_id = $1 "
                "  AND status = 'pending' "
                "  AND created_at > NOW() - INTERVAL '24 hours' "
                "ORDER BY created_at DESC",
                uid
            )

        # 2. Для каждого - спрашиваем FK
        for row in pending_rows:
            order_id = row["order_id"]
            try:
                fk_status = await fk_check_order_status(order_id)
                if fk_status and fk_status.get("status") == "paid":
                    payment = {
                        "user_id": row["user_id"],
                        "credits": row["credits"],
                        "amount":  row["amount_rub"],
                        "promo_code": row["promo_code"],
                    }
                    success = await fk_credit_paid_order(order_id, payment, source="auto_check")
                    if success:
                        recovered_count += 1
            except Exception as e:
                logging.error(f"payment_issue check error for {order_id}: {e}")

        # 3. Удаляем промежуточное сообщение
        try:
            await waiting_msg.delete()
        except Exception:
            pass

        if recovered_count > 0:
            await cb.message.answer(
                f"✅ <b>Найдено и зачислено!</b>\n\n"
                f"Восстановили {recovered_count} оплачен{'ный' if recovered_count == 1 else 'ных'} "
                f"заказ{'' if recovered_count == 1 else 'ов'}. Проверь баланс - кредиты на месте 🎉\n\n"
                f"<i>Извини за неудобство 🙏</i>",
                parse_mode="HTML"
            )
        else:
            # Платёж не нашли - алертим админа и просим клиента подождать
            try:
                user_info = await get_user(uid)
                username = (user_info.get("username") or "").strip() if user_info else ""
                full_name = (user_info.get("full_name") or "").strip() if user_info else ""
                user_label = f"@{username}" if username else (full_name or f"ID {uid}")

                pending_count = len(pending_rows) if pending_rows else 0
                pending_info = f"\nPending заказов в БД: <b>{pending_count}</b>" if pending_count else ""

                await bot.send_message(
                    ADMIN_ID,
                    f"📩 <b>Заявка на проверку платежа</b>\n\n"
                    f"👤 {user_label} (<code>{uid}</code>)\n"
                    f"⏰ {_time_module.strftime('%d.%m %H:%M')}{pending_info}\n\n"
                    f"<i>Авто-проверка не нашла оплаченных заказов. "
                    f"Возможно клиент платил через FK без orderId или платёж ещё в обработке.</i>\n\n"
                    f"Проверь личный кабинет FreeKassa или попроси у клиента чек.",
                    parse_mode="HTML"
                )
            except Exception as e:
                logging.error(f"payment_issue admin notify: {e}")

            await cb.message.answer(
                "🔍 <b>Не нашёл оплаченных заказов на твоём аккаунте за последние 24 часа.</b>\n\n"
                "Возможные причины:\n"
                "• Платёж ещё в обработке у банка (это занимает до 30 минут)\n"
                "• Оплата была через ссылку без привязки к аккаунту\n\n"
                "Я уже сообщил администратору о твоей заявке - он проверит и зачислит вручную "
                "в течение 30 минут.\n\n"
                "Если срочно - напиши @neirosetkaalex с чеком об оплате 🙏",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ К пакетам", callback_data="menu_buy")],
                    [_eib("Главное меню", "back_main")],
                ])
            )
    except Exception as e:
        logging.error(f"payment_issue handler error: {e}")
        try:
            await waiting_msg.delete()
        except Exception:
            pass
        await cb.message.answer(
            "⚠️ Не удалось проверить автоматически. Напиши @neirosetkaalex - он разберётся вручную.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_eib("Главное меню", "back_main")],
            ])
        )


@dp.callback_query(F.data == "menu_buy")
async def menu_buy(cb: CallbackQuery):
    cr = await get_credits(cb.from_user.id)
    _pack_fallbacks = {"p15": "🎯", "p25": "🥉", "p50": "🥈", "p150": "🏅", "p500": "🥇", "p1200": "💎"}
    lines = [f"💵 <b>Баланс: {cr} кредитов</b>\n"]
    for key, p in CREDIT_PACKS.items():
        raw_name = p['name'].split(' ', 1)[-1] if ' ' in p['name'] else p['name']
        ename = tg_emoji_ui(f"pack_{key}", _pack_fallbacks.get(key, ""))
        lines.append(
            f"{ename} <b>{raw_name} - {p['credits']} кредитов - {p['price']}₽</b>\n"
            f"<i>{p['desc']}</i>"
        )
    text = "\n\n".join(lines)
    try:
        await cb.message.edit_text(text, reply_markup=kb_buy(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_buy(), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def buy_pack(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    # Блокировка распространяется и на покупку - иначе заблокированный мог бы купить пакет
    if not await check_not_blocked(cb, uid):
        return
    key = cb.data.split(":")[1]
    p = CREDIT_PACKS.get(key)
    if not p:
        await cb.answer("Пакет не найден, попробуй открыть меню заново", show_alert=True)
        return
    data = await state.get_data()
    promo_code = data.get("promo_code")
    promo_discount = 0
    promo_text = ""

    if promo_code:
        ok_p, _, promo = await check_promo_for_user(promo_code, uid)
        if ok_p and promo["kind"] == "percent":
            promo_discount = promo["value"]
            promo_text = f"\n🎟 Промокод <b>{promo_code}</b>: -{promo_discount}%"

    base_price = p["price"]
    final_price = max(1, int(base_price * (100 - promo_discount) / 100)) if promo_discount > 0 else base_price

    _pack_raw_name = p['name'].split(' ', 1)[-1] if ' ' in p['name'] else p['name']
    _pack_orig_emoji = p['name'].split(' ', 1)[0] if ' ' in p['name'] else ""
    _pack_ename = tg_emoji_ui(f"pack_{key}", _pack_orig_emoji)
    msg = (
        f"{_pack_ename} {_pack_raw_name} - <b>{p.get('badge', '')}</b>\n\n"
        f"💎 <b>{p['credits']} кредитов</b>\n"
    )
    if promo_discount > 0:
        msg += f"💰 Цена: <s>{base_price}₽</s> <b>{final_price}₽</b>{promo_text}\n\n"
    else:
        msg += f"💰 Цена: <b>{final_price}₽</b>\n\n"
    msg += (
        f"📦 <i>{p['desc']}</i>\n\n"
        f"Выбери способ оплаты:"
    )

    # Показываем кнопку монеток если есть баланс
    user_coins = await get_coins(cb.from_user.id)
    rows = []
    if user_coins >= 1:
        coins_cover = min(user_coins, final_price)
        rest = max(0, final_price - int(coins_cover))
        if rest == 0:
            rows.append([InlineKeyboardButton(
                text=f"🪙 Оплатить монетками ({int(coins_cover)}₽)",
                callback_data=f"pay_coins:{key}:0"
            )])
        else:
            rows.append([InlineKeyboardButton(
                text=f"🪙 Частично монетками ({int(coins_cover)}₽) + СБП ({rest}₽)",
                callback_data=f"pay_coins:{key}:{rest}"
            )])
    rows.append([InlineKeyboardButton(text=f"Оплатить через СБП - {final_price}₽", callback_data=f"payfk:{key}:sbp", **pay_btn_kwargs())])
    if not promo_code:
        rows.append([InlineKeyboardButton(text="🎟 Применить промокод", callback_data=f"promo_apply:{key}")])
    else:
        rows.append([InlineKeyboardButton(text="❌ Убрать промокод", callback_data=f"promo_remove:{key}")])
    rows.append([_eib("Главное меню", "back_main")])

    await state.update_data(promo_pack=key, promo_final_price=final_price)

    try:
        await cb.message.edit_text(msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    except Exception:
        await cb.message.answer(msg, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("promo_apply:"))
async def promo_apply(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    await state.update_data(promo_pack=key)
    await state.set_state(PromoState.waiting_code)
    await cb.message.answer(
        "🎟 <b>Введи промокод:</b>\n\n"
        "<i>Например: NEWYEAR25</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"buy:{key}")],
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(PromoState.waiting_code)
async def promo_code_input(message: Message, state: FSMContext):
    uid = message.from_user.id
    # Заблокированные не могут применять промокоды (иначе можно фармить)
    if not await check_not_blocked(message, uid):
        await state.clear()
        return
    code = (message.text or "").strip().upper()
    data = await state.get_data()
    key = data.get("promo_pack")

    ok, msg_err, promo = await check_promo_for_user(code, uid)
    if not ok:
        await message.answer(f"❌ {msg_err}")
        return

    if promo["kind"] == "percent":
        # Сохраняем код в state - применится при оплате
        await state.update_data(promo_code=code)
        await state.set_state(None)
        await message.answer(
            f"✅ Промокод применён: скидка <b>{promo['value']}%</b>\n\n"
            f"Возвращаемся к выбору оплаты...",
            parse_mode="HTML"
        )
        # Перерисовываем окно покупки
        class _FakeCB:
            def __init__(self, msg, uid):
                self.message = msg
                self.from_user = type("U", (), {"id": uid})
                self.data = f"buy:{key}"
            async def answer(self, *a, **k): pass
        fake = _FakeCB(message, uid)
        await buy_pack(fake, state)
    elif promo["kind"] == "credits":
        # Начисляем кредиты сразу
        ok_r, msg_ok = await redeem_promo(code, uid)
        await state.clear()
        if ok_r:
            cr = await get_credits(uid)
            await message.answer(
                f"🎉 {msg_ok}\n\n💵 Баланс: <b>{cr} кредитов</b>",
                parse_mode="HTML"
            )
        else:
            await message.answer(f"❌ {msg_ok}")


@dp.callback_query(F.data.startswith("promo_remove:"))
async def promo_remove(cb: CallbackQuery, state: FSMContext):
    await state.update_data(promo_code=None)
    await buy_pack(cb, state)


@dp.callback_query(F.data.startswith("payfk:"))
async def pay_fk(cb: CallbackQuery, state: FSMContext):
    """Оплата через FreeKassa - Card RUB API (id=36) или СБП (id=42)."""
    parts = cb.data.split(":")
    key = parts[1]
    method = parts[2] if len(parts) > 2 else "sbp"
    p = CREDIT_PACKS.get(key)
    if not p:
        await cb.answer("Пакет не найден, открой меню заново", show_alert=True)
        return
    uid = cb.from_user.id

    # Применённый промокод (если есть)
    data = await state.get_data()
    promo_code = data.get("promo_code")
    amount = p["price"]
    if promo_code:
        ok_p, _, promo = await check_promo_for_user(promo_code, uid)
        if ok_p and promo["kind"] == "percent":
            amount = max(1, int(p["price"] * (100 - promo["value"]) / 100))

    import time as _time
    order_id = f"{uid}_{int(_time.time())}"

    pending_fk_payments[order_id] = {
        "user_id": uid,
        "credits": p["credits"],
        "amount": amount,
        "pack": key,
        "promo_code": promo_code,
    }
    # Сохраняем в БД - не потеряется при перезапуске
    try:
        await fk_save_order(
            order_id, uid, p["credits"], int(amount), key,
            payment_method=method,
            promo_code=promo_code
        )
    except Exception as _db_err:
        logging.error(f"pay_fk: fk_save_order failed: {_db_err}")
        # Продолжаем - заказ есть в памяти, оплату сможем обработать

    wait_msg = None
    try:
        wait_msg = await cb.message.answer("⏳ Создаю ссылку на оплату...")
    except Exception:
        pass
    try:
        if method == "card":
            # Card RUB API - пробуем через API (id=36), при ошибке - форма с i=36
            try:
                pay_url = await fk_create_order(amount, order_id, uid, payment_id=36)
                label = "💳 Оплатить картой"
            except Exception as api_err:
                logging.warning(f"Card API failed ({api_err}), falling back to form")
                pay_url = fk_pay_url(amount, order_id, method_id="36")
                label = "💳 Оплатить картой"
        else:
            # СБП - стандартная форма (работает без API)
            pay_url = fk_pay_url(amount, order_id)
            label = "Оплатить через СБП"

        # Уведомляем админа ПЕРВЫМ - до показа URL пользователю,
        # чтобы admin_msg_id точно сохранился до возможного вебхука об оплате
        # (после этого блока запустим мониторинг)
        try:
            username = cb.from_user.username or cb.from_user.full_name
            admin_msg = await bot.send_message(
                ADMIN_ID,
                f"\U0001f4b0 <b>\u041d\u043e\u0432\u044b\u0439 \u0437\u0430\u043a\u0430\u0437</b>\n\n"
                f"\U0001f464 @{username} (<code>{uid}</code>)\n"
                f"\U0001f4e6 {p['credits']} \u043a\u0440\u0435\u0434\u0438\u0442\u043e\u0432\n"
                f"\U0001f4b5 \u0421\u0443\u043c\u043c\u0430: <b>{amount}\u20bd</b>\n"
                f"💳 \u0421\u043f\u043e\u0441\u043e\u0431: \u0421\u0411\u041f\n"
                f"\U0001f194 \u0417\u0430\u043a\u0430\u0437: <code>{order_id}</code>\n\n"
                f"\u23f3 <b>\u0421\u0442\u0430\u0442\u0443\u0441: \u043e\u0436\u0438\u0434\u0430\u0435\u0442 \u043e\u043f\u043b\u0430\u0442\u044b</b>",
                parse_mode="HTML"
            )
            pool3 = await get_pool()
            async with pool3.acquire() as conn3:
                await conn3.execute(
                    "UPDATE fk_orders SET admin_msg_id=$1 WHERE order_id=$2",
                    admin_msg.message_id, order_id
                )
        except Exception as _adm_err:
            logging.error(f"pay_fk admin notify error: {_adm_err}")

        # Показываем платёжную ссылку пользователю ПОСЛЕ сохранения admin_msg_id
        if wait_msg:
            try:
                await wait_msg.delete()
            except Exception:
                pass
        await cb.message.answer(
            f"{label}\n\n"
            f"📦 <b>{p['credits']} кредитов</b> - {amount}₽\n\n"
            f"<b>Шаги:</b>\n"
            f"1️⃣ Нажми кнопку <b>«{label}»</b> и оплати\n"
            f"2️⃣ Возвращайся в бот - кредиты придут <b>автоматически</b> в течение 5-30 секунд\n\n"
            f"<i>Бот проверяет статус оплаты каждые 5 секунд и зачислит кредиты сразу как только платёж пройдёт. "
            f"Если что-то пошло не так - нажми «🔍 Проверить оплату».</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=label, url=pay_url, **(pay_btn_kwargs() if method != "card" else {}))],
                [InlineKeyboardButton(text="🔍 Проверить оплату", callback_data=f"check_pay:{order_id}")],
                [_eib("Главное меню", "back_main")],
            ]),
            parse_mode="HTML"
        )

        # Запускаем мониторинг ПОСЛЕ того как admin_msg_id уже сохранён
        asyncio.create_task(fk_monitor_order(order_id))

    except Exception as e:
        logging.error(f"pay_fk error: {e}")
        try:
            if wait_msg:
                await wait_msg.edit_text(f"❌ Ошибка создания платежа: {e}")
            else:
                await cb.message.answer(f"❌ Ошибка создания платежа: {e}")
        except Exception:
            pass
        pending_fk_payments.pop(order_id, None)
    finally:
        await cb.answer()


@dp.callback_query(F.data.startswith("check_pay:"))
async def check_pay_handler(cb: CallbackQuery):
    """Клиент нажал 'Проверить оплату' - мгновенная проверка конкретного заказа."""
    order_id = cb.data.split(":", 1)[1]
    uid = cb.from_user.id

    await cb.answer("Проверяю...")

    try:
        # 1. Достаём заказ из БД
        db_order = await fk_get_order(order_id)
        if not db_order:
            await cb.message.answer(
                "❌ Заказ не найден в системе.\n\n"
                "Если ты только что создал ссылку - попробуй через 30 секунд.\n"
                "Если ссылка была давно - создай новую через 💵 Баланс.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="💵 К пакетам", callback_data="menu_buy")],
                ])
            )
            return

        # 2. Проверка что это заказ этого юзера (защита)
        if db_order["user_id"] != uid:
            await cb.message.answer("⚠️ Этот заказ принадлежит другому аккаунту.")
            return

        # 3. Уже оплачен?
        if db_order["status"] == "paid":
            cr = await get_credits(uid)
            await cb.message.answer(
                f"✅ <b>Оплата уже зачислена!</b>\n\n"
                f"💵 Текущий баланс: <b>{cr} кредитов</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🖼️ Создать фото", callback_data="menu_image")],
                    [InlineKeyboardButton(text="🎬 Создать видео", callback_data="menu_video")],
                ])
            )
            return

        # 4. Спрашиваем FK API
        wait_msg = await cb.message.answer("⏳ <b>Проверяю статус оплаты...</b>", parse_mode="HTML")
        fk_status = await fk_check_order_status(order_id)

        if fk_status and fk_status.get("status") == "paid":
            # Зачисляем!
            payment = {
                "user_id": db_order["user_id"],
                "credits": db_order["credits"],
                "amount":  db_order["amount_rub"],
                "promo_code": db_order.get("promo_code"),
            }
            success = await fk_credit_paid_order(order_id, payment, source="manual_check")
            try:
                await wait_msg.delete()
            except Exception:
                pass
            if success:
                cr = await get_credits(uid)
                await cb.message.answer(
                    f"🎉 <b>Оплата найдена и зачислена!</b>\n\n"
                    f"➕ Начислено: <b>{db_order['credits']} кредитов</b>\n"
                    f"💵 Баланс: <b>{cr} кредитов</b>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🖼️ Создать фото", callback_data="menu_image")],
                        [InlineKeyboardButton(text="🎬 Создать видео", callback_data="menu_video")],
                    ])
                )
            else:
                # Уже было зачислено пока проверяли (race condition)
                cr = await get_credits(uid)
                await cb.message.answer(
                    f"✅ Оплата уже зачислена. Текущий баланс: <b>{cr} кр</b>",
                    parse_mode="HTML"
                )
        else:
            # Платёж не найден или ещё не прошёл
            try:
                await wait_msg.delete()
            except Exception:
                pass

            status_label = ""
            if fk_status:
                if fk_status.get("status") == "failed":
                    status_label = "\n\n<b>Статус в FreeKassa:</b> платёж отклонён"
                elif fk_status.get("status") == "new":
                    status_label = "\n\n<b>Статус в FreeKassa:</b> ожидает оплаты"

            await cb.message.answer(
                f"⏳ <b>Платёж пока не виден.</b>{status_label}\n\n"
                f"<b>Что делать:</b>\n"
                f"• Если только что оплатил - подожди 30-60 секунд и нажми «Проверить» снова\n"
                f"• Если оплачивал давно - оплата может быть в обработке банка (до 30 минут)\n"
                f"• Если уверен что оплатил - нажми <b>«Сообщить администратору»</b>\n\n"
                f"<i>Бот сам автоматически зачислит кредиты в течение 20 минут после оплаты.</i>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Проверить ещё раз", callback_data=f"check_pay:{order_id}")],
                    [InlineKeyboardButton(text="📩 Сообщить администратору", callback_data=f"report_pay:{order_id}")],
                    [InlineKeyboardButton(text="💵 К пакетам", callback_data="menu_buy")],
                ])
            )
    except Exception as e:
        logging.error(f"check_pay handler error: {e}")
        await cb.message.answer(
            "⚠️ Ошибка при проверке. Попробуй ещё раз через минуту или напиши @neirosetkaalex.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Повторить", callback_data=f"check_pay:{order_id}")],
            ])
        )


@dp.callback_query(F.data.startswith("report_pay:"))
async def report_pay_handler(cb: CallbackQuery):
    """Клиент уверен что оплатил, но бот не нашёл - алертим админа с деталями заказа."""
    order_id = cb.data.split(":", 1)[1]
    uid = cb.from_user.id

    await cb.answer()

    try:
        db_order = await fk_get_order(order_id)
        user_info = await get_user(uid)
        username = (user_info.get("username") or "").strip() if user_info else ""
        full_name = (user_info.get("full_name") or "").strip() if user_info else ""
        user_label = f"@{username}" if username else (full_name or f"ID {uid}")

        order_info = ""
        if db_order:
            order_info = (
                f"\n📦 Пакет: <b>{db_order.get('pack', '?')}</b>"
                f"\n💵 Сумма: <b>{db_order['amount_rub']}₽</b>"
                f"\n💎 Кредитов ожидается: <b>{db_order['credits']}</b>"
                f"\n⏰ Заказ создан: {db_order.get('created_at', '?')}"
                f"\n📊 Статус в БД: <b>{db_order['status']}</b>"
            )

        await bot.send_message(
            ADMIN_ID,
            f"🚨 <b>Заявка от клиента: «оплатил, но не пришло»</b>\n\n"
            f"👤 {user_label} (<code>{uid}</code>)\n"
            f"🆔 Заказ: <code>{order_id}</code>{order_info}\n\n"
            f"<b>Что делать:</b>\n"
            f"1. Проверить FreeKassa личный кабинет - есть ли платёж\n"
            f"2. Если есть - зачислить вручную через ⚖️ Управление балансами\n"
            f"3. Ответить клиенту в @{username or 'личке'}",
            parse_mode="HTML"
        )

        await cb.message.answer(
            "✅ <b>Заявка отправлена администратору.</b>\n\n"
            "Он проверит платёж в течение 30 минут и зачислит кредиты вручную.\n\n"
            "<i>Если очень срочно - напиши лично @neirosetkaalex с чеком об оплате 🙏</i>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_eib("Главное меню", "back_main")],
            ])
        )
    except Exception as e:
        logging.error(f"report_pay error: {e}")
        await cb.message.answer(
            "⚠️ Не удалось отправить заявку. Напиши лично @neirosetkaalex.",
        )


@dp.callback_query(F.data.startswith("paystars:"))
async def pay_stars(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    p = CREDIT_PACKS[key]
    await cb.message.answer_invoice(
        title=f"{p['name']} - {p['credits']} кредитов",
        description=f"Пополнение баланса AI-бота: {p['credits']} кредитов",
        payload=f"stars:{key}:{cb.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(label=p['name'], amount=p['stars'])],
    )
    await cb.answer()


