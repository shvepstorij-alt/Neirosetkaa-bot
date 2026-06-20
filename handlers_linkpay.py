# -*- coding: utf-8 -*-
"""Link-pay: оплата сервисов по ссылке (HeyGen, Suno, Kling, Higgsfield и т.п.).
Клиент платит в боте → получает инструкцию → присылает ссылку на оплату →
админ оплачивает картой и жмёт «Подписка готова». Приём ссылки — в common.process_linkpay_link.
"""
import logging

from aiogram import F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from config import ADMIN_ID, PERSONAL_USERNAME, SHOP_CATALOG, bot, dp
from states import AdminState
from db import (
    get_linkpay_order, set_linkpay_status, list_linkpay_pending,
    get_setting, set_setting,
)
from keyboards import _eib, _btn_emoji_id


# ══════════════════════════════════════════════════════════
#  КЛИЕНТ
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "linkpay_help")
async def linkpay_help(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(
        "❓ <b>Нужна помощь?</b>\n\nНапиши Александру — поможет получить ссылку 👇",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Написать", url=f"https://t.me/{PERSONAL_USERNAME}")]
        ])
    )


@dp.callback_query(F.data.startswith("linkpay_send:"))
async def linkpay_send_prompt(cb: CallbackQuery):
    order_id = cb.data.split(":", 1)[1]
    order = await get_linkpay_order(order_id)
    if not order or order["user_id"] != cb.from_user.id:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    if order["status"] != "awaiting_link":
        await cb.answer("Ссылка уже принята ✅", show_alert=True)
        return
    await cb.message.answer(
        "📎 Пришли <b>ссылку на оплату</b> одним сообщением (начинается с http...).",
        parse_mode="HTML"
    )
    await cb.answer()


# ══════════════════════════════════════════════════════════
#  АДМИН — действия по заказу
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("lp_done:"))
async def lp_done(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    order_id = cb.data.split(":", 1)[1]
    order = await get_linkpay_order(order_id)
    if not order:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    await set_linkpay_status(order_id, "done")
    try:
        await bot.send_message(
            order["user_id"],
            f"🎉 <b>Подписка оформлена!</b>\n\n📦 {order['service_name']}\n\n"
            f"Спасибо за покупку! 🙌",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_eib("Мой профиль", "menu_profile")],
                [_eib("Главное меню", "back_main")],
            ])
        )
    except Exception as e:
        logging.error(f"lp_done notify: {e}")
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.answer("✅ Клиент уведомлён", show_alert=True)
    await cb.message.answer(f"✅ Заказ <code>{order_id}</code> выполнен.", parse_mode="HTML")


@dp.callback_query(F.data.startswith("lp_clarify:"))
async def lp_clarify_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    order_id = cb.data.split(":", 1)[1]
    order = await get_linkpay_order(order_id)
    if not order:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    await state.set_state(AdminState.waiting_linkpay_clarify)
    await state.update_data(lp_uid=order["user_id"], lp_order=order_id)
    await cb.message.answer("✍️ Напиши текст уточнения для клиента (он получит его сообщением):")
    await cb.answer()


@dp.message(AdminState.waiting_linkpay_clarify, F.text)
async def lp_clarify_send(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    uid = data.get("lp_uid")
    await state.clear()
    if not uid:
        await message.answer("⚠️ Сессия истекла.")
        return
    try:
        await bot.send_message(
            uid,
            f"✍️ <b>Сообщение от Александра по твоему заказу:</b>\n\n{message.text}\n\n"
            f"Ответить можно здесь или написать @{PERSONAL_USERNAME}.",
            parse_mode="HTML"
        )
        await message.answer("✅ Отправлено клиенту.")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}")


@dp.callback_query(F.data.startswith("lp_cancel:"))
async def lp_cancel_ask(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    order_id = cb.data.split(":", 1)[1]
    await cb.message.answer(
        f"🗑 <b>Отменить заказ</b> <code>{order_id}</code>?\n"
        f"Клиент получит уведомление об отмене.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Да, отменить", callback_data=f"lp_cancel_yes:{order_id}")],
            [InlineKeyboardButton(text="↩️ Нет, оставить", callback_data="lp_cancel_no")],
        ])
    )
    await cb.answer()


@dp.callback_query(F.data == "lp_cancel_no")
async def lp_cancel_no(cb: CallbackQuery):
    await cb.answer("Отмена отменена 🙂")
    try:
        await cb.message.delete()
    except Exception:
        pass


