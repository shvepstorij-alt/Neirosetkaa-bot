# -*- coding: utf-8 -*-
"""Link-pay: оплата сервисов по ссылке (HeyGen, Suno, Kling, Higgsfield и т.п.).
Клиент платит в боте → получает инструкцию → присылает ссылку на оплату →
админ оплачивает картой и жмёт «Подписка готова». Приём ссылки — в common.process_linkpay_link.
"""
import logging
import html as _html

from aiogram import F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.filters import StateFilter

from config import ADMIN_ID, PERSONAL_USERNAME, SHOP_CATALOG, bot, dp
from states import AdminState, CredsState, OrderReplyState
from db import (
    get_linkpay_order, set_linkpay_status, list_linkpay_pending,
    get_setting, set_setting, set_linkpay_email, set_linkpay_creds, set_linkpay_admin_msg, log_event,
    add_order_msg, get_order_thread, get_user, get_linkpay_admin_msgs,
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

async def _finalize_order_chain(order_id: str, label: str, exclude_mid=None):
    """Помечает ВСЕ админские сообщения заказа одной кнопкой-статусом (label),
    убирая рабочие кнопки — чтобы один заказ не висел с разными статусами."""
    _marker = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data="lp_noop")]])
    try:
        _ids = await get_linkpay_admin_msgs(order_id)
    except Exception:
        _ids = []
    for _mid in _ids:
        if exclude_mid and _mid == exclude_mid:
            continue
        try:
            await bot.edit_message_reply_markup(chat_id=ADMIN_ID, message_id=_mid, reply_markup=_marker)
        except Exception:
            pass


@dp.callback_query(F.data == "lp_noop")
async def lp_noop(cb: CallbackQuery):
    try:
        await cb.answer()
    except Exception:
        pass


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
        await cb.message.edit_text(
            (cb.message.html_text or "") + "\n\n✅ <b>ВЫПОЛНЕН</b>",
            parse_mode="HTML", disable_web_page_preview=True, reply_markup=None)
    except Exception:
        try:
            await cb.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
    # Помечаем ВСЕ остальные сообщения этого заказа как выполненные
    await _finalize_order_chain(order_id, "✅ ВЫПОЛНЕН", cb.message.message_id)
    await cb.answer("✅ Клиент уведомлён", show_alert=True)


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
    await cb.message.answer("✍️ Отправь <b>текст, фото или файл</b> для клиента — он получит это сообщением:", parse_mode="HTML")
    await cb.answer()


@dp.message(AdminState.waiting_linkpay_clarify)
async def lp_clarify_send(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    uid = data.get("lp_uid")
    await state.clear()
    if not uid:
        await message.answer("⚠️ Сессия истекла.")
        return
    order_id = data.get("lp_order")
    _txt = message.text or message.caption or ""
    try:
        if order_id:
            await add_order_msg(order_id, "admin", _txt or "[вложение]")
        _kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Ответить", callback_data=f"cl_reply:{order_id}")],
        ]) if order_id else None
        if message.text:
            # обычный текст — одним сообщением с заголовком
            await bot.send_message(
                uid,
                f"✍️ <b>Сообщение от Александра по твоему заказу:</b>\n\n{_html.escape(message.text)}\n\n"
                f"Нажми «Ответить», чтобы написать в ответ (например, прислать код).",
                parse_mode="HTML", reply_markup=_kb
            )
        else:
            # фото / документ / видео / любой файл — заголовок + копия вложения
            await bot.send_message(
                uid,
                "✍️ <b>Сообщение от Александра по твоему заказу</b> (см. вложение ниже).\n"
                "Нажми «Ответить», чтобы написать в ответ (например, прислать код).",
                parse_mode="HTML"
            )
            await message.copy_to(chat_id=uid, reply_markup=_kb)
        await message.answer("✅ Отправлено клиенту.")
    except Exception as e:
        await message.answer(f"❌ Не удалось отправить: {e}")


@dp.callback_query(F.data.startswith("cl_reply:"))
async def cl_reply_start(cb: CallbackQuery, state: FSMContext):
    order_id = cb.data.split(":", 1)[1]
    order = await get_linkpay_order(order_id)
    if not order or order.get("user_id") != cb.from_user.id:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    await state.set_state(OrderReplyState.waiting)
    await state.update_data(reply_order=order_id)
    await cb.message.answer("✍️ Напиши сообщение Александру по заказу (текст, код и т.п.):")
    await cb.answer()


