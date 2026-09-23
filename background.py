# -*- coding: utf-8 -*-
# Auto-split module "background" — part of Neirosetkaa-bot (refactored from bot.py).
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
    ADMIN_ID, FAL_API_KEY, REMINDER_TEXTS, _activation_jobs, _claude_job_results, bot,
    user_conversations, user_orig_images,
)
from runtime_state import (
    rt,
)
from db import (
    expire_old_batches, get_pool, log_event, add_coins, activation_hours,
)
from common import (
    _check_one_gpt_code, _nsg_threshold, fk_check_order_status, fk_credit_paid_order, send_reminder,
    gpt_pool_audit, gpt_reconcile_orphans, pool_audit, pool_audit_report, tg_chunks,
    _who_user,
)

async def cleanup_stale_generations_loop():
    """Раз в 5 минут чистит зависшие записи (старше 30 мин).
    Защищает от ситуации когда бот упал в процессе генерации."""
    while True:
        try:
            await asyncio.sleep(300)
            pool = await get_pool()
            async with pool.acquire() as conn:
                result = await conn.execute(
                    "DELETE FROM active_generations WHERE started_at < NOW() - INTERVAL '30 minutes'"
                )
                if "DELETE 0" not in result:
                    logging.info(f"🧹 Cleanup stale active_generations: {result}")
        except Exception as e:
            logging.error(f"cleanup_stale_generations_loop: {e}")


async def auto_recover_lost_videos_loop():
    """Раз в час ищет в events таймауты генерации видео с request_id
    и автоматически пробует их восстановить.
    
    Отправляет найденные видео юзерам + алертит админу что было восстановлено.
    """
    import re
    await asyncio.sleep(600)  # Первый запуск через 10 минут после старта бота
    while True:
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                # Ищем ошибки с Request ID за последние 6 часов, которые ещё не восстанавливались
                events = await conn.fetch("""
                    SELECT id, user_id, data, created_at FROM events
                    WHERE kind = 'error'
                      AND data LIKE '%Request ID:%'
                      AND created_at > NOW() - INTERVAL '6 hours'
                      AND NOT EXISTS (
                          SELECT 1 FROM events e2
                          WHERE e2.user_id = events.user_id
                            AND e2.kind = 'auto_recovered'
                            AND e2.data LIKE '%' || SUBSTRING(events.data FROM 'Request ID: ([a-f0-9-]+)') || '%'
                      )
                    ORDER BY created_at DESC
                    LIMIT 20
                """)

            if not events:
                await asyncio.sleep(3600)  # 1 час до следующей проверки
                continue

            logging.info(f"🔍 Auto-recover: найдено {len(events)} потерянных видео для восстановления")

            recovered_count = 0
            for ev in events:
                try:
                    # Извлекаем request_id из текста
                    match = re.search(r'Request ID:\s*([a-f0-9-]+)', ev["data"] or "")
                    if not match:
                        continue
                    request_id = match.group(1)
                    target_uid = ev["user_id"]

                    # Пробуем восстановить - ищем на endpoint'ах Kling
                    endpoints = [
                        "fal-ai/kling-video/v3/pro/text-to-video",
                        "fal-ai/kling-video/v2.5-turbo/pro/text-to-video",
                        "fal-ai/kling-video/v3/standard/text-to-video",
                    ]
                    if not FAL_API_KEY:
                        break  # Без ключа ничего не сделаем

                    headers = {"Authorization": f"Key {FAL_API_KEY}"}
                    vid_url = None

                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
                        for ep in endpoints:
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
                                            break
                            except Exception:
                                pass

                    if not vid_url:
                        logging.debug(f"Auto-recover: видео {request_id} не найдено на fal.ai (возможно истекло)")
                        continue

                    # Скачиваем
                    vid_bytes = None
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as dl:
                        for attempt in range(3):
                            try:
                                async with dl.get(vid_url) as vr:
                                    if vr.status == 200:
                                        vid_bytes = await vr.read()
                                        if len(vid_bytes) > 10000:
                                            break
                            except Exception:
                                pass
                            await asyncio.sleep(2)

                    if not vid_bytes or len(vid_bytes) < 10000:
                        logging.warning(f"Auto-recover: не скачалось видео {request_id}")
                        continue

                    # Отправляем юзеру
                    size_mb = len(vid_bytes) / 1024 / 1024
                    try:
                        await bot.send_video(
                            chat_id=target_uid,
                            video=BufferedInputFile(vid_bytes, "recovered.mp4"),
                            caption=(
                                f"🎬 <b>Восстановили твоё видео!</b>\n\n"
                                f"Оно генерировалось с задержкой - "
                                f"мы автоматически его нашли и прислали тебе.\n\n"
                                f"Извини за ожидание 🙏"
                            ),
                            parse_mode="HTML",
                            supports_streaming=True,
                        )
                        await log_event(target_uid, "auto_recovered", f"request_id={request_id} size={size_mb:.1f}MB")
                        recovered_count += 1
                        logging.info(f"✅ Auto-recovered video {request_id} for uid={target_uid} ({size_mb:.1f} MB)")
                    except Exception as send_err:
                        logging.error(f"Auto-recover send failed: {send_err}")

                    # Пауза между восстановлениями чтобы не заспамить
                    await asyncio.sleep(3)

                except Exception as rec_err:
                    logging.error(f"Auto-recover item failed: {rec_err}")
                    continue

            if recovered_count > 0:
                # Алерт админу об успешных восстановлениях
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"🔄 <b>Автовосстановление видео</b>\n\n"
                        f"✅ Восстановлено: <b>{recovered_count}</b> видео\n"
                        f"Юзерам уже отправили.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            await asyncio.sleep(3600)  # Следующий проход через час

        except Exception as e:
            logging.error(f"auto_recover_lost_videos_loop: {e}")
            await asyncio.sleep(3600)


