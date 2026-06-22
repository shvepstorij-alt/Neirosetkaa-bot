# -*- coding: utf-8 -*-
# Auto-split module "handlers_admin" — part of Neirosetkaa-bot (refactored from bot.py).
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
    ADMIN_ID, ADMIN_SECRET, ANIM_MODELS, CREDIT_PACKS, DISABLED_MODELS, EDIT_MODELS,
    FAL_API_KEY, FK_ALLOWED_IPS, FK_API_KEY, FK_IP_CHECK_DISABLED, FK_SECRET1, FK_SECRET2,
    FK_SHOP_ID, FK_WEBHOOK_URL, IMAGE_MODELS, SHOP_CATALOG, VIDEO_MODELS, _BOT_TZ,
    bot, dp,
)
from states import (
    AdmPromoState, AdminEditState, AdminState,
)
from db import (
    add_credits, block_user, create_promo, deactivate_promo, get_credits, get_pool,
    get_setting, get_user, list_promos, log_event, set_setting, unblock_user,
    set_ref_premium, get_ref_premium, list_ref_premium, premium_ref_earned_this_month,
)
from keyboards import (
    _all_models_map, _btn_emoji_id, _section_label, kb_admin_panel, kb_balance_menu, kb_block_actions, kb_stat_menu,
    tg_emoji,
)
from common import (
    _build_stat_text, _show_activity_page, _show_payments_page, _show_users_page, _nsg_usd_rate, fk_check_order_status, show_admin_panel,
)

@dp.message(F.text.startswith("/admin"), StateFilter("*"))
async def cmd_admin(message: Message, state: FSMContext):
    # Молчим на не-админов, как будто команды не существует
    if message.from_user.id != ADMIN_ID:
        return

    # Если задан ADMIN_SECRET - требуем вторичный токен: /admin <secret>
    if ADMIN_SECRET:
        parts = (message.text or "").split(maxsplit=1)
        provided = parts[1].strip() if len(parts) > 1 else ""
        if provided != ADMIN_SECRET:
            # Не раскрываем причину отказа
            return

    await state.clear()
    await show_admin_panel(message)



@dp.message(F.text.startswith("/test_fk"), StateFilter("*"))
async def cmd_test_fk(message: Message):
    """Диагностическая команда для админа - проверяет работу FK API.
    
    Использование:
    /test_fk           - проверить конфигурацию + последний pending заказ
    /test_fk ORDER_ID  - проверить конкретный orderId через FK API
    """
    if message.from_user.id != ADMIN_ID:
        return

    parts = (message.text or "").split(maxsplit=1)
    target_order_id = parts[1].strip() if len(parts) > 1 else None

    # 1. Проверка конфигурации
    config_lines = ["🔧 <b>FK Configuration</b>\n"]
    config_lines.append(f"FK_SHOP_ID: <code>{FK_SHOP_ID or '❌ НЕ ЗАДАН'}</code>")
    config_lines.append(f"FK_API_KEY: {'✅ задан' if FK_API_KEY else '❌ НЕ ЗАДАН'}")
    config_lines.append(f"FK_SECRET1: {'✅ задан' if FK_SECRET1 else '❌ НЕ ЗАДАН'}")
    config_lines.append(f"FK_SECRET2: {'✅ задан' if FK_SECRET2 else '❌ НЕ ЗАДАН'}")
    config_lines.append(f"FK_WEBHOOK_URL: <code>{FK_WEBHOOK_URL or '❌ не задан'}</code>")
    config_lines.append(f"FK_IP_CHECK: <code>{'disabled' if FK_IP_CHECK_DISABLED else 'enabled'}</code>")
    config_lines.append(f"Allowed IPs: <code>{', '.join(sorted(FK_ALLOWED_IPS))}</code>")

    await message.answer("\n".join(config_lines), parse_mode="HTML")

    # 2. Если orderId не указан - берём последний pending из БД
    if not target_order_id:
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT order_id, user_id, credits, amount_rub, status, created_at "
                    "FROM fk_orders ORDER BY created_at DESC LIMIT 1"
                )
            if row:
                target_order_id = row["order_id"]
                await message.answer(
                    f"📦 <b>Последний заказ в БД:</b>\n"
                    f"🆔 <code>{row['order_id']}</code>\n"
                    f"👤 user: <code>{row['user_id']}</code>\n"
                    f"💵 amount: {row['amount_rub']}₽ ({row['credits']} кр)\n"
                    f"📊 status: <b>{row['status']}</b>\n"
                    f"⏰ created: {row['created_at']}\n\n"
                    f"<i>Проверяю его статус через FK API...</i>",
                    parse_mode="HTML"
                )
            else:
                await message.answer("❌ В БД нет заказов")
                return
        except Exception as e:
            await message.answer(f"❌ Ошибка чтения БД: <code>{e}</code>", parse_mode="HTML")
            return

    # 3. Проверяем через FK API
    try:
        result = await fk_check_order_status(target_order_id)
        if result is None:
            await message.answer(
                f"❌ <b>FK API ничего не вернул</b>\n\n"
                f"OrderId: <code>{target_order_id}</code>\n\n"
                f"Возможные причины:\n"
                f"• Заказ не существует в FK (не оплачен / не дошёл туда)\n"
                f"• FK_API_KEY неправильный\n"
                f"• Endpoint недоступен\n\n"
                f"Смотри Railway Logs для деталей.",
                parse_mode="HTML"
            )
        else:
            status = result.get("status")
            status_emoji = {"paid": "✅", "new": "⏳", "failed": "❌", "cancelled": "🚫"}.get(status, "❓")
            await message.answer(
                f"{status_emoji} <b>FK API ответил:</b>\n\n"
                f"OrderId: <code>{target_order_id}</code>\n"
                f"Статус FK: <b>{status}</b>\n"
                f"Сумма: {result.get('amount', '?')}\n"
                f"FK Internal ID: <code>{result.get('fk_order_id', '?')}</code>\n"
                f"Merchant ID FK: <code>{result.get('merchant_order_id', '?')}</code>",
                parse_mode="HTML"
            )
    except Exception as e:
        await message.answer(f"❌ Exception: <code>{type(e).__name__}: {e}</code>", parse_mode="HTML")


@dp.message(F.text.startswith("/audit_all"), StateFilter("*"))
async def cmd_audit_all(message: Message):
    """Массовый аудит: находит юзеров у которых баланс больше чем должен быть по истории.

    Формула ожидаемого баланса:
      initial = сумма всех начислений из credit_batches (purchase/free/referral/promo/admin)
      spent   = сумма всех списаний из generations
      expected = initial - spent
      diff    = current_balance - expected

    Если diff > 50 кредитов - подозрительно.
    """
    if message.from_user.id != ADMIN_ID:
        return

    await message.answer("🔍 Провожу аудит всех юзеров... Это займёт несколько секунд.")

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Берём всех юзеров у кого баланс > 50 (остальные вряд ли пострадали)
        users = await conn.fetch("SELECT user_id, credits FROM users WHERE credits > 50 AND user_id != $1 ORDER BY credits DESC", ADMIN_ID)

        results = []
        for user in users:
            uid = user["user_id"]
            current_balance = user["credits"]

            # Начислено через credit_batches (все легальные источники)
            initial_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(credits_left), 0) AS total FROM credit_batches WHERE user_id = $1 AND (expires_at IS NULL OR expires_at > NOW())",
                uid
            )
            initial = int(initial_row["total"])

            # Потрачено на генерации
            spent_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(credits), 0) AS total FROM generations WHERE user_id = $1",
                uid
            )
            spent = int(spent_row["total"])

            expected = initial - spent
            diff = current_balance - expected

            # Считаем только сильные расхождения (>50 кр - точно баг)
            if diff > 50:
                results.append({
                    "uid": uid,
                    "current": current_balance,
                    "expected": expected,
                    "diff": diff,
                    "initial": initial,
                    "spent": spent,
                })

    if not results:
        await message.answer("✅ Подозрительных балансов не найдено. Все юзеры в норме.")
        return

    # Сортируем по размеру переплаты (больше всего наверху)
    results.sort(key=lambda r: r["diff"], reverse=True)

    # Общая статистика
    total_excess = sum(r["diff"] for r in results)
    total_users = len(results)

    # Формируем отчёт
    text_lines = [
        f"🔴 <b>Найдено {total_users} юзеров с лишними кредитами</b>",
        f"💰 Общая переплата: <b>{total_excess} кр</b>",
        "",
        "<b>Топ подозрительных:</b>",
        ""
    ]

    # Показываем до 25 юзеров
    for i, r in enumerate(results[:25], 1):
        text_lines.append(
            f"<b>{i}.</b> <code>{r['uid']}</code>\n"
            f"   💳 Сейчас: <b>{r['current']}</b> кр\n"
            f"   ✅ Должно: {r['expected']} кр\n"
            f"   ⚠️ Лишних: <b>+{r['diff']}</b>"
        )

    if len(results) > 25:
        text_lines.append(f"\n<i>...и ещё {len(results) - 25} юзеров</i>")

    text_lines.append(
        f"\n\n<b>Что делать:</b>\n"
        f"• <code>/audit &lt;user_id&gt;</code> - посмотреть детали юзера\n"
        f"• <code>/setcredits &lt;user_id&gt; &lt;amount&gt;</code> - исправить баланс\n"
        f"• <code>/fix_all_balances</code> - автоматически исправить все (ОСТОРОЖНО!)"
    )

    full_text = "\n\n".join(text_lines)
    # Telegram лимит 4096 - режем если надо
    if len(full_text) > 4000:
        full_text = full_text[:3990] + "\n...[обрезано]"

    await message.answer(full_text, parse_mode="HTML")


@dp.message(F.text.startswith("/fix_all_balances"), StateFilter("*"))
async def cmd_fix_all_balances(message: Message):
    """Массово исправляет балансы всех юзеров у которых баланс больше чем должен быть.
    Устанавливает реальный ожидаемый баланс (initial - spent).

    ВНИМАНИЕ: необратимая операция! Перед запуском лучше посмотреть /audit_all.
    """
    if message.from_user.id != ADMIN_ID:
        return

    # Требуем подтверждение: /fix_all_balances CONFIRM
    parts = (message.text or "").split()
    if len(parts) < 2 or parts[1].strip().upper() != "CONFIRM":
        await message.answer(
            "⚠️ <b>Массовое исправление балансов</b>\n\n"
            "Эта команда установит всем юзерам с завышенным балансом правильное значение "
            "(= сумма начислений − сумма потраченного).\n\n"
            "Чтобы подтвердить выполнение, напиши:\n"
            "<code>/fix_all_balances CONFIRM</code>",
            parse_mode="HTML"
        )
        return

    await message.answer("🔧 Исправляю балансы... Это может занять минуту.")

    pool = await get_pool()
    fixed_count = 0
    total_removed = 0

    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id, credits FROM users WHERE credits > 50 AND user_id != $1", ADMIN_ID)

        for user in users:
            uid = user["user_id"]
            current = user["credits"]

            init_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(credits_left), 0) AS total FROM credit_batches WHERE user_id = $1 AND (expires_at IS NULL OR expires_at > NOW())",
                uid
            )
            spent_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(credits), 0) AS total FROM generations WHERE user_id = $1",
                uid
            )
            expected = int(init_row["total"]) - int(spent_row["total"])
            expected = max(0, expected)  # Не ставим отрицательный баланс
            diff = current - expected

            if diff > 50:
                await conn.execute("UPDATE users SET credits = $1 WHERE user_id = $2", expected, uid)
                await log_event(uid, "admin_auto_fix", f"from={current} to={expected} removed={diff}")
                fixed_count += 1
                total_removed += diff

    await message.answer(
        f"✅ <b>Исправлено балансов: {fixed_count}</b>\n"
        f"💰 Удалено лишних кредитов: <b>{total_removed} кр</b>\n\n"
        f"Все затронутые юзеры получили правильный баланс (начислено − потрачено).",
        parse_mode="HTML"
    )


@dp.message(F.text.startswith("/audit"), StateFilter("*"))
async def cmd_audit_user(message: Message):
    """Админская команда: показывает историю кредитов юзера.
    Использование: /audit <user_id>"""
    if message.from_user.id != ADMIN_ID:
        return

    parts = (message.text or "").split()
    if len(parts) < 2 or not parts[1].strip().lstrip("-").isdigit():
        await message.answer(
            "🔍 <b>Аудит кредитов юзера</b>\n\n"
            "Использование: <code>/audit &lt;user_id&gt;</code>",
            parse_mode="HTML"
        )
        return

    target_uid = int(parts[1])

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Текущий баланс
        user_row = await conn.fetchrow("SELECT credits, created_at FROM users WHERE user_id=$1", target_uid)
        if not user_row:
            await message.answer(f"❌ Юзер {target_uid} не найден")
            return

        # История событий
        events = await conn.fetch(
            "SELECT kind, details, created_at FROM events "
            "WHERE user_id=$1 ORDER BY created_at DESC LIMIT 50",
            target_uid
        )
        # История генераций
        gens = await conn.fetch(
            "SELECT type, model, credits, created_at FROM generations "
            "WHERE user_id=$1 ORDER BY created_at DESC LIMIT 30",
            target_uid
        )

    # Считаем сумму refund_or_add из events
    total_refunds = 0
    refund_count = 0
    for ev in events:
        if ev["kind"] == "refund_or_add":
            details = ev["details"] or ""
            # Парсим "amount=109"
            try:
                amt = int(details.split("amount=")[1].split()[0])
                total_refunds += amt
                refund_count += 1
            except Exception:
                pass

    total_spent = sum(g["credits"] for g in gens)

    text = (
        f"🔍 <b>Аудит юзера {target_uid}</b>\n\n"
        f"💰 Текущий баланс: <b>{user_row['credits']} кр</b>\n"
        f"📅 Зарегистрирован: {user_row['created_at'].astimezone(_BOT_TZ).strftime('%d.%m.%Y %H:%M')}\n\n"
        f"📊 <b>Сводка:</b>\n"
        f"• Потрачено на генерации: {total_spent} кр ({len(gens)} шт за последние 30)\n"
        f"• Возвратов/начислений: {total_refunds} кр ({refund_count} событий)\n\n"
    )

    if refund_count > 3:
        text += f"⚠️ <b>МНОГО ВОЗВРАТОВ</b> - возможна подозрительная активность!\n\n"

    text += "<b>Последние события:</b>\n"
    for ev in events[:15]:
        ts = ev["created_at"].astimezone(_BOT_TZ).strftime("%d.%m %H:%M")
        text += f"<code>{ts}</code> {ev['kind']}: {(ev['details'] or '')[:50]}\n"

    text += "\n<b>Последние генерации:</b>\n"
    for g in gens[:10]:
        ts = g["created_at"].astimezone(_BOT_TZ).strftime("%d.%m %H:%M")
        text += f"<code>{ts}</code> {g['type']}/{g['model']}: -{g['credits']} кр\n"

    # Telegram max message = 4096 chars, режем если надо
    if len(text) > 4000:
        text = text[:3990] + "\n...[обрезано]"

    await message.answer(text, parse_mode="HTML")


