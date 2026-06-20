# -*- coding: utf-8 -*-
# Auto-split module "common" — part of Neirosetkaa-bot (refactored from bot.py).
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
    ACTIVITY_DAYS_PER_PAGE, ADMIN_ID, ANIM_LIMIT_PER_HOUR, CLAUDE_API_KEY, COINS_REF_PERCENT, FK_ALLOWED_IPS,
    FK_API_KEY, FK_IP_CHECK_DISABLED, FK_SECRET2, FK_SHOP_ID, MAX_CONCURRENT_GENS, MOTION_LIMIT_PER_HOUR,
    PAYMENTS_PAGE_SIZE, PERSONAL_USERNAME, PHOTO_LIMIT_PER_HOUR, SHOP_CATALOG, USERS_PAGE_SIZE, VIDEO_LIMIT_PER_HOUR,
    UI_EMOJI_IDS, WEBAPP_BASE_URL, _BOT_TZ, _CLAUDE_WEBAPP_HTML_PATH, _WEBAPP_HTML_PATH, _activation_jobs, _active_generations,
    _anim_history, _check_hourly_limit, _classify_query_complexity, _claude_job_results, _get_conv, _gpt_retry_counts,
    _motion_history, _photo_history, _pool, _ref_bonus_for_count, _split_long_message, _strip_all_formatting,
    _verify_tg_init_data, _video_history, activate_chatgpt, bot, build_system_prompt, claude_client,
    clean_reply, pending_fk_payments, plan_name_to_key, strip_surrogates,
)
from runtime_state import (
    rt,
)
from db import (
    _extract_email_from_token, add_coins, add_credits_batch, delete_claude_pending_activation, delete_pending_activation, ensure_user,
    fk_get_order, fk_mark_paid, get_claude_pending_activation, get_coins, get_credits, get_next_claude_code,
    get_next_gpt_code, get_pending_activation, get_pool, get_setting, get_user, is_blocked,
    log_event, log_payment, mark_claude_code_used, mark_gpt_code_used, release_claude_code, release_gpt_code,
    save_claude_pending_activation, save_pending_activation,
    get_ref_premium, premium_ref_earned_this_month, log_premium_ref,
    get_next_perplexity_code, release_perplexity_code, mark_perplexity_code_used,
    save_perplexity_pending_activation, get_perplexity_pending_activation, delete_perplexity_pending_activation,
    create_linkpay_order, get_linkpay_order, set_linkpay_link, set_linkpay_status, set_linkpay_admin_msg,
    set_linkpay_email,
)
from keyboards import (
    _eib, kb_admin_panel, tg_emoji_ui,
)

async def check_not_blocked(cb_or_msg, uid: int) -> bool:
    """Проверяет что юзер не заблокирован. Используется везде где есть платные действия.
    Возвращает True если можно продолжать, False если заблокирован (и показывает сообщение)."""
    if await is_blocked(uid):
        msg = "🚫 Ваш аккаунт заблокирован. Для уточнений - напишите @neirosetkaalex"
        try:
            if isinstance(cb_or_msg, CallbackQuery):
                await cb_or_msg.answer(msg, show_alert=True)
            else:
                await cb_or_msg.answer(msg)
        except Exception:
            pass
        return False
    return True


async def _check_can_generate(cb_or_msg, uid: int, kind: str = "photo") -> bool:
    """Проверки перед генерацией. kind: 'photo' | 'video' | 'anim'. Возвращает True если можно."""
    # 0) Юзер не заблокирован
    if not await check_not_blocked(cb_or_msg, uid):
        return False

    # A) Проверяем количество активных генераций (макс MAX_CONCURRENT_GENS)
    pool = await get_pool()
    async with pool.acquire() as conn:
        active_count = await conn.fetchval(
            """SELECT COUNT(*) FROM active_generations
               WHERE user_id = $1 AND started_at > NOW() - INTERVAL '30 minutes'""",
            uid
        ) or 0
    if active_count >= MAX_CONCURRENT_GENS:
        msg = f"⏳ У тебя уже {active_count} генераций. Максимум {MAX_CONCURRENT_GENS} одновременно."
        if isinstance(cb_or_msg, CallbackQuery):
            await cb_or_msg.answer(msg, show_alert=True)
        else:
            await cb_or_msg.answer(msg)
        return False

    # B) Почасовой лимит - по категории
    if kind == "video":
        history, limit, label = _video_history, VIDEO_LIMIT_PER_HOUR, "видео"
    elif kind == "anim":
        history, limit, label = _anim_history, ANIM_LIMIT_PER_HOUR, "анимаций"
    elif kind == "motion":
        history, limit, label = _motion_history, MOTION_LIMIT_PER_HOUR, "Motion Control"
    else:
        history, limit, label = _photo_history, PHOTO_LIMIT_PER_HOUR, "фото"

    can, minutes = _check_hourly_limit(uid, history, limit)
    if not can:
        msg = f"⏰ Лимит: {limit} {label} в час.\nПопробуй через {minutes} мин."
        if isinstance(cb_or_msg, CallbackQuery):
            await cb_or_msg.answer(msg, show_alert=True)
        else:
            await cb_or_msg.answer(msg)
        return False

    return True


# ─── Персистентные active_generations (переживают рестарт) ─
# Таблица active_generations надёжнее чем set в памяти - переживает перезапуски бота.
# In-memory set оставлен для обратной совместимости старого кода.

async def mark_generation_active(user_id: int, kind: str = "photo") -> bool:
    """Помечает юзера как генерирующего. Допускает до MAX_CONCURRENT_GENS записей.
    Возвращает False если лимит исчерпан.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM active_generations WHERE user_id = $1 AND started_at < NOW() - INTERVAL '30 minutes'",
            user_id
        )
        count = await conn.fetchval(
            "SELECT COUNT(*) FROM active_generations WHERE user_id = $1",
            user_id
        ) or 0
        if count >= MAX_CONCURRENT_GENS:
            return False
        await conn.execute(
            "INSERT INTO active_generations (user_id, kind) VALUES ($1, $2)",
            user_id, kind
        )
        _active_generations.add(user_id)
        return True


async def unmark_generation_active(user_id: int):
    """Убирает одну активную генерацию юзера (самую старую). Вызывать в finally."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM active_generations WHERE id = ("
            "  SELECT id FROM active_generations WHERE user_id = $1 "
            "  ORDER BY started_at ASC LIMIT 1"
            ")",
            user_id
        )
        remaining = await conn.fetchval(
            "SELECT COUNT(*) FROM active_generations WHERE user_id = $1", user_id
        ) or 0
    if remaining == 0:
        _active_generations.discard(user_id)


async def fk_check_order_status(order_id: str) -> dict | None:
    """Запрашивает у FreeKassa API статус заказа по нашему MERCHANT_ORDER_ID.

    Возвращает {"status": "paid"|"new"|"failed", "amount": ...} или None при ошибке.

    Пробует несколько endpoint'ов поочерёдно - если один заблокирован, переходит к другому.
    Поле для фильтра - `paymentId` (наш merchant_order_id), а не `orderId`.
    """
    if not FK_API_KEY:
        logging.warning("fk_check_order_status: FK_API_KEY не задан в Railway Variables")
        return None

    # Список endpoint'ов в порядке приоритета - попробуем каждый
    endpoints = [
        "https://api.freekassa.ru/v1/orders",
        "https://api.fk.life/v1/orders",
        "https://api.fk.money/v1/orders",
    ]

    last_error = None
    for endpoint in endpoints:
        try:
            # Nonce в МИЛЛИСЕКУНДАХ - иначе при 2 запросах в одну секунду FK отвергнет
            nonce = str(int(_time_module.time() * 1000))

            params = {
                "shopId": int(FK_SHOP_ID),
                "nonce": nonce,
                "paymentId": str(order_id),
            }

            # HMAC-SHA256 подпись: значения отсортированных по ключам параметров через |
            sorted_vals = [str(v) for k, v in sorted(params.items())]
            sign_str = "|".join(sorted_vals)
            signature = hmac.new(FK_API_KEY.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
            params["signature"] = signature

            headers = {"Content-Type": "application/json", "Accept": "application/json"}

            # Уменьшенный timeout 8 сек - чтобы быстрее переключаться между endpoint'ами
            timeout = aiohttp.ClientTimeout(total=8)

            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.post(endpoint, json=params, headers=headers) as r:
                    resp_text = await r.text()

                    if r.status != 200:
                        logging.warning(
                            f"FK API {endpoint} status={r.status} paymentId={order_id} "
                            f"response={resp_text[:200]}"
                        )
                        last_error = f"HTTP {r.status}"
                        continue

                    try:
                        import json as _json
                        data = _json.loads(resp_text) if resp_text else {}
                    except Exception as parse_err:
                        logging.error(f"FK API parse error: {parse_err} response={resp_text[:200]}")
                        last_error = "parse error"
                        continue

                    if data.get("type") == "error":
                        logging.warning(
                            f"FK API {endpoint} type=error paymentId={order_id} "
                            f"response={resp_text[:200]}"
                        )
                        last_error = "api error"
                        continue

                    orders = data.get("orders") or []
                    if not orders:
                        logging.info(
                            f"FK API {endpoint}: пусто для paymentId={order_id} "
                            f"(заказ ещё не создан в FK или не оплачен)"
                        )
                        # Endpoint работает но заказ не найден - возвращаем None но НЕ пробуем другие
                        # endpoint'ы (они дадут тот же результат)
                        return None

                    order = orders[0]
                    fk_int_status = order.get("status")
                    merchant_id_fk = order.get("merchant_order_id", "")
                    fk_internal_id = order.get("fk_order_id", "")
                    amount = order.get("amount", 0)

                    logging.info(
                        f"FK API {endpoint}: paymentId={order_id} → "
                        f"fk_status={fk_int_status} amount={amount} "
                        f"fk_internal={fk_internal_id}"
                    )

                    if fk_int_status == 1:
                        return {
                            "status": "paid",
                            "amount": amount,
                            "fk_order_id": fk_internal_id,
                            "merchant_order_id": merchant_id_fk,
                        }
                    elif fk_int_status == 8:
                        return {"status": "failed"}
                    elif fk_int_status == 9:
                        return {"status": "cancelled"}
                    else:
                        return {"status": "new"}

        except asyncio.TimeoutError:
            logging.warning(f"FK API {endpoint} TIMEOUT for paymentId={order_id} - пробуем следующий")
            last_error = "timeout"
            continue
        except aiohttp.ClientError as e:
            logging.warning(f"FK API {endpoint} ClientError: {e} - пробуем следующий")
            last_error = f"network: {e}"
            continue
        except Exception as e:
            logging.error(f"FK API {endpoint} exception paymentId={order_id}: {type(e).__name__}: {e}")
            last_error = str(e)
            continue

    # Все endpoint'ы упали
    logging.error(f"❌ FK API: все endpoint'ы недоступны для paymentId={order_id}, last_error={last_error}")
    return None


# ─── Фоновая чистка памяти ────────────────────────────────
async def send_reminder(user_id: int, kind: str, text: str) -> bool:
    """Пытается отправить напоминание юзеру. Записывает факт отправки."""
    try:
        await bot.send_message(
            user_id, text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🎨 Генерировать фото", callback_data="menu_image"),
                 InlineKeyboardButton(text="🎬 Генерировать видео", callback_data="menu_video")],
                [_eib("Главное меню", "back_main")],
            ])
        )
        pool = await get_pool()
        async with pool.acquire() as conn:
            if kind == "unused_credits":
                # Для этого типа разрешаем повторную отправку (удаляем старую запись)
                await conn.execute(
                    "DELETE FROM reminders_sent WHERE user_id=$1 AND kind=$2",
                    user_id, kind
                )
            await conn.execute(
                "INSERT INTO reminders_sent (user_id, kind) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                user_id, kind
            )
        return True
    except Exception as e:
        logging.warning(f"Reminder {kind} to {user_id} failed: {e}")
        return False


async def fk_create_order(amount: float, order_id: str, user_id: int,
                         payment_id: int = 36, currency: str = "RUB") -> str:
    """Создаёт заказ через FreeKassa API и возвращает ссылку на оплату.
    payment_id: 36 = Card RUB API, 44 = СБП API
    """
    import time as _time
    nonce = str(int(_time.time() * 1000))
    amount_str = f"{float(amount):.2f}"  # "2490.00"

    # Только нужные поля - без дублей
    params = {
        "shopId": int(FK_SHOP_ID),
        "nonce": nonce,
        "i": payment_id,
        "email": f"user{user_id}@tgbot.local",
        "ip": "127.0.0.1",
        "amount": amount_str,
        "currency": currency,
        "orderId": order_id,
    }
    # HMAC-SHA256: сортируем по ключам, значения через |
    sorted_vals = [str(v) for k, v in sorted(params.items())]
    sign_str = "|".join(sorted_vals)
    signature = hmac.new(FK_API_KEY.encode(), sign_str.encode(), hashlib.sha256).hexdigest()
    params["signature"] = signature

    logging.info(f"FK API sign_str: {sign_str}")

    url = "https://api.fk.life/v1/orders/create"
    headers = {"Content-Type": "application/json"}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=params, headers=headers) as r:
            data = await r.json()
            logging.info(f"FK API create order response: {data}")
            if data.get("type") == "success":
                return data.get("location", "")
            raise Exception(f"FK API error: {data.get('message', data)}")


async def safe_send_media(
    send_func,
    *args,
    max_attempts: int = 3,
    op_name: str = "media",
    **kwargs
):
    """Обёртка с retry для любой bot.send_* функции (send_photo, send_video, send_document, answer_video, answer_photo).
    
    Применяет exponential backoff при таймаутах и сетевых ошибках Telegram.
    Безопасно: если все попытки провалились - исключение пробрасывается наружу.
    
    Пример использования:
      await safe_send_media(cb.message.answer_photo, BufferedInputFile(data, "img.png"), caption="...")
    """
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            result = await send_func(*args, **kwargs)
            if attempt > 1:
                logging.info(f"safe_send_media[{op_name}] succeeded on attempt {attempt}/{max_attempts}")
            return result
        except Exception as e:
            last_err = e
            err_str = str(e).lower()
            is_retryable = (
                "timeout" in err_str or "timed out" in err_str
                or "reset" in err_str or "network" in err_str
                or "temporarily" in err_str
            )
            if attempt < max_attempts and is_retryable:
                delay = 2 * attempt  # 2s, 4s, 6s
                logging.warning(f"safe_send_media[{op_name}] attempt {attempt}/{max_attempts} failed: {str(e)[:150]} - retrying in {delay}s")
                await asyncio.sleep(delay)
                continue
            # Не ретраим / последняя попытка
            logging.error(f"safe_send_media[{op_name}] failed after {attempt} attempts: {e}")
            raise
    if last_err:
        raise last_err


async def notify_admin_error(context: str, e: Exception):
    """Отправляет реальную ошибку админу с деталями + трекинг для алертов.
    Safety-блокировки - отдельный тип алерта (🟡 вместо 🔴), не считаются как инфра-ошибки."""
    err_msg = str(e)
    low = err_msg.lower()

    # Сначала проверяем точные не-safety проблемы (инфра, downstream)
    is_infra_problem = (
        "нестабил" in low or "downstream" in low or
        "openai gpt image" in low or "недоступ" in low or
        "api ключ" in low or "rate limit" in low or
        "сейчас нестабил" in low or "временно недоступна" in low
    )

    # Safety - только явные индикаторы контент-фильтра
    is_safety = (
        not is_infra_problem and (
            "🛡" in err_msg or
            "nsfw" in low or "насили" in low or "знаменит" in low or
            "violat" in low or "moderat" in low or "inappropriate" in low or
            "контент-полит" in low or "content policy" in low or
            "content_policy_violation" in low or
            "flagged by a content checker" in low or
            ("фильтр" in low and "безопасн" in low)
        )
    )

    # Логируем в БД
    try:
        event_kind = "content_blocked" if is_safety else "error"
        await log_event(None, event_kind, f"{context} | {err_msg[:500]}")
    except Exception:
        pass

    # Safety - жёлтый алерт, не идёт в счётчик критических ошибок
    if is_safety:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"🟡 <b>Промт заблокирован фильтром</b> | {context}\n\n"
                f"<i>Клиент попробовал нарушить safety. Кредиты возвращены.</i>\n\n"
                f"<code>{err_msg[:600]}</code>",
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    # Реальная ошибка - красный алерт + счётчик
    try:
        # Telegram лимит 4096 символов; оставляем 500 на форматирование
        await bot.send_message(
            ADMIN_ID,
            f"🔴 <b>Ошибка</b> | {context}\n\n<code>{err_msg[:3500]}</code>",
            parse_mode="HTML"
        )
    except Exception:
        pass
    try:
        await track_error_for_alert()
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════

async def show_admin_panel(message: Message):
    """Показать админ панель - используется и из /admin и из кнопки."""
    try:
        s = await get_admin_stats()
        await message.answer(
            f"⚙️ <b>Админ панель</b>\n\n"
            f"👥 Пользователей: <b>{s['users']}</b>\n"
            f"🎨 Генераций: <b>{s['gens']}</b>\n"
            f"💸 Кредитов потрачено: <b>{s['credits_used']}</b>\n"
            f"💳 Платежей: <b>{s['payments']}</b>\n"
            f"💰 Выручка: <b>{s['revenue']}₽</b>\n\n"
            f"<b>Топ по балансу:</b>\n{s['top_text']}",
            reply_markup=kb_admin_panel(),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"show_admin_panel error: {e}")
        await message.answer(f"⛔ Ошибка загрузки панели: {e}")


async def fk_monitor_order(order_id: str):
    """Активный мониторинг конкретного заказа FK после клика на оплату.

    Проверяет статус в FK API КАЖДЫЕ 5 СЕКУНД в течение 5 минут (60 проверок).
    
    Как только заказ paid - мгновенно зачисляем кредиты.
    Если за 5 минут не оплачен - прекращаем мониторинг.
    После 5 минут заказ всё равно подхватит:
      • auto-check loop (каждые 5 мин) если webhook не пришёл
      • кнопка "Проверить оплату" если клиент жмёт сам
    """
    CHECK_INTERVAL = 5      # секунды между проверками
    MAX_DURATION   = 300    # 5 минут максимум
    total_checks   = MAX_DURATION // CHECK_INTERVAL  # 60 проверок

    for check_num in range(1, total_checks + 1):
        await asyncio.sleep(CHECK_INTERVAL)

        try:
            # Проверяем что заказ ещё не оплачен (вдруг webhook успел раньше)
            db_order = await fk_get_order(order_id)
            if not db_order or db_order["status"] == "paid":
                # Уже зачислено - выходим
                logging.info(f"FK monitor: order {order_id} уже paid (check #{check_num}) - стоп")
                return

            # Спрашиваем FK API
            fk_status = await fk_check_order_status(order_id)
            if fk_status and fk_status.get("status") == "paid":
                # Платёж пришёл! Зачисляем
                payment = {
                    "user_id": db_order["user_id"],
                    "credits": db_order["credits"],
                    "amount":  db_order["amount_rub"],
                    "promo_code": db_order.get("promo_code"),
                }
                success = await fk_credit_paid_order(order_id, payment, source="active_monitor")
                if success:
                    elapsed_sec = check_num * CHECK_INTERVAL
                    logging.info(
                        f"⚡ FK monitor УСПЕХ: order={order_id} "
                        f"зачислен через {elapsed_sec}с (check #{check_num}/60)"
                    )
                return

            # Статус failed - выходим, ждать бесполезно
            if fk_status and fk_status.get("status") == "failed":
                logging.info(f"FK monitor: order {order_id} failed (check #{check_num}) - стоп")
                return

        except Exception as e:
            logging.warning(f"FK monitor error for order {order_id} (check #{check_num}): {e}")

    # 5 минут прошло - прекращаем
    logging.info(f"FK monitor: order {order_id} не оплачен за 5 минут - стоп (auto-check продолжит)")