@dp.callback_query(F.data.startswith("lp_cancel_yes:"))
async def lp_cancel_yes(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    order_id = cb.data.split(":", 1)[1]
    order = await get_linkpay_order(order_id)
    if not order:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    await set_linkpay_status(order_id, "cancelled")
    try:
        await bot.send_message(
            order["user_id"],
            f"❌ <b>Заказ отменён</b>\n\n📦 {order['service_name']}\n\n"
            f"Если деньги были списаны — напиши Александру по возврату: @{PERSONAL_USERNAME}",
            parse_mode="HTML"
        )
    except Exception:
        pass
    try:
        await cb.message.edit_text(
            f"🗑 Заказ <code>{order_id}</code> отменён, клиент уведомлён.", parse_mode="HTML")
    except Exception:
        pass
    await cb.answer("Отменён", show_alert=True)


# ══════════════════════════════════════════════════════════
#  АДМИН — настройка (вкл/выкл + инструкции по сервисам)
# ══════════════════════════════════════════════════════════

async def _linkpay_menu_kb():
    rows = []
    for key, sv in SHOP_CATALOG.items():
        on = (await get_setting(f"linkpay:enabled:{key}", "0") or "0") == "1"
        mark = "✅" if on else "▫️"
        _eid = _btn_emoji_id(key, sv)
        rows.append([InlineKeyboardButton(
            text=f"{mark} {sv.get('name', key)}",
            callback_data=f"adm_lp_svc:{key}",
            **({"icon_custom_emoji_id": _eid} if _eid else {})
        )])
    rows.append([InlineKeyboardButton(text="📋 Заказы в работе", callback_data="adm_lp_pending")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в админку", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm_linkpay")
async def adm_linkpay_menu(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    text = (
        "🔗 <b>Оплата по ссылке</b>\n\n"
        "✅ — включено: после оплаты клиент получает инструкцию и присылает ссылку на оплату, "
        "ты оплачиваешь картой и жмёшь «Подписка готова».\n\n"
        "Выбери сервис, чтобы вкл/выкл и задать инструкцию."
    )
    try:
        await cb.message.edit_text(text, reply_markup=await _linkpay_menu_kb(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=await _linkpay_menu_kb(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_lp_svc:"))
async def adm_lp_svc(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    key = cb.data.split(":", 1)[1]
    sv = SHOP_CATALOG.get(key, {})
    on = (await get_setting(f"linkpay:enabled:{key}", "0") or "0") == "1"
    instr = await get_setting(f"linkpay:instructions:{key}", "") or "(по умолчанию)"
    domains = await get_setting(f"linkpay:domains:{key}", "") or "(любой)"
    text = (
        f"🔗 <b>{sv.get('name', key)}</b>\n\n"
        f"Статус: <b>{'✅ включено' if on else '🔴 выключено'}</b>\n\n"
        f"<b>Инструкция клиенту:</b>\n{instr}\n\n"
        f"<b>Домены ссылки:</b> {domains}"
    )
    rows = [
        [InlineKeyboardButton(text=("🔴 Выключить" if on else "✅ Включить"),
                              callback_data=f"adm_lp_toggle:{key}")],
        [InlineKeyboardButton(text="✏️ Изменить инструкцию", callback_data=f"adm_lp_instr:{key}")],
        [InlineKeyboardButton(text="🌐 Изменить домены", callback_data=f"adm_lp_dom:{key}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_linkpay")],
    ]
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_lp_toggle:"))
async def adm_lp_toggle(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    key = cb.data.split(":", 1)[1]
    cur = (await get_setting(f"linkpay:enabled:{key}", "0") or "0") == "1"
    await set_setting(f"linkpay:enabled:{key}", "0" if cur else "1")
    await cb.answer("Включено ✅" if not cur else "Выключено 🔴")
    cb.data = f"adm_lp_svc:{key}"
    await adm_lp_svc(cb, state)


@dp.callback_query(F.data.startswith("adm_lp_instr:"))
async def adm_lp_instr_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    key = cb.data.split(":", 1)[1]
    await state.set_state(AdminState.waiting_linkpay_instr)
    await state.update_data(lp_key=key, lp_field="instr")
    await cb.message.answer(
        "✏️ Пришли текст инструкции для клиента (можно несколько строк, по шагам).\n"
        "Не используй символ «<».")
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_lp_dom:"))
async def adm_lp_dom_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    key = cb.data.split(":", 1)[1]
    await state.set_state(AdminState.waiting_linkpay_instr)
    await state.update_data(lp_key=key, lp_field="dom")
    await cb.message.answer(
        "🌐 Пришли разрешённые домены ссылки через запятую (напр. <code>suno.com, suno.ai</code>).\n"
        "Отправь <code>-</code> чтобы разрешить любой домен.", parse_mode="HTML")
    await cb.answer()


@dp.message(AdminState.waiting_linkpay_instr, F.text)
async def adm_lp_field_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    key = data.get("lp_key")
    field = data.get("lp_field")
    await state.clear()
    if not key:
        await message.answer("⚠️ Сессия истекла.")
        return
    val = message.text.strip()
    if field == "dom":
        await set_setting(f"linkpay:domains:{key}", "" if val == "-" else val)
        await message.answer("✅ Домены сохранены.")
    else:
        await set_setting(f"linkpay:instructions:{key}", val)
        await message.answer("✅ Инструкция сохранена.")


@dp.callback_query(F.data == "adm_lp_pending")
async def adm_lp_pending(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await cb.answer()
    orders = await list_linkpay_pending(30)
    lines = ["📋 <b>Заказы в работе</b>\n"]
    st_map = {"awaiting_link": "ждёт ссылку", "awaiting_payment": "ждёт оплаты"}
    for o in orders:
        tag = f"@{o['username']}" if o.get("username") else f"id{o['user_id']}"
        lines.append(f"\n• {o['service_name']} — {tag} — <i>{st_map.get(o['status'], o['status'])}</i>")
        if o.get("payment_link"):
            lines.append(f"  🔗 {o['payment_link']}")
    if len(lines) == 1:
        lines.append("\n<i>Пусто</i>")
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_linkpay")]])
    try:
        await cb.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    except Exception:
        await cb.message.answer("\n".join(lines), reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
