# -*- coding: utf-8 -*-
# Auto-split module "handlers_gpt" — part of Neirosetkaa-bot (refactored from bot.py).
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
    ADMIN_ID, PERSONAL_USERNAME, WEBAPP_BASE_URL, _BOT_TZ, bot, dp,
    is_admin, SHOP_CATALOG, plan_name_to_key,
)
from runtime_state import (
    rt,
)
from states import (
    GptAdminState,
)
from db import (
    delete_pending_activation, ensure_user, get_next_gpt_code, get_pending_activation, get_pool, log_event,
    release_gpt_code, save_pending_activation,
)
from keyboards import (
    _eib,
)

@dp.message(F.text.startswith("/add_gpt_codes"), StateFilter("*"))
async def admin_add_gpt_codes(message: Message):
    """Добавляет коды ChatGPT. /add_gpt_codes plus\nCODE1\nCODE2"""
    if not is_admin(message.from_user.id):
        return
    lines = message.text.strip().split("\n")
    parts = lines[0].split()
    plan = parts[1].lower() if len(parts) > 1 else "plus"
    if plan not in ("plus", "pro_5x", "pro_max"):
        await message.answer(
            "❌ План: <code>plus</code>, <code>pro_5x</code>, <code>pro_max</code>\n"
            "Пример: <code>/add_gpt_codes plus\nCODE1\nCODE2</code>", parse_mode="HTML")
        return
    codes = [l.strip() for l in lines[1:] if l.strip()]
    if not codes:
        await message.answer("❌ Нет кодов.\n<code>/add_gpt_codes plus\nCODE1</code>", parse_mode="HTML")
        return
    pool = await get_pool()
    added = skipped = 0
    async with pool.acquire() as conn:
        for code in codes:
            try:
                await conn.execute("INSERT INTO gpt_codes (code, plan) VALUES ($1, $2)", code, plan)
                added += 1
            except Exception:
                skipped += 1
    async with pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM gpt_codes WHERE plan=$1 AND is_used=FALSE", plan) or 0
    await message.answer(
        f"✅ <b>Коды добавлены</b>\n\n📦 {plan}\n➕ {added} добавлено  ⏭ {skipped} дублей\n"
        f"📊 Свободных: <b>{remaining}</b>", parse_mode="HTML")


@dp.message(F.text == "/gpt_codes_status", StateFilter("*"))
async def admin_gpt_codes_status(message: Message):
    if not is_admin(message.from_user.id):
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Свободные коды с деталями
        free_rows = await conn.fetch(
            """SELECT plan, code, created_at FROM gpt_codes
               WHERE is_used = FALSE ORDER BY plan, id""")
        # Статистика по тарифам
        stat_rows = await conn.fetch(
            """SELECT plan,
                      COUNT(*) FILTER(WHERE NOT is_used)                     AS free,
                      COUNT(*) FILTER(WHERE is_used AND used_by IS NOT NULL)  AS used,
                      COUNT(*) FILTER(WHERE is_used AND used_by IS NULL)      AS pending
               FROM gpt_codes GROUP BY plan ORDER BY plan""")
    if not stat_rows:
        await message.answer("📭 Кодов нет. Добавь: /add_gpt_codes")
        return

    plan_labels = _gpt_plan_labels()
    # Группируем свободные коды по тарифу
    from collections import defaultdict
    free_by_plan = defaultdict(list)
    for r in free_rows:
        free_by_plan[r["plan"]].append(r["code"])

    lines = ["📊 <b>Коды ChatGPT — статус</b>\n"]
    for r in stat_rows:
        plan = r["plan"]
        label = plan_labels.get(plan, plan)
        icon = "✅" if r["free"] > 2 else ("⚠️" if r["free"] > 0 else "🚨")
        pending_str = f"  ⏳ {r['pending']} ждут" if r["pending"] else ""
        lines.append(
            f"\n{icon} <b>{label}</b>: {r['free']} своб / {r['used']} актив{pending_str}"
        )
        codes = free_by_plan.get(plan, [])
        if codes:
            for i, c in enumerate(codes, 1):
                lines.append(f"  {i}. <code>{c}</code>")
        else:
            lines.append("  <i>свободных нет</i>")

    await message.answer("\n".join(lines), parse_mode="HTML")