# ─── Авто-проверка платежей FreeKassa ────────────────────
async def fk_auto_check_loop():
    """Каждые 5 минут проверяет FK API: ищет оплаченные заказы у которых в нашей БД
    статус всё ещё 'pending'. Это значит webhook не дошёл - зачисляем сами.

    Проверяем заказы за последний час, чтобы охватить случаи когда webhook
    задержался или не пришёл вообще."""
    await asyncio.sleep(120)  # Первый запуск через 2 минуты после старта
    while True:
        try:
            # 1. Получаем pending заказы за последний час из нашей БД
            pool = await get_pool()
            async with pool.acquire() as conn:
                pending_rows = await conn.fetch(
                    "SELECT order_id, user_id, credits, amount_rub, payment_method, promo_code "
                    "FROM fk_orders "
                    "WHERE status = 'pending' "
                    "  AND created_at > NOW() - INTERVAL '24 hours' "
                    "  AND created_at < NOW() - INTERVAL '2 minutes' "
                    "ORDER BY created_at DESC "
                    "LIMIT 50"
                )

            if not pending_rows:
                await asyncio.sleep(300)  # 5 минут до следующей проверки
                continue

            logging.info(f"🔍 FK auto-check: {len(pending_rows)} pending заказов за последний час")

            # 2. Для каждого pending заказа спрашиваем FK API его статус
            recovered = 0
            for row in pending_rows:
                order_id = row["order_id"]
                try:
                    fk_status = await fk_check_order_status(order_id)
                    if fk_status and fk_status.get("status") == "paid":
                        # FK подтвердил оплату - зачисляем
                        payment = {
                            "user_id": row["user_id"],
                            "credits": row["credits"],
                            "amount":  row["amount_rub"],
                            "promo_code": row["promo_code"],
                        }
                        success = await fk_credit_paid_order(order_id, payment, source="auto_check")
                        if success:
                            recovered += 1
                            logging.warning(
                                f"FK auto-check: ВОССТАНОВЛЕН заказ {order_id} "
                                f"user={row['user_id']} amount={row['amount_rub']}₽"
                            )
                except Exception as e:
                    logging.error(f"FK auto-check error for order {order_id}: {e}")

            if recovered > 0:
                logging.warning(f"🚨 FK auto-check: восстановлено {recovered} платежей")

        except Exception as e:
            logging.error(f"FK auto-check loop error: {e}")

        await asyncio.sleep(300)  # 5 минут


async def _memory_cleanup_loop():
    """Каждые 5 минут чистим устаревшие данные из памяти.
    Диалоги старше 30 мин и фото старше 10 мин удаляются."""
    while True:
        try:
            await asyncio.sleep(300)  # 5 минут
            now = _time_module.time()

            # Чат с AI консультантом - 30 минут неактивности
            expired_conv = [uid for uid, v in user_conversations.items()
                            if isinstance(v, dict) and now - v.get("ts", 0) > 1800]
            for uid in expired_conv:
                del user_conversations[uid]

            # Оригинальные фото для редактирования - 10 минут
            expired_img = [uid for uid, v in user_orig_images.items()
                           if isinstance(v, dict) and now - v.get("ts", 0) > 600]
            for uid in expired_img:
                del user_orig_images[uid]

            if expired_conv or expired_img:
                logging.info(f"🧹 Очищено: {len(expired_conv)} диалогов, {len(expired_img)} фото")
        except Exception as e:
            logging.error(f"Ошибка в memory_cleanup: {e}")

# ─── Модели изображений ───────────────────────────────────
async def credit_batches_loop():
    """Раз в час проверяет и списывает истёкшие партии."""
    while True:
        try:
            await asyncio.sleep(3600)
            expired = await expire_old_batches()
            if expired > 0:
                logging.info(f"🕐 Сгорело {expired} кредитов")
        except Exception as e:
            logging.error(f"credit_batches_loop: {e}")


# ─── Напоминания неактивным ────────────────────────────────

async def subscription_reminder_loop():
    import datetime as _dt
    await asyncio.sleep(60)
    while True:
        try:
            pool = await get_pool()
            now = _dt.datetime.now()
            async with pool.acquire() as conn:
                subs_3d = await conn.fetch("""
                    SELECT s.*, u.user_id FROM user_subscriptions s
                    JOIN users u ON u.user_id = s.user_id
                    WHERE s.is_active = TRUE AND s.notified_3d = FALSE
                      AND s.expires_at > NOW() AND s.expires_at < NOW() + INTERVAL '3 days'
                """)
                for s in subs_3d:
                    days_left = (s["expires_at"] - now).days + 1
                    exp = s["expires_at"].strftime("%d.%m.%Y")
                    plan = f" {s['plan_name']}" if s["plan_name"] else ""
                    try:
                        await bot.send_message(
                            s["user_id"],
                            f"⏰ <b>Подписка заканчивается!</b>\n\n"
                            f"📦 <b>{s['service_name']}{plan}</b>\n"
                            f"📅 Действует ещё <b>{days_left} дн.</b> - до {exp}\n\n"
                            f"💡 <b>Оплачивай продление в последний день</b> (когда останется 0–1 дн.).\n"
                            f"Новая подписка оформляется на месяц <b>с даты оплаты</b> и <b>не суммируется</b> с остатком — "
                            f"если оплатить сейчас, оставшиеся дни сгорят.\n\n"
                            f"Продлить подписку → 🛍 Магазин",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text=f"Продлить {s['service_name']}", callback_data=f"shop_renew:{s['service_key']}", icon_custom_emoji_id="5262479378880673679")],
                                [InlineKeyboardButton(text="👤 Мой профиль", callback_data="show_profile")],
                            ])
                        )
                        await conn.execute("UPDATE user_subscriptions SET notified_3d=TRUE WHERE id=$1", s["id"])
                        logging.info(f"Sub reminder 3d: uid={s['user_id']} service={s['service_name']}")
                    except Exception as e:
                        logging.warning(f"Sub reminder 3d failed uid={s['user_id']}: {e}")

                subs_1d = await conn.fetch("""
                    SELECT s.*, u.user_id FROM user_subscriptions s
                    JOIN users u ON u.user_id = s.user_id
                    WHERE s.is_active = TRUE AND s.notified_1d = FALSE
                      AND s.expires_at > NOW() AND s.expires_at < NOW() + INTERVAL '1 day'
                """)
                for s in subs_1d:
                    exp = s["expires_at"].strftime("%d.%m.%Y")
                    plan = f" {s['plan_name']}" if s["plan_name"] else ""
                    try:
                        await bot.send_message(
                            s["user_id"],
                            f"⚠️ <b>Подписка истекает завтра!</b>\n\n"
                            f"📦 <b>{s['service_name']}{plan}</b>\n"
                            f"📅 Дата окончания: <b>{exp}</b>\n\n"
                            f"💡 Лучше оплатить <b>завтра, в день окончания</b>: новая подписка идёт месяц "
                            f"с даты оплаты и <b>не суммируется</b> с остатком.\n\n"
                            f"Закажи продление 👇",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text=f"Продлить {s['service_name']}", callback_data=f"shop_renew:{s['service_key']}", icon_custom_emoji_id="5262479378880673679")],
                            ])
                        )
                        await conn.execute("UPDATE user_subscriptions SET notified_1d=TRUE WHERE id=$1", s["id"])
                    except Exception as e:
                        logging.warning(f"Sub reminder 1d failed uid={s['user_id']}: {e}")

        except Exception as e:
            logging.error(f"subscription_reminder_loop error: {e}")
        await asyncio.sleep(3600)