async def process_referral_bonus(user_id: int):
    """Начисляет бонус пригласившему при первой покупке реферала.
    Размер бонуса зависит от количества уже оплативших рефералов."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT referred_by, ref_bonus_paid FROM users WHERE user_id=$1", user_id
        )
        if not row or not row["referred_by"] or row["ref_bonus_paid"]:
            return
        referrer_id = row["referred_by"]
        # Премиум-партнёрам разовый бонус не начисляем — у них пожизненный %
        # (начисляется отдельно в process_premium_referral на КАЖДОЙ оплате).
        _rp_chk = await get_ref_premium(referrer_id)
        if _rp_chk and _rp_chk.get("ref_premium"):
            return
        # Если реферер заблокирован - не платим бонус, но помечаем что «обработано»
        # чтобы не дёргать эту функцию каждый раз
        if await is_blocked(referrer_id):
            logging.info(f"Ref bonus SKIPPED: referrer {referrer_id} is blocked")
            await conn.execute(
                "UPDATE users SET ref_bonus_paid=TRUE WHERE user_id=$1", user_id
            )
            return
        # Считаем сколько у реферера уже было платящих
        paid_count = await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE referred_by=$1 AND ref_bonus_paid=TRUE",
            referrer_id
        ) or 0
        await conn.execute(
            "UPDATE users SET ref_bonus_paid=TRUE WHERE user_id=$1", user_id
        )

    bonus_amount = _ref_bonus_for_count(paid_count)
    await add_credits_batch(referrer_id, bonus_amount, source="referral", days_valid=30)

    # Начисляем монетки - 10% от суммы покупки реферала
    # Сумму покупки берём из последнего платежа реферала
    try:
        pool2 = await get_pool()
        async with pool2.acquire() as conn2:
            last_amount = await conn2.fetchval(
                """SELECT amount_rub FROM fk_orders
                   WHERE user_id=$1 AND status='paid'
                   ORDER BY paid_at DESC LIMIT 1""",
                user_id
            )
        if last_amount:
            coins_earned = round(float(last_amount) * COINS_REF_PERCENT, 2)
            await add_coins(referrer_id, coins_earned, reason=f"ref_purchase uid={user_id}")
        else:
            coins_earned = 0
    except Exception as ce:
        logging.error(f"coins accrual error: {ce}")
        coins_earned = 0

    try:
        new_bal = await get_credits(referrer_id)
        new_coins = await get_coins(referrer_id)
        tier_note = ""
        if paid_count + 1 == 5:
            tier_note = "\n🎖 Ты достиг уровня 5+ рефералов - теперь 250 кр за друга!"
        elif paid_count + 1 == 10:
            tier_note = "\n🥈 Ты достиг уровня 10+ рефералов - теперь 300 кр за друга!"
        elif paid_count + 1 == 20:
            tier_note = "\n🥇 Ты достиг уровня 20+ рефералов - теперь 325 кр за друга!"
        elif paid_count + 1 == 50:
            tier_note = "\n💎 50+ рефералов! Топовый уровень - 350 кр за друга!"
        coins_line = f"\n🪙 Монетки: <b>+{coins_earned:.0f}₽</b> (10% от покупки)" if coins_earned > 0 else ""
        await bot.send_message(
            referrer_id,
            f"🎉 <b>Реферальный бонус!</b>\n\n"
            f"Твой друг сделал первую покупку.\n"
            f"✨ Кредиты: <b>+{bonus_amount} кр</b>\n"
            f"💵 Баланс кредитов: <b>{new_bal} кр</b>"
            f"{coins_line}\n"
            f"🪙 Баланс монеток: <b>{new_coins:.0f}₽</b>"
            f"{tier_note}",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"ref bonus notify error: {e}")


# ══════════════════════════════════════════════════════════
#  ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ
# ══════════════════════════════════════════════════════════

async def process_premium_referral(referee_id: int, order_id: str, amount_rub: float):
    """Премиум-рефералка: % монетками с КАЖДОЙ оплаты реферала (только для премиум-партнёров).
    Идемпотентно по order_id, с месячным лимитом из настроек."""
    try:
        if not amount_rub or float(amount_rub) <= 0:
            return
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT referred_by FROM users WHERE user_id=$1", referee_id
            )
        if not row or not row["referred_by"]:
            return
        referrer_id = row["referred_by"]
        if referrer_id == referee_id:
            return
        rp = await get_ref_premium(referrer_id)
        if not rp or not rp.get("ref_premium"):
            return
        if await is_blocked(referrer_id):
            return
        # процент: индивидуальный, иначе глобальный из настроек
        pct = rp.get("ref_premium_pct")
        if pct is None:
            try:
                pct = float(await get_setting("ref_premium_pct", "10") or "10")
            except Exception:
                pct = 10.0
        pct = float(pct)
        if pct <= 0:
            return
        reward = round(float(amount_rub) * pct / 100.0, 2)
        if reward <= 0:
            return
        # месячный лимит (0 = без лимита)
        try:
            cap = float(await get_setting("ref_premium_cap", "0") or "0")
        except Exception:
            cap = 0.0
        if cap > 0:
            earned = await premium_ref_earned_this_month(referrer_id)
            remaining = cap - earned
            if remaining <= 0:
                logging.info(f"premium ref monthly cap reached referrer={referrer_id}")
                return
            if reward > remaining:
                reward = round(remaining, 2)
        # идемпотентность по order_id (UNIQUE в ref_premium_log)
        ok = await log_premium_ref(referrer_id, referee_id, order_id, float(amount_rub), reward)
        if not ok:
            logging.info(f"premium ref already logged order={order_id}")
            return
        await add_coins(referrer_id, reward, reason=f"ref_premium order={order_id}")
        try:
            new_coins = await get_coins(referrer_id)
            await bot.send_message(
                referrer_id,
                f"💎 <b>Премиум-реферал</b>\n\n"
                f"Твой реферал оплатил покупку на {int(round(float(amount_rub)))}₽.\n"
                f"🪙 Начислено: <b>+{reward:.0f}₽</b> монетками ({pct:.0f}%).\n"
                f"💰 Баланс монеток: <b>{new_coins:.0f}₽</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    except Exception as e:
        logging.error(f"process_premium_referral error: {e}")


async def _send_long_reply(message_or_cb, text: str, reply_markup=None,
                            is_callback: bool = False):
    """Отправляет длинный ответ консультанта, разбивая на части если надо.
    Клавиатура ВСЕГДА прикрепляется к ПОСЛЕДНЕЙ части - так она не 'уезжает' вверх.

    message_or_cb: Message (из chat_message) или CallbackQuery.message (из preset)
    text: ответ консультанта (может быть длинным)
    reply_markup: клавиатура для последнего сообщения
    is_callback: передан ли message_or_cb как CallbackQuery.message
    """
    # Клиент Telegram - это объект Message (даже если пришло из callback)
    send_target = message_or_cb

    parts = _split_long_message(text, max_len=3800)

    for i, part in enumerate(parts):
        is_last = (i == len(parts) - 1)
        kb = reply_markup if is_last else None

        try:
            await send_target.answer(part, reply_markup=kb, parse_mode="HTML")
        except Exception as e:
            # HTML не распарсился - стрипаем форматирование и шлём plain text
            logging.warning(f"HTML send failed (part {i+1}/{len(parts)}): {e}")
            plain = _strip_all_formatting(part)
            try:
                await send_target.answer(plain, reply_markup=kb)
            except Exception as e2:
                logging.error(f"Plain text also failed (part {i+1}): {e2}")


async def claude_with_search(uid: int, user_text: str) -> str:
    conv = _get_conv(uid)

    # Нормализация истории: убираем подряд идущие сообщения с одинаковой ролью
    # (может случиться если предыдущий API-вызов упал посередине)
    def _normalize_history(msgs: list) -> list:
        cleaned = []
        for m in msgs:
            if not isinstance(m, dict):
                continue
            content = m.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if cleaned and cleaned[-1].get("role") == m.get("role"):
                # Та же роль подряд - заменяем последнее сообщение на новое (актуальнее)
                cleaned[-1] = m
            else:
                cleaned.append(m)
        return cleaned

    # Сохраняем только текстовые сообщения в истории (не tool_use блоки)
    conv.append({"role": "user", "content": user_text})
    if len(conv) > 20:
        del conv[:-20]

    # Проверяем что API ключ настроен
    if not CLAUDE_API_KEY:
        logging.error("Claude API: CLAUDE_API_KEY не задан!")
        if conv and conv[-1].get("role") == "user":
            conv.pop()
        # Разовый алерт админу при первом обращении после запуска
        try:
            now_ts = _time_module.time()
            last_alert = getattr(claude_with_search, "_last_admin_alert", 0)
            if now_ts - last_alert > 600:
                setattr(claude_with_search, "_last_admin_alert", now_ts)
                await bot.send_message(
                    ADMIN_ID,
                    "🚨 <b>AI-Консультант: API ключ не задан</b>\n\n"
                    "Добавь <code>CLAUDE_API_KEY</code> в Railway Variables.",
                    parse_mode="HTML"
                )
        except Exception:
            pass
        return (
            "⚠️ Консультант временно недоступен - у нас небольшие технические работы.\n\n"
            "Попробуй через пару минут 🙏\n"
            "Если срочно - напиши @neirosetkaalex, он поможет напрямую."
        )

    # Гибридная маршрутизация: простые вопросы → Haiku (в 5 раз дешевле),
    # сложные → Sonnet. Fallback список включает обе модели чтобы быть устойчивыми.
    complexity = _classify_query_complexity(user_text, conv)
    if complexity == "simple":
        # Haiku первая, если упадёт - перейдём на Sonnet
        models_to_try = [
            "claude-haiku-4-5-20251001",    # Основная для простых: 5x дешевле
            "claude-sonnet-4-6",            # Fallback: умнее
            "claude-sonnet-4-5-20250929",   # Последний резерв
        ]
        logging.info(f"Consultant [uid={uid}]: SIMPLE query → Haiku")
    else:
        # Sonnet первая - для сложных вопросов
        models_to_try = [
            "claude-sonnet-4-6",            # Основная для сложных
            "claude-haiku-4-5-20251001",    # Fallback если Sonnet недоступен
            "claude-sonnet-4-5-20250929",   # Последний резерв
        ]
        logging.info(f"Consultant [uid={uid}]: COMPLEX query → Sonnet")

    api_messages = _normalize_history(list(conv))
    last_error = None

    # Попытка 1: с web_search, пробуя каждую модель
    for model_name in models_to_try:
        try:
            resp = claude_client.messages.create(
                model=model_name,
                max_tokens=2048,
                system=build_system_prompt(),
                tools=[{
                    "type": "web_search_20250305",
                    "name": "web_search",
                    "max_uses": 3,
                }],
                messages=api_messages,
            )
            # Собираем ТОЛЬКО text-блоки
            reply = ""
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    reply += getattr(block, "text", "")
            reply = reply.strip()
            if not reply:
                reply = "Попробуй переформулировать вопрос 🙏"
            reply = clean_reply(reply)
            conv.append({"role": "assistant", "content": reply})
            return reply
        except Exception as e:
            last_error = e
            err_type = type(e).__name__
            err_msg = str(e)[:500]
            logging.warning(f"Claude API [{model_name}] with search failed [{err_type}]: {err_msg}")
            # Если это проблема с моделью - пробуем следующую
            # Если проблема с web_search - попробуем без него
            continue

    # Попытка 2: БЕЗ web_search, пробуя каждую модель
    logging.info("Claude API: все попытки с web_search провалились, пробую без search")
    for model_name in models_to_try:
        try:
            resp = claude_client.messages.create(
                model=model_name,
                max_tokens=1024,
                system=build_system_prompt(),
                messages=api_messages,
            )
            reply = ""
            for block in resp.content:
                if getattr(block, "type", None) == "text":
                    reply += getattr(block, "text", "")
            reply = clean_reply(reply.strip() or "Попробуй переформулировать 🙏")
            conv.append({"role": "assistant", "content": reply})
            logging.info(f"Claude API: fallback без search сработал на {model_name}")
            return reply
        except Exception as e:
            last_error = e
            err_type = type(e).__name__
            err_msg = str(e)[:500]
            logging.warning(f"Claude API [{model_name}] no-search failed [{err_type}]: {err_msg}")
            continue

    # Всё провалилось - подробный лог + откат истории
    import traceback
    logging.error(f"Claude API: ВСЕ попытки упали. Последняя ошибка: {last_error}")
    logging.error(f"Claude API traceback: {traceback.format_exc()[:1500]}")

    # Откатываем user message чтобы при следующей попытке не было проблем с историей
    if conv and conv[-1].get("role") == "user":
        conv.pop()

    # Разбор типа ошибки - для АДМИНСКОГО алерта (не для клиента!)
    err_str = str(last_error).lower() if last_error else ""
    admin_diagnosis = "Неизвестная ошибка API"
    admin_action = "Проверь Railway Logs - там полный traceback."
    if "authentication" in err_str or "unauthorized" in err_str or "api_key" in err_str:
        admin_diagnosis = "🔑 Проблема с API-ключом Claude"
        admin_action = "Проверь CLAUDE_API_KEY в Railway Variables."
    elif "rate_limit" in err_str or "rate limit" in err_str:
        admin_diagnosis = "⏳ Rate limit на Anthropic API"
        admin_action = "Временное ограничение, само пройдёт через минуту."
    elif "billing" in err_str or "credit balance is too low" in err_str or "insufficient" in err_str:
        admin_diagnosis = "💳 Кончились средства на Anthropic API"
        admin_action = "Пополни баланс: https://console.anthropic.com/ → Billing"
    elif "not found" in err_str or "model" in err_str and "does not exist" in err_str:
        admin_diagnosis = "🤖 Модель недоступна на твоём tier"
        admin_action = "Нужно обновить tier или переключить модель."
    elif "connection" in err_str or "timeout" in err_str:
        admin_diagnosis = "🌐 Проблема с сетью (timeout/connection)"
        admin_action = "Обычно восстанавливается сама. Если часто - проверь Railway."

    # Шлём админу диагностику, но не чаще чем раз в 10 минут (чтобы не спамить)
    try:
        now_ts = _time_module.time()
        last_alert = getattr(claude_with_search, "_last_admin_alert", 0)
        if now_ts - last_alert > 600:  # 10 минут
            setattr(claude_with_search, "_last_admin_alert", now_ts)
            err_snippet = str(last_error)[:300] if last_error else "(no error message)"
            await bot.send_message(
                ADMIN_ID,
                f"🚨 <b>AI-Консультант упал</b>\n\n"
                f"<b>Диагноз:</b> {admin_diagnosis}\n"
                f"<b>Что делать:</b> {admin_action}\n\n"
                f"<b>Юзер:</b> <code>{uid}</code>\n"
                f"<b>Ошибка:</b> <code>{err_snippet}</code>\n\n"
                f"<i>Алерты приходят не чаще раза в 10 минут. Подробности - в Railway Logs.</i>",
                parse_mode="HTML"
            )
    except Exception as alert_err:
        logging.warning(f"Не удалось отправить админ-алерт: {alert_err}")

    # КЛИЕНТУ - только нейтральное сообщение без технических деталей
    return (
        "⚠️ Консультант временно недоступен - у нас небольшие технические работы.\n\n"
        "Попробуй через пару минут 🙏\n"
        "Если срочно - напиши @neirosetkaalex, он поможет напрямую."
    )


# ══════════════════════════════════════════════════════════
#  REPLY KEYBOARD HANDLERS
# ══════════════════════════════════════════════════════════

async def _show_profile(message: Message, user):
    uid = user.id
    try:
        await ensure_user(uid)
        cr = await get_credits(uid)
    except Exception as e:
        await message.answer(f"⚠️ Ошибка загрузки профиля: {e}")
        return

    try:
        pool = await get_pool()
    except Exception as e:
        await message.answer(f"⚠️ Ошибка подключения к БД: {e}")
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE user_id=$1", uid
        )
        total_gens = row[0] or 0
        total_credits_spent = row[1] or 0
        by_model = await conn.fetch(
            "SELECT model, COUNT(*) as cnt FROM generations WHERE user_id=$1 GROUP BY model ORDER BY cnt DESC",
            uid
        )
        # Рефералы
        total_refs = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1", uid) or 0
        paid_refs  = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1 AND ref_bonus_paid=TRUE", uid) or 0

    MODEL_DISPLAY = {
        # ключи IMAGE_MODELS
        "img_fast":  "Imagen 4 Fast",
        "img_std":   "Imagen 4 Standard",
        "img_ultra": "Imagen 4 Ultra",
        "nb_flash":  "Nano Banana Flash",
        "nb_2":      "Nano Banana v2",
        "nb_pro":    "Nano Banana Pro",
        "flux_pro":  "Flux 2 Pro",
        "ideogram_v3": "Ideogram V3",
        # ключи VIDEO_MODELS
        "vid_lite":  "Veo 3.1 Lite",
        "vid_fast":  "Veo 3.1 Fast",
        "vid_pro":   "Veo 3.1 Pro",
        "kling_turbo": "Kling 2.5 Turbo",
        "kling_pro":   "Kling 3.0 Pro",
        # специальные
        "gemini-flash-image": "Редактирование фото",
        "veo-3.1-animate":    "Анимация фото",
    }

    model_lines = ""
    if by_model:
        by_model_dict = {r['model']: r['cnt'] for r in by_model}

        def brand_total(*keys):
            return sum(by_model_dict.get(k, 0) for k in keys)

        # Бренды: (эмодзи + название, список ключей)
        BRANDS = [
            (f'{tg_emoji_ui("iband_imagen", "🌟")} Imagen 4',        ["img_fast", "img_std", "img_ultra"]),
            (f'{tg_emoji_ui("iband_nano",   "🍌")} Nano Banana',      ["nb_flash", "nb_2", "nb_pro"]),
            (f'{tg_emoji_ui("iband_flux",   "🎨")} Flux &amp; Ideogram', ["flux_pro", "ideogram_v3"]),
            (f'{tg_emoji_ui("vband_veo",    "🎥")} Veo 3.1',          ["vid_lite", "vid_fast", "vid_pro"]),
            (f'{tg_emoji_ui("vband_kling",  "🎞")} Kling',            ["kling_turbo", "kling_pro"]),
            ("✏️ Редактирование",                                      ["gemini-flash-image"]),
            ("🎭 Анимация",                                            ["veo-3.1-animate"]),
        ]

        brand_parts = []
        for label, keys in BRANDS:
            total = brand_total(*keys)
            if total:
                brand_parts.append(f"  {label} — <b>{total}</b>")

        if brand_parts:
            model_lines = "\n" + "\n".join(brand_parts) + "\n"

    # Загружаем историю покупок пользователя
    purchases = []
    try:
        async with pool.acquire() as conn:
            purchases = await conn.fetch(
                """SELECT pack, amount_rub, credits, paid_at
                   FROM fk_orders
                   WHERE user_id=$1 AND status='paid'
                   ORDER BY paid_at DESC NULLS LAST
                   LIMIT 10""",
                uid
            )
    except Exception:
        pass

    # Загружаем подписки пользователя (таблица может не существовать в старых БД)
    import datetime as _dt
    subs = []
    try:
        async with pool.acquire() as conn:
            subs = await conn.fetch("""
                SELECT service_name, plan_name, expires_at
                FROM user_subscriptions
                WHERE user_id=$1 AND is_active=TRUE AND expires_at > NOW()
                ORDER BY expires_at ASC
            """, uid)
    except Exception:
        pass  # таблица не существует — игнорируем

    try:
        coins = await get_coins(uid)
    except Exception:
        coins = 0

    subs_block = ""
    if subs:
        sub_lines = []
        now = _dt.datetime.now()
        for s in subs:
            days_left = (s["expires_at"] - now).days + 1
            exp = s["expires_at"].strftime("%d.%m.%Y")
            if days_left <= 3:
                icon = "\U0001f534"  # красный - скоро истекает
            elif days_left <= 7:
                icon = "\U0001f7e1"  # жёлтый
            else:
                icon = "\u2705"  # зелёный
            plan = f" {s['plan_name']}" if s['plan_name'] else ""
            sub_lines.append(f"{icon} <b>{s['service_name']}{plan}</b> - до {exp} ({days_left} дн.)")
        subs_block = "\n\n\U0001f4e6 <b>\u041c\u043e\u0438 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0438:</b>\n" + "\n".join(sub_lines)

    coins_block = f"\n\U0001fa99 \u041c\u043e\u043d\u0435\u0442\u043a\u0438: <b>{coins:.0f}\u20bd</b>" if coins > 0 else ""

    # \u0418\u0441\u0442\u043e\u0440\u0438\u044f \u043f\u043e\u043a\u0443\u043f\u043e\u043a
    purchases_block = ""
    if purchases:
        PACK_NAMES = {
            "p15": "\ud83c\udfaf \u041f\u0440\u043e\u0431\u043d\u044b\u0439 (150 \u043a\u0440)",
            "p25": "\ud83e\udd49 \u041d\u0430\u0447\u0430\u043b\u044c\u043d\u044b\u0439 (250 \u043a\u0440)",
            "p50": "\ud83e\udd48 \u0421\u0442\u0430\u0440\u0442 (500 \u043a\u0440)",
            "p150": "\ud83c\udfc5 \u0411\u0430\u0437\u043e\u0432\u044b\u0439 (1500 \u043a\u0440)",
            "p500": "\ud83e\udd47 \u041f\u0440\u043e (5000 \u043a\u0440)",
            "p1200": "\ud83d\udc8e \u0411\u0438\u0437\u043d\u0435\u0441 (12000 \u043a\u0440)",
        }
        pur_lines = []
        for p in purchases:
            pack = p["pack"] or ""
            dt = p["paid_at"]
            date_str = dt.strftime("%d.%m.%Y") if dt else "\u2014"
            amount = p["amount_rub"] or 0
            credits_val = p["credits"] or 0
            if pack.startswith("shop:"):
                parts = pack.split(":")
                svc_key = parts[1] if len(parts) > 1 else pack
                from_catalog = SHOP_CATALOG.get(svc_key, {})
                svc_emoji = from_catalog.get("emoji", "\ud83d\udecd")
                svc_name = from_catalog.get("name", svc_key)
                label = f"{svc_emoji} {svc_name}"
            else:
                label = PACK_NAMES.get(pack, f"+{credits_val} \u043a\u0440")
            pur_lines.append(f"  \u2022 {date_str} \u2014 {label} \u2014 <b>{amount}\u20bd</b>")
        purchases_block = "\n\n\ud83e\uddfe <b>\u0418\u0441\u0442\u043e\u0440\u0438\u044f \u043f\u043e\u043a\u0443\u043f\u043e\u043a:</b>\n" + "\n".join(pur_lines)

    safe_name = strip_surrogates(user.full_name or "")
    # \u0411\u043b\u043e\u043a \u0440\u0435\u0444\u0435\u0440\u0430\u043b\u043e\u0432
    refs_block = ""
    if total_refs > 0:
        refs_block = f"\n\n\ud83e\udd1d <b>\u0420\u0435\u0444\u0435\u0440\u0430\u043b\u044b:</b> {total_refs} \u043f\u0440\u0438\u0433\u043b\u0430\u0448\u0435\u043d\u043e \u00b7 {paid_refs} \u0441 \u043f\u043e\u043a\u0443\u043f\u043a\u043e\u0439"

    text = (
        f"\ud83d\udc64 <b>\u041f\u0440\u043e\u0444\u0438\u043b\u044c</b>\n\n"
        f"\ud83c\udd94 ID: <code>{uid}</code>\n"
        f"\ud83d\udc4b \u0418\u043c\u044f: {safe_name}\n\n"
        f"\ud83d\udcb5 <b>\u0411\u0430\u043b\u0430\u043d\u0441: {cr} \u043a\u0440\u0435\u0434\u0438\u0442\u043e\u0432</b>"
        f"{coins_block}"
        f"{refs_block}\n\n"
        f"\ud83d\udcca <b>\u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430:</b>\n"
        f"  <b>\u0413\u0435\u043d\u0435\u0440\u0430\u0446\u0438\u0439:</b> {total_gens}\n"
        f"  <b>\u041a\u0440\u0435\u0434\u0438\u0442\u043e\u0432 \u043f\u043e\u0442\u0440\u0430\u0447\u0435\u043d\u043e:</b> {total_credits_spent}"
        + model_lines
        + subs_block
    )
    # БАГ 6 FIX: кнопка Активировать Claude если есть pending
    _claude_pending_btn = []
    try:
        _cp = await get_claude_pending_activation(uid)
        if _cp:
            _claude_pending_btn = [[InlineKeyboardButton(
                text=f"⚡ Активировать Claude {_cp.get('plan_name', '')}",
                callback_data="claude_reopen_webapp"
            )]]
    except Exception:
        pass

    kb_profile = InlineKeyboardMarkup(inline_keyboard=[
        *_claude_pending_btn,
        [_eib("Пригласить друга", "menu_ref")],
        [_eib("Покупки", "profile_history"),
         _eib("Главное меню", "back_main")],
        [_eib("Купить кредиты", "menu_buy"),
         _eib("Избранное", "menu_favorites")],
    ])
    try:
        await message.answer(strip_surrogates(text), reply_markup=kb_profile, parse_mode="HTML")
    except Exception as e:
        import logging
        logging.error(f"reply_profile send error uid={uid}: {e}")
        await message.answer("⚠️ Не удалось отобразить профиль. Попробуй позже.")



async def get_admin_stats() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM users")
        gens = await conn.fetchval("SELECT COUNT(*) FROM generations") or 0
        credits_used = await conn.fetchval("SELECT COALESCE(SUM(credits),0) FROM generations") or 0
        payments = await conn.fetchval("SELECT COUNT(*) FROM payments") or 0
        revenue = await conn.fetchval("SELECT COALESCE(SUM(amount_rub),0) FROM payments") or 0
        top = await conn.fetch("SELECT user_id, credits FROM users ORDER BY credits DESC LIMIT 5")
    top_text = "\n".join([f"  {i+1}. ID {r['user_id']} - {r['credits']} кредитов" for i, r in enumerate(top)])
    return dict(users=users, gens=gens, credits_used=credits_used,
                payments=payments, revenue=revenue, top_text=top_text)

async def _build_stat_text(conn, since_sql: str, label: str) -> str:
    """Формирует текст статистики за произвольный период. since_sql — SQL-выражение для WHERE."""
    new_users = await conn.fetchval(f"SELECT COUNT(*) FROM users WHERE created_at >= {since_sql}") or 0
    row = await conn.fetchrow(f"SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE created_at >= {since_sql}")
    gens, credits_used = row[0] or 0, row[1] or 0
    row2 = await conn.fetchrow(f"SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM payments WHERE created_at >= {since_sql}")
    pays, revenue = row2[0] or 0, row2[1] or 0
    by_type = await conn.fetch(f"SELECT type, COUNT(*) FROM generations WHERE created_at >= {since_sql} GROUP BY type")
    cr_row = await conn.fetchrow(
        f"SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM fk_orders "
        f"WHERE status='paid' AND (pack NOT LIKE 'shop:%' OR pack IS NULL) AND paid_at >= {since_sql}"
    )
    sh_row = await conn.fetchrow(
        f"SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM fk_orders "
        f"WHERE status='paid' AND pack LIKE 'shop:%' AND paid_at >= {since_sql}"
    )
    # Продажи магазина по сервисам
    shop_detail = await conn.fetch(
        f"SELECT pack, COUNT(*) as cnt, COALESCE(SUM(amount_rub),0) as revenue "
        f"FROM fk_orders WHERE status='paid' AND pack LIKE 'shop:%' AND paid_at >= {since_sql} "
        f"GROUP BY pack ORDER BY cnt DESC"
    )

    by_type_text = "\n".join([f"  • {r[0]}: {r[1]} шт" for r in by_type]) or "  нет данных"
    cr_n, cr_sum = cr_row[0] or 0, cr_row[1] or 0
    sh_n, sh_sum = sh_row[0] or 0, sh_row[1] or 0

    # Группируем детали магазина по сервису
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

    if shop_by_svc:
        shop_lines = "\n".join(
            f"    • {name}: <b>{d['cnt']} шт · {d['rev']}₽</b>"
            for name, d in shop_by_svc.items()
        )
        shop_detail_text = f"\n{shop_lines}"
    else:
        shop_detail_text = ""

    return (
        f"📊 <b>Статистика: {label}</b>\n\n"
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


async def _show_activity_page(cb: CallbackQuery, page: int):
    """Показывает статистику по каждому дню с пагинацией (3 дня на странице)."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Находим самый ранний день с активностью - чтобы знать границу пагинации
            oldest_date = await conn.fetchval(
                "SELECT MIN(DATE(created_at)) FROM users"
            )
            if not oldest_date:
                await cb.message.answer(
                    "📈 <b>Активность</b>\n\nДанных пока нет.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
                    ]),
                    parse_mode="HTML"
                )
                await cb.answer()
                return

            today = await conn.fetchval("SELECT CURRENT_DATE")
            total_days = (today - oldest_date).days + 1
            max_page = max(0, (total_days - 1) // ACTIVITY_DAYS_PER_PAGE)
            page = max(0, min(page, max_page))

            # Считаем общую сводку (за всё время)
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
            total_gens = await conn.fetchval("SELECT COUNT(*) FROM generations") or 0
            total_spent = await conn.fetchval(
                "SELECT COALESCE(SUM(credits),0) FROM generations"
            ) or 0
            # Купленные кредиты - через UNION payments + fk_orders (paid, не дубли)
            total_bought = await conn.fetchval("""
                SELECT COALESCE(SUM(credits),0) FROM (
                    SELECT credits, created_at FROM payments
                    UNION ALL
                    SELECT credits, created_at FROM fk_orders
                    WHERE status='paid'
                      AND NOT EXISTS (
                          SELECT 1 FROM payments p
                          WHERE p.user_id = fk_orders.user_id
                            AND p.amount_rub = fk_orders.amount_rub
                            AND ABS(EXTRACT(EPOCH FROM (p.created_at - fk_orders.created_at))) < 120
                      )
                ) t
            """) or 0

            # Достаём данные по дням для текущей страницы
            offset_days = page * ACTIVITY_DAYS_PER_PAGE
            # Страница 0 = сегодня и 2 предыдущих; страница 1 = ещё 3 раньше; и т.д.
            day_blocks = []
            day_labels = ["Сегодня", "Вчера", "Позавчера"]

            for i in range(ACTIVITY_DAYS_PER_PAGE):
                days_ago = offset_days + i
                if days_ago >= total_days:
                    break
                # Рассчитываем границы дня
                day_row = await conn.fetchrow("""
                    SELECT
                        CURRENT_DATE - $1::int AS day,
                        (CURRENT_DATE - $1::int)::timestamp AS day_start,
                        (CURRENT_DATE - $1::int + 1)::timestamp AS day_end
                """, days_ago)
                day_date = day_row["day"]
                day_start = day_row["day_start"]
                day_end = day_row["day_end"]

                # Новые юзеры
                new_users = await conn.fetchval(
                    "SELECT COUNT(*) FROM users WHERE created_at >= $1 AND created_at < $2",
                    day_start, day_end
                ) or 0

                # Генерации и потраченные кредиты
                gen_row = await conn.fetchrow(
                    "SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations "
                    "WHERE created_at >= $1 AND created_at < $2",
                    day_start, day_end
                )
                gens_count = gen_row[0] or 0
                spent_credits = gen_row[1] or 0

                # Купленные кредиты за день (UNION)
                bought_row = await conn.fetchrow("""
                    SELECT COUNT(*), COALESCE(SUM(credits),0), COALESCE(SUM(amount_rub),0) FROM (
                        SELECT credits, amount_rub, created_at FROM payments
                        UNION ALL
                        SELECT credits, amount_rub, created_at FROM fk_orders
                        WHERE status='paid'
                          AND NOT EXISTS (
                              SELECT 1 FROM payments p
                              WHERE p.user_id = fk_orders.user_id
                                AND p.amount_rub = fk_orders.amount_rub
                                AND ABS(EXTRACT(EPOCH FROM (p.created_at - fk_orders.created_at))) < 120
                          )
                    ) t WHERE created_at >= $1 AND created_at < $2
                """, day_start, day_end)
                pay_count = bought_row[0] or 0
                bought_credits = bought_row[1] or 0
                revenue_rub = bought_row[2] or 0

                # Человекочитаемая метка дня
                if page == 0 and i < len(day_labels):
                    label = day_labels[i]
                    sublabel = day_date.strftime("%d.%m")
                else:
                    label = day_date.strftime("%d.%m.%Y")
                    # День недели
                    weekdays = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
                    sublabel = weekdays[day_date.weekday()]

                day_blocks.append(
                    f"📅 <b>{label}</b> · <i>{sublabel}</i>\n"
                    f"  👥 Новых: <b>{new_users}</b>\n"
                    f"  🎨 Генераций: <b>{gens_count}</b> · потрачено {spent_credits} кр\n"
                    f"  💰 Покупок: <b>{pay_count}</b> · +{bought_credits} кр · {revenue_rub}₽"
                )

        # Формируем сообщение
        header = (
            f"📈 <b>Активность</b>\n\n"
            f"📊 <b>Всего за всё время:</b>\n"
            f"👥 Юзеров: {total_users} · 🎨 ген: {total_gens}\n"
            f"💸 Потрачено: {total_spent} кр · 💰 Куплено: {total_bought} кр\n\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
        )
        body = "\n\n".join(day_blocks) if day_blocks else "<i>Нет данных за этот период</i>"
        text = header + body + f"\n\n<i>Страница {page+1}/{max_page+1}</i>"

        # Кнопки навигации
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️ Раньше", callback_data=f"adm_act_p:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{max_page+1}", callback_data="noop"))
        if page < max_page:
            nav.append(InlineKeyboardButton(text="Позже ▶️", callback_data=f"adm_act_p:{page+1}"))

        kb_rows = []
        if nav:
            kb_rows.append(nav)
        kb_rows.append([InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")])

        try:
            await cb.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                parse_mode="HTML"
            )
        except Exception:
            await cb.message.answer(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                parse_mode="HTML"
            )
    except Exception as e:
        logging.error(f"adm_activity error: {e}")
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


# ─── Популярные модели ────────────────────────────────────

async def _show_users_page(cb: CallbackQuery, page: int):
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
            # Статистика: платящие, активные сегодня/7д, заблокированные
            paid = await conn.fetchval(
                "SELECT COUNT(DISTINCT user_id) FROM payments"
            ) or 0
            active_today = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE last_active > NOW() - INTERVAL '1 day'"
            ) or 0
            active_7d = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE last_active > NOW() - INTERVAL '7 days'"
            ) or 0
            blocked = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE is_blocked=1"
            ) or 0

            max_page = max(0, (total - 1) // USERS_PAGE_SIZE)
            page = max(0, min(page, max_page))
            offset = page * USERS_PAGE_SIZE
            rows = await conn.fetch(
                "SELECT user_id, username, full_name, credits, created_at, last_active "
                "FROM users ORDER BY created_at DESC LIMIT $1 OFFSET $2",
                USERS_PAGE_SIZE, offset
            )

        lines = []
        for r in rows:
            username = (r['username'] or "").strip()
            full_name = (r['full_name'] or "").strip()
            uid = r['user_id']
            if username:
                uname = f"@{username}"
            elif full_name:
                uname = f"<a href='tg://user?id={uid}'>{full_name}</a>"
            else:
                uname = f"<a href='tg://user?id={uid}'>ID {uid}</a>"
            date = str(r['created_at'])[:10] if r['created_at'] else "-"
            lines.append(f"• {uname} · {r['credits']} кр · <code>{uid}</code> · рег. {date}")

        text = (
            f"👥 <b>Пользователи</b>\n\n"
            f"📊 Всего: <b>{total}</b>\n"
            f"💳 Платящих: <b>{paid}</b>\n"
            f"🔥 Активных сегодня: <b>{active_today}</b>\n"
            f"📅 Активных за 7д: <b>{active_7d}</b>\n"
            f"🚫 Заблокированных: <b>{blocked}</b>\n\n"
            f"<b>Страница {page+1}/{max_page+1}:</b>\n" + ("\n".join(lines) if lines else "Пусто")
        )

        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_users_p:{page-1}"))
        nav.append(InlineKeyboardButton(text=f"{page+1}/{max_page+1}", callback_data="noop"))
        if page < max_page:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_users_p:{page+1}"))

        kb_rows = []
        if nav:
            kb_rows.append(nav)
        kb_rows.append([InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")])

        try:
            await cb.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            await cb.message.answer(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


# ─── Найти пользователя ───────────────────────────────────

async def _show_payments_page(cb: CallbackQuery, page: int):
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # Собираем ВСЕ платежи: из таблицы payments + оплаченные fk_orders
            # (на случай если webhook не дошёл до log_payment, но деньги получены)
            # UNION исключит дубли по (user_id, amount, created_at) с точностью до минуты
            unified_sql = """
                SELECT user_id, credits, amount_rub, method, created_at FROM payments
                UNION ALL
                SELECT user_id, credits, amount_rub, 'freekassa' as method, created_at
                FROM fk_orders
                WHERE status='paid'
                  AND NOT EXISTS (
                      SELECT 1 FROM payments p
                      WHERE p.user_id = fk_orders.user_id
                        AND p.amount_rub = fk_orders.amount_rub
                        AND ABS(EXTRACT(EPOCH FROM (p.created_at - fk_orders.created_at))) < 120
                  )
            """

            # Общая статистика
            total_count = await conn.fetchval(
                f"SELECT COUNT(*) FROM ({unified_sql}) t"
            ) or 0
            total_sum = await conn.fetchval(
                f"SELECT COALESCE(SUM(amount_rub),0) FROM ({unified_sql}) t"
            ) or 0
            total_credits = await conn.fetchval(
                f"SELECT COALESCE(SUM(credits),0) FROM ({unified_sql}) t"
            ) or 0

            today_row = await conn.fetchrow(
                f"SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM ({unified_sql}) t "
                f"WHERE created_at > NOW() - INTERVAL '1 day'"
            )
            week_row = await conn.fetchrow(
                f"SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM ({unified_sql}) t "
                f"WHERE created_at > NOW() - INTERVAL '7 days'"
            )

            methods = await conn.fetch(
                f"SELECT method, COUNT(*) as n, COALESCE(SUM(amount_rub),0) as sum "
                f"FROM ({unified_sql}) t GROUP BY method ORDER BY sum DESC"
            )

            # Разбивка кредиты vs магазин (из fk_orders)
            cat_cr = await conn.fetchrow(
                "SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM fk_orders "
                "WHERE status='paid' AND (pack NOT LIKE 'shop:%' OR pack IS NULL)"
            )
            cat_sh = await conn.fetchrow(
                "SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM fk_orders "
                "WHERE status='paid' AND pack LIKE 'shop:%'"
            )

            max_page = max(0, (total_count - 1) // PAYMENTS_PAGE_SIZE)
            page = max(0, min(page, max_page))
            offset = page * PAYMENTS_PAGE_SIZE

            rows = await conn.fetch(
                f"SELECT t.user_id, t.credits, t.amount_rub, t.method, t.created_at, "
                f"       u.username, u.full_name "
                f"FROM ({unified_sql}) t LEFT JOIN users u ON u.user_id = t.user_id "
                f"ORDER BY t.created_at DESC LIMIT $1 OFFSET $2",
                PAYMENTS_PAGE_SIZE, offset
            )

            # Построение текста - остаётся внутри async with чтобы conn был доступен
            if total_count == 0:
                text = "🧾 <b>История платежей</b>\n\nПлатежей пока нет."
                kb_rows = [[InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]]
            else:
                method_lines = []
                for m in methods:
                    emoji = "🏦" if m['method'] == "freekassa" else ("⭐" if m['method'] == "stars" else "💳")
                    method_lines.append(f"{emoji} {m['method']}: {m['n']} шт · {m['sum']}₽")

                pay_lines = []
                for r in rows:
                    username = (r['username'] or "").strip()
                    full_name = (r['full_name'] or "").strip()
                    uid = r['user_id']
                    if username:
                        uname = f"@{username}"
                    elif full_name:
                        uname = f"<a href='tg://user?id={uid}'>{full_name}</a>"
                    else:
                        uname = f"ID <code>{uid}</code>"
                    dt = str(r['created_at'])[:16] if r['created_at'] else "-"
                    emoji = "🏦" if r['method'] == "freekassa" else ("⭐" if r['method'] == "stars" else "💳")

                    # Подтягиваем детали FreeKassa (метод + промокод) если есть
                    details = ""
                    if r['method'] == "freekassa":
                        try:
                            fk_row = await conn.fetchrow(
                                """SELECT payment_method, promo_code FROM fk_orders
                                   WHERE user_id=$1 AND amount_rub=$2
                                     AND ABS(EXTRACT(EPOCH FROM (created_at - $3::timestamp))) < 600
                                   ORDER BY created_at DESC LIMIT 1""",
                                uid, r['amount_rub'], r['created_at']
                            )
                            if fk_row:
                                pm = fk_row['payment_method'] or ""
                                pc = fk_row['promo_code']
                                if pm == "card":
                                    details += " · 💳"
                                elif pm == "sbp":
                                    details += " · 🏦"
                                if pc:
                                    details += f" · 🎟 {pc}"
                        except Exception:
                            pass

                        # Определяем категорию платежа: магазин или кредиты
                    is_shop = False
                    try:
                        fk_pack = await conn.fetchval(
                            "SELECT pack FROM fk_orders WHERE user_id=$1 AND amount_rub=$2 "
                            "AND ABS(EXTRACT(EPOCH FROM (created_at - $3::timestamp))) < 600 "
                            "ORDER BY created_at DESC LIMIT 1",
                            uid, r['amount_rub'], r['created_at']
                        )
                        if fk_pack and str(fk_pack).startswith("shop:"):
                            is_shop = True
                    except Exception:
                        pass
                    cat_tag = " 🛍" if is_shop else " 💳"

                    pay_lines.append(
                        f"{emoji} {uname} · <b>{r['amount_rub']}₽</b>{cat_tag} · +{r['credits']} кр{details} · {dt}"
                    )

                cr_n_all = cat_cr[0] or 0
                cr_s_all = cat_cr[1] or 0
                sh_n_all = cat_sh[0] or 0
                sh_s_all = cat_sh[1] or 0

                text = (
                    f"🧾 <b>История платежей</b>\n\n"
                    f"📊 <b>Всего:</b> {total_count} платежей · {total_sum}₽ · {total_credits} кр\n"
                    f"  ├ 💳 Кредиты: <b>{cr_n_all} шт · {cr_s_all}₽</b>\n"
                    f"  └ 🛍 Магазин: <b>{sh_n_all} шт · {sh_s_all}₽</b>\n"
                    f"📅 За сутки: {today_row[0]} · {today_row[1]}₽\n"
                    f"📆 За 7 дней: {week_row[0]} · {week_row[1]}₽\n\n"
                    f"<b>По методам:</b>\n" + "\n".join(method_lines) + "\n\n"
                    f"<b>Страница {page+1}/{max_page+1}:</b>\n" + "\n".join(pay_lines)
                )

                nav = []
                if page > 0:
                    nav.append(InlineKeyboardButton(text="◀️", callback_data=f"adm_pay_p:{page-1}"))
                nav.append(InlineKeyboardButton(text=f"{page+1}/{max_page+1}", callback_data="noop"))
                if page < max_page:
                    nav.append(InlineKeyboardButton(text="▶️", callback_data=f"adm_pay_p:{page+1}"))

                kb_rows = []
                if nav:
                    kb_rows.append(nav)
                kb_rows.append([InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")])

        try:
            await cb.message.edit_text(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            await cb.message.answer(
                text,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


async def check_expiring_credits(user_id: int):
    """Проверяет истекающие кредиты и шлёт уведомление если нужно.
    Вызывается после каждой генерации."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        # Кредиты которые истекут в течение 3 дней
        expiring = await conn.fetchval(
            """SELECT COALESCE(SUM(credits_left), 0) FROM credit_batches
               WHERE user_id=$1 AND credits_left > 0
               AND expires_at IS NOT NULL
               AND expires_at > NOW()
               AND expires_at < NOW() + INTERVAL '3 days'""",
            user_id
        ) or 0
        total = await conn.fetchval(
            """SELECT COALESCE(SUM(credits_left), 0) FROM credit_batches
               WHERE user_id=$1 AND credits_left > 0
               AND (expires_at IS NULL OR expires_at > NOW())""",
            user_id
        ) or 0
        # Когда именно истекают
        nearest = await conn.fetchval(
            """SELECT MIN(expires_at) FROM credit_batches
               WHERE user_id=$1 AND credits_left > 0
               AND expires_at IS NOT NULL AND expires_at > NOW()
               AND expires_at < NOW() + INTERVAL '3 days'""",
            user_id
        )
    if expiring > 0 and nearest:
        days_left = (nearest - __import__('datetime').datetime.now()).days + 1
        days_left = max(1, days_left)
        try:
            await bot.send_message(
                user_id,
                f"⏰ <b>Напоминание о кредитах</b>\n\n"
                f"У тебя <b>{expiring} кредитов</b> сгорит через <b>{days_left} дн.</b>\n"
                f"Всего на балансе: {total} кр\n\n"
                f"Успей использовать или пополни баланс 👇",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🎨 Генерировать", callback_data="menu_image")],
                    [_eib("Купить кредиты", "menu_buy")],
                ])
            )
        except Exception:
            pass


# ─── АПСКЕЙЛ ФОТО ──────────────────────────────────────────────────────────────

async def webapp_chatgpt_handler(request: web.Request) -> web.Response:
    try:
        with open(_WEBAPP_HTML_PATH, "r", encoding="utf-8") as _f:
            _html = _f.read()
        return web.Response(text=_html, content_type="text/html", charset="utf-8")
    except FileNotFoundError:
        return web.Response(text="Mini App not found", status=404)

async def _run_activation_job(
    job_id: str, code: str, access_token: str,
    user_id: int, order_id: str, plan_name: str
):
    """Фоновая задача: Playwright-активация. Не держит HTTP-соединение."""
    try:
        # ── Тестовый режим: код начинается с TEST → пропускаем Playwright ────
        if code.startswith("TEST-"):
            await asyncio.sleep(3)  # имитируем задержку активации
            await delete_pending_activation(user_id)
            _activation_jobs[job_id] = {"status": "done", "success": True}
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"🧪 <b>Тест активации завершён</b>\n"
                    f"👤 <code>{user_id}</code> — тестовый код, нигде не записан.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return
        # ─────────────────────────────────────────────────────────────────────
        result = await activate_chatgpt(code, access_token)

        # Если код уже использован на 987ai.vip — берём следующий свободный и повторяем
        if not result.get("success") and result.get("code_already_used"):
            _bad_code = code
            _plan_key = plan_name_to_key(plan_name)
            logging.warning(f"Код {_bad_code} уже использован, ищем следующий (plan={_plan_key})")

            # Помечаем плохой код как постоянно использованный (не возвращаем в пул)
            try:
                _pool2 = await get_pool()
                async with _pool2.acquire() as _conn2:
                    await _conn2.execute(
                        "UPDATE gpt_codes SET is_used=TRUE, used_by=$1, used_at=NOW() "
                        "WHERE code=$2",
                        user_id, _bad_code
                    )
            except Exception as _e:
                logging.error(f"Не удалось пометить плохой код {_bad_code}: {_e}")

            new_code = await get_next_gpt_code(_plan_key)
            if new_code:
                logging.info(f"Новый код для {user_id}: {new_code}")
                await save_pending_activation(user_id, new_code, order_id, _plan_key, plan_name)
                code = new_code
                try:
                    await bot.send_message(
                        user_id,
                        "🔄 Первый код занят — автоматически выдаю следующий, подожди немного...",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                result = await activate_chatgpt(code, access_token)
            else:
                _activation_jobs[job_id] = {
                    "status": "done", "success": False,
                    "error": "Коды временно закончились. Александр активирует вручную в течение часа 🙌"
                }
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"🚨 <b>Коды {_plan_key} закончились!</b>\n\n"
                        f"👤 <code>{user_id}</code> ({plan_name}) ждёт активации.\n"
                        f"Добавь коды: /add_gpt_codes",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                return

        if result.get("success"):
            _email = _extract_email_from_token(access_token)
            await mark_gpt_code_used(code, user_id, order_id, _email)
            await delete_pending_activation(user_id)
            # Заменяем сообщение клиента на поздравление и убираем кнопку «Нужна помощь»
            _mid = _gpt_act_msg.pop(user_id, None)
            if _mid:
                try:
                    import datetime as _dt_end
                    _end = (_dt_end.datetime.now(_BOT_TZ) + _dt_end.timedelta(days=_subscription_days(plan_name))).strftime("%d.%m.%Y")
                    _prof_kw = ({"icon_custom_emoji_id": UI_EMOJI_IDS["menu_profile"]}
                                if UI_EMOJI_IDS.get("menu_profile") else {})
                    _email_disp = _email or "\u2014"
                    await bot.edit_message_text(
                        "\U0001f389 <b>\u041f\u043e\u0434\u043f\u0438\u0441\u043a\u0430 ChatGPT \u0430\u043a\u0442\u0438\u0432\u0438\u0440\u043e\u0432\u0430\u043d\u0430!</b>\n\n"
                        f"\U0001f4e6 \u0422\u0430\u0440\u0438\u0444: <b>{plan_name}</b>\n"
                        f"\U0001f4e7 \u0410\u043a\u043a\u0430\u0443\u043d\u0442: <b>{_email_disp}</b>\n"
                        f"\U0001f511 \u041a\u043b\u044e\u0447: <code>{code}</code>\n"
                        f"\U0001f4c5 \u0414\u0435\u0439\u0441\u0442\u0432\u0443\u0435\u0442 \u0434\u043e: <b>{_end}</b>\n\n"
                        "\u0421\u043f\u0430\u0441\u0438\u0431\u043e \u0437\u0430 \u043f\u043e\u043a\u0443\u043f\u043a\u0443! \U0001f64c",
                        chat_id=user_id, message_id=_mid, parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="\u041c\u043e\u0439 \u043f\u0440\u043e\u0444\u0438\u043b\u044c", callback_data="menu_profile", **_prof_kw)],
                            [_eib("\u0413\u043b\u0430\u0432\u043d\u043e\u0435 \u043c\u0435\u043d\u044e", "back_main")],
                        ])
                    )
                except Exception as _ee:
                    logging.warning(f"edit gpt activation msg failed: {_ee}")
            try:
                import datetime as _dt
                _used_at = _dt.datetime.now(_BOT_TZ).strftime("%d.%m.%Y %H:%M")
                # Получаем username и full_name клиента из БД
                try:
                    _pool = await get_pool()
                    async with _pool.acquire() as _conn:
                        _urow = await _conn.fetchrow(
                            "SELECT username, full_name FROM users WHERE user_id=$1", user_id
                        )
                    _username  = _urow["username"]  if _urow and _urow["username"]  else ""
                    _full_name = _urow["full_name"] if _urow and _urow["full_name"] else ""
                except Exception:
                    _username = _full_name = ""
                _tg_name = (f"@{_username}" if _username else _full_name) or f"id{user_id}"
                _caption = (
                    f"✅ <b>ChatGPT авто-активация OK</b>\n\n"
                    f"👤 Клиент: <b>{_tg_name}</b>  (<code>{user_id}</code>)\n"
                    f"📧 Email: <b>{_email or '—'}</b>\n"
                    f"🔑 Код: <code>{code}</code>\n"
                    f"📦 Тариф: <b>{plan_name}</b>\n"
                    f"⏱ Время: <b>{_used_at}</b>\n"
                    f"🆔 Order: <code>{order_id}</code>"
                )
                _screenshot = result.get("screenshot")
                if _screenshot:
                    await bot.send_photo(ADMIN_ID, BufferedInputFile(_screenshot, "ok.png"),
                                         caption=_caption, parse_mode="HTML")
                else:
                    await bot.send_message(ADMIN_ID, _caption, parse_mode="HTML")
            except Exception:
                pass
            _activation_jobs[job_id] = {"status": "done", "success": True}
        else:
            error_text = result.get("error", "Ошибка активации")
            _plan_key = plan_name_to_key(plan_name)
            import urllib.parse as _uparse2
            from aiogram.types import WebAppInfo as _WebAppInfo

            # ── Тип ошибки определяет что делать дальше ──────────────────────
            _token_invalid = result.get("token_invalid", False)

            if _token_invalid:
                # Токен от клиента невалидный/истёк — код не трогаем, просим переcкопировать
                # Pending остаётся с тем же кодом, сессия жива
                _same_url = (
                    f"{WEBAPP_BASE_URL}/webapp/chatgpt"
                    f"?plan={_uparse2.quote(plan_name)}&code={_uparse2.quote(code)}"
                )
                try:
                    await bot.send_message(
                        user_id,
                        "❌ <b>Токен недействителен или истёк</b>\n\n"
                        "Токен нужно скопировать заново — он обновляется после каждого входа в ChatGPT.\n\n"
                        "<b>Как получить новый токен:</b>\n"
                        "1. Зайди на <b>chatgpt.com</b> и войди в аккаунт\n"
                        "2. Открой <b>chatgpt.com/api/auth/session</b>\n"
                        "3. Скопируй весь текст целиком и вставь в форму\n\n"
                        "👇 Нажми кнопку ниже и попробуй снова",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(
                                text="🔄 Ввести токен заново",
                                web_app=_WebAppInfo(url=_same_url)
                            )],
                            [InlineKeyboardButton(
                                text="❓ Нужна помощь",
                                callback_data="gpt_need_help"
                            )],
                        ])
                    )
                except Exception as _te:
                    logging.error(f"Token invalid message failed: {_te}")

            else:
                # Другая ошибка (таймаут, сеть, неизвестное) — код остаётся тем же
                # Считаем попытки только для не-токенных ошибок
                _gpt_retry_counts.setdefault(user_id, 0)
                _gpt_retry_counts[user_id] += 1
                attempt = _gpt_retry_counts[user_id]
                MAX_RETRIES = 3

                if attempt < MAX_RETRIES:
                    # Pending остаётся, код тот же — клиент просто повторяет
                    _same_url = (
                        f"{WEBAPP_BASE_URL}/webapp/chatgpt"
                        f"?plan={_uparse2.quote(plan_name)}&code={_uparse2.quote(code)}"
                    )
                    try:
                        await bot.send_message(
                            user_id,
                            f"⚠️ <b>Попытка {attempt} из {MAX_RETRIES} не удалась</b>\n\n"
                            f"{error_text}\n\n"
                            f"Попробуй ещё раз 👇",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="🔄 Повторить активацию",
                                    web_app=_WebAppInfo(url=_same_url)
                                )],
                                [InlineKeyboardButton(
                                    text="❓ Нужна помощь",
                                    callback_data="gpt_need_help"
                                )],
                            ])
                        )
                    except Exception as _re:
                        logging.error(f"Retry message failed: {_re}")
                else:
                    # Исчерпаны авто-попытки. ВАЖНО: код и pending НЕ трогаем —
                    # чтобы клиент не упирался в «Время сессии истекло», а Александр
                    # мог активировать вручную тем же кодом. Код освободится сам через
                    # ~2 часа (gpt_codes_cleanup_loop), если активация так и не случится.
                    _gpt_retry_counts.pop(user_id, None)
                    try:
                        await bot.send_message(
                            user_id,
                            f"😔 <b>Не удалось активировать после {MAX_RETRIES} попыток</b>\n\n"
                            f"Напиши Александру — активирую вручную в течение 15–30 минут!",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="💬 Написать Александру",
                                    url=f"https://t.me/{PERSONAL_USERNAME}"
                                )],
                            ])
                        )
                    except Exception:
                        pass
                    # Уведомляем Александра С КОДОМ — чтобы активировал вручную
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            "🚨 <b>Авто-активация ChatGPT не удалась</b>\n\n"
                            f"👤 <code>{user_id}</code>\n"
                            f"🔑 Код: <code>{code}</code>\n"
                            f"📦 Тариф: <b>{plan_name}</b>\n"
                            f"🆔 Заказ: <code>{order_id}</code>\n"
                            f"⚠️ Ошибка: {error_text}\n\n"
                            "Код зарезервирован за клиентом — активируй вручную ИМ ЖЕ.",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
                    _activation_jobs[job_id] = {
                        "status": "done", "success": False,
                        "error": f"Не удалось после {MAX_RETRIES} попыток. Напиши @{PERSONAL_USERNAME}"
                    }
                    return  # выходим, не перезаписываем job ниже

            _activation_jobs[job_id] = {"status": "done", "success": False, "error": error_text}
            try:
                import datetime as _dt
                _fail_at = _dt.datetime.now(_BOT_TZ).strftime("%d.%m.%Y %H:%M")
                try:
                    _pool2 = await get_pool()
                    async with _pool2.acquire() as _conn2:
                        _urow2 = await _conn2.fetchrow(
                            "SELECT username, full_name FROM users WHERE user_id=$1", user_id
                        )
                    _un2 = _urow2["username"]  if _urow2 and _urow2["username"]  else ""
                    _fn2 = _urow2["full_name"] if _urow2 and _urow2["full_name"] else ""
                except Exception:
                    _un2 = _fn2 = ""
                _tg2 = (f"@{_un2}" if _un2 else _fn2) or f"id{user_id}"
                screenshot = result.get("screenshot")
                txt = (
                    f"❌ <b>ChatGPT авто-активация НЕУДАЧА</b>\n\n"
                    f"👤 Клиент: <b>{_tg2}</b>  (<code>{user_id}</code>)\n"
                    f"🔑 Код: <code>{code}</code>\n"
                    f"📦 Тариф: <b>{plan_name}</b>\n"
                    f"⏱ Время: <b>{_fail_at}</b>\n"
                    f"❗ {error_text}\n"
                    f"🔄 Код возвращён в пул."
                )
                if screenshot:
                    await bot.send_photo(ADMIN_ID, BufferedInputFile(screenshot, "err.png"),
                                         caption=txt, parse_mode="HTML")
                else:
                    await bot.send_message(ADMIN_ID, txt, parse_mode="HTML")
            except Exception:
                pass
    except Exception as e:
        logging.error(f"_run_activation_job {job_id}: {e}", exc_info=True)
        _activation_jobs[job_id] = {
            "status": "done", "success": False,
            "error": "Внутренняя ошибка сервера. Напиши Александру."
        }


# Клиенты, уже предупреждённые о повторной активации (in-memory, сбрасывается при рестарте).
# Повторное нажатие «Попробовать снова» = принудительная активация (на другой аккаунт).
_gpt_double_warned: set = set()
_claude_double_warned: set = set()
# message_id активационного сообщения клиента (чтобы заменить на поздравление после успеха)
_gpt_act_msg: dict = {}
_claude_act_msg: dict = {}
# Claude: заказы, которым уже выдали авто-замену кода после жёсткого сбоя (кап = 1 раз/заказ)
_claude_replaced_orders: set = set()
_perplexity_double_warned: set = set()
_perplexity_act_msg: dict = {}
_perplexity_replaced_orders: set = set()
_perplexity_job_results: dict = {}
_PERPLEXITY_WEBAPP_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "perplexity_webapp.html")

# ── Таймер 2 часов на сообщение самостоятельной активации (GPT/Claude) ──
ACTIVATION_WINDOW_MIN = 120

def _activation_timer_line(deadline) -> str:
    import datetime as _dtt
    rem = int((deadline - _dtt.datetime.now(_BOT_TZ)).total_seconds() // 60)
    if rem <= 0:
        return ""
    h, m = divmod(rem, 60)
    if h and m:
        r = f"~{h} ч {m} мин"
    elif h:
        r = f"~{h} ч"
    else:
        r = f"~{max(1, m)} мин"
    return (f"\n\n⏳ На самостоятельную активацию осталось <b>{r}</b> "
            f"(до {deadline.strftime('%H:%M')}).")

async def _activation_timer_job(user_id, message_id, base_text, kb_active,
                                deadline, expired_text, kb_expired, act_store):
    """Обновляет таймер на сообщении активации; при истечении меняет текст и убирает кнопку активации."""
    import datetime as _dtt
    while True:
        secs = (deadline - _dtt.datetime.now(_BOT_TZ)).total_seconds()
        if secs <= 0:
            break
        await asyncio.sleep(min(1800, secs))
        if act_store.get(user_id) != message_id:
            return  # уже активировано / сообщение заменено
        if (deadline - _dtt.datetime.now(_BOT_TZ)).total_seconds() <= 0:
            break
        try:
            await bot.edit_message_text(
                base_text + _activation_timer_line(deadline),
                chat_id=user_id, message_id=message_id,
                parse_mode="HTML", reply_markup=kb_active)
        except Exception:
            pass
    if act_store.get(user_id) != message_id:
        return
    act_store.pop(user_id, None)
    try:
        await bot.edit_message_text(
            expired_text, chat_id=user_id, message_id=message_id,
            parse_mode="HTML", reply_markup=kb_expired)
    except Exception:
        pass


def _subscription_days(plan_name: str) -> int:
    """Срок подписки в днях по названию тарифа: годовой → 365, иначе 30."""
    _n = (plan_name or "").lower()
    if any(k in _n for k in ("год", "year", "annual", "ежегод", "12 мес", "12мес")):
        return 365
    return 30


async def api_activate_chatgpt_handler(request: web.Request) -> web.Response:
    """POST /api/activate-chatgpt — запускает задачу в фоне, сразу возвращает job_id."""
    import json as _json
    def _resp(data, status=200):
        return web.Response(text=_json.dumps(data, ensure_ascii=False),
                            content_type="application/json", status=status)
    try:
        body = await request.json()
    except Exception:
        return _resp({"success": False, "error": "Неверный формат запроса"}, 400)

    access_token = (body.get("token") or "").strip()
    init_data    = (body.get("init_data") or "").strip()

    user_id = _verify_tg_init_data(init_data)
    if not user_id:
        return _resp({"success": False, "error": "Ошибка авторизации. Перезапусти мини-приложение."}, 403)
    if not access_token.startswith("eyJ") or len(access_token) < 100:
        return _resp({"success": False, "error": "Некорректный токен. Скопируй текст со страницы ещё раз."})

    pending = await get_pending_activation(user_id)
    if not pending:
        return _resp({"success": False, "error": f"Время сессии истекло. Напиши @{PERSONAL_USERNAME}"})

    # Guard: повторная активация за 35 дней — НЕ блокируем жёстко.
    # Первый раз предупреждаем, повторное нажатие «Попробовать снова» = активируем
    # принудительно (клиент может оформлять подписку на другой аккаунт, напр. другу).
    try:
        _pool_dbl = await get_pool()
        async with _pool_dbl.acquire() as _c_dbl:
            _recent_act = await _c_dbl.fetchrow(
                "SELECT code, plan, used_at, email FROM gpt_codes"
                " WHERE used_by=$1 AND used_at > NOW() - INTERVAL '35 days'"
                " AND used_by IS NOT NULL ORDER BY used_at DESC LIMIT 1",
                user_id
            )
        if _recent_act:
            _us = _recent_act["used_at"].strftime("%d.%m.%Y %H:%M") if _recent_act["used_at"] else "-"
            _u = await get_user(user_id)
            if _u and _u.get("username"):
                _uname = "@" + _u["username"]
            elif _u and _u.get("full_name"):
                _uname = _u["full_name"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            else:
                _uname = "\u0431\u0435\u0437 \u043d\u0438\u043a\u0430"
            if user_id not in _gpt_double_warned:
                _gpt_double_warned.add(user_id)
                logging.warning(f'GPT repeat activation: warned user={user_id} code={_recent_act["code"]}')
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        "\u26a0\ufe0f <b>\u041f\u043e\u0432\u0442\u043e\u0440\u043d\u0430\u044f \u0430\u043a\u0442\u0438\u0432\u0430\u0446\u0438\u044f ChatGPT</b> (\u043a\u043b\u0438\u0435\u043d\u0442 \u043f\u0440\u0435\u0434\u0443\u043f\u0440\u0435\u0436\u0434\u0451\u043d)\n\n"
                        f"\U0001f464 {_uname} (<code>{user_id}</code>)\n"
                        f"\U0001f511 \u0423\u0436\u0435 \u0430\u043a\u0442\u0438\u0432\u0438\u0440\u043e\u0432\u0430\u043d: <code>{_recent_act['code']}</code>\n"
                        f"\U0001f4e6 \u0422\u0430\u0440\u0438\u0444: <b>{_recent_act['plan']}</b>\n"
                        f"\u23f1 \u0414\u0430\u0442\u0430: <b>{_us}</b>\n"
                        f"\U0001f4e7 Email: {_recent_act.get('email') or '-'}\n\n"
                        "\u0415\u0441\u043b\u0438 \u043d\u0430\u0436\u043c\u0451\u0442 \u00ab\u041f\u043e\u043f\u0440\u043e\u0431\u043e\u0432\u0430\u0442\u044c \u0441\u043d\u043e\u0432\u0430\u00bb \u2014 \u0430\u043a\u0442\u0438\u0432\u0438\u0440\u0443\u0435\u0442 \u043d\u0430 \u0434\u0440\u0443\u0433\u043e\u0439 \u0430\u043a\u043a\u0430\u0443\u043d\u0442.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                return _resp({"success": False, "error": (
                    "\u26a0\ufe0f \u041d\u0430 \u0442\u0432\u043e\u0439 \u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u0443\u0436\u0435 \u0430\u043a\u0442\u0438\u0432\u0438\u0440\u043e\u0432\u0430\u043d\u0430 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0430 ChatGPT.\n\n"
                    "\u0415\u0441\u043b\u0438 \u043e\u0444\u043e\u0440\u043c\u043b\u044f\u0435\u0448\u044c \u043d\u0430 \u0414\u0420\u0423\u0413\u041e\u0419 \u0430\u043a\u043a\u0430\u0443\u043d\u0442 (\u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440, \u0434\u043b\u044f \u0434\u0440\u0443\u0433\u0430) \u2014 \u043d\u0430\u0436\u043c\u0438 \u00ab\u041f\u043e\u043f\u0440\u043e\u0431\u043e\u0432\u0430\u0442\u044c \u0441\u043d\u043e\u0432\u0430\u00bb, \u0438 \u0430\u043a\u0442\u0438\u0432\u0430\u0446\u0438\u044f \u043f\u0440\u043e\u0439\u0434\u0451\u0442.\n\n"
                    f"\u0415\u0441\u043b\u0438 \u044d\u0442\u043e \u0441\u043b\u0443\u0447\u0430\u0439\u043d\u043e \u2014 \u043d\u0430\u043f\u0438\u0448\u0438 @{PERSONAL_USERNAME}."
                )})
            else:
                _gpt_double_warned.discard(user_id)
                logging.info(f"GPT forced re-activation user={user_id}")
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        "\u2705 <b>\u041f\u043e\u0432\u0442\u043e\u0440\u043d\u0430\u044f \u0430\u043a\u0442\u0438\u0432\u0430\u0446\u0438\u044f ChatGPT \u2014 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430</b>\n\n"
                        f"\U0001f464 {_uname} (<code>{user_id}</code>) \u0430\u043a\u0442\u0438\u0432\u0438\u0440\u0443\u0435\u0442 \u0435\u0449\u0451 \u0440\u0430\u0437 (\u0434\u0440\u0443\u0433\u043e\u0439 \u0430\u043a\u043a\u0430\u0443\u043d\u0442).",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    except Exception as _dbl_e:
        logging.error(f'double-activation check: {_dbl_e}')


    # Всегда используем pending["code"] — он актуальный даже после retry
    # URL-код от клиента игнорируем — pending является авторитетным источником
    code = pending["code"]
    order_id  = pending["order_id"]
    plan_name = pending["plan_name"]

    job_id = str(uuid.uuid4())[:12]
    _activation_jobs[job_id] = {"status": "pending"}
    asyncio.create_task(
        _run_activation_job(job_id, code, access_token, user_id, order_id, plan_name)
    )
    logging.info(f"ChatGPT activation started: job={job_id} user={user_id} code={code}")
    return _resp({"job_id": job_id, "status": "started"})


async def api_activation_status_handler(request: web.Request) -> web.Response:
    """GET /api/activate-status/{job_id} — статус задачи активации."""
    import json as _json
    job_id = request.match_info.get("job_id", "")
    job    = _activation_jobs.get(job_id)
    if not job:
        return web.Response(
            text=_json.dumps({"status": "not_found", "error": "Задача не найдена"}),
            content_type="application/json", status=404
        )
    return web.Response(text=_json.dumps(job, ensure_ascii=False), content_type="application/json")



async def fk_credit_paid_order(order_id: str, payment: dict, source: str = "webhook") -> bool:
    """Зачисляет кредиты по оплаченному заказу.

    Используется и в webhook, и в авто-проверке FK API.
    Защищена от двойного зачисления через fk_mark_paid (атомарная операция в БД).

    Args:
        order_id: ID заказа в FK
        payment: dict с полями user_id, credits, amount, [promo_code]
        source: "webhook" или "auto_check" - для логирования

    Returns: True если зачислили, False если уже было зачислено
    """
    user_id    = payment["user_id"]
    credits    = payment["credits"]
    amount_rub = payment["amount"]

    # 1. Атомарно помечаем заказ как paid - если уже было paid, mark_paid вернёт False
    was_marked = await fk_mark_paid(order_id)
    if not was_marked:
        # Уже зачислено другим путём
        logging.info(f"FK order {order_id} already paid (source={source})")
        return False

    # 2. Зачисляем кредиты партией (на 30 дней) и логируем
    await add_credits_batch(user_id, credits, source="purchase", days_valid=30)
    await log_payment(user_id, credits, int(amount_rub), "freekassa")
    await process_referral_bonus(user_id)
    await process_premium_referral(user_id, order_id, amount_rub)

    # Если был промокод - инкрементим используемость
    promo_code = payment.get("promo_code") if isinstance(payment, dict) else None
    if promo_code:
        try:
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO promo_uses (code, user_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                    promo_code, user_id
                )
                await conn.execute(
                    "UPDATE promocodes SET used_count = used_count + 1 WHERE code=$1",
                    promo_code
                )
            await log_event(user_id, "promo_used_purchase", f"code={promo_code}")
        except Exception as e:
            logging.error(f"promo apply on purchase: {e}")

    # 3. Уведомляем пользователя в Telegram
    try:
        # Считаем баланс ДО зачисления чтобы показать сколько прибавилось
        new_balance = await get_credits(user_id)
        old_balance = max(0, new_balance - credits)

        # Дополнительное сообщение если зачисление не через webhook (была задержка)
        delayed_note = ""
        if source in ("auto_check", "manual_check"):
            delayed_note = "\n\n<i>⚠️ Платёж был обработан с небольшой задержкой - извини за неудобство 🙏</i>"

        # Метод оплаты для сообщения
        db_order_for_msg = await fk_get_order(order_id)
        method_used_msg = (db_order_for_msg or {}).get("payment_method", "sbp") if db_order_for_msg else "sbp"

        # Определяем тип заказа - магазин или кредиты
        # NS Gifts (App Store / iCloud) — мгновенная доставка кода
        if order_id.startswith("nsg_"):
            await nsgifts_fulfill_after_payment(order_id, user_id)
            return True

        is_shop_order = order_id.startswith("shop_")
        pack_info = (db_order_for_msg or {}).get("pack", "") if db_order_for_msg else pack

        if is_shop_order:
            # Заказ из магазина - показываем информацию о товаре
            shop_key = pack_info.split(":")[1] if pack_info and ":" in pack_info else ""
            plan_idx = int(pack_info.split(":")[2]) if pack_info and pack_info.count(":") >= 2 else 0
            s = SHOP_CATALOG.get(shop_key, {})
            plans = s.get("plans", [])
            p = plans[plan_idx] if plan_idx < len(plans) else {}
            service_name = f"{s.get('emoji', '')} {s.get('name', '')} - {p.get('name', '')}" if s else "Товар из магазина"

            # Автоматически создаём подписку на 1 месяц
            import datetime as _dt2
            try:
                expires_at = _dt2.datetime.now() + _dt2.timedelta(days=30)
                svc_display = f"{s.get('name', shop_key)}"
                plan_display = p.get("name", "")
                pool_sub = await get_pool()
                async with pool_sub.acquire() as conn_sub:
                    await conn_sub.execute("""
                        INSERT INTO user_subscriptions
                        (user_id, service_key, service_name, plan_name, expires_at, created_by)
                        VALUES ($1,$2,$3,$4,$5,$6)
                        ON CONFLICT DO NOTHING
                    """, user_id, shop_key, svc_display, plan_display, expires_at, 0)
                logging.info(f"Подписка создана: user={user_id} svc={shop_key} до {expires_at.date()}")
            except Exception as sub_err:
                logging.error(f"Ошибка создания подписки: {sub_err}")

            if shop_key == "chatgpt":
                import urllib.parse as _uparse
                _plan_name = p.get("name", "Plus")
                if not rt.chatgpt_webapp_enabled:
                    await bot.send_message(
                        user_id,
                        f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                        f"📦 <b>{service_name}</b> — {amount_rub}₽\n\n"
                        f"🔧 Сейчас ведутся технические работы. "
                        f"Александр активирует подписку вручную в течение часа 🙌{delayed_note}",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="❓ Написать Александру",
                                url=f"https://t.me/{PERSONAL_USERNAME}")],
                        ])
                    )
                if not rt.chatgpt_webapp_enabled:
                    await bot.send_message(
                        user_id,
                        f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                        f"📦 <b>{service_name}</b> — {amount_rub}₽\n\n"
                        f"🔧 Сейчас ведутся технические работы. "
                        f"Александр активирует подписку вручную в течение часа 🙌{delayed_note}",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="❓ Написать Александру",
                                url=f"https://t.me/{PERSONAL_USERNAME}")],
                        ])
                    )
                    await bot.send_message(
                        ADMIN_ID,
                        f"🛍 <b>Заказ ChatGPT (ручная активация)</b>\n"
                        f"👤 <code>{user_id}</code>  📦 {service_name}\n"
                        f"💵 {amount_rub}₽  🆔 <code>{order_id}</code>",
                        parse_mode="HTML"
                    )
                    return
                _plan_key  = plan_name_to_key(_plan_name)
                _code      = await get_next_gpt_code(_plan_key)
                if _code is None:
                    await bot.send_message(
                        user_id,
                        f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                        f"📦 <b>{service_name}</b> — {amount_rub}₽\n\n"
                        f"⚠️ Коды временно закончились. Александр активирует вручную в течение часа 🙌"
                        f"{delayed_note}", parse_mode="HTML")
                    await bot.send_message(
                        ADMIN_ID,
                        f"🚨 <b>КОДЫ ChatGPT ЗАКОНЧИЛИСЬ!</b>\n"
                        f"Заказ <code>{order_id}</code> — активируй вручную!\n"
                        f"Добавь коды: /add_gpt_codes", parse_mode="HTML")
                else:
                    await save_pending_activation(user_id, _code, order_id, _plan_key, _plan_name)
                    _webapp_url = f"{WEBAPP_BASE_URL}/webapp/chatgpt?plan={_uparse.quote(_plan_name)}&code={_uparse.quote(_code)}"
                    from aiogram.types import WebAppInfo
                    import datetime as _dt_gpt
                    _base_gpt = (
                        f"🎉 <b>Оплата прошла!</b>\n\n"
                        f"📦 <b>{service_name}</b> — {amount_rub}₽\n\n"
                        f"Осталось активировать подписку — нажми кнопку ниже 👇{delayed_note}"
                    )
                    _kb_gpt_active = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✨ Активировать подписку",
                                              web_app=WebAppInfo(url=_webapp_url))],
                        [InlineKeyboardButton(text="❓ Нужна помощь",
                                              callback_data="gpt_need_help")],
                    ])
                    _kb_gpt_expired = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="❓ Нужна помощь",
                                              callback_data="gpt_need_help")],
                    ])
                    _dl_gpt = _dt_gpt.datetime.now(_BOT_TZ) + _dt_gpt.timedelta(minutes=ACTIVATION_WINDOW_MIN)
                    _exp_gpt = (
                        f"⏰ <b>Время самостоятельной активации истекло</b>\n\n"
                        f"📦 <b>{service_name}</b> — оплата сохранена.\n"
                        f"Напиши Александру — активирую вручную 🙌"
                    )
                    _m_act_gpt = await bot.send_message(
                        user_id, _base_gpt + _activation_timer_line(_dl_gpt),
                        parse_mode="HTML", reply_markup=_kb_gpt_active)
                    _gpt_act_msg[user_id] = _m_act_gpt.message_id
                    asyncio.create_task(_activation_timer_job(
                        user_id, _m_act_gpt.message_id, _base_gpt, _kb_gpt_active,
                        _dl_gpt, _exp_gpt, _kb_gpt_expired, _gpt_act_msg))
            elif shop_key == "claude":
                # ── Авто-активация Claude через bypriceactivate.pro Mini App ──
                _plan_name_cl = p.get("name", "Pro")
                _plan_key_cl = {
                    "Pro":     "pro",
                    "Max 5×":  "max_5x",
                    "Max 20×": "max_20x",
                }.get(_plan_name_cl, "pro")
                if not rt.claude_webapp_enabled:
                    await bot.send_message(
                        user_id,
                        f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                        f"📦 <b>{service_name}</b> — {amount_rub}₽\n\n"
                        f"Александр активирует Claude вручную в течение часа 🙌"
                        f"{delayed_note}",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(
                                text="❓ Написать Александру",
                                url=f"https://t.me/{PERSONAL_USERNAME}"
                            )],
                        ])
                    )
                    await bot.send_message(
                        ADMIN_ID,
                        f"🛍 <b>Claude (ручная активация)</b>\n"
                        f"👤 <code>{user_id}</code>  📦 {service_name}\n"
                        f"💵 {amount_rub}₽  🆔 <code>{order_id}</code>",
                        parse_mode="HTML"
                    )
                else:
                    _code_cl = await get_next_claude_code(_plan_key_cl)
                    if _code_cl is None:
                        await bot.send_message(
                            user_id,
                            f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                            f"📦 <b>{service_name}</b> — {amount_rub}₽\n\n"
                            f"⚠️ Коды временно закончились. "
                            f"Александр активирует вручную в течение часа 🙌"
                            f"{delayed_note}",
                            parse_mode="HTML"
                        )
                        await bot.send_message(
                            ADMIN_ID,
                            f"🚨 <b>КОДЫ Claude {_plan_name_cl} ЗАКОНЧИЛИСЬ!</b>\n"
                            f"Заказ <code>{order_id}</code> user=<code>{user_id}</code>\n"
                            f"Пополни коды на bypriceactivate.pro",
                            parse_mode="HTML"
                        )
                    else:
                        await _send_claude_webapp_to_user(
                            user_id=user_id,
                            code=_code_cl,
                            order_id=order_id,
                            plan=_plan_key_cl,
                            plan_name=_plan_name_cl,
                            delayed_note=delayed_note,
                        )
            elif shop_key == "perplexity":
                # ── Авто-активация Perplexity через bypriceactivate.pro Mini App ──
                _plan_name_cl = p.get("name", "Pro")
                _plan_key_cl = "pro"
                if not rt.perplexity_webapp_enabled:
                    await bot.send_message(
                        user_id,
                        f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                        f"📦 <b>{service_name}</b> — {amount_rub}₽\n\n"
                        f"Александр активирует Perplexity вручную в течение часа 🙌"
                        f"{delayed_note}",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(
                                text="❓ Написать Александру",
                                url=f"https://t.me/{PERSONAL_USERNAME}"
                            )],
                        ])
                    )
                    await bot.send_message(
                        ADMIN_ID,
                        f"🛍 <b>Perplexity (ручная активация)</b>\n"
                        f"👤 <code>{user_id}</code>  📦 {service_name}\n"
                        f"💵 {amount_rub}₽  🆔 <code>{order_id}</code>",
                        parse_mode="HTML"
                    )
                else:
                    _code_cl = await get_next_perplexity_code(_plan_key_cl)
                    if _code_cl is None:
                        await bot.send_message(
                            user_id,
                            f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                            f"📦 <b>{service_name}</b> — {amount_rub}₽\n\n"
                            f"⚠️ Коды временно закончились. "
                            f"Александр активирует вручную в течение часа 🙌"
                            f"{delayed_note}",
                            parse_mode="HTML"
                        )
                        await bot.send_message(
                            ADMIN_ID,
                            f"🚨 <b>КОДЫ Perplexity {_plan_name_cl} ЗАКОНЧИЛИСЬ!</b>\n"
                            f"Заказ <code>{order_id}</code> user=<code>{user_id}</code>\n"
                            f"Пополни коды на bypriceactivate.pro",
                            parse_mode="HTML"
                        )
                    else:
                        await _send_perplexity_webapp_to_user(
                            user_id=user_id,
                            code=_code_cl,
                            order_id=order_id,
                            plan=_plan_key_cl,
                            plan_name=_plan_name_cl,
                            delayed_note=delayed_note,
                        )
            elif await _is_linkpay(shop_key):
                await _send_linkpay_instructions(
                    user_id=user_id, shop_key=shop_key,
                    service_name=service_name, plan_name=p.get("name", ""),
                    order_id=order_id, amount_rub=amount_rub, delayed_note=delayed_note,
                )
            elif await _is_creds(shop_key):
                await _send_creds_instructions(
                    user_id=user_id, shop_key=shop_key,
                    service_name=service_name, plan_name=p.get("name", ""),
                    order_id=order_id, amount_rub=amount_rub, delayed_note=delayed_note,
                )
            else:
                await bot.send_message(
                    user_id,
                    f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                    f"━━━━━━━━━━━━━━━━━━━\n"
                    f"📦 <b>Товар:</b> {service_name}\n"
                    f"💵 <b>Сумма:</b> {amount_rub}₽\n"
                    f"💳 <b>Способ оплаты:</b> СБП\n"
                    f"━━━━━━━━━━━━━━━━━━━\n\n"
                    f"🆔 Заказ: <code>{order_id}</code>\n\n"
                    f"Александр свяжется с тобой и активирует подписку в течение часа 🙌\n"
                    f"{delayed_note}\n\n"
                    f"<i>Пока ждёшь - попробуй генерацию фото и видео прямо в боте! 🎨</i>",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎨 Генерировать фото", callback_data="menu_image"),
                         InlineKeyboardButton(text="🎬 Генерировать видео", callback_data="menu_video")],
                        [_eib("Купить кредиты", "menu_buy")],
                        [_eib("Главное меню", "back_main")],
                    ]))
        else:
            # Покупка кредитов - показываем баланс
            await bot.send_message(
                user_id,
                f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"💎 <b>Зачислено:</b> +{credits} кредитов\n"
                f"💵 <b>Баланс:</b> {old_balance} → <b>{new_balance} кр</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n\n"
                f"💳 Способ оплаты: СБП · {amount_rub}₽\n"
                f"🆔 Заказ: <code>{order_id}</code>\n\n"
                f"<i>⏳ Кредиты действуют 30 дней с момента покупки</i>"
                f"{delayed_note}\n\n"
                f"<b>Готов творить? 🚀</b>",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🖼️ Создать фото", callback_data="menu_image"),
                     InlineKeyboardButton(text="🎬 Создать видео", callback_data="menu_video")],
                    [InlineKeyboardButton(text="🤖 AI-Консультант", callback_data="menu_chat")],
                    [_eib("Главное меню", "back_main")],
                ])
            )
        logging.info(f"FK payment success ({source}): user={user_id} credits=+{credits} balance={old_balance}→{new_balance} order={order_id}")
    except Exception as e:
        logging.error(f"FK notify user error ({source}): {e}")

    # 4. Уведомляем админа
    try:
        user_info = await get_user(user_id)
        username = (user_info.get("username") or "").strip() if user_info else ""
        full_name = (user_info.get("full_name") or "").strip() if user_info else ""
        user_label = f"@{username}" if username else (full_name or f"ID {user_id}")

        # Пробуем отредактировать существующее сообщение (если было при создании заказа)
        db_order_admin = await fk_get_order(order_id)
        admin_msg_id = (db_order_admin or {}).get("admin_msg_id") if db_order_admin else None

        is_shop = order_id.startswith("shop_")
        pack_info = (db_order_admin or {}).get("pack", "") if db_order_admin else ""

        if is_shop and pack_info:
            shop_key = pack_info.split(":")[1] if ":" in pack_info else ""
            plan_idx = int(pack_info.split(":")[2]) if pack_info.count(":") >= 2 else 0
            s_cat = SHOP_CATALOG.get(shop_key, {})
            plans = s_cat.get("plans", [])
            p_cat = plans[plan_idx] if plan_idx < len(plans) else {}
            service_name = f"{s_cat.get('emoji', '')} {s_cat.get('name', '')} {p_cat.get('name', '')}" if s_cat else "Товар из магазина"

            admin_msg = (
                f"\u2705 <b>Заказ оплачен!</b>\n\n"
                f"\U0001f464 {user_label} (<code>{user_id}</code>)\n"
                f"\U0001f4e6 {service_name}\n"
                f"\U0001f4b5 Сумма: <b>{amount_rub}\u20bd</b>\n"
                f"💳 \u0421\u043f\u043e\u0441\u043e\u0431: \u0421\u0411\u041f\n"
                f"\U0001f194 \u0417\u0430\u043a\u0430\u0437: <code>{order_id}</code>\n\n"
                f"\u2705 <b>\u0421\u0442\u0430\u0442\u0443\u0441: \u043e\u043f\u043b\u0430\u0447\u0435\u043d</b>"
            )
        else:
            admin_msg = (
                f"\U0001f4b0 <b>\u041e\u043f\u043b\u0430\u0442\u0430 \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u0430!</b>\n\n"
                f"\U0001f464 {user_label} (<code>{user_id}</code>)\n"
                f"\U0001f4b5 \u0421\u0443\u043c\u043c\u0430: <b>{amount_rub}\u20bd</b>\n"
                f"\U0001f48e \u041a\u0440\u0435\u0434\u0438\u0442\u043e\u0432: <b>{credits}</b>\n"
                f"💳 \u0421\u043f\u043e\u0441\u043e\u0431: \u0421\u0411\u041f\n"
                f"\U0001f194 \u0417\u0430\u043a\u0430\u0437: <code>{order_id}</code>\n\n"
                f"\u2705 <b>\u0421\u0442\u0430\u0442\u0443\u0441: \u043e\u043f\u043b\u0430\u0447\u0435\u043d</b>"
            )
        if promo_code:
            admin_msg += f"\n\U0001f39f \u041f\u0440\u043e\u043c\u043e\u043a\u043e\u0434: <code>{promo_code}</code>"

        if admin_msg_id:
            # Редактируем существующее сообщение
            try:
                await bot.edit_message_text(
                    admin_msg, chat_id=ADMIN_ID,
                    message_id=admin_msg_id, parse_mode="HTML"
                )
            except Exception:
                await bot.send_message(ADMIN_ID, admin_msg, parse_mode="HTML")
        else:
            await bot.send_message(ADMIN_ID, admin_msg, parse_mode="HTML")
    except Exception as e:
        logging.error(f"FK admin notify error: {e}")

    return True


async def fk_webhook_handler(request: web.Request) -> web.Response:
    """Принимает уведомление от FreeKassa об успешной оплате."""
    # 0. ПЕРВЫМ ДЕЛОМ - пытаемся распарсить body для логов даже если потом откажем
    raw_body = ""
    try:
        raw_body = await request.text()
    except Exception:
        pass

    # Логируем КАЖДЫЙ запрос к этому endpoint - для диагностики
    logging.info(
        f"📥 FK webhook ВХОД: "
        f"method={request.method} "
        f"remote={request.remote} "
        f"x-forwarded-for={request.headers.get('X-Forwarded-For', 'NONE')} "
        f"body_len={len(raw_body)} "
        f"body={raw_body[:300]}"
    )

    try:
        # Проверяем IP-адрес отправителя
        # Railway использует X-Forwarded-For - берём первый IP в цепочке (реальный клиент)
        client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if not client_ip:
            client_ip = request.remote or ""

        if client_ip not in FK_ALLOWED_IPS:
            if FK_IP_CHECK_DISABLED:
                # Аварийный режим - пропускаем но логируем
                logging.warning(
                    f"FK webhook from IP NOT in whitelist: {client_ip} "
                    f"(но FK_IP_CHECK=disabled - принимаем)"
                )
                # Алертим админа что пришёл с неизвестного IP - может FK обновили список
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"ℹ️ <b>Webhook с неизвестного IP</b>\n\n"
                        f"IP: <code>{client_ip}</code>\n"
                        f"Сейчас принимается (FK_IP_CHECK=disabled).\n"
                        f"Если это реально FK - добавь IP в FK_ALLOWED_IPS в коде.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            else:
                logging.warning(f"❌ FK webhook ОТКЛОНЁН - IP не в whitelist: {client_ip}")
                # Алерт админу - может быть это FK с новым IP!
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"🚨 <b>Webhook заблокирован - неизвестный IP</b>\n\n"
                        f"IP: <code>{client_ip}</code>\n"
                        f"Body: <code>{raw_body[:200]}</code>\n\n"
                        f"<b>Если это FreeKassa с новым IP</b> - установи в Railway "
                        f"переменную <code>FK_IP_CHECK=disabled</code> чтобы временно принимать "
                        f"платежи. Подпись webhook всё равно проверяется!",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                return web.Response(text="FORBIDDEN", status=403)

        # Парсим - поддерживаем и form-data и JSON (FK иногда меняет формат)
        data = {}
        try:
            data = dict(await request.post())
        except Exception:
            pass
        if not data and raw_body:
            # Пробуем как JSON
            try:
                import json as _json
                data = _json.loads(raw_body)
            except Exception:
                # Пробуем как query string
                from urllib.parse import parse_qs
                parsed = parse_qs(raw_body)
                data = {k: v[0] if isinstance(v, list) and v else v for k, v in parsed.items()}

        logging.info(f"FK webhook PARSED from {client_ip}: {data}")

        merchant_id = data.get("MERCHANT_ID", "")
        amount      = data.get("AMOUNT", "")
        order_id    = data.get("MERCHANT_ORDER_ID", "")
        recv_sign   = data.get("SIGN", "")

        # 1. Проверяем ID магазина
        if str(merchant_id) != str(FK_SHOP_ID):
            logging.warning(f"FK wrong merchant: {merchant_id}")
            return web.Response(text="WRONG MERCHANT")

        # 2. Проверяем подпись: MD5(MERCHANT_ID:AMOUNT:SECRET2:MERCHANT_ORDER_ID)
        expected_sign = hashlib.md5(
            f"{FK_SHOP_ID}:{amount}:{FK_SECRET2}:{order_id}".encode()
        ).hexdigest()
        if recv_sign != expected_sign:
            logging.warning(f"FK wrong sign. Got: {recv_sign}, expected: {expected_sign}")
            return web.Response(text="WRONG SIGN")

        # 3. Ищем заказ - сначала в памяти, потом в БД
        payment = pending_fk_payments.get(order_id)
        if not payment:
            # В памяти нет - ищем в БД (бот мог перезапуститься)
            db_order = await fk_get_order(order_id)
            if not db_order:
                logging.warning(f"FK order not found anywhere: {order_id}")
                return web.Response(text="YES")
            if db_order["status"] == "paid":
                logging.info(f"FK order already paid: {order_id}")
                return web.Response(text="YES")
            payment = {
                "user_id": db_order["user_id"],
                "credits": db_order["credits"],
                "amount":  db_order["amount_rub"],
            }
        else:
            # Удаляем из памяти
            del pending_fk_payments[order_id]

        # 4. Помечаем как оплаченный в БД (защита от двойного зачисления)
        # ВАЖНО: fk_credit_paid_order сама вызывает fk_mark_paid внутри,
        # поэтому здесь НЕ вызываем - иначе кредиты не зачислятся

        user_id    = payment["user_id"]
        credits    = payment["credits"]
        amount_rub = payment["amount"]

        # 4.1 Проверяем что оплаченная сумма совпадает с ожидаемой (защита от фрода)
        try:
            received_amount = float(amount)
            expected_amount = float(amount_rub)
            is_shop_order = order_id.startswith("shop_")

            if abs(received_amount - expected_amount) > 1.0:
                # Определяем тип заказа для правильного сообщения и логики
                db_order_for_check = await fk_get_order(order_id)
                pack_info = (db_order_for_check or {}).get("pack", "") if db_order_for_check else ""
                promo_in_db = (db_order_for_check or {}).get("promo_code", "") if db_order_for_check else ""

                if is_shop_order:
                    # Магазин: клиент мог оплатить больше (без скидки) — это ОК
                    if received_amount >= expected_amount:
                        logging.info(
                            f"FK SHOP: received {received_amount} >= expected {expected_amount} — OK"
                        )
                        # Продолжаем обработку
                    else:
                        # Оплачено меньше ожидаемого — алертим но НЕ блокируем webhook
                        # (деньги пришли, клиент заплатил что-то — разбираемся вручную)
                        logging.error(
                            f"FK SHOP AMOUNT LOW! order={order_id} user={user_id} "
                            f"expected={expected_amount} received={received_amount} pack={pack_info}"
                        )
                        try:
                            await bot.send_message(
                                ADMIN_ID,
                                f"⚠️ <b>Магазин: сумма меньше ожидаемой</b>\n\n"
                                f"Заказ: <code>{order_id}</code>\n"
                                f"Юзер: <code>{user_id}</code>\n"
                                f"Сервис: <code>{pack_info}</code>\n"
                                f"Ожидали: <b>{expected_amount}₽</b>\n"
                                f"Пришло: <b>{received_amount}₽</b>\n"
                                f"Промокод: <code>{promo_in_db or 'нет'}</code>\n\n"
                                f"⚠️ Подписка оформлена, но сумма расходится. Проверь вручную.",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                        # НЕ блокируем — зачисляем с предупреждением
                else:
                    # Кредиты: строгая проверка — если меньше, блокируем (фрод)
                    if received_amount < expected_amount - 1.0:
                        logging.error(
                            f"FK CREDITS AMOUNT MISMATCH! order={order_id} user={user_id} "
                            f"expected={expected_amount} received={received_amount}"
                        )
                        try:
                            await bot.send_message(
                                ADMIN_ID,
                                f"🚨 <b>Несовпадение суммы — КРЕДИТЫ</b>\n\n"
                                f"Заказ: <code>{order_id}</code>\n"
                                f"Юзер: <code>{user_id}</code>\n"
                                f"Ожидали: <b>{expected_amount}₽</b>\n"
                                f"Пришло: <b>{received_amount}₽</b>\n\n"
                                f"🚫 Кредиты НЕ зачислены. Разберись вручную.",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                        return web.Response(text="AMOUNT MISMATCH", status=400)
        except (ValueError, TypeError) as ve:
            logging.warning(f"FK AMOUNT parse error: {ve}")

        # 5. Зачисляем кредиты
        await fk_credit_paid_order(order_id, payment, source="webhook")
        return web.Response(text="YES")

    except Exception as e:
        logging.error(f"FK webhook error: {e}")
        return web.Response(text="ERROR", status=500)



async def getip_handler(request: web.Request) -> web.Response:
    """GET /getip — возвращает исходящий IP сервера (для NS Gifts whitelist)."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.ipify.org", timeout=aiohttp.ClientTimeout(total=5)) as r:
                ip = await r.text()
        return web.Response(text=f"Outbound IP: {ip.strip()}", content_type="text/plain")
    except Exception as e:
        return web.Response(text=f"Error: {e}", status=500)

async def setup_webhook_server():
    app = web.Application()
    FK_WEBHOOK_PATH = os.getenv("FK_WEBHOOK_URL", "").replace("https://", "").replace("http://", "")
    FK_WEBHOOK_PATH = "/" + FK_WEBHOOK_PATH.split("/", 1)[-1] if "/" in FK_WEBHOOK_PATH else "/fk_webhook"
    for path in set([FK_WEBHOOK_PATH, "/fk-webhook", "/fk_webhook"]):
        app.router.add_post(path, fk_webhook_handler)
        logging.info(f"FK webhook зарегистрован: POST {path}")
    # /getip — временный эндпоинт для получения исходящего IP
    app.router.add_get("/getip", getip_handler)
    app.router.add_get("/webapp/chatgpt", webapp_chatgpt_handler)
    app.router.add_get("/webapp/claude", webapp_claude_handler)
    app.router.add_post("/api/activate-claude", api_activate_claude_handler)
    app.router.add_get("/api/activate-claude-status/{order_id}", api_activate_claude_status_handler)
    logging.info("Claude Mini App: /webapp/claude + /api/activate-claude + status")
    app.router.add_get("/webapp/perplexity", webapp_perplexity_handler)
    app.router.add_post("/api/activate-perplexity", api_activate_perplexity_handler)
    app.router.add_get("/api/activate-perplexity-status/{order_id}", api_activate_perplexity_status_handler)
    logging.info("Perplexity Mini App: /webapp/perplexity + /api/activate-perplexity + status")

    app.router.add_post("/api/activate-chatgpt", api_activate_chatgpt_handler)
    app.router.add_get("/api/activate-status/{job_id}", api_activation_status_handler)
    logging.info("Mini App: /webapp/chatgpt + /api/activate-chatgpt + /api/activate-status")
    port = int(os.getenv("FK_WEBHOOK_PORT", "8080"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"✅ FK webhook сервер запущен на порту {port}")



async def _ensure_playwright_browser():
    """
    Проверяет наличие Playwright Chromium.

    В идеале браузер уже скачан при сборке (nixpacks.toml) в /app/pw-browsers/.
    Если нет — скачивает с --with-deps (apt-get ставит системные либы, браузер качается).
    Это fallback для первого деплоя или если сборка не выполнила playwright install.
    """
    import glob

    # Приоритет путей: сборочный /app (персистентный) → /tmp (эфемерный)
    for browsers_path in ["/app/pw-browsers", "/tmp/pw-browsers"]:
        pattern = f"{browsers_path}/chromium*/chrome-headless-shell-linux64/chrome-headless-shell"
        found = glob.glob(pattern)
        if found:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
            logging.info(f"✅ Playwright browser найден: {found[0]}")
            return

    # Браузер не найден нигде — скачиваем в /app/pw-browsers с системными зависимостями
    browsers_path = "/app/pw-browsers"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
    logging.warning(
        f"⚠️ Playwright browser не найден. "
        f"Скачиваем с --with-deps в {browsers_path} (~2-3 мин)..."
    )
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": browsers_path}
    proc = await asyncio.create_subprocess_exec(
        "python", "-m", "playwright", "install", "--with-deps", "chromium",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode == 0:
        found = glob.glob(f"{browsers_path}/chromium*/chrome-headless-shell-linux64/chrome-headless-shell")
        logging.info(f"✅ Playwright browser установлен: {found[0] if found else 'проверь путь'}")
    else:
        # --with-deps не сработал (нет apt-get) — пробуем /tmp без deps как последний шанс
        logging.error(f"❌ --with-deps failed (code {proc.returncode}): {stderr.decode()[:300]}")
        logging.warning("Пробуем /tmp/pw-browsers без --with-deps как последний вариант...")
        browsers_path = "/tmp/pw-browsers"
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
        env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
        proc2 = await asyncio.create_subprocess_exec(
            "python", "-m", "playwright", "install", "chromium",
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc2.communicate()
        logging.info(f"Browser fallback install complete (код: {proc2.returncode})")


async def _check_one_gpt_code(code_row: dict) -> str:
    """Проверяет один код. Возвращает новый check_status."""
    from chatgpt_activation import check_gpt_code
    try:
        result = await check_gpt_code(code_row["code"])
        return result.get("status", "error"), result.get("email", "")
    except Exception as e:
        logging.error(f"_check_one_gpt_code {code_row['code']}: {e}")
        return "error", ""


async def _send_claude_webapp_to_user(
    user_id: int, code: str, order_id: str,
    plan: str, plan_name: str, delayed_note: str = ""
) -> bool:
    """Сохраняет pending и отправляет клиенту кнопку WebApp."""
    import urllib.parse as _up
    from aiogram.types import WebAppInfo as _WAI

    await save_claude_pending_activation(user_id, code, order_id, plan, plan_name)

    webapp_url = (
        f"{WEBAPP_BASE_URL}/webapp/claude"
        f"?plan={_up.quote(plan_name)}&code={_up.quote(code)}"
    )
    try:
        import datetime as _dt_cl
        _base_cl = (
            f"🎉 <b>Оплата прошла!</b>\n\n"
            f"📦 <b>Claude {plan_name}</b>\n\n"
            f"Осталось активировать подписку — нажми кнопку ниже, "
            f"введи Organization ID из настроек Claude, и подписка "
            f"активируется автоматически за 1–2 минуты 👇"
            f"{delayed_note}"
        )
        _kb_cl_active = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Активировать Claude", web_app=_WAI(url=webapp_url))],
            [InlineKeyboardButton(text="❓ Нужна помощь", callback_data="claude_need_help")],
        ])
        _kb_cl_expired = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❓ Нужна помощь", callback_data="claude_need_help")],
        ])
        _dl_cl = _dt_cl.datetime.now(_BOT_TZ) + _dt_cl.timedelta(minutes=ACTIVATION_WINDOW_MIN)
        _exp_cl = (
            f"⏰ <b>Время самостоятельной активации истекло</b>\n\n"
            f"📦 <b>Claude {plan_name}</b> — оплата сохранена.\n"
            f"Напиши Александру — активирую вручную 🙌"
        )
        _m_act_cl = await bot.send_message(
            user_id, _base_cl + _activation_timer_line(_dl_cl),
            parse_mode="HTML", reply_markup=_kb_cl_active)
        _claude_act_msg[user_id] = _m_act_cl.message_id
        asyncio.create_task(_activation_timer_job(
            user_id, _m_act_cl.message_id, _base_cl, _kb_cl_active,
            _dl_cl, _exp_cl, _kb_cl_expired, _claude_act_msg))
        await log_event(user_id, "claude_webapp_sent",
                        f"code={code} plan={plan_name} order={order_id}")
        return True
    except Exception as _e:
        logging.error(f"_send_claude_webapp_to_user uid={user_id}: {_e}")
        return False


# ─── Фоновый polling job ──────────────────────────────────────────────────────

async def _claude_test_activation_job(fake_bpa: int, user_id: int, plan_name: str):
    """Симулирует успешную активацию для тестового кода TEST-."""
    await asyncio.sleep(4)  # имитируем задержку как в реальном сценарии
    _claude_job_results[fake_bpa] = {"status": "done", "success": True}
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🧪 <b>Claude тест завершён</b>\n"
            f"👤 <code>{user_id}</code>  📦 {plan_name}\n"
            f"Это фейковая активация — реальный код не потрачен.",
            parse_mode="HTML"
        )
    except Exception:
        pass


async def _take_claude_bpa_screenshot(bpa_order_id: int) -> bytes | None:
    """Делает скриншот статуса активации на bypriceactivate.pro."""
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as _p:
            _br = await _p.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--single-process"]
            )
            _pg = await _br.new_page(viewport={"width": 900, "height": 500})
            await _pg.goto(
                f"https://bypriceactivate.pro/api/activate/{bpa_order_id}",
                timeout=20000, wait_until="networkidle"
            )
            _ss = await _pg.screenshot(full_page=True)
            await _br.close()
            return _ss
    except Exception as _se:
        logging.warning(f"Claude BPA screenshot failed: {_se}")
        return None


async def _claude_activation_polling_job(
    bpa_order_id: int, code: str, user_id: int,
    order_id: str, plan_name: str, org_id: str
):
    """
    Опрашивает bypriceactivate.pro каждые 5 сек.
    При done — помечает код, уведомляет тебя и клиента.
    При failed — возвращает код, пишет обоим.
    """
    _claude_job_results[bpa_order_id] = {"status": "queued"}
    logging.info(f"Claude polling bpa={bpa_order_id} user={user_id} code={code}")

    for attempt in range(120):          # макс 10 минут (120 × 5 сек)
        await asyncio.sleep(5)
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as _s:
                async with _s.get(
                    f"https://bypriceactivate.pro/api/activate/{bpa_order_id}"
                ) as _r:
                    if _r.status != 200:
                        continue
                    _d = await _r.json()

            status = _d.get("status")
            _claude_job_results[bpa_order_id] = {"status": status}

            # ── Успех ──────────────────────────────────────────
            if status == "done":
                await mark_claude_code_used(code, user_id, order_id, org_id)
                await delete_claude_pending_activation(user_id)
                _claude_job_results[bpa_order_id] = {"status": "done", "success": True}

                import datetime as _dt2
                _ts = _dt2.datetime.now(_BOT_TZ).strftime("%d.%m.%Y %H:%M")
                try:
                    _pool2 = await get_pool()
                    async with _pool2.acquire() as _c2:
                        _ur = await _c2.fetchrow(
                            "SELECT username, full_name FROM users WHERE user_id=$1",
                            user_id
                        )
                    _un = (_ur["username"] if _ur else "") or ""
                    _fn = (_ur["full_name"] if _ur else "") or ""
                except Exception:
                    _un = _fn = ""
                _tg = (f"@{_un}" if _un else _fn) or f"id{user_id}"

                # Заменяем сообщение клиента на поздравление, убираем «Нужна помощь»
                import datetime as _dt_end_cl
                _end_cl = (_dt_end_cl.datetime.now(_BOT_TZ) + _dt_end_cl.timedelta(days=_subscription_days(plan_name))).strftime("%d.%m.%Y")
                _prof_kw_cl = ({"icon_custom_emoji_id": UI_EMOJI_IDS["menu_profile"]}
                               if UI_EMOJI_IDS.get("menu_profile") else {})
                _congrats_cl = (
                    "🎉 <b>Подписка Claude активирована!</b>\n\n"
                    f"📦 Тариф: <b>{plan_name}</b>\n"
                    f"🏢 Organization ID: <code>{org_id}</code>\n"
                    f"🔑 Ключ: <code>{code}</code>\n"
                    f"📅 Действует до: <b>{_end_cl}</b>\n\n"
                    "Подписка появится в Claude в течение 5–10 минут. Спасибо за покупку! 🙌"
                )
                _kb_cl = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Открыть Claude ↗", url="https://claude.ai")],
                    [InlineKeyboardButton(text="Мой профиль", callback_data="menu_profile", **_prof_kw_cl)],
                    [_eib("Главное меню", "back_main")],
                ])
                _mid_cl = _claude_act_msg.pop(user_id, None)
                _edited_cl = False
                if _mid_cl:
                    try:
                        await bot.edit_message_text(_congrats_cl, chat_id=user_id, message_id=_mid_cl,
                                                    parse_mode="HTML", reply_markup=_kb_cl)
                        _edited_cl = True
                    except Exception as _ee_cl:
                        logging.warning(f"edit claude activation msg failed: {_ee_cl}")
                if not _edited_cl:
                    try:
                        await bot.send_message(user_id, _congrats_cl, parse_mode="HTML", reply_markup=_kb_cl)
                    except Exception:
                        pass

                try:
                    _caption_ok = (
                        f"✅ <b>Claude авто-активация OK</b>\n\n"
                        f"👤 <b>{_tg}</b>  (<code>{user_id}</code>)\n"
                        f"🔑 Код: <code>{code}</code>\n"
                        f"📦 Тариф: <b>{plan_name}</b>\n"
                        f"🆔 Org ID: <code>{org_id}</code>\n"
                        f"🔢 BPA: <code>{bpa_order_id}</code>\n"
                        f"⏱ {_ts}"
                    )
                    _ss_ok = await _take_claude_bpa_screenshot(bpa_order_id)
                    if _ss_ok:
                        await bot.send_photo(
                            ADMIN_ID,
                            BufferedInputFile(_ss_ok, "claude_ok.png"),
                            caption=_caption_ok, parse_mode="HTML"
                        )
                    else:
                        await bot.send_message(ADMIN_ID, _caption_ok, parse_mode="HTML")
                except Exception:
                    pass
                await log_event(user_id, "claude_activation_ok",
                                f"code={code} bpa={bpa_order_id} plan={plan_name}")
                return

            # ── Ошибка ─────────────────────────────────────────
            elif status == "failed":
                _err = _d.get("error") or "Ошибка активации"
                _claude_job_results[bpa_order_id] = {
                    "status": "done", "success": False, "error": _err
                }
                _is_stock = ("out of stock" in _err.lower() or "out-of-stock" in _err.lower())
                _replaced_code = None
                if _is_stock:
                    # нет стока — код валиден, возвращаем в пул
                    await release_claude_code(code)
                    await delete_claude_pending_activation(user_id)
                else:
                    # Жёсткий сбой: код «сгорел» у провайдера (повторно непригоден).
                    # ОДИН раз автоматически выдаём свежий код, чтобы клиент активировал сам.
                    if order_id not in _claude_replaced_orders:
                        _pend_f = await get_claude_pending_activation(user_id)
                        _plan_key_f = ((_pend_f or {}).get("plan")) or {
                            "Pro": "pro", "Max 5×": "max_5x", "Max 20×": "max_20x"
                        }.get(plan_name, "pro")
                        _new_code_f = await get_next_claude_code(_plan_key_f)
                        if _new_code_f:
                            _claude_replaced_orders.add(order_id)
                            # старый код оставляем зарезервированным (is_used=TRUE) — он мёртв
                            await save_claude_pending_activation(
                                user_id, _new_code_f, order_id, _plan_key_f, plan_name)
                            _replaced_code = _new_code_f
                            logging.info(
                                f"Claude auto-replace user={user_id} order={order_id} "
                                f"dead={code} new={_new_code_f}")
                    if not _replaced_code:
                        # авто-замена недоступна (нет свежих кодов / уже заменяли) → ручная
                        await delete_claude_pending_activation(user_id)

                # ── алерт админу ──
                try:
                    _caption_fail = (
                        f"❌ <b>Claude FAILED</b>\n"
                        f"👤 <code>{user_id}</code>  📦 {plan_name}\n"
                        f"🔑 <code>{code}</code>  🔢 BPA: <code>{bpa_order_id}</code>\n"
                        f"❌ {_err[:300]}"
                        + (f"\n♻️ Выдан новый код: <code>{_replaced_code}</code>" if _replaced_code
                           else "\n⚠️ Авто-замена недоступна — нужна ручная активация")
                    )
                    _ss_fail = await _take_claude_bpa_screenshot(bpa_order_id)
                    if _ss_fail:
                        await bot.send_photo(
                            ADMIN_ID,
                            BufferedInputFile(_ss_fail, "claude_fail.png"),
                            caption=_caption_fail, parse_mode="HTML"
                        )
                    else:
                        await bot.send_message(ADMIN_ID, _caption_fail, parse_mode="HTML")
                except Exception:
                    pass

                # ── сообщение клиенту ──
                try:
                    if _replaced_code:
                        await bot.send_message(
                            user_id,
                            "😔 <b>Первый код не сработал</b>\n\n"
                            "Мы автоматически выдали новый код. "
                            "Нажми «🔁 Попробовать снова» — активируем заново 👇",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="🔁 Попробовать снова",
                                    callback_data="claude_reopen_webapp"
                                )],
                                [InlineKeyboardButton(
                                    text="❓ Нужна помощь",
                                    callback_data="claude_need_help"
                                )],
                            ])
                        )
                    else:
                        await bot.send_message(
                            user_id,
                            f"😔 <b>Активация Claude не прошла</b>\n\n"
                            f"{_err[:300]}\n\n"
                            f"Напиши Александру — активирую вручную за 15–30 мин!",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="✅ Активировали тариф вручную",
                                    callback_data="claude_manual_activated"
                                )],
                                [InlineKeyboardButton(
                                    text="❓ Нужна помощь",
                                    callback_data="claude_need_help"
                                )],
                            ])
                        )
                except Exception:
                    pass
                await log_event(user_id, "claude_activation_fail",
                                f"code={code} bpa={bpa_order_id} replaced={_replaced_code or '-'} err={_err[:120]}")
                return

        except Exception as _e:
            logging.error(f"Claude poll #{attempt} bpa={bpa_order_id}: {_e}")

    # ── Таймаут ────────────────────────────────────────────────
    # Код и pending НЕ трогаем — чтобы клиент мог нажать «активировали вручную»,
    # а Александр активировал тем же кодом. Освободится сам через ~2ч (cleanup-loop).
    _claude_job_results[bpa_order_id] = {
        "status": "done", "success": False,
        "error": "Активация затянулась. Напиши Александру — он поможет!"
    }
    try:
        await bot.send_message(
            user_id,
            f"⏰ <b>Активация Claude затянулась</b>\n\n"
            f"Мы не получили подтверждение от сервиса активации.\n"
            f"Напиши Александру — разберёмся и активируем вручную!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Активировали тариф вручную",
                    callback_data="claude_manual_activated"
                )],
                [InlineKeyboardButton(
                    text="❓ Нужна помощь",
                    callback_data="claude_need_help"
                )],
            ])
        )
    except Exception:
        pass
    try:
        await bot.send_message(
            ADMIN_ID,
            f"⏰ <b>Claude TIMEOUT</b>\n"
            f"👤 <code>{user_id}</code>  🔢 BPA: <code>{bpa_order_id}</code>\n"
            f"🔑 Код: <code>{code}</code>\n"
            f"10 минут — проверь вручную на bypriceactivate.pro\n"
            f"Код зарезервирован за клиентом — активируй вручную им же.",
            parse_mode="HTML"
        )
    except Exception:
        pass


