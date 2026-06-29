# -*- coding: utf-8 -*-
# Auto-split module "handlers_perplexity" — part of Neirosetkaa-bot (refactored from bot.py).
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
    ADMIN_ID, PERSONAL_USERNAME, WEBAPP_BASE_URL, bot, dp,
)
from runtime_state import (
    rt,
)
from states import (
    PerplexityAdminState,
)
from db import (
    count_perplexity_codes_by_plan, delete_perplexity_pending_activation, ensure_user, get_perplexity_pending_activation, get_next_perplexity_code, get_pool,
    log_event, mark_perplexity_code_used, release_perplexity_code, save_perplexity_pending_activation,
)
from keyboards import (
    _eib,
)
from common import (
    _send_perplexity_webapp_to_user,
)

@dp.callback_query(F.data == "perplexity_need_help")
async def cb_perplexity_need_help(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    await ensure_user(uid, cb.from_user.username or '', cb.from_user.full_name)
    await cb.message.answer(
        "❓ <b>Нужна помощь с активацией Perplexity?</b>\n\n"
        "Напиши Александру — активирует вручную в течение 15\u201330 минут.\n\n"
        "После того как Александр активировал твою подписку — нажми кнопку ниже \U0001f447",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="\u2705 Александр уже активировал",
                callback_data="perplexity_manual_activated"
            )],
            [InlineKeyboardButton(
                text="\U0001f4ac Написать Александру",
                url=f"https://t.me/{PERSONAL_USERNAME}"
            )],
        ])
    )
    try:
        pending = await get_perplexity_pending_activation(uid)
        code_info = (
            f"\n\U0001f511 Код: <code>{pending['code']}</code>"
            f"\n\U0001f4e6 Тариф: <b>{pending.get('plan_name', '?')}</b>"
        ) if pending else ""
        await bot.send_message(
            ADMIN_ID,
            "❓ <b>Клиент нажал «Нужна помощь» — Perplexity</b>\n\n"
            f"\U0001f464 <code>{uid}</code>{code_info}\n\n"
            "Активируй Perplexity вручную и попроси клиента нажать «Александр уже активировал».",
            parse_mode="HTML"
        )
    except Exception:
        pass


@dp.callback_query(F.data == "perplexity_manual_activated")
async def cb_perplexity_manual_activated(cb: CallbackQuery):
    await cb.answer()
    uid = cb.from_user.id
    pending = await get_perplexity_pending_activation(uid)
    if pending:
        code = pending["code"]
        plan_name = pending.get("plan_name", "?")
        # Помечаем код как использованный вручную (без bpa)
        await mark_perplexity_code_used(code, uid, pending.get("order_id", ""), pending.get("org_id", ""))
        await delete_perplexity_pending_activation(uid)
        await log_event(uid, "perplexity_manual_activated", f"code={code} plan={plan_name}")
        await cb.message.answer(
            "\u2705 <b>Готово!</b>\n\n"
            "Подписка активирована. Можешь заходить в Perplexity и пользоваться \U0001f389\n\n"
            "Если возникнут вопросы — пиши @neirosetkaalex",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_eib("Главное меню", "back_main")]
            ])
        )
        try:
            await bot.send_message(
                ADMIN_ID,
                "\u2705 <b>Ручная активация Perplexity подтверждена клиентом</b>\n\n"
                f"\U0001f464 <code>{uid}</code>\n"
                f"\U0001f511 Код: <code>{code}</code>\n"
                f"\U0001f4e6 Тариф: <b>{plan_name}</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    else:
        await cb.message.answer(
            "\u2139\ufe0f Активная сессия не найдена — возможно уже завершена ранее.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_eib("Главное меню", "back_main")]
            ])
        )


