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
    _verify_tg_init_data, _video_history, activate_chatgpt, bot, dp, build_system_prompt, claude_client,
    clean_reply, pending_fk_payments, plan_name_to_key, strip_surrogates,
    CLAUDE_PROVIDERS, CLAUDE_PROVIDER_ORDER, CLAUDE_DEFAULT_PROVIDER,
    claude_provider_base, claude_provider_name,
    GPT_PROVIDERS, GPT_PROVIDER_ORDER, GPT_DEFAULT_PROVIDER,
    gpt_provider_base, gpt_provider_name,
)
from runtime_state import (
    rt,
)
from db import (
    _extract_email_from_token, add_coins, add_credits_batch, delete_claude_pending_activation, delete_pending_activation, ensure_user,
    fk_get_order, fk_mark_paid, get_claude_pending_activation, get_coins, get_credits, get_next_claude_code,
    get_next_gpt_code, get_pending_activation, get_pool, get_setting, set_setting, get_user, is_blocked,
    count_claude_free_by_provider, count_claude_free_by_provider_plan, count_gpt_free_by_provider,
    log_event, log_payment, mark_claude_code_used, mark_gpt_code_used, release_claude_code, release_gpt_code,
    save_claude_pending_activation, save_pending_activation,
    get_ref_premium, premium_ref_earned_this_month, log_premium_ref,
    get_next_perplexity_code, release_perplexity_code, mark_perplexity_code_used,
    save_perplexity_pending_activation, get_perplexity_pending_activation, delete_perplexity_pending_activation,
    create_linkpay_order, get_linkpay_order, set_linkpay_link, set_linkpay_status, set_linkpay_admin_msg,
    set_linkpay_email,
    get_pending_activation_by_code, get_claude_pending_activation_by_code,
    get_perplexity_pending_activation_by_code,
)
from keyboards import (
    _eib, kb_admin_panel, tg_emoji, tg_emoji_ui,
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


# Антиспам админ-алертов: одинаковый контекст (клиент+операция) — не чаще 1 раза в 10 мин
_admin_err_at: dict = {}

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

    # Троттлинг: одинаковый контекст не чаще раза в 10 мин (клиент, тыкающий подряд, не спамит)
    import time as _t_ae
    _now_ae = _t_ae.time()
    if _now_ae - _admin_err_at.get(context, 0.0) < 600:
        return
    _admin_err_at[context] = _now_ae

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
        # Атомарно захватываем «бонус ещё не выплачен» — защита от гонки двух параллельных платежей
        _claim = await conn.execute(
            "UPDATE users SET ref_bonus_paid=TRUE WHERE user_id=$1 AND ref_bonus_paid=FALSE", user_id
        )
        if _claim.split()[-1] == "0":
            # уже обработано другим платежом/потоком — выходим без повторного начисления
            return
        # Если реферер заблокирован - бонус не платим (флаг уже установлен выше)
        if await is_blocked(referrer_id):
            logging.info(f"Ref bonus SKIPPED: referrer {referrer_id} is blocked")
            return
        # Считаем сколько у реферера уже было платящих (без текущего, который мы только что пометили)
        paid_count = (await conn.fetchval(
            "SELECT COUNT(*) FROM users WHERE referred_by=$1 AND ref_bonus_paid=TRUE",
            referrer_id
        ) or 1) - 1

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


_conv_loaded: set = set()


async def claude_with_search(uid: int, user_text: str) -> str:
    conv = _get_conv(uid)
    # Подтягиваем историю из БД один раз за процесс (переживает рестарт/деплой)
    if uid not in _conv_loaded:
        _conv_loaded.add(uid)
        try:
            from db import load_consultant_conv
            _saved = await load_consultant_conv(uid)
            if _saved and not conv:
                conv.extend(_saved)
        except Exception as _le:
            logging.warning(f"load conv {uid}: {_le}")

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
            try:
                from db import save_consultant_conv
                await save_consultant_conv(uid, conv[-20:])
            except Exception:
                pass
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
            try:
                from db import save_consultant_conv
                await save_consultant_conv(uid, conv[-20:])
            except Exception:
                pass
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

async def _show_profile(message: Message, user, edit: bool = False):
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
                SELECT service_key, service_name, plan_name, expires_at
                FROM user_subscriptions
                WHERE user_id=$1 AND is_active=TRUE
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
        active_lines, expired_lines = [], []
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
            _line = f"{icon} <b>{s['service_name']}{plan}</b> - до {exp} ({days_left} дн.)"
            # детали аккаунта под подпиской: GPT → почта, Claude → Org ID + код,
            # creds → почта+пароль. Заказ по ссылке оплаты → деталей нет, только тариф.
            _sk = (s["service_key"] or "").lower()
            _sn = (s["service_name"] or "").lower()
            _det = []
            try:
                if "claude" in _sk or "claude" in _sn:
                    _r = await pool.fetchrow(
                        "SELECT code, org_id FROM claude_codes WHERE used_by=$1 "
                        "AND org_id IS NOT NULL AND org_id<>'' ORDER BY used_at DESC LIMIT 1", uid)
                    if _r:
                        if _r["org_id"]: _det.append(f"   🏢 Org ID: <code>{_r['org_id']}</code>")
                        if _r["code"]:   _det.append(f"   🎟 Код: <code>{_r['code']}</code>")
                elif "gpt" in _sk or "chatgpt" in _sn or "gpt" in _sn:
                    _r = await pool.fetchrow(
                        "SELECT code, email FROM gpt_codes WHERE used_by=$1 ORDER BY used_at DESC LIMIT 1", uid)
                    if _r:
                        if _r["email"]: _det.append(f"   📧 Email: <code>{_r['email']}</code>")
                        if _r["code"]:  _det.append(f"   🎟 Код: <code>{_r['code']}</code>")
                else:
                    _r = await pool.fetchrow(
                        "SELECT account_email, account_pass FROM linkpay_orders "
                        "WHERE user_id=$1 AND service_name=$2 AND (account_email<>'' OR account_pass<>'') "
                        "ORDER BY created_at DESC LIMIT 1", uid, s["service_name"])
                    if _r:
                        if _r["account_email"]: _det.append(f"   📧 Почта: <code>{_r['account_email']}</code>")
                        if _r["account_pass"]:  _det.append(f"   🔑 Пароль: <code>{_r['account_pass']}</code>")
            except Exception:
                pass
            if _det:
                _line += "\n" + "\n".join(_det)
            if s["expires_at"] <= now:
                # \u0438\u0441\u0442\u0451\u043a\u0448\u0430\u044f: \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u043c \u00ab\u0438\u0441\u0442\u0435\u043a\u043b\u0430 DATE\u00bb, \u0431\u0435\u0437 \u0441\u0447\u0451\u0442\u0447\u0438\u043a\u0430 \u0434\u043d\u0435\u0439
                _el = f"\u26d4 <b>{s['service_name']}{plan}</b> - \u0438\u0441\u0442\u0435\u043a\u043b\u0430 {exp}"
                if _det:
                    _el += "\n" + "\n".join(_det)
                expired_lines.append(_el)
            else:
                active_lines.append(_line)
        _parts = []
        _parts.append(("\u2705 <b>\u0410\u043a\u0442\u0438\u0432\u043d\u044b\u0435:</b>\n" + "\n".join(active_lines))
                      if active_lines else "<i>\u0410\u043a\u0442\u0438\u0432\u043d\u044b\u0445 \u043f\u043e\u0434\u043f\u0438\u0441\u043e\u043a \u043d\u0435\u0442.</i>")
        if expired_lines:
            _parts.append("\u26d4 <b>\u0418\u0441\u0442\u0451\u043a\u0448\u0438\u0435:</b>\n" + "\n".join(expired_lines))
        subs_block = "\n\n\U0001f4e6 <b>\u041c\u043e\u0438 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0438:</b>\n" + "\n\n".join(_parts)

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
                text=f"⚡ Активировать Claude {_cp.get('plan_name', '')}", style="success",
                callback_data="claude_reopen_webapp"
            )]]
    except Exception:
        pass

    kb_profile = InlineKeyboardMarkup(inline_keyboard=[
        *_claude_pending_btn,
        [_eib("Пригласить друга", "menu_ref")],
        [_eib("Мои подписки", "menu_subs"),
         _eib("Покупки", "profile_history")],
        [_eib("Главное меню", "back_main")],
        [_eib("Купить кредиты", "menu_buy"),
         _eib("Избранное", "menu_favorites")],
    ])
    _txt = strip_surrogates(text)
    if edit:
        try:
            await message.edit_text(_txt, reply_markup=kb_profile, parse_mode="HTML")
            return
        except Exception:
            # сообщение с медиа/уже удалено — заменяем новым
            try:
                await message.delete()
            except Exception:
                pass
    try:
        await message.answer(_txt, reply_markup=kb_profile, parse_mode="HTML")
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

# Заголовки «не кэшировать» для всех WebApp-страниц: иначе после деплоя у клиента
# остаётся старый JS и новые экраны/логика не работают.
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Expires": "0",
}


async def webapp_chatgpt_handler(request: web.Request) -> web.Response:
    try:
        with open(_WEBAPP_HTML_PATH, "r", encoding="utf-8") as _f:
            _html = _f.read()
        return web.Response(text=_html, content_type="text/html", charset="utf-8",
                            headers=_NO_CACHE_HEADERS)
    except FileNotFoundError:
        return web.Response(text="Mini App not found", status=404)


async def webapp_admin_handler(request: web.Request) -> web.Response:
    """Отдаёт HTML админ-панели (Mini App)."""
    try:
        with open(_ADMIN_WEBAPP_HTML_PATH, "r", encoding="utf-8") as _f:
            _html = _f.read()
        return web.Response(text=_html, content_type="text/html", charset="utf-8",
                            headers=_NO_CACHE_HEADERS)
    except FileNotFoundError:
        return web.Response(text="Admin Mini App not found", status=404)


async def api_admin_overview_handler(request: web.Request) -> web.Response:
    """Данные дашборда админки. Доступ только админу (по initData)."""
    import json as _json
    try:
        try:
            _body = await request.json()
        except Exception:
            _body = {}
        init_data = (_body.get("initData") if isinstance(_body, dict) else None) or request.query.get("initData", "")
        uid = _verify_tg_init_data(init_data)
        if uid != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)

        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT COUNT(*) AS c, COALESCE(SUM(amount_rub),0) AS r FROM fk_orders "
                "WHERE status='paid' AND paid_at >= CURRENT_DATE"
            )
            new_users = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE created_at >= CURRENT_DATE"
            ) or 0
            wk = await conn.fetch(
                "SELECT date_trunc('day', paid_at) AS d, COALESCE(SUM(amount_rub),0) AS s "
                "FROM fk_orders WHERE status='paid' AND paid_at >= CURRENT_DATE - INTERVAL '6 days' "
                "GROUP BY d ORDER BY d"
            )
            svc = await conn.fetch(
                "SELECT pack, COALESCE(SUM(amount_rub),0) AS r FROM fk_orders "
                "WHERE status='paid' AND pack LIKE 'shop:%' AND paid_at >= CURRENT_DATE GROUP BY pack"
            )

        import datetime as _dt_ov
        today = _dt_ov.date.today()
        wkmap = {}
        for r in wk:
            dd = r["d"]
            dd = dd.date() if hasattr(dd, "date") else dd
            wkmap[dd] = int(r["s"] or 0)
        _wd_ov = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]
        week = []
        for i in range(7):
            _dq = today - _dt_ov.timedelta(days=6 - i)
            week.append({"value": wkmap.get(_dq, 0), "date": _dq.isoformat(), "label": _wd_ov[_dq.weekday()]})

        agg = {}
        for r in svc:
            parts = (r["pack"] or "").split(":")
            k = parts[1] if len(parts) > 1 else "?"
            nm = (SHOP_CATALOG.get(k, {}) or {}).get("name", k)
            agg[nm] = agg.get(nm, 0) + int(r["r"] or 0)
        by_service = [{"label": n, "val": v} for n, v in sorted(agg.items(), key=lambda x: -x[1])][:6]

        return web.json_response({
            "ok": True,
            "orders": int(row["c"] or 0),
            "revenue": int(row["r"] or 0),
            "newUsers": int(new_users),
            "week": week,
            "byService": by_service,
        })
    except Exception as _e:
        logging.error(f"api_admin_overview: {_e}")
        return web.json_response({"ok": False, "error": "server"}, status=500)


def _admin_uid_from_body(body) -> "int | None":
    init_data = (body.get("initData") if isinstance(body, dict) else None) or ""
    return _verify_tg_init_data(init_data)


async def api_admin_profit_handler(request: web.Request) -> web.Response:
    """Реальный отчёт прибыли по сервисам/тарифам + комиссия 2%. Admin-only."""
    import datetime as _dt_pf
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        period = (body.get("period") or "day")
        day_off = int(body.get("dayOffset") or 0)
        today = _dt_pf.date.today()
        if period == "week":
            since, until = today - _dt_pf.timedelta(days=6), today + _dt_pf.timedelta(days=1)
        elif period == "month":
            since, until = today - _dt_pf.timedelta(days=29), today + _dt_pf.timedelta(days=1)
        else:
            since = today - _dt_pf.timedelta(days=day_off); until = since + _dt_pf.timedelta(days=1)
        rate = float(await get_setting("cost_usd_rate", "90") or "90")
        pool = await get_pool()
        async with pool.acquire() as conn:
            shop_rows = await conn.fetch(
                "SELECT pack, COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS rev FROM fk_orders "
                "WHERE status='paid' AND pack LIKE 'shop:%' AND paid_at>=$1 AND paid_at<$2 GROUP BY pack",
                since, until)
            nsg = await conn.fetchrow(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(price_rub),0) AS rev, COALESCE(SUM(price_usd),0) AS usd "
                "FROM nsgifts_orders WHERE status='fulfilled' AND created_at>=$1 AND created_at<$2", since, until)
            cr = await conn.fetchrow(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS rev FROM fk_orders "
                "WHERE status='paid' AND paid_at>=$1 AND paid_at<$2 "
                "AND (pack IS NULL OR (pack NOT LIKE 'shop:%' AND pack NOT LIKE 'nsg:%'))", since, until)
        by = {}; total_rev = 0; total_cost = 0
        for r in shop_rows:
            parts = (r["pack"] or "").split(":")
            k = parts[1] if len(parts) > 1 else ""
            idx = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            scat = SHOP_CATALOG.get(k, {}) or {}; nm = scat.get("name", k)
            plans = scat.get("plans", []); pname = plans[idx]["name"] if 0 <= idx < len(plans) else f"#{idx}"
            usd = float(await get_setting(f"cost_usd:{k}:{idx}", "0") or "0")
            unit = round(usd * rate) if usd > 0 else 0
            cost = unit * r["cnt"]
            d = by.setdefault(nm, {"name": nm, "cnt": 0, "rev": 0, "cost": 0, "missing": False, "plans": {}})
            d["cnt"] += r["cnt"]; d["rev"] += int(r["rev"]); d["cost"] += cost
            _pl = d["plans"].setdefault(pname, {"cnt": 0, "rev": 0, "cost": 0, "missing": False})
            _pl["cnt"] += r["cnt"]; _pl["rev"] += int(r["rev"]); _pl["cost"] += cost
            if unit == 0:
                d["missing"] = True; _pl["missing"] = True
            total_rev += int(r["rev"]); total_cost += cost
        services = []
        for nm, d in sorted(by.items(), key=lambda x: -x[1]["rev"]):
            services.append({"name": nm, "cnt": d["cnt"], "rev": d["rev"], "cost": d["cost"],
                             "profit": d["rev"] - d["cost"], "missing": d["missing"],
                             "plans": [{"name": pn, "cnt": pv["cnt"], "rev": pv["rev"], "cost": pv["cost"],
                                        "profit": pv["rev"] - pv["cost"], "missing": pv["missing"]}
                                       for pn, pv in sorted(d["plans"].items(), key=lambda x: -x[1]["rev"])]})
        if (nsg["cnt"] or 0):
            nrev = int(nsg["rev"] or 0); ncost = round(float(nsg["usd"] or 0) * rate)
            total_rev += nrev; total_cost += ncost
            services.append({"name": "App Store", "cnt": nsg["cnt"], "rev": nrev, "cost": ncost,
                             "profit": nrev - ncost, "missing": False, "plans": []})
        credits = {"cnt": int(cr["cnt"] or 0), "rev": int(cr["rev"] or 0)}
        total_rev += credits["rev"]
        com = round(total_rev * 0.02); profit = total_rev - total_cost - com
        margin = round(profit / total_rev * 100) if total_rev else 0
        return web.json_response({"ok": True, "services": services, "credits": credits,
                                  "totals": {"rev": total_rev, "cost": total_cost, "commission": com,
                                             "profit": profit, "margin": margin}, "rate": rate})
    except Exception as _e:
        logging.error(f"api_admin_profit: {_e}")
        return web.json_response({"ok": False, "error": "server"}, status=500)


async def api_admin_prices_handler(request: web.Request) -> web.Response:
    """Текущие цены ₽ и закуп $ по всем сервисам/тарифам. Admin-only."""
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        rate = float(await get_setting("cost_usd_rate", "90") or "90")
        services = []
        for k, scat in SHOP_CATALOG.items():
            plans = scat.get("plans", [])
            if not plans:
                continue
            pl = []
            for i, p in enumerate(plans):
                usd = float(await get_setting(f"cost_usd:{k}:{i}", "0") or "0")
                price = int(p.get("price") or 0)
                marg = round((price - usd * rate) / price * 100) if price > 0 else 0
                man = (await get_setting(f"manual:{k}:{i}", "0") or "0") == "1"
                pl.append({"idx": i, "name": p.get("name", ""), "price": price, "costUsd": usd, "margin": marg, "manual": man, "desc": p.get("desc", "")})
            services.append({"key": k, "name": scat.get("name", k), "emoji": scat.get("emoji", ""), "desc": scat.get("desc", ""), "plans": pl})
        return web.json_response({"ok": True, "rate": rate, "services": services})
    except Exception as _e:
        logging.error(f"api_admin_prices: {_e}")
        return web.json_response({"ok": False, "error": "server"}, status=500)


async def api_admin_prices_save_handler(request: web.Request) -> web.Response:
    """Сохранение цен ₽, закупа $ и курса. Admin-only."""
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        if body.get("rate") is not None:
            try:
                await set_setting("cost_usd_rate", str(int(float(body["rate"]))))
            except Exception:
                pass
        items = body.get("items") or []
        pool = await get_pool()
        async with pool.acquire() as conn:
            for it in items:
                try:
                    k = str(it.get("key", "")); idx = int(it.get("idx", 0))
                    pname = str(it.get("name", ""))
                    if not k:
                        continue
                    if it.get("costUsd") is not None:
                        await set_setting(f"cost_usd:{k}:{idx}", str(float(it["costUsd"])))
                    scat = SHOP_CATALOG.get(k)
                    if it.get("price") is not None:
                        pr = int(float(it["price"]))
                        # 1) пробуем по ИМЕНИ тарифа
                        _res = None
                        if pname:
                            _res = await conn.execute("UPDATE bot_shop_items SET price=$1 WHERE key=$2 AND plan_name=$3", pr, k, pname)
                        # 2) ФОЛБЭК: если по имени 0 строк (в БД другое имя — иной символ «×»,
                        #    пробелы и т.п.) — обновляем по ПОЗИЦИИ в том же порядке, что и форма цен.
                        #    Так цена гарантированно сохраняется и не «слетает» после деплоя.
                        if (not pname) or (isinstance(_res, str) and _res.strip().endswith(" 0")):
                            _row = await conn.fetchrow(
                                "SELECT plan_idx FROM bot_shop_items WHERE key=$1 AND enabled=TRUE AND plan_idx>=0 "
                                "ORDER BY sort_order, plan_idx OFFSET $2 LIMIT 1", k, idx)
                            if _row:
                                await conn.execute("UPDATE bot_shop_items SET price=$1 WHERE key=$2 AND plan_idx=$3", pr, k, _row["plan_idx"])
                        if scat and 0 <= idx < len(scat.get("plans", [])):
                            scat["plans"][idx]["price"] = pr
                    if it.get("desc") is not None and pname:
                        _pd = str(it["desc"])
                        _resd = await conn.execute("UPDATE bot_shop_items SET plan_desc=$1 WHERE key=$2 AND plan_name=$3", _pd, k, pname)
                        if isinstance(_resd, str) and _resd.strip().endswith(" 0"):
                            _rowd = await conn.fetchrow(
                                "SELECT plan_idx FROM bot_shop_items WHERE key=$1 AND enabled=TRUE AND plan_idx>=0 "
                                "ORDER BY sort_order, plan_idx OFFSET $2 LIMIT 1", k, idx)
                            if _rowd:
                                await conn.execute("UPDATE bot_shop_items SET plan_desc=$1 WHERE key=$2 AND plan_idx=$3", _pd, k, _rowd["plan_idx"])
                        if scat and 0 <= idx < len(scat.get("plans", [])):
                            scat["plans"][idx]["desc"] = _pd
                    if it.get("manual") is not None:
                        await set_setting(f"manual:{k}:{idx}", "1" if it.get("manual") else "0")
                except Exception as _ie:
                    logging.warning(f"prices_save item: {_ie}")
            # описание сервиса
            svc_key = str(body.get("svcKey", "")); svc_desc = body.get("svcDesc")
            if svc_key and svc_desc is not None:
                await conn.execute("UPDATE bot_shop_items SET service_desc=$1 WHERE key=$2", str(svc_desc), svc_key)
                _sc = SHOP_CATALOG.get(svc_key)
                if _sc:
                    _sc["desc"] = str(svc_desc)
        return web.json_response({"ok": True})
    except Exception as _e:
        logging.error(f"api_admin_prices_save: {_e}")
        return web.json_response({"ok": False, "error": "server"}, status=500)


async def api_admin_stats_handler(request: web.Request) -> web.Response:
    """Статистика за период. Admin-only."""
    import datetime as _dt_st
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        period = body.get("period") or "day"
        today = _dt_st.date.today()
        anchor = today
        _ds = body.get("date")
        if _ds:
            try:
                anchor = _dt_st.date.fromisoformat(str(_ds)); period = "day"
            except Exception:
                anchor = today
        if period == "week":
            since = anchor - _dt_st.timedelta(days=6); until = anchor + _dt_st.timedelta(days=1)
        elif period == "month":
            since = anchor - _dt_st.timedelta(days=29); until = anchor + _dt_st.timedelta(days=1)
        else:
            since = anchor; until = anchor + _dt_st.timedelta(days=1)
        pool = await get_pool()
        async with pool.acquire() as conn:
            o = await conn.fetchrow(
                "SELECT COUNT(*) AS c, COALESCE(SUM(amount_rub),0) AS r FROM fk_orders "
                "WHERE status='paid' AND paid_at>=$1 AND paid_at<$2", since, until)
            new_users = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE created_at>=$1 AND created_at<$2", since, until) or 0
            g = await conn.fetchrow(
                "SELECT COUNT(*) AS c, COALESCE(SUM(credits),0) AS cr FROM generations "
                "WHERE created_at>=$1 AND created_at<$2", since, until)
            by_type = await conn.fetch(
                "SELECT type, COUNT(*) AS c FROM generations WHERE created_at>=$1 AND created_at<$2 "
                "GROUP BY type ORDER BY c DESC", since, until)
            ser = await conn.fetch(
                "SELECT date_trunc('day', paid_at) AS d, COUNT(*) AS c FROM fk_orders "
                "WHERE status='paid' AND paid_at>=$1 AND paid_at<$2 GROUP BY d ORDER BY d",
                anchor - _dt_st.timedelta(days=6), anchor + _dt_st.timedelta(days=1))
        wd = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        smap = {}
        for r in ser:
            dd = r["d"]; dd = dd.date() if hasattr(dd, "date") else dd
            smap[dd] = int(r["c"] or 0)
        series = []
        for i in range(7):
            day = anchor - _dt_st.timedelta(days=6 - i)
            series.append({"label": wd[day.weekday()], "value": smap.get(day, 0), "date": day.isoformat()})
        return web.json_response({
            "ok": True, "orders": int(o["c"] or 0), "revenue": int(o["r"] or 0),
            "newUsers": int(new_users), "gens": int(g["c"] or 0), "creditsSpent": int(g["cr"] or 0),
            "anchor": anchor.isoformat(), "period": period,
            "series": series,
            "byType": [{"label": (r["type"] or "?"), "val": int(r["c"] or 0)} for r in by_type],
        })
    except Exception as _e:
        logging.error(f"api_admin_stats: {_e}")
        return web.json_response({"ok": False, "error": "server"}, status=500)