# ─── Веб-хэндлеры ────────────────────────────────────────────────────────────

async def webapp_claude_handler(request: web.Request) -> web.Response:
    """GET /webapp/claude — отдаёт claude_webapp.html"""
    try:
        with open(_CLAUDE_WEBAPP_HTML_PATH, "r", encoding="utf-8") as _f:
            return web.Response(text=_f.read(), content_type="text/html", charset="utf-8")
    except FileNotFoundError:
        return web.Response(text="Claude Mini App not found", status=404)


async def api_activate_claude_handler(request: web.Request) -> web.Response:
    """POST /api/activate-claude"""
    import json as _j, re as _re

    def _resp(data, status=200):
        return web.Response(
            text=_j.dumps(data, ensure_ascii=False),
            content_type="application/json", status=status
        )

    if not rt.claude_webapp_enabled:
        return _resp({"error": f"Временно недоступно. Напиши @{PERSONAL_USERNAME}"}, 503)

    try:
        body = await request.json()
    except Exception:
        return _resp({"error": "Неверный формат запроса"}, 400)

    org_id    = (body.get("org_id") or "").strip().lower()
    init_data = (body.get("init_data") or "").strip()

    user_id = _verify_tg_init_data(init_data)
    if not user_id:
        return _resp({"error": "Ошибка авторизации. Перезапусти мини-приложение."}, 403)

    if not _re.match(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', org_id
    ):
        return _resp({"error": "Неверный формат Organization ID. Должен быть UUID."})

    pending = await get_claude_pending_activation(user_id)
    if not pending:
        return _resp({
            "error": f"Время сессии истекло. Напиши @{PERSONAL_USERNAME} для нового кода."
        })

    code      = pending["code"]
    order_id  = pending["order_id"]
    plan_name = pending["plan_name"]

    # ТЕСТ: если код фейковый (TEST-) — симулируем успех без реального запроса
    if code.startswith("TEST-"):
        import random as _rand
        fake_bpa = _rand.randint(9000000, 9999999)
        _claude_job_results[fake_bpa] = {"status": "queued"}
        asyncio.create_task(_claude_test_activation_job(fake_bpa, user_id, plan_name))
        await delete_claude_pending_activation(user_id)
        logging.info(f"Claude TEST activation: fake_bpa={fake_bpa} user={user_id}")
        return _resp({"order_id": fake_bpa, "status": "queued"})

    # БАГ 3 FIX: если job уже запущен — не делаем новый POST, просто возвращаем существующий order_id
    existing_bpa = pending.get("bpa_order_id")
    if existing_bpa:
        # Polling job уже работает или завершился — возвращаем клиенту тот же order_id
        logging.info(f"Claude reuse bpa={existing_bpa} user={user_id}")
        return _resp({"order_id": existing_bpa, "status": "queued"})

    # Guard: повторная активация Claude за 35 дней — предупреждаем, повторное
    # нажатие «Попробовать снова» активирует принудительно (на другой аккаунт).
    try:
        _pool_dbl = await get_pool()
        async with _pool_dbl.acquire() as _c_dbl:
            _recent = await _c_dbl.fetchrow(
                "SELECT code, plan, used_at FROM claude_codes"
                " WHERE used_by=$1 AND used_at > NOW() - INTERVAL '35 days'"
                " AND used_by IS NOT NULL ORDER BY used_at DESC LIMIT 1",
                user_id
            )
        if _recent:
            _us = _recent["used_at"].strftime("%d.%m.%Y %H:%M") if _recent["used_at"] else "-"
            _u = await get_user(user_id)
            if _u and _u.get("username"):
                _uname = "@" + _u["username"]
            elif _u and _u.get("full_name"):
                _uname = _u["full_name"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            else:
                _uname = "без ника"
            if user_id not in _claude_double_warned:
                _claude_double_warned.add(user_id)
                logging.warning(f'Claude repeat activation: warned user={user_id} code={_recent["code"]}')
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        "⚠️ <b>Повторная активация Claude</b> (клиент предупреждён)\n\n"
                        f"👤 {_uname} (<code>{user_id}</code>)\n"
                        f"🔑 Уже активирован: <code>{_recent['code']}</code>\n"
                        f"📦 Тариф: <b>{_recent['plan']}</b>\n"
                        f"⏱ Дата: <b>{_us}</b>\n\n"
                        "Если клиент нажмёт «Попробовать снова» — активирует на другой аккаунт.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                return _resp({"error": (
                    "⚠️ На твой аккаунт уже активирована подписка Claude.\n\n"
                    "Если оформляешь на ДРУГОЙ аккаунт (например, для друга) — "
                    "нажми «Попробовать снова», и активация пройдёт.\n\n"
                    "Если это случайно — напиши Александру."
                )})
            else:
                _claude_double_warned.discard(user_id)
                logging.info(f"Claude forced re-activation user={user_id}")
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        "✅ <b>Повторная активация Claude — подтверждена</b>\n\n"
                        f"👤 {_uname} (<code>{user_id}</code>) активирует ещё раз (другой аккаунт).",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    except Exception as _dbl_e:
        logging.error(f"claude double-activation check: {_dbl_e}")

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as _s:
            async with _s.post(
                "https://bypriceactivate.pro/api/activate",
                json={"code": code, "org_id": org_id},
                headers={"Content-Type": "application/json"},
            ) as _r:
                try:
                    _rd = await _r.json()
                except Exception:
                    _rd = {}

                if _r.status == 201:
                    bpa_order_id = _rd.get("order_id")
                    if not bpa_order_id:
                        return _resp({"error": "Сервис не вернул order_id."})

                    _pool3 = await get_pool()
                    async with _pool3.acquire() as _c3:
                        await _c3.execute(
                            "UPDATE claude_pending_activations "
                            "SET org_id=$1, bpa_order_id=$2 WHERE user_id=$3",
                            org_id, bpa_order_id, user_id
                        )
                    asyncio.create_task(_claude_activation_polling_job(
                        bpa_order_id, code, user_id, order_id, plan_name, org_id
                    ))
                    logging.info(
                        f"Claude activation: bpa={bpa_order_id} user={user_id} code={code}"
                    )
                    return _resp({"order_id": bpa_order_id, "status": "queued"})

                elif _r.status == 409:
                    _detail = _rd.get("detail", "")
                    if "already claimed" in _detail:
                        _msg = "Код уже активирован. Напиши Александру."
                    elif "out of stock" in _detail:
                        _msg = "Временно нет активаций. Александр активирует вручную."
                        try:
                            await bot.send_message(
                                ADMIN_ID,
                                f"🚨 <b>Claude — нет стока!</b>\n"
                                f"👤 <code>{user_id}</code> ({plan_name})\n"
                                f"Пополни коды на bypriceactivate.pro",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                    else:
                        _msg = _detail or "Ошибка кода."
                    return _resp({"error": _msg})

                elif _r.status == 404:
                    return _resp({"error": "Код не найден. Напиши Александру."})

                else:
                    logging.error(f"bypriceactivate.pro HTTP {_r.status}: {str(_rd)[:200]}")
                    return _resp({"error": f"Ошибка ({_r.status}). Попробуй ещё раз."})

    except aiohttp.ClientError as _e:
        logging.error(f"Claude activate network: {_e}")
        return _resp({"error": "Нет связи с сервисом. Попробуй ещё раз."})
    except Exception as _e:
        logging.error(f"Claude activate error: {_e}", exc_info=True)
        return _resp({"error": "Внутренняя ошибка. Напиши Александру."})


async def api_activate_claude_status_handler(request: web.Request) -> web.Response:
    """GET /api/activate-claude-status/{order_id}"""
    import json as _j2
    try:
        bpa_order_id = int(request.match_info.get("order_id", ""))
    except ValueError:
        return web.Response(
            text=_j2.dumps({"status": "not_found"}),
            content_type="application/json", status=400
        )
    result = _claude_job_results.get(bpa_order_id)
    if result is None:
        return web.Response(
            text=_j2.dumps({"status": "not_found"}),
            content_type="application/json", status=404
        )
    return web.Response(
        text=_j2.dumps(result, ensure_ascii=False),
        content_type="application/json"
    )


# ─── Callback: клиент переоткрывает WebApp сам ───────────────────────────────

async def _nsg_usd_rate() -> float:
    v = await get_setting("nsgifts_usd_rate")
    try:    return float(v)
    except: return 90.0

async def _nsg_markup() -> float:
    v = await get_setting("nsgifts_markup")
    try:    return float(v)
    except: return 18.0

async def _nsg_threshold() -> float:
    v = await get_setting("nsgifts_balance_threshold")
    try:    return float(v)
    except: return 30.0


# ──────────────────────────────────────────────────────────────────────────────
#  ХЕНДЛЕР: shop_svc:appstore — выбор региона
#  ВАЖНО: зарегистрируй ЭТО ДО существующего @dp.callback_query(F.data.startswith("shop_svc:"))
#  Если вставляешь в конец файла — переименуй callback_data в меню на "nsg_start"
#  (см. ниже menu_shop_nsg_override — он заменит стандартную кнопку appstore)
# ──────────────────────────────────────────────────────────────────────────────

async def nsgifts_fulfill_after_payment(fk_order_id: str, user_id: int):
    """
    Вызывается из fk_credit_paid_order когда order_id начинается с 'nsg_'.
    1. Ищем запись в nsgifts_orders
    2. Создаём заказ в NS Gifts
    3. Оплачиваем → получаем пин-коды
    4. Отправляем пользователю
    """
    if not rt.nsgifts_client:
        logging.error("NSGifts: client not initialized, cannot fulfill")
        await bot.send_message(
            user_id,
            "⚠️ Оплата получена, но автодоставка временно недоступна.\n"
            "Напиши @neirosetkaalex — код пришлю вручную в течение 15 минут.",
        )
        await bot.send_message(
            ADMIN_ID,
            f"🚨 <b>NSGifts: client not init</b>\n"
            f"fk_order={fk_order_id}  uid={user_id}\nАктивируй вручную!",
            parse_mode="HTML"
        )
        return

    pool = await get_pool()

    # 1. Получаем информацию о заказе
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM nsgifts_orders WHERE fk_order_id=$1", fk_order_id
        )

    if not row:
        logging.error(f"NSGifts: order not found for fk_order={fk_order_id}")
        await bot.send_message(
            ADMIN_ID,
            f"🚨 <b>NSGifts: запись не найдена</b>\n"
            f"fk_order={fk_order_id}  uid={user_id}",
            parse_mode="HTML"
        )
        return

    if row["status"] == "fulfilled":
        logging.info(f"NSGifts: order {fk_order_id} already fulfilled")
        return   # идемпотентность — уже выполнен

    service_id   = row["service_id"]
    service_name = row["service_name"]
    price_rub    = row["price_rub"]

    # Уведомляем что обрабатываем
    try:
        await bot.send_message(
            user_id,
            f"✅ <b>Оплата получена!</b>\n"
            f"🆔 Заказ: <code>{fk_order_id}</code>\n\n"
            f"Получаем код — займёт пару секунд… ⏳",
            parse_mode="HTML"
        )
    except Exception:
        pass

    try:
        # 2. Создаём заказ
        create_resp = await rt.nsgifts_client.create_order(service_id, quantity=1)
        custom_id   = create_resp.get("custom_id") or create_resp.get("_custom_id")

        if not custom_id:
            raise RuntimeError(f"create_order: no custom_id in response: {create_resp}")

        # Сохраняем custom_id
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE nsgifts_orders SET ns_custom_id=$1, status='paying' "
                "WHERE fk_order_id=$2",
                custom_id, fk_order_id
            )

        # 3. Оплачиваем
        pay_resp = await rt.nsgifts_client.pay_order(custom_id)
        status   = pay_resp.get("status", "")
        pins     = pay_resp.get("pins") or []

        if status == "insufficient":
            raise RuntimeError("Insufficient balance on NS Gifts account")

        if not pins:
            raise RuntimeError(f"pay_order returned no pins: {pay_resp}")

        # 4. Сохраняем и отправляем
        import json as _json
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE nsgifts_orders SET status='fulfilled', "
                "ns_custom_id=$1, pins_json=$2 WHERE fk_order_id=$3",
                custom_id, _json.dumps(pins), fk_order_id
            )

        # Формируем красивое сообщение с кодом
        pins_text = "\n".join(f"<code>{p}</code>" for p in pins)
        await bot.send_message(
            user_id,
            f"🎉 <b>Вот твой код!</b>\n\n"
            f"📦 <b>{service_name}</b>\n"
            f"🆔 Заказ: <code>{fk_order_id}</code>\n\n"
            f"🔑 <b>Код активации:</b>\n{pins_text}\n\n"
            f"📲 <b>Как активировать:</b>\n"
            f"1. Открой <b>App Store</b> на iPhone/iPad\n"
            f"2. Нажми на свой <b>аватар</b> (вверху справа)\n"
            f"3. Выбери <b>«Погасить подарочную карту или код»</b>\n"
            f"4. Нажми <b>«Ввести код вручную»</b> и вставь код выше\n"
            f"5. Готово — баланс пополнится 🎉\n\n"
            f"⚠️ <b>Важно:</b> код работает только на Apple ID того же региона, "
            f"что и карта (например, код 🇺🇸 USA — только на американском Apple ID).\n\n"
            f"Если код не активируется — пиши @{PERSONAL_USERNAME} 🙌",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🌍 Как сменить регион Apple ID",
                                      callback_data="nsg_region_help")]
            ])
        )

        # Алерт администратору
        balance = await rt.nsgifts_client.check_balance()
        threshold = await _nsg_threshold()
        balance_warn = f"\n⚠️ <b>Баланс NS Gifts: ${balance:.2f}</b> — пора пополнить!" \
                       if balance < threshold else f"\nБаланс NS Gifts: ${balance:.2f}"
        await bot.send_message(
            ADMIN_ID,
            f"✅ <b>Apple Gift Card продан</b>\n\n"
            f"👤 <code>{user_id}</code>\n"
            f"📦 {service_name}\n"
            f"💵 {price_rub} ₽\n"
            f"🔑 {', '.join(pins)}"
            f"{balance_warn}",
            parse_mode="HTML"
        )
        await log_event(user_id, "nsgifts_fulfilled",
                        f"fk={fk_order_id} ns={custom_id} pins={len(pins)}")

    except Exception as e:
        logging.error(f"NSGifts fulfill failed for {fk_order_id}: {e}", exc_info=True)

        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE nsgifts_orders SET status='failed', error_msg=$1 "
                "WHERE fk_order_id=$2",
                str(e)[:500], fk_order_id
            )

        # Сообщение пользователю
        await bot.send_message(
            user_id,
            "😔 Оплата прошла, но при выдаче кода возникла ошибка.\n\n"
            f"Напиши @{PERSONAL_USERNAME} — код пришлю вручную в течение 15 минут! 🙏"
        )

        # Алерт администратору с деталями
        await bot.send_message(
            ADMIN_ID,
            f"🚨 <b>NSGifts ОШИБКА выдачи</b>\n\n"
            f"👤 <code>{user_id}</code>\n"
            f"📦 {service_name}  (service_id={service_id})\n"
            f"💵 {price_rub} ₽\n"
            f"🆔 FK: <code>{fk_order_id}</code>\n\n"
            f"❌ Ошибка: <code>{str(e)[:300]}</code>\n\n"
            f"Активируй вручную через кабинет wholesale.ns.gifts",
            parse_mode="HTML"
        )
        await log_event(user_id, "nsgifts_failed",
                        f"fk={fk_order_id} error={str(e)[:300]}")