# ══════════════════════════════════════════════════════════
#  GPT CODE RECHECKER — фоновая проверка кодов активации
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "perplexity_reopen_webapp")
async def perplexity_reopen_webapp(cb: CallbackQuery):
    """Клиент нажал кнопку повторно — переотправляем WebApp если есть pending."""
    uid = cb.from_user.id
    pending = await get_perplexity_pending_activation(uid)
    if not pending:
        await cb.answer(
            "⚠️ Сессия истекла. Напиши Александру для нового кода.",
            show_alert=True
        )
        return
    import urllib.parse as _up3
    from aiogram.types import WebAppInfo as _WAI3
    webapp_url = (
        f"{WEBAPP_BASE_URL}/webapp/perplexity"
        f"?plan={_up3.quote(pending['plan_name'])}"
        f"&code={_up3.quote(pending['code'])}"
    )
    await cb.message.answer(
        f"⚡ <b>Активация Perplexity {pending['plan_name']}</b>\n\n"
        f"Нажми кнопку, введи Perplexity User ID (perplexity.ai/api/auth/session) — "
        f"подписка активируется автоматически за 1–2 минуты.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text=f"⚡ Активировать Perplexity {pending['plan_name']}", style="success",
                web_app=_WAI3(url=webapp_url)
            )],
            [InlineKeyboardButton(
                text="❓ Нужна помощь", style="primary",
                callback_data="perplexity_need_help"
            )],
        ])
    )
    await cb.answer()


# ─── Администрирование Perplexity Mini App ───────────────────────────────────────

@dp.callback_query(F.data == "adm_perplexity_webapp")
async def adm_perplexity_webapp_menu(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    data = await count_perplexity_codes_by_plan()
    by_plan    = data.get("by_plan", {})
    total_act  = data.get("total_activations", 0)
    last_used  = data.get("last_used")

    LABELS = {"pro": "Perplexity Pro", "max_5x": "Perplexity Max 5×", "max_20x": "Perplexity Max 20×"}
    codes_text = ""
    for key, label in LABELS.items():
        s = by_plan.get(key, {"free": 0, "activated": 0, "reserved": 0})
        icon = "✅" if s["free"] > 2 else ("⚠️" if s["free"] > 0 else "🚨")
        reserved_str = f" / ⏳ {s['reserved']} ждут" if s.get("reserved", 0) > 0 else ""
        codes_text += f"\n{icon} <b>{label}</b>: {s['free']} свободных / {s['activated']} активировано{reserved_str}"
    if not codes_text:
        codes_text = "\n📭 Кодов нет — добавь через кнопку ниже"

    last_txt = ""
    if last_used:
        used_at = last_used.get("used_at")
        if used_at:
            used_str = used_at.strftime("%d.%m %H:%M") if hasattr(used_at, "strftime") else str(used_at)[:16]
            last_txt = f"\n\n⏱ Последняя: <code>{last_used['code']}</code> ({used_str})"

    status_str = "✅ ВКЛЮЧЁН" if rt.perplexity_webapp_enabled else "🔴 ВЫКЛЮЧЕН (тех. работы)"
    text = (
        f"⚡ <b>Perplexity Mini App</b>\n\n"
        f"Статус: <b>{status_str}</b>\n\n"
        f"<b>Коды активации:</b>{codes_text}\n"
        f"📊 Всего активаций: <b>{total_act}</b>"
        f"{last_txt}\n\n"
        f"<i>При выключении клиенты получают сообщение о тех. работах</i>"
    )
    toggle_txt = "🔴 Выключить (тех. работы)" if rt.perplexity_webapp_enabled else "✅ Включить"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_txt,              callback_data="adm_perplexity_toggle")],
        [InlineKeyboardButton(text="➕ Добавить коды",       callback_data="adm_perplexity_add:pro")],
        [InlineKeyboardButton(text="📦 Свободные коды",    callback_data="adm_perplexity_free:pro"),
         InlineKeyboardButton(text="⏳ Ждущие",             callback_data="adm_perplexity_pending")],
        [InlineKeyboardButton(text="📋 История активаций",  callback_data="adm_perplexity_history:0")],
        [InlineKeyboardButton(text="⬅️ Назад в панель",     callback_data="adm_back")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "adm_perplexity_toggle")