@dp.message(OrderReplyState.waiting)
async def cl_reply_send(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("reply_order")
    await state.clear()
    if not order_id:
        return
    order = await get_linkpay_order(order_id)
    if not order:
        await message.answer("⚠️ Заказ не найден.")
        return
    _txt = message.text or message.caption or ""
    await add_order_msg(order_id, "client", _txt or "[вложение]")
    _u = await get_user(message.from_user.id)
    _tag = ("@" + _u["username"]) if (_u and _u.get("username")) else (f"id{message.from_user.id}")
    _kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подписка готова", callback_data=f"lp_done:{order_id}")],
        [InlineKeyboardButton(text="✍️ Уточнение", callback_data=f"lp_clarify:{order_id}"),
         InlineKeyboardButton(text="📜 История", callback_data=f"lp_thread:{order_id}")],
        [InlineKeyboardButton(text="🗑 Отменить заказ", callback_data=f"lp_cancel:{order_id}")],
    ])
    _head = (
        f"📨 <b>Ответ клиента по заказу</b>\n\n"
        f"👤 {_tag} (<code>{message.from_user.id}</code>)\n"
        f"📦 {order.get('service_name','')} · {order.get('plan_name') or '—'}\n"
        f"🆔 <code>{order_id}</code>"
    )
    try:
        if message.text:
            _m_reply = await bot.send_message(
                ADMIN_ID, _head + f"\n\n💬 {_html.escape(message.text)}",
                parse_mode="HTML", reply_markup=_kb)
        else:
            # фото / файл / скрин от клиента — заголовок + копия вложения
            await bot.send_message(ADMIN_ID, _head + "\n\n💬 <i>вложение ниже</i>", parse_mode="HTML")
            _m_reply = await message.copy_to(chat_id=ADMIN_ID, reply_markup=_kb)
        await set_linkpay_admin_msg(order_id, _m_reply.message_id)
    except Exception as e:
        logging.error(f"cl_reply forward: {e}")
    await message.answer("✅ Отправлено Александру. Он ответит здесь же.")


@dp.callback_query(F.data.startswith("lp_thread:"))
async def lp_thread_view(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    order_id = cb.data.split(":", 1)[1]
    order = await get_linkpay_order(order_id)
    msgs = await get_order_thread(order_id)
    from config import _BOT_TZ
    o = order or {}
    _uname = o.get("username") or ""
    _tag = f"@{_uname}" if _uname else (f"id{o.get('user_id')}" if o.get("user_id") else "—")
    _created = ""
    try:
        if o.get("created_at"):
            _created = o["created_at"].astimezone(_BOT_TZ).strftime("%d.%m.%Y %H:%M")
    except Exception:
        _created = ""
    head = (
        f"📜 <b>История заказа</b>\n"
        f"🆔 <code>{order_id}</code>\n"
        f"👤 {_tag}" + (f" (<code>{o.get('user_id')}</code>)" if o.get("user_id") else "") + "\n"
        f"📦 {o.get('service_name','')}" + (f" · {o.get('plan_name')}" if o.get('plan_name') else "") + "\n"
        f"💵 {o.get('amount_rub', 0)}₽ · статус: <b>{o.get('status','')}</b>\n"
    )
    if _created:
        head += f"🗓 Создан: {_created}\n"
    _email = o.get("account_email") or ""
    _passw = o.get("account_pass") or ""
    if _email:
        head += f"📧 Email: <code>{_html.escape(_email)}</code>\n"
    if _passw:
        head += f"🔑 Пароль: <code>{_html.escape(_passw)}</code>\n"
    _link = o.get("payment_link") or ""
    if _link:
        head += f"🔗 Ссылка: {_html.escape(_link)}\n"
    head += "\n"
    if not msgs:
        body = "<i>Переписки пока нет.</i>"
    else:
        lines = []
        for m in msgs:
            who = "🛠 Ты" if m["sender"] == "admin" else "👤 Клиент"
            ts = m["created_at"].astimezone(_BOT_TZ).strftime("%d.%m %H:%M") if m.get("created_at") else ""
            lines.append(f"<b>{who}</b> <i>{ts}</i>\n{_html.escape(m['text'] or '')}")
        body = "\n\n".join(lines)
    _kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Уточнение", callback_data=f"lp_clarify:{order_id}")],
    ])
    try:
        await cb.message.answer(head + body, parse_mode="HTML", reply_markup=_kb)
    except Exception:
        await cb.message.answer(head + "(история слишком длинная)")
    await cb.answer()


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
    # Помечаем ВСЕ остальные сообщения этого заказа как отменённые
    await _finalize_order_chain(order_id, "🗑 ОТМЕНЁН", cb.message.message_id)
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