# ──────────────────────────────────────────────────────────────────────────────
#  Фоновый цикл: алерт на низкий баланс NS Gifts
# ──────────────────────────────────────────────────────────────────────────────


# ══════════════════════════════════════════════════════════
#  PERPLEXITY — авто-активация (копия Claude, идентификатор = Perplexity User ID)
# ══════════════════════════════════════════════════════════

async def _send_perplexity_webapp_to_user(
    user_id: int, code: str, order_id: str,
    plan: str, plan_name: str, delayed_note: str = ""
) -> bool:
    """Сохраняет pending и отправляет клиенту кнопку WebApp."""
    import urllib.parse as _up
    from aiogram.types import WebAppInfo as _WAI

    await save_perplexity_pending_activation(user_id, code, order_id, plan, plan_name)

    webapp_url = (
        f"{WEBAPP_BASE_URL}/webapp/perplexity"
        f"?plan={_up.quote(plan_name)}&code={_up.quote(code)}"
    )
    try:
        import datetime as _dt_cl
        _base_cl = (
            f"🎉 <b>Оплата прошла!</b>\n\n"
            f"📦 <b>Perplexity {plan_name}</b>\n\n"
            f"Осталось активировать подписку — нажми кнопку ниже, "
            f"введи Perplexity User ID (perplexity.ai/api/auth/session), и подписка "
            f"активируется автоматически за 1–2 минуты 👇"
            f"{delayed_note}"
        )
        _kb_cl_active = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Активировать Perplexity", web_app=_WAI(url=webapp_url))],
            [InlineKeyboardButton(text="❓ Нужна помощь", callback_data="perplexity_need_help")],
        ])
        _kb_cl_expired = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❓ Нужна помощь", callback_data="perplexity_need_help")],
        ])
        _dl_cl = _dt_cl.datetime.now(_BOT_TZ) + _dt_cl.timedelta(minutes=ACTIVATION_WINDOW_MIN)
        _exp_cl = (
            f"⏰ <b>Время самостоятельной активации истекло</b>\n\n"
            f"📦 <b>Perplexity {plan_name}</b> — оплата сохранена.\n"
            f"Напиши Александру — активирую вручную 🙌"
        )
        _m_act_cl = await bot.send_message(
            user_id, _base_cl + _activation_timer_line(_dl_cl),
            parse_mode="HTML", reply_markup=_kb_cl_active)
        _perplexity_act_msg[user_id] = _m_act_cl.message_id
        asyncio.create_task(_activation_timer_job(
            user_id, _m_act_cl.message_id, _base_cl, _kb_cl_active,
            _dl_cl, _exp_cl, _kb_cl_expired, _perplexity_act_msg))
        await log_event(user_id, "perplexity_webapp_sent",
                        f"code={code} plan={plan_name} order={order_id}")
        return True
    except Exception as _e:
        logging.error(f"_send_perplexity_webapp_to_user uid={user_id}: {_e}")
        return False