async def api_admin_sales_handler(request: web.Request) -> web.Response:
    """Продажи магазина по сервисам/тарифам за период или дату. Admin-only."""
    import datetime as _dt_sa
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        period = body.get("period") or "day"
        today = _dt_sa.date.today()
        _pd = body.get("date")
        if _pd:
            try:
                _a = _dt_sa.date.fromisoformat(str(_pd)); since, until = _a, _a + _dt_sa.timedelta(days=1)
            except Exception:
                since, until = today, today + _dt_sa.timedelta(days=1)
        elif period == "week":
            since, until = today - _dt_sa.timedelta(days=6), today + _dt_sa.timedelta(days=1)
        elif period == "month":
            since, until = today - _dt_sa.timedelta(days=29), today + _dt_sa.timedelta(days=1)
        else:
            since, until = today, today + _dt_sa.timedelta(days=1)
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT pack, COUNT(*) AS cnt, COALESCE(SUM(amount_rub),0) AS rev FROM fk_orders "
                "WHERE status='paid' AND pack LIKE 'shop:%' AND paid_at>=$1 AND paid_at<$2 GROUP BY pack",
                since, until)
            nsg = await conn.fetchrow(
                "SELECT COUNT(*) AS cnt, COALESCE(SUM(price_rub),0) AS rev FROM nsgifts_orders "
                "WHERE status='fulfilled' AND created_at>=$1 AND created_at<$2", since, until)
        by = {}; total = 0
        for r in rows:
            parts = (r["pack"] or "").split(":")
            k = parts[1] if len(parts) > 1 else ""
            idx = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            scat = SHOP_CATALOG.get(k, {}) or {}; nm = scat.get("name", k)
            plans = scat.get("plans", []); pname = plans[idx]["name"] if 0 <= idx < len(plans) else f"#{idx}"
            d = by.setdefault(nm, {"name": nm, "emoji": scat.get("emoji", ""), "cnt": 0, "rev": 0, "plans": {}})
            d["cnt"] += r["cnt"]; d["rev"] += int(r["rev"]); d["plans"][pname] = d["plans"].get(pname, 0) + r["cnt"]
            total += int(r["rev"])
        services = []
        for nm, d in sorted(by.items(), key=lambda x: -x[1]["rev"]):
            services.append({"name": nm, "emoji": d["emoji"], "cnt": d["cnt"], "rev": d["rev"],
                             "plans": [{"name": pn, "cnt": pc} for pn, pc in sorted(d["plans"].items(), key=lambda x: -x[1])]})
        if (nsg["cnt"] or 0):
            nrev = int(nsg["rev"] or 0); total += nrev
            services.append({"name": "App Store", "emoji": "🍎", "cnt": int(nsg["cnt"]), "rev": nrev, "plans": []})
        return web.json_response({"ok": True, "services": services, "total": total})
    except Exception as _e:
        logging.error(f"api_admin_sales: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_plan_add_handler(request: web.Request) -> web.Response:
    """Добавить тариф в сервис. Admin-only."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        k = str(body.get("key", "")); name = str(body.get("name", "")).strip()
        try: price = int(float(body.get("price") or 0))
        except Exception: price = 0
        scat = SHOP_CATALOG.get(k)
        if not k or not scat or not name or price <= 0:
            return web.json_response({"ok": False, "msg": "Нужно название и цена"})
        pool = await get_pool()
        async with pool.acquire() as conn:
            mx = await conn.fetchval("SELECT COALESCE(MAX(plan_idx),-1) FROM bot_shop_items WHERE key=$1", k)
            new_idx = int(mx) + 1
            await conn.execute(
                "INSERT INTO bot_shop_items (key, plan_idx, service_name, emoji, service_desc, plan_name, price, stars, plan_desc, sort_order) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (key, plan_idx) DO NOTHING",
                k, new_idx, scat.get("name", k), scat.get("emoji", ""), scat.get("desc", ""), name, price, 0, "", new_idx)
        scat.setdefault("plans", []).append({"name": name, "price": price, "stars": 0, "desc": ""})
        return web.json_response({"ok": True})
    except Exception as _e:
        logging.error(f"api_admin_plan_add: {_e}")
        return web.json_response({"ok": False, "msg": "Ошибка"}, status=500)


async def api_admin_plan_delete_handler(request: web.Request) -> web.Response:
    """Удалить тариф из сервиса. Admin-only."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        k = str(body.get("key", "")); name = str(body.get("name", ""))
        scat = SHOP_CATALOG.get(k)
        if not k or not scat or not name:
            return web.json_response({"ok": False})
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM bot_shop_items WHERE key=$1 AND plan_name=$2", k, name)
        scat["plans"] = [p for p in scat.get("plans", []) if p.get("name") != name]
        return web.json_response({"ok": True})
    except Exception as _e:
        logging.error(f"api_admin_plan_delete: {_e}")
        return web.json_response({"ok": False, "msg": "Ошибка"}, status=500)


async def api_admin_analytics_handler(request: web.Request) -> web.Response:
    """Аналитика: топ моделей/юзеров, активность, пользователи. Admin-only."""
    import datetime as _dt_an
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        today = _dt_an.date.today()
        pool = await get_pool()
        async with pool.acquire() as conn:
            tm = await conn.fetch(
                "SELECT model, COUNT(*) AS c FROM generations GROUP BY model ORDER BY c DESC LIMIT 6")
            tu = await conn.fetch(
                "SELECT g.user_id, u.username, COUNT(*) AS c FROM generations g "
                "LEFT JOIN users u ON g.user_id=u.user_id GROUP BY g.user_id, u.username "
                "ORDER BY c DESC LIMIT 6")
            act = await conn.fetch(
                "SELECT date_trunc('day', created_at) AS d, COUNT(*) AS c FROM generations "
                "WHERE created_at>=$1 GROUP BY d ORDER BY d", today - _dt_an.timedelta(days=8))
            total = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
            active30 = await conn.fetchval(
                "SELECT COUNT(DISTINCT user_id) FROM generations WHERE created_at>=$1",
                today - _dt_an.timedelta(days=30)) or 0
            new_today = await conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at>=CURRENT_DATE") or 0
            with_buy = await conn.fetchval("SELECT COUNT(DISTINCT user_id) FROM fk_orders WHERE status='paid'") or 0
        amap = {}
        for r in act:
            dd = r["d"]; dd = dd.date() if hasattr(dd, "date") else dd
            amap[dd] = int(r["c"] or 0)
        activity = [amap.get(today - _dt_an.timedelta(days=8 - i), 0) for i in range(9)]
        return web.json_response({
            "ok": True,
            "topModels": [{"label": (r["model"] or "?"), "val": int(r["c"] or 0)} for r in tm],
            "topUsers": [{"label": ("@" + r["username"]) if r["username"] else ("ID " + str(r["user_id"])), "val": int(r["c"] or 0)} for r in tu],
            "activity": activity,
            "users": {"total": int(total), "active30": int(active30), "newToday": int(new_today), "withBuy": int(with_buy)},
        })
    except Exception as _e:
        logging.error(f"api_admin_analytics: {_e}")
        return web.json_response({"ok": False, "error": "server"}, status=500)


async def api_admin_promos_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        from db import list_promos
        rows = await list_promos(only_active=False, limit=100)
        out = []
        for r in rows:
            exp = r.get("expires_at")
            out.append({"code": r.get("code"), "kind": r.get("kind"), "value": r.get("value"),
                        "maxUses": r.get("max_uses") or 0, "used": r.get("used_count") or 0,
                        "active": bool(r.get("active")),
                        "expires": exp.strftime("%d.%m.%Y") if exp else None})
        return web.json_response({"ok": True, "promos": out})
    except Exception as _e:
        logging.error(f"api_admin_promos: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_promo_create_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        from db import create_promo
        code = str(body.get("code", "")).strip().upper()
        kind = str(body.get("kind", "percent"))
        if kind not in ("percent", "credits"):
            kind = "percent"
        value = int(float(body.get("value") or 0))
        uses = int(float(body.get("uses") or 1))
        days = int(float(body.get("days") or 0))
        if not code or value <= 0:
            return web.json_response({"ok": False, "msg": "Код и значение обязательны"})
        ok, msg = await create_promo(code, kind, value, uses, days)
        return web.json_response({"ok": bool(ok), "msg": msg})
    except Exception as _e:
        logging.error(f"api_admin_promo_create: {_e}")
        return web.json_response({"ok": False, "msg": "Ошибка"}, status=500)


async def api_admin_promo_deactivate_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        from db import deactivate_promo
        ok = await deactivate_promo(str(body.get("code", "")).strip().upper())
        return web.json_response({"ok": bool(ok)})
    except Exception as _e:
        logging.error(f"api_admin_promo_deactivate: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_models_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        from config import IMAGE_MODELS, VIDEO_MODELS, EDIT_MODELS, ANIM_MODELS, DISABLED_MODELS
        secs = [("image", "Фото", IMAGE_MODELS), ("video", "Видео", VIDEO_MODELS),
                ("edit", "Редактирование", EDIT_MODELS), ("anim", "Анимация", ANIM_MODELS)]
        out = []
        for sec, title, d in secs:
            ms = [{"key": k, "name": m.get("name", k), "credits": m.get("credits", 0),
                   "enabled": k not in DISABLED_MODELS} for k, m in d.items()]
            out.append({"section": sec, "title": title, "models": ms})
        return web.json_response({"ok": True, "sections": out})
    except Exception as _e:
        logging.error(f"api_admin_models: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_model_toggle_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        from config import IMAGE_MODELS, VIDEO_MODELS, EDIT_MODELS, ANIM_MODELS, DISABLED_MODELS
        key = str(body.get("key", "")); section = str(body.get("section", ""))
        enabled = bool(body.get("enabled"))
        dmap = {"image": IMAGE_MODELS, "video": VIDEO_MODELS, "edit": EDIT_MODELS, "anim": ANIM_MODELS}
        models = dmap.get(section, {})
        if key not in models:
            return web.json_response({"ok": False})
        if enabled:
            DISABLED_MODELS.discard(key)
        else:
            DISABLED_MODELS.add(key)
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO bot_gen_prices (model_key, section, credits, enabled) VALUES ($1,$2,$3,$4) "
                "ON CONFLICT (model_key) DO UPDATE SET enabled=$4",
                key, section, models[key].get("credits", 10), enabled)
        return web.json_response({"ok": True, "enabled": enabled})
    except Exception as _e:
        logging.error(f"api_admin_model_toggle: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_orders_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        kinds = body.get("kinds")
        status = str(body.get("status") or "work")
        work_st = ["awaiting_link", "awaiting_payment", "awaiting_creds", "awaiting_setup"]
        pool = await get_pool()
        async with pool.acquire() as conn:
            if status == "done":
                rows = await conn.fetch("SELECT * FROM linkpay_orders WHERE status IN ('done') ORDER BY created_at DESC LIMIT 100")
            else:
                rows = await conn.fetch("SELECT * FROM linkpay_orders WHERE status = ANY($1::text[]) ORDER BY created_at DESC LIMIT 100", work_st)
        if kinds:
            rows = [r for r in rows if r.get("kind") in kinds]
        st = {"awaiting_link": "ждёт ссылку", "awaiting_payment": "ждёт оплаты",
              "awaiting_creds": "ждёт данные", "awaiting_setup": "оформляется",
              "done": "выполнен", "cancelled": "отменён"}
        out = []
        for r in rows:
            out.append({"id": r.get("fk_order_id"), "service": r.get("service_name"),
                        "plan": r.get("plan_name"), "kind": r.get("kind"),
                        "user": ("@" + r["username"]) if r.get("username") else ("id" + str(r.get("user_id"))),
                        "status": st.get(r.get("status"), r.get("status")), "link": r.get("payment_link")})
        return web.json_response({"ok": True, "orders": out})
    except Exception as _e:
        logging.error(f"api_admin_orders: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_order_action_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        oid = str(body.get("id", "")); action = str(body.get("action", ""))
        order = await get_linkpay_order(oid)
        if not order:
            return web.json_response({"ok": False, "msg": "Заказ не найден"})
        uid = order.get("user_id"); svc = order.get("service_name", "")
        if action == "done":
            await set_linkpay_status(oid, "done")
            try:
                await bot.send_message(uid, f"🎉 <b>Подписка оформлена!</b>\n\n📦 {svc}\n\nСпасибо за покупку! 🙌", parse_mode="HTML")
            except Exception: pass
        elif action == "cancel":
            await set_linkpay_status(oid, "cancelled")
            try:
                await bot.send_message(uid, f"❌ <b>Заказ отменён</b>\n\n📦 {svc}\n\nЕсли это ошибка — напиши @{PERSONAL_USERNAME}.", parse_mode="HTML")
            except Exception: pass
        elif action == "clarify":
            txt = str(body.get("text") or "Уточните, пожалуйста, детали заказа.")
            try:
                from db import add_order_msg
                await add_order_msg(oid, "admin", txt)
            except Exception: pass
            try:
                _rkb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✍️ Ответить", callback_data=f"cl_reply:{oid}")]])
                await bot.send_message(uid, f"✍️ <b>Сообщение от Александра по заказу:</b>\n\n{__import__('html').escape(txt)}\n\nНажми «Ответить», чтобы написать в ответ (например, прислать код).", parse_mode="HTML", reply_markup=_rkb)
            except Exception: pass
        else:
            return web.json_response({"ok": False, "msg": "Неизвестное действие"})
        return web.json_response({"ok": True})
    except Exception as _e:
        logging.error(f"api_admin_order_action: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_order_thread_handler(request: web.Request) -> web.Response:
    """История заказа (переписка) для Mini App. Admin-only."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        oid = str(body.get("id", ""))
        order = await get_linkpay_order(oid) or {}
        from db import get_order_thread
        msgs = await get_order_thread(oid)
        out = []
        for m in msgs:
            ts = m.get("created_at")
            out.append({"sender": m["sender"], "text": m.get("text") or "",
                        "date": ts.astimezone(_BOT_TZ).strftime("%d.%m %H:%M") if ts else ""})
        u = await get_user(order.get("user_id")) if order.get("user_id") else None
        tag = ("@" + u["username"]) if (u and u.get("username")) else (("id" + str(order.get("user_id"))) if order.get("user_id") else "—")
        _cr = order.get("created_at")
        _cr_s = _cr.astimezone(_BOT_TZ).strftime("%d.%m.%Y %H:%M") if _cr else ""
        return web.json_response({"ok": True, "order": {
            "id": oid,
            "service": order.get("service_name", ""), "plan": order.get("plan_name") or "",
            "status": order.get("status", ""), "user": tag, "amount": int(order.get("amount_rub") or 0),
            "kind": order.get("kind") or "", "uid": order.get("user_id"), "date": _cr_s,
            "email": order.get("account_email") or "", "passw": order.get("account_pass") or "",
            "link": order.get("payment_link") or ""},
            "messages": out})
    except Exception as _e:
        logging.error(f"api_admin_order_thread: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_order_delete_handler(request: web.Request) -> web.Response:
    """Удалить заказ и его переписку (для теста/чистки). Admin-only."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        oid = str(body.get("id", ""))
        if not oid:
            return web.json_response({"ok": False})
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM linkpay_orders WHERE fk_order_id=$1", oid)
            await conn.execute("DELETE FROM order_thread WHERE order_id=$1", oid)
        return web.json_response({"ok": True})
    except Exception as _e:
        logging.error(f"api_admin_order_delete: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_shop_orders_handler(request: web.Request) -> web.Response:
    """Заказы по авто-активации (chatgpt/claude/perplexity) и App Store с полной инфой. Admin-only."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        svc = str(body.get("svc", ""))
        pool = await get_pool()
        out = []
        if svc == "appstore":
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT n.fk_order_id, n.service_name, n.price_rub, n.status, n.created_at, n.pins_json, u.username, n.user_id "
                    "FROM nsgifts_orders n LEFT JOIN users u ON u.user_id=n.user_id "
                    "ORDER BY n.created_at DESC LIMIT 60")
            for r in rows:
                ts = r["created_at"]
                out.append({"id": r["fk_order_id"], "plan": r["service_name"] or "App Store",
                            "amount": int(r["price_rub"] or 0), "status": r["status"] or "",
                            "activated": (r["status"] == "fulfilled"),
                            "date": ts.astimezone(_BOT_TZ).strftime("%d.%m %H:%M") if ts else "",
                            "user": ("@" + r["username"]) if r["username"] else ("id" + str(r["user_id"])),
                            "acc": "", "code": (r["pins_json"] or "")[:120]})
            return web.json_response({"ok": True, "orders": out})
        if svc not in ("chatgpt", "claude", "perplexity"):
            return web.json_response({"ok": False, "msg": "Неизвестный сервис"})
        tbl = {"chatgpt": "gpt_codes", "claude": "claude_codes", "perplexity": "perplexity_codes"}[svc]
        acccol = "email" if svc == "chatgpt" else "org_id"
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT o.order_id, o.amount_rub, o.pack, o.paid_at, u.username, o.user_id "
                "FROM fk_orders o LEFT JOIN users u ON u.user_id=o.user_id "
                "WHERE o.status='paid' AND o.pack LIKE $1 ORDER BY o.paid_at DESC LIMIT 60",
                f"shop:{svc}:%")
            for r in rows:
                parts = (r["pack"] or "").split(":")
                idx = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                scat = SHOP_CATALOG.get(svc, {}) or {}; plans = scat.get("plans", [])
                pname = plans[idx]["name"] if 0 <= idx < len(plans) else f"#{idx}"
                cr = await conn.fetchrow(
                    f"SELECT code, {acccol} AS acc, used_at FROM {tbl} WHERE order_id=$1 LIMIT 1", r["order_id"])
                _man = (await get_setting(f"manual:{svc}:{idx}", "0") or "0") == "1"
                _done = (await get_setting(f"order_done:{r['order_id']}", "0") or "0") == "1"
                ts = r["paid_at"]
                import datetime as _dt_so
                _act = bool(cr and cr["used_at"]) or _done
                if _act:
                    _state = "done"
                elif _man:
                    _state = "work"
                elif ts and (_dt_so.datetime.now(_BOT_TZ) - ts.astimezone(_BOT_TZ)).total_seconds() > ACTIVATION_WINDOW_MIN * 60:
                    _state = "expired"
                else:
                    _state = "process"
                _stlabel = {"done": "активирован", "work": "в работе",
                            "process": "в процессе", "expired": "сессия истекла"}[_state]
                out.append({"id": r["order_id"], "plan": pname, "amount": int(r["amount_rub"] or 0),
                            "status": _stlabel, "state": _state, "manual": _man,
                            "activated": _act,
                            "date": ts.astimezone(_BOT_TZ).strftime("%d.%m %H:%M") if ts else "",
                            "user": ("@" + r["username"]) if r["username"] else ("id" + str(r["user_id"])),
                            "acc": (cr["acc"] if cr else "") or "", "code": (cr["code"] if cr else "") or ""})
        return web.json_response({"ok": True, "orders": out})
    except Exception as _e:
        logging.error(f"api_admin_shop_orders: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_shop_order_action_handler(request: web.Request) -> web.Response:
    """Действия по заказу авто-активации: ручная активация (другим кодом), возврат кода в пул, удаление. Admin-only."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        svc = str(body.get("svc", "")); oid = str(body.get("id", ""))
        action = str(body.get("action", "")); code = str(body.get("code", "")).strip()
        tblmap = {"chatgpt": "gpt_codes", "claude": "claude_codes", "perplexity": "perplexity_codes"}
        pool = await get_pool()
        if action == "delete":
            async with pool.acquire() as conn:
                if svc == "appstore":
                    await conn.execute("DELETE FROM nsgifts_orders WHERE fk_order_id=$1", oid)
                else:
                    await conn.execute("DELETE FROM fk_orders WHERE order_id=$1", oid)
            return web.json_response({"ok": True})
        tbl = tblmap.get(svc)
        if not tbl:
            return web.json_response({"ok": False, "msg": "Для этого сервиса доступно только удаление"})
        acccol = "email" if svc == "chatgpt" else "org_id"
        if action == "release":
            async with pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {tbl} SET is_used=FALSE, used_by=NULL, order_id=NULL, used_at=NULL, {acccol}=NULL WHERE order_id=$1",
                    oid)
            return web.json_response({"ok": True})
        if action == "manual":
            # Ручная активация: возвращаем закреплённый за заказом код в пул + помечаем заказ выполненным
            async with pool.acquire() as conn:
                await conn.execute(
                    f"UPDATE {tbl} SET is_used=FALSE, used_by=NULL, order_id=NULL, used_at=NULL, {acccol}=NULL WHERE order_id=$1",
                    oid)
                o = await conn.fetchrow("SELECT user_id FROM fk_orders WHERE order_id=$1", oid)
                uid = o["user_id"] if o else None
            await set_setting(f"order_done:{oid}", "1")
            if uid:
                try:
                    await bot.send_message(uid, "🎉 <b>Подписка активирована!</b>\n\nГотово, пользуйся 🙌", parse_mode="HTML")
                except Exception:
                    pass
            return web.json_response({"ok": True})
        return web.json_response({"ok": False})
    except Exception as _e:
        logging.error(f"api_admin_shop_order_action: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_user_find_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        q = str(body.get("q", "")).strip()
        if not q:
            return web.json_response({"ok": False, "msg": "Введите ID или @ник"})
        pool = await get_pool()
        async with pool.acquire() as conn:
            if q.startswith("@"):
                row = await conn.fetchrow("SELECT * FROM users WHERE lower(username)=lower($1)", q[1:])
            else:
                try:
                    _uid = int(q)
                except Exception:
                    return web.json_response({"ok": False, "msg": "Введите ID или @ник"})
                row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", _uid)
            if not row:
                return web.json_response({"ok": False, "msg": "Пользователь не найден"})
            u = dict(row); uid = u["user_id"]
            pur = await conn.fetchrow(
                "SELECT COUNT(*) AS c, COALESCE(SUM(amount_rub),0) AS s FROM fk_orders "
                "WHERE user_id=$1 AND status='paid'", uid)
        rp = await get_ref_premium(uid)
        cr = u.get("created_at")
        return web.json_response({"ok": True, "user": {
            "id": uid, "username": u.get("username") or "", "name": u.get("full_name") or "",
            "credits": int(u.get("credits") or 0), "blocked": bool(u.get("is_blocked")),
            "created": cr.strftime("%d.%m.%Y") if cr else "",
            "purchases": int(pur["c"] or 0), "spent": int(pur["s"] or 0),
            "refPremium": bool(rp and rp.get("ref_premium")),
            "refPct": (rp.get("ref_premium_pct") if rp else None)}})
    except Exception as _e:
        logging.error(f"api_admin_user_find: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_balance_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        from db import add_credits
        uid = int(body.get("id")); op = str(body.get("op", "")); amount = int(float(body.get("amount") or 0))
        pool = await get_pool()
        if op == "set":
            async with pool.acquire() as conn:
                await conn.execute("UPDATE users SET credits=$1 WHERE user_id=$2", max(0, amount), uid)
        elif op == "add":
            await add_credits(uid, amount)
        elif op == "deduct":
            await add_credits(uid, -amount)
        else:
            return web.json_response({"ok": False, "msg": "Неизвестная операция"})
        async with pool.acquire() as conn:
            bal = await conn.fetchval("SELECT credits FROM users WHERE user_id=$1", uid)
        return web.json_response({"ok": True, "balance": int(bal or 0)})
    except Exception as _e:
        logging.error(f"api_admin_balance: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_blocks_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, username FROM users WHERE is_blocked=1 ORDER BY user_id LIMIT 100")
        return web.json_response({"ok": True, "blocked": [
            {"id": r["user_id"], "username": r["username"] or ""} for r in rows]})
    except Exception as _e:
        logging.error(f"api_admin_blocks: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_block_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        from db import block_user, unblock_user
        uid = int(body.get("id")); blk = bool(body.get("block"))
        if blk:
            await block_user(uid)
        else:
            await unblock_user(uid)
        return web.json_response({"ok": True, "blocked": blk})
    except Exception as _e:
        logging.error(f"api_admin_block: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_referral_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        pct = float(await get_setting("ref_premium_pct", "10") or "10")
        cap = float(await get_setting("ref_premium_cap", "0") or "0")
        pool = await get_pool()
        async with pool.acquire() as conn:
            parts = await conn.fetch("SELECT user_id, username, ref_premium_pct FROM users WHERE ref_premium=TRUE")
            earned = await conn.fetch("SELECT referrer_id, COALESCE(SUM(amount_rub),0) AS s FROM ref_premium_log GROUP BY referrer_id")
        emap = {r["referrer_id"]: float(r["s"] or 0) for r in earned}
        partners = [{"id": r["user_id"], "username": r["username"] or "",
                     "pct": (r["ref_premium_pct"] if r["ref_premium_pct"] is not None else pct),
                     "earned": round(emap.get(r["user_id"], 0))} for r in parts]
        return web.json_response({"ok": True, "globalPct": pct, "cap": cap, "partners": partners})
    except Exception as _e:
        logging.error(f"api_admin_referral: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_referral_set_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        from db import set_ref_premium
        action = str(body.get("action", ""))
        if action == "global_pct":
            await set_setting("ref_premium_pct", str(float(body.get("value") or 0)))
        elif action == "cap":
            await set_setting("ref_premium_cap", str(int(float(body.get("value") or 0))))
        elif action == "add":
            uid = int(body.get("uid"))
            pv = body.get("pct")
            pctf = float(pv) if pv not in (None, "") else None
            await set_ref_premium(uid, True, pctf)
        elif action == "remove":
            await set_ref_premium(int(body.get("uid")), False, None)
        else:
            return web.json_response({"ok": False})
        return web.json_response({"ok": True})
    except Exception as _e:
        logging.error(f"api_admin_referral_set: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_svc_list_handler(request: web.Request) -> web.Response:
    """Список сервисов оплата-по-ссылке и вход-в-аккаунт с инструкциями/доменом. Admin-only."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        linkpay = []; creds = []
        for k, scat in SHOP_CATALOG.items():
            nm = scat.get("name", k)
            if (await get_setting(f"linkpay:enabled:{k}", "0") or "0") == "1":
                linkpay.append({"key": k, "name": nm,
                                "instructions": await get_setting(f"linkpay:instructions:{k}", ""),
                                "domain": await get_setting(f"linkpay:domains:{k}", "")})
            if (await get_setting(f"creds:enabled:{k}", "0") or "0") == "1":
                creds.append({"key": k, "name": nm,
                              "instructions": await get_setting(f"creds:instructions:{k}", "")})
        return web.json_response({"ok": True, "linkpay": linkpay, "creds": creds})
    except Exception as _e:
        logging.error(f"api_admin_svc_list: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_svc_save_handler(request: web.Request) -> web.Response:
    """Сохранить инструкцию/домен сервиса. Admin-only."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        kind = str(body.get("kind", "")); key = str(body.get("key", ""))
        if not key or kind not in ("linkpay", "creds"):
            return web.json_response({"ok": False})
        if body.get("instructions") is not None:
            await set_setting(f"{kind}:instructions:{key}", str(body.get("instructions")))
        if kind == "linkpay" and body.get("domain") is not None:
            await set_setting(f"linkpay:domains:{key}", str(body.get("domain")))
        return web.json_response({"ok": True})
    except Exception as _e:
        logging.error(f"api_admin_svc_save: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_miniapp_detail_handler(request: web.Request) -> web.Response:
    """Детали Mini App: последние активации + свободные коды. Admin-only."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        svc = str(body.get("service", ""))
        tbl = {"chatgpt": "gpt_codes", "claude": "claude_codes", "perplexity": "perplexity_codes"}.get(svc)
        if not tbl:
            return web.json_response({"ok": False})
        idcol = "email" if svc == "chatgpt" else "org_id"
        has_prov = svc in ("claude", "chatgpt")
        _pcol = ", provider" if has_prov else ""
        pool = await get_pool()
        async with pool.acquire() as conn:
            recent = await conn.fetch(
                f"SELECT c.code, c.used_by, c.used_at, c.order_id, c.{idcol} AS acc, u.username "
                f"FROM {tbl} c LEFT JOIN users u ON u.user_id=c.used_by "
                f"WHERE c.is_used=TRUE AND c.used_at IS NOT NULL ORDER BY c.used_at DESC LIMIT 15")
            free = await conn.fetch(f"SELECT code, plan{_pcol} FROM {tbl} WHERE is_used=FALSE ORDER BY id LIMIT 100")
            # «Ждущие» коды: выданы клиенту (is_used), но активация ещё не завершена (used_by IS NULL)
            pending = []
            if svc == "claude":
                pending = await conn.fetch(
                    "SELECT c.code, c.plan, c.provider, p.user_id, p.org_id, p.created_at, "
                    "       p.plan_name, u.username "
                    "FROM claude_codes c "
                    "LEFT JOIN claude_pending_activations p ON p.code=c.code "
                    "LEFT JOIN users u ON u.user_id=p.user_id "
                    "WHERE c.is_used=TRUE AND c.used_by IS NULL "
                    "ORDER BY c.id DESC LIMIT 100")
            elif svc == "chatgpt":
                pending = await conn.fetch(
                    "SELECT c.code, c.plan, c.provider, p.user_id, c.reserved_at AS created_at, "
                    "       p.plan_name, u.username "
                    "FROM gpt_codes c "
                    "LEFT JOIN gpt_pending_activations p ON p.code=c.code "
                    "LEFT JOIN users u ON u.user_id=p.user_id "
                    "WHERE c.is_used=TRUE AND c.used_by IS NULL "
                    "ORDER BY c.id DESC LIMIT 100")
        rec = []
        for r in recent:
            ua = r["used_at"]
            rec.append({"code": r["code"],
                        "user": ("@" + r["username"]) if r["username"] else ("id" + str(r["used_by"]) if r["used_by"] else "—"),
                        "date": ua.astimezone(_BOT_TZ).strftime("%d.%m %H:%M") if ua else "",
                        "order": r["order_id"] or "", "acc": r["acc"] or ""})
        freec = [dict({"code": r["code"], "plan": r["plan"]},
                      **({"provider": r["provider"]} if has_prov else {})) for r in free]
        resp = {"ok": True, "recent": rec, "free": freec, "freeCount": len(freec)}
        if has_prov:
            if svc == "claude":
                _pset, _reg, _order, _def, _pname, _countfn = (
                    "claude", CLAUDE_PROVIDERS, CLAUDE_PROVIDER_ORDER, CLAUDE_DEFAULT_PROVIDER,
                    claude_provider_name, count_claude_free_by_provider)
            else:
                _pset, _reg, _order, _def, _pname, _countfn = (
                    "gpt", GPT_PROVIDERS, GPT_PROVIDER_ORDER, GPT_DEFAULT_PROVIDER,
                    gpt_provider_name, count_gpt_free_by_provider)
            active = await get_setting(f"{_pset}_provider", _def) or _def
            if active not in _reg:
                active = _def
            failover = (await get_setting(f"{_pset}_failover", "1") or "1") == "1"
            freeby = await _countfn()
            _dis_raw = await get_setting(f"{_pset}_disabled", "") or ""
            _dis_set = {x for x in _dis_raw.split(",") if x}
            resp["providers"] = [
                {"key": p, "name": _pname(p), "free": int(freeby.get(p, 0)),
                 "disabled": (p in _dis_set)} for p in _order]
            resp["activeProvider"] = active
            resp["failover"] = failover
            pend = []
            for r in pending:
                ca = r["created_at"]
                pend.append({
                    "code": r["code"], "plan": r["plan"],
                    "provider": r["provider"], "providerName": _pname(r["provider"]),
                    "user": ("@" + r["username"]) if r["username"] else ("id" + str(r["user_id"]) if r["user_id"] else "—"),
                    "org": (r["org_id"] if svc == "claude" else "") or "",
                    "date": ca.astimezone(_BOT_TZ).strftime("%d.%m %H:%M") if ca else "",
                })
            resp["pending"] = pend
            resp["pendingCount"] = len(pend)
        return web.json_response(resp)
    except Exception as _e:
        logging.error(f"api_admin_miniapp_detail: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_code_delete_handler(request: web.Request) -> web.Response:
    """Удалить свободный код из пула. Admin-only."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        svc = str(body.get("service", "")); code = str(body.get("code", ""))
        tbl = {"chatgpt": "gpt_codes", "claude": "claude_codes", "perplexity": "perplexity_codes"}.get(svc)
        if not tbl or not code:
            return web.json_response({"ok": False})
        pool = await get_pool()
        async with pool.acquire() as conn:
            r = await conn.execute(f"DELETE FROM {tbl} WHERE code=$1 AND is_used=FALSE", code)
        deleted = r.split()[-1] if isinstance(r, str) else "0"
        return web.json_response({"ok": True, "deleted": int(deleted) if str(deleted).isdigit() else 0})
    except Exception as _e:
        logging.error(f"api_admin_code_delete: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_claude_provider_handler(request: web.Request) -> web.Response:
    """Выбор активного сайта авто-активации / тумблер фолбэка. Admin-only.
    service: 'claude' (по умолчанию) | 'chatgpt'."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        svc = str(body.get("service", "claude"))
        if svc == "chatgpt":
            _reg, _set = GPT_PROVIDERS, "gpt"
        else:
            _reg, _set = CLAUDE_PROVIDERS, "claude"
        kind = str(body.get("kind", ""))
        if kind == "set":
            prov = str(body.get("provider", ""))
            if prov not in _reg:
                return web.json_response({"ok": False, "msg": "Неизвестный сайт"})
            await set_setting(f"{_set}_provider", prov)
        elif kind == "failover":
            await set_setting(f"{_set}_failover", "1" if body.get("on") else "0")
        elif kind == "toggle":
            # Пауза/возврат сайта в цепочку активации (когда нет кодов или сайт сломан).
            prov = str(body.get("provider", ""))
            if prov not in _reg:
                return web.json_response({"ok": False, "msg": "Неизвестный сайт"})
            _cur = await get_setting(f"{_set}_disabled", "") or ""
            _dis = {p for p in _cur.split(",") if p}
            if body.get("off"):          # off=true → выключить (пауза)
                _dis.add(prov)
            else:                        # иначе → включить обратно
                _dis.discard(prov)
            await set_setting(f"{_set}_disabled", ",".join(sorted(_dis)))
            return web.json_response({"ok": True, "disabled": sorted(_dis)})
        else:
            return web.json_response({"ok": False})
        return web.json_response({"ok": True})
    except Exception as _e:
        logging.error(f"api_admin_claude_provider: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_claude_code_action_handler(request: web.Request) -> web.Response:
    """Действие над «ждущим» кодом: release (вернуть в пул) или delete. Admin-only.
    service: 'claude' (по умолчанию) | 'chatgpt'."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        code = str(body.get("code", "")); action = str(body.get("action", ""))
        svc = str(body.get("service", "claude"))
        if not code:
            return web.json_response({"ok": False})
        if svc == "chatgpt":
            _codes_tbl, _pend_tbl = "gpt_codes", "gpt_pending_activations"
            _release = ("UPDATE gpt_codes SET is_used=FALSE, used_by=NULL, used_at=NULL, "
                        "order_id=NULL, reserved_at=NULL WHERE code=$1")
        else:
            _codes_tbl, _pend_tbl = "claude_codes", "claude_pending_activations"
            _release = ("UPDATE claude_codes SET is_used=FALSE, used_by=NULL, used_at=NULL, "
                        "order_id=NULL, org_id=NULL WHERE code=$1")
        pool = await get_pool()
        async with pool.acquire() as conn:
            # снимаем pending-привязку в любом случае (клиент потеряет старую сессию)
            await conn.execute(f"DELETE FROM {_pend_tbl} WHERE code=$1", code)
            if action == "release":
                await conn.execute(_release, code)
            elif action == "delete":
                await conn.execute(f"DELETE FROM {_codes_tbl} WHERE code=$1", code)
            else:
                return web.json_response({"ok": False, "msg": "Неизвестное действие"})
        return web.json_response({"ok": True})
    except Exception as _e:
        logging.error(f"api_admin_claude_code_action: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_service_add_handler(request: web.Request) -> web.Response:
    """Добавить новый сервис в магазин (с первым тарифом). Admin-only."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        import re as _re_s, hashlib as _h_s
        name = str(body.get("name", "")).strip()
        emoji = str(body.get("emoji", "")).strip()
        plan_name = str(body.get("planName", "")).strip() or "Pro"
        try:
            price = int(float(body.get("price") or 0))
        except Exception:
            price = 0
        if not name or price <= 0:
            return web.json_response({"ok": False, "msg": "Нужно название и цена"})
        slug = _re_s.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "svc"
        key = f"{slug}_{_h_s.md5(name.encode()).hexdigest()[:5]}"
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO bot_shop_items (key, plan_idx, service_name, emoji, service_desc, plan_name, price, stars, plan_desc, sort_order) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) ON CONFLICT (key, plan_idx) DO NOTHING",
                key, 0, name, emoji, "", plan_name, price, 0, "", 999)
        SHOP_CATALOG[key] = {"_key": key, "name": name, "emoji": emoji, "desc": "",
                             "plans": [{"name": plan_name, "price": price, "stars": 0, "desc": ""}]}
        return web.json_response({"ok": True, "key": key})
    except Exception as _e:
        logging.error(f"api_admin_service_add: {_e}")
        return web.json_response({"ok": False, "msg": "Ошибка"}, status=500)


async def api_admin_service_delete_handler(request: web.Request) -> web.Response:
    """Удалить сервис из магазина. Admin-only. Базовые сервисы защищены."""
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        key = str(body.get("key", ""))
        if not key:
            return web.json_response({"ok": False})
        if key in ("chatgpt", "claude", "perplexity", "appstore"):
            return web.json_response({"ok": False, "msg": "Базовый сервис нельзя удалить (активация по коду)"})
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM bot_shop_items WHERE key=$1", key)
        SHOP_CATALOG.pop(key, None)
        return web.json_response({"ok": True})
    except Exception as _e:
        logging.error(f"api_admin_service_delete: {_e}")
        return web.json_response({"ok": False, "msg": "Ошибка"}, status=500)


async def api_admin_settings_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        welcome = await get_setting("welcome_extra", "")
        maint = (await get_setting("maintenance", "0")) == "1"
        pool = await get_pool()
        async with pool.acquire() as conn:
            gpt = await conn.fetchval("SELECT COUNT(*) FROM gpt_codes WHERE is_used=FALSE") or 0
            cl = await conn.fetchval("SELECT COUNT(*) FROM claude_codes WHERE is_used=FALSE") or 0
            px = await conn.fetchval("SELECT COUNT(*) FROM perplexity_codes WHERE is_used=FALSE") or 0
        rate = await get_setting("nsgifts_usd_rate", "100")
        markup = await get_setting("nsgifts_markup", "15")
        thr = await get_setting("nsgifts_balance_threshold", "30")
        miniapps = [
            {"key": "chatgpt", "name": "ChatGPT", "enabled": bool(rt.chatgpt_webapp_enabled), "codes": int(gpt)},
            {"key": "claude", "name": "Claude", "enabled": bool(rt.claude_webapp_enabled), "codes": int(cl)},
            {"key": "perplexity", "name": "Perplexity", "enabled": bool(rt.perplexity_webapp_enabled), "codes": int(px)},
        ]
        return web.json_response({"ok": True, "welcome": welcome, "maintenance": maint,
                                  "miniapps": miniapps,
                                  "appstore": {"rate": rate, "markup": markup, "threshold": thr}})
    except Exception as _e:
        logging.error(f"api_admin_settings: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_setting_save_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        kind = str(body.get("kind", ""))
        if kind == "welcome":
            await set_setting("welcome_extra", str(body.get("text", "")))
        elif kind == "maintenance":
            await set_setting("maintenance", "1" if body.get("on") else "0")
        elif kind == "appstore":
            if body.get("rate") is not None:
                await set_setting("nsgifts_usd_rate", str(int(float(body["rate"]))))
            if body.get("markup") is not None:
                await set_setting("nsgifts_markup", str(int(float(body["markup"]))))
            if body.get("threshold") is not None:
                await set_setting("nsgifts_balance_threshold", str(int(float(body["threshold"]))))
        else:
            return web.json_response({"ok": False})
        return web.json_response({"ok": True})
    except Exception as _e:
        logging.error(f"api_admin_setting_save: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_miniapp_toggle_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        key = str(body.get("key", "")); en = bool(body.get("enabled"))
        if key == "chatgpt":
            rt.chatgpt_webapp_enabled = en
        elif key == "claude":
            rt.claude_webapp_enabled = en
        elif key == "perplexity":
            rt.perplexity_webapp_enabled = en
        else:
            return web.json_response({"ok": False})
        # Персистим, чтобы тумблер пережил деплой/рестарт
        try:
            await set_setting(f"miniapp_enabled:{key}", "1" if en else "0")
        except Exception:
            pass
        return web.json_response({"ok": True, "enabled": en})
    except Exception as _e:
        logging.error(f"api_admin_miniapp_toggle: {_e}")
        return web.json_response({"ok": False}, status=500)


async def load_miniapp_toggles():
    """Загружает сохранённые тумблеры mini-app из settings (переживают деплой)."""
    try:
        for _k, _attr in (("chatgpt", "chatgpt_webapp_enabled"),
                          ("claude", "claude_webapp_enabled"),
                          ("perplexity", "perplexity_webapp_enabled")):
            _v = await get_setting(f"miniapp_enabled:{_k}", None)
            if _v is not None:
                setattr(rt, _attr, _v == "1")
    except Exception as _e:
        logging.warning(f"load_miniapp_toggles: {_e}")


async def api_admin_add_codes_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        import re as _re_c
        service = str(body.get("service", ""))
        plan = str(body.get("plan", "") or ("plus" if service == "chatgpt" else "pro"))
        raw = str(body.get("codes", ""))
        # Разбираем ввод максимально терпимо:
        #  • разделители — перенос строки, пробел, запятая, точка с запятой, таб;
        #  • код может быть прислан ССЫЛКОЙ вида https://site/?code=XXXX — достаём code;
        #  • код может начинаться с цифры и содержать _ (старое правило требовало букву
        #    в начале и роняло часть кодов «в никуда»);
        #  • дубликаты внутри одной вставки схлопываем.
        _tokens = [t for t in _re_c.split(r"[\s,;]+", raw.strip()) if t]
        lines = []
        for _t in _tokens:
            _m_url = _re_c.search(r"[?&]code=([A-Za-z0-9\-_]+)", _t)
            lines.append(_m_url.group(1) if _m_url else _t)
        rx = _re_c.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_]{5,}$")
        _seen_batch = set(); _uniq = []
        for _c in lines:
            if _c not in _seen_batch:
                _seen_batch.add(_c); _uniq.append(_c)
        _dupe_in_batch = len(lines) - len(_uniq)
        valid = [c for c in _uniq if rx.match(c)]
        if service not in ("chatgpt", "claude", "perplexity"):
            return web.json_response({"ok": False, "msg": "Неизвестный сервис"})
        if not valid:
            return web.json_response({"ok": False, "msg": "Нет валидных кодов"})
        # Claude и ChatGPT: коды тегируются сайтом-провайдером (у каждого свой пул).
        # Берём provider из запроса, иначе — текущий активный сайт из настроек.
        if service == "claude":
            _reg, _set, _def = CLAUDE_PROVIDERS, "claude", CLAUDE_DEFAULT_PROVIDER
        elif service == "chatgpt":
            _reg, _set, _def = GPT_PROVIDERS, "gpt", GPT_DEFAULT_PROVIDER
        else:
            _reg, _set, _def = None, None, None
        _provider = str(body.get("provider", "") or "").strip()
        if _reg is not None and _provider not in _reg:
            _provider = (await get_setting(f"{_set}_provider", _def) or _def)
            if _provider not in _reg:
                _provider = _def
        pool = await get_pool()
        added = 0
        dupes = 0            # уже были в базе (ON CONFLICT DO NOTHING) — раньше молча считались добавленными
        _dupe_list = []      # примеры дублей, чтобы админ мог сверить
        _bad_list = [c for c in _uniq if c not in valid][:10]
        async with pool.acquire() as conn:
            for c in valid:
                try:
                    if service == "chatgpt":
                        _res = await conn.execute("INSERT INTO gpt_codes (code, plan, provider) VALUES ($1,$2,$3) ON CONFLICT (code) DO NOTHING", c, plan, _provider)
                    elif service == "claude":
                        _res = await conn.execute("INSERT INTO claude_codes (code, plan, provider) VALUES ($1,$2,$3) ON CONFLICT (code) DO NOTHING", c, plan, _provider)
                    else:
                        _res = await conn.execute("INSERT INTO perplexity_codes (code, plan) VALUES ($1,$2) ON CONFLICT (code) DO NOTHING", c, plan)
                    # asyncpg возвращает 'INSERT 0 1' при вставке и 'INSERT 0 0' при конфликте
                    if isinstance(_res, str) and _res.strip().endswith(" 0"):
                        dupes += 1
                        if len(_dupe_list) < 10:
                            _dupe_list.append(c)
                    else:
                        added += 1
                except Exception as _e_ins:
                    dupes += 1
                    if len(_dupe_list) < 10:
                        _dupe_list.append(c)
                    logging.warning(f"add-codes insert {c}: {_e_ins}")
        _msg = {"ok": True, "added": added, "dupes": dupes,
                "skipped": len(_uniq) - len(valid), "dupInBatch": _dupe_in_batch,
                "dupeList": _dupe_list, "badList": _bad_list, "total": len(lines)}
        if service in ("claude", "chatgpt"):
            _msg["provider"] = _provider
        return web.json_response(_msg)
    except Exception as _e:
        logging.error(f"api_admin_add_codes: {_e}")
        return web.json_response({"ok": False}, status=500)


async def api_admin_broadcast_handler(request: web.Request) -> web.Response:
    try:
        try: body = await request.json()
        except Exception: body = {}
        if _admin_uid_from_body(body) != ADMIN_ID:
            return web.json_response({"ok": False}, status=403)
        text = str(body.get("text", "")).strip()
        if not text:
            return web.json_response({"ok": False, "msg": "Пустой текст"})
        pool = await get_pool()
        async with pool.acquire() as conn:
            users = await conn.fetch("SELECT user_id FROM users WHERE is_blocked=0")
        ids = [r["user_id"] for r in users]

        async def _bcast():
            ok = 0
            for u in ids:
                try:
                    await bot.send_message(u, text, parse_mode="HTML")
                    ok += 1
                except Exception:
                    pass
                await asyncio.sleep(0.05)
            try:
                await bot.send_message(ADMIN_ID, f"✅ Рассылка завершена: доставлено {ok}/{len(ids)}")
            except Exception:
                pass
        asyncio.create_task(_bcast())
        return web.json_response({"ok": True, "count": len(ids)})
    except Exception as _e:
        logging.error(f"api_admin_broadcast: {_e}")
        return web.json_response({"ok": False}, status=500)

async def _admin_fail_shot(text, screenshot=None):
    """Отправляет админу текст о сбое и, если есть, скриншот сайта активации отдельным фото."""
    try:
        await bot.send_message(ADMIN_ID, text, parse_mode="HTML")
    except Exception:
        try:
            await bot.send_message(ADMIN_ID, text)
        except Exception:
            pass
    if screenshot:
        try:
            from aiogram.types import BufferedInputFile
            await bot.send_photo(
                ADMIN_ID,
                BufferedInputFile(screenshot, filename="activation.png"),
                caption="📸 Экран сайта активации")
        except Exception as _se:
            logging.warning(f"send fail screenshot: {_se}")


async def _run_activation_job(
    job_id: str, code: str, access_token: str,
    user_id: int, order_id: str, plan_name: str,
    provider: str = "987ai", session_raw: str = "", force: bool = False
):
    """Фоновая задача: Playwright-активация. Не держит HTTP-соединение.
    provider: '987ai' (текущий, по access_token) | 'aipro' (6661231.xyz, по Session JSON).
    force=True — клиент подтвердил принудительную активацию поверх уже активной подписки."""
    async def _do_activate(_code):
        if provider == "aipro":
            from chatgpt_activation import activate_chatgpt_aipro
            return await activate_chatgpt_aipro(_code, session_raw or access_token, force=force)
        if provider == "kkqq":
            from chatgpt_activation import activate_chatgpt_kkqq
            return await activate_chatgpt_kkqq(_code, session_raw or access_token, force=force)
        return await activate_chatgpt(_code, access_token)
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
        result = await _do_activate(code)

        # Если код уже использован на сайте — перебираем следующие коды ЭТОГО ЖЕ сайта,
        # пока не найдём рабочий или коды не кончатся (собираем пропущенные для отчёта).
        _gpt_used_codes = []
        while not result.get("success") and result.get("code_already_used") and len(_gpt_used_codes) < 10:
            _bad_code = code
            _plan_key = plan_name_to_key(plan_name)
            _gpt_used_codes.append(_bad_code)
            logging.warning(f"Код {_bad_code} уже использован, беру следующий (plan={_plan_key}, site={provider})")

            # Помечаем плохой код как постоянно использованный (не возвращаем в пул)
            try:
                _pool2 = await get_pool()
                async with _pool2.acquire() as _conn2:
                    await _conn2.execute(
                        "UPDATE gpt_codes SET is_used=TRUE, used_by=$1, used_at=NOW() WHERE code=$2",
                        user_id, _bad_code
                    )
            except Exception as _e:
                logging.error(f"Не удалось пометить плохой код {_bad_code}: {_e}")

            new_code = await get_next_gpt_code(_plan_key, provider)
            if not new_code:
                _activation_jobs[job_id] = {
                    "status": "done", "success": False,
                    "error": "Коды временно закончились. Александр активирует вручную в течение часа 🙌"
                }
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"🚨 <b>Коды {_plan_key} закончились!</b>\n\n"
                        f"👤 <code>{user_id}</code> ({plan_name}) ждёт активации.\n"
                        f"♻️ Пропущены использованные ({len(_gpt_used_codes)}): "
                        f"{', '.join(_gpt_used_codes)}\n"
                        f"Добавь коды: /add_gpt_codes",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                return
            logging.info(f"Новый код для {user_id}: {new_code} (site={provider})")
            await save_pending_activation(user_id, new_code, order_id, _plan_key, plan_name, provider)
            code = new_code
            if len(_gpt_used_codes) == 1:
                try:
                    await bot.send_message(
                        user_id,
                        "🔄 Первый код занят — автоматически выдаю следующий, подожди немного...",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
            result = await _do_activate(code)

        # ── Авто-фолбэк при СБОЕ САЙТА: нет стока, таймаут, сайт лежит (Cloudflare 522),
        # браузер не поднялся и т.п. → уходим на другой сайт с его кодом.
        # НЕ фолбэсим ошибки КЛИЕНТА (протухшая сессия, «нужна проверка», подтверждение) —
        # там смена сайта не поможет, а «код использован» обрабатывается циклом выше.
        def _gpt_site_failure(_res):
            if _res.get("success"):
                return False
            if (_res.get("token_invalid") or _res.get("needs_check")
                    or _res.get("needs_force_confirm") or _res.get("code_already_used")):
                return False
            return True

        if _gpt_site_failure(result):
            _plan_key_oos = plan_name_to_key(plan_name)
            _fail_reason = (result.get("error") or "сбой сайта")[:140]
            _tried_sites = [provider]
            # запоминаем сообщение о переключении — в него допишем ИТОГ активации
            _switch_msg_id = None
            _switch_text = ""
            for _np in await _gpt_provider_order():
                if _np in _tried_sites:
                    continue
                _nc = await get_next_gpt_code(_plan_key_oos, _np)
                if not _nc:
                    continue
                try:
                    await release_gpt_code(code)   # прежний код валиден — вернём в пул его сайта
                except Exception:
                    pass
                _tried_sites.append(_np)
                provider = _np
                code = _nc
                await save_pending_activation(user_id, code, order_id, _plan_key_oos, plan_name, provider)
                # клиенту показываем непрерывную загрузку с пометкой «пробую ещё раз»
                _activation_jobs[job_id] = {"status": "pending", "retrying": True}
                _switch_text = (
                    f"🔀 <b>ChatGPT — авто-переключение сайта</b> ({plan_name})\n"
                    f"Прежний сайт не сработал: {_fail_reason}\n"
                    f"Ушли на <b>{gpt_provider_name(_np)}</b>.")
                try:
                    _sw_msg = await bot.send_message(ADMIN_ID, _switch_text, parse_mode="HTML")
                    _switch_msg_id = _sw_msg.message_id
                except Exception:
                    _switch_msg_id = None
                result = await _do_activate(code)
                if not _gpt_site_failure(result):
                    break
                _fail_reason = (result.get("error") or "сбой сайта")[:140]
            if _gpt_site_failure(result):
                # НОВОЕ сообщение о неудаче + скриншот последнего сайта
                await _admin_fail_shot(
                    f"🚨 <b>ChatGPT — не удалось НИ НА ОДНОМ сайте</b> ({plan_name})\n"
                    f"👤 <code>{user_id}</code>\n"
                    f"🔑 Код: <code>{code}</code>\n"
                    f"🧭 Пробовали: {', '.join(gpt_provider_name(_p) for _p in _tried_sites)}\n"
                    f"❗️ Последняя ошибка: {_fail_reason}\n"
                    f"Активируй вручную.",
                    result.get("screenshot"))

        if result.get("success"):
            _email = result.get("email") or _extract_email_from_token(access_token)
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
            else:
                # _gpt_act_msg не было (напр. force-повтор через новый веб-апп) — шлём НОВОЕ
                # сообщение, иначе клиент не получит подтверждение в чате (жаловался клиент).
                try:
                    import datetime as _dt_e2
                    _end2 = (_dt_e2.datetime.now(_BOT_TZ) + _dt_e2.timedelta(days=_subscription_days(plan_name))).strftime("%d.%m.%Y")
                    await bot.send_message(
                        user_id,
                        "🎉 <b>Подписка ChatGPT активирована!</b>\n\n"
                        f"📦 Тариф: <b>{plan_name}</b>\n"
                        f"📧 Аккаунт: <b>{_email or '—'}</b>\n"
                        f"🔑 Ключ: <code>{code}</code>\n"
                        f"📅 Действует до: <b>{_end2}</b>\n\n"
                        "Спасибо за покупку! 🙌",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="Мой профиль", callback_data="menu_profile")],
                            [_eib("Главное меню", "back_main")],
                        ]))
                except Exception as _e2:
                    logging.error(f"gpt success new msg: {_e2}")
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
                    f"🔑 Итоговый код: <code>{code}</code>\n"
                    f"📦 Тариф: <b>{plan_name}</b>\n"
                    f"⏱ Время: <b>{_used_at}</b>\n"
                    f"🆔 Order: <code>{order_id}</code>\n"
                    + await _fk_num_line(order_id)
                )
                # принудительное продление поверх уже активной подписки
                if result.get("forced"):
                    _pu = result.get("prev_until") or "—"
                    _caption += (
                        f"\n\n♻️ <b>Принудительное продление</b> (клиент подтвердил поверх активной подписки)\n"
                        f"📅 Прошлая подписка действовала до: <b>{_pu}</b>\n"
                        f"⏱ Последняя активация: <b>{_used_at}</b>"
                    )
                # Если было авто-переключение сайта — дописываем ИТОГ в то самое сообщение,
                # чтобы в одном месте было видно: куда ушли и чем закончилось.
                try:
                    if locals().get("_switch_msg_id"):
                        await bot.edit_message_text(
                            _switch_text
                            + "\n\n✅ <b>Активация успешна</b>\n"
                              f"👤 Клиент: <b>{_tg_name}</b> (<code>{user_id}</code>)\n"
                              f"📧 Почта: <b>{_email or '—'}</b>\n"
                              f"🔑 Ключ: <code>{code}</code>\n"
                              f"🌐 Сайт активации: <b>{gpt_provider_name(provider)}</b>\n"
                              f"⏱ {_used_at}",
                            chat_id=ADMIN_ID, message_id=_switch_msg_id, parse_mode="HTML")
                except Exception as _e_sw:
                    logging.warning(f"edit switch msg: {_e_sw}")
                _gu = _gpt_used_codes if "_gpt_used_codes" in dir() else []
                if _gu:
                    _uc = "\n".join(f"   • <code>{c}</code>" for c in _gu)
                    _caption += f"\n\n♻️ <b>Пропущены уже использованные коды</b> ({len(_gu)}):\n{_uc}"
                if _gu:
                    # были пропущены использованные коды → отдельное НОВОЕ сообщение об успехе
                    await bot.send_message(ADMIN_ID, _caption, parse_mode="HTML")
                else:
                    # Обновляем ТО ЖЕ сообщение заказа (создан → оплачен → активирован), без скрина
                    try:
                        _ord_ok = await fk_get_order(order_id)
                        _amid_ok = (_ord_ok or {}).get("admin_msg_id")
                    except Exception:
                        _amid_ok = None
                    if _amid_ok:
                        try:
                            await bot.edit_message_text(_caption, chat_id=ADMIN_ID, message_id=_amid_ok, parse_mode="HTML")
                        except Exception:
                            await bot.send_message(ADMIN_ID, _caption, parse_mode="HTML")
                    else:
                        await bot.send_message(ADMIN_ID, _caption, parse_mode="HTML")
            except Exception:
                pass
            _fail_clear("gpt", user_id)
            _activation_jobs[job_id] = {"status": "done", "success": True}
        else:
            error_text = result.get("error", "Ошибка активации")
            _plan_key = plan_name_to_key(plan_name)
            import urllib.parse as _uparse2
            from aiogram.types import WebAppInfo as _WebAppInfo

            # ── needs_force_confirm: у аккаунта уже есть активная подписка. Спрашиваем
            #    клиента и, если подтвердит, активируем принудительно (force=1). Код НЕ трогаем.
            if result.get("needs_force_confirm"):
                import urllib.parse as _uparse_f
                from aiogram.types import WebAppInfo as _WebAppInfoF
                _acc = result.get("already_account") or ""
                _until = result.get("already_until") or ""
                _force_url = (
                    f"{WEBAPP_BASE_URL}/webapp/chatgpt"
                    f"?plan={_uparse_f.quote(plan_name)}&code={_uparse_f.quote(code)}&force=1"
                )
                try:
                    await bot.send_message(
                        user_id,
                        "⚠️ <b>На аккаунте уже есть активная подписка ChatGPT Plus</b>"
                        + (f"\n📧 Аккаунт: <code>{_acc}</code>" if _acc else "")
                        + (f"\n📅 Действует до: <b>{_until}</b>" if _until else "")
                        + "\n\nМожно активировать <b>принудительно</b> — но это начнёт новый месяц, "
                          "и остаток текущей подписки может <b>не суммироваться</b> (сгореть).\n\n"
                          "Если согласен — нажми кнопку ниже и подтверди активацию заново.",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="⚡ Активировать принудительно",
                                                  web_app=_WebAppInfoF(url=_force_url))],
                            [InlineKeyboardButton(text="❓ Нужна помощь", callback_data="gpt_need_help")],
                        ])
                    )
                except Exception as _fe:
                    logging.error(f"needs_force_confirm msg: {_fe}")
                await _admin_fail_shot(
                    "⚠️ <b>ChatGPT — у аккаунта уже есть Plus</b> (клиенту предложено активировать принудительно)\n\n"
                    f"👤 <code>{user_id}</code> · {plan_name}\n"
                    f"🔑 <code>{code}</code>\n"
                    f"📧 {_acc or '—'}" + (f" · до {_until}" if _until else ""),
                    result.get("screenshot"))
                _activation_jobs[job_id] = {"status": "done", "success": False, "need_force": True,
                                            "account": _acc, "until": _until,
                                            "error": "На аккаунте уже есть активная подписка Plus."}
                return

            # ── Тип ошибки определяет что делать дальше ──────────────────────
            # needs_check: активация МОГЛА пройти (сайт был в процессе). НЕ блэймим токен,
            # НЕ даём авто-повтор (риск двойной активации), код не трогаем; клиенту —
            # нейтральный экран, админу — скриншот на ручную проверку.
            if result.get("needs_check"):
                try:
                    await bot.send_message(
                        user_id,
                        "⏳ <b>Активация обрабатывается</b>\n\n"
                        "Сайт принял запрос — подписка может появиться в течение 5–10 минут. "
                        "Если не появится, напиши Александру, он проверит.",
                        parse_mode="HTML")
                except Exception:
                    pass
                await _admin_fail_shot(
                    "⚠️ <b>ChatGPT — нужна ручная проверка</b>\n\n"
                    f"👤 <code>{user_id}</code> · {plan_name}\n"
                    f"🔑 <code>{code}</code>\n"
                    f"{error_text}\n\n"
                    "Проверь на 6661231.xyz по email клиента ПЕРЕД повторной активацией (риск двойной).",
                    result.get("screenshot"))
                _activation_jobs[job_id] = {"status": "done", "success": False, "pending": True}
                return

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
                                text="❓ Нужна помощь", style="primary",
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
                            f"💡 Частая причина — устаревший токен/сессия: открой "
                            f"<b>chatgpt.com/api/auth/session</b>, скопируй ВЕСЬ текст заново и вставь. "
                            f"И проверь, что аккаунт на бесплатном плане.\n\n"
                            f"Попробуй ещё раз 👇",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="🔄 Повторить активацию",
                                    web_app=_WebAppInfo(url=_same_url)
                                )],
                                [InlineKeyboardButton(
                                    text="❓ Нужна помощь", style="primary",
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
                            f"💡 Чаще всего помогает: заново скопировать токен со страницы "
                            f"chatgpt.com/api/auth/session (он обновляется после каждого входа) "
                            f"и убедиться, что аккаунт на бесплатном плане.\n\n"
                            f"Если не выходит — напиши Александру, активирую вручную в течение 15–30 минут!",
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
                    # Уведомляем Александра С КОДОМ — чтобы активировал вручную (не чаще 1/15 мин на клиента)
                    if _fail_should_alert("gpt", user_id):
                        await _admin_fail_shot(
                            "🚨 <b>Авто-активация ChatGPT не удалась</b>\n\n"
                            f"👤 <code>{user_id}</code>\n"
                            f"🔑 Код: <code>{code}</code>\n"
                            f"📦 Тариф: <b>{plan_name}</b>\n"
                            f"🆔 Заказ: <code>{order_id}</code>\n"
                            + await _fk_num_line(order_id)
                            + f"⚠️ Ошибка: {error_text}\n\n"
                            "Код зарезервирован за клиентом — активируй вручную ИМ ЖЕ.",
                            result.get("screenshot")
                        )
                    _activation_jobs[job_id] = {
                        "status": "done", "success": False,
                        "error": f"Не удалось после {MAX_RETRIES} попыток. Напиши @{PERSONAL_USERNAME}"
                    }
                    return  # выходим, не перезаписываем job ниже

            _activation_jobs[job_id] = {"status": "done", "success": False, "error": error_text}
            if _fail_should_alert("gpt", user_id):
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
                    f"🔒 Код закреплён за клиентом — активируй вручную ИМ ЖЕ."
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
    finally:
        # снимаем метку активной активации клиента (защита от двойного запуска)
        try:
            if _gpt_job_active.get(user_id) == job_id:
                _gpt_job_active.pop(user_id, None)
        except Exception:
            pass


# Клиенты, уже предупреждённые о повторной активации (in-memory, сбрасывается при рестарте).
# Повторное нажатие «Попробовать снова» = принудительная активация (на другой аккаунт).
_gpt_double_warned: set = set()
_claude_double_warned: set = set()

# Антиспам алертов о НЕУДАЧНОЙ активации: не чаще 1 раза в 15 мин на (сервис, клиент).
# Успех сбрасывает троттл (следующий сбой снова уведомит сразу).
_fail_alert_at: dict = {}
def _fail_should_alert(service: str, user_id: int, window: int = 900) -> bool:
    import time as _t
    _k = (service, user_id)
    _now = _t.time()
    if _now - _fail_alert_at.get(_k, 0.0) >= window:
        _fail_alert_at[_k] = _now
        return True
    return False
def _fail_clear(service: str, user_id: int):
    _fail_alert_at.pop((service, user_id), None)

# message_id активационного сообщения клиента (чтобы заменить на поздравление после успеха)
_gpt_act_msg: dict = {}
_claude_act_msg: dict = {}
# GPT: активная задача активации по user_id — защита от двойного запуска
# (клиент дважды нажал «Активировать» → два job'а тянут по коду из пула).
_gpt_job_active: dict = {}
# Claude: активная цепочка активации по user_id (dedupe двойных кликов «Активировать»)
_claude_chain_active: dict = {}
# Claude: контекст «нужна проверка» по короткому токену (для кнопок админа: успех / другой сайт)
_claude_needcheck: dict = {}
# Claude: счётчик АВТОПОВТОРОВ при «нет стока» по заказу (сток у провайдера «мигает»,
# коды при этом остаются валидными — есть смысл подождать и попробовать снова).
_claude_oos_retry: dict = {}   # (оставлено на будущее; автоповторы при «нет стока» отключены)
# Claude: заказы, которым уже выдали авто-замену кода после жёсткого сбоя (кап = 1 раз/заказ)
_claude_replaced_orders: set = set()
_perplexity_double_warned: set = set()
_perplexity_act_msg: dict = {}
_perplexity_replaced_orders: set = set()
_perplexity_job_results: dict = {}
_PERPLEXITY_WEBAPP_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "perplexity_webapp.html")
_ADMIN_WEBAPP_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin_webapp.html")

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
    # Полный Session JSON — мини-апп уже шлёт его как raw_token (весь вставленный текст).
    session_raw  = (body.get("session") or body.get("raw_token") or "").strip()
    init_data    = (body.get("init_data") or "").strip()
    _fb_code     = (body.get("code") or "").strip()
    _force       = bool(body.get("force"))   # клиент подтвердил принудительную активацию (уже есть Plus)

    user_id = _verify_tg_init_data(init_data)
    if not user_id and _fb_code:
        # Фолбэк: initData не прошёл (открыто в браузере / старый клиент) —
        # опознаём клиента по коду активации (секрет, выдан только покупателю).
        _fb = await get_pending_activation_by_code(_fb_code)
        if _fb:
            user_id = _fb.get("user_id")
    if not user_id:
        return _resp({"success": False, "error": "Ошибка авторизации. Перезапусти мини-приложение."}, 403)
    if not access_token.startswith("eyJ") or len(access_token) < 100:
        return _resp({"success": False, "error": "Некорректный токен. Скопируй текст со страницы ещё раз."})

    pending = await get_pending_activation(user_id)
    if not pending:
        return _resp({"success": False, "error": f"Время сессии истекло. Напиши @{PERSONAL_USERNAME}"})

    # Guard: повторная активация за 29 дней — НЕ блокируем жёстко.
    # Первый раз предупреждаем, повторное нажатие «Попробовать снова» = активируем
    # принудительно (клиент может оформлять подписку на другой аккаунт, напр. другу).
    try:
        _pool_dbl = await get_pool()
        async with _pool_dbl.acquire() as _c_dbl:
            _recent_act = await _c_dbl.fetchrow(
                "SELECT code, plan, used_at, email FROM gpt_codes"
                " WHERE used_by=$1 AND used_at > NOW() - INTERVAL '29 days'"
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
                # \u041a\u043b\u0438\u0435\u043d\u0442\u0443 \u043f\u043e\u043a\u0430\u0437\u044b\u0432\u0430\u0435\u043c \u041e\u0411\u0415 \u043f\u043e\u0447\u0442\u044b: \u043a\u0443\u0434\u0430 \u0430\u043a\u0442\u0438\u0432\u0438\u0440\u043e\u0432\u0430\u043b\u0438 \u0440\u0430\u043d\u044c\u0448\u0435 \u0438 \u043a\u0443\u0434\u0430 \u0438\u0434\u0451\u0442 \u0441\u0435\u0439\u0447\u0430\u0441,
                # \u0438\u043d\u0430\u0447\u0435 \u043d\u0435\u043f\u043e\u043d\u044f\u0442\u043d\u043e \u2014 \u0442\u043e\u0442 \u0436\u0435 \u044d\u0442\u043e \u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u0438\u043b\u0438 \u0434\u0440\u0443\u0433\u043e\u0439.
                _prev_email = (_recent_act.get("email") or "").strip()
                _now_email = ""
                try:
                    _now_email = (_extract_email_from_token(access_token) or "").strip()
                except Exception:
                    _now_email = ""
                _same = bool(_prev_email and _now_email and
                             _prev_email.lower() == _now_email.lower())
                _lines_dbl = ["\u26a0\ufe0f \u041d\u0430 \u044d\u0442\u043e\u0442 \u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u0443\u0436\u0435 \u0430\u043a\u0442\u0438\u0432\u0438\u0440\u043e\u0432\u0430\u043b\u0438 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0443 ChatGPT.\n"]
                if _prev_email:
                    _lines_dbl.append(f"\ud83d\udce7 \u041f\u0440\u043e\u0448\u043b\u0430\u044f \u0430\u043a\u0442\u0438\u0432\u0430\u0446\u0438\u044f: {_prev_email} ({_us})")
                else:
                    _lines_dbl.append(f"\ud83d\udcc5 \u041f\u0440\u043e\u0448\u043b\u0430\u044f \u0430\u043a\u0442\u0438\u0432\u0430\u0446\u0438\u044f: {_us}")
                if _now_email:
                    _lines_dbl.append(f"\ud83d\udce7 \u0421\u0435\u0439\u0447\u0430\u0441 \u0430\u043a\u0442\u0438\u0432\u0438\u0440\u0443\u0435\u0448\u044c: {_now_email}")
                if _same:
                    _lines_dbl.append(
                        "\n\u041d\u0430 \u044d\u0442\u043e\u0442 \u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u0431\u044b\u043b\u0430 \u043e\u0444\u043e\u0440\u043c\u043b\u0435\u043d\u0430 \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0430 \u043c\u0435\u043d\u0435\u0435 \u043c\u0435\u0441\u044f\u0446\u0430 \u043d\u0430\u0437\u0430\u0434 "
                        "\u0438 \u043e\u043d\u0430 \u0435\u0449\u0451 \u0430\u043a\u0442\u0438\u0432\u043d\u0430. \u041c\u043e\u0436\u0435\u0442\u0435 \u043f\u0440\u043e\u0434\u043b\u0438\u0442\u044c \u043f\u043e\u0434\u043f\u0438\u0441\u043a\u0443 \u043d\u0430\u0447\u0438\u043d\u0430\u044f \u0441 \u0441\u0435\u0433\u043e\u0434\u043d\u044f\u0448\u043d\u0435\u0433\u043e \u0434\u043d\u044f "
                        "\u043f\u043e \u043a\u043d\u043e\u043f\u043a\u0435 \u00ab\u041f\u043e\u043f\u0440\u043e\u0431\u043e\u0432\u0430\u0442\u044c \u0441\u043d\u043e\u0432\u0430\u00bb.")
                elif _now_email:
                    _lines_dbl.append(
                        "\n\u042d\u0442\u043e \u0414\u0420\u0423\u0413\u041e\u0419 \u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u2014 \u0432\u0441\u0451 \u0432 \u043f\u043e\u0440\u044f\u0434\u043a\u0435. \u041d\u0430\u0436\u043c\u0438 \u00ab\u041f\u043e\u043f\u0440\u043e\u0431\u043e\u0432\u0430\u0442\u044c \u0441\u043d\u043e\u0432\u0430\u00bb, \u0438 \u0430\u043a\u0442\u0438\u0432\u0430\u0446\u0438\u044f \u043f\u0440\u043e\u0439\u0434\u0451\u0442.")
                else:
                    _lines_dbl.append(
                        "\n\u0415\u0441\u043b\u0438 \u043e\u0444\u043e\u0440\u043c\u043b\u044f\u0435\u0448\u044c \u043d\u0430 \u0414\u0420\u0423\u0413\u041e\u0419 \u0430\u043a\u043a\u0430\u0443\u043d\u0442 (\u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440, \u0434\u043b\u044f \u0434\u0440\u0443\u0433\u0430) \u2014 \u043d\u0430\u0436\u043c\u0438 \u00ab\u041f\u043e\u043f\u0440\u043e\u0431\u043e\u0432\u0430\u0442\u044c \u0441\u043d\u043e\u0432\u0430\u00bb.")
                _lines_dbl.append(f"\n\u0415\u0441\u043b\u0438 \u044d\u0442\u043e \u0441\u043b\u0443\u0447\u0430\u0439\u043d\u043e \u2014 \u043d\u0430\u043f\u0438\u0448\u0438 @{PERSONAL_USERNAME}.")
                return _resp({"success": False, "error": "\n".join(_lines_dbl)})
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
    provider  = pending.get("provider", "987ai")

    # Для сайта aipro нужен полный Session JSON. Если клиент прислал сырой session — берём его,
    # иначе (старый клиент прислал только токен) для aipro активация невозможна.
    if provider in ("aipro", "kkqq") and not session_raw:
        return _resp({"success": False, "error": "Обнови мини-приложение и вставь весь текст со страницы сессии заново."})

    # ЗАЩИТА ОТ ДВОЙНОЙ АКТИВАЦИИ: если у клиента уже крутится активация — возвращаем
    # ТОТ ЖЕ job вместо запуска второго. Иначе два параллельных запуска берут по коду
    # из пула, первый активирует Plus, а второй видит «на аккаунте уже есть подписка».
    _prev_job = _gpt_job_active.get(user_id)
    if _prev_job:
        _pj = _activation_jobs.get(_prev_job)
        if _pj and _pj.get("status") != "done":
            logging.info(f"GPT activation dedupe: user={user_id} уже выполняется job={_prev_job}")
            return _resp({"job_id": _prev_job, "status": "started"})

    job_id = str(uuid.uuid4())[:12]
    _activation_jobs[job_id] = {"status": "pending"}
    _gpt_job_active[user_id] = job_id
    asyncio.create_task(
        _run_activation_job(job_id, code, access_token, user_id, order_id, plan_name, provider, session_raw, _force)
    )
    logging.info(f"ChatGPT activation started: job={job_id} user={user_id} code={code} site={provider} force={_force}")
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



async def _fk_num_line(order_id: str) -> str:
    """Готовая HTML-строка с номером платежа FreeKassa для сообщений по заказу.
    Возвращает '🧾 FreeKassa: <code>NNN</code>\\n' либо '' (Stripe/вебхук без intid)."""
    try:
        _o = await fk_get_order(order_id)
        _n = (_o or {}).get("fk_intid") or ""
        return f"\U0001f9fe FreeKassa: <code>{_n}</code>\n" if _n else ""
    except Exception:
        return ""


async def _disable_client_pay_msg(order_id: str):
    """После успешной оплаты гасит кнопки в сообщении оплаты у КЛИЕНТА, чтобы он не
    оплачивал повторно тот же заказ. Сообщение меняем на «Оплата получена»."""
    try:
        _pool = await get_pool()
        async with _pool.acquire() as _c:
            _row = await _c.fetchrow(
                "SELECT user_id, client_msg_id FROM fk_orders WHERE order_id=$1", order_id)
        if not _row or not _row["client_msg_id"]:
            return
        try:
            await bot.edit_message_text(
                "✅ <b>Оплата получена.</b> Заказ обрабатывается.",
                chat_id=_row["user_id"], message_id=_row["client_msg_id"], parse_mode="HTML")
        except Exception:
            # текст мог не поменяться (тот же контент) — тогда просто убираем кнопки
            try:
                await bot.edit_message_reply_markup(
                    chat_id=_row["user_id"], message_id=_row["client_msg_id"], reply_markup=None)
            except Exception:
                pass
    except Exception as _e:
        logging.error(f"disable client pay msg {order_id}: {_e}")


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

    # 0. Валидация суммы для НЕ-webhook путей (webhook валидирует ДО вызова).
    # Кредитные заказы: пришло меньше ожидаемого = блок (анти-фрод, как в вебхуке).
    # shop_/nsg_ пропускаем: там своя (мягкая) логика и отдельные проверки.
    if source != "webhook" and not order_id.startswith("shop_") and not order_id.startswith("nsg_"):
        try:
            _fkchk = await fk_check_order_status(order_id)
            _recv = float((_fkchk or {}).get("amount") or 0)
            _exp = float(amount_rub or 0)
            if _recv and _exp and _recv < _exp - 1.0:
                logging.error(f"FK AMOUNT MISMATCH ({source}) order={order_id} exp={_exp} recv={_recv}")
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"\U0001f6a8 <b>Несовпадение суммы ({source})</b>\n"
                        f"Заказ <code>{order_id}</code>\n"
                        f"Ожидали <b>{_exp:.0f}₽</b>, пришло <b>{_recv:.0f}₽</b>\n"
                        f"Начисление ЗАБЛОКИРОВАНО. Разберись вручную.",
                        parse_mode="HTML")
                except Exception:
                    pass
                return False
        except Exception:
            pass

    # 1. Атомарно помечаем заказ как paid - если уже было paid, mark_paid вернёт False
    was_marked = await fk_mark_paid(order_id)
    if not was_marked:
        # Уже зачислено другим путём
        logging.info(f"FK order {order_id} already paid (source={source})")
        return False

    # Гасим кнопки оплаты в сообщении у клиента (первое подтверждение оплаты)
    await _disable_client_pay_msg(order_id)

    # 2. Зачисляем кредиты партией (на 30 дней) и логируем.
    # Если начисление упало ПОСЛЕ mark_paid — деньги приняты, услуга не выдана:
    # громко алертим админа (иначе сбой был бы «тихим» и невосстановимым).
    try:
        await add_credits_batch(user_id, credits, source="purchase", days_valid=30)
        await log_payment(user_id, credits, int(amount_rub), "freekassa")
    except Exception as _grant_err:
        logging.error(f"FK GRANT FAILED after mark_paid order={order_id}: {_grant_err}", exc_info=True)
        try:
            await bot.send_message(
                ADMIN_ID,
                f"\U0001f6a8 <b>СБОЙ ЗАЧИСЛЕНИЯ ПОСЛЕ ОПЛАТЫ</b>\n\n"
                f"Заказ помечен оплаченным, но кредиты/лог не зачислились.\n"
                f"\U0001f464 <code>{user_id}</code>\n\U0001f48e Кредитов: <b>{credits}</b>\n"
                f"\U0001f4b5 Сумма: <b>{amount_rub}₽</b>\n\U0001f194 <code>{order_id}</code>\n\n"
                f"Проверь и начисли вручную: <code>/credit {order_id}</code>",
                parse_mode="HTML")
        except Exception:
            pass
        return False
    try:
        await process_referral_bonus(user_id)
        await process_premium_referral(user_id, order_id, amount_rub)
    except Exception as _ref_err:
        logging.error(f"FK referral post-processing error order={order_id}: {_ref_err}")

    # Если был промокод - инкрементим используемость.
    # Промокод хранится в заказе БД (в payload вебхука FreeKassa его нет!).
    try:
        _dbo_promo = await fk_get_order(order_id)
    except Exception:
        _dbo_promo = None
    promo_code = ((_dbo_promo or {}).get("promo_code")) or (payment.get("promo_code") if isinstance(payment, dict) else None)
    promo_code = (promo_code or "").strip().upper() or None
    if promo_code and promo_code != "PROMO_APPLIED":
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
        pack_info = (db_order_for_msg or {}).get("pack", "") if db_order_for_msg else ""

        if is_shop_order:
            # Заказ из магазина - показываем информацию о товаре
            shop_key = pack_info.split(":")[1] if pack_info and ":" in pack_info else ""
            plan_idx = int(pack_info.split(":")[2]) if pack_info and pack_info.count(":") >= 2 else 0
            s = SHOP_CATALOG.get(shop_key, {})
            plans = s.get("plans", [])
            p = plans[plan_idx] if plan_idx < len(plans) else {}
            _svc_em = tg_emoji({**s, "_key": shop_key}) if s else ""
            service_name = f"{_svc_em} {s.get('name', '')} - {p.get('name', '')}".strip() if s else "Товар из магазина"

            # Автоматически создаём подписку на 1 месяц
            import datetime as _dt2
            try:
                expires_at = _dt2.datetime.now() + _dt2.timedelta(days=30)
                svc_display = f"{s.get('name', shop_key)}"
                plan_display = p.get("name", "")
                pool_sub = await get_pool()
                async with pool_sub.acquire() as conn_sub:
                    # Продление: гасим прежнюю активную подписку ТОГО ЖЕ ТАРИФА (реальное
                    # продление), но НЕ трогаем другие тарифы того же сервиса — клиент мог
                    # купить и Claude Pro, и Claude Max 5× (или для разных аккаунтов), они
                    # должны показываться отдельно. Раньше гасили по service_key → в профиле
                    # оставался только последний тариф сервиса.
                    await conn_sub.execute(
                        "UPDATE user_subscriptions SET is_active=FALSE "
                        "WHERE user_id=$1 AND service_key=$2 AND plan_name=$3 AND is_active=TRUE",
                        user_id, shop_key, plan_display)
                    await conn_sub.execute("""
                        INSERT INTO user_subscriptions
                        (user_id, service_key, service_name, plan_name, expires_at, created_by)
                        VALUES ($1,$2,$3,$4,$5,$6)
                        ON CONFLICT DO NOTHING
                    """, user_id, shop_key, svc_display, plan_display, expires_at, 0)
                logging.info(f"Подписка создана: user={user_id} svc={shop_key} до {expires_at.date()}")
            except Exception as sub_err:
                logging.error(f"Ошибка создания подписки: {sub_err}")

            _sk_low = (shop_key or "").lower()
            _sn_low = (s.get("name", "").lower() if isinstance(s, dict) else "")
            if ("chatgpt" in _sk_low) or ("chatgpt" in _sn_low) or _sk_low == "gpt":
                _svc_kind = "chatgpt"
            elif ("claude" in _sk_low) or ("claude" in _sn_low):
                _svc_kind = "claude"
            elif ("perplex" in _sk_low) or ("perplex" in _sn_low):
                _svc_kind = "perplexity"
            else:
                _svc_kind = None
            logging.info(f"[shop-pay] order={order_id} shop_key={shop_key!r} name={s.get('name','') if isinstance(s,dict) else ''!r} -> kind={_svc_kind}")

            if await _is_manual_plan(shop_key, plan_idx):
                await _send_manual_order(
                    user_id=user_id, shop_key=shop_key,
                    service_name=service_name, plan_name=p.get("name", ""),
                    order_id=order_id, amount_rub=amount_rub, delayed_note=delayed_note,
                )
            elif _svc_kind == "chatgpt":
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
                    await bot.send_message(
                        ADMIN_ID,
                        f"🛍 <b>Заказ ChatGPT (ручная активация)</b>\n"
                        f"👤 <code>{user_id}</code>  📦 {service_name}\n"
                        f"💵 {amount_rub}₽  🆔 <code>{order_id}</code>",
                        parse_mode="HTML"
                    )
                    return
                _plan_key  = plan_name_to_key(_plan_name)
                _code, _gpt_prov = await _gpt_pick_code(_plan_key)
                if _code is None:
                    await bot.send_message(
                        user_id,
                        f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                        f"📦 <b>{service_name}</b> — {amount_rub}₽\n\n"
                        f"⚠️ Коды временно закончились. Александр активирует вручную в течение часа 🙌"
                        f"{delayed_note}", parse_mode="HTML")
                    await bot.send_message(
                        ADMIN_ID,
                        f"🚨 <b>КОДЫ ChatGPT ЗАКОНЧИЛИСЬ НА ВСЕХ САЙТАХ!</b>\n"
                        f"Заказ <code>{order_id}</code> — активируй вручную!\n"
                        f"Пополни коды на любом сайте ChatGPT.", parse_mode="HTML")
                else:
                    await save_pending_activation(user_id, _code, order_id, _plan_key, _plan_name, _gpt_prov)
                    _webapp_url = f"{WEBAPP_BASE_URL}/webapp/chatgpt?plan={_uparse.quote(_plan_name)}&code={_uparse.quote(_code)}"
                    from aiogram.types import WebAppInfo
                    import datetime as _dt_gpt
                    _base_gpt = (
                        f"🎉 <b>Оплата прошла!</b>\n\n"
                        f"📦 <b>{service_name}</b> — {amount_rub}₽\n\n"
                        f"Осталось активировать подписку — нажми кнопку ниже 👇\n\n"
                        f"🎟 Код активации: <code>{_code}</code>{delayed_note}"
                    )
                    _kb_gpt_active = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✨ Активировать подписку", style="success",
                                              web_app=WebAppInfo(url=_webapp_url))],
                        [InlineKeyboardButton(text="❓ Нужна помощь", style="primary",
                                              callback_data="gpt_need_help")],
                    ])
                    # После окна кнопка активации ОСТАЁТСЯ — код живёт до 12 ч
                    _kb_gpt_expired = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✨ Активировать подписку", style="success",
                                              web_app=WebAppInfo(url=_webapp_url))],
                        [InlineKeyboardButton(text="❓ Нужна помощь", style="primary",
                                              callback_data="gpt_need_help")],
                    ])
                    _dl_gpt = _dt_gpt.datetime.now(_BOT_TZ) + _dt_gpt.timedelta(minutes=ACTIVATION_WINDOW_MIN)
                    _exp_gpt = (
                        f"📦 <b>{service_name}</b> — оплата сохранена ✅\n\n"
                        f"Если ещё не активировал — можно сделать это <b>сейчас</b>: нажми кнопку ниже.\n"
                        f"🎟 Код: <code>{_code}</code>\n\n"
                        f"Не получается — напиши Александру, активирует вручную 🙌"
                    )
                    # СНАЧАЛА инструкция…
                    try:
                        await bot.send_message(
                            user_id,
                            "📋 <b>Инструкция по активации ChatGPT</b>\n\n"
                            "1️⃣ Зайди на <b>chatgpt.com</b> и авторизуйся (в Chrome или Safari).\n"
                            "2️⃣ В том же браузере открой страницу с токеном:\n"
                            "<code>chatgpt.com/api/auth/session</code>\n"
                            "3️⃣ Скопируй <b>весь</b> текст страницы целиком.\n"
                            "4️⃣ Вернись в мини-приложение (кнопка «Активировать подписку»), "
                            "вставь токен — подписка активируется автоматически за 1–2 минуты.\n\n"
                            f"🎟 Код активации: <code>{_code}</code>\n"
                            "⚠️ Аккаунт должен быть на бесплатном плане.",
                            parse_mode="HTML")
                    except Exception:
                        pass
                    # …ПОТОМ сообщение с кнопкой активации (его id отслеживаем для таймера/успеха)
                    _m_act_gpt = await bot.send_message(
                        user_id, _base_gpt + _activation_timer_line(_dl_gpt),
                        parse_mode="HTML", reply_markup=_kb_gpt_active)
                    _gpt_act_msg[user_id] = _m_act_gpt.message_id
                    asyncio.create_task(_activation_timer_job(
                        user_id, _m_act_gpt.message_id, _base_gpt, _kb_gpt_active,
                        _dl_gpt, _exp_gpt, _kb_gpt_expired, _gpt_act_msg))
            elif _svc_kind == "claude":
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
                    _code_cl, _prov_cl = await _claude_pick_code(_plan_key_cl)
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
                            f"🚨 <b>КОДЫ Claude {_plan_name_cl} ЗАКОНЧИЛИСЬ НА ВСЕХ САЙТАХ!</b>\n"
                            f"Заказ <code>{order_id}</code> user=<code>{user_id}</code>\n"
                            f"Пополни коды на любом из сайтов Claude.",
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
                            provider=_prov_cl,
                        )
            elif _svc_kind == "perplexity":
                # ── Авто-активация Perplexity через bypriceactivate.pro Mini App ──
                _plan_name_cl = p.get("name", "Pro")
                _plan_key_cl = "pro"
                logging.info(f"[perplexity-pay] order={order_id} uid={user_id} enabled={rt.perplexity_webapp_enabled} plan_idx={plan_idx}")
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
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="❓ Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}")],
                            ])
                        )
                        await bot.send_message(
                            ADMIN_ID,
                            f"🚨 <b>КОДЫ Perplexity {_plan_name_cl} ЗАКОНЧИЛИСЬ!</b>\n"
                            f"Заказ <code>{order_id}</code> user=<code>{user_id}</code>\n"
                            f"Пополни коды на bypriceactivate.pro",
                            parse_mode="HTML"
                        )
                    else:
                        _ok_px = await _send_perplexity_webapp_to_user(
                            user_id=user_id,
                            code=_code_cl,
                            order_id=order_id,
                            plan=_plan_key_cl,
                            plan_name=_plan_name_cl,
                            delayed_note=delayed_note,
                        )
                        if not _ok_px:
                            try:
                                await release_perplexity_code(_code_cl)
                            except Exception:
                                pass
                            await bot.send_message(
                                user_id,
                                f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                                f"📦 <b>{service_name}</b> — {amount_rub}₽\n\n"
                                f"Александр активирует Perplexity вручную в течение часа 🙌{delayed_note}",
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="❓ Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}")],
                                ])
                            )
                            await bot.send_message(
                                ADMIN_ID,
                                f"🚨 <b>Perplexity: не удалось отправить кнопку активации</b>\n"
                                f"👤 <code>{user_id}</code>  🆔 <code>{order_id}</code>\n"
                                f"Код возвращён в пул. Проверь логи Railway.",
                                parse_mode="HTML"
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
                    f"🆔 Заказ: <code>{order_id}</code>\n"
                    + await _fk_num_line(order_id) + "\n"
                    + f"Александр свяжется с тобой и активирует подписку в течение часа 🙌\n"
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
                f"🆔 Заказ: <code>{order_id}</code>\n"
                + await _fk_num_line(order_id) + "\n"
                + f"<i>⏳ Кредиты действуют 30 дней с момента покупки</i>"
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
        # Номер платежа В FreeKassa (колонка «Номер» в ЛК) — по нему ищется платёж
        _fk_no = ((db_order_admin or {}).get("fk_intid") or "") if db_order_admin else ""
        _fk_line = f"\U0001f9fe FreeKassa: <code>{_fk_no}</code>\n" if _fk_no else ""

        if is_shop and pack_info:
            shop_key = pack_info.split(":")[1] if ":" in pack_info else ""
            plan_idx = int(pack_info.split(":")[2]) if pack_info.count(":") >= 2 else 0
            s_cat = SHOP_CATALOG.get(shop_key, {})
            plans = s_cat.get("plans", [])
            p_cat = plans[plan_idx] if plan_idx < len(plans) else {}
            _svc_em = tg_emoji({**s_cat, "_key": shop_key}) if s_cat else ""
            service_name = f"{_svc_em} {s_cat.get('name', '')} {p_cat.get('name', '')}".strip() if s_cat else "Товар из магазина"

            admin_msg = (
                f"\u2705 <b>Заказ оплачен!</b>\n\n"
                f"\U0001f464 {user_label} (<code>{user_id}</code>)\n"
                f"\U0001f4e6 {service_name}\n"
                f"\U0001f4b5 Сумма: <b>{amount_rub}\u20bd</b>\n"
                f"💳 \u0421\u043f\u043e\u0441\u043e\u0431: \u0421\u0411\u041f\n"
                f"\U0001f194 \u0417\u0430\u043a\u0430\u0437: <code>{order_id}</code>\n"
                f"{_fk_line}\n"
                f"\u2705 <b>\u0421\u0442\u0430\u0442\u0443\u0441: \u043e\u043f\u043b\u0430\u0447\u0435\u043d</b>"
            )
        else:
            admin_msg = (
                f"\U0001f4b0 <b>\u041e\u043f\u043b\u0430\u0442\u0430 \u043f\u043e\u043b\u0443\u0447\u0435\u043d\u0430!</b>\n\n"
                f"\U0001f464 {user_label} (<code>{user_id}</code>)\n"
                f"\U0001f4b5 \u0421\u0443\u043c\u043c\u0430: <b>{amount_rub}\u20bd</b>\n"
                f"\U0001f48e \u041a\u0440\u0435\u0434\u0438\u0442\u043e\u0432: <b>{credits}</b>\n"
                f"💳 \u0421\u043f\u043e\u0441\u043e\u0431: \u0421\u0411\u041f\n"
                f"\U0001f194 \u0417\u0430\u043a\u0430\u0437: <code>{order_id}</code>\n"
                f"{_fk_line}\n"
                f"\u2705 <b>\u0421\u0442\u0430\u0442\u0443\u0441: \u043e\u043f\u043b\u0430\u0447\u0435\u043d</b>"
            )
        if promo_code:
            admin_msg += f"\n\U0001f39f \u041f\u0440\u043e\u043c\u043e\u043a\u043e\u0434: <code>{promo_code}</code>"

        # Тип заказа (ручной/вход/ссылка) — добавляем футер и кнопки → единое сообщение
        _lp_admin_kb = None
        try:
            _lp = await get_linkpay_order(order_id)
        except Exception:
            _lp = None
        if _lp:
            _knd = _lp.get("kind")
            if _knd == "manual":
                admin_msg += "\n\n🧾 <b>Ручной заказ</b> — оформи и нажми «Подписка готова»."
                _lp_admin_kb = _lp_kb(order_id, full=True)
            elif _knd == "creds":
                admin_msg += "\n\n🔐 <b>Вход в аккаунт</b> — ждём данные аккаунта от клиента."
                _lp_admin_kb = _lp_kb(order_id, full=False)
            elif _knd == "linkpay":
                admin_msg += "\n\n🔗 <b>Оплата по ссылке</b> — ждём ссылку от клиента."
                _lp_admin_kb = _lp_kb(order_id, full=False)
            if admin_msg_id:
                try:
                    await set_linkpay_admin_msg(order_id, admin_msg_id)
                except Exception:
                    pass

        if admin_msg_id:
            # Редактируем существующее сообщение
            try:
                await bot.edit_message_text(
                    admin_msg, chat_id=ADMIN_ID,
                    message_id=admin_msg_id, parse_mode="HTML",
                    reply_markup=_lp_admin_kb,
                )
            except Exception:
                await bot.send_message(ADMIN_ID, admin_msg, parse_mode="HTML", reply_markup=_lp_admin_kb)
        else:
            await bot.send_message(ADMIN_ID, admin_msg, parse_mode="HTML", reply_markup=_lp_admin_kb)
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
        # Номер платежа В САМОЙ FreeKassa (в ЛК он в колонке «Номер», по нему идёт поиск).
        # FreeKassa шлёт его как intid (встречается и в разном регистре).
        fk_intid    = str(data.get("intid") or data.get("INTID")
                          or data.get("int_id") or "").strip()

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

        # Сохраняем номер FreeKassa в заказ — чтобы показывать его в сообщениях админу
        if fk_intid:
            try:
                _pool_fk = await get_pool()
                async with _pool_fk.acquire() as _c_fk:
                    await _c_fk.execute(
                        "UPDATE fk_orders SET fk_intid=$1 WHERE order_id=$2", fk_intid, order_id)
            except Exception as _e_fk:
                logging.warning(f"save fk_intid {order_id}: {_e_fk}")

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
    app.router.add_get("/webapp/admin", webapp_admin_handler)
    app.router.add_post("/api/admin/overview", api_admin_overview_handler)
    app.router.add_post("/api/admin/profit", api_admin_profit_handler)
    app.router.add_post("/api/admin/prices", api_admin_prices_handler)
    app.router.add_post("/api/admin/prices-save", api_admin_prices_save_handler)
    app.router.add_post("/api/admin/stats", api_admin_stats_handler)
    app.router.add_post("/api/admin/sales", api_admin_sales_handler)
    app.router.add_post("/api/admin/plan-add", api_admin_plan_add_handler)
    app.router.add_post("/api/admin/plan-delete", api_admin_plan_delete_handler)
    app.router.add_post("/api/admin/analytics", api_admin_analytics_handler)
    app.router.add_post("/api/admin/promos", api_admin_promos_handler)
    app.router.add_post("/api/admin/promo-create", api_admin_promo_create_handler)
    app.router.add_post("/api/admin/promo-deactivate", api_admin_promo_deactivate_handler)
    app.router.add_post("/api/admin/models", api_admin_models_handler)
    app.router.add_post("/api/admin/model-toggle", api_admin_model_toggle_handler)
    app.router.add_post("/api/admin/orders", api_admin_orders_handler)
    app.router.add_post("/api/admin/order-action", api_admin_order_action_handler)
    app.router.add_post("/api/admin/order-thread", api_admin_order_thread_handler)
    app.router.add_post("/api/admin/order-delete", api_admin_order_delete_handler)
    app.router.add_post("/api/admin/shop-orders", api_admin_shop_orders_handler)
    app.router.add_post("/api/admin/shop-order-action", api_admin_shop_order_action_handler)
    app.router.add_post("/api/admin/user-find", api_admin_user_find_handler)
    app.router.add_post("/api/admin/balance", api_admin_balance_handler)
    app.router.add_post("/api/admin/blocks", api_admin_blocks_handler)
    app.router.add_post("/api/admin/block", api_admin_block_handler)
    app.router.add_post("/api/admin/referral", api_admin_referral_handler)
    app.router.add_post("/api/admin/referral-set", api_admin_referral_set_handler)
    app.router.add_post("/api/admin/svc-list", api_admin_svc_list_handler)
    app.router.add_post("/api/admin/svc-save", api_admin_svc_save_handler)
    app.router.add_post("/api/admin/miniapp-detail", api_admin_miniapp_detail_handler)
    app.router.add_post("/api/admin/code-delete", api_admin_code_delete_handler)
    app.router.add_post("/api/admin/service-add", api_admin_service_add_handler)
    app.router.add_post("/api/admin/service-delete", api_admin_service_delete_handler)
    app.router.add_post("/api/admin/settings", api_admin_settings_handler)
    app.router.add_post("/api/admin/setting-save", api_admin_setting_save_handler)
    app.router.add_post("/api/admin/miniapp-toggle", api_admin_miniapp_toggle_handler)
    app.router.add_post("/api/admin/add-codes", api_admin_add_codes_handler)
    app.router.add_post("/api/admin/claude-provider", api_admin_claude_provider_handler)
    app.router.add_post("/api/admin/claude-code-action", api_admin_claude_code_action_handler)
    app.router.add_post("/api/admin/broadcast", api_admin_broadcast_handler)
    logging.info("Admin Mini App: /webapp/admin + /api/admin/overview")
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


# ─── Мультипровайдер Claude: выбор активного сайта + авто-фолбэк ──────────────
async def _claude_active_provider() -> str:
    p = await get_setting("claude_provider", CLAUDE_DEFAULT_PROVIDER) or CLAUDE_DEFAULT_PROVIDER
    return p if p in CLAUDE_PROVIDERS else CLAUDE_DEFAULT_PROVIDER


async def _claude_failover_on() -> bool:
    return (await get_setting("claude_failover", "1") or "1") == "1"


async def _claude_provider_order() -> list:
    """Активный провайдер первым; при включённом фолбэке — остальные следом."""
    active = await _claude_active_provider()
    order = [active]
    if await _claude_failover_on():
        for p in CLAUDE_PROVIDER_ORDER:
            if p in CLAUDE_PROVIDERS and p not in order:
                order.append(p)
    return order


async def _claude_pick_code(plan: str):
    """Берёт код из пула активного провайдера. Если пусто и включён фолбэк —
    пробует остальные сайты. Возвращает (code, provider) либо (None, None).
    При уходе на запасной сайт уведомляет админа."""
    order = await _claude_provider_order()
    active = order[0]
    for prov in order:
        code = await get_next_claude_code(plan, prov)
        if code:
            if prov != active:
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"🔀 <b>Claude — авто-переключение сайта</b>\n"
                        f"У <b>{claude_provider_name(active)}</b> закончились коды ({plan}).\n"
                        f"Активация ушла на <b>{claude_provider_name(prov)}</b>.\n"
                        f"Пополни коды на {claude_provider_name(active)}.",
                        parse_mode="HTML")
                except Exception:
                    pass
            return code, prov
    return None, None


# ─── Активация Claude через разные API сайтов (bpa | partner) ─────────────────
async def _claude_bpa_redeem(base: str, code: str, org_id: str) -> dict:
    """Первый сайт (bypriceactivate.pro): POST /api/activate {code, org_id}.
    Возвращает нормализованный dict: {ok, ref, err_kind, err_msg}."""
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as _s:
            async with _s.post(f"{base}/api/activate",
                               json={"code": code, "org_id": org_id},
                               headers={"Content-Type": "application/json"}) as _r:
                try: _d = await _r.json()
                except Exception: _d = {}
                if _r.status == 201:
                    _oid = _d.get("order_id")
                    if not _oid:
                        return {"ok": False, "err_kind": "other", "err_msg": "Сервис не вернул order_id."}
                    return {"ok": True, "ref": str(_oid)}
                if _r.status == 409:
                    _det = _d.get("detail", "")
                    _detl = (_det or "").lower()
                    # ЭТОТ код уже израсходован → берём СЛЕДУЮЩИЙ код того же сайта
                    # (сайт отдаёт разные формулировки: already claimed / already fulfilled / used)
                    if ("already claimed" in _detl or "already fulfilled" in _detl
                            or "fulfilled" in _detl or "already used" in _detl
                            or "already redeemed" in _detl or "code used" in _detl):
                        return {"ok": False, "err_kind": "already_claimed", "err_msg": _det}
                    if ("out of stock" in _detl or "no stock" in _detl
                            or "no available" in _detl or "insufficient" in _detl):
                        return {"ok": False, "err_kind": "out_of_stock", "err_msg": _det}
                    if _blob_has_plan(_det):
                        return {"ok": False, "err_kind": "has_plan", "err_msg": _det}
                    return {"ok": False, "err_kind": "other", "err_msg": _det or "Ошибка кода."}
                if _r.status == 404:
                    return {"ok": False, "err_kind": "not_found", "err_msg": "Код не найден."}
                if _r.status == 400:
                    # по докам: «org_id must be a valid UUID» / «code is required» / «JSON body required»
                    _det4 = (_d.get("detail", "") or "").lower()
                    if "org_id" in _det4 or "uuid" in _det4:
                        return {"ok": False, "err_kind": "bad_org", "err_msg": _d.get("detail", "")}
                    return {"ok": False, "err_kind": "other", "err_msg": _d.get("detail", "") or "HTTP 400"}
                return {"ok": False, "err_kind": "other", "err_msg": f"HTTP {_r.status}"}
    except aiohttp.ClientError as _e:
        return {"ok": False, "err_kind": "network", "err_msg": str(_e)}


# bypriceactivate.pro: регион зашит в ПРЕФИКС кода (AU-… = Австралия, TR-… = Турция).
# Продукт на сайте называется «{база}_{регион}», напр. claude_pro_australia / claude_pro_turkey.
_BPA_REGIONS = {"AU": "australia", "TR": "turkey", "EG": "egypt", "US": "usa",
                "IN": "india", "NG": "nigeria", "BR": "brazil", "ID": "indonesia"}


def _bpa_product_for_code(plan_key: str, code: str) -> str:
    """Определяет продукт bpa по тарифу и префиксу кода. '' — если не удалось."""
    _base = {"pro": "claude_pro", "max_5x": "claude_max_5x",
             "max_20x": "claude_max_20x"}.get((plan_key or "").lower())
    if not _base:
        return ""
    _pref = (code or "").split("-")[0].upper() if "-" in (code or "") else ""
    _reg = _BPA_REGIONS.get(_pref)
    return f"{_base}_{_reg}" if _reg else _base


async def _claude_bpa_stock_product(base: str, product: str) -> int:
    """Сток bypriceactivate.pro по КОНКРЕТНОМУ продукту (GET /api/stock/{product}).
    Возвращает available или -1, если неизвестно (тогда просто пробуем код)."""
    if not product:
        return -1
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as _s:
            async with _s.get(f"{base}/api/stock/{product}") as _r:
                if _r.status != 200:
                    return -1
                _d = await _r.json()
        return int(_d.get("available", -1))
    except Exception:
        return -1


def _blob_has_plan(s: str) -> bool:
    """Признаки того, что у аккаунта клиента УЖЕ есть активная подписка Claude
    (клиент может исправить сам — отменить текущую подписку)."""
    s = (s or "").lower()
    return any(k in s for k in [
        "already subscribed", "active subscription", "existing subscription",
        "already has a subscription", "already a member", "already have",
        "已订阅", "订阅中", "已是会员", "已有订阅", "当前已订阅",
    ])


async def _claude_partner_redeem(cfg: dict, code: str, org_id: str, order_id: str) -> dict:
    """Второй сайт (rootchatgptplus.com): partner-API.
    POST /api/partner/v1/redemptions  (Bearer + Idempotency-Key)
    body {card_code, organization_id, confirm_overwrite:true}."""
    import json as _json
    base = cfg.get("base", ""); key = cfg.get("key", ""); _pn = cfg.get("name", base)
    if not key:
        return {"ok": False, "err_kind": "other",
                "err_msg": f"Ключ сайта {_pn} не задан (переменная окружения с API-ключом)."}
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        # стабильный ключ идемпотентности по заказу — защита от дублей при ретраях
        "Idempotency-Key": f"GPT11-{order_id}",
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as _s:
            async with _s.post(f"{base}/api/partner/v1/redemptions",
                               json={"card_code": code, "organization_id": org_id,
                                     "confirm_overwrite": True},
                               headers=headers) as _r:
                _http = _r.status
                _txt = await _r.text()
                try: _d = _json.loads(_txt)
                except Exception: _d = {}
        if _d.get("code") == 0:
            _data = _d.get("data") or {}
            _ref = str(_data.get("order_no") or "")
            if not _ref:
                return {"ok": False, "err_kind": "other", "err_msg": "Сайт не вернул order_no."}
            return {"ok": True, "ref": _ref}
        # логируем сырой ответ сайта для диагностики
        logging.error(f"partner redeem {_pn} HTTP {_http}: {_txt[:600]}")
        # ошибка: маппим по ключевым словам в ответе
        _blob = _json.dumps(_d, ensure_ascii=False).lower() if _d else (_txt or "").lower()
        if "card_used" in _blob or "已使用" in _blob:
            return {"ok": False, "err_kind": "already_claimed", "err_msg": "card_used"}
        if "no_stock" in _blob or "库存" in _blob:
            return {"ok": False, "err_kind": "out_of_stock", "err_msg": "no_stock"}
        if "invalid_card" in _blob:
            return {"ok": False, "err_kind": "not_found", "err_msg": "invalid_card"}
        if "invalid_organization_id" in _blob:
            return {"ok": False, "err_kind": "bad_org", "err_msg": "invalid_organization_id"}
        if _blob_has_plan(_blob):
            return {"ok": False, "err_kind": "has_plan", "err_msg": "already_subscribed"}
        if "ip_not_allowed" in _blob:
            return {"ok": False, "err_kind": "other",
                    "err_msg": f"IP сервера (152.55.176.64) не в белом списке {_pn}."}
        if "invalid_api_key" in _blob:
            return {"ok": False, "err_kind": "other", "err_msg": f"Неверный API-ключ сайта {_pn}."}
        if "not found" in _blob or _http == 404:
            return {"ok": False, "err_kind": "other",
                    "err_msg": f"У {_pn} нет partner-API по этому адресу (HTTP {_http}). Нужен свой API/ключ."}
        _msg = str(_d.get("message") or _d.get("error") or f"Ошибка сайта {_pn} (HTTP {_http}).")
        return {"ok": False, "err_kind": "other", "err_msg": _msg}
    except aiohttp.ClientError as _e:
        return {"ok": False, "err_kind": "network", "err_msg": str(_e)}


def _ipiap_order_no(order_id: str) -> str:
    """orderNo для ipiap обязан быть ровно 32 символа — берём md5 от нашего order_id
    (детерминированно ⇒ идемпотентно при ретраях)."""
    import hashlib as _hl
    return _hl.md5(f"GPT11-{order_id}".encode("utf-8")).hexdigest()


def _ipiap_sign(body_str: str, secret: str) -> str:
    import hashlib as _hl
    return _hl.md5((body_str + secret).encode("utf-8")).hexdigest()


async def _http_ip_retry(method, url, *, headers=None, data=None, total=30, retries=5):
    """HTTP-запрос с повтором при IP-отказе (403) и сетевой ошибке.
    Railway HA даёт 3 общих egress-IP; часть запросов уходит с не-белого IP и упирается
    в whitelist поставщика (403). Новое соединение может уйти с белого IP — повторяем.
    Безопасно для идемпотентных вызовов: ipiap orderNo и vip666 idempotency_key
    детерминированы, поэтому сервер дедуплицирует повтор, а 403 приходит ДО обработки.
    Возвращает (status, text); status=0 при полном провале сети."""
    _st, _tx = 0, ""
    for _i in range(retries):
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=total)) as _s:
                async with _s.request(method, url, headers=headers, data=data) as _r:
                    _st = _r.status
                    _tx = await _r.text()
                    if _r.status != 403:
                        return _st, _tx
                    logging.warning(f"_http_ip_retry 403 (попытка {_i+1}) {url}")
        except aiohttp.ClientError as _e:
            _st, _tx = 0, str(_e)
        await asyncio.sleep(0.6)
    return _st, _tx


async def _claude_order_redeem(cfg: dict, code: str, org_id: str, order_id: str) -> dict:
    """Сайт ipiap.com: order-API с подписью MD5.
    POST /api/order/create  header sign=MD5(body+apiSecret),
    body {apiId, orderNo(32), serialNumber, organizationId}."""
    import json as _json
    base = cfg.get("base", ""); api_id = cfg.get("api_id", ""); secret = cfg.get("api_secret", "")
    _pn = cfg.get("name", base)
    if not api_id or not secret:
        return {"ok": False, "err_kind": "other",
                "err_msg": f"Не заданы apiId/apiSecret сайта {_pn} "
                           f"(переменные IPIAP_CLAUDE_API_ID / IPIAP_CLAUDE_API_SECRET)."}
    order_no = _ipiap_order_no(order_id)
    body = {"apiId": api_id, "orderNo": order_no, "serialNumber": code}
    if org_id:
        body["organizationId"] = org_id
    body_str = _json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    headers = {"sign": _ipiap_sign(body_str, secret), "Content-Type": "application/json"}
    try:
        _http, _txt = await _http_ip_retry("POST", f"{base}/api/order/create",
                                           headers=headers, data=body_str.encode("utf-8"))
        if _http == 0:
            return {"ok": False, "err_kind": "network", "err_msg": _txt}
        try: _d = _json.loads(_txt)
        except Exception: _d = {}
        if _d.get("code") == 0:
            _ref = str((_d.get("data") or {}).get("orderNo") or order_no)
            return {"ok": True, "ref": _ref}
        logging.error(f"order redeem {_pn} HTTP {_http}: {_txt[:600]}")
        _blob = (_txt or "").lower()
        _msg = str(_d.get("msg") or _d.get("message") or f"Ошибка сайта {_pn} (HTTP {_http}).")
        # код не принадлежит этому продавцу (коды из другого источника) — НЕ про подпись/IP
        if ("不是本商户" in _blob or "本商户" in _blob or "not the merchant" in _blob
                or "not your" in _blob or "wrong merchant" in _blob):
            return {"ok": False, "err_kind": "other",
                    "err_msg": f"{_pn}: код не принадлежит этому продавцу (коды из другого источника). {_msg}"}
        if _d.get("code") == 500 or "sign" in _blob:
            return {"ok": False, "err_kind": "other",
                    "err_msg": f"Ошибка подписи/секрета {_pn} (проверь apiSecret/whitelist IP). {_msg}"}
        if _blob_has_plan(_blob):
            return {"ok": False, "err_kind": "has_plan", "err_msg": _msg}
        if "已使用" in _blob or "already" in _blob or "used" in _blob:
            return {"ok": False, "err_kind": "already_claimed", "err_msg": _msg}
        if "库存" in _blob or "stock" in _blob:
            return {"ok": False, "err_kind": "out_of_stock", "err_msg": _msg}
        if "不存在" in _blob or "not found" in _blob or "invalid" in _blob:
            return {"ok": False, "err_kind": "not_found", "err_msg": _msg}
        return {"ok": False, "err_kind": "other", "err_msg": _msg}
    except aiohttp.ClientError as _e:
        return {"ok": False, "err_kind": "network", "err_msg": str(_e)}


async def _claude_order_query(cfg: dict, order_no: str) -> dict:
    """Опрос статуса заказа ipiap: POST /api/order/query.
    Возвращает {status: 'success'|'failed'|'pending', reason}."""
    import json as _json
    base = cfg.get("base", ""); api_id = cfg.get("api_id", ""); secret = cfg.get("api_secret", "")
    body = {"apiId": api_id, "orderNo": order_no}
    body_str = _json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    headers = {"sign": _ipiap_sign(body_str, secret), "Content-Type": "application/json"}
    try:
        _hs, _txt = await _http_ip_retry("POST", f"{base}/api/order/query",
                                         headers=headers, data=body_str.encode("utf-8"), total=20)
        try: _d = _json.loads(_txt)
        except Exception: _d = {}
        _data = _d.get("data") or {}
        _st = _data.get("orderStatus")
        if _st == 2:
            return {"status": "success", "reason": ""}
        if _st == 3:
            return {"status": "failed", "reason": str(_data.get("reason") or "")}
        return {"status": "pending", "reason": ""}
    except Exception as _e:
        return {"status": "pending", "reason": str(_e)}


def _agent_idem(order_id, code) -> str:
    """Идемпотентный ключ для agent-API (vip666): один и тот же для повторов
    одной и той же попытки (order+code). Формат подходит под ^[A-Za-z0-9][...]{7,127}$."""
    import hashlib as _h
    _raw = f"{order_id}:{code}".encode("utf-8")
    return "redeem-" + _h.md5(_raw).hexdigest()   # 7 + 32 = 39 симв.


def _agent_base(cfg: dict) -> str:
    """Нормализует base agent-API: убирает хвостовой /api/agent/v1, если он уже
    включён в VIP666_AGENT_BASE (чтобы не задвоить префикс при сборке URL)."""
    b = (cfg.get("base", "") or "").rstrip("/")
    if b.endswith("/api/agent/v1"):
        b = b[:-len("/api/agent/v1")]
    return b


async def _claude_agent_redeem(cfg: dict, code: str, org_id: str, order_id: str) -> dict:
    """Сайт vip666ai.com: «代理 API v1» (Agent API).
    POST /api/agent/v1/cards/redeem, заголовок X-Agent-API-Key,
    body {card_code, redeem_type:'claude_org_id', target_value/target_confirm=orgId,
    idempotency_key}. Возвращает {ok, ref=idempotency_key} либо err_kind."""
    import json as _json
    base = _agent_base(cfg); key = cfg.get("key", "")
    _pn = cfg.get("name", base)
    if not key:
        return {"ok": False, "err_kind": "other",
                "err_msg": f"Не задан API-ключ сайта {_pn} (переменная VIP666_AGENT_KEY)."}
    if not org_id:
        return {"ok": False, "err_kind": "bad_org",
                "err_msg": f"{_pn}: не передан Organization ID."}
    _idem = _agent_idem(order_id, code)
    body = {"card_code": code, "redeem_type": "claude_org_id",
            "target_value": org_id, "target_confirm": org_id, "idempotency_key": _idem}
    body_str = _json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    headers = {"X-Agent-API-Key": key, "Content-Type": "application/json"}
    try:
        _http, _txt = await _http_ip_retry("POST", f"{base}/api/agent/v1/cards/redeem",
                                           headers=headers, data=body_str.encode("utf-8"))
        if _http == 0:
            return {"ok": False, "err_kind": "network", "err_msg": _txt}
        try: _d = _json.loads(_txt)
        except Exception: _d = {}
        _code = _d.get("code")
        if _code == 0:
            # ref = idempotency_key: по нему опрашиваем статус (GET /redeem/{idem})
            return {"ok": True, "ref": _idem}
        logging.error(f"agent redeem {_pn} HTTP {_http} code={_code}: {_txt[:600]}")
        _msg = str(_d.get("message") or f"Ошибка сайта {_pn} (HTTP {_http}).")
        # Бизнес-коды (HTTP 200): карта/остаток/подтверждение
        if _code == 100102:                     # уже использована → следующий код
            return {"ok": False, "err_kind": "already_claimed", "err_msg": _msg}
        if _code in (100101, 100103):           # невалидна / просрочена → следующий код
            return {"ok": False, "err_kind": "not_found", "err_msg": _msg}
        if _code == 100501 or _http in (500, 503):  # нет остатка / сервис недоступен → следующий сайт
            return {"ok": False, "err_kind": "out_of_stock", "err_msg": _msg}
        if _code == 100401:                     # нужно подтверждение перезаписи = уже есть подписка
            return {"ok": False, "err_kind": "has_plan", "err_msg": _msg}
        if _blob_has_plan((_txt or "").lower()):
            return {"ok": False, "err_kind": "has_plan", "err_msg": _msg}
        if _code == 40300 or _http == 403:      # ключ/scope/источник → это про КОНФИГ, не про код
            _b403 = (_txt or "").lower()
            if "来源" in _b403 or "域名" in _b403 or "origin" in _b403 or "source" in _b403:
                # ключ ограничен по домену-источнику (允许来源). Сервер-к-серверу мы без Origin —
                # админ сайта должен снять ограничение (允许来源 = 未限制) для этого ключа.
                return {"ok": False, "err_kind": "other",
                        "err_msg": f"{_pn}: ключ ограничен по домену-источнику (允许来源). "
                                   f"Попроси админа снять ограничение источника для ключа (未限制). {_msg}"}
            return {"ok": False, "err_kind": "other",
                    "err_msg": f"{_pn}: доступ запрещён (проверь VIP666_AGENT_KEY/scope). {_msg}"}
        if _code == 40900 or _http == 409:      # ещё обрабатывается — уходим в опрос статуса
            return {"ok": True, "ref": _idem}
        # 100301 充值失败 / 40000 / прочее → считаем сбоем сайта, пробуем следующий
        return {"ok": False, "err_kind": "other", "err_msg": _msg}
    except aiohttp.ClientError as _e:
        return {"ok": False, "err_kind": "network", "err_msg": str(_e)}


async def _claude_agent_query(cfg: dict, idem: str) -> dict:
    """Опрос статуса agent-API vip666: GET /api/agent/v1/redeem/{idempotency_key}.
    status: pending|processing|success|failed|review|unknown."""
    import json as _json
    base = _agent_base(cfg); key = cfg.get("key", "")
    headers = {"X-Agent-API-Key": key}
    try:
        _hs, _txt = await _http_ip_retry("GET", f"{base}/api/agent/v1/redeem/{idem}",
                                         headers=headers, total=20)
        try: _d = _json.loads(_txt)
        except Exception: _d = {}
        _st = str(((_d.get("data") or {}).get("status")) or "").lower()
        if _st == "success":
            return {"status": "success", "reason": ""}
        if _st in ("failed", "review"):
            return {"status": "failed", "reason": str((_d.get("data") or {}).get("message") or "")}
        return {"status": "pending", "reason": ""}
    except Exception as _e:
        return {"status": "pending", "reason": str(_e)}


async def _claude_redeem_via(provider: str, code: str, org_id: str, order_id: str) -> dict:
    """Единый вызов активации Claude под любой сайт (по типу api)."""
    cfg = CLAUDE_PROVIDERS.get(provider, {})
    if cfg.get("api") == "partner":
        return await _claude_partner_redeem(cfg, code, org_id, order_id)
    if cfg.get("api") == "agent":
        return await _claude_agent_redeem(cfg, code, org_id, order_id)
    if cfg.get("api") == "order":
        return await _claude_order_redeem(cfg, code, org_id, order_id)
    if cfg.get("api") == "browser":
        # Браузерные сайты (6661231.xyz) активируются отдельной фоновой задачей,
        # синхронный redeem для них не поддерживается (защита от блокировки HTTP).
        return {"ok": False, "err_kind": "other",
                "err_msg": f"{cfg.get('name', provider)} — браузерная активация, не через redeem."}
    return await _claude_bpa_redeem(cfg.get("base", ""), code, org_id)


# ─── Мультипровайдер ChatGPT: выбор активного сайта + авто-фолбэк ─────────────
async def _gpt_active_provider() -> str:
    p = await get_setting("gpt_provider", GPT_DEFAULT_PROVIDER) or GPT_DEFAULT_PROVIDER
    return p if p in GPT_PROVIDERS else GPT_DEFAULT_PROVIDER


async def _gpt_failover_on() -> bool:
    return (await get_setting("gpt_failover", "1") or "1") == "1"


async def _gpt_provider_order() -> list:
    active = await _gpt_active_provider()
    order = [active]
    if await _gpt_failover_on():
        for p in GPT_PROVIDER_ORDER:
            if p in GPT_PROVIDERS and p not in order:
                order.append(p)
    return order


async def _gpt_pick_code(plan: str):
    """Берёт CDK-код из пула активного сайта ChatGPT; при пустом пуле и включённом
    фолбэке пробует остальные. Возвращает (code, provider) либо (None, None)."""
    order = await _gpt_provider_order()
    active = order[0]
    for prov in order:
        code = await get_next_gpt_code(plan, prov)
        if code:
            if prov != active:
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"🔀 <b>ChatGPT — авто-переключение сайта</b>\n"
                        f"У <b>{gpt_provider_name(active)}</b> закончились коды ({plan}).\n"
                        f"Активация ушла на <b>{gpt_provider_name(prov)}</b>.\n"
                        f"Пополни коды на {gpt_provider_name(active)}.",
                        parse_mode="HTML")
                except Exception:
                    pass
            return code, prov
    return None, None


async def _send_claude_webapp_to_user(
    user_id: int, code: str, order_id: str,
    plan: str, plan_name: str, delayed_note: str = "", provider: str = "bpa"
) -> bool:
    """Сохраняет pending и отправляет клиенту кнопку WebApp."""
    import urllib.parse as _up
    from aiogram.types import WebAppInfo as _WAI

    await save_claude_pending_activation(user_id, code, order_id, plan, plan_name, provider)

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
            f"активируется автоматически (обычно за 1–2 минуты, иногда до 5) 👇\n\n"
            f"🎟 Код активации: <code>{code}</code>"
            f"{delayed_note}"
        )
        _kb_cl_active = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Активировать Claude", style="success", web_app=_WAI(url=webapp_url))],
            [InlineKeyboardButton(text="❓ Нужна помощь", style="primary", callback_data="claude_need_help")],
        ])
        # После окна кнопка активации ОСТАЁТСЯ — код живёт до 12 ч, клиент может активировать сам
        _kb_cl_expired = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Активировать Claude", style="success", web_app=_WAI(url=webapp_url))],
            [InlineKeyboardButton(text="❓ Нужна помощь", style="primary", callback_data="claude_need_help")],
        ])
        _dl_cl = _dt_cl.datetime.now(_BOT_TZ) + _dt_cl.timedelta(minutes=ACTIVATION_WINDOW_MIN)
        _exp_cl = (
            f"📦 <b>Claude {plan_name}</b> — оплата сохранена ✅\n\n"
            f"Если ещё не активировал — можно сделать это <b>сейчас</b>: нажми кнопку ниже, "
            f"введи Organization ID.\n"
            f"🎟 Код: <code>{code}</code>\n\n"
            f"Не получается — напиши Александру, активирует вручную 🙌"
        )
        # СНАЧАЛА инструкция…
        try:
            await bot.send_message(
                user_id,
                "📋 <b>Инструкция по активации Claude</b>\n\n"
                "1️⃣ Зайди на <b>claude.ai</b> и авторизуйся (в Chrome или Safari).\n"
                "2️⃣ Открой настройки аккаунта:\n"
                "<code>claude.ai/settings/account</code>\n"
                "3️⃣ Прокрути до «Organization ID» и скопируй UUID.\n"
                "4️⃣ Вернись в мини-приложение (кнопка «Активировать Claude»), "
                "вставь Organization ID — подписка активируется автоматически за 1–5 минут.\n\n"
                f"🎟 Код активации: <code>{code}</code>\n"
                "⚠️ Аккаунт Claude должен быть на Free-плане. "
                "Если есть платная подписка — сначала отмени её на claude.ai/settings/billing.",
                parse_mode="HTML")
        except Exception:
            pass
        # …ПОТОМ сообщение с кнопкой активации (его id отслеживаем для таймера/успеха)
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


async def _take_claude_bpa_screenshot(bpa_order_id: int, provider: str = "bpa") -> bytes | None:
    """Делает скриншот статуса активации на сайте-провайдере (только для bpa-типа)."""
    if CLAUDE_PROVIDERS.get(provider, {}).get("api", "bpa") != "bpa":
        return None  # у partner-API нет публичной страницы статуса
    try:
        from playwright.async_api import async_playwright
        _base = claude_provider_base(provider)
        async with async_playwright() as _p:
            _br = await _p.chromium.launch(
                headless=True,
                args=["--no-sandbox","--disable-dev-shm-usage","--disable-gpu","--single-process"]
            )
            _pg = await _br.new_page(viewport={"width": 900, "height": 500})
            await _pg.goto(
                f"{_base}/api/activate/{bpa_order_id}",
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
    order_id: str, plan_name: str, org_id: str, provider: str = "bpa", _tried=None
):
    """
    Опрашивает сайт-провайдер каждые 5 сек.
    При done — помечает код, уведомляет тебя и клиента.
    При failed — АВТО-переключает активацию на код другого сайта (если есть), иначе пишет обоим.
    _tried — множество уже пробованных сайтов (для авто-фолбэка без зацикливания).
    """
    _claude_job_results[bpa_order_id] = {"status": "queued"}
    _tried_set = set(_tried or ()) | {provider}
    _replaced_code = None  # авто-замена кода отключена; ветка elif оставлена мёртвой
    _cfg = CLAUDE_PROVIDERS.get(provider, {})
    _api = _cfg.get("api", "bpa")
    _base = _cfg.get("base", "")
    logging.info(f"Claude polling ref={bpa_order_id} user={user_id} code={code} via={provider} api={_api}")

    for attempt in range(120):          # макс 10 минут (120 × 5 сек)
        await asyncio.sleep(5)
        try:
            if _api == "partner":
                # partner-API: GET /api/partner/v1/redemptions/{order_no} (Bearer)
                _hdr = {"Authorization": f"Bearer {_cfg.get('key','')}"}
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as _s:
                    async with _s.get(
                        f"{_base}/api/partner/v1/redemptions/{bpa_order_id}", headers=_hdr
                    ) as _r:
                        if _r.status != 200:
                            continue
                        _d = await _r.json()
                _pst = str((_d.get("data") or {}).get("status") or "")
                # нормализуем статусы partner в словарь первого сайта
                if _pst == "succeeded":
                    status = "done"
                elif _pst in ("failed", "review"):
                    status = "failed"
                    _d = {"status": "failed", "error": _pst}
                else:
                    status = _pst   # pending / processing — просто ждём дальше
            elif _api == "order":
                # ipiap order-API: POST /api/order/query (подпись MD5), orderStatus 0/1/2/3
                _q = await _claude_order_query(_cfg, str(bpa_order_id))
                if _q["status"] == "success":
                    status = "done"
                elif _q["status"] == "failed":
                    status = "failed"
                    _d = {"status": "failed", "error": _q.get("reason") or "failed"}
                else:
                    status = "pending"   # 0/1 — ждём дальше
            elif _api == "agent":
                # vip666 agent-API: GET /api/agent/v1/redeem/{idempotency_key}
                _q = await _claude_agent_query(_cfg, str(bpa_order_id))
                if _q["status"] == "success":
                    status = "done"
                elif _q["status"] == "failed":
                    status = "failed"
                    _d = {"status": "failed", "error": _q.get("reason") or "failed"}
                else:
                    status = "pending"
            else:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=15)
                ) as _s:
                    async with _s.get(
                        f"{_base}/api/activate/{bpa_order_id}"
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
                    try:
                        _ord_ok = await fk_get_order(order_id)
                        _amid_ok = (_ord_ok or {}).get("admin_msg_id")
                    except Exception:
                        _amid_ok = None
                    if _amid_ok:
                        try:
                            await bot.edit_message_text(_caption_ok, chat_id=ADMIN_ID, message_id=_amid_ok, parse_mode="HTML")
                        except Exception:
                            await bot.send_message(ADMIN_ID, _caption_ok, parse_mode="HTML")
                    else:
                        await bot.send_message(ADMIN_ID, _caption_ok, parse_mode="HTML")
                except Exception:
                    pass
                _fail_clear("claude", user_id)
                await log_event(user_id, "claude_activation_ok",
                                f"code={code} bpa={bpa_order_id} plan={plan_name}")
                return

            # ── Ошибка ─────────────────────────────────────────
            elif status == "failed":
                _err = _d.get("error") or "Ошибка активации"
                _claude_job_results[bpa_order_id] = {
                    "status": "done", "success": False, "error": _err
                }
                # Сбрасываем bpa_order_id в pending, чтобы «Попробовать снова» создал НОВЫЙ BPA-заказ
                try:
                    _pool_rb = await get_pool()
                    async with _pool_rb.acquire() as _c_rb:
                        await _c_rb.execute(
                            "UPDATE claude_pending_activations SET bpa_order_id=NULL WHERE user_id=$1", user_id)
                except Exception:
                    pass

                # ── АВТО-ФОЛБЭК: активация провалилась → пробуем код ДРУГОГО сайта ──
                # (у каждого сайта свои коды; берём код ещё не пробованного сайта и повторяем там)
                try:
                    _pk_oos = plan_name_to_key(plan_name)
                    for _np in await _claude_provider_order():
                        if _np in _tried_set:
                            continue
                        _nc = await get_next_claude_code(_pk_oos, _np)
                        if not _nc:
                            continue
                        _res2 = await _claude_redeem_via(_np, _nc, org_id, order_id)
                        if not _res2.get("ok"):
                            continue   # код этого сайта не принят — пробуем следующий
                        _ref2 = _res2["ref"]
                        _is_bpa2 = CLAUDE_PROVIDERS.get(_np, {}).get("api", "bpa") == "bpa"
                        _pool_fo = await get_pool()
                        async with _pool_fo.acquire() as _c_fo:
                            if _is_bpa2:
                                await _c_fo.execute(
                                    "UPDATE claude_pending_activations SET code=$1, provider=$2, bpa_order_id=$3 WHERE user_id=$4",
                                    _nc, _np, int(_ref2), user_id)
                            else:
                                await _c_fo.execute(
                                    "UPDATE claude_pending_activations SET code=$1, provider=$2, bpa_order_id=NULL WHERE user_id=$3",
                                    _nc, _np, user_id)
                        try:
                            await bot.send_message(
                                ADMIN_ID,
                                f"🔀 <b>Claude — авто-переключение сайта после неудачи</b>\n"
                                f"👤 <code>{user_id}</code> ({plan_name})\n"
                                f"Прежний сайт не активировал — ушли на <b>{claude_provider_name(_np)}</b>.",
                                parse_mode="HTML")
                        except Exception:
                            pass
                        _claude_job_results[_ref2] = {"status": "queued"}
                        asyncio.create_task(_claude_activation_polling_job(
                            _ref2, _nc, user_id, order_id, plan_name, org_id, _np, _tried_set))
                        logging.info(f"Claude auto-failover after fail: user={user_id} -> {_np} ref={_ref2}")
                        return   # активация переехала на другой сайт — клиента не тревожим
                except Exception as _fo_e:
                    logging.error(f"Claude failover after fail: {_fo_e}")

                _is_stock = ("out of stock" in _err.lower() or "out-of-stock" in _err.lower())
                if _is_stock:
                    # провайдер временно без стока — код клиента ОСТАЁТСЯ валидным.
                    # НЕ возвращаем в пул и НЕ удаляем pending: клиент сам повторит, когда пополнят.
                    _fail_note = "Временно нет мест у провайдера — код сохранён за клиентом, можно повторить."
                else:
                    # Сбой обычно временный (провайдер: payment/timeout). Код НЕ сжигаем и НЕ ротируем —
                    # оставляем закреплённым за клиентом: можно повторить тем же кодом или активировать вручную.
                    _fail_note = "Код сохранён — повтори тем же кодом позже или активируй вручную."

                # ── алерт админу (не чаще 1 раза в 15 мин на клиента) ──
                if _fail_should_alert("claude", user_id):
                    try:
                        _caption_fail = (
                            f"❌ <b>Claude FAILED</b>\n"
                            f"👤 <code>{user_id}</code>  📦 {plan_name}\n"
                            f"🔑 <code>{code}</code>  🔢 BPA: <code>{bpa_order_id}</code>\n"
                            f"❌ {_err[:300]}"
                            + f"\n♻️ {_fail_note}"
                        )
                        _ss_fail = await _take_claude_bpa_screenshot(bpa_order_id, provider)
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
                    if _is_stock:
                        await bot.send_message(
                            user_id,
                            "⏳ <b>Временно нет мест у провайдера</b>\n\n"
                            "Твой код сохранён и остаётся действительным. Провайдер пополняет запас — "
                            "попробуй активировать снова через 5–10 минут 👇",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="🔁 Попробовать снова",
                                    callback_data="claude_reopen_webapp"
                                )],
                                [InlineKeyboardButton(
                                    text="❓ Нужна помощь", style="primary",
                                    callback_data="claude_need_help"
                                )],
                            ])
                        )
                    elif _replaced_code:
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
                                    text="❓ Нужна помощь", style="primary",
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
                                    text="❓ Нужна помощь", style="primary",
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
                    text="❓ Нужна помощь", style="primary",
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
    """GET /webapp/claude — отдаёт claude_webapp.html.
    БЕЗ КЭША: Telegram агрессивно кэширует WebApp, из-за чего после деплоя у клиента
    оставался старый JS (напр. не отрабатывал экран подтверждения активации)."""
    try:
        with open(_CLAUDE_WEBAPP_HTML_PATH, "r", encoding="utf-8") as _f:
            return web.Response(text=_f.read(), content_type="text/html", charset="utf-8",
                                headers=_NO_CACHE_HEADERS)
    except FileNotFoundError:
        return web.Response(text="Claude Mini App not found", status=404)


async def _run_claude_browser_job(ref, code, org_id, user_id, order_id, plan_name, plan_key, provider):
    """Фоновая активация Claude через браузерный сайт (6661231.xyz, Playwright).
    Обновляет _claude_job_results[ref]; клиент опрашивает /api/activate-claude-status/{ref}."""
    _claude_job_results[ref] = {"status": "queued"}
    try:
        from chatgpt_activation import activate_claude_aipro
        result = await activate_claude_aipro(code, org_id, plan_key)

        # код уже использован на сайте → следующий код этого же сайта, один ретрай
        if not result.get("success") and result.get("code_already_used"):
            try:
                _pool2 = await get_pool()
                async with _pool2.acquire() as _c2:
                    await _c2.execute(
                        "UPDATE claude_codes SET is_used=TRUE, used_by=$1, used_at=NOW() WHERE code=$2",
                        user_id, code)
            except Exception:
                pass
            _nc = await get_next_claude_code(plan_key, provider)
            if _nc:
                code = _nc
                await save_claude_pending_activation(user_id, code, order_id, plan_key, plan_name, provider)
                try:
                    await bot.send_message(user_id, "🔄 Первый код занят — выдаю следующий, подожди немного...", parse_mode="HTML")
                except Exception:
                    pass
                result = await activate_claude_aipro(code, org_id, plan_key)

        # ── Успех ──
        if result.get("success"):
            await mark_claude_code_used(code, user_id, order_id, org_id)
            await delete_claude_pending_activation(user_id)
            _claude_job_results[ref] = {"status": "done", "success": True}

            import datetime as _dt2
            _ts = _dt2.datetime.now(_BOT_TZ).strftime("%d.%m.%Y %H:%M")
            try:
                _pool2 = await get_pool()
                async with _pool2.acquire() as _c2:
                    _ur = await _c2.fetchrow("SELECT username, full_name FROM users WHERE user_id=$1", user_id)
                _un = (_ur["username"] if _ur else "") or ""
                _fn = (_ur["full_name"] if _ur else "") or ""
            except Exception:
                _un = _fn = ""
            _tg = (f"@{_un}" if _un else _fn) or f"id{user_id}"

            _end_cl = (_dt2.datetime.now(_BOT_TZ) + _dt2.timedelta(days=_subscription_days(plan_name))).strftime("%d.%m.%Y")
            _prof_kw = ({"icon_custom_emoji_id": UI_EMOJI_IDS["menu_profile"]} if UI_EMOJI_IDS.get("menu_profile") else {})
            _congrats = (
                "🎉 <b>Подписка Claude активирована!</b>\n\n"
                f"📦 Тариф: <b>{plan_name}</b>\n"
                f"🏢 Organization ID: <code>{org_id}</code>\n"
                f"🔑 Ключ: <code>{code}</code>\n"
                f"📅 Действует до: <b>{_end_cl}</b>\n\n"
                "Подписка появится в Claude в течение 5–10 минут. Спасибо за покупку! 🙌"
            )
            _kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Открыть Claude ↗", url="https://claude.ai")],
                [InlineKeyboardButton(text="Мой профиль", callback_data="menu_profile", **_prof_kw)],
                [_eib("Главное меню", "back_main")],
            ])
            _mid = _claude_act_msg.pop(user_id, None)
            _edited = False
            if _mid:
                try:
                    await bot.edit_message_text(_congrats, chat_id=user_id, message_id=_mid, parse_mode="HTML", reply_markup=_kb)
                    _edited = True
                except Exception:
                    pass
            if not _edited:
                try:
                    await bot.send_message(user_id, _congrats, parse_mode="HTML", reply_markup=_kb)
                except Exception:
                    pass

            try:
                _caption = (
                    f"✅ <b>Claude авто-активация OK</b> (6661231.xyz)\n\n"
                    f"👤 <b>{_tg}</b>  (<code>{user_id}</code>)\n"
                    f"🔑 Код: <code>{code}</code>\n"
                    f"📦 Тариф: <b>{plan_name}</b>\n"
                    f"🆔 Org ID: <code>{org_id}</code>\n"
                    f"⏱ {_ts}"
                )
                try:
                    _ord_ok = await fk_get_order(order_id)
                    _amid = (_ord_ok or {}).get("admin_msg_id")
                except Exception:
                    _amid = None
                if _amid:
                    try:
                        await bot.edit_message_text(_caption, chat_id=ADMIN_ID, message_id=_amid, parse_mode="HTML")
                    except Exception:
                        await bot.send_message(ADMIN_ID, _caption, parse_mode="HTML")
                else:
                    await bot.send_message(ADMIN_ID, _caption, parse_mode="HTML")
            except Exception:
                pass
            _fail_clear("claude", user_id)
            await log_event(user_id, "claude_activation_ok", f"code={code} browser={provider} plan={plan_name}")
            return

        # ── Неуспех ──
        _err = result.get("error") or "Не удалось активировать."
        _claude_job_results[ref] = {"status": "done", "success": False, "error": _err}
        try:
            await bot.send_message(
                ADMIN_ID,
                f"❌ <b>Claude (6661231.xyz) — активация не прошла</b>\n"
                f"👤 <code>{user_id}</code> · {plan_name}\n"
                f"🔑 <code>{code}</code>\n"
                f"🧩 <code>{org_id}</code>\n"
                f"⚠️ {(_err)[:300]}",
                parse_mode="HTML")
        except Exception:
            pass
    except Exception as _e:
        logging.error(f"claude browser job {ref}: {_e}", exc_info=True)
        _claude_job_results[ref] = {"status": "done", "success": False,
                                    "error": "Ошибка активации. Напиши Александру."}


async def _claude_wait_result(provider: str, ref) -> str:
    """Опрашивает статус активации на API-сайте: 'success' | 'failed' | 'timeout'.
    Без уведомлений и фолбэка — только результат ОДНОГО сайта (фолбэком рулит цепочка)."""
    cfg = CLAUDE_PROVIDERS.get(provider, {})
    api = cfg.get("api", "bpa")
    base = cfg.get("base", "")
    # bpa сам ретраит внутри заказа (status=running, пока идут повторы; failed — только когда
    # попытки исчерпаны). Ему даём больше времени — иначе обрываем активацию, которая бы прошла.
    _iters = 84 if api == "bpa" else 36      # bpa ≈7 мин, остальные ≈3 мин
    for _ in range(_iters):
        await asyncio.sleep(5)
        try:
            if api == "partner":
                _hdr = {"Authorization": f"Bearer {cfg.get('key','')}"}
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as _s:
                    async with _s.get(f"{base}/api/partner/v1/redemptions/{ref}", headers=_hdr) as _r:
                        if _r.status != 200:
                            continue
                        _d = await _r.json()
                _st = str((_d.get("data") or {}).get("status") or "")
                if _st == "succeeded":
                    return "success"
                if _st in ("failed", "review"):
                    return "failed"
            elif api == "order":
                _q = await _claude_order_query(cfg, str(ref))
                if _q["status"] == "success":
                    return "success"
                if _q["status"] == "failed":
                    return "failed"
            elif api == "agent":
                _q = await _claude_agent_query(cfg, str(ref))
                if _q["status"] == "success":
                    return "success"
                if _q["status"] == "failed":
                    return "failed"
            else:  # bpa
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as _s:
                    async with _s.get(f"{base}/api/activate/{ref}") as _r:
                        if _r.status != 200:
                            continue
                        _d = await _r.json()
                _st = _d.get("status")
                if _st == "done":
                    return "success"
                if _st == "failed":
                    return "failed"
        except Exception:
            continue
    return "timeout"


async def _claude_notify_success(ref, code, user_id, order_id, plan_name, org_id, site_name="", used_codes=None):
    """Помечает код, чистит pending, уведомляет клиента и админа (общий блок успеха).
    used_codes — список кодов, пропущенных как «уже использованные» (для отчёта админу)."""
    await mark_claude_code_used(code, user_id, order_id, org_id)
    await delete_claude_pending_activation(user_id)
    _claude_job_results[ref] = {"status": "done", "success": True}

    import datetime as _dt2
    _ts = _dt2.datetime.now(_BOT_TZ).strftime("%d.%m.%Y %H:%M")
    try:
        _pool2 = await get_pool()
        async with _pool2.acquire() as _c2:
            _ur = await _c2.fetchrow("SELECT username, full_name FROM users WHERE user_id=$1", user_id)
        _un = (_ur["username"] if _ur else "") or ""
        _fn = (_ur["full_name"] if _ur else "") or ""
    except Exception:
        _un = _fn = ""
    _tg = (f"@{_un}" if _un else _fn) or f"id{user_id}"

    _end_cl = (_dt2.datetime.now(_BOT_TZ) + _dt2.timedelta(days=_subscription_days(plan_name))).strftime("%d.%m.%Y")
    _prof_kw = ({"icon_custom_emoji_id": UI_EMOJI_IDS["menu_profile"]} if UI_EMOJI_IDS.get("menu_profile") else {})
    _congrats = (
        "🎉 <b>Подписка Claude активирована!</b>\n\n"
        f"📦 Тариф: <b>{plan_name}</b>\n"
        f"🏢 Organization ID: <code>{org_id}</code>\n"
        f"🔑 Ключ: <code>{code}</code>\n"
        f"📅 Действует до: <b>{_end_cl}</b>\n\n"
        "Подписка появится в Claude в течение 5–10 минут. Спасибо за покупку! 🙌"
    )
    _kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Открыть Claude ↗", url="https://claude.ai")],
        [InlineKeyboardButton(text="Мой профиль", callback_data="menu_profile", **_prof_kw)],
        [_eib("Главное меню", "back_main")],
    ])
    _mid = _claude_act_msg.pop(user_id, None)
    _edited = False
    if _mid:
        try:
            await bot.edit_message_text(_congrats, chat_id=user_id, message_id=_mid, parse_mode="HTML", reply_markup=_kb)
            _edited = True
        except Exception:
            pass
    if not _edited:
        try:
            await bot.send_message(user_id, _congrats, parse_mode="HTML", reply_markup=_kb)
        except Exception:
            pass

    try:
        _caption = (
            f"✅ <b>Claude авто-активация OK</b>" + (f" ({site_name})" if site_name else "") + "\n\n"
            f"👤 <b>{_tg}</b>  (<code>{user_id}</code>)\n"
            f"🔑 Итоговый код: <code>{code}</code>\n"
            f"📦 Тариф: <b>{plan_name}</b>\n"
            f"🆔 Org ID: <code>{org_id}</code>\n"
            f"🆔 Заказ: <code>{order_id}</code>\n"
            + await _fk_num_line(order_id)
            + f"⏱ {_ts}"
        )
        if used_codes:
            _uc = "\n".join(f"   • <code>{c}</code>" for c in used_codes)
            _caption += f"\n\n♻️ <b>Пропущены уже использованные коды</b> ({len(used_codes)}):\n{_uc}"
        if used_codes:
            # были пропущены использованные коды → отдельное НОВОЕ сообщение об успехе
            await bot.send_message(ADMIN_ID, _caption, parse_mode="HTML")
        else:
            try:
                _ord_ok = await fk_get_order(order_id)
                _amid = (_ord_ok or {}).get("admin_msg_id")
            except Exception:
                _amid = None
            if _amid:
                try:
                    await bot.edit_message_text(_caption, chat_id=ADMIN_ID, message_id=_amid, parse_mode="HTML")
                except Exception:
                    await bot.send_message(ADMIN_ID, _caption, parse_mode="HTML")
            else:
                await bot.send_message(ADMIN_ID, _caption, parse_mode="HTML")
    except Exception:
        pass
    _fail_clear("claude", user_id)
    await log_event(user_id, "claude_activation_ok", f"code={code} site={site_name} plan={plan_name}")


async def _run_claude_activation_chain(ref, user_id, order_id, org_id, plan_name, plan_key,
                                       skip_providers=None, force=False):
    """Единая цепочка активации Claude по всем сайтам с непрерывной загрузкой у клиента.
    Порядок сайтов — по числу свободных кодов ЭТОГО тарифа (убыв.); 6661231.xyz наравне.
    Переключаемся ТОЛЬКО при сбое сайта / отсутствии стока; ошибка клиента (bad org) — стоп.
    При переходе на следующий сайт ставим retrying=True → клиент видит «пробую повторную активацию».
    skip_providers — сайты, которые пропустить (напр. после ручного «не активировалась — другой сайт»)."""
    _skip = set(skip_providers or ())
    _claude_job_results[ref] = {"status": "queued"}
    _report = []       # диагностика: что пробовали и почему упало
    _used_codes = []   # коды, пропущенные как «уже использованные» (для отчёта админу)
    _last_shot = None  # последний скриншот сайта активации (для отправки админу при сбое)
    # Коды, под которые не оказалось стока: они ЦЕЛЫ и должны вернуться в пул, но НЕ сразу —
    # иначе get_next_claude_code (берёт первый свободный по id) отдаст тот же код по кругу.
    # Держим их зарезервированными до конца прогона и освобождаем в finally.
    _oos_release = []
    _bpa_stock_cache = {}   # {product: available} — чтобы не дёргать /api/stock на каждый код
    _oos_total = 0          # сколько раз получили «нет стока» (для решения об автоповторе)
    try:
        # предварительно зарезервированный при покупке код вернём в пул — выбор честный по стоку
        try:
            _pend0 = await get_claude_pending_activation(user_id)
            if _pend0 and _pend0.get("code"):
                await release_claude_code(_pend0["code"])
        except Exception:
            pass

        # порядок сайтов по числу свободных кодов этого тарифа
        try:
            _counts = await count_claude_free_by_provider_plan(plan_key)
        except Exception:
            _counts = {}
        # сайты, поставленные админом на паузу в админке (claude_disabled)
        try:
            _dis_raw = await get_setting("claude_disabled", "") or ""
            _disabled = {p for p in _dis_raw.split(",") if p}
        except Exception:
            _disabled = set()
        _order = [p for p in CLAUDE_PROVIDER_ORDER
                  if p in CLAUDE_PROVIDERS and _counts.get(p, 0) > 0
                  and p not in _skip and p not in _disabled]
        _order.sort(key=lambda p: _counts.get(p, 0), reverse=True)
        logging.info(f"Claude chain {ref}: order={_order} counts={_counts} "
                     f"disabled={sorted(_disabled)} skip={sorted(_skip)} plan_key={plan_key!r}")

        _counts_txt = ", ".join(
            f"{CLAUDE_PROVIDERS.get(p,{}).get('name',p)}={_counts.get(p,0)}"
            for p in CLAUDE_PROVIDER_ORDER if p in CLAUDE_PROVIDERS) or "—"

        if not _order:
            _claude_job_results[ref] = {"status": "done", "success": False,
                "error": "Временно нет кодов ни на одном сайте. Александр активирует вручную."}
            try:
                await bot.send_message(ADMIN_ID,
                    f"🚨 <b>Claude — нет свободных кодов В ПУЛЕ бота</b> ({plan_name})\n"
                    f"👤 <code>{user_id}</code>\n"
                    f"📦 Свободно по сайтам: {_counts_txt}\n"
                    f"<i>Похоже, коды на сайт не добавлены в пул бота (через админку). "
                    f"Сток на самом сайте бот не видит.</i>", parse_mode="HTML")
            except Exception:
                pass
            return

        _attempt = 0
        _MAX = 12   # предохранитель: не жечь весь пул и не держать клиента вечно
        _tried_codes = set()   # коды, уже опробованные в этом прогоне (чтобы не зациклиться)
        for _prov in _order:
            _cfg = CLAUDE_PROVIDERS.get(_prov, {})
            _api = _cfg.get("api", "bpa")
            _site = _cfg.get("name", _prov)
            _oos_count = 0      # сколько кодов подряд дали «нет стока» на этом сайте
            _OOS_MAX = 5        # коды bpa бывают на РАЗНЫЕ регионы (turkey/australia/egypt),
                                # поэтому даём несколько попыток: под один регион стока нет,
                                # под другой — есть. Больше 5 не пробуем, идём на следующий сайт.
            # ВАЖНО: предпроверку стока по /api/stock/{product} НЕ делаем — коды bpa
            # резолвятся в РЕГИОНАЛЬНЫЕ продукты (claude_pro_turkey и т.п.), а не в базовый
            # claude_pro. Проверка базового продукта врала (0 при 493 у turkey) и зря
            # пропускала сайт. Реальную доступность узнаём по ответу на конкретный код.
            _dead_prods = set()   # продукты-регионы этого сайта, где сток = 0
            _skips = 0            # дешёвые пропуски по стоку (не считаются попытками активации)
            # перебираем коды ЭТОГО сайта, пока не найдём рабочий (при «код использован/битый»)
            _got_code_here = False
            while _attempt < _MAX:
                _code = await get_next_claude_code(plan_key, _prov)
                if not _code:
                    if not _got_code_here:
                        _report.append(f"{_site}: свободных кодов в пуле не оказалось (план {plan_key})")
                        logging.warning(f"Claude chain {ref}: {_prov} get_next вернул None (plan={plan_key})")
                    break   # коды этого сайта кончились → следующий сайт
                if _code in _tried_codes:
                    _report.append(f"{_site}: коды закончились (повтор уже пробованного)")
                    break
                _tried_codes.add(_code)
                _got_code_here = True
                # ВНИМАНИЕ: предпроверку стока по /api/stock НЕ делаем. Проверено на практике:
                # эндпоинт отдаёт available=0 по claude_pro_australia, а активация ЭТИМ ЖЕ кодом
                # проходит успешно (сайт докупает по ходу и сам ретраит внутри заказа).
                # Единственная правда — ответ на конкретную попытку активации.
                _attempt += 1
                try:
                    await save_claude_pending_activation(user_id, _code, order_id, plan_key, plan_name, _prov)
                    _pool_u = await get_pool()
                    async with _pool_u.acquire() as _cu:
                        await _cu.execute("UPDATE claude_pending_activations SET org_id=$1 WHERE user_id=$2", org_id, user_id)
                except Exception:
                    pass
                # со второй попытки показываем клиенту «пробую повторную активацию»
                _claude_job_results[ref] = {"status": "processing", "retrying": _attempt > 1}
                logging.info(f"Claude chain ref={ref} attempt={_attempt} site={_prov} code={_code} api={_api}")

                if _api == "browser":
                    _bsite = _cfg.get("browser_site", "aipro")
                    if _bsite == "ipiap":
                        from chatgpt_activation import activate_claude_ipiap
                        _r = await activate_claude_ipiap(_code, org_id, plan_key)
                    elif _bsite == "vip666":
                        from chatgpt_activation import activate_claude_vip666
                        _r = await activate_claude_vip666(_code, org_id, plan_key)
                    elif _bsite == "bpa":
                        from chatgpt_activation import activate_claude_bpa
                        _r = await activate_claude_bpa(_code, org_id, plan_key, force=force)
                    elif _bsite == "ios891":
                        from chatgpt_activation import activate_claude_ios891
                        _r = await activate_claude_ios891(_code, org_id, plan_key)
                    else:
                        from chatgpt_activation import activate_claude_aipro
                        _r = await activate_claude_aipro(_code, org_id, plan_key)
                    if _r.get("success"):
                        await _claude_notify_success(ref, _code, user_id, order_id, plan_name, org_id, _site, _used_codes)
                        return
                    if _r.get("bad_org"):
                        try: await release_claude_code(_code)
                        except Exception: pass
                        _claude_job_results[ref] = {"status": "done", "success": False,
                            "error": ("❗ Organization ID не подошёл. Проверь: "
                                      "• это Organization ID (не User ID) со страницы claude.ai/settings/account, формат 8-4-4-4-12; "
                                      "• аккаунт должен быть на Free-плане — если есть платная подписка, сначала отмени её на claude.ai/settings/billing. "
                                      "Затем попробуй снова.")}
                        return
                    if _r.get("has_plan"):
                        try: await release_claude_code(_code)
                        except Exception: pass
                        _claude_job_results[ref] = {"status": "done", "success": False,
                            "error": ("У этого аккаунта уже есть активная подписка Claude. "
                                      "Отмени текущую подписку на claude.ai/settings/billing и попробуй снова.")}
                        return
                    if _r.get("needs_force_confirm"):
                        # Сайт просит подтвердить пополнение (на Org ID уже была активация).
                        # Спрашиваем КЛИЕНТА: кнопка ведёт в мини-приложение с force=1,
                        # там он подтверждает и активация продолжается без потери кода.
                        try: await release_claude_code(_code)
                        except Exception: pass
                        import urllib.parse as _uq_cf
                        from aiogram.types import WebAppInfo as _WAI_cf
                        _prev_txt = _r.get("already_until") or ""
                        _force_url = (f"{WEBAPP_BASE_URL}/webapp/claude"
                                      f"?plan={_uq_cf.quote(plan_name)}&force=1")
                        _claude_job_results[ref] = {
                            "status": "done", "success": False, "need_force": True,
                            "org": org_id, "prev": _prev_txt,
                            "error": "На этот аккаунт уже была активация — нужно подтверждение."}
                        try:
                            await bot.send_message(
                                user_id,
                                "⚠️ <b>Нужно твоё подтверждение</b>\n\n"
                                f"На этот аккаунт (Org ID <code>{org_id}</code>) уже была активация"
                                + (f" — <b>{_prev_txt}</b>" if _prev_txt else "") + ".\n\n"
                                "Если это <b>твой</b> аккаунт — подтверди, и подписка пополнится. "
                                "Если Org ID указан по ошибке — не подтверждай, подписка уйдёт чужому "
                                "аккаунту и вернуть её будет нельзя.\n\n"
                                "Нажми кнопку и подтверди активацию 👇",
                                parse_mode="HTML",
                                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                    [InlineKeyboardButton(text="✅ Это мой аккаунт — активировать",
                                                          web_app=_WAI_cf(url=_force_url))],
                                    [InlineKeyboardButton(text="❓ Нужна помощь",
                                                          callback_data="claude_need_help")],
                                ]))
                        except Exception as _e_cf:
                            logging.error(f"claude needs_force_confirm msg: {_e_cf}")
                        await _admin_fail_shot(
                            f"⚠️ <b>Claude {_site} — нужно подтверждение клиента</b>\n"
                            f"👤 <code>{user_id}</code> · {plan_name}\n"
                            f"🔑 <code>{_code}</code>\n🧩 Org: <code>{org_id}</code>\n"
                            + (f"📋 Прежняя активация: {_prev_txt}\n" if _prev_txt else "")
                            + "Клиенту отправлена кнопка подтверждения. Код возвращён в пул.",
                            _r.get("screenshot"))
                        return
                    if _r.get("needs_check"):
                        # активация вероятно прошла, но не подтверждена — НЕ фолбэсим авто (риск двойной),
                        # код НЕ возвращаем. Клиенту — нейтральный экран; АДМИНУ — кнопки решения.
                        _claude_job_results[ref] = {"status": "done", "success": False, "pending": True,
                            "error": "Активация обрабатывается. Подписка появится в течение 5–10 минут. Если не появится — напиши Александру."}
                        import uuid as _uuid_nc
                        _tok = _uuid_nc.uuid4().hex[:12]
                        _claude_needcheck[_tok] = {
                            "user_id": user_id, "order_id": order_id, "code": _code, "org_id": org_id,
                            "plan_name": plan_name, "plan_key": plan_key, "provider": _prov,
                            "site": _site, "ref": ref}
                        _cap = (f"⚠️ <b>Claude {_site} — нужна проверка</b>\n"
                                f"👤 <code>{user_id}</code> · {plan_name}\n"
                                f"🔑 <code>{_code}</code>\n🧩 Org: <code>{org_id}</code>\n\n"
                                f"Активация, вероятно, прошла, но бот не поймал подтверждение. "
                                f"Проверь код по Org ID на сайте (Card Query), затем выбери:")
                        _kb_nc = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="✅ Подписка активирована", callback_data=f"clnc_ok:{_tok}")],
                            [InlineKeyboardButton(text="🔄 Не активировалась — другой сайт", callback_data=f"clnc_next:{_tok}")],
                        ])
                        try:
                            _shot_nc = _r.get("screenshot")
                            if _shot_nc:
                                from aiogram.types import BufferedInputFile as _BIF_nc
                                await bot.send_photo(ADMIN_ID, _BIF_nc(_shot_nc, filename="claude_check.png"),
                                                     caption=_cap, parse_mode="HTML", reply_markup=_kb_nc)
                            else:
                                await bot.send_message(ADMIN_ID, _cap, parse_mode="HTML", reply_markup=_kb_nc)
                        except Exception:
                            try:
                                await bot.send_message(ADMIN_ID, _cap, parse_mode="HTML", reply_markup=_kb_nc)
                            except Exception:
                                pass
                        return
                    if _r.get("code_already_used"):
                        _last_shot = _r.get("screenshot") or _last_shot
                        _used_codes.append(_code)
                        _report.append(f"{_site}: код <code>{_code}</code> — уже использован, беру следующий")
                        logging.warning(f"Claude chain {ref}: browser {_prov} код использован — следующий код")
                        continue   # СЛЕДУЮЩИЙ код того же сайта (код НЕ возвращаем — он реально занят)
                    # прочий сбой браузера/сайта — код цел, вернём в пул, СМЕНА сайта
                    try: await release_claude_code(_code)
                    except Exception: pass
                    _last_shot = _r.get("screenshot") or _last_shot
                    _report.append(f"{_site}: {_r.get('error') or 'сбой браузера'}")
                    logging.warning(f"Claude chain {ref}: browser {_prov} fail: {_r.get('error')}")
                    break
                else:
                    _res = await _claude_redeem_via(_prov, _code, org_id, order_id)
                    if _res.get("ok"):
                        _wait = await _claude_wait_result(_prov, _res["ref"])
                        if _wait == "success":
                            await _claude_notify_success(ref, _code, user_id, order_id, plan_name, org_id, _site, _used_codes)
                            return
                        # сайт принял код, но не завершил — код израсходован; СМЕНА сайта
                        logging.warning(f"Claude chain {ref}: {_prov} activation {_wait} — фолбэк")
                        _report.append(f"{_site}: активация {_wait} (сайт принял код, но не завершил за 3 мин)")
                        break
                    _kind = _res.get("err_kind", "other")
                    if _kind == "bad_org":
                        try: await release_claude_code(_code)
                        except Exception: pass
                        _claude_job_results[ref] = {"status": "done", "success": False,
                            "error": ("❗ Organization ID не подошёл. Проверь: "
                                      "• это Organization ID (не User ID) со страницы claude.ai/settings/account, формат 8-4-4-4-12; "
                                      "• аккаунт должен быть на Free-плане — если есть платная подписка, сначала отмени её на claude.ai/settings/billing. "
                                      "Затем попробуй снова.")}
                        return
                    if _kind == "has_plan":
                        # ошибка клиента: у аккаунта уже есть подписка → не фолбэсим, код цел вернём
                        try: await release_claude_code(_code)
                        except Exception: pass
                        _claude_job_results[ref] = {"status": "done", "success": False,
                            "error": ("У этого аккаунта уже есть активная подписка Claude. "
                                      "Отмени текущую подписку на claude.ai/settings/billing и попробуй снова.")}
                        return
                    if _kind in ("already_claimed", "not_found"):
                        _used_codes.append(_code)
                        _report.append(f"{_site}: код <code>{_code}</code> — уже использован/битый ({_kind}), беру следующий")
                        logging.warning(f"Claude chain {ref}: {_prov} код битый ({_kind}) — следующий код")
                        continue   # СЛЕДУЮЩИЙ код того же сайта (код помечен использованным)
                    if _kind == "out_of_stock":
                        # Нет стока именно под ЭТОТ код: у сайта коды бывают на разные товары/регионы
                        # (напр. «out of stock for claude_pro_australia — your code stays valid»).
                        # Код ЦЕЛ → вернём его в пул В КОНЦЕ прогона (не сейчас: иначе он снова
                        # станет «первым свободным» и бот получит тот же код по кругу),
                        # а пока пробуем СЛЕДУЮЩИЙ код этого же сайта — но не больше _OOS_MAX раз.
                        _oos_release.append(_code)
                        _oos_count += 1
                        _oos_total += 1
                        logging.warning(f"Claude chain {ref}: {_prov} нет стока под код {_code} ({_oos_count}/{_OOS_MAX})")
                        _report.append(
                            f"{_site}: код <code>{_code}</code> — нет стока под него "
                            f"({_res.get('err_msg') or ''})"[:220])
                        if _oos_count >= _OOS_MAX:
                            _report.append(f"{_site}: стока нет и по другим кодам — перехожу на следующий сайт")
                            break   # сайт пуст → следующий сайт
                        # Гейт стока у сайта моментальный (сток «мигает»): даём паузу,
                        # чтобы следующая попытка попала в момент, когда место освободилось.
                        await asyncio.sleep(6)
                        continue   # СЛЕДУЮЩИЙ код того же сайта (код вернём в пул в конце)
                    # network / other (403 и т.п.) — код цел, вернём; СМЕНА сайта
                    try: await release_claude_code(_code)
                    except Exception: pass
                    logging.error(f"Claude chain {ref}: {_prov} redeem fail {_kind}: {_res.get('err_msg')}")
                    _report.append(f"{_site}: {_kind} — {_res.get('err_msg') or ''}"[:160])
                    break
            if _attempt >= _MAX:
                break

        # Автоповторов при «нет стока» НЕ делаем: если региона нет прямо сейчас, повтор через
        # несколько минут не поможет. Вместо этого цепочка уже перебирает ОСТАЛЬНЫЕ сайты
        # (кроме поставленных на паузу в админке) — см. цикл выше.

        # все сайты исчерпаны
        _claude_job_results[ref] = {"status": "done", "success": False,
            "error": ("Не удалось активировать автоматически. Частые причины: "
                      "у аккаунта уже есть платная подписка Claude (отмени её на claude.ai/settings/billing) "
                      "или аккаунт не на Free-плане. Проверь это и попробуй снова — "
                      "либо напиши Александру, активирует вручную.")}
        # каждый сайт — отдельным абзацем (пустая строка между), чтобы отчёт читался
        _rep_txt = "\n\n".join(f"• {r}" for r in _report) if _report else "• (ни одна попытка не выполнилась)"
        _counts_lines = "\n".join(
            f"   · {CLAUDE_PROVIDERS.get(p, {}).get('name', p)}: <b>{_counts.get(p, 0)}</b>"
            for p in CLAUDE_PROVIDER_ORDER if p in CLAUDE_PROVIDERS)
        # кнопки: повторить автоактивацию / отметить, что активировал вручную
        import uuid as _uuid_f
        _ftok = _uuid_f.uuid4().hex[:12]
        _claude_needcheck[_ftok] = {
            "user_id": user_id, "order_id": order_id, "code": "", "org_id": org_id,
            "plan_name": plan_name, "plan_key": plan_key, "provider": "",
            "site": "", "ref": ref}
        _kb_fail = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Повторить автоактивацию", callback_data=f"clfail_retry:{_ftok}")],
            [InlineKeyboardButton(text="✅ Активировал вручную", callback_data=f"clfail_manual:{_ftok}")],
        ])
        _plan_line = " → ".join(CLAUDE_PROVIDERS.get(p, {}).get("name", p) for p in _order) or "—"
        _dis_line = (", ".join(CLAUDE_PROVIDERS.get(p, {}).get("name", p) for p in sorted(_disabled))
                     if _disabled else "")
        _fail_txt = (
            f"❌ <b>Claude — активация не прошла НИ НА ОДНОМ сайте</b>\n"
            f"👤 <code>{user_id}</code> · {plan_name}\n"
            f"🧩 Org ID: <code>{org_id}</code>\n\n"
            f"📦 <b>Кодов в пуле бота</b> (не сток сайта):\n{_counts_lines}\n\n"
            f"🧭 <b>Планировали обойти:</b> {_plan_line}\n"
            + (f"⏸ <b>На паузе:</b> {_dis_line}\n" if _dis_line else "")
            + f"\n<b>Что пробовали:</b>\n{_rep_txt}\n\n"
            f"Проверь и активируй вручную либо нажми кнопку 👇")
        try:
            if _last_shot:
                from aiogram.types import BufferedInputFile as _BIF_f
                await bot.send_photo(ADMIN_ID, _BIF_f(_last_shot, filename="claude_fail.png"),
                                     caption=_fail_txt, parse_mode="HTML", reply_markup=_kb_fail)
            else:
                await bot.send_message(ADMIN_ID, _fail_txt, parse_mode="HTML", reply_markup=_kb_fail)
        except Exception:
            try:
                await bot.send_message(ADMIN_ID, _fail_txt, parse_mode="HTML", reply_markup=_kb_fail)
            except Exception:
                pass
    except Exception as _e:
        logging.error(f"claude chain {ref}: {_e}", exc_info=True)
        _claude_job_results[ref] = {"status": "done", "success": False,
            "error": "Внутренняя ошибка активации. Напиши Александру."}
    finally:
        # Возвращаем в пул коды, под которые не было стока (они целы и валидны).
        for _c_oos in _oos_release:
            try:
                await release_claude_code(_c_oos)
            except Exception as _e_oos:
                logging.error(f"release oos code {_c_oos}: {_e_oos}")
        try:
            if _claude_chain_active.get(user_id) == ref:
                _claude_chain_active.pop(user_id, None)
        except Exception:
            pass


@dp.callback_query(F.data.startswith("clnc_ok:"))
async def clnc_ok_handler(cb: CallbackQuery):
    """Админ подтвердил: подписка на сайте активирована → помечаем код, уведомляем клиента."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    _tok = cb.data.split(":", 1)[1]
    _ctx = _claude_needcheck.pop(_tok, None)
    if not _ctx:
        await cb.answer("Контекст устарел (бот перезапускался). Помети код вручную.", show_alert=True); return
    try:
        await _claude_notify_success(_ctx["ref"], _ctx["code"], _ctx["user_id"], _ctx["order_id"],
                                     _ctx["plan_name"], _ctx["org_id"], _ctx["site"], used_codes=None)
    except Exception as _e:
        logging.error(f"clnc_ok: {_e}")
        await cb.answer("Ошибка при подтверждении, см. лог.", show_alert=True); return
    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подтверждено — активирована", callback_data="noop")]]))
    except Exception:
        pass
    await cb.answer("Готово: код помечен, клиент уведомлён ✅")


@dp.callback_query(F.data.startswith("clnc_next:"))
async def clnc_next_handler(cb: CallbackQuery):
    """Админ подтвердил: на этом сайте НЕ активировалась → жжём код (во избежание двойной)
    и переносим активацию на ДРУГОЙ сайт (пропуская текущий провайдер)."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    _tok = cb.data.split(":", 1)[1]
    _ctx = _claude_needcheck.pop(_tok, None)
    if not _ctx:
        await cb.answer("Контекст устарел (бот перезапускался). Активируй вручную.", show_alert=True); return
    await cb.answer("Переношу на другой сайт…")
    # Активация НЕ прошла → возвращаем старый код в пул (он не израсходован), чтобы он
    # достался другому клиенту. Новый код возьмётся из пула ДРУГОГО сайта (текущий пропускаем).
    try:
        await release_claude_code(_ctx["code"])
    except Exception as _e:
        logging.error(f"clnc_next release: {_e}")
    # клиенту — снова «идёт активация», запускаем цепочку по ОСТАЛЬНЫМ сайтам
    import uuid as _uuid_nx
    _new_ref = _uuid_nx.uuid4().hex[:16]
    _claude_chain_active[_ctx["user_id"]] = _new_ref
    try:
        await bot.send_message(_ctx["user_id"],
            "⏳ Активация продолжается на другом сайте — подписка появится в течение нескольких минут.")
    except Exception:
        pass
    asyncio.create_task(_run_claude_activation_chain(
        _new_ref, _ctx["user_id"], _ctx["order_id"], _ctx["org_id"],
        _ctx["plan_name"], _ctx["plan_key"], skip_providers={_ctx["provider"]}))
    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🔄 Перенесено с {_ctx.get('site','')} на другой сайт", callback_data="noop")]]))
    except Exception:
        pass


@dp.callback_query(F.data.startswith("clfail_retry:"))
async def clfail_retry_handler(cb: CallbackQuery):
    """Финальный сбой → админ жмёт «Повторить автоактивацию» (напр. после пополнения стока)."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    _ctx = _claude_needcheck.pop(cb.data.split(":", 1)[1], None)
    if not _ctx:
        await cb.answer("Контекст устарел (бот перезапускался). Запусти активацию заново.", show_alert=True); return
    await cb.answer("Запускаю активацию заново…")
    import uuid as _uuid_r
    _new_ref = _uuid_r.uuid4().hex[:16]
    _claude_chain_active[_ctx["user_id"]] = _new_ref
    try:
        await bot.send_message(_ctx["user_id"],
            "⏳ Пробуем активировать подписку ещё раз — это займёт несколько минут.")
    except Exception:
        pass
    asyncio.create_task(_run_claude_activation_chain(
        _new_ref, _ctx["user_id"], _ctx["order_id"], _ctx["org_id"],
        _ctx["plan_name"], _ctx["plan_key"]))
    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Запущена повторная активация", callback_data="noop")]]))
    except Exception:
        pass


@dp.callback_query(F.data.startswith("clfail_manual:"))
async def clfail_manual_handler(cb: CallbackQuery):
    """Финальный сбой → админ активировал подписку вручную: закрываем заказ и уведомляем клиента."""
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    _ctx = _claude_needcheck.pop(cb.data.split(":", 1)[1], None)
    if not _ctx:
        await cb.answer("Контекст устарел (бот перезапускался).", show_alert=True); return
    try:
        await delete_claude_pending_activation(_ctx["user_id"])
    except Exception:
        pass
    _claude_job_results[_ctx["ref"]] = {"status": "done", "success": True}
    try:
        await bot.send_message(
            _ctx["user_id"],
            f"🎉 <b>Подписка Claude активирована!</b>\n\n"
            f"📦 Тариф: <b>{_ctx['plan_name']}</b>\n"
            f"🧩 Org ID: <code>{_ctx['org_id']}</code>\n\n"
            f"Проверь на claude.ai — если что-то не так, напиши Александру.",
            parse_mode="HTML")
    except Exception as _e:
        logging.error(f"clfail_manual notify: {_e}")
    try:
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Активировано вручную — клиент уведомлён", callback_data="noop")]]))
    except Exception:
        pass
    await cb.answer("Готово: клиент уведомлён ✅")


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
    _fb_code  = (body.get("code") or "").strip()

    user_id = _verify_tg_init_data(init_data)
    if not user_id and _fb_code:
        _fb = await get_claude_pending_activation_by_code(_fb_code)
        if _fb:
            user_id = _fb.get("user_id")
    if not user_id:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ <b>Mini-app: не прошла авторизация</b>\n"
                f"initData len=<b>{len(init_data)}</b> · "
                f"hash={'hash=' in init_data} · signature={'signature=' in init_data}",
                parse_mode="HTML")
        except Exception:
            pass
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
    provider  = pending.get("provider", "bpa")
    plan_key  = pending.get("plan", "pro")

    # ТЕСТ: если код фейковый (TEST-) — симулируем успех без реального запроса
    if code.startswith("TEST-"):
        import random as _rand
        fake_bpa = str(_rand.randint(9000000, 9999999))
        _claude_job_results[fake_bpa] = {"status": "queued"}
        asyncio.create_task(_claude_test_activation_job(fake_bpa, user_id, plan_name))
        await delete_claude_pending_activation(user_id)
        logging.info(f"Claude TEST activation: fake_bpa={fake_bpa} user={user_id}")
        return _resp({"order_id": fake_bpa, "status": "queued"})

    # БАГ 3 FIX: если job уже запущен — не делаем новый POST, возвращаем тот же order_id.
    # (только для bpa: bpa_order_id хранится в БД; у partner дедуп на стороне сайта по Idempotency-Key)
    existing_bpa = pending.get("bpa_order_id")
    if existing_bpa:
        _prev = _claude_job_results.get(str(existing_bpa)) or _claude_job_results.get(existing_bpa)
        _prev_failed = bool(_prev and _prev.get("status") == "done" and _prev.get("success") is False)
        if not _prev_failed:
            logging.info(f"Claude reuse bpa={existing_bpa} user={user_id}")
            return _resp({"order_id": str(existing_bpa), "status": "queued"})
        logging.info(f"Claude prev bpa={existing_bpa} failed — делаем новый POST")

    # Guard: повторная активация Claude за 29 дней — предупреждаем, повторное
    # нажатие «Попробовать снова» активирует принудительно (на другой аккаунт).
    try:
        _pool_dbl = await get_pool()
        async with _pool_dbl.acquire() as _c_dbl:
            _recent = await _c_dbl.fetchrow(
                "SELECT code, plan, used_at, org_id FROM claude_codes"
                " WHERE used_by=$1 AND used_at > NOW() - INTERVAL '29 days'"
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
                        f"⏱ Дата: <b>{_us}</b>\n"
                        f"🧩 Прошлый Org: <code>{_recent.get('org_id') or '—'}</code>\n"
                        f"🧩 Сейчас Org: <code>{org_id or '—'}</code>\n\n"
                        "Если клиент нажмёт «Попробовать снова» — активирует на другой аккаунт.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass
                # Показываем клиенту ОБА Organization ID: прошлый и текущий,
                # чтобы он сам понял — тот же это аккаунт или другой.
                _prev_org = (_recent.get("org_id") or "").strip()
                _same_org = bool(_prev_org and org_id and
                                 _prev_org.lower() == (org_id or "").lower())
                _l_cl = ["⚠️ На этот аккаунт уже активировали подписку Claude.\n"]
                if _prev_org:
                    _l_cl.append(f"🧩 Прошлая активация: {_prev_org} ({_us})")
                else:
                    _l_cl.append(f"📅 Прошлая активация: {_us}")
                if org_id:
                    _l_cl.append(f"🧩 Сейчас активируешь: {org_id}")
                if _same_org:
                    _l_cl.append(
                        "\nНа этот аккаунт была оформлена подписка менее месяца назад "
                        "и она ещё активна. Можете продлить подписку начиная с сегодняшнего дня "
                        "по кнопке «Попробовать снова».")
                elif org_id:
                    _l_cl.append(
                        "\nЭто ДРУГОЙ аккаунт — всё в порядке. Нажми «Попробовать снова», и активация пройдёт.")
                else:
                    _l_cl.append(
                        "\nЕсли оформляешь на ДРУГОЙ аккаунт (например, для друга) — нажми «Попробовать снова».")
                _l_cl.append("\nЕсли это случайно — напиши Александру.")
                return _resp({"error": "\n".join(_l_cl)})
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

    # ── Единая цепочка активации: непрерывная загрузка у клиента, а авто-фолбэк
    #    по всем сайтам (по числу свободных кодов тарифа) — под капотом. ──
    _prev_ref = _claude_chain_active.get(user_id)
    if _prev_ref:
        _pr = _claude_job_results.get(_prev_ref)
        if _pr and _pr.get("status") != "done":
            return _resp({"order_id": _prev_ref, "status": "queued"})
    _ref = "cl_" + str(uuid.uuid4())[:12]
    _claude_chain_active[user_id] = _ref
    _claude_job_results[_ref] = {"status": "queued"}
    _force_cl = bool(body.get("force"))   # клиент подтвердил пополнение поверх прежней активации
    asyncio.create_task(_run_claude_activation_chain(
        _ref, user_id, order_id, org_id, plan_name, plan_key, force=_force_cl))
    logging.info(f"Claude activation chain started: ref={_ref} user={user_id} plan={plan_key} force={_force_cl}")
    return _resp({"order_id": _ref, "status": "queued"})

    # ── (устар.) прежний пофайловый redeem-цикл ниже больше НЕ выполняется ──
    try:
        _order_all = await _claude_provider_order()
        if await _claude_failover_on():
            _try_order = [provider] + [p for p in _order_all if p != provider]
        else:
            _try_order = [provider]

        _last = None
        _last_msg = ""
        _last_prov = provider
        for _prov in _try_order:
            # браузерные сайты (6661231.xyz) не участвуют в синхронном redeem-фолбэке
            if CLAUDE_PROVIDERS.get(_prov, {}).get("api") == "browser":
                continue
            # для запасного сайта берём код из ЕГО пула (у каждого сайта свои коды)
            if _prov != provider:
                _newcode = await get_next_claude_code(plan_key, _prov)
                if not _newcode:
                    continue
                try:
                    await release_claude_code(code)   # вернём неиспользованный код прежнего сайта
                except Exception:
                    pass
                code = _newcode
                provider = _prov
                _pool_sw = await get_pool()
                async with _pool_sw.acquire() as _csw:
                    await _csw.execute(
                        "UPDATE claude_pending_activations SET code=$1, provider=$2 WHERE user_id=$3",
                        code, provider, user_id)
                try:
                    await bot.send_message(
                        ADMIN_ID,
                        f"🔀 <b>Claude — фолбэк при активации</b>\n"
                        f"Ушли на <b>{claude_provider_name(_prov)}</b> (на прежнем сайте нет стока).",
                        parse_mode="HTML")
                except Exception:
                    pass

            _res = await _claude_redeem_via(provider, code, org_id, order_id)
            if _res.get("ok"):
                _ref = _res["ref"]
                _is_bpa = CLAUDE_PROVIDERS.get(provider, {}).get("api", "bpa") == "bpa"
                _pool3 = await get_pool()
                async with _pool3.acquire() as _c3:
                    if _is_bpa:
                        await _c3.execute(
                            "UPDATE claude_pending_activations "
                            "SET org_id=$1, bpa_order_id=$2, provider=$3 WHERE user_id=$4",
                            org_id, int(_ref), provider, user_id)
                    else:
                        # partner: order_no — строка, в INTEGER-колонку не пишем
                        await _c3.execute(
                            "UPDATE claude_pending_activations "
                            "SET org_id=$1, bpa_order_id=NULL, provider=$2 WHERE user_id=$3",
                            org_id, provider, user_id)
                asyncio.create_task(_claude_activation_polling_job(
                    _ref, code, user_id, order_id, plan_name, org_id, provider))
                logging.info(f"Claude activation via {provider}: ref={_ref} user={user_id} code={code}")
                return _resp({"order_id": _ref, "status": "queued"})

            _kind = _res.get("err_kind", "other")
            if _kind == "already_claimed":
                return _resp({"error": "Код уже активирован. Напиши Александру."})
            if _kind == "not_found":
                return _resp({"error": "Код не найден. Напиши Александру."})
            if _kind == "bad_org":
                return _resp({"error": "Неверный Organization ID — проверь и попробуй снова."})
            if _kind == "out_of_stock":
                _last = "out_of_stock"
                continue   # пробуем следующий сайт (если фолбэк вкл)
            # network / other — пробуем следующий сайт
            logging.error(f"Claude redeem {provider}: {_res.get('err_msg')}")
            _last = _kind
            _last_msg = _res.get("err_msg") or _kind
            _last_prov = provider
            continue

        # все доступные сайты исчерпаны
        if _last == "out_of_stock":
            try:
                await bot.send_message(
                    ADMIN_ID,
                    f"🚨 <b>Claude — нет стока НА ВСЕХ сайтах!</b>\n"
                    f"👤 <code>{user_id}</code> ({plan_name})\n"
                    f"Пополни коды/сток на сайтах Claude.",
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return _resp({"error": "Временно нет активаций. Александр активирует вручную."})
        # диагностика: сообщаем админу ТОЧНУЮ причину отказа сайта
        try:
            await bot.send_message(
                ADMIN_ID,
                f"❌ <b>Claude — активация не прошла</b>\n"
                f"👤 <code>{user_id}</code> · {plan_name}\n"
                f"🔧 Сайт: <b>{claude_provider_name(_last_prov)}</b>\n"
                f"📄 Код: <code>{code}</code>\n"
                f"🧩 Org ID: <code>{org_id}</code>\n"
                f"⚠️ Причина: <code>{(_last_msg or _last or 'unknown')}</code>",
                parse_mode="HTML")
        except Exception:
            pass
        return _resp({"error": "Не удалось активировать. Попробуй ещё раз или напиши Александру."})

    except aiohttp.ClientError as _e:
        logging.error(f"Claude activate network: {_e}")
        return _resp({"error": "Нет связи с сервисом. Попробуй ещё раз."})
    except Exception as _e:
        logging.error(f"Claude activate error: {_e}", exc_info=True)
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ <b>Claude activate exception</b>\n"
                f"user=<code>{user_id}</code> code=<code>{code}</code>\n"
                f"{type(_e).__name__}: {str(_e)[:300]}",
                parse_mode="HTML")
        except Exception:
            pass
        return _resp({"error": "Внутренняя ошибка. Напиши Александру."})


async def api_activate_claude_status_handler(request: web.Request) -> web.Response:
    """GET /api/activate-claude-status/{order_id}. order_id может быть строкой
    (partner-API «R…») или числом (bpa) — ищем по обоим вариантам ключа."""
    import json as _j2
    _raw = (request.match_info.get("order_id", "") or "").strip()
    result = _claude_job_results.get(_raw)
    if result is None:
        try:
            result = _claude_job_results.get(int(_raw))
        except (ValueError, TypeError):
            result = None
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

    webapp_url = (
        f"{WEBAPP_BASE_URL}/webapp/perplexity"
        f"?plan={_up.quote(plan_name)}&code={_up.quote(code)}"
    )
    try:
        await save_perplexity_pending_activation(user_id, code, order_id, plan, plan_name)
        import datetime as _dt_cl
        _base_cl = (
            f"🎉 <b>Оплата прошла!</b>\n\n"
            f"📦 <b>Perplexity {plan_name}</b>\n\n"
            f"Осталось активировать подписку — нажми кнопку ниже, "
            f"введи Perplexity User ID (perplexity.ai/api/auth/session), и подписка "
            f"активируется автоматически за 1–2 минуты 👇\n\n"
            f"🎟 Код активации: <code>{code}</code>"
            f"{delayed_note}"
        )
        _kb_cl_active = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Активировать Perplexity", style="success", web_app=_WAI(url=webapp_url))],
            [InlineKeyboardButton(text="❓ Нужна помощь", style="primary", callback_data="perplexity_need_help")],
        ])
        _kb_cl_expired = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❓ Нужна помощь", style="primary", callback_data="perplexity_need_help")],
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
        try:
            await bot.send_message(
                user_id,
                "📋 <b>Инструкция по активации Perplexity</b>\n\n"
                "1️⃣ Зайди на <b>perplexity.ai</b> и авторизуйся (в Chrome или Safari).\n"
                "2️⃣ Открой страницу сессии:\n"
                "<code>perplexity.ai/api/auth/session</code>\n"
                "3️⃣ Скопируй значение поля «id» (UUID).\n"
                "4️⃣ Вернись в мини-приложение (кнопка «Активировать Perplexity»), "
                "вставь User ID — подписка активируется автоматически за 1–2 минуты.\n\n"
                f"🎟 Код активации: <code>{code}</code>\n"
                "⚠️ Убедись, что вошёл именно в нужный аккаунт Perplexity.",
                parse_mode="HTML")
        except Exception:
            pass
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
    _replaced_code = None  # авто-замена кода отключена; ветка elif оставлена мёртвой
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
                    try:
                        _ord_ok = await fk_get_order(order_id)
                        _amid_ok = (_ord_ok or {}).get("admin_msg_id")
                    except Exception:
                        _amid_ok = None
                    if _amid_ok:
                        try:
                            await bot.edit_message_text(_caption_ok, chat_id=ADMIN_ID, message_id=_amid_ok, parse_mode="HTML")
                        except Exception:
                            await bot.send_message(ADMIN_ID, _caption_ok, parse_mode="HTML")
                    else:
                        await bot.send_message(ADMIN_ID, _caption_ok, parse_mode="HTML")
                except Exception:
                    pass
                _fail_clear("perplexity", user_id)
                await log_event(user_id, "perplexity_activation_ok",
                                f"code={code} bpa={bpa_order_id} plan={plan_name}")
                return

            # ── Ошибка ─────────────────────────────────────────
            elif status == "failed":
                _err = _d.get("error") or "Ошибка активации"
                _perplexity_job_results[bpa_order_id] = {
                    "status": "done", "success": False, "error": _err
                }
                # Сбрасываем bpa_order_id в pending, чтобы «Попробовать снова» создал НОВЫЙ BPA-заказ
                try:
                    _pool_rb = await get_pool()
                    async with _pool_rb.acquire() as _c_rb:
                        await _c_rb.execute(
                            "UPDATE perplexity_pending_activations SET bpa_order_id=NULL WHERE user_id=$1", user_id)
                except Exception:
                    pass
                _is_stock = ("out of stock" in _err.lower() or "out-of-stock" in _err.lower())
                if _is_stock:
                    # провайдер временно без стока — код клиента ОСТАЁТСЯ валидным.
                    # НЕ возвращаем в пул и НЕ удаляем pending: клиент сам повторит, когда пополнят.
                    _fail_note = "Временно нет мест у провайдера — код сохранён за клиентом, можно повторить."
                else:
                    # Сбой обычно временный (провайдер: payment/timeout). Код НЕ сжигаем и НЕ ротируем —
                    # оставляем закреплённым за клиентом: можно повторить тем же кодом или активировать вручную.
                    _fail_note = "Код сохранён — повтори тем же кодом позже или активируй вручную."

                # ── алерт админу (не чаще 1 раза в 15 мин на клиента) ──
                if _fail_should_alert("perplexity", user_id):
                    try:
                        _caption_fail = (
                            f"❌ <b>Perplexity FAILED</b>\n"
                            f"👤 <code>{user_id}</code>  📦 {plan_name}\n"
                            f"🔑 <code>{code}</code>  🔢 BPA: <code>{bpa_order_id}</code>\n"
                            f"❌ {_err[:300]}"
                            + f"\n♻️ {_fail_note}"
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
                    if _is_stock:
                        await bot.send_message(
                            user_id,
                            "⏳ <b>Временно нет мест у провайдера</b>\n\n"
                            "Твой код сохранён и остаётся действительным. Провайдер пополняет запас — "
                            "попробуй активировать снова через 5–10 минут 👇",
                            parse_mode="HTML",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(
                                    text="🔁 Попробовать снова",
                                    callback_data="perplexity_reopen_webapp"
                                )],
                                [InlineKeyboardButton(
                                    text="❓ Нужна помощь", style="primary",
                                    callback_data="perplexity_need_help"
                                )],
                            ])
                        )
                    elif _replaced_code:
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
                                    text="❓ Нужна помощь", style="primary",
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
                                    text="❓ Нужна помощь", style="primary",
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
                    text="❓ Нужна помощь", style="primary",
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
    _fb_code  = (body.get("code") or "").strip()

    user_id = _verify_tg_init_data(init_data)
    if not user_id and _fb_code:
        _fb = await get_perplexity_pending_activation_by_code(_fb_code)
        if _fb:
            user_id = _fb.get("user_id")
    if not user_id:
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ <b>Mini-app: не прошла авторизация</b>\n"
                f"initData len=<b>{len(init_data)}</b> · "
                f"hash={'hash=' in init_data} · signature={'signature=' in init_data}",
                parse_mode="HTML")
        except Exception:
            pass
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
        _prev = _perplexity_job_results.get(existing_bpa)
        _prev_failed = bool(_prev and _prev.get("status") == "done" and _prev.get("success") is False)
        if not _prev_failed:
            # Polling job уже работает/завершился успехом — возвращаем тот же order_id
            logging.info(f"Perplexity reuse bpa={existing_bpa} user={user_id}")
            return _resp({"order_id": existing_bpa, "status": "queued"})
        logging.info(f"Perplexity prev bpa={existing_bpa} failed — делаем новый POST")

    # Guard: повторная активация Perplexity за 29 дней — предупреждаем, повторное
    # нажатие «Попробовать снова» активирует принудительно (на другой аккаунт).
    try:
        _pool_dbl = await get_pool()
        async with _pool_dbl.acquire() as _c_dbl:
            _recent = await _c_dbl.fetchrow(
                "SELECT code, plan, used_at FROM perplexity_codes"
                " WHERE used_by=$1 AND used_at > NOW() - INTERVAL '29 days'"
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
        try:
            await bot.send_message(
                ADMIN_ID,
                f"⚠️ <b>Perplexity activate exception</b>\n"
                f"user=<code>{user_id}</code> code=<code>{code}</code>\n"
                f"{type(_e).__name__}: {str(_e)[:300]}",
                parse_mode="HTML")
        except Exception:
            pass
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
            [InlineKeyboardButton(text="❓ Нужна помощь", style="primary", callback_data="linkpay_help")],
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
            f"🆔 Заказ: <code>{order['fk_order_id']}</code>\n"
            + await _fk_num_line(order['fk_order_id'])
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подписка готова", callback_data=f"lp_done:{order['fk_order_id']}")],
            [InlineKeyboardButton(text="✍️ Уточнение",       callback_data=f"lp_clarify:{order['fk_order_id']}"),
             InlineKeyboardButton(text="📜 История",          callback_data=f"lp_thread:{order['fk_order_id']}")],
            [InlineKeyboardButton(text="🗑 Отменить заказ",   callback_data=f"lp_cancel:{order['fk_order_id']}")],
        ])
        # ВСЕГДА шлём НОВОЕ сообщение (не редактируем старое «оплачен»), чтобы
        # заказ со ссылкой всплыл внизу чата и не потерялся. admin_msg_id обновляем
        # на новое сообщение — статус-кнопки (Готово/Уточнение) будут править именно его.
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
            [InlineKeyboardButton(text="❓ Нужна помощь", style="primary", callback_data="linkpay_help")],
        ])
        await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logging.error(f"_send_creds_instructions uid={user_id}: {e}")


# ── Ручная выдача по тарифу (manual): заказ админу, без авто-активации ────────

def _lp_kb(order_id, full=True):
    """Кнопки заказа (вход/ссылка/ручной). full=True — полный набор с «Подписка готова»."""
    if full:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Подписка готова", callback_data=f"lp_done:{order_id}")],
            [InlineKeyboardButton(text="✍️ Уточнение", callback_data=f"lp_clarify:{order_id}"),
             InlineKeyboardButton(text="📜 История", callback_data=f"lp_thread:{order_id}")],
            [InlineKeyboardButton(text="🗑 Отменить заказ", callback_data=f"lp_cancel:{order_id}")],
        ])
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 История", callback_data=f"lp_thread:{order_id}"),
         InlineKeyboardButton(text="🗑 Отменить", callback_data=f"lp_cancel:{order_id}")],
    ])


async def _is_manual_plan(shop_key, plan_idx) -> bool:
    try:
        return (await get_setting(f"manual:{shop_key}:{plan_idx}", "0") or "0") == "1"
    except Exception:
        return False


async def _send_manual_order(user_id, shop_key, service_name, plan_name,
                             order_id, amount_rub, delayed_note=""):
    """Тариф с ручной выдачей: создаём заказ, уведомляем клиента и админа (с кнопками)."""
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
                                   kind="manual", status="awaiting_payment")
        # Номер платежа в FreeKassa — чтобы клиент и админ сверялись по ОДНОМУ номеру
        _fk_no_cl = ""
        try:
            _dbo_cl = await fk_get_order(order_id)
            _fk_no_cl = (_dbo_cl or {}).get("fk_intid") or ""
        except Exception:
            pass
        _num_line = (f"\U0001f9fe Номер заказа: <code>{_fk_no_cl}</code>\n\n"
                     if _fk_no_cl else f"\U0001f194 Номер заказа: <code>{order_id}</code>\n\n")
        import urllib.parse as _uq_manual
        _msg_to_alex = _uq_manual.quote(
            f"Приветствую! Оплатил заказ.\nСервис: {service_name}\n"
            f"Номер заказа: {_fk_no_cl or order_id}")
        await bot.send_message(
            user_id,
            f"🎉 <b>Оплата прошла!</b>\n\n📦 <b>{service_name}</b>\n\n"
            f"{_num_line}"
            f"❗️ <b>Отправьте Александру чек об оплате и номер вашего заказа</b> 👇"
            f"{delayed_note}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✍️ Написать Александру",
                                      url=f"https://t.me/{PERSONAL_USERNAME}?text={_msg_to_alex}")],
            ]))
        # Сообщение админу формирует section 4 (единое изменяющееся сообщение заказа)
        await log_event(user_id, "manual_order", f"order={order_id} svc={shop_key}")
    except Exception as e:
        logging.error(f"_send_manual_order uid={user_id}: {e}")