async def reminders_loop():
    """Раз в 3 часа проверяет неактивных и шлёт напоминания."""
    await asyncio.sleep(300)  # первые 5 минут не трогаем
    while True:
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                # day3: 3 дня неактивности, ещё не слали 'day3'
                rows3 = await conn.fetch("""
                    SELECT u.user_id FROM users u
                    WHERE u.last_active < NOW() - INTERVAL '3 days'
                      AND u.last_active > NOW() - INTERVAL '7 days'
                      AND COALESCE(u.is_blocked, 0) = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM reminders_sent r
                          WHERE r.user_id = u.user_id AND r.kind = 'day3'
                      )
                    LIMIT 50
                """)
                # day7: 7-14 дней, не слали 'day7'
                rows7 = await conn.fetch("""
                    SELECT u.user_id FROM users u
                    WHERE u.last_active < NOW() - INTERVAL '7 days'
                      AND u.last_active > NOW() - INTERVAL '14 days'
                      AND COALESCE(u.is_blocked, 0) = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM reminders_sent r
                          WHERE r.user_id = u.user_id AND r.kind = 'day7'
                      )
                    LIMIT 50
                """)
                # day14: 14+ дней, не слали 'day14'
                rows14 = await conn.fetch("""
                    SELECT u.user_id FROM users u
                    WHERE u.last_active < NOW() - INTERVAL '14 days'
                      AND u.last_active > NOW() - INTERVAL '30 days'
                      AND COALESCE(u.is_blocked, 0) = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM reminders_sent r
                          WHERE r.user_id = u.user_id AND r.kind = 'day14'
                      )
                    LIMIT 50
                """)

            sent_count = 0
            for r in rows3:
                if await send_reminder(r["user_id"], "day3", REMINDER_TEXTS["day3"]):
                    sent_count += 1
                await asyncio.sleep(0.1)  # не спамим API Telegram
            for r in rows7:
                if await send_reminder(r["user_id"], "day7", REMINDER_TEXTS["day7"]):
                    sent_count += 1
                await asyncio.sleep(0.1)
            for r in rows14:
                if await send_reminder(r["user_id"], "day14", REMINDER_TEXTS["day14"]):
                    sent_count += 1
                await asyncio.sleep(0.1)

            # Напоминание о неиспользованных кредитах ОТКЛЮЧЕНО.
            # Купленные кредиты не сгорают — рассылка «не дай им пропасть зря»
            # вводила клиентов в заблуждение, убрана по требованию.

            if sent_count > 0:
                logging.info(f"📬 Отправлено напоминаний: {sent_count}")

            # Раз в 3 часа
            await asyncio.sleep(3 * 3600)
        except Exception as e:
            logging.error(f"reminders_loop: {e}")
            await asyncio.sleep(3600)



async def db_cleanup_loop():
    """Фоновая чистка старых данных в БД. Запускается раз в сутки."""
    while True:
        try:
            # Ждём 24 часа (первая чистка - через 10 мин после старта)
            await asyncio.sleep(600 if not hasattr(db_cleanup_loop, '_started') else 86400)
            db_cleanup_loop._started = True

            pool = await get_pool()
            async with pool.acquire() as conn:
                # Старые записи generations > 180 дней
                r1 = await conn.execute(
                    "DELETE FROM generations WHERE created_at < NOW() - INTERVAL '180 days'"
                )
                # Завершённые fk_orders > 90 дней
                r2 = await conn.execute(
                    "DELETE FROM fk_orders WHERE status IN ('paid','completed','failed') "
                    "AND created_at < NOW() - INTERVAL '90 days'"
                )
                # События > 60 дней
                r3 = await conn.execute(
                    "DELETE FROM events WHERE created_at < NOW() - INTERVAL '60 days'")
                # Ключи gpt_confirm_msg:<заказ> живут ровно до того, как
                # сообщение о сбое перепишется в «активация прошла». Если
                # этого не случилось, ключ бесполезен: Telegram не даёт
                # править сообщения старше 48 часов. Чистим по ЗАКАЗУ —
                # только когда он действительно старый; ключи заказов, для
                # которых записи в fk_orders нет, не трогаем вовсе.
                await conn.execute(
                    """DELETE FROM settings
                       WHERE key LIKE 'gpt_confirm_msg:%'
                         AND EXISTS (SELECT 1 FROM fk_orders o
                                      WHERE o.order_id = substring(settings.key from 17)
                                        AND o.created_at < NOW() - INTERVAL '3 days')"""
                )
                # Брошенные состояния диалогов: клиент начал сценарий и ушёл.
                # Без чистки таблица растёт бесконечно.
                try:
                    r_fsm = await conn.execute(
                        "DELETE FROM fsm_storage WHERE updated_at < NOW() - INTERVAL '3 days'")
                except Exception as _e_fsm:
                    r_fsm = f"skip ({_e_fsm})"
                logging.info(f"🧹 FSM-состояния старше 3 дней: {r_fsm}")
                # Давно истёкшие подписки убираем из активных: иначе список
                # «Мои подписки» бесконечно растёт, а профиль делает по паре
                # запросов к БД на каждую строку. Месяц после окончания держим
                # видимыми — клиент успевает увидеть «истекла» и продлить.
                try:
                    r_subs = await conn.execute(
                        "UPDATE user_subscriptions SET is_active=FALSE "
                        "WHERE is_active=TRUE AND expires_at < NOW() - INTERVAL '30 days'"
                    )
                except Exception as _e_subs:
                    r_subs = f"skip ({_e_subs})"
                logging.info(f"🧹 Подписки деактивированы (истекли >30 дней): {r_subs}")
                # Подстраховка: пароли клиентов от их аккаунтов не должны лежать в БД
                # дольше месяца, даже если заказ забыли закрыть кнопкой «Готово».
                try:
                    r4 = await conn.execute(
                        "UPDATE linkpay_orders SET account_pass='' "
                        "WHERE account_pass <> '' AND created_at < NOW() - INTERVAL '30 days'"
                    )
                except Exception as _e_pw:
                    r4 = f"skip ({_e_pw})"
                logging.info(f"🧹 DB cleanup: gens={r1}, fk_orders={r2}, events={r3}, passwords_cleared={r4}")
        except Exception as e:
            logging.error(f"DB cleanup error: {e}")

