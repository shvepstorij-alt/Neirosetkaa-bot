# -*- coding: utf-8 -*-
"""handlers_desc — просмотр, подтверждение и правка черновиков описаний тарифов.

Всё в ОДНОМ сообщении (навигация через edit_text):
• Сводка постранично (◀️ ▶️).
• Редактирование по сервисам: сервис → его пункты (описание сервиса + тарифы) → пункт.
• Правка текста: присылаешь новый текст, он заменяет черновик пункта.
"""
import html
import logging
from aiogram import F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from config import ADMIN_ID, bot, dp, SHOP_CATALOG
from db import (
    get_desc_drafts, update_desc_draft, clear_desc_drafts, apply_desc_drafts,
)

logger = logging.getLogger(__name__)

PAGE = 4  # пунктов на страницу сводки


class DescEditState(StatesGroup):
    waiting_text = State()


def _svc_name(key):
    return (SHOP_CATALOG.get(key, {}) or {}).get("name", key)


def _item_label(d):
    pn = d.get("plan_name") or ""
    return f"{_svc_name(d['key'])} — {'описание сервиса' if not pn else 'тариф «' + pn + '»'}"


def _trim(s, n):
    s = s or ""
    s = s[:n] + ("…" if len(s) > n else "")
    return html.escape(s)   # текст описаний может содержать < & > — экранируем для parse_mode=HTML


# ─── Рендер экранов: возвращают (text, keyboard) ──────────────────────────────
async def render_page(n: int):
    drafts = await get_desc_drafts()
    if not drafts:
        return ("Черновиков нет. Запусти /refresh_desc.", None)
    total = len(drafts)
    pages = (total + PAGE - 1) // PAGE
    n = max(0, min(n, pages - 1))
    chunk = drafts[n * PAGE:(n + 1) * PAGE]
    lines = [f"📝 <b>Черновик описаний</b> — стр. {n+1}/{pages} · всего изменений: {total}\n"]
    for j, d in enumerate(chunk):
        gi = n * PAGE + j
        lines.append(f"<b>{gi+1}. {_item_label(d)}</b>\n{_trim(d.get('new_descr'), 280)}\n")
    nav = []
    if n > 0:
        nav.append(InlineKeyboardButton(text="◀️ Назад", callback_data=f"dcpage:{n-1}"))
    nav.append(InlineKeyboardButton(text=f"{n+1}/{pages}", callback_data="dcnop"))
    if n < pages - 1:
        nav.append(InlineKeyboardButton(text="Вперёд ▶️", callback_data=f"dcpage:{n+1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="✏️ Редактировать по сервисам", callback_data="dcsvcs")],
        [InlineKeyboardButton(text="✅ Применить всё", callback_data="dcapply")],
        [InlineKeyboardButton(text="🚫 Отклонить", callback_data="dcreject")],
    ])
    return ("\n".join(lines), kb)


async def _render_svcs():
    drafts = await get_desc_drafts()
    if not drafts:
        return ("Черновиков нет.", None)
    order, counts = [], {}
    for d in drafts:
        k = d["key"]
        if k not in counts:
            order.append(k); counts[k] = 0
        counts[k] += 1
    rows = [[InlineKeyboardButton(text=f"{_svc_name(k)} ({counts[k]})", callback_data=f"dcsvc:{k}")]
            for k in order]
    rows.append([InlineKeyboardButton(text="✅ Применить всё", callback_data="dcapply")])
    rows.append([InlineKeyboardButton(text="🚫 Отклонить", callback_data="dcreject")])
    rows.append([InlineKeyboardButton(text="◀️ К сводке", callback_data="dcpage:0")])
    return ("✏️ <b>Редактирование по сервисам</b>\nВыбери сервис:",
            InlineKeyboardMarkup(inline_keyboard=rows))


async def _render_svc(key: str):
    drafts = await get_desc_drafts()
    rows = []
    for gi, d in enumerate(drafts):
        if d["key"] != key:
            continue
        pn = d.get("plan_name") or ""
        label = "📄 Описание сервиса" if not pn else f"🎫 {pn}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"dcitem:{gi}")])
    if not rows:
        return await _render_svcs()
    rows.append([InlineKeyboardButton(text="◀️ К сервисам", callback_data="dcsvcs")])
    return (f"✏️ <b>{_svc_name(key)}</b>\nВыбери, что посмотреть или поправить:",
            InlineKeyboardMarkup(inline_keyboard=rows))


async def _render_item(gi: int):
    drafts = await get_desc_drafts()
    if gi >= len(drafts):
        return ("Пункт не найден (черновик изменился).", None)
    d = drafts[gi]
    txt = (f"✏️ <b>{_item_label(d)}</b>\n\n"
           f"<b>Было:</b>\n{_trim(d.get('old_descr') or '—', 500)}\n\n"
           f"<b>Стало (черновик):</b>\n{_trim(d.get('new_descr') or '—', 1200)}")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Заменить/дополнить текст", callback_data=f"dcedit:{gi}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data=f"dcsvc:{d['key']}")],
    ])
    return (txt, kb)