# ── Mini App handlers ─────────────────────────────────────────────────────────

def _gpt_plan_labels() -> dict:
    """Имена тарифов ChatGPT по ключу пула кодов (динамически из каталога)."""
    labels = {plan_name_to_key(_p.get("name", "")): _p.get("name", "")
              for _p in SHOP_CATALOG.get("chatgpt", {}).get("plans", [])}
    labels.setdefault("plus", "Plus")
    labels.setdefault("pro_5x", "Pro 5×")
    labels.setdefault("pro_max", "Pro Max")
    return labels


@dp.callback_query(F.data == "adm_gpt_webapp")
async def adm_gpt_webapp_menu(cb: CallbackQuery):
    """Управление ChatGPT Mini App из админки."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT plan,
                      COUNT(*) FILTER(WHERE NOT is_used)                        AS free,
                      COUNT(*) FILTER(WHERE is_used AND used_by IS NOT NULL)    AS activated,
                      COUNT(*) FILTER(WHERE is_used AND used_by IS NULL)        AS reserved
               FROM gpt_codes GROUP BY plan ORDER BY plan""")
        total_activations = await conn.fetchval(
            "SELECT COUNT(*) FROM gpt_codes WHERE is_used=TRUE AND used_by IS NOT NULL") or 0
        last_used = await conn.fetchrow(
            """SELECT code, plan, used_at, used_by
               FROM gpt_codes WHERE is_used=TRUE
               ORDER BY used_at DESC LIMIT 1""")

    by_plan = {r["plan"]: r for r in rows}
    _cg_plans = sorted(SHOP_CATALOG.get("chatgpt", {}).get("plans", []),
                       key=lambda pp: pp.get("price", 0))
    _ordered = []
    for _p in _cg_plans:
        _k = plan_name_to_key(_p.get("name", ""))
        if _k not in [x[0] for x in _ordered]:
            _ordered.append((_k, _p.get("name", _k)))
    for _k in by_plan:
        if _k not in [x[0] for x in _ordered]:
            _ordered.append((_k, _k))
    codes_text = ""
    for _k, _name in _ordered:
        r = by_plan.get(_k)
        free = r["free"] if r else 0
        activated = r["activated"] if r else 0
        reserved = r["reserved"] if r else 0
        icon = "✅" if free > 2 else ("⚠️" if free > 0 else "🚨")
        reserved_str = f" / ⏳ {reserved} ждут" if reserved > 0 else ""
        codes_text += f"\n{icon} <b>{_name}</b>: {free} свободных / {activated} активированных{reserved_str}"
    if not codes_text:
        codes_text = "\n📭 Кодов нет — добавь через кнопку ниже"

    last_txt = ""
    if last_used:
        import datetime as _dt
        used_at = last_used["used_at"]
        if used_at:
            used_str = used_at.strftime("%d.%m %H:%M") if hasattr(used_at, 'strftime') else str(used_at)[:16]
            last_txt = f"\n\n⏱ Последняя активация: <code>{last_used['code']}</code> ({used_str})"

    status = "✅ ВКЛЮЧЁН" if rt.chatgpt_webapp_enabled else "🔴 ВЫКЛЮЧЕН (тех. работы)"
    text = (
        f"✨ <b>ChatGPT Mini App</b>\n\n"
        f"Статус: <b>{status}</b>\n\n"
        f"<b>Коды активации:</b>{codes_text}\n"
        f"📊 Всего активаций: <b>{total_activations}</b>"
        f"{last_txt}\n\n"
        f"<i>При выключении клиенты получают сообщение о тех. работах</i>"
    )
    toggle_text = "🔴 Выключить (тех. работы)" if rt.chatgpt_webapp_enabled else "✅ Включить"
    _add_rows, _cur = [], []
    for _k, _name in _ordered:
        _cur.append(InlineKeyboardButton(text=f"➕ {_name}", callback_data=f"adm_gpt_add:{_k}"))
        if len(_cur) == 2:
            _add_rows.append(_cur); _cur = []
    if _cur:
        _add_rows.append(_cur)
    if not _add_rows:
        _add_rows = [[InlineKeyboardButton(text="➕ Добавить коды", callback_data="adm_gpt_add:plus")]]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data="adm_gpt_toggle")],
        *_add_rows,
        [InlineKeyboardButton(text="📦 Свободные коды",     callback_data="adm_gpt_free:plus"),
         InlineKeyboardButton(text="⏳ Ждущие коды",         callback_data="adm_gpt_pending:plus")],
        [InlineKeyboardButton(text="📋 История активаций",   callback_data="adm_gpt_history:0")],
        [InlineKeyboardButton(text="⬅️ Назад в панель",      callback_data="adm_back")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "adm_gpt_toggle")