# ─── Фоновый polling job ──────────────────────────────────────────────────────


async def _perplexity_test_activation_job(fake_bpa: int, user_id: int, plan_name: str):
    """Симулирует успешную активацию для тестового кода TEST-."""
    await asyncio.sleep(4)  # имитируем задержку как в реальном сценарии
    _perplexity_job_results[fake_bpa] = {"status": "done", "success": True}
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🧪 <b>Perplexity тест завершён</b>\n"
            f"👤 <code>{user_id}</code>  📦 {plan_name}\n"
            f"Это фейковая активация — реальный код не потрачен.",
            parse_mode="HTML"
        )
    except Exception:
        pass


async def _perplexity_activation_polling_job(
    bpa_order_id: int, code: str, user_id: int,
    order_id: str, plan_name: str, org_id: str
):
    """
    Опрашивает bypriceactivate.pro каждые 5 сек.
    При done — помечает код, уведомляет тебя и клиента.
    При failed — возвращает код, пишет обоим.
    """
    _perplexity_job_results[bpa_order_id] = {"status": "queued"}
    logging.info(f"Perplexity polling bpa={bpa_order_id} user={user_id} code={code}")

    for attempt in range(120):          # макс 10 минут (120 × 5 сек)
        await asyncio.sleep(5)
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            ) as _s:
                async with _s.get(
                    f"https://bypriceactivate.pro/api/activate/{bpa_order_id}"
                ) as _r:
                    if _r.status != 200:
                        continue
                    _d = await _r.json()

            status = _d.get("status")
            _perplexity_job_results[bpa_order_id] = {"status": status}

            # ── Успех ──────────────────────────────────────────
            if status == "done":
                await mark_perplexity_code_used(code, user_id, order_id, org_id)
                await delete_perplexity_pending_activation(user_id)
                _perplexity_job_results[bpa_order_id] = {"status": "done", "success": True}

                import datetime as _dt2
                _ts = _dt2.datetime.now(_BOT_TZ).strftime("%d.%m.%Y %H:%M")
                try:
                    _pool2 = await get_pool()
                    async with _pool2.acquire() as _c2:
                        _ur = await _c2.fetchrow(
                            "SELECT username, full_name FROM users WHERE user_id=$1",
                            user_id
                        )
                    _un = (_ur["username"] if _ur else "") or ""
                    _fn = (_ur["full_name"] if _ur else "") or ""
                except Exception:
                    _un = _fn = ""
                _tg = (f"@{_un}" if _un else _fn) or f"id{user_id}"

                # Заменяем сообщение клиента на поздравление, убираем «Нужна помощь»
                import datetime as _dt_end_cl
                _end_cl = (_dt_end_cl.datetime.now(_BOT_TZ) + _dt_end_cl.timedelta(days=_subscription_days(plan_name))).strftime("%d.%m.%Y")
                _prof_kw_cl = ({"icon_custom_emoji_id": UI_EMOJI_IDS["menu_profile"]}
                               if UI_EMOJI_IDS.get("menu_profile") else {})
                _congrats_cl = (
                    "🎉 <b>Подписка Perplexity активирована!</b>\n\n"
                    f"📦 Тариф: <b>{plan_name}</b>\n"
                    f"🏢 Perplexity User ID: <code>{org_id}</code>\n"
                    f"🔑 Ключ: <code>{code}</code>\n"
                    f"📅 Действует до: <b>{_end_cl}</b>\n\n"
                    "Подписка появится в Perplexity в течение 5–10 минут. Спасибо за покупку! 🙌"
                )
                _kb_cl = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="Открыть Perplexity ↗", url="https://perplexity.ai")],
                    [InlineKeyboardButton(text="Мой профиль", callback_data="menu_profile", **_prof_kw_cl)],
                    [_eib("Главное меню", "back_main")],
                ])
                _mid_cl = _perplexity_act_msg.pop(user_id, None)
                _edited_cl = False
                if _mid_cl:
                    try:
                        await bot.edit_message_text(_congrats_cl, chat_id=user_id, message_id=_mid_cl,
                                                    parse_mode="HTML", reply_markup=_kb_cl)
                        _edited_cl = True
                    except Exception as _ee_cl:
                        logging.warning(f"edit perplexity activation msg failed: {_ee_cl}")
                if not _edited_cl:
                    try:
                        await bot.send_message(user_id, _congrats_cl, parse_mode="HTML", reply_markup=_kb_cl)
                    except Exception:
                        pass

                try:
                    _caption_ok = (
                        f"✅ <b>Perplexity авто-активация OK</b>\n\n"
                        f"👤 <b>{_tg}</b>  (<code>{user_id}</code>)\n"
                        f"🔑 Код: <code>{code}</code>\n"
                        f"📦 Тариф: <b>{plan_name}</b>\n"
                        f"🆔 Org ID: <code>{org_id}</code>\n"
                        f"🔢 BPA: <code>{bpa_order_id}</code>\n"
                        f"⏱ {_ts}"
                    )
                    _ss_ok = await _take_claude_bpa_screenshot(bpa_order_id)
                    if _ss_ok:
                        await bot.send_photo(
                            ADMIN_ID,
                            BufferedInputFile(_ss_ok, "perplexity_ok.png"),
                            caption=_caption_ok, parse_mode="HTML"
                        )
                    else:
                        await bot.send_message(ADMIN_ID, _caption_ok, parse_mode="HTML")
                except Exception:
                    pass
                await log_event(user_id, "perplexity_activation_ok",
                                f"code={code} bpa={bpa_order_id} plan={plan_name}")
                return

            # ── Ошибка ─────────────────────────────────────────
            elif status == "failed":
                _err = _d.get("error") or "Ошибка активации"
                _perplexity_job_results[bpa_order_id] = {
                    "status": "done", "success": False, "error": _err
                }
                _is_stock = ("out of stock" in _err.lower() or "out-of-stock" in _err.lower())
                _replaced_code = None
                if _is_stock:
                    # нет стока — код валиден, возвращаем в пул
                    await release_perplexity_code(code)
                    await delete_perplexity_pending_activation(user_id)
                else:
                    # Жёсткий сбой: код «сгорел» у провайдера (повторно непригоден).
                    # ОДИН раз автоматически выдаём свежий код, чтобы клиент активировал сам.
                    if order_id not in _perplexity_replaced_orders:
                        _pend_f = await get_perplexity_pending_activation(user_id)
                        _plan_key_f = ((_pend_f or {}).get("plan")) or {
                            "Pro": "pro", "Max 5×": "max_5x", "Max 20×": "max_20x"
                        }.get(plan_name, "pro")
                        _new_code_f = await get_next_perplexity_code(_plan_key_f)
                        if _new_code_f:
                            _perplexity_replaced_orders.add(order_id)
                            # старый код оставляем зарезервированным (is_used=TRUE) — он мёртв
                            await save_perplexity_pending_activation(
                                user_id, _new_code_f, order_id, _plan_key_f, plan_name)
                            _replaced_code = _new_code_f
                            logging.info(
                                f"Perplexity auto-replace user={user_id} order={order_id} "
                                f"dead={code} new={_new_code_f}")
                    if not _replaced_code:
                        # авто-замена недоступна (нет свежих кодов / уже заменяли) → ручная
                        await delete_perplexity_pending_activation(user_id)

                # ── алерт админу ──
                try:
                    _caption_fail = (
                        f"❌ <b>Perplexity FAILED</b>\n"
                        f"👤 <code>{user_id}</code>  📦 {plan_name}\n"
                        f"🔑 <code>{code}</code>  🔢 BPA: <code>{bpa_order_id}</code>\n"
                        f"❌ {_err[:300]}"
                        + (f"\n♻️ Выдан новый код: <code>{_replaced_code}</code>" if _replaced_code
                           else "\n⚠️ Авто-замена недоступна — нужна ручная активация")
                    )
                    _ss_fail = await _take_claude_bpa_screenshot(bpa_order_id)
                    if _ss_fail:
                        await bot.send_photo(
                            ADMIN_ID,
                            BufferedInputFile(_ss_fail, "perplexity_fail.png"),
                            caption=_caption_fail, parse_mode="HTML"
                        )
                    else:
                        await bot.send_message(ADMIN_ID, _caption_fail, parse_mode="HTML")
                except Exception:
                    pass

                # ── сообщение клиенту ──
                try:
                    if _replaced_code:
                        await bot.send_message(
                            user_id,
                            "😔 <b>Первый код не сработал</b>\n\n"
                            "Мы автоматически выдали новый код. "
                            "Нажми «🔁 Попробовать снова» — активируем заново 👇",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="🔁 Попробовать снова",
                                    callback_data="perplexity_reopen_webapp"
                                )],
                                [InlineKeyboardButton(
                                    text="❓ Нужна помощь",
                                    callback_data="perplexity_need_help"
                                )],
                            ])
                        )
                    else:
                        await bot.send_message(
                            user_id,
                            f"😔 <b>Активация Perplexity не прошла</b>\n\n"
                            f"{_err[:300]}\n\n"
                            f"Напиши Александру — активирую вручную за 15–30 мин!",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="✅ Активировали тариф вручную",
                                    callback_data="perplexity_manual_activated"
                                )],
                                [InlineKeyboardButton(
                                    text="❓ Нужна помощь",
                                    callback_data="perplexity_need_help"
                                )],
                            ])
                        )
                except Exception:
                    pass
                await log_event(user_id, "perplexity_activation_fail",
                                f"code={code} bpa={bpa_order_id} replaced={_replaced_code or '-'} err={_err[:120]}")
                return

        except Exception as _e:
            logging.error(f"Perplexity poll #{attempt} bpa={bpa_order_id}: {_e}")

    # ── Таймаут ────────────────────────────────────────────────
    # Код и pending НЕ трогаем — чтобы клиент мог нажать «активировали вручную»,
    # а Александр активировал тем же кодом. Освободится сам через ~2ч (cleanup-loop).
    _perplexity_job_results[bpa_order_id] = {
        "status": "done", "success": False,
        "error": "Активация затянулась. Напиши Александру — он поможет!"
    }
    try:
        await bot.send_message(
            user_id,
            f"⏰ <b>Активация Perplexity затянулась</b>\n\n"
            f"Мы не получили подтверждение от сервиса активации.\n"
            f"Напиши Александру — разберёмся и активируем вручную!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(
                    text="✅ Активировали тариф вручную",
                    callback_data="perplexity_manual_activated"
                )],
                [InlineKeyboardButton(
                    text="❓ Нужна помощь",
                    callback_data="perplexity_need_help"
                )],
            ])
        )
    except Exception:
        pass
    try:
        await bot.send_message(
            ADMIN_ID,
            f"⏰ <b>Perplexity TIMEOUT</b>\n"
            f"👤 <code>{user_id}</code>  🔢 BPA: <code>{bpa_order_id}</code>\n"
            f"🔑 Код: <code>{code}</code>\n"
            f"10 минут — проверь вручную на bypriceactivate.pro\n"
            f"Код зарезервирован за клиентом — активируй вручную им же.",
            parse_mode="HTML"
        )
    except Exception:
        pass