@dp.message(F.text.startswith("/setcredits"), StateFilter("*"))
async def cmd_set_credits(message: Message):
    """Админская команда: установить баланс кредитов юзера на конкретное значение.
    Использование: /setcredits <user_id> <amount>"""
    if message.from_user.id != ADMIN_ID:
        return

    parts = (message.text or "").split()
    if len(parts) < 3:
        await message.answer(
            "💰 <b>Установка баланса юзера</b>\n\n"
            "Использование: <code>/setcredits &lt;user_id&gt; &lt;amount&gt;</code>\n"
            "Пример: <code>/setcredits 675546503 150</code> - поставить 150 кредитов",
            parse_mode="HTML"
        )
        return

    try:
        target_uid = int(parts[1])
        new_amount = int(parts[2])
    except ValueError:
        await message.answer("❌ Неверный формат. Должны быть числа.")
        return

    if new_amount < 0:
        await message.answer("❌ Баланс не может быть отрицательным")
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Получаем текущий баланс
        old_row = await conn.fetchrow("SELECT credits FROM users WHERE user_id=$1", target_uid)
        if not old_row:
            await message.answer(f"❌ Юзер {target_uid} не найден")
            return
        old_credits = old_row["credits"]
        await conn.execute("UPDATE users SET credits = $1 WHERE user_id = $2", new_amount, target_uid)

    await log_event(target_uid, "admin_set_credits", f"from={old_credits} to={new_amount} by_admin={message.from_user.id}")
    await message.answer(
        f"✅ Баланс юзера <code>{target_uid}</code> изменён:\n"
        f"Было: <b>{old_credits} кр</b>\n"
        f"Стало: <b>{new_amount} кр</b>",
        parse_mode="HTML"
    )


@dp.message(F.text.startswith("/recover"), StateFilter("*"))
async def cmd_recover(message: Message):
    """Админская команда для ручного восстановления потерянного видео из fal.ai.
    Использование: /recover <request_id> [<target_user_id>]
    Если target_user_id не указан - видео отправится админу."""
    if message.from_user.id != ADMIN_ID:
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "📹 <b>Восстановление видео с fal.ai</b>\n\n"
            "Использование:\n"
            "<code>/recover &lt;request_id&gt;</code> - отправит видео тебе\n"
            "<code>/recover &lt;request_id&gt; &lt;user_id&gt;</code> - отправит указанному юзеру\n\n"
            "<b>Где взять request_id:</b>\n"
            "1. https://fal.ai/dashboard → Latest generations\n"
            "2. Кликни на нужное видео\n"
            "3. В URL будет request_id (или скопируй из детального вида)",
            parse_mode="HTML"
        )
        return

    request_id = parts[1].strip()
    target_uid = int(parts[2]) if len(parts) >= 3 and parts[2].strip().isdigit() else message.from_user.id

    if not FAL_API_KEY:
        await message.answer("⚠️ FAL_API_KEY не задан.")
        return

    status_msg = await message.answer(f"🔍 Ищу видео <code>{request_id}</code>...", parse_mode="HTML")

    # Пробуем найти видео на разных известных endpoint'ах Kling
    endpoints = [
        "fal-ai/kling-video/v3/pro/text-to-video",
        "fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
        "fal-ai/kling-video/v3/standard/text-to-video",
    ]
    headers = {"Authorization": f"Key {FAL_API_KEY}"}
    vid_url = None
    found_endpoint = None

    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        for ep in endpoints:
            # Правильный URL результата (без /response на конце, по актуальной доке fal.ai)
            result_url = f"https://queue.fal.run/{ep}/requests/{request_id}"
            try:
                async with s.get(result_url, headers=headers) as r:
                    if r.status == 200 and r.content_type and "json" in r.content_type:
                        rd = await r.json()
                        video = rd.get("video")
                        if isinstance(video, dict):
                            vid_url = video.get("url")
                        elif isinstance(video, str):
                            vid_url = video
                        if not vid_url:
                            vid_url = rd.get("video_url")
                        if vid_url:
                            found_endpoint = ep
                            break
            except Exception as e:
                logging.debug(f"recover: endpoint {ep} check failed: {e}")

    if not vid_url:
        await status_msg.edit_text(
            f"❌ Не нашёл видео с request_id <code>{request_id}</code>\n\n"
            f"Проверь ID в fal.ai dashboard. Возможно оно уже удалено (fal.ai хранит результаты ограниченное время).",
            parse_mode="HTML"
        )
        return

    await status_msg.edit_text(f"📥 Нашёл на <code>{found_endpoint}</code>, скачиваю...", parse_mode="HTML")

    # Скачиваем с retry
    vid_bytes = None
    for attempt in range(1, 4):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as dl:
                async with dl.get(vid_url) as vr:
                    if vr.status == 200:
                        vid_bytes = await vr.read()
                        if len(vid_bytes) > 10000:
                            break
        except Exception as e:
            logging.warning(f"recover download attempt {attempt}/3 failed: {e}")
            await asyncio.sleep(2 * attempt)

    if not vid_bytes or len(vid_bytes) < 10000:
        await status_msg.edit_text(f"❌ Не удалось скачать видео по URL. Попробуй позже.")
        return

    size_mb = len(vid_bytes) / 1024 / 1024

    # Отправляем юзеру
    try:
        await bot.send_video(
            chat_id=target_uid,
            video=BufferedInputFile(vid_bytes, "recovered.mp4"),
            caption=(
                f"🎬 Восстановленное видео\n"
                f"(генерация, которая зависла - администратор вручную её забрал)"
            ),
            supports_streaming=True,
        )
        if size_mb < 48:
            await bot.send_document(
                chat_id=target_uid,
                document=BufferedInputFile(vid_bytes, f"recovered_{request_id[:8]}.mp4"),
                caption="📁 Оригинал без сжатия",
                disable_content_type_detection=True,
            )
        await status_msg.edit_text(
            f"✅ Видео отправлено юзеру <code>{target_uid}</code>\n"
            f"📦 Размер: {size_mb:.1f} MB\n"
            f"🔧 Endpoint: <code>{found_endpoint}</code>",
            parse_mode="HTML"
        )
    except Exception as send_err:
        logging.error(f"recover send_video failed: {send_err}")
        await status_msg.edit_text(f"⚠️ Видео скачал ({size_mb:.1f} MB), но отправить не смог: {str(send_err)[:200]}")


@dp.message(F.text == "🛠️ Админ панель", StateFilter("*"))
async def reply_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    await state.clear()
    await show_admin_panel(message)


# ─── Меню выбора периода статистики ──────────────────────

# ─── Папка "Аналитика" (Топ моделей / Топ юзеров / Активность / Пользователи) ──

@dp.callback_query(F.data == "adm_analytics_menu")
async def adm_analytics_menu_handler(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔥 Топ моделей",   callback_data="adm_popular"),
         InlineKeyboardButton(text="👑 Топ юзеров",    callback_data="adm_top_users")],
        [InlineKeyboardButton(text="📈 Активность",    callback_data="adm_activity"),
         InlineKeyboardButton(text="👤 Пользователи",  callback_data="adm_users")],
        [InlineKeyboardButton(text="◀️ Панель",        callback_data="adm_back")],
    ])
    try:
        await cb.message.edit_text(
            "📁 <b>Аналитика</b>\n\nВыбери раздел:",
            reply_markup=kb, parse_mode="HTML"
        )
    except Exception:
        await cb.message.answer(
            "📁 <b>Аналитика</b>\n\nВыбери раздел:",
            reply_markup=kb, parse_mode="HTML"
        )
    await cb.answer()


@dp.callback_query(F.data == "adm_stat_menu")
async def adm_stat_menu_handler(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    try:
        await cb.message.edit_text(
            "📊 <b>Статистика</b>\n\nВыбери период:",
            reply_markup=kb_stat_menu(), parse_mode="HTML"
        )
    except Exception:
        await cb.message.answer(
            "📊 <b>Статистика</b>\n\nВыбери период:",
            reply_markup=kb_stat_menu(), parse_mode="HTML"
        )
    await cb.answer()


@dp.callback_query(F.data == "adm_stat_day")
async def adm_stat_day(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True); return
    pool = await get_pool()
    async with pool.acquire() as conn:
        text = await _build_stat_text(conn, "CURRENT_DATE", "сегодня")
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_stat_menu")]])
    await cb.message.answer(text, reply_markup=back_kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "adm_stat_week")
async def adm_stat_week(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True); return
    pool = await get_pool()
    async with pool.acquire() as conn:
        text = await _build_stat_text(conn, "NOW() - INTERVAL '7 days'", "7 дней")
        # Дополняем разбивкой по дням
        by_day = await conn.fetch(
            "SELECT DATE(created_at), COUNT(*) FROM generations "
            "WHERE created_at >= NOW() - INTERVAL '7 days' GROUP BY DATE(created_at) ORDER BY 1"
        )
    by_day_text = "\n".join([f"  {r[0]}: {r[1]} ген." for r in by_day]) or "  нет данных"
    text += f"\n\n<b>По дням:</b>\n{by_day_text}"
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_stat_menu")]])
    await cb.message.answer(text, reply_markup=back_kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "adm_stat_month")
async def adm_stat_month(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    pool = await get_pool()
    async with pool.acquire() as conn:
        text = await _build_stat_text(conn, "NOW() - INTERVAL '30 days'", "30 дней")
        by_week = await conn.fetch(
            "SELECT DATE_TRUNC('week', created_at)::date, COUNT(*) FROM generations "
            "WHERE created_at >= NOW() - INTERVAL '30 days' GROUP BY 1 ORDER BY 1"
        )
    week_text = "\n".join([f"  Неделя с {r[0]}: {r[1]} ген." for r in by_week]) or "  нет данных"
    text += f"\n\n<b>По неделям:</b>\n{week_text}"
    back_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_stat_menu")]])
    await cb.message.answer(text, reply_markup=back_kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "adm_stat_pick")
async def adm_stat_pick(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    await state.set_state(AdminState.waiting_stat_date)
    await cb.message.answer(
        "📅 <b>Введи дату</b> в формате <code>ДД.ММ.ГГГГ</code>\n\nНапример: <code>28.05.2026</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="❌ Отмена", callback_data="adm_stat_menu")
        ]])
    )
    await cb.answer()


@dp.message(StateFilter(AdminState.waiting_stat_date))
async def adm_stat_date_input(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    import datetime as _dt
    raw = (message.text or "").strip()
    try:
        dt = _dt.datetime.strptime(raw, "%d.%m.%Y").date()
    except ValueError:
        await message.answer(
            "❌ Неверный формат. Введи дату как <code>ДД.ММ.ГГГГ</code>, например <code>28.05.2026</code>",
            parse_mode="HTML"
        )
        return
    await state.clear()
    since_sql = f"'{dt.isoformat()}'::date"
    until_sql = f"'{dt.isoformat()}'::date + INTERVAL '1 day'"
    pool = await get_pool()
    async with pool.acquire() as conn:
        new_users = await conn.fetchval(
            f"SELECT COUNT(*) FROM users WHERE created_at >= {since_sql} AND created_at < {until_sql}"
        ) or 0
        row = await conn.fetchrow(
            f"SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations "
            f"WHERE created_at >= {since_sql} AND created_at < {until_sql}"
        )
        gens, credits_used = row[0] or 0, row[1] or 0
        row2 = await conn.fetchrow(
            f"SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM payments "
            f"WHERE created_at >= {since_sql} AND created_at < {until_sql}"
        )
        pays, revenue = row2[0] or 0, row2[1] or 0
        by_type = await conn.fetch(
            f"SELECT type, COUNT(*) FROM generations "
            f"WHERE created_at >= {since_sql} AND created_at < {until_sql} GROUP BY type"
        )
        cr_row = await conn.fetchrow(
            f"SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM fk_orders "
            f"WHERE status='paid' AND (pack NOT LIKE 'shop:%' OR pack IS NULL) "
            f"AND paid_at >= {since_sql} AND paid_at < {until_sql}"
        )
        sh_row = await conn.fetchrow(
            f"SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM fk_orders "
            f"WHERE status='paid' AND pack LIKE 'shop:%' "
            f"AND paid_at >= {since_sql} AND paid_at < {until_sql}"
        )
        shop_detail = await conn.fetch(
            f"SELECT pack, COUNT(*) as cnt, COALESCE(SUM(amount_rub),0) as revenue "
            f"FROM fk_orders WHERE status='paid' AND pack LIKE 'shop:%' "
            f"AND paid_at >= {since_sql} AND paid_at < {until_sql} "
            f"GROUP BY pack ORDER BY cnt DESC"
        )
    by_type_text = "\n".join([f"  • {r[0]}: {r[1]} шт" for r in by_type]) or "  нет данных"
    cr_n, cr_sum = cr_row[0] or 0, cr_row[1] or 0
    sh_n, sh_sum = sh_row[0] or 0, sh_row[1] or 0

    shop_by_svc: dict = {}
    for r in shop_detail:
        pack = r["pack"] or ""
        parts = pack.split(":")
        svc_key = parts[1] if len(parts) > 1 else pack
        svc = SHOP_CATALOG.get(svc_key, {})
        svc_name = f"{svc.get('emoji','')}{svc.get('name', svc_key)}".strip()
        if svc_name not in shop_by_svc:
            shop_by_svc[svc_name] = {"cnt": 0, "rev": 0}
        shop_by_svc[svc_name]["cnt"] += r["cnt"]
        shop_by_svc[svc_name]["rev"] += r["revenue"]

    shop_detail_text = ""
    if shop_by_svc:
        shop_detail_text = "\n" + "\n".join(
            f"    • {name}: <b>{d['cnt']} шт · {d['rev']}₽</b>"
            for name, d in shop_by_svc.items()
        )

    label = dt.strftime("%d.%m.%Y")
    text = (
        f"📊 <b>Статистика за {label}</b>\n\n"
        f"🆕 Новых пользователей: <b>{new_users}</b>\n"
        f"🎨 Генераций: <b>{gens}</b>\n"
        f"💸 Кредитов потрачено: <b>{credits_used}</b>\n"
        f"💳 Оплат: <b>{pays}</b>\n"
        f"💰 Выручка: <b>{revenue}₽</b>\n"
        f"  ├ 💳 Кредиты: <b>{cr_n} шт · {cr_sum}₽</b>\n"
        f"  └ 🛍 Магазин: <b>{sh_n} шт · {sh_sum}₽</b>"
        + shop_detail_text + "\n\n"
        f"<b>По типу генераций:</b>\n{by_type_text}"
    )
    back_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔎 Другой день", callback_data="adm_stat_pick")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_stat_menu")],
    ])
    await message.answer(text, reply_markup=back_kb, parse_mode="HTML")


# ══════════════════════════════════════════════════════════
#  УПРАВЛЕНИЕ БАЛАНСАМИ КЛИЕНТОВ (админ)
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "adm_balance_menu")
async def adm_balance_menu(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await cb.message.edit_text(
        "💳 <b>Управление балансами</b>\n\n"
        "🔍 <b>Аудит всех</b> - найдёт юзеров с завышенным балансом\n"
        "👤 <b>Аудит юзера</b> - детали по конкретному ID\n"
        "✏️ <b>Установить баланс</b> - точное значение\n"
        "➖ <b>Снять кредиты</b> - отнять у клиента\n"
        "🔧 <b>Исправить все</b> - массовый фикс по формуле (начислено − потрачено)",
        reply_markup=kb_balance_menu(),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data == "adm_back")
async def adm_back_to_panel(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer()
        return
    try:
        await cb.message.edit_text(
            "🛠 <b>Админ-панель</b>\n\nВыбери действие:",
            reply_markup=kb_admin_panel(),
            parse_mode="HTML"
        )
    except Exception:
        await cb.message.answer(
            "🛠 <b>Админ-панель</b>\n\nВыбери действие:",
            reply_markup=kb_admin_panel(),
            parse_mode="HTML"
        )
    await cb.answer()


# ── Аудит всех юзеров ─────────────────────────────────────
@dp.callback_query(F.data == "adm_bal_audit_all")
async def adm_bal_audit_all(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await cb.answer("🔍 Провожу аудит...")
    await cb.message.edit_text("🔍 Провожу аудит всех юзеров... Это займёт несколько секунд.")

    pool = await get_pool()
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id, credits FROM users WHERE credits > 50 AND user_id != $1 ORDER BY credits DESC", ADMIN_ID)
        results = []
        for user in users:
            uid = user["user_id"]
            current = user["credits"]
            init_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(credits_left), 0) AS total FROM credit_batches WHERE user_id = $1 AND (expires_at IS NULL OR expires_at > NOW())", uid
            )
            spent_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(credits), 0) AS total FROM generations WHERE user_id = $1", uid
            )
            expected = int(init_row["total"]) - int(spent_row["total"])
            diff = current - expected
            if diff > 50:
                results.append({"uid": uid, "current": current, "expected": expected, "diff": diff})

    if not results:
        await cb.message.edit_text(
            "✅ <b>Подозрительных балансов не найдено</b>\n\nВсе юзеры в норме.",
            reply_markup=kb_balance_menu(),
            parse_mode="HTML"
        )
        return

    results.sort(key=lambda r: r["diff"], reverse=True)
    total_excess = sum(r["diff"] for r in results)

    lines = [
        f"🔴 <b>Найдено {len(results)} юзеров с лишними кредитами</b>",
        f"💰 Общая переплата: <b>{total_excess} кр</b>",
        "",
        "<b>Топ подозрительных:</b>",
        ""
    ]
    for i, r in enumerate(results[:25], 1):
        lines.append(
            f"<b>{i}.</b> <code>{r['uid']}</code>\n"
            f"   💳 Сейчас: <b>{r['current']}</b> кр\n"
            f"   ✅ Должно: {r['expected']} кр\n"
            f"   ⚠️ Лишних: <b>+{r['diff']}</b>"
        )
    if len(results) > 25:
        lines.append(f"\n<i>...и ещё {len(results) - 25} юзеров</i>")

    full_text = "\n\n".join(lines)
    if len(full_text) > 4000:
        full_text = full_text[:3990] + "\n...[обрезано]"

    await cb.message.edit_text(full_text, reply_markup=kb_balance_menu(), parse_mode="HTML")


