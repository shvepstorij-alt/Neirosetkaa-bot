# -*- coding: utf-8 -*-
"""handlers_desc — подтверждение и ручное редактирование черновиков описаний тарифов.

Флоу: models_refresh генерирует черновики и шлёт превью с кнопками
«Применить всё / Редактировать / Отклонить». Здесь — обработка этих кнопок
и правка отдельных пунктов (заменить/дополнить текст описания).
"""
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


class DescEditState(StatesGroup):
    waiting_text = State()


def _svc_name(key):
    return (SHOP_CATALOG.get(key, {}) or {}).get("name", key)


def _item_label(d):
    pn = d.get("plan_name") or ""
    return f"{_svc_name(d['key'])} — {'сервис' if not pn else pn}"


async def _show_edit_list(target):
    """Рисует список пунктов черновика кнопками (target — message для .answer)."""
    drafts = await get_desc_drafts()
    if not drafts:
        await target.answer("Черновиков нет. Запусти /refresh_desc.")
        return
    rows = []
    for i, d in enumerate(drafts):
        rows.append([InlineKeyboardButton(
            text=f"✏️ {i+1}. {_item_label(d)}"[:62], callback_data=f"descitem:{i}")])
    rows.append([InlineKeyboardButton(text="✅ Применить всё", callback_data="descdraft_apply")])
    rows.append([InlineKeyboardButton(text="🚫 Отклонить", callback_data="descdraft_reject")])
    await target.answer(
        "✏️ <b>Пункты черновика</b>\nВыбери, что посмотреть или поправить:",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


@dp.callback_query(F.data == "descdraft_apply")
async def descdraft_apply(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    n = await apply_desc_drafts()
    try:
        await cb.message.edit_text(
            f"✅ Опубликовано описаний: <b>{n}</b>.\nОбновлено в магазине.", parse_mode="HTML")
    except Exception:
        await cb.message.answer(f"✅ Опубликовано описаний: {n}.")
    await cb.answer("Применено")


@dp.callback_query(F.data == "descdraft_reject")
async def descdraft_reject(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    await clear_desc_drafts()
    try:
        await cb.message.edit_text("🚫 Черновик отклонён. Описания не менялись.")
    except Exception:
        await cb.message.answer("🚫 Черновик отклонён.")
    await cb.answer("Отклонено")


@dp.callback_query(F.data == "descdraft_edit")
async def descdraft_edit(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    await cb.answer()
    await _show_edit_list(cb.message)


@dp.callback_query(F.data.startswith("descitem:"))
async def descdraft_item(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    try:
        idx = int(cb.data.split(":")[1])
    except Exception:
        await cb.answer("Ошибка", show_alert=True); return
    drafts = await get_desc_drafts()
    if idx >= len(drafts):
        await cb.answer("Пункт не найден (черновик изменился)", show_alert=True); return
    d = drafts[idx]
    await state.set_state(DescEditState.waiting_text)
    await state.update_data(edit_key=d["key"], edit_plan=d.get("plan_name") or "")
    _old = (d.get("old_descr") or "—")
    _new = (d.get("new_descr") or "—")
    await cb.message.answer(
        f"✏️ <b>{_item_label(d)}</b>\n\n"
        f"<b>Было:</b>\n{_old[:600]}\n\n"
        f"<b>Стало (черновик):</b>\n{_new[:900]}\n\n"
        f"Пришли новый текст, чтобы ЗАМЕНИТЬ черновик (можешь дополнить/переписать). "
        f"Или /skip — оставить как есть.",
        parse_mode="HTML")
    await cb.answer()


@dp.message(DescEditState.waiting_text, F.text)
async def descdraft_receive(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    await state.clear()
    if (message.text or "").strip() == "/skip":
        await message.answer("Оставил без изменений.")
        await _show_edit_list(message)
        return
    await update_desc_draft(data.get("edit_key"), data.get("edit_plan") or "", message.text.strip())
    await message.answer("✅ Черновик пункта обновлён.")
    await _show_edit_list(message)