async def adm_perplexity_toggle(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return

    rt.perplexity_webapp_enabled = not rt.perplexity_webapp_enabled
    await cb.answer(
        f"Perplexity Mini App {'включён ✅' if rt.perplexity_webapp_enabled else 'выключен 🔴'}",
        show_alert=True
    )
    await adm_perplexity_webapp_menu(cb)

@dp.callback_query(F.data.startswith("adm_perplexity_history:"))
async def adm_perplexity_history(cb: CallbackQuery):
    """История активаций Perplexity с пагинацией."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    offset = int(cb.data.split(":")[1])
    limit = 8
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT cc.code, cc.plan, cc.used_at, cc.used_by, cc.order_id, cc.org_id,
                      u.username, u.full_name
               FROM perplexity_codes cc
               LEFT JOIN users u ON u.user_id = cc.used_by
               WHERE cc.is_used = TRUE AND cc.used_by IS NOT NULL
               ORDER BY cc.used_at DESC
               LIMIT $1 OFFSET $2""",
            limit + 1, offset)
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM perplexity_codes WHERE is_used=TRUE AND used_by IS NOT NULL") or 0

    has_more = len(rows) > limit
    rows = rows[:limit]

    if not rows:
        await cb.answer("Нет активаций", show_alert=True)
        return

    PLAN_LABELS = {"pro": "Pro", "max_5x": "Max 5×", "max_20x": "Max 20×"}
    lines = [f"📋 <b>История активаций Perplexity</b> (всего {total}):\n"]
    for idx, r in enumerate(rows, start=offset + 1):
        used_at = r["used_at"]
        used_str = used_at.strftime("%d.%m.%y %H:%M") if used_at and hasattr(used_at, "strftime") else "—"
        plan_name = PLAN_LABELS.get(r["plan"], r["plan"])
        uid_str = str(r["used_by"]) if r["used_by"] else "—"
        uname = r["username"] or ""
        fname = r["full_name"] or ""
        tg_nick = f"@{uname}" if uname else (fname if fname else f"id{uid_str}")
        org = (r["org_id"] or "—")[:18]
        lines.append(
            f"\n{idx}. {tg_nick}  <i>{used_str}</i>\n"
            f"🆔 <code>{org}</code>\n"
            f"🔑 <code>{r['code']}</code>"
        )

    nav = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_perplexity_history:{offset - limit}"))
    if has_more:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_perplexity_history:{offset + limit}"))
    kb_rows = []
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_perplexity_webapp")])
    try:
        await cb.message.edit_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    except Exception:
        await cb.message.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_perplexity_free:"))