# ── Аудит одного юзера ────────────────────────────────────
@dp.callback_query(F.data == "adm_bal_audit_one")
async def adm_bal_audit_one_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_balance_uid)
    await state.update_data(balance_action="audit")
    await cb.message.edit_text(
        "👤 <b>Аудит юзера</b>\n\nВведи Telegram ID пользователя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_balance_menu")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


# ── Установить баланс ─────────────────────────────────────
@dp.callback_query(F.data == "adm_bal_set")
async def adm_bal_set_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_balance_uid)
    await state.update_data(balance_action="set")
    await cb.message.edit_text(
        "✏️ <b>Установить баланс</b>\n\nВведи Telegram ID пользователя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_balance_menu")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


# ── Снять кредиты ─────────────────────────────────────────
@dp.callback_query(F.data == "adm_bal_deduct")
async def adm_bal_deduct_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_balance_uid)
    await state.update_data(balance_action="deduct")
    await cb.message.edit_text(
        "➖ <b>Снять кредиты</b>\n\nВведи Telegram ID пользователя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_balance_menu")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


# ── Обработчик ввода UID (общий для 3 операций) ───────────
@dp.message(AdminState.waiting_balance_uid)
async def adm_bal_got_uid(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    txt = (message.text or "").strip()
    try:
        target_uid = int(txt)
    except ValueError:
        await message.answer(f"⛔ <code>{txt}</code> - не числовой ID", parse_mode="HTML")
        return

    data = await state.get_data()
    action = data.get("balance_action", "audit")

    pool = await get_pool()
    async with pool.acquire() as conn:
        user_row = await conn.fetchrow("SELECT credits, created_at FROM users WHERE user_id=$1", target_uid)
        if not user_row:
            await message.answer(f"❌ Юзер <code>{target_uid}</code> не найден в БД", parse_mode="HTML",
                                  reply_markup=kb_balance_menu())
            await state.clear()
            return

        current = user_row["credits"]
        init_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(credits_left), 0) AS total FROM credit_batches WHERE user_id = $1 AND (expires_at IS NULL OR expires_at > NOW())", target_uid
        )
        spent_row = await conn.fetchrow(
            "SELECT COALESCE(SUM(credits), 0) AS total FROM generations WHERE user_id = $1", target_uid
        )
        expected = int(init_row["total"]) - int(spent_row["total"])
        gens_count = await conn.fetchval(
            "SELECT COUNT(*) FROM generations WHERE user_id = $1", target_uid
        )
        purchases_count = await conn.fetchval(
            "SELECT COUNT(*) FROM credit_batches WHERE user_id = $1 AND source = 'purchase'", target_uid
        )

    diff = current - expected

    info = (
        f"👤 <b>Юзер {target_uid}</b>\n"
        f"📅 Зарегистрирован: {user_row['created_at'].strftime('%d.%m.%Y')}\n\n"
        f"💰 Текущий баланс: <b>{current} кр</b>\n"
        f"📥 Начислено всего: {int(init_row['total'])} кр\n"
        f"📤 Потрачено на генерации: {int(spent_row['total'])} кр ({gens_count} шт)\n"
        f"🛒 Покупок: {purchases_count}\n"
        f"🎯 Должно быть: <b>{expected} кр</b>\n"
    )

    if diff > 50:
        info += f"\n⚠️ <b>Переплата: +{diff} кр</b>"
    elif diff < -50:
        info += f"\n⚠️ <b>Недостача: {diff} кр</b>"
    else:
        info += f"\n✅ Баланс в норме (расхождение {diff} кр)"

    if action == "audit":
        # Показали - и хватит, возвращаемся в меню
        await state.clear()
        await message.answer(info, reply_markup=kb_balance_menu(), parse_mode="HTML")
        return

    # Для set/deduct сохраняем UID и спрашиваем сумму
    await state.update_data(target_uid=target_uid, current_balance=current, expected_balance=expected)

    if action == "set":
        await state.set_state(AdminState.waiting_balance_set)
        await message.answer(
            info + f"\n\n✏️ <b>Сколько поставить?</b>\n"
                   f"Введи число (рекомендуется <b>{max(0, expected)} кр</b> - правильный баланс)",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_balance_menu")]
            ]),
            parse_mode="HTML"
        )
    elif action == "deduct":
        recommended_deduct = max(0, diff) if diff > 0 else 0
        await state.set_state(AdminState.waiting_balance_deduct)
        await message.answer(
            info + f"\n\n➖ <b>Сколько снять?</b>\n"
                   f"Введи число"
                   + (f" (рекомендуется <b>{recommended_deduct} кр</b> - удалит переплату)" if recommended_deduct else ""),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_balance_menu")]
            ]),
            parse_mode="HTML"
        )


# ── Установка нового баланса ──────────────────────────────
@dp.message(AdminState.waiting_balance_set)
async def adm_bal_set_confirm(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    txt = (message.text or "").strip()
    try:
        new_balance = int(txt)
    except ValueError:
        await message.answer(f"⛔ <code>{txt}</code> - не число", parse_mode="HTML")
        return
    if new_balance < 0:
        await message.answer("❌ Баланс не может быть отрицательным")
        return

    data = await state.get_data()
    target_uid = data.get("target_uid")
    old_balance = data.get("current_balance", 0)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET credits = $1 WHERE user_id = $2", new_balance, target_uid)
    await log_event(target_uid, "admin_set_credits",
                    f"from={old_balance} to={new_balance} by_admin={message.from_user.id}")

    await state.clear()
    await message.answer(
        f"✅ <b>Баланс обновлён</b>\n\n"
        f"👤 <code>{target_uid}</code>\n"
        f"Было: <b>{old_balance} кр</b>\n"
        f"Стало: <b>{new_balance} кр</b>\n"
        f"Разница: <b>{new_balance - old_balance:+d} кр</b>",
        reply_markup=kb_balance_menu(),
        parse_mode="HTML"
    )


# ── Снятие кредитов ───────────────────────────────────────
@dp.message(AdminState.waiting_balance_deduct)
async def adm_bal_deduct_confirm(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    txt = (message.text or "").strip()
    try:
        amount = int(txt)
    except ValueError:
        await message.answer(f"⛔ <code>{txt}</code> - не число", parse_mode="HTML")
        return
    if amount <= 0:
        await message.answer("❌ Сумма должна быть положительной")
        return

    data = await state.get_data()
    target_uid = data.get("target_uid")
    old_balance = data.get("current_balance", 0)
    new_balance = max(0, old_balance - amount)  # не уходим в минус

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET credits = $1 WHERE user_id = $2", new_balance, target_uid)
    await log_event(target_uid, "admin_deduct_credits",
                    f"from={old_balance} to={new_balance} amount={amount} by_admin={message.from_user.id}")

    await state.clear()
    actual_deducted = old_balance - new_balance
    await message.answer(
        f"✅ <b>Кредиты сняты</b>\n\n"
        f"👤 <code>{target_uid}</code>\n"
        f"Было: <b>{old_balance} кр</b>\n"
        f"Запросил снять: {amount} кр\n"
        f"Снято: <b>{actual_deducted} кр</b>\n"
        f"Стало: <b>{new_balance} кр</b>"
        + (f"\n\n<i>ℹ️ Снято меньше т.к. баланс не уходит в минус</i>" if actual_deducted < amount else ""),
        reply_markup=kb_balance_menu(),
        parse_mode="HTML"
    )


# ── Массовый фикс всех балансов ───────────────────────────
@dp.callback_query(F.data == "adm_bal_fix_all")
async def adm_bal_fix_all_confirm(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    # Сначала показываем превью - сколько юзеров затронет
    pool = await get_pool()
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id, credits FROM users WHERE credits > 50 AND user_id != $1", ADMIN_ID)
        to_fix = []
        for user in users:
            uid = user["user_id"]
            current = user["credits"]
            init_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(credits_left), 0) AS total FROM credit_batches WHERE user_id = $1 AND (expires_at IS NULL OR expires_at > NOW())", uid
            )
            spent_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(credits), 0) AS total FROM generations WHERE user_id = $1", uid
            )
            expected = max(0, int(init_row["total"]) - int(spent_row["total"]))
            if current - expected > 50:
                to_fix.append({"uid": uid, "current": current, "expected": expected})

    if not to_fix:
        await cb.message.edit_text(
            "✅ Нечего исправлять - все балансы в норме.",
            reply_markup=kb_balance_menu()
        )
        await cb.answer()
        return

    total_remove = sum(r["current"] - r["expected"] for r in to_fix)

    await cb.message.edit_text(
        f"🔧 <b>Массовое исправление балансов</b>\n\n"
        f"Будет исправлено юзеров: <b>{len(to_fix)}</b>\n"
        f"Будет удалено кредитов: <b>{total_remove}</b>\n\n"
        f"⚠️ Это необратимая операция!\n"
        f"Для каждого юзера баланс будет установлен = <b>начислено − потрачено</b>\n\n"
        f"Подтвердить?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, исправить", callback_data="adm_bal_fix_all_do")],
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_balance_menu")],
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data == "adm_bal_fix_all_do")
async def adm_bal_fix_all_do(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await cb.answer("Исправляю...")
    await cb.message.edit_text("🔧 Исправляю балансы...")

    pool = await get_pool()
    fixed_count = 0
    total_removed = 0
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id, credits FROM users WHERE credits > 50 AND user_id != $1", ADMIN_ID)
        for user in users:
            uid = user["user_id"]
            current = user["credits"]
            init_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(credits_left), 0) AS total FROM credit_batches WHERE user_id = $1 AND (expires_at IS NULL OR expires_at > NOW())", uid
            )
            spent_row = await conn.fetchrow(
                "SELECT COALESCE(SUM(credits), 0) AS total FROM generations WHERE user_id = $1", uid
            )
            expected = max(0, int(init_row["total"]) - int(spent_row["total"]))
            diff = current - expected
            if diff > 50:
                await conn.execute("UPDATE users SET credits = $1 WHERE user_id = $2", expected, uid)
                await log_event(uid, "admin_auto_fix",
                                f"from={current} to={expected} removed={diff} by_admin={cb.from_user.id}")
                fixed_count += 1
                total_removed += diff

    await cb.message.edit_text(
        f"✅ <b>Исправлено балансов: {fixed_count}</b>\n"
        f"💰 Удалено лишних кредитов: <b>{total_removed} кр</b>\n\n"
        f"Все затронутые юзеры получили правильный баланс.",
        reply_markup=kb_balance_menu(),
        parse_mode="HTML"
    )


# ══════════════════════════════════════════════════════════
#  КОНЕЦ: УПРАВЛЕНИЕ БАЛАНСАМИ
# ══════════════════════════════════════════════════════════


@dp.callback_query(F.data == "adm_give_credits")
async def adm_give_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_user_id)
    await cb.message.answer(
        "➕ <b>Начислить кредиты</b>\n\nВведи Telegram ID пользователя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_user_id)
async def adm_get_user_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text:
        await message.answer("❌ Отправь Telegram ID текстом")
        return
    txt = message.text.strip()
    logging.info(f"ADMIN give_credits input: '{txt}'")
    try:
        target_id = int(txt)
    except (ValueError, TypeError):
        await message.answer(
            f"⛔ <code>{txt}</code> - не числовой ID\n"
            f"Введи только цифры, например: <code>123456789</code>",
            parse_mode="HTML"
        )
        return
    try:
        user = await get_user(target_id)
        credits_balance = user["credits"] if user else 0
        status = "✅ Зарегистрирован" if user else "⚠️ Не в базе (создам при начислении)"
        await state.update_data(target_id=target_id)
        await state.set_state(AdminState.waiting_credits)
        await message.answer(
            f"👤 ID: <code>{target_id}</code>\n"
            f"Статус: {status}\n"
            f"Баланс: <b>{credits_balance} кредитов</b>\n\n"
            f"Сколько кредитов начислить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"adm_get_user_id error: {e}")
        await message.answer(f"⛔ Ошибка: {e}")
        await state.clear()


@dp.message(AdminState.waiting_credits)
async def adm_give_credits_confirm(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    txt = message.text.strip() if message.text else ""
    try:
        amount = int(txt)
        if amount <= 0:
            await message.answer("❌ Введи положительное число:")
            return
    except (ValueError, TypeError):
        await message.answer("❌ Введи число, например: <code>50</code>", parse_mode="HTML")
        return
    data = await state.get_data()
    target_id = data["target_id"]
    # Создаём пользователя если его нет
    user = await get_user(target_id)
    if not user:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, credits) VALUES ($1, 0) ON CONFLICT DO NOTHING",
                target_id
            )
    await add_credits(target_id, amount)
    new_balance = await get_credits(target_id)
    await state.clear()
    await message.answer(
        f"✨ <b>Кредиты начислены!</b>\n\n"
        f"👤 ID: <code>{target_id}</code>\n"
        f"✨ Начислено: <b>{amount} кредитов</b>\n"
        f"💳 Новый баланс: <b>{new_balance} кредитов</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Ещё начислить", callback_data="adm_give_credits")],
            [InlineKeyboardButton(text="◀️ Панель",        callback_data="adm_back")],
        ]),
        parse_mode="HTML"
    )
    try:
        await bot.send_message(
            target_id,
            f"🎁 Тебе начислено <b>{amount} кредитов</b> от администратора!\n"
            f"💎 Баланс: <b>{new_balance} кредитов</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass


@dp.callback_query(F.data == "adm_cancel")
async def adm_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("❌ Отменено. Нажми /admin чтобы вернуться в панель.")
    except Exception:
        await cb.message.answer("❌ Отменено.")
    await cb.answer()


# ─── Блокировки ───────────────────────────────────────────

@dp.callback_query(F.data == "adm_blocks")
async def adm_blocks_menu(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            blocked_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_blocked=1") or 0
            blocked_list = await conn.fetch("SELECT user_id FROM users WHERE is_blocked=1 LIMIT 10")

        blocked_text = ", ".join([str(r["user_id"]) for r in blocked_list]) or "нет"

        await cb.message.answer(
            f"🚫 <b>Блокировки</b>\n\n"
            f"Заблокировано пользователей: <b>{blocked_count}</b>\n"
            f"ID: {blocked_text}\n\n"
            f"Введи ID пользователя чтобы заблокировать или разблокировать:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
            ]),
            parse_mode="HTML"
        )
        await state.set_state(AdminState.waiting_block_id)
    except Exception as e:
        logging.error(f"adm_blocks error: {e}")
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