async def gpt_codes_cleanup_loop():
    """Раз в 30 минут освобождает коды, зарезервированные дольше окна активации.

    Срок = activation_hours:chatgpt + 1 ч запаса, и дополнительно код не трогаем,
    пока на него есть ЖИВАЯ запись в gpt_pending_activations. Иначе код клиента
    возвращался в пул и мог уйти другому, пока у первого ещё открыта активация."""
    while True:
        try:
            await asyncio.sleep(1800)  # 30 минут
            pool = await get_pool()
            # Срок берём из настройки activation_hours:chatgpt (админ меняет её в
            # панели) + 1 час запаса. Раньше здесь было жёстко 12 часов: при окне
            # активации больше 12 ч код клиента возвращался в пул и мог уйти
            # второму клиенту — оба оплатили, активировал один.
            try:
                _act_h = await activation_hours("chatgpt")
            except Exception:
                _act_h = 12
            _release_after_h = max(2, int(_act_h) + 1)
            # ПЕРЕД возвратом в пул сверяем коды с сайтом активации: код мог
            # быть реально потрачен, а бот этого не зафиксировал (упал,
            # перезапустился, счёл неудачей). Тогда used_by остаётся NULL, код
            # выглядит свободным — и уходит следующему клиенту. Так 11.09.2026
            # один код ушёл двум клиентам, второму бот показал чужую почту.
            try:
                _audit = await gpt_pool_audit(include_reserved=True)
                if _audit.get("spent"):
                    _sp = "\n".join(f"• <code>{c}</code> — {v}"
                                     for c, v in _audit["spent"][:15])
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ <b>Похоже, в пуле есть потраченные коды</b> "
                        f"({len(_audit['spent'])})\n{_sp}\n\n"
                        f"Ничего не гасил и не удалял — проверь сам. "
                        f"В пул автоматически они не вернутся, и в выдаче "
                        f"стоят последними. Удалить можно в админ-панели.",
                        parse_mode="HTML")
            except Exception as _e_au:
                logging.warning(f"gpt_pool_audit перед возвратом: {_e_au}")
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM gpt_pending_activations WHERE expires_at < NOW()")
                released = await conn.execute(
                    """UPDATE gpt_codes
                       SET is_used=FALSE, reserved_at=NULL
                       WHERE is_used=TRUE
                         AND used_by IS NULL
                         AND reserved_at < NOW() - make_interval(hours => $1)
                         AND COALESCE(check_status,'unchecked') NOT IN ('used','error','invalid')
                         AND NOT EXISTS (
                             SELECT 1 FROM gpt_pending_activations p
                             WHERE p.code = gpt_codes.code
                         )""",
                    _release_after_h
                )
                if released and released != "UPDATE 0":
                    logging.info(f"🔑 gpt_codes cleanup: {released}")
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"🔑 <b>Коды ChatGPT возвращены в пул</b>\n"
                            f"Клиенты оплатили но не активировали за {_release_after_h} ч.\n"
                            f"<i>{released}</i>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"gpt_codes_cleanup_loop: {e}")