# ─── Веб-хэндлеры ────────────────────────────────────────────────────────────


async def webapp_perplexity_handler(request: web.Request) -> web.Response:
    """GET /webapp/perplexity — отдаёт perplexity_webapp.html"""
    try:
        with open(_PERPLEXITY_WEBAPP_HTML_PATH, "r", encoding="utf-8") as _f:
            return web.Response(text=_f.read(), content_type="text/html", charset="utf-8")
    except FileNotFoundError:
        return web.Response(text="Perplexity Mini App not found", status=404)


async def api_activate_perplexity_handler(request: web.Request) -> web.Response:
    """POST /api/activate-perplexity"""
    import json as _j, re as _re

    def _resp(data, status=200):
        return web.Response(
            text=_j.dumps(data, ensure_ascii=False),
            content_type="application/json", status=status
        )

    if not rt.perplexity_webapp_enabled:
        return _resp({"error": f"Временно недоступно. Напиши @{PERSONAL_USERNAME}"}, 503)

    try:
        body = await request.json()
    except Exception:
        return _resp({"error": "Неверный формат запроса"}, 400)

    org_id    = (body.get("org_id") or "").strip().lower()
    init_data = (body.get("init_data") or "").strip()

    user_id = _verify_tg_init_data(init_data)
    if not user_id:
        return _resp({"error": "Ошибка авторизации. Перезапусти мини-приложение."}, 403)

    if not _re.match(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', org_id
    ):
        return _resp({"error": "Неверный формат Perplexity User ID. Должен быть UUID."})

    pending = await get_perplexity_pending_activation(user_id)
    if not pending:
        return _resp({
            "error": f"Время сессии истекло. Напиши @{PERSONAL_USERNAME} для нового кода."
        })

    code      = pending["code"]
    order_id  = pending["order_id"]
    plan_name = pending["plan_name"]

    # ТЕСТ: если код фейковый (TEST-) — симулируем успех без реального запроса
    if code.startswith("TEST-"):
        import random as _rand
        fake_bpa = _rand.randint(9000000, 9999999)
        _perplexity_job_results[fake_bpa] = {"status": "queued"}
        asyncio.create_task(_perplexity_test_activation_job(fake_bpa, user_id, plan_name))
        await delete_perplexity_pending_activation(user_id)
        logging.info(f"Perplexity TEST activation: fake_bpa={fake_bpa} user={user_id}")
        return _resp({"order_id": fake_bpa, "status": "queued"})

    # БАГ 3 FIX: если job уже запущен — не делаем новый POST, просто возвращаем существующий order_id
    existing_bpa = pending.get("bpa_order_id")
    if existing_bpa:
        # Polling job уже работает или завершился — возвращаем клиенту тот же order_id
        logging.info(f"Perplexity reuse bpa={existing_bpa} user={user_id}")
        return _resp({"order_id": existing_bpa, "status": "queued"})

    # Guard: повторная активация Perplexity за 35 дней — предупреждаем, повторное
    # нажатие «Попробовать снова» активирует принудительно (на другой аккаунт).
    try:
        _pool_dbl = await get_pool()
        async with _pool_dbl.acquire() as _c_dbl:
            _recent = await _c_dbl.fetchrow(
                "SELECT code, plan, used_at FROM perplexity_codes"
                " WHERE used_by=$1 AND used_at > NOW() - INTERVAL '35 days'"
                " AND used_by IS NOT NULL ORDER BY used_at DESC LIMIT 1",
                user_id
            )
        if _recent:
            _us = _recent["used_at"].strftime("%d.%m.%Y %H:%M") if _recent["used_at"] else "-"
            _u = await get_user(user_id)
            if _u and _u.get("username"):
                _uname = "@" + _u["username"]
            elif _u and _u.get("full_name"):
                _uname = _u["full_name"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            else:
                _uname = "без ника"
            if user_id not in _perplexity_double_warned:
                _perplexity_double_warned.add(user_id)
                logging.warning(f'Perplexity repeat activation: warned user={user_id} code={_recent["code"]}')
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        "⚠️ <b>Повторная активация Perplexity</b> (клиент предупреждён)\n\n"
                        f"👤 {_uname} (<code>{user_id}</code>)\n"
                        f"🔑 Уже активирован: <code>{_recent['code']}</code>\n"
                        f"📦 Тариф: <b>{_recent['plan']}</b>\n"
                        f"⏱ Дата: <b>{_us}</b>\n\n"
                        "Если клиент нажмёт «Попробовать снова» — активирует на другой аккаунт.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                return _resp({"error": (
                    "⚠️ На твой аккаунт уже активирована подписка Perplexity.\n\n"
                    "Если оформляешь на ДРУГОЙ аккаунт (например, для друга) — "
                    "нажми «Попробовать снова», и активация пройдёт.\n\n"
                    "Если это случайно — напиши Александру."
                )})
            else:
                _perplexity_double_warned.discard(user_id)
                logging.info(f"Perplexity forced re-activation user={user_id}")
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        "✅ <b>Повторная активация Perplexity — подтверждена</b>\n\n"
                        f"👤 {_uname} (<code>{user_id}</code>) активирует ещё раз (другой аккаунт).",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
    except Exception as _dbl_e:
        logging.error(f"perplexity double-activation check: {_dbl_e}")

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=30)
        ) as _s:
            async with _s.post(
                "https://bypriceactivate.pro/api/activate",
                json={"code": code, "org_id": org_id},
                headers={"Content-Type": "application/json"},
            ) as _r:
                try:
                    _rd = await _r.json()
                except Exception:
                    _rd = {}

                if _r.status == 201:
                    bpa_order_id = _rd.get("order_id")
                    if not bpa_order_id:
                        return _resp({"error": "Сервис не вернул order_id."})

                    _pool3 = await get_pool()
                    async with _pool3.acquire() as _c3:
                        await _c3.execute(
                            "UPDATE perplexity_pending_activations "
                            "SET org_id=$1, bpa_order_id=$2 WHERE user_id=$3",
                            org_id, bpa_order_id, user_id
                        )
                    asyncio.create_task(_perplexity_activation_polling_job(
                        bpa_order_id, code, user_id, order_id, plan_name, org_id
                    ))
                    logging.info(
                        f"Perplexity activation: bpa={bpa_order_id} user={user_id} code={code}"
                    )
                    return _resp({"order_id": bpa_order_id, "status": "queued"})

                elif _r.status == 409:
                    _detail = _rd.get("detail", "")
                    if "already claimed" in _detail:
                        _msg = "Код уже активирован. Напиши Александру."
                    elif "out of stock" in _detail:
                        _msg = "Временно нет активаций. Александр активирует вручную."
                        try:
                            await bot.send_message(
                                ADMIN_ID,
                                f"🚨 <b>Perplexity — нет стока!</b>\n"
                                f"👤 <code>{user_id}</code> ({plan_name})\n"
                                f"Пополни коды на bypriceactivate.pro",
                                parse_mode="HTML"
                            )
                        except Exception:
                            pass
                    else:
                        _msg = _detail or "Ошибка кода."
                    return _resp({"error": _msg})

                elif _r.status == 404:
                    return _resp({"error": "Код не найден. Напиши Александру."})

                else:
                    logging.error(f"bypriceactivate.pro HTTP {_r.status}: {str(_rd)[:200]}")
                    return _resp({"error": f"Ошибка ({_r.status}). Попробуй ещё раз."})

    except aiohttp.ClientError as _e:
        logging.error(f"Perplexity activate network: {_e}")
        return _resp({"error": "Нет связи с сервисом. Попробуй ещё раз."})
    except Exception as _e:
        logging.error(f"Perplexity activate error: {_e}", exc_info=True)
        return _resp({"error": "Внутренняя ошибка. Напиши Александру."})


