# -*- coding: utf-8 -*-
# Auto-split module "bot (entry point)" — part of Neirosetkaa-bot (refactored from bot.py).
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
    ADMIN_ID, NSGIFTS_API_SECRET, NSGIFTS_LOGIN, NSGIFTS_PASSWORD, NSGIFTS_USER_ID, WEBSHARE_PROXY,
    NSGIFTS_PROXY, bot, dp, validate_chat_prompt,
)
from runtime_state import (
    rt,
)
from db import (
    ensure_user, get_setting, init_db, is_blocked, load_prices_from_db,
)
from keyboards import (
    kb_after_consultant_reply,
)
from common import (
    _ensure_playwright_browser, claude_with_search, setup_webhook_server, process_linkpay_link,
    load_miniapp_toggles, _assistant_enabled, ASSISTANT_OFF_TEXT, _assistant_off_kb,
)
from background import (
    _activation_jobs_cleanup_loop, _claude_job_results_cleanup_loop, _memory_cleanup_loop, auto_recover_lost_videos_loop, claude_codes_cleanup_loop, cleanup_stale_generations_loop,
    credit_batches_loop, coins_refund_loop, db_cleanup_loop, fk_auto_check_loop, gpt_code_rechecker_loop, gpt_codes_cleanup_loop, models_desc_refresh_loop, nsgifts_balance_alert_loop, perplexity_codes_cleanup_loop,
    reminders_loop, subscription_reminder_loop,
)
from _registration_order import ORIG_ORDER as _ORIG_ORDER

# -- import handler modules (registers their @dp handlers) --
import handlers_user
import handlers_shop
import handlers_generation
import handlers_chat
import handlers_admin
import handlers_gpt
import handlers_claude
import handlers_perplexity
import handlers_linkpay
import handlers_nsgifts
import handlers_desc

# ── Premium-эмодзи: middleware подменяет обычные эмодзи на custom во всех
# исходящих сообщениях (HTML-текст → <tg-emoji>, инлайн-кнопки → иконка). ──
from premium_emoji import PremiumEmojiMiddleware
bot.session.middleware(PremiumEmojiMiddleware())

# handle_message: the broad catch-all (defined here, order fixed below)
@dp.message(
    StateFilter(None),  # только вне FSM-состояний — иначе перехватывает admin/edit states
    ~F.text.startswith("/privacy") & ~F.text.startswith("/publicoffer") &
    ~F.text.startswith("/help") & ~F.text.startswith("/ref") & ~F.text.startswith("/start") &
    ~F.text.startswith("/admin") & ~F.text.startswith("/test_fk") & ~F.text.startswith("/credit") &
    ~F.text.startswith("/sub") & ~F.text.startswith("/add_gpt_codes") &
    ~F.text.startswith("/gpt_codes_status") & ~F.text.startswith("/test_gpt_webapp") &
    ~F.text.startswith("/test_chatgpt") & ~F.text.startswith("/test_claude_webapp") &
    ~F.text.startswith("/test_perplexity_webapp") &
    ~F.text.startswith("/test_linkpay") &
    ~F.text.startswith("/test_creds") &
    ~F.text.startswith("/myip") & ~F.text.startswith("/audit") &
    ~F.text.startswith("/fix_all_balances") & ~F.text.startswith("/setcredits") &
    ~F.text.startswith("/recover") & ~F.text.startswith("/falcheck") & ~F.text.startswith("/emoji") & ~F.text.startswith("/shopkeys") &
    ~F.text.startswith("/nsg_") & ~F.text.startswith("/refresh_desc") &
    ~F.text.startswith("/apply_desc") &
    ~F.text.startswith("/subs_restore") & ~F.text.startswith("/release_codes")
)
async def handle_message(message: Message, state: FSMContext):
    if not message.text:
        return
    await ensure_user(message.from_user.id, message.from_user.username or '', message.from_user.full_name)
    uid = message.from_user.id
    if uid != ADMIN_ID and await get_setting("maintenance") == "1":
        await message.answer("⚙️ Бот на техобслуживании. Скоро вернётся!")
        return
    if await is_blocked(uid):
        await message.answer("🚫 Ваш доступ к боту ограничен.")
        return

    # Link-pay: клиент прислал ссылку на оплату по ожидающему заказу
    try:
        if await process_linkpay_link(uid, message.text):
            return
    except Exception:
        pass

    # AI-Консультант выключен админом — показываем заглушку с каталогом/контактом
    if not await _assistant_enabled():
        await message.answer(ASSISTANT_OFF_TEXT, reply_markup=_assistant_off_kb(), parse_mode="HTML")
        return

    # Валидация сообщения для консультанта
    ok_v, err = validate_chat_prompt(message.text)
    if not ok_v and err:
        await message.answer(err)
        return

    await bot.send_chat_action(message.chat.id, "typing")
    reply = await claude_with_search(uid, message.text)
    try:
        await message.answer(reply, reply_markup=kb_after_consultant_reply(), parse_mode="HTML")
    except Exception:
        await message.answer(reply, reply_markup=kb_after_consultant_reply())