@dp.message(AdminState.waiting_block_id)
async def adm_block_check_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    txt = message.text.strip() if message.text else ""
    try:
        target_id = int(txt)
    except (ValueError, TypeError):
        await message.answer("❌ Введи числовой Telegram ID, например: <code>123456789</code>", parse_mode="HTML")
        return
    user = await get_user(target_id)
    if not user:
        await message.answer(
            f"🔍 Пользователь <code>{target_id}</code> не найден в базе.\n"
            f"Он ещё не использовал бота.",
            parse_mode="HTML"
        )
        await state.clear()
        return
    blocked = bool(user.get("is_blocked", 0))
    status = "🚫 Заблокирован" if blocked else "✅ Активен"
    await state.clear()
    await message.answer(
        f"👤 ID: <code>{target_id}</code>\n"
        f"Статус: {status}\n"
        f"Баланс: <b>{user['credits']} кредитов</b>",
        reply_markup=kb_block_actions(target_id, blocked),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("adm_block:"))
async def adm_do_block(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        target_id = int(cb.data.split(":")[1])
        await block_user(target_id)
        await cb.message.edit_text(
            f"🚫 Пользователь <code>{target_id}</code> заблокирован.\n"
            f"Он больше не сможет пользоваться ботом.",
            reply_markup=kb_block_actions(target_id, True),
            parse_mode="HTML"
        )
        try:
            await bot.send_message(target_id, "🚫 Ваш доступ к боту ограничен администратором.")
        except Exception:
            pass
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


@dp.callback_query(F.data.startswith("adm_unblock:"))
async def adm_do_unblock(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        target_id = int(cb.data.split(":")[1])
        await unblock_user(target_id)
        await cb.message.edit_text(
            f"✅ Пользователь <code>{target_id}</code> разблокирован.",
            reply_markup=kb_block_actions(target_id, False),
            parse_mode="HTML"
        )
        try:
            await bot.send_message(target_id, "✅ Ваш доступ к боту восстановлен!")
        except Exception:
            pass
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()



# ─── Активность ───────────────────────────────────────────

@dp.callback_query(F.data == "adm_activity")
async def adm_activity(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    await _show_activity_page(cb, page=0)


@dp.callback_query(F.data.startswith("adm_act_p:"))
async def adm_activity_page(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    try:
        page = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        page = 0
    await _show_activity_page(cb, page)


@dp.callback_query(F.data == "adm_popular")
async def adm_popular(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    # Словарь ключ → читаемое название
    MODEL_NAMES = {
        "img_fast":          "⚡ Imagen 4 Fast",
        "img_std":           "🌟 Imagen 4",
        "img_ultra":         "✨ Imagen 4 Ultra",
        "vid_lite":          "🎞 Veo 3.1 Lite",
        "vid_fast":          "🎥 Veo 3.1 Fast",
        "vid_pro":           "💎 Veo 3.1",
        "gemini-flash-image":"✏️ Редактирование фото",
        "edit":              "✏️ Редактирование фото",
    }
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model, COUNT(*), SUM(credits) FROM generations GROUP BY model ORDER BY COUNT(*) DESC"
            )
        if not rows:
            text = "🔥 <b>Популярные модели</b>\n\nПока нет генераций."
        else:
            # Группируем по категориям
            CATEGORIES = {
                "photo": {
                    "label": "🖼 Фото",
                    "keys": ["img_fast", "img_std", "img_ultra",
                             "nb_flash", "nb_2", "nb_pro",
                             "flux_pro", "ideogram_v3"],
                },
                "video": {
                    "label": "🎥 Видео",
                    "keys": ["vid_lite", "vid_fast", "vid_pro",
                             "kling_turbo", "kling_pro"],
                },
                "edit": {
                    "label": "✏️ Редактирование",
                    "keys": ["gemini-flash-image", "edit"],
                },
                "anim": {
                    "label": "🎭 Анимация",
                    "keys": ["veo-3.1-animate"],
                },
            }
            # Словарь ключ → (gens, credits) из БД
            stats = {r[0]: (r[1], r[2] or 0) for r in rows}
            other_keys = set(stats.keys())

            sections = []
            for cat in CATEGORIES.values():
                cat_rows = []
                for key in cat["keys"]:
                    if key in stats:
                        other_keys.discard(key)
                        name = MODEL_NAMES.get(key, key)
                        gens, creds = stats[key]
                        cat_rows.append((name, gens, creds))
                if cat_rows:
                    # Сортируем по числу генераций внутри категории
                    cat_rows.sort(key=lambda x: x[1], reverse=True)
                    lines = "\n".join(
                        f"    • <b>{n}</b>: {g} ген · {c} кр"
                        for n, g, c in cat_rows
                    )
                    sections.append(f"{cat['label']}\n{lines}")

            # Остальные модели (если есть неизвестные ключи)
            if other_keys:
                extra_lines = []
                for key in other_keys:
                    name = MODEL_NAMES.get(key, key)
                    gens, creds = stats[key]
                    extra_lines.append(f"    • <b>{name}</b>: {gens} ген · {creds} кр")
                sections.append("🔧 Прочее\n" + "\n".join(extra_lines))

            # Итого
            total_gens = sum(v[0] for v in stats.values())
            total_creds = sum(v[1] for v in stats.values())

            text = (
                "🔥 <b>Популярные модели</b>\n\n"
                + "\n\n".join(sections)
                + f"\n\n━━━━━━━━━━━━━━\n"
                + f"📊 Итого: <b>{total_gens} ген</b> · <b>{total_creds} кр</b>"
            )
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
        ]), parse_mode="HTML")
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


# ─── Топ активных пользователей ───────────────────────────

@dp.callback_query(F.data == "adm_top_users")
async def adm_top_users(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT g.user_id, u.username, COUNT(*) as cnt, COALESCE(SUM(g.credits),0) as total_credits
                FROM generations g LEFT JOIN users u ON g.user_id=u.user_id
                GROUP BY g.user_id, u.username ORDER BY cnt DESC LIMIT 10
            """)
        if not rows:
            text = "🏆 <b>Топ активных</b>\n\nПока нет данных."
        else:
            lines = []
            for i, r in enumerate(rows):
                uname = f"@{r[1]}" if r[1] else f"ID {r[0]}"
                lines.append(f"  {i+1}. {uname}: {r[2]} ген, {r[3]} кредитов")
            text = "🏆 <b>Топ активных пользователей</b>\n\n" + "\n".join(lines)
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
        ]), parse_mode="HTML")
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


# ─── Список пользователей ─────────────────────────────────

@dp.callback_query(F.data == "adm_users")
async def adm_users(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    await _show_users_page(cb, page=0)


@dp.callback_query(F.data.startswith("adm_users_p:"))
async def adm_users_page(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    try:
        page = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        page = 0
    await _show_users_page(cb, page)


@dp.callback_query(F.data == "adm_find")
async def adm_find_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    await state.set_state(AdminState.waiting_find_user)
    await cb.message.answer(
        "🔍 <b>Найти пользователя</b>\n\nВведи Telegram ID:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_find_user)
async def adm_find_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    txt = message.text.strip() if message.text else ""
    try:
        uid = int(txt)
    except (ValueError, TypeError):
        await message.answer(
            "❌ Введи числовой Telegram ID\n<i>Пример: 123456789</i>",
            parse_mode="HTML"
        )
        return
    await state.clear()
    user = await get_user(uid)
    if not user:
        await message.answer(
            f"🔍 Пользователь <code>{uid}</code> не найден.\n"
            f"Он ещё не запускал бота.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
            ]),
            parse_mode="HTML"
        )
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE user_id=$1", uid
        )
        pay_row = await conn.fetchrow(
            "SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM payments WHERE user_id=$1", uid
        )
        last_gen = await conn.fetchrow(
            "SELECT model, created_at FROM generations WHERE user_id=$1 ORDER BY created_at DESC LIMIT 1", uid
        )
    blocked = "🚫 Да" if user.get("is_blocked") else "✅ Нет"
    username = (user.get("username") or "").strip()
    full_name = (user.get("full_name") or "").strip()
    uname = f"@{username}" if username else (full_name or "-")
    last_active = str(user.get("last_active", ""))[:16].replace("T", " ")
    created_at = str(user.get("created_at", ""))[:10]
    last_gen_text = f"{last_gen['model']} ({str(last_gen['created_at'])[:10]})" if last_gen else "-"

    kb_rows = [
        [InlineKeyboardButton(
            text="✍️ Написать пользователю",
            url=f"tg://user?id={uid}"
        )],
        [InlineKeyboardButton(
            text="💰 Начислить кредиты",
            callback_data=f"adm_give_to:{uid}"
        )],
        [InlineKeyboardButton(
            text="🚫 Заблокировать" if not user.get("is_blocked") else "✅ Разблокировать",
            callback_data=f"adm_block:{uid}" if not user.get("is_blocked") else f"adm_unblock:{uid}"
        )],
        [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")],
    ]
    await message.answer(
        f"🪪 <b>Пользователь</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📋 Имя: {full_name or '-'}\n"
        f"📧 Username: {('@' + username) if username else '-'}\n"
        f"💎 Баланс: <b>{user['credits']} кредитов</b>\n"
        f"🎨 Генераций: <b>{row[0]}</b> ({row[1]} кредитов потрачено)\n"
        f"💰 Платежей: {pay_row[0]} на {pay_row[1]}₽\n"
        f"🕐 Последняя активность: {last_active or '-'}\n"
        f"🎯 Последняя генерация: {last_gen_text}\n"
        f"🚫 Заблокирован: {blocked}\n"
        f"📅 Регистрация: {created_at}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML"
    )


# ─── Быстрое начисление из карточки пользователя ──────────

@dp.callback_query(F.data.startswith("adm_give_to:"))
async def adm_give_to(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    uid = int(cb.data.split(":")[1])
    await state.update_data(target_user_id=uid)
    await state.set_state(AdminState.waiting_credits)
    await cb.message.answer(
        f"\U0001f4b3 Начислить кредиты пользователю <code>{uid}</code>\n\nВведи количество кредитов:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


# ─── История платежей ─────────────────────────────────────

@dp.callback_query(F.data == "adm_payments")
async def adm_payments(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    await _show_payments_page(cb, page=0)


@dp.callback_query(F.data.startswith("adm_pay_p:"))
async def adm_payments_page(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    try:
        page = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        page = 0
    await _show_payments_page(cb, page)


@dp.callback_query(F.data == "adm_spend")
async def adm_spend_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    await state.set_state(AdminState.waiting_spend_uid)
    await cb.message.answer(
        "💰 <b>Расход по пользователю</b>\n\nВведи Telegram ID:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_spend_uid)
async def adm_spend_show(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    txt = message.text.strip() if message.text else ""
    try:
        uid = int(txt)
    except (ValueError, TypeError):
        await message.answer("❌ Введи числовой ID, например: <code>123456789</code>", parse_mode="HTML")
        return
    await state.clear()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT model, COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE user_id=$1 GROUP BY model ORDER BY COUNT(*) DESC",
            uid
        )
        total = await conn.fetchrow(
            "SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE user_id=$1", uid
        )
    user = await get_user(uid)
    if not user:
        await message.answer(
            f"🔍 Пользователь <code>{uid}</code> не найден в базе.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
            ]),
            parse_mode="HTML"
        )
        return
    if not rows:
        await message.answer(
            f"💰 Пользователь <code>{uid}</code> ещё не делал генераций.\n"
            f"Баланс: <b>{user['credits']} кредитов</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
            ]),
            parse_mode="HTML"
        )
        return
    lines = [f"  • {r[0]}: {r[1]} раз, {r[2] or 0} кредитов" for r in rows]
    await message.answer(
        f"💰 <b>Расход пользователя</b> <code>{uid}</code>\n\n"
        f"Всего генераций: <b>{total[0]}</b>\n"
        f"Всего кредитов потрачено: <b>{total[1]}</b>\n"
        f"Текущий баланс: <b>{user['credits']} кредитов</b>\n\n"
        f"<b>По моделям:</b>\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
        ]),
        parse_mode="HTML"
    )


# ─── Изменить приветствие ─────────────────────────────────

@dp.callback_query(F.data == "adm_welcome")
async def adm_welcome_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    current = await get_setting("welcome_extra", "")
    await state.set_state(AdminState.waiting_welcome)
    await cb.message.answer(
        f"✏️ <b>Изменить приветствие</b>\n\n"
        f"Текущий доп. текст:\n<i>{current or 'не задан'}</i>\n\n"
        f"Введи новый текст (добавится к стандартному приветствию):\n"
        f"Или напиши <b>убрать</b> чтобы удалить.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_welcome)
async def adm_welcome_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    text = "" if message.text.strip().lower() == "убрать" else message.text.strip()
    await set_setting("welcome_extra", text)
    await message.answer(
        f"✅ Приветствие {'удалено' if not text else 'обновлено'}!\n\n"
        f"<i>{text or 'пусто'}</i>",
        parse_mode="HTML"
    )


# ─── Рассылка ─────────────────────────────────────────────

@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_blocked=0") or 0
    await state.set_state(AdminState.waiting_broadcast)
    await cb.message.answer(
        f"📢 <b>Рассылка</b>\n\n"
        f"Получателей: <b>{total} пользователей</b>\n\n"
        f"Отправь сообщение для рассылки — <b>текст, фото или видео</b> с любым "
        f"форматированием.\nОформляй прямо в Telegram (жирный, курсив, эмодзи, "
        f"картинка) — бот скопирует его всем как есть:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_broadcast)
async def adm_broadcast_send(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    pool = await get_pool()
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users WHERE is_blocked=0")
    sent = 0
    failed = 0
    status_msg = await message.answer(f"📢 Рассылка запущена... 0/{len(users)}")
    for i, r in enumerate(users):
        uid = r["user_id"]
        try:
            # copy_message копирует ЛЮБОЕ сообщение: текст, фото, видео, документ —
            # с форматированием, эмодзи и подписью. Рассылка поддерживает любой формат.
            await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id,
                                   message_id=message.message_id)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)  # ~20 сообщений/сек — не упереться во флуд-лимит Telegram
        if (i + 1) % 25 == 0:
            try:
                await status_msg.edit_text(f"📢 Рассылка... {i+1}/{len(users)}")
            except Exception:
                pass
    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"✅ Отправлено: {sent}\n"
        f"❌ Не доставлено: {failed}",
        parse_mode="HTML"
    )


# ─── Техобслуживание ──────────────────────────────────────

@dp.callback_query(F.data == "adm_maintenance")
async def adm_maintenance(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    try:
        current = await get_setting("maintenance", "0")
        new_val = "0" if current == "1" else "1"
        await set_setting("maintenance", new_val)
        status = "🔴 ВКЛЮЧЁН" if new_val == "1" else "🟢 ВЫКЛЮЧЕН"
        await cb.message.answer(
            f"🔧 <b>Техобслуживание {status}</b>\n\n"
            f"{'Пользователи видят сообщение о техработах.' if new_val == '1' else 'Бот работает в штатном режиме.'}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


# ─── Кнопка "назад к панели" ──────────────────────────────

# adm_back обработчик выше (adm_back_to_panel)


# ─── АДМИН: промокоды ────────────────────────────────────

@dp.callback_query(F.data == "adm_prices")
async def adm_prices(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer()
        return
    await cb.message.edit_text(
        "\U0001f4b5 <b>\u0420\u0435\u0434\u0430\u043a\u0442\u043e\u0440 \u0446\u0435\u043d \u0438 \u0442\u043e\u0432\u0430\u0440\u043e\u0432</b>\n\n\u0412\u044b\u0431\u0435\u0440\u0438 \u0440\u0430\u0437\u0434\u0435\u043b:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f4e6 \u041f\u0430\u043a\u0435\u0442\u044b \u043a\u0440\u0435\u0434\u0438\u0442\u043e\u0432", callback_data="adm_prices_packs")],
            [InlineKeyboardButton(text="\U0001f6cd \u041c\u0430\u0433\u0430\u0437\u0438\u043d \u043f\u043e\u0434\u043f\u0438\u0441\u043e\u043a", callback_data="adm_prices_shop")],
            [InlineKeyboardButton(text="\U0001f3a8 \u0426\u0435\u043d\u044b \u043d\u0430 \u0433\u0435\u043d\u0435\u0440\u0430\u0446\u0438\u0438", callback_data="adm_prices_gen")],
            [InlineKeyboardButton(text="\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434", callback_data="adm_back")],
        ])
    )
    await cb.answer()


@dp.callback_query(F.data == "adm_prices_packs")
async def adm_prices_packs(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        return
    lines = [f"\u2022 <b>{p['name']}</b> \u2014 {p['credits']} \u043a\u0440 \u0437\u0430 <b>{p['price']}\u20bd</b>" for _, p in CREDIT_PACKS.items()]
    rows = [[InlineKeyboardButton(text=f"\u270f\ufe0f {p['name']} ({p['price']}\u20bd)", callback_data=f"adm_edit_pack:{key}")] for key, p in CREDIT_PACKS.items()]
    rows += [[InlineKeyboardButton(text="\u2795 \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043f\u0430\u043a\u0435\u0442", callback_data="adm_add_pack")],
             [InlineKeyboardButton(text="\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434", callback_data="adm_prices")]]
    await cb.message.edit_text(
        "\U0001f4e6 <b>\u041f\u0430\u043a\u0435\u0442\u044b \u043a\u0440\u0435\u0434\u0438\u0442\u043e\u0432</b>\n\n" + "\n".join(lines),
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_edit_pack:"))
async def adm_edit_pack(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return
    key = cb.data.split(":")[1]
    p = CREDIT_PACKS.get(key)
    if not p:
        await cb.answer("\u041f\u0430\u043a\u0435\u0442 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d", show_alert=True); return
    await state.update_data(edit_pack_key=key)
    await cb.message.edit_text(
        f"\u270f\ufe0f <b>\u0420\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435 \u043f\u0430\u043a\u0435\u0442\u0430</b>\n\n\u041a\u043b\u044e\u0447: <code>{key}</code>\n\u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435: <b>{p['name']}</b>\n\u041a\u0440\u0435\u0434\u0438\u0442\u043e\u0432: <b>{p['credits']}</b>\n\u0426\u0435\u043d\u0430: <b>{p['price']}\u20bd</b>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f4b0 \u0426\u0435\u043d\u0430", callback_data=f"adm_pack_field:{key}:price"),
             InlineKeyboardButton(text="\U0001f48e \u041a\u0440\u0435\u0434\u0438\u0442\u044b", callback_data=f"adm_pack_field:{key}:credits")],
            [InlineKeyboardButton(text="\U0001f4dd \u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435", callback_data=f"adm_pack_field:{key}:name")],
            [InlineKeyboardButton(text="\U0001f5d1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c", callback_data=f"adm_del_pack:{key}")],
            [InlineKeyboardButton(text="\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434", callback_data="adm_prices_packs")],
        ]))
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_pack_field:"))
async def adm_pack_field(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    parts = cb.data.split(":")
    key, field = parts[1], parts[2]
    p = CREDIT_PACKS.get(key, {})
    field_names = {"price": "\u0446\u0435\u043d\u0443 (\u20bd)", "credits": "\u043a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e \u043a\u0440\u0435\u0434\u0438\u0442\u043e\u0432", "name": "\u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435"}
    await state.update_data(edit_pack_key=key, edit_pack_field=field)
    await state.set_state(AdminEditState.waiting_value)
    await cb.message.edit_text(
        f"\u270f\ufe0f \u0412\u0432\u0435\u0434\u0438 \u043d\u043e\u0432\u043e\u0435 \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u0434\u043b\u044f <b>{field_names.get(field, field)}</b>\n\n\u0422\u0435\u043a\u0443\u0449\u0435\u0435: <code>{p.get(field, '')}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="\u274c \u041e\u0442\u043c\u0435\u043d\u0430", callback_data="adm_prices_packs")]]))
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_del_pack:"))
async def adm_del_pack(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    key = cb.data.split(":")[1]
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE bot_credit_packs SET enabled=FALSE WHERE key=$1", key)
    CREDIT_PACKS.pop(key, None)
    await cb.answer("\u041f\u0430\u043a\u0435\u0442 \u0443\u0434\u0430\u043b\u0451\u043d", show_alert=True)
    await adm_prices_packs(cb)


@dp.callback_query(F.data == "adm_add_pack")
async def adm_add_pack(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.update_data(edit_pack_key=None, edit_pack_field="new_pack")
    await state.set_state(AdminEditState.waiting_value)
    await cb.message.edit_text(
        "\u2795 <b>\u041d\u043e\u0432\u044b\u0439 \u043f\u0430\u043a\u0435\u0442</b>\n\n\u0424\u043e\u0440\u043c\u0430\u0442:\n<code>\u043a\u043b\u044e\u0447|\u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435|\u043a\u0440\u0435\u0434\u0438\u0442\u044b|\u0446\u0435\u043d\u0430</code>\n\n\u041f\u0440\u0438\u043c\u0435\u0440: <code>p300|\U0001f3c6 \u041c\u0430\u043a\u0441\u0438\u043c\u0443\u043c|3000|1490</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="\u274c \u041e\u0442\u043c\u0435\u043d\u0430", callback_data="adm_prices_packs")]]))
    await cb.answer()


@dp.callback_query(F.data == "adm_prices_shop")
async def adm_prices_shop(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    rows = []
    for key, s in SHOP_CATALOG.items():
        _eid = _btn_emoji_id(key, s)
        _cnt = len(s.get('plans', []))
        rows.append([InlineKeyboardButton(
            text=(f"{s['name']} ({_cnt} \u0442\u0430\u0440\u0438\u0444\u043e\u0432)" if _eid
                  else f"{s.get('emoji','')} {s['name']} ({_cnt} \u0442\u0430\u0440\u0438\u0444\u043e\u0432)"),
            callback_data=f"adm_shop_service:{key}",
            **({"icon_custom_emoji_id": _eid} if _eid else {})
        )])
    rows += [[InlineKeyboardButton(text="\u2795 \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0441\u0435\u0440\u0432\u0438\u0441", callback_data="adm_add_service")],
             [InlineKeyboardButton(text="\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434", callback_data="adm_prices")]]
    await cb.message.edit_text("\U0001f6cd <b>\u041c\u0430\u0433\u0430\u0437\u0438\u043d \u043f\u043e\u0434\u043f\u0438\u0441\u043e\u043a</b>",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_shop_service:"))
async def adm_shop_service(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    key = cb.data.split(":")[1]
    s = SHOP_CATALOG.get(key)
    if not s:
        await cb.answer("\u0421\u0435\u0440\u0432\u0438\u0441 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d", show_alert=True); return
    plans_text = "\n".join([f"  {i}. {p['name']} \u2014 <b>{p['price']}\u20bd</b>" for i, p in enumerate(s['plans'])])
    # \u0422\u0430\u0440\u0438\u0444\u044b
    rows = [[InlineKeyboardButton(text=f"\u270f\ufe0f {p['name']} ({p['price']}\u20bd)", callback_data=f"adm_shop_plan:{key}:{i}")] for i, p in enumerate(s['plans'])]
    # \u0420\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435 \u0441\u0430\u043c\u043e\u0433\u043e \u0441\u0435\u0440\u0432\u0438\u0441\u0430
    rows += [
        [InlineKeyboardButton(text="\ud83d\udcdd \u041d\u0430\u0437\u0432\u0430\u043d\u0438\u0435", callback_data=f"adm_svc_field:{key}:name"),
         InlineKeyboardButton(text="\ud83c\udfad \u042d\u043c\u043e\u0434\u0437\u0438",   callback_data=f"adm_svc_field:{key}:emoji")],
        [InlineKeyboardButton(text="\ud83d\udcc4 \u041e\u043f\u0438\u0441\u0430\u043d\u0438\u0435", callback_data=f"adm_svc_field:{key}:desc")],
        [InlineKeyboardButton(text="\u2795 \u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u0442\u0430\u0440\u0438\u0444",  callback_data=f"adm_add_plan:{key}")],
        [InlineKeyboardButton(text="\ud83d\uddd1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0441\u0435\u0440\u0432\u0438\u0441",  callback_data=f"adm_del_service:{key}")],
        [InlineKeyboardButton(text="\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434", callback_data="adm_prices_shop")],
    ]
    text = (
        f"{tg_emoji(s)} <b>{s['name']}</b>\n"
        f"<i>{s.get('desc', '')}</i>\n\n"
        + (plans_text if plans_text else "<i>\u0422\u0430\u0440\u0438\u0444\u043e\u0432 \u043d\u0435\u0442</i>")
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


# \u2500\u2500\u2500 \u0420\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435 \u043f\u043e\u043b\u0435\u0439 \u0441\u0435\u0440\u0432\u0438\u0441\u0430 (\u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435 / \u044d\u043c\u043e\u0434\u0437\u0438 / \u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435) \u2500\u2500

@dp.callback_query(F.data.startswith("adm_svc_field:"))
async def adm_svc_field(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    parts = cb.data.split(":")
    key, field = parts[1], parts[2]
    s = SHOP_CATALOG.get(key, {})
    field_labels = {"name": "\u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435", "emoji": "\u044d\u043c\u043e\u0434\u0437\u0438", "desc": "\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435"}
    current = s.get(field, "")
    await state.update_data(edit_shop_key=key, edit_shop_plan=None, edit_shop_field=f"svc_{field}")
    await state.set_state(AdminEditState.waiting_value)
    await cb.message.edit_text(
        f"\u270f\ufe0f \u0412\u0432\u0435\u0434\u0438 \u043d\u043e\u0432\u043e\u0435 <b>{field_labels.get(field, field)}</b> \u0434\u043b\u044f <b>{s.get('emoji','')} {s.get('name', key)}</b>\n\n"
        f"\u0422\u0435\u043a\u0443\u0449\u0435\u0435: <code>{current}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="\u274c \u041e\u0442\u043c\u0435\u043d\u0430", callback_data=f"adm_shop_service:{key}")
        ]])
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_shop_plan:"))
async def adm_shop_plan(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    parts = cb.data.split(":")
    key, plan_idx = parts[1], int(parts[2])
    s = SHOP_CATALOG.get(key, {})
    plans = s.get("plans", [])
    if plan_idx >= len(plans):
        await cb.answer("\u0422\u0430\u0440\u0438\u0444 \u043d\u0435 \u043d\u0430\u0439\u0434\u0435\u043d", show_alert=True); return
    p = plans[plan_idx]
    _manual = (await get_setting(f"manual:{key}:{plan_idx}", "0") or "0") == "1"
    _mline = ("🧾 Ручная выдача: <b>ВКЛ</b> (заказ тебе, без авто-активации)"
              if _manual else "🧾 Ручная выдача: <b>выкл</b> (авто-флоу сервиса)")
    await cb.message.edit_text(
        f"✏️ <b>{s.get('name', key)} — {p['name']}</b>\n\nЦена: <b>{p['price']}₽</b>\n{p.get('desc', '')}\n\n{_mline}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💰 Цена", callback_data=f"adm_plan_field:{key}:{plan_idx}:price"),
             InlineKeyboardButton(text="📝 Название", callback_data=f"adm_plan_field:{key}:{plan_idx}:name")],
            [InlineKeyboardButton(text="📄 Описание", callback_data=f"adm_plan_field:{key}:{plan_idx}:desc")],
            [InlineKeyboardButton(text=("🧾 Ручная выдача: ВЫКЛ" if _manual else "🧾 Ручная выдача: ВКЛ"),
                                  callback_data=f"adm_plan_manual:{key}:{plan_idx}")],
            [InlineKeyboardButton(text="🗑 Удалить тариф", callback_data=f"adm_del_plan:{key}:{plan_idx}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_shop_service:{key}")],
        ]))
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_plan_manual:"))
async def adm_plan_manual(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        return
    parts = cb.data.split(":")
    key, plan_idx = parts[1], int(parts[2])
    cur = (await get_setting(f"manual:{key}:{plan_idx}", "0") or "0") == "1"
    await set_setting(f"manual:{key}:{plan_idx}", "0" if cur else "1")
    cb.data = f"adm_shop_plan:{key}:{plan_idx}"
    await adm_shop_plan(cb, state)


@dp.callback_query(F.data.startswith("adm_plan_field:"))
async def adm_plan_field(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    parts = cb.data.split(":")
    key, plan_idx, field = parts[1], int(parts[2]), parts[3]
    s = SHOP_CATALOG.get(key, {})
    p = s.get("plans", [])[plan_idx] if plan_idx < len(s.get("plans", [])) else {}
    field_names = {"price": "\u0446\u0435\u043d\u0443 (\u20bd)", "name": "\u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435", "desc": "\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435"}
    await state.update_data(edit_shop_key=key, edit_shop_plan=plan_idx, edit_shop_field=field)
    await state.set_state(AdminEditState.waiting_value)
    await cb.message.edit_text(
        f"\u270f\ufe0f \u0412\u0432\u0435\u0434\u0438 \u043d\u043e\u0432\u043e\u0435 \u0437\u043d\u0430\u0447\u0435\u043d\u0438\u0435 \u0434\u043b\u044f <b>{field_names.get(field, field)}</b>\n\n\u0422\u0435\u043a\u0443\u0449\u0435\u0435: <code>{p.get(field, '')}</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="\u274c \u041e\u0442\u043c\u0435\u043d\u0430", callback_data=f"adm_shop_service:{key}")]]))
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_del_plan:"))
async def adm_del_plan(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    parts = cb.data.split(":")
    key, plan_idx = parts[1], int(parts[2])
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE bot_shop_items SET enabled=FALSE WHERE key=$1 AND plan_idx=$2", key, plan_idx)
    if key in SHOP_CATALOG and plan_idx < len(SHOP_CATALOG[key]["plans"]):
        SHOP_CATALOG[key]["plans"].pop(plan_idx)
    await cb.answer("\u0422\u0430\u0440\u0438\u0444 \u0443\u0434\u0430\u043b\u0451\u043d", show_alert=True)
    await adm_shop_service(cb)


@dp.callback_query(F.data.startswith("adm_del_service:"))
async def adm_del_service(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    key = cb.data.split(":")[1]
    s = SHOP_CATALOG.get(key, {})
    name = f"{s.get('emoji','')} {s.get('name', key)}"
    # \u041f\u0435\u0440\u0432\u044b\u0439 \u0448\u0430\u0433 \u2014 \u0437\u0430\u043f\u0440\u043e\u0441 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u044f
    await cb.message.edit_text(
        f"\ud83d\uddd1 <b>\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0441\u0435\u0440\u0432\u0438\u0441?</b>\n\n"
        f"{name}\n\n"
        f"\u26a0\ufe0f \u042d\u0442\u043e \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u043d\u0435\u043b\u044c\u0437\u044f \u043e\u0442\u043c\u0435\u043d\u0438\u0442\u044c. \u0412\u0441\u0435 \u0442\u0430\u0440\u0438\u0444\u044b \u0441\u0435\u0440\u0432\u0438\u0441\u0430 \u0431\u0443\u0434\u0443\u0442 \u0443\u0434\u0430\u043b\u0435\u043d\u044b.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\u2705 \u0414\u0430, \u0443\u0434\u0430\u043b\u0438\u0442\u044c",  callback_data=f"adm_del_service_confirm:{key}")],
            [InlineKeyboardButton(text="\u274c \u041e\u0442\u043c\u0435\u043d\u0430",       callback_data=f"adm_shop_service:{key}")],
        ])
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_del_service_confirm:"))
async def adm_del_service_confirm(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    key = cb.data.split(":")[1]
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE bot_shop_items SET enabled=FALSE WHERE key=$1", key)
    SHOP_CATALOG.pop(key, None)
    await cb.answer("\u0421\u0435\u0440\u0432\u0438\u0441 \u0443\u0434\u0430\u043b\u0451\u043d", show_alert=True)
    await adm_prices_shop(cb)


@dp.callback_query(F.data == "adm_add_service")
async def adm_add_service(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    await state.update_data(edit_shop_key=None, edit_shop_field="new_service")
    await state.set_state(AdminEditState.waiting_value)
    await cb.message.edit_text(
        "➕ <b>Новый сервис</b>\n\n"
        "Отправь 4 строки подряд:\n\n"
        "<code>ключ\n"
        "эмодзи\n"
        "название\n"
        "описание</code>\n\n"
        "Пример:\n"
        "<code>notion\n"
        "📓\n"
        "Notion AI\n"
        "Инструмент для заметок с AI</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_prices_shop")]]))
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_add_plan:"))
async def adm_add_plan(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    key = cb.data.split(":")[1]
    await state.update_data(edit_shop_key=key, edit_shop_field="new_plan")
    await state.set_state(AdminEditState.waiting_value)
    sname = SHOP_CATALOG.get(key, {}).get("name", key)
    await cb.message.edit_text(
        f"\u2795 <b>\u041d\u043e\u0432\u044b\u0439 \u0442\u0430\u0440\u0438\u0444 \u0434\u043b\u044f {sname}</b>\n\n\u0424\u043e\u0440\u043c\u0430\u0442:\n<code>\u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435|\u0446\u0435\u043d\u0430|\u043e\u043f\u0438\u0441\u0430\u043d\u0438\u0435</code>\n\n\u041f\u0440\u0438\u043c\u0435\u0440: <code>Business|5000|\u0414\u043b\u044f \u043a\u043e\u043c\u0430\u043d\u0434</code>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="\u274c \u041e\u0442\u043c\u0435\u043d\u0430", callback_data=f"adm_shop_service:{key}")]]))
    await cb.answer()


@dp.callback_query(F.data == "adm_prices_gen")
async def adm_prices_gen(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    await cb.message.edit_text(
        "\U0001f3a8 <b>\u0426\u0435\u043d\u044b \u043d\u0430 \u0433\u0435\u043d\u0435\u0440\u0430\u0446\u0438\u0438</b>\n\n\u0412\u044b\u0431\u0435\u0440\u0438 \u0440\u0430\u0437\u0434\u0435\u043b:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="\U0001f4f7 \u0424\u043e\u0442\u043e-\u043c\u043e\u0434\u0435\u043b\u0438", callback_data="adm_gen_section:image")],
            [InlineKeyboardButton(text="\U0001f3ac \u0412\u0438\u0434\u0435\u043e-\u043c\u043e\u0434\u0435\u043b\u0438", callback_data="adm_gen_section:video")],
            [InlineKeyboardButton(text="\U0001f58c\ufe0f \u0420\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435", callback_data="adm_gen_section:edit")],
            [InlineKeyboardButton(text="\U0001f3c3 \u0410\u043d\u0438\u043c\u0430\u0446\u0438\u044f", callback_data="adm_gen_section:anim")],
            [InlineKeyboardButton(text="\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434", callback_data="adm_prices")],
        ]))
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_gen_section:"))
async def adm_gen_section(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    section = cb.data.split(":")[1]
    models = {"image": IMAGE_MODELS, "video": VIDEO_MODELS, "edit": EDIT_MODELS, "anim": ANIM_MODELS}.get(section, {})
    rows = [[InlineKeyboardButton(text=f"\u270f\ufe0f {m['name']} \u2014 {m.get('credits',0)} \u043a\u0440", callback_data=f"adm_edit_gen:{key}:{section}")] for key, m in models.items()]
    rows.append([InlineKeyboardButton(text="\u2b05\ufe0f \u041d\u0430\u0437\u0430\u0434", callback_data="adm_prices_gen")])
    snames = {"image": "\u0424\u043e\u0442\u043e", "video": "\u0412\u0438\u0434\u0435\u043e", "edit": "\u0420\u0435\u0434\u0430\u043a\u0442\u0438\u0440\u043e\u0432\u0430\u043d\u0438\u0435", "anim": "\u0410\u043d\u0438\u043c\u0430\u0446\u0438\u044f"}
    await cb.message.edit_text(f"\U0001f3a8 <b>{snames.get(section, section)}</b>", parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@dp.callback_query(F.data.startswith("adm_edit_gen:"))
async def adm_edit_gen(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID: return
    parts = cb.data.split(":")
    key, section = parts[1], parts[2]
    m = {"image": IMAGE_MODELS, "video": VIDEO_MODELS, "edit": EDIT_MODELS, "anim": ANIM_MODELS}.get(section, {}).get(key, {})
    await state.update_data(edit_gen_key=key, edit_gen_section=section, edit_pack_field="gen_credits")
    await state.set_state(AdminEditState.waiting_value)
    await cb.message.edit_text(
        f"\u270f\ufe0f <b>{m.get('name', key)}</b>\n\n\u0422\u0435\u043a\u0443\u0449\u0430\u044f \u0446\u0435\u043d\u0430: <b>{m.get('credits', 0)} \u043a\u0440</b>\n\n\u0412\u0432\u0435\u0434\u0438 \u043d\u043e\u0432\u043e\u0435 \u043a\u043e\u043b\u0438\u0447\u0435\u0441\u0442\u0432\u043e \u043a\u0440\u0435\u0434\u0438\u0442\u043e\u0432:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="\u274c \u041e\u0442\u043c\u0435\u043d\u0430", callback_data=f"adm_gen_section:{section}")]]))
    await cb.answer()


@dp.message(AdminEditState.waiting_value)
async def adm_edit_value(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    data = await state.get_data()
    value = message.text.strip()
    pool = await get_pool()
    field = data.get("edit_pack_field", "")
    pack_key = data.get("edit_pack_key")
    shop_key = data.get("edit_shop_key")
    shop_field = data.get("edit_shop_field", "")
    shop_plan = data.get("edit_shop_plan")
    gen_key = data.get("edit_gen_key")
    gen_section = data.get("edit_gen_section")
    try:
        if field in ("price", "credits", "name") and pack_key:
            val = int(value) if field != "name" else value
            CREDIT_PACKS[pack_key][field] = val
            async with pool.acquire() as conn:
                await conn.execute(f"UPDATE bot_credit_packs SET {field}=$1 WHERE key=$2", val, pack_key)
            await state.clear()
            await message.answer(f"\u2705 {CREDIT_PACKS[pack_key]['name']} \u2192 {field} = <code>{val}</code>", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="\U0001f4e6 \u041a \u043f\u0430\u043a\u0435\u0442\u0430\u043c", callback_data="adm_prices_packs")]]))
        elif field == "new_pack":
            parts = [x.strip() for x in value.split("|")]
            if len(parts) < 4: await message.answer("\u274c \u0424\u043e\u0440\u043c\u0430\u0442: \u043a\u043b\u044e\u0447|\u043d\u0430\u0437\u0432\u0430\u043d\u0438\u0435|\u043a\u0440\u0435\u0434\u0438\u0442\u044b|\u0446\u0435\u043d\u0430"); return
            nk, nm, nc, np_ = parts[0], parts[1], int(parts[2]), int(parts[3])
            CREDIT_PACKS[nk] = {"name": nm, "credits": nc, "price": np_, "stars": 0, "desc": "", "badge": ""}
            async with pool.acquire() as conn:
                await conn.execute("INSERT INTO bot_credit_packs (key,name,credits,price,sort_order) VALUES ($1,$2,$3,$4,$5) ON CONFLICT (key) DO UPDATE SET name=$2,credits=$3,price=$4,enabled=TRUE", nk, nm, nc, np_, len(CREDIT_PACKS))
            await state.clear()
            await message.answer(f"\u2705 \u041f\u0430\u043a\u0435\u0442 <b>{nm}</b> \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d!", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="\U0001f4e6 \u041a \u043f\u0430\u043a\u0435\u0442\u0430\u043c", callback_data="adm_prices_packs")]]))
        elif shop_field in ("price", "name", "desc") and shop_key and shop_plan is not None:
            val = int(value) if shop_field == "price" else value
            SHOP_CATALOG[shop_key]["plans"][shop_plan][shop_field] = val
            col = {"price": "price", "name": "plan_name", "desc": "plan_desc"}[shop_field]
            async with pool.acquire() as conn:
                await conn.execute(f"UPDATE bot_shop_items SET {col}=$1 WHERE key=$2 AND plan_idx=$3", val, shop_key, shop_plan)
            await state.clear()
            await message.answer(f"\u2705 \u041e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u043e!", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="\U0001f6cd \u041c\u0430\u0433\u0430\u0437\u0438\u043d", callback_data="adm_prices_shop")]]))
        elif shop_field in ("svc_name", "svc_emoji", "svc_desc") and shop_key:
            field_map = {"svc_name": "name", "svc_emoji": "emoji", "svc_desc": "desc"}
            col_map   = {"svc_name": "service_name", "svc_emoji": "emoji", "svc_desc": "service_desc"}
            f = field_map[shop_field]
            col = col_map[shop_field]
            if shop_key in SHOP_CATALOG:
                SHOP_CATALOG[shop_key][f] = value
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(f"UPDATE bot_shop_items SET {col}=$1 WHERE key=$2", value, shop_key)
            await state.clear()
            svc = SHOP_CATALOG.get(shop_key, {})
            await message.answer(
                f"\u2705 {svc.get('emoji','')} <b>{svc.get('name', shop_key)}</b> \u2014 \u043e\u0431\u043d\u043e\u0432\u043b\u0435\u043d\u043e!",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="\u2b05\ufe0f \u041a \u0441\u0435\u0440\u0432\u0438\u0441\u0443", callback_data=f"adm_shop_service:{shop_key}")
                ]])
            )
        elif shop_field == "new_service":
            parts = [x.strip() for x in value.split("\n") if x.strip()]
            if len(parts) < 4:
                await message.answer(
                    "❌ Нужно 4 строки:\n\n<code>ключ\nэмодзи\nназвание\nописание</code>",
                    parse_mode="HTML"
                )
                return
            nk, em, nm, desc = parts[0], parts[1], parts[2], parts[3]
            SHOP_CATALOG[nk] = {"name": nm, "emoji": em, "desc": desc, "plans": []}
            # \u0421\u043e\u0445\u0440\u0430\u043d\u044f\u0435\u043c \u0441\u0435\u0440\u0432\u0438\u0441 \u0432 \u0411\u0414 \u043a\u0430\u043a placeholder (plan_idx=-1, \u0447\u0442\u043e\u0431\u044b \u043d\u0435 \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u043b\u0441\u044f \u0432 \u043c\u0430\u0433\u0430\u0437\u0438\u043d\u0435)
            # \u041f\u0440\u0438 \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d\u0438\u0438 \u043f\u0435\u0440\u0432\u043e\u0433\u043e \u0442\u0430\u0440\u0438\u0444\u0430 \u043e\u043d \u043f\u043e\u044f\u0432\u0438\u0442\u0441\u044f \u043d\u043e\u0440\u043c\u0430\u043b\u044c\u043d\u043e
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO bot_shop_items
                    (key, plan_idx, service_name, emoji, service_desc, plan_name, price, plan_desc, enabled)
                    VALUES ($1, -1, $2, $3, $4, '', 0, '', FALSE)
                    ON CONFLICT (key, plan_idx) DO UPDATE
                    SET service_name=$2, emoji=$3, service_desc=$4
                """, nk, nm, em, desc)
            await state.clear()
            await message.answer(f"\u2705 \u0421\u0435\u0440\u0432\u0438\u0441 <b>{em} {nm}</b> \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d!\n\n\u0422\u0435\u043f\u0435\u0440\u044c \u0434\u043e\u0431\u0430\u0432\u044c \u0442\u0430\u0440\u0438\u0444\u044b \u0447\u0435\u0440\u0435\u0437 \u0440\u0435\u0434\u0430\u043a\u0442\u043e\u0440.", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="\ud83d\udecd \u041c\u0430\u0433\u0430\u0437\u0438\u043d", callback_data="adm_prices_shop")]]))
        elif shop_field == "new_plan" and shop_key:
            parts = [x.strip() for x in value.split("\n") if x.strip()]
            if len(parts) < 3:
                await message.answer(
                    "❌ Нужно 3 строки:\n\n<code>название\nцена (число)\nописание</code>",
                    parse_mode="HTML"
                )
                return
            pn, pr, pd = parts[0], int(parts[1]), parts[2]
            s = SHOP_CATALOG.get(shop_key, {})
            ni = len(s.get("plans", []))
            s.setdefault("plans", []).append({"name": pn, "price": pr, "stars": 0, "desc": pd})
            async with pool.acquire() as conn:
                await conn.execute("INSERT INTO bot_shop_items (key,plan_idx,service_name,emoji,service_desc,plan_name,price,plan_desc) VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (key,plan_idx) DO UPDATE SET plan_name=$6,price=$7,plan_desc=$8,enabled=TRUE",
                    shop_key, ni, s["name"], s.get("emoji",""), s.get("desc",""), pn, pr, pd)
            await state.clear()
            await message.answer(f"\u2705 \u0422\u0430\u0440\u0438\u0444 <b>{pn}</b> \u0434\u043e\u0431\u0430\u0432\u043b\u0435\u043d!", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="\U0001f6cd \u041c\u0430\u0433\u0430\u0437\u0438\u043d", callback_data="adm_prices_shop")]]))
        elif field == "gen_credits" and gen_key:
            nc = int(value)
            models = {"image": IMAGE_MODELS, "video": VIDEO_MODELS, "edit": EDIT_MODELS, "anim": ANIM_MODELS}.get(gen_section, {})
            if gen_key in models: models[gen_key]["credits"] = nc
            async with pool.acquire() as conn:
                await conn.execute("INSERT INTO bot_gen_prices (model_key,section,credits) VALUES ($1,$2,$3) ON CONFLICT (model_key) DO UPDATE SET credits=$3", gen_key, gen_section, nc)
            mn = models.get(gen_key, {}).get("name", gen_key)
            await state.clear()
            await message.answer(f"\u2705 <b>{mn}</b> \u2192 <b>{nc} \u043a\u0440</b>", parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="\U0001f3a8 \u041a \u0433\u0435\u043d\u0435\u0440\u0430\u0446\u0438\u044f\u043c", callback_data="adm_prices_gen")]]))
        else:
            await message.answer("\u274c \u041d\u0435\u0438\u0437\u0432\u0435\u0441\u0442\u043d\u043e\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435.")
            await state.clear()
    except ValueError:
        await message.answer("\u274c \u0412\u0432\u0435\u0434\u0438 \u0447\u0438\u0441\u043b\u043e.")
    except Exception as e:
        logging.error(f"adm_edit_value: {e}")
        await message.answer(f"\u274c \u041e\u0448\u0438\u0431\u043a\u0430: {e}")
        await state.clear()