async def adm_perplexity_free_codes(cb: CallbackQuery):
    """Просмотр свободных кодов Perplexity с возможностью удалить."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    parts = cb.data.split(":")
    plan = parts[1]
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    PER = 20
    PLAN_LABELS = {"pro": "Pro", "max_5x": "Max 5×", "max_20x": "Max 20×"}
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval(
            "SELECT COUNT(*) FROM perplexity_codes WHERE plan=$1 AND is_used=FALSE", plan) or 0
        pages = max(1, (total + PER - 1) // PER)
        if page >= pages: page = pages - 1
        if page < 0: page = 0
        rows = await conn.fetch(
            "SELECT id, code, created_at FROM perplexity_codes "
            "WHERE plan=$1 AND is_used=FALSE ORDER BY id ASC LIMIT $2 OFFSET $3",
            plan, PER, page * PER)

    plan_nav = [
        InlineKeyboardButton(text="Pro",    callback_data="adm_perplexity_free:pro"),
    ]
    if not rows:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            plan_nav,
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_perplexity_webapp")],
        ])
        try:
            await cb.message.edit_text(
                f"📦 <b>Свободные коды — Perplexity {PLAN_LABELS.get(plan, plan)}</b>\n\n📭 Кодов нет.",
                parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass
        await cb.answer()
        return

    lines = [f"📦 <b>Свободные коды — Perplexity {PLAN_LABELS.get(plan, plan)}</b> (всего {total}) · стр. {page+1}/{pages}\n"]
    code_btns = []
    for r in rows:
        created = r["created_at"]
        date_str = created.strftime("%d.%m.%y") if created and hasattr(created, "strftime") else "—"
        lines.append(f"• <code>{r['code']}</code>  <i>{date_str}</i>")
        code_btns.append([
            InlineKeyboardButton(
                text=f"🗑 {r['code']}",
                callback_data=f"adm_perplexity_del_code:{r['id']}:{plan}"
            )
        ])

    page_nav = []
    if page > 0:
        page_nav.append(InlineKeyboardButton(text="‹ Пред", callback_data=f"adm_perplexity_free:{plan}:{page-1}"))
    if page < pages - 1:
        page_nav.append(InlineKeyboardButton(text="След ›", callback_data=f"adm_perplexity_free:{plan}:{page+1}"))
    _kbrows = [plan_nav, *code_btns]
    if page_nav: _kbrows.append(page_nav)
    _kbrows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_perplexity_webapp")])
    kb = InlineKeyboardMarkup(inline_keyboard=_kbrows)
    try:
        await cb.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    except Exception:
        await cb.message.answer("\n".join(lines), parse_mode="HTML", reply_markup=kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_perplexity_del_code:"))
async def adm_perplexity_del_code(cb: CallbackQuery):
    """Шаг 1 — подтверждение удаления кода Perplexity."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    parts = cb.data.split(":")
    code_id, plan = int(parts[1]), parts[2]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT code, is_used FROM perplexity_codes WHERE id=$1", code_id)
    if not row:
        await cb.answer("Код не найден", show_alert=True)
        return
    if row["is_used"]:
        await cb.answer("❌ Код уже использован, нельзя удалить", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить",
                              callback_data=f"adm_perplexity_del_confirm:{code_id}:{plan}")],
        [InlineKeyboardButton(text="❌ Отмена",
                              callback_data=f"adm_perplexity_free:{plan}")],
    ])
    try:
        await cb.message.edit_text(
            f"🗑 <b>Удалить код Perplexity?</b>\n\n"
            f"<code>{row['code']}</code>\n\n"
            f"Это действие нельзя отменить.",
            parse_mode="HTML", reply_markup=kb
        )
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_perplexity_del_confirm:"))
async def adm_perplexity_del_confirm(cb: CallbackQuery):
    """Шаг 2 — подтверждение получено, удаляем код Perplexity."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    parts = cb.data.split(":")
    code_id, plan = int(parts[1]), parts[2]
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT code, is_used FROM perplexity_codes WHERE id=$1", code_id)
        if not row:
            await cb.answer("Код не найден", show_alert=True)
            return
        if row["is_used"]:
            await cb.answer("❌ Код уже использован", show_alert=True)
            return
        await conn.execute("DELETE FROM perplexity_codes WHERE id=$1", code_id)
    await cb.answer(f"✅ Код {row['code']} удалён", show_alert=True)
    cb.data = f"adm_perplexity_free:{plan}"
    await adm_perplexity_free_codes(cb)


@dp.callback_query(F.data == "adm_perplexity_pending")
async def adm_perplexity_pending_codes(cb: CallbackQuery):
    """Ждущие (зарезервированные) коды Perplexity — is_used=TRUE, used_by=NULL.
    Источник тот же, что у счётчика «ждут» в меню (раньше брался из другой таблицы —
    из-за этого число и список расходились)."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    import time as _t_pend
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT cc.id, cc.code, cc.plan, cc.created_at,
                      p.user_id AS p_uid, p.plan_name, p.expires_at,
                      u.username, u.full_name
               FROM perplexity_codes cc
               LEFT JOIN perplexity_pending_activations p ON p.code = cc.code
               LEFT JOIN users u ON u.user_id = p.user_id
               WHERE cc.is_used = TRUE AND cc.used_by IS NULL
               ORDER BY cc.created_at ASC NULLS LAST
               LIMIT 20""")

    if not rows:
        await cb.answer("Нет зарезервированных кодов", show_alert=True)
        return

    lines = [f"⏳ <b>Ждущие (зарезервированные) коды Perplexity</b> ({len(rows)}):\n"]
    kb_rows = []
    for r in rows:
        if r["p_uid"]:
            uname = r["username"] or ""
            fname = r["full_name"] or ""
            who = f"@{uname}" if uname else (fname or f"id{r['p_uid']}")
            exp = r["expires_at"]
            if exp and hasattr(exp, "timestamp") and exp.timestamp() > _t_pend.time():
                status = f"{who} — ждёт ввод Org ID (истекает {exp.strftime('%H:%M')})"
            else:
                status = f"{who} — активация истекла, можно вернуть в пул"
        else:
            status = "⚠️ осиротевший резерв (нет активации) — верни в пул"
        lines.append(
            f"\n• <code>{r['code']}</code> | {r['plan'] or 'pro'}\n  {status}"
        )
        kb_rows.append([InlineKeyboardButton(
            text=f"♻️ Вернуть в пул: {r['code'][:14]}",
            callback_data=f"adm_perplexity_release:{r['id']}"
        )])

    kb_rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_perplexity_webapp")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    try:
        await cb.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_perplexity_release:"))
async def adm_perplexity_release_orphan(cb: CallbackQuery):
    """Вернуть зарезервированный код Perplexity обратно в пул (по id)."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        code_id = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        await cb.answer("Ошибка", show_alert=True)
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT code FROM perplexity_codes WHERE id=$1", code_id)
        if not row:
            await cb.answer("Код не найден", show_alert=True)
            return
        await conn.execute(
            "UPDATE perplexity_codes SET is_used=FALSE, used_by=NULL, used_at=NULL, "
            "order_id=NULL, org_id=NULL WHERE id=$1", code_id)
        await conn.execute("DELETE FROM perplexity_pending_activations WHERE code=$1", row["code"])
    await cb.answer("♻️ Код возвращён в пул", show_alert=True)
    await adm_perplexity_pending_codes(cb)