async def api_activate_perplexity_status_handler(request: web.Request) -> web.Response:
    """GET /api/activate-perplexity-status/{order_id}"""
    import json as _j2
    try:
        bpa_order_id = int(request.match_info.get("order_id", ""))
    except ValueError:
        return web.Response(
            text=_j2.dumps({"status": "not_found"}),
            content_type="application/json", status=400
        )
    result = _perplexity_job_results.get(bpa_order_id)
    if result is None:
        return web.Response(
            text=_j2.dumps({"status": "not_found"}),
            content_type="application/json", status=404
        )
    return web.Response(
        text=_j2.dumps(result, ensure_ascii=False),
        content_type="application/json"
    )


# ─── Callback: клиент переоткрывает WebApp сам ───────────────────────────────


# ── Link-pay: инструкции после оплаты (оплата по ссылке) ─────────────────────

_LINKPAY_DEFAULT_INSTR = (
    "1. Зайди на сайт сервиса и авторизуйся в СВОЙ аккаунт.\n"
    "2. Открой раздел тарифов / подписки.\n"
    "3. Выбери нужный тариф (и регион, если требуется).\n"
    "4. Дойди до страницы оплаты и СКОПИРУЙ ссылку на оплату.\n"
    "5. Нажми кнопку ниже и пришли эту ссылку."
)


