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
    bot, dp, validate_chat_prompt,
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
)
from background import (
    _activation_jobs_cleanup_loop, _claude_job_results_cleanup_loop, _memory_cleanup_loop, auto_recover_lost_videos_loop, claude_codes_cleanup_loop, cleanup_stale_generations_loop,
    credit_batches_loop, db_cleanup_loop, fk_auto_check_loop, gpt_code_rechecker_loop, gpt_codes_cleanup_loop, nsgifts_balance_alert_loop,
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
    ~F.text.startswith("/recover") & ~F.text.startswith("/emoji") & ~F.text.startswith("/shopkeys") &
    ~F.text.startswith("/nsg_")
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


async def main():
    await _ensure_playwright_browser()
    await init_db()
    await load_prices_from_db()
    asyncio.create_task(setup_webhook_server())
    asyncio.create_task(cleanup_stale_generations_loop())
    asyncio.create_task(auto_recover_lost_videos_loop())
    asyncio.create_task(fk_auto_check_loop())
    asyncio.create_task(_memory_cleanup_loop())
    asyncio.create_task(credit_batches_loop())
    asyncio.create_task(subscription_reminder_loop())
    asyncio.create_task(reminders_loop())
    asyncio.create_task(db_cleanup_loop())
    asyncio.create_task(gpt_codes_cleanup_loop())
    asyncio.create_task(gpt_code_rechecker_loop())
    asyncio.create_task(_activation_jobs_cleanup_loop())
    asyncio.create_task(claude_codes_cleanup_loop())
    asyncio.create_task(_claude_job_results_cleanup_loop())
    # NS Gifts: инициализируем клиент и фоновые задачи

    if NSGIFTS_USER_ID and NSGIFTS_LOGIN and NSGIFTS_API_SECRET:
        from ns_gifts import NSGiftsClient
        rt.nsgifts_client = NSGiftsClient(
            user_id    = NSGIFTS_USER_ID,
            login      = NSGIFTS_LOGIN,
            password   = NSGIFTS_PASSWORD,
            api_secret = NSGIFTS_API_SECRET,
            proxy      = WEBSHARE_PROXY,
        )
        logging.info("✅ NS Gifts client initialized")
        asyncio.create_task(nsgifts_balance_alert_loop())
    else:
        logging.warning("⚠️  NS Gifts: env-переменные не заданы — App Store отключён")

    await dp.start_polling(bot)


# ─── /myip — текущий исходящий IP сервера (Railway) ──────────────────────────

if __name__ == "__main__":
    asyncio.run(main())