async def _edit(cb: CallbackQuery, text, kb):
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=kb,
                                   disable_web_page_preview=True)
    except Exception:
        try:
            await cb.message.answer(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            pass


def _is_admin(cb):
    return cb.from_user.id == ADMIN_ID


# ─── Навигация ────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "dcnop")
async def dc_nop(cb: CallbackQuery, state: FSMContext):
    await cb.answer()


@dp.callback_query(F.data.startswith("dcpage:"))
async def dc_page(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        await cb.answer("❌", show_alert=True); return
    await state.clear()
    n = int(cb.data.split(":")[1])
    t, kb = await render_page(n)
    await _edit(cb, t, kb)
    await cb.answer()


@dp.callback_query(F.data == "dcsvcs")
async def dc_svcs(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        await cb.answer("❌", show_alert=True); return
    await state.clear()
    t, kb = await _render_svcs()
    await _edit(cb, t, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("dcsvc:"))
async def dc_svc(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        await cb.answer("❌", show_alert=True); return
    await state.clear()
    key = cb.data.split(":", 1)[1]
    t, kb = await _render_svc(key)
    await _edit(cb, t, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("dcitem:"))
async def dc_item(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        await cb.answer("❌", show_alert=True); return
    await state.clear()
    gi = int(cb.data.split(":")[1])
    t, kb = await _render_item(gi)
    await _edit(cb, t, kb)
    await cb.answer()


@dp.callback_query(F.data.startswith("dcedit:"))
async def dc_edit(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        await cb.answer("❌", show_alert=True); return
    gi = int(cb.data.split(":")[1])
    drafts = await get_desc_drafts()
    if gi >= len(drafts):
        await cb.answer("Пункт не найден", show_alert=True); return
    d = drafts[gi]
    await state.set_state(DescEditState.waiting_text)
    await state.update_data(dc_msg=cb.message.message_id, dc_chat=cb.message.chat.id,
                            dc_gi=gi, dc_key=d["key"], dc_plan=d.get("plan_name") or "")
    _txt = (f"✏️ <b>{_item_label(d)}</b>\n\n"
            f"<b>Текущий черновик:</b>\n{_trim(d.get('new_descr') or '—', 1000)}\n\n"
            f"Пришли новый текст — он ЗАМЕНИТ черновик этого пункта. Или «Отмена».")
    _kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Отмена", callback_data=f"dcitem:{gi}")],
    ])
    await _edit(cb, _txt, _kb)
    await cb.answer()


@dp.callback_query(F.data == "dcapply")
async def dc_apply(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        await cb.answer("❌", show_alert=True); return
    await state.clear()
    n = await apply_desc_drafts()
    try:
        await cb.message.edit_text(
            f"✅ Опубликовано описаний: <b>{n}</b>. Обновлено в магазине.",
            parse_mode="HTML")
    except Exception:
        await cb.message.answer(f"✅ Опубликовано описаний: {n}.")
    await cb.answer("Применено")


@dp.callback_query(F.data == "dcreject")
async def dc_reject(cb: CallbackQuery, state: FSMContext):
    if not _is_admin(cb):
        await cb.answer("❌", show_alert=True); return
    await state.clear()
    await clear_desc_drafts()
    try:
        await cb.message.edit_text("🚫 Черновик отклонён. Описания не менялись.")
    except Exception:
        await cb.message.answer("🚫 Черновик отклонён.")
    await cb.answer("Отклонено")


# ─── Приём нового текста для пункта (правка в том же сообщении) ────────────────
@dp.message(DescEditState.waiting_text, F.text)
async def dc_receive(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    await state.clear()
    _mid = data.get("dc_msg")
    _chat = data.get("dc_chat", message.chat.id)
    _gi = data.get("dc_gi", 0)
    # убираем введённый текст админа (антиспам)
    try:
        await message.bot.delete_message(message.chat.id, message.message_id)
    except Exception:
        pass
    _t = (message.text or "").strip()
    if _t and _t not in ("/skip", "Отмена", "отмена"):
        await update_desc_draft(data.get("dc_key"), data.get("dc_plan") or "", _t)
    # перерисовываем пункт в ТОМ ЖЕ сообщении
    t, kb = await _render_item(_gi)
    try:
        await message.bot.edit_message_text(t, chat_id=_chat, message_id=_mid,
                                             parse_mode="HTML", reply_markup=kb,
                                             disable_web_page_preview=True)
    except Exception:
        await message.answer(t, parse_mode="HTML", reply_markup=kb)