async def gpt_orphans_loop():
    """Подбирает активации, о которых бот не узнал, что они прошли.

    Два случая. Первый: деплой убил задачу на полуслове (активация длится до
    5 минут) — сайт её довёл, а записать некому. Второй: бот сам объявил
    неудачу, а сайт довёл активацию позже (11.09.2026 — неудача в 12:48,
    fulfilled в 13:01). В обоих случаях код остаётся «свободным» и через два
    часа уходит следующему клиенту.

    Раз в 5 минут: клиент ждёт подписку здесь и сейчас, и разница между
    «узнали через 5 минут» и «через 20» — это разница между «бот сам всё
    поправил» и «клиент успел написать в поддержку».
    """
    # Почта и Organization ID приходят С САЙТА. Один «<» в их ответе — и
    # Telegram отказался бы разобрать сообщение, а находка молча пропала бы:
    # отправка обёрнута в try, и в лог ушла бы строчка, которую никто не
    # читает. Экранируем всё внешнее.
    import html as _h_w
    def _ew(v):
        return _h_w.escape(str(v if v is not None else ""))

    def _orgline(label, v):
        """Строка про org с честной подписью.

        В колонке org сайт показывает разное: у кодов iOS — настоящий
        Organization ID аккаунта, у филиппинских — номер СВОЕГО заказа
        (выяснилось 17.09.2026). Подписывать номер заказа словом «org» значит
        каждый раз заставлять себя гадать, почему он «не совпал».
        """
        try:
            from chatgpt_activation import _org_kind as _ok_b
            if _ok_b(v) == "siteorder":
                return f"🧾 заказ на сайте: <code>{_ew(v)}</code>\n"
        except Exception:
            pass
        return f"{label}: <code>{_ew(v or '—')}</code>\n"

    await asyncio.sleep(90)           # даём боту подняться и подхватить вебхук
    _pass_no = 0
    while True:
        _pass_no += 1
        _t_started = time.time()
        try:
            _r = await gpt_reconcile_orphans()
            for _f in (_r.get("fixed") or []):
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"♻️ <b>Дописал потерянную активацию ChatGPT</b>\n"
                        f"👤 {await _who_user(_f['user_id'])} · {_f['plan_name']}\n"
                        f"🔑 <code>{_f['code']}</code> — сайт: {_f['status']}\n"
                        + (f"📧 {_f['email']}\n" if _f.get("email") else "")
                        + f"🆔 <code>{_f['order_id']}</code>\n\n"
                        + ("⚠️ <b>В строке ожидания не было номера заказа</b> — "
                           "подписку клиенту записал, но связать её с покупкой "
                           "нечем: карточку заказа не найти, в прибыли код не "
                           "сойдётся. Проверь этот заказ руками.\n\n"
                           if _f.get("no_order") else "")
                        + f"Активация прошла, но бот об этом не узнал вовремя "
                        f"(рестарт или сайт дозавершил её после отказа). Код "
                        f"закреплён за клиентом, подписка записана, клиенту "
                        f"сообщил.",
                        parse_mode="HTML")
                except Exception:
                    pass
            for _u in (_r.get("unsure") or []):
                # Дедуп по паре КОД+КЛИЕНТ. Этот путь НЕ удаляет строку
                # ожидания (и правильно: код закреплён за клиентом), поэтому
                # одна и та же находка попадает в КАЖДЫЙ проход. Клиент в
                # ключе обязателен: тот же код у ДРУГОГО клиента — это новая
                # находка, и глушить её старой пометкой нельзя. На 20 минутах это было три
                # сообщения в час, на пяти стало бы двенадцать.
                try:
                    from db import get_setting as _gs2, set_setting as _ss2
                    if (await _gs2(f"unsure:{_u['code']}:{_u['user_id']}", "")) == "1":
                        continue
                except Exception:
                    pass
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"❓ <b>Оборванная активация — проверь вручную</b>\n"
                        f"👤 {await _who_user(_u['user_id'])} · {_u['plan_name']}\n"
                        f"🔑 <code>{_ew(_u['code'])}</code> — сайт: {_ew(_u['status'])}\n"
                        f"📧 на сайте: <code>{_ew(_u['site_email'] or '—')}</code>\n"
                        f"📧 у клиента: <code>{_ew(_u['client_email'] or '—')}</code>\n"
                        + _orgline("🏢 org на сайте", _u.get('site_org'))
                        + _orgline("🏢 org у клиента", _u.get('client_org'))
                        + f"🆔 <code>{_u['order_id']}</code>\n\n"
                        f"Код потрачен, но что он ушёл именно этому клиенту — "
                        f"подтвердить не могу. Подписку НЕ записывал: иначе "
                        f"клиент увидел бы в профиле то, чего у него нет.\n"
                        f"Код пометил — в пул сам не вернётся.\n"
                        f"<i>Если проверил и это точно его аккаунт — "
                        f"<code>/gpt_lost_ok {_u['code']}</code></i>",
                        parse_mode="HTML")
                    try:
                        from db import set_setting as _ss3
                        await _ss3(f"unsure:{_u['code']}:{_u['user_id']}", "1")
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"gpt_orphans_loop: {e}")
        # Засекаем КАЖДЫЙ проход быстрой сверки. Без этого вопрос «почему бот
        # заметил активацию только через полчаса» не имеет ответа: изнутри
        # видно лишь итог, а не когда проход был и сколько занял. 17.09.2026
        # именно это и пришлось выяснять раскопками.
        try:
            from db import set_setting as _ss_t
            await _ss_t("recon_last_at", str(int(time.time())))
            await _ss_t("recon_last_ms", str(int((time.time() - _t_started) * 1000)))
        except Exception:
            pass

        # ── Второй проход: активации, которые ПРОШЛИ, а бот записал неудачу ──
        # Сверху разбираются «висящие» активации — те, у кого жива строка
        # ожидания. Но перебор «код уже использован» строку затирает следующим
        # кодом, и такие случаи наверх не попадают вовсе: 15.09.2026 три заказа
        # подряд были активированы на сайте (fulfilled, почта клиента, разница
        # в минуты), а бот сообщил о неудаче. Видел это только ручной
        # /gpt_codes_recover.
        # Сам ничего не пишем: присылаем находку с кнопкой.
        # Тяжёлый проход живёт в ТОМ ЖЕ цикле, что и быстрый, и пока он идёт,
        # быстрый не начнётся. А он перебирает все сожжённые коды за двое
        # суток — это отдельный запрос к сайту и работа по базе. Из-за этого
        # «раз в 5 минут» на бумаге превращалось в «раз в 5 минут ПЛЮС сколько
        # займут оба прохода», и клиент ждал дольше обещанного.
        # Быстрая сверка идёт каждый проход, тяжёлая — каждый третий, то есть
        # примерно раз в 15 минут. Ей спешить некуда: она разбирает случаи, где
        # строки ожидания уже нет, и сама ничего не записывает.
        _t_scan = time.time()
        try:
            if _pass_no % 3 != 1:
                _lost_found = []             # не наш проход — тяжёлое пропускаем
            else:
                from common import gpt_lost_activations_scan
                _lost_found = await gpt_lost_activations_scan(hours=48)
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            for _l in _lost_found:
                _m = _l.get("match")
                _head = ("✅ <b>Похоже, активация всё-таки прошла</b>"
                         if _m is True else
                         "❓ <b>Код потрачен — чей аккаунт, не подтверждаю</b>")
                _txt = (f"{_head}\n"
                        f"👤 {_ew(_l['user'])} ({await _who_user(_l['user_id'])})\n"
                        f"🔑 <code>{_ew(_l['code'])}</code> — сайт: {_ew(_l['status'])}\n"
                        f"📧 на сайте: <code>{_ew(_l['site_email'] or '—')}</code>\n"
                        f"📧 у клиента: <code>{_ew(_l['client_email'] or '—')}</code>\n"
                        + ((_orgline("🏢 org на сайте", _l.get('site_org'))
                            + _orgline("🏢 org у клиента", _l.get('client_org')))
                           if (_l.get('site_org') or _l.get('client_org')) else "")
                        + ""
                        + (f"🆔 <code>{_l['order_id']}</code>\n" if _l.get("order_id") else "")
                        + (f"🕐 сайт: {_ew(_l['site_when'])}\n" if _l.get("site_when") else ""))
                if _m is True:
                    _txt += ((f"\nСошлось по {'Organization ID' if _l.get('by')=='org' else 'почте'} — "
                              f"подписка ушла этому клиенту, "
                             "а бот записал неудачу. Нажми, чтобы дописать: "
                             "код привяжется к заказу, сообщение заказа "
                             "поправится, клиенту уйдёт уведомление."))
                    _kb = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(text="✅ Записать активацию",
                                             callback_data=f"gptlost:{_l['code']}")]])
                elif _m is False:
                    _txt += ((f"\n{'Organization ID' if _l.get('by')=='org' else 'Почты'} "
                              f"РАЗНЫЕ — записывать нельзя: клиент увидел бы "
                              f"в профиле чужую подписку. Разберись вручную."))
                    _kb = None
                else:
                    _txt += ("\nСверить почту не с чем. Ничего не трогаю — "
                             "посмотри сам.")
                    _kb = None
                try:
                    await bot.send_message(ADMIN_ID, _txt, parse_mode="HTML",
                                           reply_markup=_kb)
                    # Помечаем показанным сразу: иначе одна и та же находка
                    # будет приходить каждые 20 минут.
                    from db import set_setting as _ss
                    await _ss(f"lostact:{_l['code']}:{_l['user_id']}", "1")
                except Exception as _e_s:
                    logging.warning(f"lostact alert {_l['code']}: {_e_s}")
        except Exception as e2:
            logging.error(f"gpt_lost_activations: {e2}")
        if _pass_no % 3 == 1:
            try:
                from db import set_setting as _ss_t2
                await _ss_t2("lostscan_last_at", str(int(time.time())))
                await _ss_t2("lostscan_last_ms", str(int((time.time() - _t_scan) * 1000)))
            except Exception:
                pass

        # Ждём 5 минут ОТ НАЧАЛА прохода, а не после него. Прежний безусловный
        # sleep(5 мин) в конце давал интервал «5 минут плюс сколько заняли оба
        # прохода»: обещали пять, а на деле выходило больше — и понять это
        # снаружи было нельзя. Если проход затянулся дольше пяти минут,
        # следующий стартует сразу, но не чаще раза в 30 секунд.
        _spent = time.time() - _t_started
        await asyncio.sleep(max(30, 5 * 60 - _spent))