async def _lp_svc_view(key, note=""):
    sv = SHOP_CATALOG.get(key, {})
    on = (await get_setting(f"linkpay:enabled:{key}", "0") or "0") == "1"
    instr = await get_setting(f"linkpay:instructions:{key}", "") or "(по умолчанию)"
    domains = await get_setting(f"linkpay:domains:{key}", "") or "(любой)"
    head = (note + "\n\n") if note else ""
    text = (
        head +
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
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data.startswith("adm_lp_svc:"))
async def adm_lp_svc(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    key = cb.data.split(":", 1)[1]
    text, kb = await _lp_svc_view(key)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_lp_toggle:"))
async def adm_lp_toggle(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    key = cb.data.split(":", 1)[1]
    cur = (await get_setting(f"linkpay:enabled:{key}", "0") or "0") == "1"
    await set_setting(f"linkpay:enabled:{key}", "0" if cur else "1")
    await cb.answer("Включено ✅" if not cur else "Выключено 🔴")
    text, kb = await _lp_svc_view(key)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("adm_lp_instr:"))
async def adm_lp_instr_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    key = cb.data.split(":", 1)[1]
    await state.set_state(AdminState.waiting_linkpay_instr)
    await state.update_data(lp_key=key, lp_field="instr",
                            lp_menu_chat=cb.message.chat.id, lp_menu_mid=cb.message.message_id)
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
    await state.update_data(lp_key=key, lp_field="dom",
                            lp_menu_chat=cb.message.chat.id, lp_menu_mid=cb.message.message_id)
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
    menu_chat = data.get("lp_menu_chat")
    menu_mid = data.get("lp_menu_mid")
    await state.clear()
    if not key:
        await message.answer("⚠️ Сессия истекла.")
        return
    val = message.text.strip()
    if field == "creds_instr":
        await set_setting(f"creds:instructions:{key}", val)
        note = "✅ Инструкция сохранена"
        _view = await _creds_svc_view(key, note=note)
    elif field == "dom":
        await set_setting(f"linkpay:domains:{key}", "" if val == "-" else val)
        note = "✅ Домены сохранены"
        _view = await _lp_svc_view(key, note=note)
    else:
        await set_setting(f"linkpay:instructions:{key}", val)
        note = "✅ Инструкция сохранена"
        _view = await _lp_svc_view(key, note=note)
    try:
        await message.delete()
    except Exception:
        pass
    text, kb = _view
    if menu_chat and menu_mid:
        try:
            await bot.edit_message_text(text, chat_id=menu_chat, message_id=menu_mid,
                                        reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


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


# ─── Тест-команда (только админ): прогон флоу без оплаты ──────────────────────

@dp.message(F.text.startswith("/test_linkpay"), StateFilter("*"))
async def test_linkpay(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    import random, string as _s
    from common import _send_linkpay_instructions
    parts = (message.text or "").split()
    key = parts[1] if len(parts) > 1 else "suno"
    sv = SHOP_CATALOG.get(key)
    if not sv:
        await message.answer(
            f"❌ Сервис «{key}» не найден.\nПример: <code>/test_linkpay suno</code>",
            parse_mode="HTML")
        return
    plans = sv.get("plans", [])
    plan = plans[0] if plans else {}
    plan_name = plan.get("name", "Pro")
    amount = plan.get("price", 0)
    service_name = f"{sv.get('emoji','')} {sv.get('name', key)} - {plan_name}".strip()
    order_id = "TESTLP-" + "".join(random.choices(_s.ascii_uppercase + _s.digits, k=8))
    await message.answer(
        f"🧪 <b>Тест link-pay: {sv.get('name', key)}</b>\n"
        f"Сейчас придёт сообщение, как видит клиент после оплаты 👇\n"
        f"<i>Заказ тестовый: {order_id}</i>",
        parse_mode="HTML")
    await _send_linkpay_instructions(
        user_id=message.from_user.id, shop_key=key,
        service_name=service_name, plan_name=plan_name,
        order_id=order_id, amount_rub=amount)


# ══════════════════════════════════════════════════════════
#  CREDS — оформление по логину/паролю (Zoom, Krea, YouTube)
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data.startswith("creds_send:"))
async def creds_send_start(cb: CallbackQuery, state: FSMContext):
    order_id = cb.data.split(":", 1)[1]
    order = await get_linkpay_order(order_id)
    if not order or order["user_id"] != cb.from_user.id:
        await cb.answer("Заказ не найден", show_alert=True)
        return
    if order["status"] != "awaiting_creds":
        await cb.answer("Данные уже получены ✅", show_alert=True)
        return
    await state.set_state(CredsState.waiting_email)
    await state.update_data(creds_order=order_id)
    await cb.message.answer("📧 Пришли <b>email</b> от аккаунта одним сообщением:", parse_mode="HTML")
    await cb.answer()


@dp.message(CredsState.waiting_email, F.text)
async def creds_email(message: Message, state: FSMContext):
    await state.update_data(creds_email=message.text.strip())
    await state.set_state(CredsState.waiting_password)
    await message.answer("🔑 Теперь пришли <b>пароль</b> от аккаунта:", parse_mode="HTML")


@dp.message(CredsState.waiting_password, F.text)
async def creds_password(message: Message, state: FSMContext):
    password = message.text.strip()
    data = await state.get_data()
    order_id = data.get("creds_order")
    email = data.get("creds_email", "")
    await state.clear()
    order = await get_linkpay_order(order_id) if order_id else None
    if not order:
        await message.answer("⚠️ Сессия истекла. Открой сообщение с заказом и нажми «Отправить данные аккаунта» снова.")
        return
    await set_linkpay_creds(order_id, email, password)
    await message.answer(
        "✅ <b>Данные получены!</b>\n\nАлександр оформит подписку в ближайшее время. "
        "После оформления рекомендуем сменить пароль 🔒",
        parse_mode="HTML")
    uname = order.get("username") or ""
    tag = f"@{uname}" if uname else f"id{order['user_id']}"
    admin_text = (
        f"🔐 <b>Заказ (вход в аккаунт)</b>\n\n"
        f"👤 {tag} (<code>{order['user_id']}</code>)\n"
        f"📦 {order['service_name']}\n"
        f"🎫 Тариф: <b>{order.get('plan_name') or '—'}</b>\n"
        f"💵 Оплачено: <b>{order['amount_rub']}₽</b>\n"
        f"📧 Email: <code>{_html.escape(email)}</code>\n"
        f"🔑 Пароль: <code>{_html.escape(password)}</code>\n"
        f"🆔 <code>{order_id}</code>"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подписка готова", callback_data=f"lp_done:{order_id}")],
        [InlineKeyboardButton(text="✍️ Уточнение",       callback_data=f"lp_clarify:{order_id}"),
         InlineKeyboardButton(text="📜 История",          callback_data=f"lp_thread:{order_id}")],
        [InlineKeyboardButton(text="🗑 Отменить заказ",   callback_data=f"lp_cancel:{order_id}")],
    ])
    # ВСЕГДА новое сообщение (не редактируем старое «оплачен») — чтобы заказ с логином/паролем
    # всплыл внизу чата и не потерялся. admin_msg_id обновляем на него.
    try:
        m = await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=kb)
        await set_linkpay_admin_msg(order_id, m.message_id)
    except Exception as e:
        logging.error(f"creds admin notify: {e}")
    await log_event(message.from_user.id, "creds_received", f"order={order_id}")