# ══════════════════════════════════════════════════════════
#  УПРАВЛЕНИЕ МОДЕЛЯМИ (вкл/выкл) — /admin → 🤖 Управление моделями
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "adm_models")
async def adm_models(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    rows = [
        [InlineKeyboardButton(text="📷 Фото-модели",        callback_data="adm_models_sec:image")],
        [InlineKeyboardButton(text="🎬 Видео-модели",       callback_data="adm_models_sec:video")],
        [InlineKeyboardButton(text="🖌 Редактирование",     callback_data="adm_models_sec:edit")],
        [InlineKeyboardButton(text="🏃 Анимация",           callback_data="adm_models_sec:anim")],
        [InlineKeyboardButton(text="⬅️ Назад",              callback_data="adm_back")],
    ]
    total_off = len(DISABLED_MODELS)
    text = (
        f"🤖 <b>Управление моделями</b>\n\n"
        f"Отключено сейчас: <b>{total_off}</b>\n\n"
        f"Выбери раздел чтобы включить или выключить модели.\n"
        f"Отключённые модели не видны пользователям."
    )
    await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()

@dp.callback_query(F.data.startswith("adm_models_sec:"))
async def adm_models_sec(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    section = cb.data.split(":")[1]
    models = _all_models_map().get(section, {})
    rows = []
    for key, m in models.items():
        status = "❌" if key in DISABLED_MODELS else "✅"
        rows.append([InlineKeyboardButton(
            text=f"{status} {m['name']} — {m.get('credits', 0)} кр",
            callback_data=f"adm_toggle_model:{key}:{section}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_models")])
    label = _section_label(section)
    off_count = sum(1 for k in models if k in DISABLED_MODELS)
    await cb.message.edit_text(
        f"🤖 <b>{label}</b>\n\n"
        f"✅ — включена  |  ❌ — выключена\n"
        f"Отключено: {off_count} из {len(models)}",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("adm_toggle_model:"))
async def adm_toggle_model(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    parts = cb.data.split(":")
    key, section = parts[1], parts[2]
    models = _all_models_map().get(section, {})
    if key not in models:
        await cb.answer("Модель не найдена", show_alert=True)
        return

    # Переключаем
    new_enabled = key in DISABLED_MODELS  # если сейчас выключена — включаем
    if new_enabled:
        DISABLED_MODELS.discard(key)
    else:
        DISABLED_MODELS.add(key)

    # Сохраняем в БД
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO bot_gen_prices (model_key, section, credits, enabled) "
            "VALUES ($1, $2, $3, $4) ON CONFLICT (model_key) DO UPDATE SET enabled=$4",
            key, section, models[key].get("credits", 10), new_enabled
        )

    m_name = models[key]["name"]
    status_text = "включена ✅" if new_enabled else "выключена ❌"
    await cb.answer(f"{m_name} {status_text}", show_alert=False)

    # Обновляем список
    rows = []
    for k, m in models.items():
        status = "❌" if k in DISABLED_MODELS else "✅"
        rows.append([InlineKeyboardButton(
            text=f"{status} {m['name']} — {m.get('credits', 0)} кр",
            callback_data=f"adm_toggle_model:{k}:{section}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_models")])
    label = _section_label(section)
    off_count = sum(1 for k in models if k in DISABLED_MODELS)
    try:
        await cb.message.edit_text(
            f"🤖 <b>{label}</b>\n\n"
            f"✅ — включена  |  ❌ — выключена\n"
            f"Отключено: {off_count} из {len(models)}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
#  СТАТИСТИКА ПРОДАЖ МАГАЗИНА — /admin → 🛍 Продажи магазина
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "adm_shop_sales")
async def adm_shop_sales(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    rows = [
        [InlineKeyboardButton(text="📅 Сегодня",   callback_data="adm_shop_sales_p:day")],
        [InlineKeyboardButton(text="📆 Неделя",    callback_data="adm_shop_sales_p:week")],
        [InlineKeyboardButton(text="🗓 Месяц",     callback_data="adm_shop_sales_p:month")],
        [InlineKeyboardButton(text="⬅️ Назад",     callback_data="adm_back")],
    ]
    await cb.message.edit_text(
        "🛍 <b>Продажи магазина</b>\n\nВыбери период:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )
    await cb.answer()

@dp.callback_query(F.data.startswith("adm_shop_sales_p:"))
async def adm_shop_sales_period(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    period = cb.data.split(":")[1]

    interval_sql = {"day": "CURRENT_DATE", "week": "NOW() - INTERVAL '7 days'", "month": "NOW() - INTERVAL '30 days'"}
    period_label = {"day": "сегодня", "week": "7 дней", "month": "30 дней"}
    since = interval_sql.get(period, "CURRENT_DATE")

    pool = await get_pool()
    async with pool.acquire() as conn:
        rows_db = await conn.fetch(
            f"SELECT pack, amount_rub, paid_at FROM fk_orders "
            f"WHERE order_id LIKE 'shop_%' AND status='paid' AND paid_at >= {since} "
            f"ORDER BY paid_at DESC"
        )

    total_orders = len(rows_db)
    total_revenue = sum(r["amount_rub"] for r in rows_db)

    # Разбивка по сервисам
    by_service: dict = {}
    for r in rows_db:
        pack = r["pack"] or ""
        # pack формат: "shop:KEY:plan_idx"
        parts = pack.split(":")
        if len(parts) >= 2:
            svc_key = parts[1]
            svc = SHOP_CATALOG.get(svc_key, {})
            svc_name = f"{svc.get('emoji', '')} {svc.get('name', svc_key)}".strip()
        else:
            svc_name = pack or "Неизвестно"
        if svc_name not in by_service:
            by_service[svc_name] = {"count": 0, "revenue": 0}
        by_service[svc_name]["count"] += 1
        by_service[svc_name]["revenue"] += r["amount_rub"]

    # Сортируем по выручке
    sorted_svc = sorted(by_service.items(), key=lambda x: x[1]["revenue"], reverse=True)
    breakdown = "\n".join(
        f"  • {name}: <b>{d['count']} шт</b> — <b>{d['revenue']}₽</b>"
        for name, d in sorted_svc
    ) or "  нет продаж"

    label = period_label.get(period, period)
    text = (
        f"🛍 <b>Продажи магазина за {label}</b>\n\n"
        f"📦 Заказов: <b>{total_orders}</b>\n"
        f"💰 Выручка: <b>{total_revenue}₽</b>\n\n"
        f"<b>По сервисам:</b>\n{breakdown}"
    )
    rows = [
        [InlineKeyboardButton(text="📅 Сегодня",  callback_data="adm_shop_sales_p:day"),
         InlineKeyboardButton(text="📆 Неделя",   callback_data="adm_shop_sales_p:week"),
         InlineKeyboardButton(text="🗓 Месяц",    callback_data="adm_shop_sales_p:month")],
        [InlineKeyboardButton(text="⬅️ Назад",    callback_data="adm_shop_sales")],
    ]
    try:
        await cb.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except Exception:
        await cb.message.answer(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    await cb.answer()


@dp.callback_query(F.data == "adm_promos")
async def adm_promos(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    promos = await list_promos(only_active=True, limit=30)
    text = f"🎟 <b>Промокоды (активные)</b>\n\n"
    if not promos:
        text += "<i>Пока нет активных промокодов</i>"
    else:
        for p in promos[:15]:
            kind_label = f"-{p['value']}%" if p['kind'] == 'percent' else f"+{p['value']} кр"
            uses = f"{p['used_count']}/{p['max_uses']}" if p['max_uses'] else f"{p['used_count']}/∞"
            exp = ""
            if p.get('expires_at'):
                exp = f" · до {p['expires_at'].strftime('%d.%m')}"
            text += f"<code>{p['code']}</code> {kind_label} · {uses}{exp}\n"
        if len(promos) > 15:
            text += f"\n...и ещё {len(promos)-15}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать", callback_data="adm_promo_create")],
        [InlineKeyboardButton(text="❌ Деактивировать", callback_data="adm_promo_deactivate")],
        [InlineKeyboardButton(text="📋 Показать все (включая неактивные)", callback_data="adm_promo_all")],
        [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "adm_promo_all")
async def adm_promo_all(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    promos = await list_promos(only_active=False, limit=50)
    text = f"🎟 <b>Все промокоды</b>\n\n"
    if not promos:
        text += "<i>Пока нет промокодов</i>"
    else:
        for p in promos[:25]:
            kind_label = f"-{p['value']}%" if p['kind'] == 'percent' else f"+{p['value']} кр"
            uses = f"{p['used_count']}/{p['max_uses']}" if p['max_uses'] else f"{p['used_count']}/∞"
            mark = "" if p['active'] else " ⛔"
            text += f"<code>{p['code']}</code> {kind_label} · {uses}{mark}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К промокодам", callback_data="adm_promos")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "adm_promo_create")
async def adm_promo_create_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    await state.set_state(AdmPromoState.waiting_code)
    await cb.message.answer(
        "🎟 <b>Создание промокода</b>\n\n"
        "Введи название кода (латиница, цифры, _ и -):\n"
        "<i>Примеры: NEWYEAR25, BLOGER10, HELLO50</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_promos")],
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdmPromoState.waiting_code, F.text)
async def adm_promo_code_handler(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    code = (message.text or "").strip().upper()

    if data.get("_deact"):
        # Деактивация
        ok = await deactivate_promo(code)
        await state.clear()
        if ok:
            await message.answer(
                f"✅ Промокод <code>{code}</code> деактивирован",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="◀️ К промокодам", callback_data="adm_promos")],
                ]),
                parse_mode="HTML"
            )
        else:
            await message.answer(
                f"❌ Код <code>{code}</code> не найден",
                parse_mode="HTML"
            )
        return

    # Создание - валидация кода
    if not code or not code.replace("_", "").replace("-", "").isalnum():
        await message.answer("❌ Код должен содержать только буквы, цифры, _ и -")
        return
    await state.update_data(code=code)
    await state.set_state(AdmPromoState.waiting_kind)
    await message.answer(
        f"Код: <code>{code}</code>\n\n"
        "Выбери тип:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💸 Скидка (%)", callback_data="admp_kind:percent")],
            [InlineKeyboardButton(text="💎 Бонусные кредиты", callback_data="admp_kind:credits")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_promos")],
        ]),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("admp_kind:"))