async def gpt_pool_audit_loop():
    """Раз в 3 часа сверяет пулы ChatGPT, Claude и Perplexity с сайтом активации.

    Ловит коды, потраченные мимо бота: сайт про них говорит fulfilled/claimed,
    а у нас они числятся свободными. Такой код, попав клиенту, раньше давал
    ложный «успех» с чужой почтой.
    """
    await asyncio.sleep(300)          # даём боту подняться
    while True:
        try:
            _r = await pool_audit(include_reserved=True)
            if _r.get("ok") and (_r.get("spent") or _r.get("odd")):
                try:
                    for _part in tg_chunks(pool_audit_report(_r)):
                        await bot.send_message(ADMIN_ID, _part, parse_mode="HTML")
                except Exception:
                    pass
        except Exception as e:
            logging.error(f"gpt_pool_audit_loop: {e}")
        await asyncio.sleep(3 * 3600)


async def _activation_jobs_cleanup_loop():
    """Каждый час удаляет завершённые задачи из _activation_jobs."""
    while True:
        await asyncio.sleep(3600)
        done_keys = [k for k, v in list(_activation_jobs.items()) if v.get("status") == "done"]
        for k in done_keys:
            del _activation_jobs[k]
        if done_keys:
            logging.info(f"🧹 activation_jobs cleanup: {len(done_keys)} tasks removed")




# ── Помощь с активацией ChatGPT ──────────────────────────────────────────────


async def gpt_dead_order_release_loop():
    """Возвращает в пул коды, которые сайт держал «claimed» в момент отказа.

    Зачем отдельная петля. Когда у сайта не проходит его карта
    (provider_status=failed_precharge), заказ мёртв и код не потрачен — но на
    странице /query он в этот момент ещё «claimed». Отдать его следующему
    клиенту сразу нельзя: «claimed» — промежуточное состояние, и оно может
    дозреть до «fulfilled» (так 10.09.2026 подряд ушло пять кодов). А бросить
    нельзя тем более: gpt_codes_cleanup_loop помеченные коды пропускает
    (check_status='error'), а речекер работает только по 987ai — то есть сам
    такой код не освободится НИКОГДА и тихо выпадет из пула.

    Поэтому _safe_release ставит метку «ждём освобождения: …», а эта петля раз
    в 10 минут переспрашивает сайт по каждому такому коду:
      • unused    → возвращаем в пул и снимаем метку;
      • fulfilled → сайт всё-таки выдал подписку по этому коду. Код потрачен,
                    и это важнее возврата: клиент мог уже получить активацию
                    по iOS-коду. Шлём Александру отдельный сигнал;
      • claimed   → ждём дальше, но не вечно: через 6 часов сдаёмся и зовём
                    Александра руками.
    """
    await asyncio.sleep(300)          # первый заход через 5 минут после старта
    while True:
        try:
            await asyncio.sleep(600)  # каждые 10 минут
            from db import release_gpt_code as _rel_dl
            from chatgpt_activation import (bpa_query_codes as _q_dl,
                                            BPA_FREE_STATUSES as _FREE_DL)
            pool = await get_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """SELECT code, used_by,
                              (COALESCE(reserved_at, NOW())
                               < NOW() - INTERVAL '6 hours') AS too_old
                       FROM gpt_codes
                       WHERE provider = 'bpa'
                         AND flagged_reason LIKE 'ждём освобождения:%'
                         -- Та же защита, что в gpt_codes_cleanup_loop: пока у
                         -- клиента открыта активация по этому коду, отдавать
                         -- его в пул нельзя — уйдёт второму, активирует один.
                         AND NOT EXISTS (
                             SELECT 1 FROM gpt_pending_activations p
                             WHERE p.code = gpt_codes.code)
                       ORDER BY COALESCE(last_checked_at, '2000-01-01') ASC
                       LIMIT 20""")
            if not rows:
                continue
            _freed, _spent, _gaveup = [], [], []
            for _r in rows:
                _c = _r["code"]
                try:
                    _st = ((await _q_dl([_c])).get(str(_c).strip().upper())
                           or {}).get("status", "")
                except Exception as _e_q:
                    logging.warning(f"dead-order release: /query {_c}: {_e_q}")
                    continue
                _st = str(_st or "").strip().lower()
                if not _st:
                    continue          # сайт промолчал — вернёмся через 10 минут
                async with pool.acquire() as conn:
                    if _st in _FREE_DL:
                        await _rel_dl(_c)
                        await conn.execute(
                            "UPDATE gpt_codes SET check_status='unchecked', "
                            "flagged_reason=NULL, last_checked_at=NOW() WHERE code=$1", _c)
                        _freed.append(_c)
                        logging.warning(f"dead-order release: {_c} освободился "
                                        f"на сайте — вернул в пул.")
                    elif _st in ("fulfilled", "zoom_token_ready"):
                        await conn.execute(
                            "UPDATE gpt_codes SET check_status='used', last_checked_at=NOW(), "
                            "flagged_reason=$2 WHERE code=$1",
                            _c, f"сайт всё-таки выдал подписку по этому коду ({_st})")
                        _spent.append((_c, _r["used_by"], _st))
                        logging.error(f"dead-order release: {_c} у сайта стал {_st} "
                                      f"ПОСЛЕ отказа — код потрачен.")
                    elif _r["too_old"]:
                        await conn.execute(
                            "UPDATE gpt_codes SET last_checked_at=NOW(), flagged_reason=$2 "
                            "WHERE code=$1", _c,
                            f"не освободился за 6 ч: сайт держит {_st}")
                        _gaveup.append((_c, _st))
                    else:
                        await conn.execute(
                            "UPDATE gpt_codes SET last_checked_at=NOW() WHERE code=$1", _c)
                await asyncio.sleep(3)      # не долбим сайт
            try:
                if _freed:
                    await bot.send_message(
                        ADMIN_ID,
                        "🔑 <b>Коды сами вернулись в пул</b>\n"
                        + "\n".join(f"• <code>{c}</code>" for c in _freed[:15])
                        + "\n\nСайт освободил их после провалившегося заказа — "
                          "делать ничего не нужно.",
                        parse_mode="HTML")
                for _c, _uid, _st in _spent[:5]:
                    _who = await _who_user(_uid) if _uid else "—"
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ <b>Сайт всё-таки выдал подписку по коду</b>\n"
                        f"🔑 <code>{_c}</code> · стал <b>{_st}</b> уже ПОСЛЕ отказа\n"
                        f"👤 {_who}\n\n"
                        f"Код потрачен, в пул не вернулся — это правильно. Но если "
                        f"этому клиенту мы уже активировали подписку по iOS-коду, "
                        f"то ушли две. Проверь заказ.",
                        parse_mode="HTML")
                if _gaveup:
                    await bot.send_message(
                        ADMIN_ID,
                        "🔒 <b>Коды не освободились за 6 часов</b>\n"
                        + "\n".join(f"• <code>{c}</code> — сайт держит {s}"
                                    for c, s in _gaveup[:15])
                        + "\n\nБольше не переспрашиваю. Ничего не гасил: "
                          "<code>/gpt_codes_recover</code>",
                        parse_mode="HTML")
            except Exception as _e_msg:
                logging.warning(f"dead-order release: сообщение админу: {_e_msg}")
        except Exception as e:
            logging.error(f"gpt_dead_order_release_loop: {e}")