# ══════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════
#  FREEKASSA - ОПЛАТА СБП
# ══════════════════════════════════════════════════════════



# -- Restore EXACT original handler registration order ------------------------
# Splitting handlers across modules changes the order @dp decorators run in.
# For callbacks this is harmless (filters are mutually exclusive); for message
# handlers order can matter (reply-buttons vs FSM states). We sort each
# observer's handler list by the handler's original line number in the old
# monolithic bot.py, so dispatch is byte-for-byte identical to before.
def _restore_handler_order():
    for _ev, _obs in dp.observers.items():
        try:
            _obs.handlers.sort(
                key=lambda h: _ORIG_ORDER.get(getattr(h.callback, "__name__", ""), 10**9)
            )
        except Exception as _e:
            logging.warning("could not restore handler order for %s: %s", _ev, _e)

_restore_handler_order()


# ── Техобслуживание: блокируем НЕ-админов для ВСЕХ апдейтов, пока включён режим ──
# Раньше проверка была только в handle_message (консультант) и не трогала кнопки/команды,
# поэтому режим выглядел «нерабочим». Теперь — единый шлюз на весь бот (кроме админа).
import time as _maint_time
_maint_cache = {"val": "0", "ts": 0.0}

async def _maintenance_on() -> bool:
    if _maint_time.time() - _maint_cache["ts"] > 10:
        try:
            _maint_cache["val"] = await get_setting("maintenance", "0")
        except Exception:
            pass
        _maint_cache["ts"] = _maint_time.time()
    return _maint_cache["val"] == "1"

@dp.update.outer_middleware()
async def _maintenance_guard(handler, event, data):
    user = data.get("event_from_user")
    if user is None:
        _obj = getattr(event, "message", None) or getattr(event, "callback_query", None)
        user = getattr(_obj, "from_user", None)
    if user is not None and user.id != ADMIN_ID and await _maintenance_on():
        cbq = getattr(event, "callback_query", None)
        msg = getattr(event, "message", None)
        try:
            if cbq is not None:
                await cbq.answer("⚙️ Идут техработы. Загляни чуть позже 🙏", show_alert=True)
            elif msg is not None:
                await msg.answer("⚙️ Бот на техобслуживании. Скоро вернёмся!")
        except Exception:
            pass
        return  # прерываем обработку апдейта
    return await handler(event, data)


# ── Глобальный перехват необработанных ошибок ────────────────────────────────
# Раньше любое непойманное исключение в хендлере означало ПОЛНУЮ тишину для
# клиента (ответа нет, кнопка «не работает») и отсутствие алерта для админа.
# Теперь: пишем traceback в лог, извиняемся перед клиентом, алертим админа
# (notify_admin_error сам троттлит одинаковые ошибки — не чаще 1 раза в 10 мин).
def _register_global_error_handler():
    try:
        @dp.errors()
        async def _global_error_handler(event):
            exc = getattr(event, "exception", None)
            upd = getattr(event, "update", None)

            # Безобидные ошибки Telegram: клиент уже всё получил, реагировать нечем.
            # Самая частая — устаревший callback после долгой генерации видео
            # ("query is too old"): ответить на него уже нельзя, но это не сбой.
            _low = str(exc or "").lower()
            if ("query is too old" in _low or "query id is invalid" in _low
                    or "message is not modified" in _low
                    or "message to delete not found" in _low
                    or "message to edit not found" in _low
                    or "message can't be deleted" in _low):
                logging.info(f"Безобидная ошибка Telegram (без алерта): {str(exc)[:150]}")
                return True

            try:
                logging.exception(f"UNHANDLED в хендлере: {type(exc).__name__}: {exc}")
            except Exception:
                pass

            # 1) Отвечаем клиенту, чтобы бот не «молчал».
            #    Исключение — безобидные ошибки Telegram (см. ниже): по ним клиент
            #    уже всё получил, лишнее «что-то пошло не так» только пугает.
            _uid = None
            try:
                cbq = getattr(upd, "callback_query", None)
                msg = getattr(upd, "message", None)
                if cbq is not None:
                    _uid = cbq.from_user.id
                    try:
                        await cbq.answer("⚠️ Что-то пошло не так. Попробуй ещё раз 🙏", show_alert=True)
                    except Exception:
                        pass
                elif msg is not None:
                    _uid = msg.from_user.id if msg.from_user else None
                    try:
                        await msg.answer(
                            "⚠️ Что-то пошло не так при обработке запроса.\n"
                            "Попробуй ещё раз или напиши @neirosetkaalex."
                        )
                    except Exception:
                        pass
            except Exception:
                pass

            # 2) Алертим админа (с троттлингом внутри notify_admin_error)
            try:
                from common import notify_admin_error
                await notify_admin_error(f"Необработанная ошибка uid={_uid}", exc or Exception("unknown"))
            except Exception:
                pass
            return True

        logging.info("✅ Глобальный обработчик ошибок зарегистрирован")
    except Exception as _e:
        logging.warning(f"Не удалось зарегистрировать глобальный обработчик ошибок: {_e}")