async def adm_promo_kind(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    kind = cb.data.split(":")[1]
    await state.update_data(kind=kind)
    await state.set_state(AdmPromoState.waiting_value)
    if kind == "percent":
        prompt = "Введи размер скидки в % (1-99):\n<i>Пример: 20</i>"
    else:
        prompt = "Введи количество кредитов:\n<i>Пример: 50</i>"
    await cb.message.answer(
        prompt,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_promos")],
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdmPromoState.waiting_value)
async def adm_promo_value(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        value = int((message.text or "").strip())
    except ValueError:
        await message.answer("❌ Введи число")
        return
    data = await state.get_data()
    if data["kind"] == "percent" and not (1 <= value <= 99):
        await message.answer("❌ Процент от 1 до 99")
        return
    if data["kind"] == "credits" and value < 1:
        await message.answer("❌ Кредиты должны быть больше 0")
        return
    await state.update_data(value=value)
    await state.set_state(AdmPromoState.waiting_uses)
    await message.answer(
        "Тип использования:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔂 Одноразовый (1 раз)", callback_data="admp_uses:1")],
            [InlineKeyboardButton(text="🔁 Многоразовый (100)", callback_data="admp_uses:100")],
            [InlineKeyboardButton(text="🔁 Многоразовый (1000)", callback_data="admp_uses:1000")],
            [InlineKeyboardButton(text="♾ Без лимита", callback_data="admp_uses:0")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_promos")],
        ])
    )