async def _is_linkpay(shop_key: str) -> bool:
    if not shop_key:
        return False
    try:
        return (await get_setting(f"linkpay:enabled:{shop_key}", "0") or "0") == "1"
    except Exception:
        return False


async def _send_linkpay_instructions(user_id, shop_key, service_name, plan_name,
                                     order_id, amount_rub, delayed_note=""):
    """После оплаты: создаём link-pay заказ и шлём клиенту инструкцию + кнопку отправки ссылки."""
    try:
        try:
            _pool = await get_pool()
            async with _pool.acquire() as _c:
                _u = await _c.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
            _uname = (_u["username"] if _u else "") or ""
        except Exception:
            _uname = ""
        await create_linkpay_order(user_id, _uname, order_id, shop_key,
                                   service_name, plan_name, amount_rub)
        try:
            instr = await get_setting(f"linkpay:instructions:{shop_key}", "") or _LINKPAY_DEFAULT_INSTR
        except Exception:
            instr = _LINKPAY_DEFAULT_INSTR
        text = (
            f"🎉 <b>Оплата прошла!</b>\n\n"
            f"📦 <b>{service_name}</b>\n\n"
            f"Чтобы оформить подписку, получи ссылку на оплату по инструкции "
            f"и пришли её сюда 👇\n\n"
            f"<b>📋 Инструкция:</b>\n{instr}\n"
            f"{delayed_note}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📎 Отправить ссылку на оплату",
                                  callback_data=f"linkpay_send:{order_id}")],
            [InlineKeyboardButton(text="❓ Нужна помощь", callback_data="linkpay_help")],
        ])
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logging.error(f"_send_linkpay_instructions uid={user_id}: {e}")


async def process_linkpay_link(user_id, text) -> bool:
    """Если у юзера есть link-pay заказ (awaiting_link) и в тексте есть ссылка —
    фиксируем её, уведомляем админа. Возвращает True если обработали."""
    try:
        import re as _re
        txt = (text or "").strip()
        _pool = await get_pool()
        async with _pool.acquire() as _c:
            row = await _c.fetchrow(
                "SELECT * FROM linkpay_orders WHERE user_id=$1 AND status='awaiting_link' "
                "ORDER BY created_at DESC LIMIT 1", user_id)
        if not row:
            return False
        order = dict(row)
        m = _re.search(r"https?://\S+", txt)
        if not m:
            return False  # нет ссылки — пусть обработает консультант
        link = m.group(0)
        domains = (await get_setting(f"linkpay:domains:{order['service_key']}", "") or "").strip()
        if domains:
            allowed = [d.strip().lower() for d in domains.split(",") if d.strip()]
            host = _re.sub(r"^https?://", "", link).split("/")[0].lower()
            if not any(host == d or host.endswith("." + d) for d in allowed):
                await bot.send_message(
                    user_id,
                    f"⚠️ Ссылка должна быть с сайта сервиса ({', '.join(allowed)}). Проверь и пришли снова.")
                return True
        await set_linkpay_link(order["fk_order_id"], link)
        await bot.send_message(
            user_id,
            "✅ <b>Ссылка получена!</b>\n\nАлександр оплатит её в ближайшее время — "
            "подписка придёт сюда. Спасибо за покупку! 🙌",
            parse_mode="HTML")
        uname = order.get("username") or ""
        tag = f"@{uname}" if uname else f"id{user_id}"
        admin_text = (
            f"💳 <b>Заказ на оплату по ссылке</b>\n\n"
            f"👤 {tag} (<code>{user_id}</code>)\n"
            f"📦 {order['service_name']}\n"
            f"🎫 Тариф: <b>{order.get('plan_name') or '—'}</b>\n"
            f"💵 Оплачено клиентом: <b>{order['amount_rub']}₽</b>\n"
            f"🔗 Ссылка: {link}\n"
            f"🆔 <code>{order['fk_order_id']}</code>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подписка готова", callback_data=f"lp_done:{order['fk_order_id']}")],
            [InlineKeyboardButton(text="✍️ Уточнение",       callback_data=f"lp_clarify:{order['fk_order_id']}")],
            [InlineKeyboardButton(text="🗑 Отменить заказ",   callback_data=f"lp_cancel:{order['fk_order_id']}")],
        ])
        try:
            _m = await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML",
                                        reply_markup=kb, disable_web_page_preview=True)
            await set_linkpay_admin_msg(order["fk_order_id"], _m.message_id)
        except Exception as _e:
            logging.error(f"linkpay admin notify: {_e}")
        await log_event(user_id, "linkpay_link", f"order={order['fk_order_id']} svc={order['service_key']}")
        return True
    except Exception as e:
        logging.error(f"process_linkpay_link: {e}")
        return False


# ── Creds: оформление по логину/паролю (Zoom, Krea, YouTube и т.п.) ──────────

_CREDS_DEFAULT_INSTR = (
    "Для оформления подписки нужны <b>email и пароль</b> от твоего аккаунта сервиса.\n"
    "Нажми кнопку ниже и пришли их по очереди. После оформления рекомендуем сменить пароль."
)


async def _is_creds(shop_key: str) -> bool:
    if not shop_key:
        return False
    try:
        return (await get_setting(f"creds:enabled:{shop_key}", "0") or "0") == "1"
    except Exception:
        return False


async def _send_creds_instructions(user_id, shop_key, service_name, plan_name,
                                   order_id, amount_rub, delayed_note=""):
    """После оплаты: создаём creds-заказ и просим клиента прислать данные аккаунта."""
    try:
        try:
            _pool = await get_pool()
            async with _pool.acquire() as _c:
                _u = await _c.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
            _uname = (_u["username"] if _u else "") or ""
        except Exception:
            _uname = ""
        await create_linkpay_order(user_id, _uname, order_id, shop_key,
                                   service_name, plan_name, amount_rub,
                                   kind="creds", status="awaiting_creds")
        try:
            instr = await get_setting(f"creds:instructions:{shop_key}", "") or _CREDS_DEFAULT_INSTR
        except Exception:
            instr = _CREDS_DEFAULT_INSTR
        text = (
            f"🎉 <b>Оплата прошла!</b>\n\n"
            f"📦 <b>{service_name}</b>\n\n"
            f"{instr}\n{delayed_note}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔐 Отправить данные аккаунта",
                                  callback_data=f"creds_send:{order_id}")],
            [InlineKeyboardButton(text="❓ Нужна помощь", callback_data="linkpay_help")],
        ])
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logging.error(f"_send_creds_instructions uid={user_id}: {e}")