_register_global_error_handler()


# Все фоновые циклы устроены как `while True: try/except`, но если задача всё же
# умрёт (ошибка вне try, отмена, падение при старте) — раньше об этом никто бы не
# узнал: тихо переставали возвращаться коды, монетки, сгорать партии, уходить
# напоминания. Супервизор перезапускает такую задачу и пишет админу.
_BG_TASKS: dict = {}


def _spawn_bg(coro_factory, name: str):
    async def _runner():
        _fails = 0
        while True:
            try:
                await coro_factory()
                logging.error(f"⚠️ Фоновая задача {name} завершилась сама — перезапуск через 60с")
            except asyncio.CancelledError:
                raise
            except Exception as _e_bg:
                _fails += 1
                logging.exception(f"💥 Фоновая задача {name} упала ({_fails}): {_e_bg}")
                # Алертим только первые падения, чтобы не спамить при системном сбое
                if _fails <= 3:
                    try:
                        from common import notify_admin_error
                        await notify_admin_error(f"Фоновая задача {name} упала", _e_bg)
                    except Exception:
                        pass
            await asyncio.sleep(60)

    _t = asyncio.create_task(_runner())
    _BG_TASKS[name] = _t
    return _t


async def main():
    await _ensure_playwright_browser()
    await init_db()
    await load_prices_from_db()
    await load_miniapp_toggles()
    asyncio.create_task(setup_webhook_server())
    _spawn_bg(cleanup_stale_generations_loop, "cleanup_stale_generations_loop")
    _spawn_bg(auto_recover_lost_videos_loop, "auto_recover_lost_videos_loop")
    _spawn_bg(fk_auto_check_loop, "fk_auto_check_loop")
    _spawn_bg(_memory_cleanup_loop, "_memory_cleanup_loop")
    _spawn_bg(credit_batches_loop, "credit_batches_loop")
    _spawn_bg(subscription_reminder_loop, "subscription_reminder_loop")
    _spawn_bg(reminders_loop, "reminders_loop")
    _spawn_bg(db_cleanup_loop, "db_cleanup_loop")
    _spawn_bg(gpt_codes_cleanup_loop, "gpt_codes_cleanup_loop")
    _spawn_bg(gpt_code_rechecker_loop, "gpt_code_rechecker_loop")
    _spawn_bg(_activation_jobs_cleanup_loop, "_activation_jobs_cleanup_loop")
    _spawn_bg(claude_codes_cleanup_loop, "claude_codes_cleanup_loop")
    _spawn_bg(perplexity_codes_cleanup_loop, "perplexity_codes_cleanup_loop")
    _spawn_bg(coins_refund_loop, "coins_refund_loop")
    _spawn_bg(models_desc_refresh_loop, "models_desc_refresh_loop")
    _spawn_bg(_claude_job_results_cleanup_loop, "_claude_job_results_cleanup_loop")
    # NS Gifts: инициализируем клиент и фоновые задачи

    if NSGIFTS_USER_ID and NSGIFTS_LOGIN and NSGIFTS_API_SECRET:
        from ns_gifts import NSGiftsClient
        rt.nsgifts_client = NSGiftsClient(
            user_id    = NSGIFTS_USER_ID,
            login      = NSGIFTS_LOGIN,
            password   = NSGIFTS_PASSWORD,
            api_secret = NSGIFTS_API_SECRET,
            # NSGIFTS_PROXY задан → весь трафик NS Gifts через прокси с фиксированным IP
            # (его один вносим в whitelist NS Gifts): не важно, на какой из 3 общих
            # Railway-IP сел контейнер — сайт всегда видит один адрес. Пусто → напрямую.
            proxy      = NSGIFTS_PROXY,
        )
        logging.info("✅ NS Gifts client initialized")
        _spawn_bg(nsgifts_balance_alert_loop, "nsgifts_balance_alert_loop")
    else:
        logging.warning("⚠️  NS Gifts: env-переменные не заданы — App Store отключён")

    await dp.start_polling(bot)


# ─── /myip — текущий исходящий IP сервера (Railway) ──────────────────────────

if __name__ == "__main__":
    asyncio.run(main())