async def adm_gpt_toggle(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return

    rt.chatgpt_webapp_enabled = not rt.chatgpt_webapp_enabled
    status = "✅ ВКЛЮЧЁН" if rt.chatgpt_webapp_enabled else "🔴 ВЫКЛЮЧЕН"
    await cb.answer(f"Mini App {status}", show_alert=True)
    await adm_gpt_webapp_menu(cb)


@dp.callback_query(F.data.startswith("adm_gpt_add:"))
async def adm_gpt_add_start(cb: CallbackQuery, state: FSMContext):
    """Начало добавления кодов — запрашиваем список."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    plan = cb.data.split(":")[1]
    await state.update_data(gpt_add_plan=plan)
    await state.set_state(GptAdminState.waiting_codes)
    plan_labels = _gpt_plan_labels()
    await cb.message.answer(
        f"➕ <b>Добавление кодов — {plan_labels.get(plan, plan)}</b>\n\n"
        f"Отправь коды — каждый с новой строки:\n\n"
        f"<code>CODE1\nCODE2\nCODE3</code>\n\n"
        f"Или отправь /cancel чтобы отменить",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(GptAdminState.waiting_codes, StateFilter("*"))
async def adm_gpt_codes_input(message: Message, state: FSMContext):
    """Получаем коды и сохраняем в БД."""
    if message.from_user.id != ADMIN_ID:
        return
    if message.text and message.text.strip() == "/cancel":
        await state.clear()
        await message.answer("❌ Отменено")
        return
    data = await state.get_data()
    plan = data.get("gpt_add_plan", "plus")
    codes = [l.strip() for l in (message.text or "").split("\n") if l.strip()]
    if not codes:
        await message.answer("❌ Нет кодов. Отправь каждый код с новой строки.")
        return
    pool = await get_pool()
    added = skipped = 0
    async with pool.acquire() as conn:
        for code in codes:
            try:
                await conn.execute("INSERT INTO gpt_codes (code, plan) VALUES ($1, $2)", code, plan)
                added += 1
            except Exception:
                skipped += 1
    async with pool.acquire() as conn:
        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM gpt_codes WHERE plan=$1 AND is_used=FALSE", plan) or 0
    await state.clear()
    plan_labels = _gpt_plan_labels()
    await message.answer(
        f"✅ <b>Коды добавлены!</b>\n\n"
        f"📦 План: <b>{plan_labels.get(plan, plan)}</b>\n"
        f"➕ Добавлено: <b>{added}</b>\n"
        f"⏭ Дублей: <b>{skipped}</b>\n"
        f"📊 Свободных теперь: <b>{remaining}</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад к Mini App", callback_data="adm_gpt_webapp")],
        ])
    )


@dp.callback_query(F.data.startswith("adm_gpt_history:"))
async def adm_gpt_history(cb: CallbackQuery):
    """История использования кодов с пагинацией."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    offset = int(cb.data.split(":")[1])
    limit = 8
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT gc.code, gc.plan, gc.used_at, gc.used_by, gc.order_id, gc.email,
                      u.username, u.full_name
               FROM gpt_codes gc
               LEFT JOIN users u ON u.user_id = gc.used_by
               WHERE gc.is_used = TRUE AND gc.used_by IS NOT NULL
               ORDER BY gc.used_at DESC
               LIMIT $1 OFFSET $2""",
            limit + 1, offset)
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM gpt_codes WHERE is_used=TRUE AND used_by IS NOT NULL") or 0

    has_more = len(rows) > limit
    rows = rows[:limit]

    if not rows:
        await cb.answer("Нет использованных кодов", show_alert=True)
        return

    plan_labels = _gpt_plan_labels()
    lines = [f"📋 <b>История активаций</b> (всего {total}):\n"]
    for idx, r in enumerate(rows, start=offset + 1):
        used_at = r["used_at"]
        used_str = used_at.strftime("%d.%m.%y %H:%M") if used_at and hasattr(used_at, "strftime") else "—"
        plan_name = plan_labels.get(r["plan"], r["plan"])
        email_str  = r["email"] or "—"
        uid_str    = str(r["used_by"]) if r["used_by"] else "—"
        uname      = r["username"] or ""
        fname      = r["full_name"] or ""
        tg_nick    = f"@{uname}" if uname else (fname if fname else f"id{uid_str}")
        lines.append(
            f"\n{idx}. {tg_nick}  <i>{used_str}</i>\n"
            f"📧 {email_str}\n"
            f"🔑 <code>{r['code']}</code>"
        )

    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_gpt_history:{offset-limit}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_gpt_history:{offset+limit}"))

    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_gpt_webapp")])

    try:
        await cb.message.edit_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            parse_mode="HTML"
        )
    except Exception:
        await cb.message.answer(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
            parse_mode="HTML"
        )
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_gpt_free:"))
async def adm_gpt_free_codes(cb: CallbackQuery):
    """Просмотр свободных (неиспользованных) кодов с возможностью удалить."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return

    parts = cb.data.split(":")
    plan = parts[1]
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    PER = 20
    plan_labels = _gpt_plan_labels()

    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM gpt_codes WHERE plan=$1 AND is_used=FALSE", plan) or 0
        pages = max(1, (total + PER - 1) // PER)
        if page >= pages: page = pages - 1
        if page < 0: page = 0
        rows = await conn.fetch(
            """SELECT id, code, created_at FROM gpt_codes
               WHERE plan=$1 AND is_used=FALSE
               ORDER BY id ASC LIMIT $2 OFFSET $3""", plan, PER, page * PER)

    plan_nav = [
        InlineKeyboardButton(text="Plus",    callback_data="adm_gpt_free:plus"),
        InlineKeyboardButton(text="Pro 5×",  callback_data="adm_gpt_free:pro_5x"),
        InlineKeyboardButton(text="Pro Max", callback_data="adm_gpt_free:pro_max"),
    ]

    if not rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            plan_nav,
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_gpt_webapp")],
        ])
        try:
            await cb.message.edit_text(
                f"📦 <b>Свободные коды — {plan_labels.get(plan, plan)}</b>\n\n"
                f"📭 Кодов нет. Добавь через «➕ Добавить».",
                parse_mode="HTML", reply_markup=kb
            )
        except Exception:
            pass
        await cb.answer()
        return

    lines = [f"📦 <b>Свободные коды — {plan_labels.get(plan, plan)}</b> (всего {total}) · стр. {page+1}/{pages}\n"]
    code_btns = []
    for r in rows:
        created = r["created_at"]
        date_str = created.strftime("%d.%m.%y") if created and hasattr(created, "strftime") else "—"
        lines.append(f"• <code>{r['code']}</code>  <i>{date_str}</i>")
        code_btns.append([
            InlineKeyboardButton(
                text=f"🗑 {r['code']}",
                callback_data=f"adm_gpt_del_code:{r['id']}:{plan}"
            )
        ])

    page_nav = []
    if page > 0:
        page_nav.append(InlineKeyboardButton(text="‹ Пред", callback_data=f"adm_gpt_free:{plan}:{page-1}"))
    if page < pages - 1:
        page_nav.append(InlineKeyboardButton(text="След ›", callback_data=f"adm_gpt_free:{plan}:{page+1}"))
    _kbrows = [plan_nav, *code_btns]
    if page_nav: _kbrows.append(page_nav)
    _kbrows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_gpt_webapp")])
    kb = InlineKeyboardMarkup(inline_keyboard=_kbrows)
    try:
        await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    except Exception:
        await cb.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_gpt_del_code:"))