@dp.callback_query(F.data.startswith("adm_perplexity_add:"))
async def adm_perplexity_add_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return
    plan = cb.data.split(":")[1]
    LABELS2 = {"pro": "Pro", "max_5x": "Max 5×", "max_20x": "Max 20×"}
    await state.set_state(PerplexityAdminState.waiting_codes)
    await state.update_data(perplexity_plan=plan)
    await cb.message.answer(
        f"📥 <b>Коды Perplexity {LABELS2.get(plan, plan)}</b>\n\n"
        f"Отправь коды — каждый с новой строки.\n"
        f"Пример: <code>XXXX-XXXX-XXXX-XXXX</code>\n\n/cancel — отмена",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(PerplexityAdminState.waiting_codes, StateFilter("*"))
async def adm_perplexity_receive_codes(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    text = message.text or ""
    if text.startswith("/"):
        await message.answer("⚠️ Это команда, а не коды. Отправь коды (каждый с новой строки) или /cancel.")
        return
    data = await state.get_data()
    plan = data.get("perplexity_plan", "pro")
    _code_re = re.compile(r"^[A-Za-z][A-Za-z0-9][A-Za-z0-9-]*$")
    _raw = [l.strip() for l in text.splitlines() if l.strip()]
    codes = [c for c in _raw if _code_re.match(c)]
    _skipped = len(_raw) - len(codes)
    if not codes:
        await message.answer("⚠️ Не нашёл валидных кодов (код начинается с латинской буквы, без «/»). Отправь ещё раз или /cancel.")
        return
    added = 0
    pool = await get_pool()
    async with pool.acquire() as conn:
        for code in codes:
            try:
                await conn.execute(
                    "INSERT INTO perplexity_codes (code, plan) VALUES ($1,$2) "
                    "ON CONFLICT (code) DO NOTHING",
                    code, plan
                )
                added += 1
            except Exception:
                pass
    await state.clear()
    LABELS3 = {"pro": "Pro", "max_5x": "Max 5×", "max_20x": "Max 20×"}
    _sk = f"\n⏭ Пропущено невалидных строк: {_skipped}" if _skipped else ""
    await message.answer(
        f"✅ Добавлено <b>{added}</b> кодов Perplexity {LABELS3.get(plan, plan)}{_sk}",
        parse_mode="HTML"
    )
    await log_event(message.from_user.id, "perplexity_codes_added", f"plan={plan} n={added}")


@dp.callback_query(F.data == "adm_perplexity_send")
async def adm_perplexity_send_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return
    await state.set_state(PerplexityAdminState.waiting_plan)
    await cb.message.answer(
        "📤 <b>Отправить Perplexity WebApp юзеру</b>\n\n"
        "Формат: <code>USER_ID</code> (тариф всегда Pro)\n"
        "Пример: <code>123456789</code>\n\n/cancel — отмена",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(PerplexityAdminState.waiting_plan, StateFilter("*"))
async def adm_perplexity_do_send(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if message.text == "/cancel":
        await state.clear()
        await message.answer("Отменено.")
        return
    parts = (message.text or "").strip().split()
    if not parts:
        await message.answer("Формат: USER_ID")
        return
    try:
        target = int(parts[0])
    except ValueError:
        await message.answer("Неверный USER_ID")
        return
    plan = "pro"
    plan_name = "Pro"
    code = await get_next_perplexity_code(plan)
    if not code:
        await message.answer(f"🚨 Нет кодов для {plan_name}! Добавь через меню.")
        await state.clear()
        return
    import time as _t2
    order_id = f"perplexity_{target}_{int(_t2.time())}"
    ok = await _send_perplexity_webapp_to_user(target, code, order_id, plan, plan_name)
    if ok:
        await message.answer(
            f"✅ WebApp отправлен <code>{target}</code>\n"
            f"Код: <code>{code}</code>  Тариф: {plan_name}",
            parse_mode="HTML"
        )
    else:
        await message.answer("❌ Не удалось отправить — проверь user_id")
        await release_perplexity_code(code)
        await delete_perplexity_pending_activation(target)
    await state.clear()

# ─── Тест-команда (только для тебя) ──────────────────────────────────────────

@dp.message(F.text.startswith("/test_perplexity_webapp"), StateFilter("*"))
async def test_perplexity_webapp(message: Message):
    """
    Полный тест Perplexity Mini App с ФЕЙКОВЫМ кодом.
    Только для админа. Реальные коды из БД не тратятся.
    """
    if message.from_user.id != ADMIN_ID:
        return

    import random, string as _string, urllib.parse as _up4
    from aiogram.types import WebAppInfo as _WAI4

    # Фейковый код — не из БД, пропустит реальную активацию
    suffix = "".join(random.choices(_string.ascii_uppercase + _string.digits, k=12))
    fake_code  = f"TEST-{suffix}"
    fake_order = f"TEST-ORD-{suffix[:6]}"
    uid = message.from_user.id

    await save_perplexity_pending_activation(uid, fake_code, fake_order, "pro", "Pro")

    webapp_url = (
        f"{WEBAPP_BASE_URL}/webapp/perplexity"
        f"?plan={_up4.quote('Pro')}&code={_up4.quote(fake_code)}"
    )

    await message.answer(
        f"🎉 <b>Оплата прошла!</b>\n\n"
        f"📦 <b>Perplexity Pro</b>\n\n"
        f"Осталось активировать подписку — нажми кнопку ниже 👇\n\n"
        f"<i>⚠️ ТЕСТ — фейковый код, реальной активации нет</i>\n"
        f"<i>🔑 Код: <code>{fake_code}</code></i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="⚡ Активировать Perplexity", style="success",
                web_app=_WAI4(url=webapp_url)
            )],
            [InlineKeyboardButton(
                text="❓ Нужна помощь", style="primary",
                callback_data="perplexity_need_help"
            )],
        ])
    )