async def gpt_code_rechecker_loop():
    """Раз в 2 часа проверяет свободные коды через Playwright.
    Помечает плохие (used/invalid) и хорошие (ok).
    Алертит Александра если нашлись плохие коды."""
    await asyncio.sleep(120)  # первый запуск через 2 мин после старта
    while True:
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                # Берём до 20 непроверенных свободных кодов (приоритет — без статуса).
                # ВАЖНО: речекер работает через 987ai.vip, поэтому проверяет ТОЛЬКО коды
                # сайта 987ai. Коды других сайтов (напр. 6661231.xyz) он не трогает —
                # иначе они ошибочно метятся invalid и пропадают из выдачи.
                rows = await conn.fetch(
                    """SELECT id, code, plan FROM gpt_codes
                       WHERE is_used = FALSE
                         AND provider = '987ai'
                         AND COALESCE(check_status, 'unchecked') NOT IN ('ok', 'used', 'invalid')
                       ORDER BY
                         CASE COALESCE(check_status,'unchecked')
                           WHEN 'unchecked' THEN 0
                           WHEN 'error'     THEN 1
                           ELSE 2
                         END,
                         COALESCE(last_checked_at, '2000-01-01') ASC
                       LIMIT 20"""
                )

            if not rows:
                logging.info("gpt_code_rechecker: нечего проверять")
                await asyncio.sleep(7200)
                continue

            logging.info(f"gpt_code_rechecker: проверяем {len(rows)} кодов")
            flagged = []  # [(code, status, email), ...]
            ok_count = 0

            for row in rows:
                status, email = await _check_one_gpt_code(row)
                pool2 = await get_pool()
                async with pool2.acquire() as conn:
                    await conn.execute(
                        """UPDATE gpt_codes
                           SET check_status=$1, last_checked_at=NOW(),
                               flagged_reason=CASE WHEN $1 IN ('used','invalid') THEN $2 ELSE NULL END
                           WHERE id=$3""",
                        status,
                        f"email={email}" if email else status,
                        row["id"]
                    )
                if status == "ok":
                    ok_count += 1
                    logging.info(f"gpt_code_rechecker ✅ ok: {row['code']}")
                elif status in ("used", "invalid"):
                    flagged.append((row["code"], status, email))
                    logging.warning(f"gpt_code_rechecker ⚠️ {status}: {row['code']} email={email}")
                else:
                    logging.debug(f"gpt_code_rechecker ❓ {status}: {row['code']}")

                # Пауза между запросами — не долбим сайт
                await asyncio.sleep(8)

            # Алерт Александру если нашлись плохие коды
            if flagged:
                lines = []
                for code, st, em in flagged:
                    icon = "♻️" if st == "used" else "❌"
                    lines.append(f"{icon} <code>{code}</code> — {st}" + (f" ({em})" if em else ""))
                try:
                    _lines_str = "\n".join(lines)
                    _msg = (
                        f"🔍 <b>Речекер кодов ChatGPT (987ai.vip): найдены проблемные</b>\n\n"
                        f"{_lines_str}\n\n"
                        f"✅ Проверено рабочих: <b>{ok_count}</b>\n"
                        f"⚠️ Помечено: <b>{len(flagged)}</b>\n\n"
                        f"Плохие коды исключены из выдачи автоматически."
                    )
                    await bot.send_message(ADMIN_ID, _msg, parse_mode="HTML")
                except Exception:
                    pass
            else:
                logging.info(f"gpt_code_rechecker: всё чисто, ok={ok_count}")

        except Exception as e:
            logging.error(f"gpt_code_rechecker_loop: {e}")

        await asyncio.sleep(7200)  # следующий прогон через 2 часа



# ═══════════════════════════════════════════════════════════════════
#  CLAUDE MINI APP
# ═══════════════════════════════════════════════════════════════════

# ─── Путь к HTML и флаг включения ────────────────────────────────────────────
async def claude_codes_cleanup_loop():
    """Каждые 30 минут возвращает в пул коды которые зарезервированы
    но не активированы > 2 часов (клиент получил код но не открыл WebApp)."""
    while True:
        try:
            await asyncio.sleep(1800)  # 30 минут
            pool = await get_pool()
            async with pool.acquire() as conn:
                # 1) Удаляем ПРОСРОЧЕННЫЕ резервы (pending истёк — клиент не активировал за 2ч).
                #    Иначе мёртвая запись «висит» в «Ждущих» и код при JOIN двоится.
                await conn.execute(
                    "DELETE FROM claude_pending_activations WHERE expires_at < NOW()")
                # 2) Возвращаем в пул коды, что зарезервированы (is_used, used_by=NULL),
                #    но больше не привязаны ни к одному ЖИВОМУ резерву.
                released = await conn.execute(
                    """UPDATE claude_codes
                       SET is_used=FALSE, used_by=NULL, used_at=NULL, order_id=NULL, org_id=NULL
                       WHERE is_used=TRUE
                         AND used_by IS NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM claude_pending_activations p
                             WHERE p.code = claude_codes.code
                         )"""
                )
                if released and released != "UPDATE 0":
                    logging.info(f"🔑 claude_codes cleanup: {released}")
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"🔑 <b>Коды Claude возвращены в пул</b>\n"
                            f"Клиенты оплатили но не активировали в течение 2 часов.\n"
                            f"<i>{released}</i>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"claude_codes_cleanup_loop: {e}")