async def adm_gpt_del_code(cb: CallbackQuery):
    """Шаг 1 — показываем подтверждение перед удалением."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    parts = cb.data.split(":")
    code_id, plan = int(parts[1]), parts[2]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT code, is_used FROM gpt_codes WHERE id=$1", code_id)
    if not row:
        await cb.answer("Код не найден", show_alert=True)
        return
    if row["is_used"]:
        await cb.answer("❌ Код уже использован, нельзя удалить", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить",
                              callback_data=f"adm_gpt_del_confirm:{code_id}:{plan}")],
        [InlineKeyboardButton(text="❌ Отмена",
                              callback_data=f"adm_gpt_free:{plan}")],
    ])
    try:
        await cb.message.edit_text(
            f"🗑 <b>Удалить код?</b>\n\n"
            f"<code>{row['code']}</code>\n\n"
            f"Это действие нельзя отменить.",
            parse_mode="HTML", reply_markup=kb
        )
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_gpt_del_confirm:"))
async def adm_gpt_del_confirm(cb: CallbackQuery):
    """Шаг 2 — подтверждение получено, удаляем код."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    parts = cb.data.split(":")
    code_id, plan = int(parts[1]), parts[2]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT code, is_used FROM gpt_codes WHERE id=$1", code_id)
        if not row:
            await cb.answer("Код не найден", show_alert=True)
            return
        if row["is_used"]:
            await cb.answer("❌ Код уже использован", show_alert=True)
            return
        await conn.execute("DELETE FROM gpt_codes WHERE id=$1", code_id)
    await cb.answer(f"✅ Код {row['code']} удалён", show_alert=True)
    cb.data = f"adm_gpt_free:{plan}"
    await adm_gpt_free_codes(cb)





