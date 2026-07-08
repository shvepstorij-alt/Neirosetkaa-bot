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
    expire_old_batches, get_pool, log_event, add_coins,
)
from common import (
    _check_one_gpt_code, _nsg_threshold, fk_check_order_status, fk_credit_paid_order, send_reminder,
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

            # Напоминание о неиспользованных кредитах (7+ дней, баланс > 20 кр)
            async with pool.acquire() as conn:
                rows_credits = await conn.fetch("""
                    SELECT u.user_id,
                           COALESCE(SUM(b.credits_left), 0) AS total_credits
                    FROM users u
                    JOIN credit_batches b ON b.user_id = u.user_id
                    WHERE b.credits_left > 20
                      AND (b.expires_at IS NULL OR b.expires_at > NOW())
                      AND u.last_active < NOW() - INTERVAL '7 days'
                      AND u.last_active > NOW() - INTERVAL '30 days'
                      AND COALESCE(u.is_blocked, 0) = 0
                      AND NOT EXISTS (
                          SELECT 1 FROM reminders_sent r
                          WHERE r.user_id = u.user_id AND r.kind = 'unused_credits'
                          AND r.sent_at > NOW() - INTERVAL '14 days'
                      )
                    GROUP BY u.user_id
                    HAVING SUM(b.credits_left) > 20
                    LIMIT 30
                """)

            for r in rows_credits:
                credits = int(r["total_credits"])
                text = (
                    f"💎 У тебя на балансе <b>{credits} кредитов</b> - и они ждут!\n\n"
                    f"Не дай им пропасть зря. Сгенерируй фото или видео прямо сейчас 👇"
                )
                if await send_reminder(r["user_id"], "unused_credits", text):
                    sent_count += 1
                await asyncio.sleep(0.1)

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
                    "DELETE FROM events WHERE created_at < NOW() - INTERVAL '60 days'"
                )
                logging.info(f"🧹 DB cleanup: gens={r1}, fk_orders={r2}, events={r3}")
        except Exception as e:
            logging.error(f"DB cleanup error: {e}")

async def gpt_codes_cleanup_loop():
    """Раз в 30 минут освобождает коды которые зарезервированы > 2 часов но не активированы.
    Это происходит если клиент оплатил но так и не открыл Mini App."""
    while True:
        try:
            await asyncio.sleep(1800)  # 30 минут
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "DELETE FROM gpt_pending_activations WHERE expires_at < NOW()")
                released = await conn.execute(
                    """UPDATE gpt_codes
                       SET is_used=FALSE, reserved_at=NULL
                       WHERE is_used=TRUE
                         AND used_by IS NULL
                         AND reserved_at < NOW() - INTERVAL '2 hours'"""
                )
                if released and released != "UPDATE 0":
                    logging.info(f"🔑 gpt_codes cleanup: {released}")
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            f"🔑 <b>Коды ChatGPT возвращены в пул</b>\n"
                            f"Клиенты оплатили но не активировали в течение 2 часов.\n"
                            f"<i>{released}</i>",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
        except Exception as e:
            logging.error(f"gpt_codes_cleanup_loop: {e}")


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
    """Каждый час удаляет завершённые записи из _claude_job_results."""
    while True:
        await asyncio.sleep(3600)
        done_keys = [k for k, v in list(_claude_job_results.items()) if v.get("status") == "done"]
        for k in done_keys:
            del _claude_job_results[k]
        if done_keys:
            logging.info(f"🧹 claude_job_results cleanup: {len(done_keys)} removed")


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