async def perplexity_codes_cleanup_loop():
    """Каждые 30 минут возвращает в пул коды Perplexity, которые зарезервированы,
    но не активированы > 2 часов (клиент получил код, но не открыл WebApp)."""
    while True:
        try:
            await asyncio.sleep(1800)  # 30 минут
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM perplexity_pending_activations WHERE expires_at < NOW()")
                released = await conn.execute(
                    """UPDATE perplexity_codes
                       SET is_used=FALSE, used_by=NULL, used_at=NULL, order_id=NULL, org_id=NULL
                       WHERE is_used=TRUE
                         AND used_by IS NULL
                         AND NOT EXISTS (
                             SELECT 1 FROM perplexity_pending_activations p
                             WHERE p.code = perplexity_codes.code
                         )"""
                )
                if released and released != "UPDATE 0":
                    logging.info(f"\U0001f511 perplexity_codes cleanup: {released}")
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"\U0001f511 <b>Коды Perplexity возвращены в пул</b>\n"
                            f"Клиенты оплатили но не активировали в течение 2 часов.\n"
                            f"<i>{released}</i>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"perplexity_codes_cleanup_loop: {e}")


async def coins_refund_loop():
    """Раз в час возвращает монетки по НЕоплаченным заказам старше 24ч
    (клиент применил монетки + СБП, но доплату так и не внёс)."""
    while True:
        try:
            await asyncio.sleep(3600)  # 1 час
            pool = await get_pool()
            async with pool.acquire() as conn:
                cands = await conn.fetch(
                    "SELECT order_id, user_id, coins_spent FROM fk_orders "
                    "WHERE status != 'paid' AND coins_spent > 0 "
                    "AND created_at < NOW() - INTERVAL '24 hours'")
                for r in cands:
                    # атомарно «забираем» возврат, чтобы не вернуть дважды
                    claim = await conn.execute(
                        "UPDATE fk_orders SET coins_spent=0 "
                        "WHERE order_id=$1 AND coins_spent=$2 AND status != 'paid'",
                        r["order_id"], r["coins_spent"])
                    if claim.split()[-1] != "1":
                        continue
                    _amt = int(r["coins_spent"] or 0)
                    if _amt <= 0:
                        continue
                    try:
                        await add_coins(r["user_id"], float(_amt),
                                        reason=f"refund unpaid {r['order_id']}")
                    except Exception as _ce:
                        logging.error(f"coins_refund add_coins fail {r['order_id']}: {_ce}")
                        continue
                    logging.info(f"\U0001fa99 coins refund {_amt} uid={r['user_id']} order={r['order_id']}")
                    try:
                        await bot.send_message(
                            r["user_id"],
                            f"\U0001fa99 <b>Монетки возвращены</b>\n\n"
                            f"Заказ не был оплачен в течение суток — вернули <b>{_amt}\u20bd</b> монетками на баланс.",
                            parse_mode="HTML")
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"coins_refund_loop: {e}")


async def _claude_job_results_cleanup_loop():
    """Каждый час удаляет завершённые записи о задачах активации.

    Perplexity добавлен позже Claude: его словарь чистился только рестартом
    и рос по одной записи на каждый заказ.
    """
    while True:
        await asyncio.sleep(3600)
        done_keys = [k for k, v in list(_claude_job_results.items()) if v.get("status") == "done"]
        for k in done_keys:
            del _claude_job_results[k]
        _pp_done = []
        try:
            from common import _perplexity_job_results as _ppj
            _pp_done = [k for k, v in list(_ppj.items())
                        if isinstance(v, dict) and v.get("status") == "done"]
            for k in _pp_done:
                _ppj.pop(k, None)
        except Exception as _e_pp:
            logging.warning(f"perplexity_job_results cleanup: {_e_pp}")
        if done_keys or _pp_done:
            logging.info(f"🧹 job_results cleanup: claude={len(done_keys)} perplexity={len(_pp_done)}")


async def nsgifts_balance_alert_loop():
    """Проверяет баланс NS Gifts раз в час. Шлёт алерт если ниже порога."""
    await asyncio.sleep(600)   # первый запуск через 10 мин после старта
    _alerted_low = False       # не спамить одно сообщение

    while True:
        try:
            if rt.nsgifts_client:
                balance   = await rt.nsgifts_client.check_balance()
                threshold = await _nsg_threshold()
                if balance < threshold and not _alerted_low:
                    await bot.send_message(
                        ADMIN_ID,
                        f"⚠️ <b>NS Gifts: низкий баланс!</b>\n\n"
                        f"Текущий баланс: <b>${balance:.2f}</b>\n"
                        f"Порог: ${threshold:.0f}\n\n"
                        f"Пополни кабинет: https://wholesale.ns.gifts",
                        parse_mode="HTML"
                    )
                    _alerted_low = True
                    logging.warning(f"NSGifts low balance alert: ${balance:.2f}")
                elif balance >= threshold:
                    _alerted_low = False   # сбрасываем флаг после пополнения
        except Exception as e:
            logging.error(f"nsgifts_balance_alert_loop: {e}")

        await asyncio.sleep(3600)   # раз в час


# ──────────────────────────────────────────────────────────────────────────────
#  Хендлер: обход стандартного shop_svc для appstore
#  Вставить В НАЧАЛО bot.py (после импортов) или ПЕРЕД существующим shop_svc:
#  Иначе: в меню магазина замени callback_data appstore с "shop_svc:appstore"
#  на "nsg_start" (в функции menu_shop, в цикле where key == "appstore")
#
#  ИЛИ: добавь в начало существующего shop_svc хендлера:
#    if key == "appstore":
#        await cb.message.edit_text("…")  # редирект на nsg_start
#        await nsg_start(cb)
#        return
# ──────────────────────────────────────────────────────────────────────────────

# ──────────────────────────────────────────────────────────────────────────────
#  Админка NS Gifts — кнопка «🍎 App Store» в разделе настроек
# ──────────────────────────────────────────────────────────────────────────────


# ─── Авто-обновление описаний тарифов (актуальные модели) ──────────────────────
async def models_desc_refresh_loop():
    """Раз в неделю переписывает описания тарифов актуальными моделями (web_search)."""
    await asyncio.sleep(180)  # дать боту прогрузиться после старта
    while True:
        try:
            from models_refresh import refresh_all_descriptions
            await refresh_all_descriptions(notify=True)
        except Exception as e:
            logging.error(f"models_desc_refresh_loop: {e}")
        await asyncio.sleep(7 * 24 * 3600)  # раз в неделю