@dp.callback_query(F.data.startswith("adm_gpt_pending:"))
async def adm_gpt_pending_codes(cb: CallbackQuery):
    """Просмотр ждущих (зарезервированных) кодов — is_used=TRUE, used_by=NULL."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return

    plan = cb.data.split(":")[1]
    plan_labels = _gpt_plan_labels()

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT gc.id, gc.code, gc.reserved_at,
                      pa.user_id AS pa_uid, u.username, u.full_name
               FROM gpt_codes gc
               LEFT JOIN gpt_pending_activations pa ON pa.code = gc.code
               LEFT JOIN users u ON u.user_id = pa.user_id
               WHERE gc.plan = $1 AND gc.is_used = TRUE AND gc.used_by IS NULL
               ORDER BY gc.reserved_at ASC NULLS LAST
               LIMIT 20""", plan)
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM gpt_codes WHERE plan=$1 AND is_used=TRUE AND used_by IS NULL", plan) or 0

    plan_nav = [
        InlineKeyboardButton(text="Plus",    callback_data="adm_gpt_pending:plus"),
        InlineKeyboardButton(text="Pro 5×",  callback_data="adm_gpt_pending:pro_5x"),
        InlineKeyboardButton(text="Pro Max", callback_data="adm_gpt_pending:pro_max"),
    ]

    if not rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            plan_nav,
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_gpt_webapp")],
        ])
        try:
            await cb.message.edit_text(
                f"⏳ <b>Ждущие коды — {plan_labels.get(plan, plan)}</b>\n\n"
                f"Нет зарезервированных кодов.",
                parse_mode="HTML", reply_markup=kb
            )
        except Exception:
            pass
        await cb.answer()
        return

    lines = [f"⏳ <b>Ждущие коды — {plan_labels.get(plan, plan)}</b> (всего {total}):\n"
             f"<i>Зарезервированы, но клиент ещё не активировал</i>\n"]
    code_btns = []
    for r in rows:
        reserved = r["reserved_at"]
        date_str = reserved.astimezone(_BOT_TZ).strftime("%d.%m %H:%M") if reserved and hasattr(reserved, "strftime") else "—"
        uname = r["username"] or r["full_name"] or (f"id{r['pa_uid']}" if r["pa_uid"] else "—")
        tg_str = f"@{uname}" if r["username"] else uname
        lines.append(f"• <code>{r['code']}</code>  👤 {tg_str}  ⏱ {date_str}")
        code_btns.append([
            InlineKeyboardButton(
                text=f"🔓 В пул",
                callback_data=f"adm_gpt_release:{r['id']}:{plan}"
            ),
            InlineKeyboardButton(
                text=f"🗑 {r['code']}",
                callback_data=f"adm_gpt_del_pending:{r['id']}:{plan}"
            ),
        ])

    kb = InlineKeyboardMarkup(inline_keyboard=[
        plan_nav,
        *code_btns,
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_gpt_webapp")],
    ])
    try:
        await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    except Exception:
        await cb.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_gpt_release:"))