@dp.callback_query(F.data.startswith("admp_uses:"))
async def adm_promo_uses(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    uses = int(cb.data.split(":")[1])
    await state.update_data(uses=uses)
    await state.set_state(AdmPromoState.waiting_days)
    await cb.message.answer(
        "Срок действия:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="7 дней",  callback_data="admp_days:7"),
             InlineKeyboardButton(text="14 дней", callback_data="admp_days:14")],
            [InlineKeyboardButton(text="30 дней", callback_data="admp_days:30"),
             InlineKeyboardButton(text="90 дней", callback_data="admp_days:90")],
            [InlineKeyboardButton(text="♾ Бессрочно", callback_data="admp_days:0")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_promos")],
        ])
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("admp_days:"))
async def adm_promo_days(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    days = int(cb.data.split(":")[1])
    data = await state.get_data()
    ok, msg = await create_promo(
        code=data["code"],
        kind=data["kind"],
        value=data["value"],
        max_uses=data["uses"],
        days_valid=days,
    )
    await state.clear()
    if ok:
        kind_label = f"-{data['value']}%" if data['kind'] == 'percent' else f"+{data['value']} кредитов"
        uses_label = f"{data['uses']} раз" if data['uses'] else "без лимита"
        days_label = f"{days} дней" if days else "бессрочно"
        await cb.message.answer(
            f"✅ <b>Промокод создан!</b>\n\n"
            f"<code>{data['code']}</code>\n"
            f"Тип: <b>{kind_label}</b>\n"
            f"Использований: <b>{uses_label}</b>\n"
            f"Срок: <b>{days_label}</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ К промокодам", callback_data="adm_promos")],
            ]),
            parse_mode="HTML"
        )
    else:
        await cb.message.answer(f"❌ {msg}")
    await cb.answer()


@dp.callback_query(F.data == "adm_promo_deactivate")
async def adm_promo_deact_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    await state.set_state(AdmPromoState.waiting_code)
    await state.update_data(_deact=True)
    await cb.message.answer(
        "❌ <b>Деактивация промокода</b>\n\n"
        "Введи код который хочешь отключить:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_promos")],
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


# Переопределяем обработчик waiting_code чтобы учитывать _deact - УЖЕ ВЫШЕ


# ══════════════════════════════════════════════════════════
#  РЕДАКТИРОВАНИЕ ФОТО ПО РЕФЕРЕНСУ
# ══════════════════════════════════════════════════════════



# ══════════════════════════════════════════════════════════
#  💰 ПРИБЫЛЬ / СЕБЕСТОИМОСТЬ
# ══════════════════════════════════════════════════════════

async def _cost_usd_rate() -> float:
    """Курс доллара для закупа (₽/$), задаётся админом."""
    try:
        return float(await get_setting("cost_usd_rate", "90") or "90")
    except Exception:
        return 90.0


async def _plan_cost_usd(key: str, plan_idx: int) -> float:
    """Цена закупа тарифа в долларах (0 = не задана)."""
    try:
        return float(await get_setting(f"cost_usd:{key}:{plan_idx}", "0") or "0")
    except Exception:
        return 0.0


async def _plan_cost(key: str, plan_idx: int) -> int:
    """Себестоимость тарифа в ₽ = закуп$ × курс закупа. 0 = не задана."""
    usd = await _plan_cost_usd(key, plan_idx)
    if usd <= 0:
        return 0
    return round(usd * await _cost_usd_rate())