# ─── Админ: настройка creds ───────────────────────────────────────────────────

async def _creds_menu_kb():
    rows = []
    for key, sv in SHOP_CATALOG.items():
        on = (await get_setting(f"creds:enabled:{key}", "0") or "0") == "1"
        mark = "✅" if on else "▫️"
        _eid = _btn_emoji_id(key, sv)
        rows.append([InlineKeyboardButton(
            text=f"{mark} {sv.get('name', key)}",
            callback_data=f"adm_creds_svc:{key}",
            **({"icon_custom_emoji_id": _eid} if _eid else {})
        )])
    rows.append([InlineKeyboardButton(text="📋 Заказы в работе", callback_data="adm_lp_pending")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад в админку", callback_data="adm_back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _creds_svc_view(key, note=""):
    sv = SHOP_CATALOG.get(key, {})
    on = (await get_setting(f"creds:enabled:{key}", "0") or "0") == "1"
    instr = await get_setting(f"creds:instructions:{key}", "") or "(по умолчанию: попросим email и пароль)"
    head = (note + "\n\n") if note else ""
    text = (
        head +
        f"🔐 <b>{sv.get('name', key)}</b>\n\n"
        f"Статус: <b>{'✅ включено' if on else '🔴 выключено'}</b>\n\n"
        f"<b>Инструкция клиенту:</b>\n{instr}"
    )
    rows = [
        [InlineKeyboardButton(text=("🔴 Выключить" if on else "✅ Включить"),
                              callback_data=f"adm_creds_toggle:{key}")],
        [InlineKeyboardButton(text="✏️ Изменить инструкцию", callback_data=f"adm_creds_instr:{key}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_creds")],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm_creds")
async def adm_creds_menu(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    text = (
        "🔐 <b>Вход в аккаунт (логин/пароль)</b>\n\n"
        "✅ — включено: после оплаты клиент присылает email и пароль, "
        "ты оформляешь подписку вручную и жмёшь «Подписка готова».\n\n"
        "Выбери сервис, чтобы вкл/выкл и задать инструкцию."
    )
    try:
        await cb.message.edit_text(text, reply_markup=await _creds_menu_kb(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=await _creds_menu_kb(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_creds_svc:"))
async def adm_creds_svc(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    key = cb.data.split(":", 1)[1]
    text, kb = await _creds_svc_view(key)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_creds_toggle:"))
async def adm_creds_toggle(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    key = cb.data.split(":", 1)[1]
    cur = (await get_setting(f"creds:enabled:{key}", "0") or "0") == "1"
    await set_setting(f"creds:enabled:{key}", "0" if cur else "1")
    await cb.answer("Включено ✅" if not cur else "Выключено 🔴")
    text, kb = await _creds_svc_view(key)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass


@dp.callback_query(F.data.startswith("adm_creds_instr:"))
async def adm_creds_instr_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    key = cb.data.split(":", 1)[1]
    await state.set_state(AdminState.waiting_linkpay_instr)
    await state.update_data(lp_key=key, lp_field="creds_instr",
                            lp_menu_chat=cb.message.chat.id, lp_menu_mid=cb.message.message_id)
    await cb.message.answer(
        "✏️ Пришли текст инструкции для клиента (что прислать, как включить доступ и т.п.).\n"
        "Не используй символ «<».")
    await cb.answer()


# ─── Тест-команда creds ───────────────────────────────────────────────────────

@dp.message(F.text.startswith("/test_creds"), StateFilter("*"))
async def test_creds(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    import random, string as _s
    from common import _send_creds_instructions
    parts = (message.text or "").split()
    key = parts[1] if len(parts) > 1 else "krea"
    sv = SHOP_CATALOG.get(key)
    if not sv:
        await message.answer(f"❌ Сервис «{key}» не найден. Пример: <code>/test_creds krea</code>", parse_mode="HTML")
        return
    plans = sv.get("plans", [])
    plan = plans[0] if plans else {}
    plan_name = plan.get("name", "Pro")
    amount = plan.get("price", 0)
    service_name = f"{sv.get('emoji','')} {sv.get('name', key)} - {plan_name}".strip()
    order_id = "TESTCR-" + "".join(random.choices(_s.ascii_uppercase + _s.digits, k=8))
    await message.answer(
        f"🧪 <b>Тест creds: {sv.get('name', key)}</b>\nСейчас придёт сообщение как у клиента 👇\n"
        f"<i>Заказ тестовый: {order_id}</i>", parse_mode="HTML")
    await _send_creds_instructions(
        user_id=message.from_user.id, shop_key=key,
        service_name=service_name, plan_name=plan_name,
        order_id=order_id, amount_rub=amount)