async def adm_gpt_release_code(cb: CallbackQuery):
    """Шаг 1 — подтверждение перед возвратом ждущего кода в пул."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    parts = cb.data.split(":")
    code_id, plan = int(parts[1]), parts[2]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT code FROM gpt_codes WHERE id=$1", code_id)
    if not row:
        await cb.answer("Код не найден", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, вернуть в пул",
                              callback_data=f"adm_gpt_release_confirm:{code_id}:{plan}")],
        [InlineKeyboardButton(text="❌ Отмена",
                              callback_data=f"adm_gpt_pending:{plan}")],
    ])
    try:
        await cb.message.edit_text(
            f"🔓 <b>Вернуть код в пул?</b>\n\n"
            f"<code>{row['code']}</code>\n\n"
            f"Код станет свободным и может быть выдан другому клиенту.",
            parse_mode="HTML", reply_markup=kb
        )
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_gpt_release_confirm:"))
async def adm_gpt_release_confirm(cb: CallbackQuery):
    """Шаг 2 — возвращаем ждущий код в пул."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    parts = cb.data.split(":")
    code_id, plan = int(parts[1]), parts[2]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT code, used_by FROM gpt_codes WHERE id=$1", code_id)
        if not row:
            await cb.answer("Код не найден", show_alert=True)
            return
        if row["used_by"] is not None:
            await cb.answer("❌ Код уже активирован, нельзя вернуть", show_alert=True)
            return
        # Возвращаем код в пул
        await conn.execute(
            "UPDATE gpt_codes SET is_used=FALSE, reserved_at=NULL WHERE id=$1", code_id
        )
        # Удаляем pending активацию если есть
        await conn.execute(
            "DELETE FROM gpt_pending_activations WHERE code=$1", row["code"]
        )
    await cb.answer(f"✅ Код {row['code']} возвращён в пул", show_alert=True)
    cb.data = f"adm_gpt_pending:{plan}"
    await adm_gpt_pending_codes(cb)


@dp.callback_query(F.data.startswith("adm_gpt_del_pending:"))
async def adm_gpt_del_pending(cb: CallbackQuery):
    """Подтверждение перед удалением ждущего кода."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    parts = cb.data.split(":")
    code_id, plan = int(parts[1]), parts[2]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT code, used_by FROM gpt_codes WHERE id=$1", code_id)
    if not row:
        await cb.answer("Код не найден", show_alert=True)
        return
    if row["used_by"] is not None:
        await cb.answer("❌ Код уже активирован", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить",
                              callback_data=f"adm_gpt_del_pending_confirm:{code_id}:{plan}")],
        [InlineKeyboardButton(text="❌ Отмена",
                              callback_data=f"adm_gpt_pending:{plan}")],
    ])
    try:
        await cb.message.edit_text(
            f"🗑 <b>Удалить ждущий код?</b>\n\n"
            f"<code>{row['code']}</code>\n\n"
            f"Код и его pending-активация будут удалены. Клиент не сможет активировать.",
            parse_mode="HTML", reply_markup=kb
        )
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_gpt_del_pending_confirm:"))
async def adm_gpt_del_pending_confirm(cb: CallbackQuery):
    """Удаляем ждущий код и его pending-активацию."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    parts = cb.data.split(":")
    code_id, plan = int(parts[1]), parts[2]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT code, used_by FROM gpt_codes WHERE id=$1", code_id)
        if not row:
            await cb.answer("Код не найден", show_alert=True)
            return
        if row["used_by"] is not None:
            await cb.answer("❌ Код уже активирован, нельзя удалить", show_alert=True)
            return
        await conn.execute("DELETE FROM gpt_pending_activations WHERE code=$1", row["code"])
        await conn.execute("DELETE FROM gpt_codes WHERE id=$1", code_id)
    await cb.answer(f"✅ Код {row['code']} удалён", show_alert=True)
    cb.data = f"adm_gpt_pending:{plan}"
    await adm_gpt_pending_codes(cb)