async def _build_profit_text(since_sql: str, label: str) -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        shop_rows = await conn.fetch(
            f"SELECT pack, COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS rev "
            f"FROM fk_orders WHERE status='paid' AND pack LIKE 'shop:%' AND paid_at >= {since_sql} "
            f"GROUP BY pack"
        )
        nsg_row = await conn.fetchrow(
            f"SELECT COUNT(*) AS cnt, COALESCE(SUM(price_rub),0) AS rev, COALESCE(SUM(price_usd),0) AS usd "
            f"FROM nsgifts_orders WHERE status='fulfilled' AND created_at >= {since_sql}"
        )
        cr_row = await conn.fetchrow(
            f"SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS rev FROM fk_orders "
            f"WHERE status='paid' AND paid_at >= {since_sql} "
            f"AND (pack IS NULL OR (pack NOT LIKE 'shop:%' AND pack NOT LIKE 'nsg:%'))"
        )

    by_svc: dict = {}
    total_rev = 0
    total_cost = 0
    for r in shop_rows:
        parts = (r["pack"] or "").split(":")
        key = parts[1] if len(parts) > 1 else ""
        idx = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
        svc = SHOP_CATALOG.get(key, {})
        _pref = tg_emoji({**svc, "_key": key})
        nm = f"{_pref} {svc.get('name', key)}".strip()
        unit = await _plan_cost(key, idx)
        cost = unit * r["cnt"]
        d = by_svc.setdefault(nm, {"cnt": 0, "rev": 0, "cost": 0, "missing": False})
        d["cnt"] += r["cnt"]
        d["rev"] += int(r["rev"])
        d["cost"] += cost
        if unit == 0:
            d["missing"] = True
        total_rev += int(r["rev"])
        total_cost += cost

    lines = [f"💰 <b>Прибыль: {label}</b>\n"]
    for nm, d in sorted(by_svc.items(), key=lambda x: -x[1]["rev"]):
        prof = d["rev"] - d["cost"]
        warn = " ⚠️ себест. не задана" if d["missing"] else ""
        lines.append(f"\n<b>{nm}</b>: {d['cnt']} шт\n  {d['rev']}₽ − {d['cost']}₽ = <b>{prof:+}₽</b>{warn}")

    nsg_cnt = nsg_row["cnt"] or 0
    if nsg_cnt:
        rate = await _nsg_usd_rate()
        nsg_rev = int(nsg_row["rev"] or 0)
        nsg_cost = round(float(nsg_row["usd"] or 0) * rate)
        total_rev += nsg_rev
        total_cost += nsg_cost
        _ap = tg_emoji({"_key": "appstore", "emoji": "🍎"})
        lines.append(f"\n<b>{_ap} App Store (автодоставка)</b>: {nsg_cnt} шт\n  {nsg_rev}₽ − {nsg_cost}₽ = <b>{nsg_rev - nsg_cost:+}₽</b>")

    cr_cnt = cr_row["cnt"] or 0
    cr_rev = int(cr_row["rev"] or 0)
    if cr_cnt:
        total_rev += cr_rev
        lines.append(f"\n<b>💳 Кредиты</b>: {cr_cnt} шт · {cr_rev}₽\n  <i>(себестоимость AI не учитывается)</i>")

    profit = total_rev - total_cost
    margin = round(profit / total_rev * 100) if total_rev else 0
    _rate = await _cost_usd_rate()
    _rev_usd = total_rev / _rate if _rate else 0
    _cost_usd = total_cost / _rate if _rate else 0
    _prof_usd = profit / _rate if _rate else 0
    lines.append(
        f"\n━━━━━━━━━━━━━\n"
        f"💵 Выручка: <b>{total_rev}₽</b> (≈ ${_rev_usd:,.0f})\n"
        f"📉 Затраты: <b>{total_cost}₽</b> (≈ ${_cost_usd:,.0f})\n"
        f"💰 <b>Прибыль: {profit:+}₽</b> (≈ ${_prof_usd:+,.0f}) · маржа {margin}%\n"
        f"<i>Курс конвертации: {_rate:.0f} ₽/$</i>"
    )
    return "\n".join(lines)


def _kb_profit(period: str = ""):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сегодня", callback_data="adm_profit_p:day"),
         InlineKeyboardButton(text="7 дней", callback_data="adm_profit_p:week"),
         InlineKeyboardButton(text="30 дней", callback_data="adm_profit_p:month")],
        [InlineKeyboardButton(text="⚙️ Задать себестоимость", callback_data="adm_profit_costs")],
        [InlineKeyboardButton(text="⬅️ Назад в админку", callback_data="adm_back")],
    ])


@dp.callback_query(F.data == "adm_profit")
async def adm_profit_menu(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await cb.answer()
    text = (
        "💰 <b>Прибыль</b>\n\n"
        "Расчёт: выручка − себестоимость за период.\n"
        "Выбери период 👇\n\n"
        "<i>Себестоимость подписок задаётся кнопкой ниже по каждому тарифу. "
        "App Store считается автоматически из закупки.</i>"
    )
    try:
        await cb.message.edit_text(text, reply_markup=_kb_profit(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=_kb_profit(), parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_profit_p:"))
async def adm_profit_period(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await cb.answer()
    period = cb.data.split(":")[1]
    since = {"day": "CURRENT_DATE", "week": "NOW() - INTERVAL '7 days'",
             "month": "NOW() - INTERVAL '30 days'"}.get(period, "CURRENT_DATE")
    label = {"day": "сегодня", "week": "7 дней", "month": "30 дней"}.get(period, "период")
    text = await _build_profit_text(since, label)
    try:
        await cb.message.edit_text(text, reply_markup=_kb_profit(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=_kb_profit(), parse_mode="HTML")


async def _profit_costs_view(note: str = ""):
    rate = await _cost_usd_rate()
    rows = [[InlineKeyboardButton(text=f"💱 Курс закупа: {rate:.0f} ₽/$ — изменить", callback_data="adm_pcost_rate")]]
    for key, s in SHOP_CATALOG.items():
        if not s.get("plans"):
            continue
        _eid = _btn_emoji_id(key, s)
        rows.append([InlineKeyboardButton(
            text=(s.get("name", key) if _eid else f"{s.get('emoji','')} {s.get('name', key)}".strip()),
            callback_data=f"adm_pcost_svc:{key}",
            **({"icon_custom_emoji_id": _eid} if _eid else {})
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_profit")])
    head = (note + "\n\n") if note else ""
    text = (head +
            "⚙️ <b>Себестоимость тарифов</b>\n\n"
            "1. Задай <b>курс закупа доллара</b>.\n"
            "2. По каждому тарифу укажи <b>цену закупа в $</b> — рубли посчитаются сами.")
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def _pcost_svc_view(key: str, note: str = ""):
    s = SHOP_CATALOG.get(key, {})
    rate = await _cost_usd_rate()
    rows = []
    for i, p in enumerate(s.get("plans", [])):
        usd = await _plan_cost_usd(key, i)
        c_txt = f"${usd:g} (≈{round(usd*rate)}₽)" if usd > 0 else "не задана"
        rows.append([InlineKeyboardButton(
            text=f"{p.get('name','')} · цена {p.get('price',0)}₽ · закуп: {c_txt}",
            callback_data=f"adm_pcost_set:{key}:{i}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_profit_costs")])
    head = (note + "\n\n") if note else ""
    text = head + f"⚙️ <b>{s.get('name', key)}</b>\n\nВыбери тариф, чтобы задать цену закупа в $:"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@dp.callback_query(F.data == "adm_profit_costs")
async def adm_profit_costs(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    text, kb = await _profit_costs_view()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_pcost_svc:"))
async def adm_profit_cost_svc(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    key = cb.data.split(":")[1]
    text, kb = await _pcost_svc_view(key)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data.startswith("adm_pcost_set:"))
async def adm_profit_cost_set_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    parts = cb.data.split(":")
    key, idx = parts[1], int(parts[2])
    s = SHOP_CATALOG.get(key, {})
    plans = s.get("plans", [])
    p = plans[idx] if idx < len(plans) else {}
    await state.set_state(AdminState.waiting_plan_cost)
    await state.update_data(pcost_key=key, pcost_idx=idx,
                           panel_chat=cb.message.chat.id, panel_mid=cb.message.message_id)
    cur = await _plan_cost_usd(key, idx)
    rate = await _cost_usd_rate()
    text = (
        f"💵 Цена закупа в <b>долларах</b> для <b>{s.get('name', key)} {p.get('name','')}</b>\n"
        f"Цена клиенту: <b>{p.get('price',0)}₽</b>\n"
        f"Курс закупа: <b>{rate:.0f} ₽/$</b>\n"
        f"Текущий закуп: <b>${cur:g}</b>\n\n"
        f"Введи цену закупа в долларах (например 20 или 19.99):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"adm_pcost_svc:{key}")]
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.message(AdminState.waiting_plan_cost, F.text)
async def adm_profit_cost_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        val = float(message.text.strip().replace(",", "."))
        assert val >= 0
    except Exception:
        await message.answer("❌ Введи число ≥ 0 (в долларах):")
        return
    data = await state.get_data()
    key = data.get("pcost_key")
    idx = data.get("pcost_idx")
    pchat = data.get("panel_chat")
    pmid = data.get("panel_mid")
    await state.clear()
    if key is None or idx is None:
        await message.answer("⚠️ Сессия истекла, открой раздел заново.")
        return
    await set_setting(f"cost_usd:{key}:{idx}", str(val))
    rate = await _cost_usd_rate()
    rub = round(val * rate)
    s = SHOP_CATALOG.get(key, {})
    plans = s.get("plans", [])
    p = plans[idx] if idx < len(plans) else {}
    price = p.get("price", 0)
    prof = price - rub
    note = (f"✅ {p.get('name','')}: закуп ${val:g} × {rate:.0f}₽ = {rub}₽, "
            f"прибыль {prof:+}₽")
    try:
        await message.delete()
    except Exception:
        pass
    text, kb = await _pcost_svc_view(key, note=note)
    if pchat and pmid:
        try:
            await bot.edit_message_text(text, chat_id=pchat, message_id=pmid,
                                        reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_pcost_rate")
async def adm_profit_rate_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_cost_rate)
    await state.update_data(panel_chat=cb.message.chat.id, panel_mid=cb.message.message_id)
    rate = await _cost_usd_rate()
    text = (
        f"💱 Текущий курс закупа доллара: <b>{rate:.0f} ₽/$</b>\n\n"
        f"Введи новый курс (например 92 или 90.5):"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_profit_costs")]
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.message(AdminState.waiting_cost_rate, F.text)
async def adm_profit_rate_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        val = float(message.text.strip().replace(",", "."))
        assert 1 <= val <= 1000
    except Exception:
        await message.answer("❌ Введи курс — число от 1 до 1000:")
        return
    data = await state.get_data()
    pchat = data.get("panel_chat")
    pmid = data.get("panel_mid")
    await set_setting("cost_usd_rate", str(val))
    await state.clear()
    note = f"✅ Курс закупа: {val:.0f} ₽/$"
    try:
        await message.delete()
    except Exception:
        pass
    text, kb = await _profit_costs_view(note=note)
    if pchat and pmid:
        try:
            await bot.edit_message_text(text, chat_id=pchat, message_id=pmid,
                                        reply_markup=kb, parse_mode="HTML")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ══════════════════════════════════════════════════════════
#  🤝 ПРЕМИУМ-РЕФЕРАЛКА
# ══════════════════════════════════════════════════════════

async def _refprem_menu():
    """Текст + клавиатура меню премиум-рефералки."""
    try:
        pct = float(await get_setting("ref_premium_pct", "10") or "10")
    except Exception:
        pct = 10.0
    try:
        cap = float(await get_setting("ref_premium_cap", "0") or "0")
    except Exception:
        cap = 0.0
    partners = await list_ref_premium()

    cap_txt = f"{cap:.0f}₽ / мес" if cap > 0 else "без лимита"
    lines = [
        "🤝 <b>Премиум-рефералка</b>\n",
        f"Глобальный %: <b>{pct:.0f}%</b>",
        f"Месячный лимит: <b>{cap_txt}</b>\n",
        "Партнёр получает % монетками с <b>каждой</b> оплаты своих рефералов.",
    ]
    rows = [
        [InlineKeyboardButton(text=f"✏️ Глобальный % ({pct:.0f}%)", callback_data="adm_refp_pct"),
         InlineKeyboardButton(text="✏️ Лимит/мес", callback_data="adm_refp_cap")],
        [InlineKeyboardButton(text="➕ Добавить партнёра", callback_data="adm_refp_add")],
    ]
    if partners:
        rows.append([InlineKeyboardButton(text="❌ Убрать партнёра", callback_data="adm_refp_delask")])

    if partners:
        lines.append("\n<b>Партнёры:</b>")
        for pr in partners:
            uid = pr["user_id"]
            uname = (pr.get("username") or "").lstrip("@")
            ppct = pr.get("ref_premium_pct")
            ppct_txt = f"{float(ppct):.0f}%" if ppct is not None else f"{pct:.0f}% (глоб.)"
            earned = await premium_ref_earned_this_month(uid)
            refs = pr.get("refs", 0)
            tag = f"@{uname} (<code>{uid}</code>)" if uname else f"<code>{uid}</code>"
            lines.append(
                f"\n• {tag} — <b>{ppct_txt}</b> · рефералов: {refs} · "
                f"за месяц: {earned:.0f}₽"
            )
    else:
        lines.append("\n<i>Партнёров пока нет.</i>")

    rows.append([InlineKeyboardButton(text="⬅️ Назад в админку", callback_data="adm_back")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _refprem_show(cb):
    text, kb = await _refprem_menu()
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "adm_refprem")
async def adm_refprem_menu(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.clear()
    await cb.answer()
    await _refprem_show(cb)


@dp.callback_query(F.data == "adm_refp_pct")
async def adm_refp_pct_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_refp_pct)
    await cb.message.answer(
        "Введи <b>глобальный %</b> премиум-рефералки (например 10 или 7.5):",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_refp_pct, F.text)
async def adm_refp_pct_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        val = float(message.text.strip().replace(",", "."))
        assert 0 <= val <= 100
    except Exception:
        await message.answer("❌ Введи число от 0 до 100:")
        return
    await set_setting("ref_premium_pct", str(val))
    await state.clear()
    await message.answer(f"✅ Глобальный % премиум-рефералки: <b>{val:.0f}%</b>", parse_mode="HTML")


@dp.callback_query(F.data == "adm_refp_cap")
async def adm_refp_cap_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_refp_cap)
    await cb.message.answer(
        "Введи <b>месячный лимит</b> начислений в ₽ на одного партнёра.\n"
        "0 = без лимита:",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_refp_cap, F.text)
async def adm_refp_cap_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        val = float(message.text.strip().replace(",", "."))
        assert val >= 0
    except Exception:
        await message.answer("❌ Введи число ≥ 0:")
        return
    await set_setting("ref_premium_cap", str(val))
    await state.clear()
    cap_txt = f"{val:.0f}₽ / мес" if val > 0 else "без лимита"
    await message.answer(f"✅ Месячный лимит: <b>{cap_txt}</b>", parse_mode="HTML")


@dp.callback_query(F.data == "adm_refp_add")
async def adm_refp_add_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_refp_add)
    await cb.message.answer(
        "Введи <b>ID пользователя</b>, которому дать премиум-рефералку.\n\n"
        "Можно с индивидуальным %: <code>123456789 15</code>\n"
        "Без % — будет использоваться глобальный.",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_refp_add, F.text)
async def adm_refp_add_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    parts = message.text.strip().split()
    try:
        uid = int(parts[0])
    except Exception:
        await message.answer("❌ Неверный ID. Пример: <code>123456789</code> или <code>123456789 15</code>", parse_mode="HTML")
        return
    pct = None
    if len(parts) > 1:
        try:
            pct = float(parts[1].replace(",", "."))
            assert 0 <= pct <= 100
        except Exception:
            await message.answer("❌ % должен быть числом 0–100. Пример: <code>123456789 15</code>", parse_mode="HTML")
            return
    u = await get_user(uid)
    if not u:
        await message.answer("⚠️ Пользователь не найден в боте (он должен хотя бы раз запустить бота).")
        return
    await set_ref_premium(uid, True, pct)
    await state.clear()
    pct_txt = f"{pct:.0f}%" if pct is not None else "глобальный %"
    await message.answer(
        f"✅ Премиум-рефералка включена для <code>{uid}</code> ({pct_txt}).",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm_refp_delask")
async def adm_refp_del_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_refp_del)
    await cb.message.answer(
        "Введи <b>ID партнёра</b>, которого убрать из премиум-рефералки:",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_refp_del, F.text)
async def adm_refp_del_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        uid = int(message.text.strip().split()[0])
    except Exception:
        await message.answer("❌ Неверный ID. Введи числовой ID партнёра:")
        return
    rp = await get_ref_premium(uid)
    if not rp or not rp.get("ref_premium"):
        await state.clear()
        await message.answer(f"⚠️ <code>{uid}</code> не в списке премиум-партнёров.", parse_mode="HTML")
        return
    await set_ref_premium(uid, False, None)
    await state.clear()
    await message.answer(
        f"✅ Партнёр <code>{uid}</code> убран из премиум-рефералки.",
        parse_mode="HTML"
    )