@dp.message(F.text == "/test_gpt_webapp", StateFilter("*"))
async def test_gpt_webapp(message: Message):
    """Тест Mini App кнопки без оплаты. Только для админа."""
    if not is_admin(message.from_user.id):
        return
    uid = message.from_user.id
    import random, string as _string, urllib.parse as _uparse
    suffix = "".join(random.choices(_string.ascii_uppercase + _string.digits, k=12))
    code = f"TEST-{suffix}"  # фейковый код — реальные из пула НЕ тратятся
    await save_pending_activation(uid, code, f"TEST-ORD-{suffix[:6]}", "plus", "Plus")
    webapp_url = f"{WEBAPP_BASE_URL}/webapp/chatgpt?plan={_uparse.quote('Plus')}&code={_uparse.quote(code)}"
    from aiogram.types import WebAppInfo
    await message.answer(
        f"🧪 <b>Тест Mini App (фейковый код)</b>\n\nКод: <code>{code}</code>\n"
        f"<i>Реальные коды из пула не тратятся</i>\nНажми кнопку 👇",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="✨ Активировать подписку", style="success",
                web_app=WebAppInfo(url=webapp_url)
            )],
        ])
    )
    await message.answer(
        "📋 <b>Инструкция по активации ChatGPT</b>\n\n"
        "1️⃣ Зайди на <b>chatgpt.com</b> и авторизуйся (в Chrome или Safari).\n"
        "2️⃣ В том же браузере открой страницу с токеном:\n"
        "<code>chatgpt.com/api/auth/session</code>\n"
        "3️⃣ Скопируй <b>весь</b> текст страницы целиком.\n"
        "4️⃣ Вернись в мини-приложение (кнопка «Активировать подписку»), "
        "вставь токен — подписка активируется автоматически за 1–2 минуты.\n\n"
        f"🎟 Код активации: <code>{code}</code>\n"
        "⚠️ Аккаунт должен быть на бесплатном плане.",
        parse_mode="HTML")


@dp.callback_query(F.data == "gpt_need_help")
async def cb_gpt_need_help(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    await ensure_user(uid, cb.from_user.username or '', cb.from_user.full_name)
    await cb.message.answer(
        "❓ <b>Нужна помощь с активацией?</b>\n\n"
        "Напиши Александру — он активирует вручную в течение 15–30 минут.\n\n"
        "После того как Александр активировал твою подписку — нажми кнопку ниже 👇",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="💬 Написать Александру",
                url=f"https://t.me/{PERSONAL_USERNAME}"
            )],
            [InlineKeyboardButton(
                text="✅ Активировали тариф вручную",
                callback_data="gpt_manual_activated"
            )],
        ])
    )
    try:
        pending = await get_pending_activation(uid)
        code_info = (
            f"\n🔑 Код: <code>{pending['code']}</code>"
            f"\n📦 Тариф: <b>{pending.get('plan_name', '?')}</b>"
        ) if pending else ""
        await bot.send_message(
            ADMIN_ID,
            f"❓ <b>Клиент нажал «Нужна помощь»</b>\n\n"
            f"👤 <code>{uid}</code>{code_info}\n\n"
            f"Активируй вручную и попроси клиента нажать «Активировали тариф вручную».",
            parse_mode="HTML"
        )
    except Exception:
        pass


@dp.callback_query(F.data == "gpt_manual_activated")
async def cb_gpt_manual_activated(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    pending = await get_pending_activation(uid)
    if pending:
        code = pending["code"]
        plan_name = pending.get("plan_name", "?")
        await release_gpt_code(code)
        await delete_pending_activation(uid)
        await log_event(uid, "manual_activated", f"code={code} plan={plan_name}")
        await cb.message.answer(
            "✅ <b>Готово!</b>\n\n"
            "Сессия закрыта. Можешь заходить в ChatGPT и пользоваться 🎉\n\n"
            "Если возникнут вопросы — пиши @neirosetkaalex",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_eib("Главное меню", "back_main")]
            ])
        )
        try:
            await bot.send_message(
                ADMIN_ID,
                f"✅ <b>Ручная активация подтверждена клиентом</b>\n\n"
                f"👤 <code>{uid}</code>\n"
                f"🔑 Код: <code>{code}</code> — возвращён в пул\n"
                f"📦 Тариф: <b>{plan_name}</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    else:
        await cb.message.answer(
            "ℹ️ Активная сессия не найдена — возможно уже завершена ранее.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_eib("Главное меню", "back_main")]
            ])
        )




# ── Помощь с активацией Claude ──────────────────────────────────────────────

