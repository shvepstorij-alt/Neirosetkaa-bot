# -*- coding: utf-8 -*-
# Auto-split module "handlers_user" — part of Neirosetkaa-bot (refactored from bot.py).
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
    ADMIN_ID, ADMIN_USERNAME, CHANNEL_ID, FREE_CREDITS, PERSONAL_USERNAME, REF_BONUS,
    SHOP_CATALOG, WELCOME_BACK, WELCOME_NEW, bot, dp, is_admin,
    strip_surrogates,
)
from db import (
    add_credits_batch, ensure_user, fk_get_order, get_coins, get_credits, get_gen_count,
    get_pool, get_user, is_blocked,
)
from keyboards import (
    _eib, kb_image_brands, kb_main, kb_reply, kb_video_brands, tg_emoji_ui,
)
from common import (
    _show_profile, fk_credit_paid_order,
)

@dp.message(F.text.startswith("/start"), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id

    # Заблокированный юзер получает короткое сообщение и ничего не происходит
    if await is_blocked(uid):
        await message.answer("🚫 Ваш аккаунт заблокирован. Для уточнений - напишите @neirosetkaalex")
        return

    # Парсим реф-параметр: /start ref_123456
    parts = message.text.strip().split()
    referred_by = None
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            rid = int(parts[1][4:])
            if rid != uid:
                # Проверяем что пригласивший не заблокирован (иначе можно ему рефбонусами нагадить)
                if not await is_blocked(rid):
                    referred_by = rid
        except ValueError:
            pass

    existing = await get_user(uid)
    is_new = existing is None

    await ensure_user(
        uid,
        message.from_user.username or '',
        message.from_user.full_name,
        referred_by=referred_by if is_new else None
    )
    credits = await get_credits(uid)
    is_admin = (uid == ADMIN_ID)

    # Уведомляем пригласившего
    if is_new and referred_by:
        try:
            await bot.send_message(
                referred_by,
                f"🎉 <b>По твоей ссылке зарегистрировался новый пользователь!</b>\n\n"
                f"💰 <b>+{REF_BONUS} кредитов</b> начислятся тебе когда он сделает первую покупку.",
                parse_mode="HTML"
            )
        except Exception:
            pass
        text = (
            f"👋 Привет, {message.from_user.first_name}!\n"
            f"Я - бот Neirosetka 🎨 Помогу создавать фото и видео с помощью ИИ прямо в Telegram.\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎁 Тебя пригласил друг!\n"
            f"Получи <b>+{REF_BONUS} бонусных кредитов</b> 🎉\n"
            f"💵 Баланс: <b>{credits} кредитов</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎨 Что можно сделать:\n"
            f"📷 Генерация изображений\n"
            f"🎬 Генерация видео\n"
            f"🖌 Редактирование фото по описанию\n"
            f"🏃 Анимация фото в видео\n"
            f"🎭 Motion Control - перенос движений с видео на фото\n"
            f"🤖 AI-консультант по нейросетям и подключению VPN - бесплатно\n"
            f"🛍 Магазин подписок - ChatGPT, Claude, Midjourney, Grok и многие другие!\n\n"
            f"⏳ Кредиты действуют 30 дней\n"
            f"📢 Гайды и новости у нас в канале @{ADMIN_USERNAME}\n\n"
            f"Выбери действие 👇"
        )
    else:
        gen_count = await get_gen_count(uid) if not is_new else 0
        text = (WELCOME_NEW if is_new else WELCOME_BACK).format(
            name=message.from_user.first_name,
            credits=credits,
            gen_count=gen_count,
            channel=ADMIN_USERNAME,
        )

    await message.answer("👇", reply_markup=kb_reply(is_admin))
    await message.answer(text, reply_markup=kb_main(), parse_mode="HTML", disable_web_page_preview=True)


@dp.callback_query(F.data == "show_profile")
async def show_profile_cb(cb: CallbackQuery):
    await cb.answer()
    await reply_profile(cb.message)


@dp.message(F.text.startswith("/sub"), StateFilter("*"))
async def cmd_sub(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    import datetime as _dt
    parts = (message.text or "").split(maxsplit=4)
    if len(parts) < 2:
        await message.answer(
            "📋 <b>Управление подписками</b>\n\n"
            "Команды:\n"
            "<code>/sub add USER_ID СЕРВИС ДАТА [ТАРИФ]</code>\n"
            "<code>/sub list USER_ID</code>\n"
            "<code>/sub del SUB_ID</code>\n\n"
            "Форматы даты: <code>01.08.2026</code> или <code>+30</code> (дней)",
            parse_mode="HTML"
        )
        return
    action = parts[1].lower()
    pool = await get_pool()
    if action == "add" and len(parts) >= 5:
        try:
            uid = int(parts[2])
            service = parts[3]
            date_str = parts[4].split()[0]
            plan = parts[4].split()[1] if len(parts[4].split()) > 1 else ""
            if date_str.startswith("+"):
                expires = _dt.datetime.now() + _dt.timedelta(days=int(date_str[1:]))
            elif "." in date_str:
                expires = _dt.datetime.strptime(date_str, "%d.%m.%Y")
            else:
                expires = _dt.datetime.strptime(date_str, "%Y-%m-%d")
            async with pool.acquire() as conn:
                sub_id = await conn.fetchval("""
                    INSERT INTO user_subscriptions
                    (user_id, service_key, service_name, plan_name, expires_at, created_by)
                    VALUES ($1,$2,$3,$4,$5,$6) RETURNING id
                """, uid, service.lower(), service, plan, expires, ADMIN_ID)
            exp_str = expires.strftime("%d.%m.%Y")
            await message.answer(
                f"✅ Подписка добавлена!\n\n"
                f"👤 ID: {uid}\n📦 {service} {plan}\n📅 До: <b>{exp_str}</b>\n🆔 Sub ID: {sub_id}",
                parse_mode="HTML"
            )
            try:
                await bot.send_message(uid,
                    f"🎉 <b>Подписка активирована!</b>\n\n"
                    f"📦 <b>{service} {plan}</b>\n📅 Действует до: <b>{exp_str}</b>\n\n"
                    f"👤 Мой профиль → видно все подписки",
                    parse_mode="HTML")
            except Exception:
                pass
        except (ValueError, IndexError) as e:
            await message.answer(f"❌ Ошибка: {e}")
    elif action == "list" and len(parts) >= 3:
        uid = int(parts[2])
        async with pool.acquire() as conn:
            subs = await conn.fetch(
                "SELECT id, service_name, plan_name, expires_at, is_active FROM user_subscriptions WHERE user_id=$1 ORDER BY expires_at DESC LIMIT 10", uid)
        if not subs:
            await message.answer(f"📋 У {uid} нет подписок")
            return
        lines = []
        now = _dt.datetime.now()
        for s in subs:
            st = "✅" if s["is_active"] and s["expires_at"] > now else "❌"
            exp = s["expires_at"].strftime("%d.%m.%Y")
            lines.append(f"{st} [{s['id']}] {s['service_name']} {s['plan_name']} - до {exp}")
        await message.answer("📋 <b>Подписки:</b>\n\n" + "\n".join(lines), parse_mode="HTML")
    elif action == "del" and len(parts) >= 3:
        async with pool.acquire() as conn:
            await conn.execute("UPDATE user_subscriptions SET is_active=FALSE WHERE id=$1", int(parts[2]))
        await message.answer(f"✅ Подписка #{parts[2]} деактивирована")
    else:
        await message.answer("❌ Неверный формат. /sub - справка")


@dp.message(F.text.startswith("/credit"), StateFilter("*"))
async def cmd_credit(message: Message):
    """Быстрое зачисление кредитов. /credit ORDER_ID или /credit UID СУММА.

    Примеры:
    /credit 603210532_1777318959
    /credit 603210532 150
    """
    if message.from_user.id != ADMIN_ID:
        return

    parts = (message.text or "").split()
    if len(parts) < 2:
        await message.answer(
            "<b>Использование:</b>\n\n"
            "<code>/credit ORDER_ID</code> - зачислить pending заказ из БД\n"
            "<code>/credit UID СУММА</code> - добавить кредиты юзеру\n\n"
            "<b>Примеры:</b>\n"
            "<code>/credit 603210532_1777318959</code>\n"
            "<code>/credit 603210532 150</code>",
            parse_mode="HTML"
        )
        return

    arg1 = parts[1]

    # Вариант 1: /credit ORDER_ID - зачислить по существующему заказу
    if "_" in arg1:
        order_id = arg1
        try:
            db_order = await fk_get_order(order_id)
            if not db_order:
                await message.answer(f"❌ Заказ <code>{order_id}</code> не найден в БД", parse_mode="HTML")
                return

            if db_order["status"] == "paid":
                await message.answer(
                    f"⚠️ Заказ уже зачислен ранее.\n"
                    f"Юзер: <code>{db_order['user_id']}</code>\n"
                    f"Кредитов: {db_order['credits']}",
                    parse_mode="HTML"
                )
                return

            # Зачисляем через общую функцию (с уведомлением юзера и атомарной защитой)
            payment = {
                "user_id": db_order["user_id"],
                "credits": db_order["credits"],
                "amount":  db_order["amount_rub"],
                "promo_code": db_order.get("promo_code"),
            }
            success = await fk_credit_paid_order(order_id, payment, source="manual_admin")
            if success:
                await message.answer(
                    f"✅ <b>Зачислено!</b>\n\n"
                    f"Юзер: <code>{db_order['user_id']}</code>\n"
                    f"Кредитов: {db_order['credits']}\n"
                    f"Сумма: {db_order['amount_rub']}₽",
                    parse_mode="HTML"
                )
            else:
                await message.answer("⚠️ Заказ уже был зачислен (race condition)")
        except Exception as e:
            await message.answer(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")
        return

    # Вариант 2: /credit UID СУММА - добавить кредиты напрямую
    if len(parts) < 3:
        await message.answer("❌ Укажи сумму: <code>/credit UID СУММА</code>", parse_mode="HTML")
        return

    try:
        target_uid = int(arg1)
        credits_to_add = int(parts[2])
    except ValueError:
        await message.answer("❌ UID и СУММА должны быть числами", parse_mode="HTML")
        return

    try:
        await add_credits_batch(target_uid, credits_to_add, source="admin_manual", days_valid=30)
        new_balance = await get_credits(target_uid)

        # Уведомляем юзера
        try:
            await bot.send_message(
                target_uid,
                f"🎉 <b>Зачислены кредиты!</b>\n\n"
                f"━━━━━━━━━━━━━━━━━━━\n"
                f"💎 <b>Зачислено:</b> +{credits_to_add} кредитов\n"
                f"💵 <b>Баланс:</b> <b>{new_balance} кр</b>\n"
                f"━━━━━━━━━━━━━━━━━━━\n\n"
                f"<i>⏳ Кредиты действуют 30 дней</i>\n\n"
                f"Можешь начинать генерацию! 🚀",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🖼️ Создать фото", callback_data="menu_image"),
                     InlineKeyboardButton(text="🎬 Создать видео", callback_data="menu_video")],
                ])
            )
        except Exception as e:
            logging.error(f"/credit notify user error: {e}")

        await message.answer(
            f"✅ <b>Зачислено!</b>\n\n"
            f"Юзер: <code>{target_uid}</code>\n"
            f"Кредитов: +{credits_to_add}\n"
            f"Новый баланс: <b>{new_balance}</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        await message.answer(f"❌ Ошибка: <code>{e}</code>", parse_mode="HTML")


@dp.callback_query(F.data == "noop")
async def noop_handler(cb: CallbackQuery):
    await cb.answer()


@dp.callback_query(F.data == "back_main")
async def back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    credits = await get_credits(cb.from_user.id)
    _bm_text = f"👋 {cb.from_user.first_name}, баланс: <b>{credits} кредитов</b>\n\nВыбери действие 👇"
    try:
        await cb.message.edit_text(_bm_text, reply_markup=kb_main(), parse_mode="HTML")
    except Exception:
        # фото/видео-сообщение — edit_text невозможен, отправляем новым
        try:
            await cb.message.delete()
        except Exception:
            pass
        await cb.message.answer(_bm_text, reply_markup=kb_main(), parse_mode="HTML")
    await cb.answer()

# ══════════════════════════════════════════════════════════
#  БАЛАНС / ОПЛАТА
# ══════════════════════════════════════════════════════════

@dp.message(F.text == "/ref", StateFilter("*"))
async def cmd_ref(message: Message):
    """Команда /ref - показать реферальную ссылку."""
    uid = message.from_user.id
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_refs = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1", uid) or 0
        paid_refs  = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1 AND ref_bonus_paid=TRUE", uid) or 0
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{uid}"
    user_coins = await get_coins(uid)
    earned = paid_refs * REF_BONUS
    await message.answer(
        f"\U0001f91d <b>Пригласить друга</b>\n\n"
        f"<b>За каждого друга - +{REF_BONUS} кредитов тебе и ему!</b>\n\n"
        f"❓ <b>Как работает:</b>\n"
        f"1\u20e3 Поделись своей ссылкой\n"
        f"2\u20e3 Друг регистрируется \u2192 он получает <b>+{REF_BONUS} кредитов</b>\n"
        f"3\u20e3 Друг делает первую покупку \u2192 ты получаешь <b>+{REF_BONUS} кредитов</b>\n\n"
        f"\U0001f4ca <b>Статистика:</b>\n"
        f"\U0001f465 Приглашено: <b>{total_refs}</b>\n"
        f"\U0001f4b0 Купили: <b>{paid_refs}</b>\n"
        f"\U0001f381 Заработано: <b>{earned} кредитов</b>\n\n"
        f"\U0001f517 <b>Твоя ссылка:</b>\n"
        f"<code>{ref_link}</code>",
        parse_mode="HTML"
    )



# ══════════════════════════════════════════════════════════
#  МАГАЗИН ПОДПИСОК
# ══════════════════════════════════════════════════════════

# ── Кастомные Telegram эмодзи по ключу сервиса ───────────────────────────────
# Добавляй новые по мере нахождения через @JsonDumpBot
@dp.callback_query(F.data == "new_main")
async def new_main_from_photo(cb: CallbackQuery, state: FSMContext):
    """Главное меню новым сообщением (для фото/видео где нельзя edit_text)."""
    await state.clear()
    credits = await get_credits(cb.from_user.id)
    await cb.message.answer(
        f"👋 Баланс: <b>{credits} кредитов</b>\n\nВыбери действие 👇",
        reply_markup=kb_main(), parse_mode="HTML"
    )
    await cb.answer()

# ══════════════════════════════════════════════════════════
#  ГЕНЕРАЦИЯ ВИДЕО
# ══════════════════════════════════════════════════════════

@dp.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated):
    if str(event.chat.id) != str(CHANNEL_ID):
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return
    await ensure_user(user.id)
    try:
        await bot.send_message(
            chat_id=user.id,
            text=f"👋 Привет! Рад приветствовать тебя в канале!\n\n"
                 f"Я - AI-ассистент Александра. Помогу:\n"
                 f"🎨 Создать изображение (Imagen 4)\n"
                 f"🎥 Создать видео (Veo 3.1)\n"
                 f"💬 Разобраться в нейросетях\n"
                 f"💳 Оформить подписку - оплата в рублях\n\n"
                 f"🎁 Тебе начислено <b>{FREE_CREDITS} бесплатных кредитов</b>!\n\n"
                 f"Напиши /start чтобы начать 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_eib("Главное меню", "back_main")],
                [InlineKeyboardButton(text="💌 Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}")],
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Не удалось отправить приветствие {user.id}: {e}")


# ══════════════════════════════════════════════════════════
#  ФУНКЦИЯ CLAUDE С ВЕБ-ПОИСКОМ
# ══════════════════════════════════════════════════════════

@dp.message(F.text.contains("Главное меню"), StateFilter("*"))
async def reply_main_menu(message: Message, state: FSMContext):
    await state.clear()
    credits = await get_credits(message.from_user.id)
    await message.answer(
        f"👋 {message.from_user.first_name}, баланс: <b>{credits} кредитов</b>\n\nВыбери действие 👇",
        reply_markup=kb_main(), parse_mode="HTML"
    )


@dp.message(F.text.contains("Создать фото"), StateFilter("*"))
async def reply_create_photo(message: Message, state: FSMContext):
    await state.clear()
    cr = await get_credits(message.from_user.id)
    await message.answer(
        f"📷 <b>Создать изображение</b>\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n\n"
        f"<b>Выбери модель:</b>\n\n"
        f'{tg_emoji_ui("iband_gptimg", "🤖")} <b>GPT Image</b> - OpenAI, #1 в Image Arena\n'
        f'{tg_emoji_ui("iband_imagen", "🌟")} <b>Imagen 4</b> - флагман Google, от 7 кр\n'
        f'{tg_emoji_ui("iband_nano", "🍌")} <b>Nano Banana</b> - Gemini, 4K, от 10 кр\n'
        f'{tg_emoji_ui("iband_flux", "🎭")} <b>Flux</b> - фотореализм, от 12 кр\n'
        f'{tg_emoji_ui("iband_ideogram", "✒️")} <b>Ideogram</b> - идеальный текст, от 14 кр\n'
        f'{tg_emoji_ui("iband_grok", "⚡")} <b>Grok Imagine</b> - xAI, высокий реализм',
        reply_markup=kb_image_brands(), parse_mode="HTML"
    )


@dp.message(F.text.contains("Создать видео"), StateFilter("*"))
async def reply_create_video(message: Message, state: FSMContext):
    await state.clear()
    cr = await get_credits(message.from_user.id)
    await message.answer(
        f"🎬 <b>Создать видео</b>\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n\n"
        f"<b>Выбери модель:</b>\n\n"
        f'{tg_emoji_ui("vband_veo", "🎥")} <b>Veo</b> - до 4K + аудио, от 239 кр\n'
        f'{tg_emoji_ui("vband_kling", "🎞")} <b>Kling</b> - плавная физика + аудио, от 109 кр\n'
        f'{tg_emoji_ui("vband_seedance", "🎬")} <b>Seedance</b> - нативное аудио, от 99 кр\n'
        f'{tg_emoji_ui("vband_wan", "🌊")} <b>Wan</b> - топ open-source, от 80 кр\n'
        f'{tg_emoji_ui("vband_grok", "⚡")} <b>Grok</b> - xAI, нативное аудио, от 99 кр\n\n'
        f"⏱ <i>Время генерации: 1–10 минут</i>",
        reply_markup=kb_video_brands(), parse_mode="HTML"
    )


@dp.callback_query(F.data == "menu_profile")
async def menu_profile_cb(cb: CallbackQuery):
    """Открыть профиль через inline-кнопку в главном меню."""
    await cb.answer()
    await _show_profile(cb.message, cb.from_user)


@dp.message(F.text.contains("Мой профиль"), StateFilter("*"))
async def reply_profile(message: Message):
    await _show_profile(message, message.from_user)


@dp.callback_query(F.data == "noop")
async def _noop(cb: CallbackQuery):
    await cb.answer()


# ─── Расход по пользователю ───────────────────────────────

@dp.callback_query(F.data == "fav_save")
async def fav_save(cb: CallbackQuery):
    """Сохраняет последний результат генерации в избранное."""
    uid = cb.from_user.id
    msg = cb.message

    # Ищем фото или видео в сообщении
    file_id = None
    media_type = "photo"
    if msg.photo:
        file_id = msg.photo[-1].file_id
        media_type = "photo"
    elif msg.video:
        file_id = msg.video.file_id
        media_type = "video"
    elif msg.document:
        file_id = msg.document.file_id
        media_type = "document"

    if not file_id:
        await cb.answer("Нечего сохранять", show_alert=True)
        return

    # Извлекаем промт и модель из caption
    caption = msg.caption or ""
    prompt_match = None
    model_match = None
    if caption:
        import re
        model_match = re.search(r'(GPT Image|Imagen|Nano Banana|Flux|Ideogram|Grok|Veo|Kling|Seedance|Wan)', caption)

    pool = await get_pool()
    async with pool.acquire() as conn:
        # Проверяем дубликат
        exists = await conn.fetchval(
            "SELECT 1 FROM favorites WHERE user_id=$1 AND file_id=$2", uid, file_id
        )
        if exists:
            await cb.answer("Уже в избранном ❤️", show_alert=True)
            return
        await conn.execute(
            "INSERT INTO favorites (user_id, file_id, media_type, prompt, model) VALUES ($1, $2, $3, $4, $5)",
            uid, file_id, media_type, caption[:200] if caption else None,
            model_match.group(1) if model_match else None
        )
    await cb.answer("❤️ Сохранено в избранное!")


@dp.callback_query(F.data == "profile_history")
async def profile_history(cb: CallbackQuery):
    """История покупок пользователя."""
    uid = cb.from_user.id
    await cb.answer()
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT pack, amount_rub, credits, paid_at
                   FROM fk_orders
                   WHERE user_id=$1 AND status='paid'
                   ORDER BY paid_at DESC NULLS LAST
                   LIMIT 20""",
                uid
            )
        if not rows:
            await cb.message.answer("🧾 <b>История покупок</b>\n\nПокупок пока нет.", parse_mode="HTML")
            return

        PACK_NAMES = {
            "p15":   "🎯 Пробный (150 кр)",
            "p25":   "🥉 Начальный (250 кр)",
            "p50":   "🥈 Старт (500 кр)",
            "p150":  "🏅 Базовый (1500 кр)",
            "p500":  "🥇 Про (5000 кр)",
            "p1200": "💎 Бизнес (12000 кр)",
        }
        lines = []
        for p in rows:
            pack = p["pack"] or ""
            dt = p["paid_at"]
            date_str = dt.strftime("%d.%m.%Y") if dt else "—"
            amount = p["amount_rub"] or 0
            if pack.startswith("shop:"):
                parts = pack.split(":")
                svc_key = parts[1] if len(parts) > 1 else pack
                svc = SHOP_CATALOG.get(svc_key, {})
                label = f"{svc.get('emoji','🛍')} {svc.get('name', svc_key)}"
            else:
                credits_val = p["credits"] or 0
                label = PACK_NAMES.get(pack, f"+{credits_val} кр")
            lines.append(f"• {date_str} — {label} — <b>{amount}₽</b>")

        total = sum(r["amount_rub"] or 0 for r in rows)
        text = (
            f"🧾 <b>История покупок</b>\n"
            f"<i>Последние {len(rows)} операций · итого {total}₽</i>\n\n"
            + "\n".join(lines)
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👤 Профиль", callback_data="noop"),
        ]])
        await cb.message.answer(strip_surrogates(text), parse_mode="HTML")
    except Exception as e:
        logging.error(f"profile_history error uid={uid}: {e}")
        await cb.message.answer("⚠️ Не удалось загрузить историю покупок.")


@dp.callback_query(F.data == "menu_favorites")
async def menu_favorites(cb: CallbackQuery):
    uid = cb.from_user.id
    pool = await get_pool()
    async with pool.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM favorites WHERE user_id=$1", uid) or 0
        items = await conn.fetch(
            "SELECT file_id, media_type, prompt, model, created_at FROM favorites "
            "WHERE user_id=$1 ORDER BY created_at DESC",
            uid
        )

    if count == 0:
        try:
            await cb.message.edit_text(
                "❤️ <b>Избранное</b>\n\nПока пусто. Нажми ❤️ после генерации чтобы сохранить.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_eib("Главное меню", "back_main")],
                ])
            )
        except Exception:
            pass
        await cb.answer()
        return

    await cb.answer()
    from aiogram.types import InputMediaPhoto, InputMediaVideo, InputMediaDocument

    def _fav_caption(it):
        return f"❤️ {it['model'] or 'Генерация'} · {it['created_at'].strftime('%d.%m.%Y')}"

    def _fav_media(it):
        mt = it["media_type"]
        if mt == "photo":
            return InputMediaPhoto(media=it["file_id"], caption=_fav_caption(it))
        if mt == "video":
            return InputMediaVideo(media=it["file_id"], caption=_fav_caption(it))
        return InputMediaDocument(media=it["file_id"], caption=_fav_caption(it))

    # Документы нельзя смешивать с фото/видео в одном альбоме — отправляем раздельно
    media_items = [it for it in items if it["media_type"] in ("photo", "video")]
    doc_items   = [it for it in items if it["media_type"] not in ("photo", "video")]

    async def _send_albums(group):
        # Альбомами максимум по 10; одиночный элемент шлём обычным сообщением
        for i in range(0, len(group), 10):
            chunk = group[i:i + 10]
            try:
                if len(chunk) == 1:
                    it = chunk[0]
                    cap = _fav_caption(it)
                    if it["media_type"] == "photo":
                        await cb.message.answer_photo(it["file_id"], caption=cap)
                    elif it["media_type"] == "video":
                        await cb.message.answer_video(it["file_id"], caption=cap)
                    else:
                        await cb.message.answer_document(it["file_id"], caption=cap)
                else:
                    await cb.message.answer_media_group([_fav_media(it) for it in chunk])
            except Exception as e:
                logging.warning(f"fav album send failed: {e}")

    await _send_albums(media_items)
    await _send_albums(doc_items)

    await cb.message.answer(
        f"❤️ <b>Избранное</b> - {count} сохранений",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [_eib("Главное меню", "back_main")],
        ])
    )


@dp.message(F.text == "/help", StateFilter("*"))
async def cmd_help(message: Message):
    await message.answer(
        "\U0001f6e1\ufe0f <b>Поддержка Neirosetka</b>\n\n"
        "Если что-то пошло не так - мы всегда рядом!\n\n"
        "1. Укажите ваш Telegram ID: <code>{}</code>\n"
        "2. Опишите проблему подробно\n"
        "3. Добавьте скриншот, если поможет разобраться быстрее\n\n"
        "\U0001f4ac Пишите сюда: @{}\n"
        "\u23f3 Обычно отвечаем в течение 1–6 часов".format(
            message.from_user.id, PERSONAL_USERNAME
        ),
        parse_mode="HTML"
    )


@dp.message(F.text == "/privacy", StateFilter("*"))
async def cmd_privacy(message: Message):
    await message.answer(
        "\U0001f512 <b>Политика конфиденциальности @Neirosetkaa_bot</b>\n\n"
        "<b>1. Общие положения</b>\n"
        "Использование бота означает согласие с данной Политикой и условиями обработки персональных данных.\n\n"
        "<b>2. Какие данные собираем</b>\n"
        "• Имя пользователя в Telegram\n"
        "• Username в Telegram\n"
        "• Telegram ID (user_id)\n\n"
        "Данные используются исключительно для:\n"
        "- обработки платежей и начисления кредитов\n"
        "- технической поддержки\n"
        "- уведомлений о работе сервиса\n\n"
        "<b>3. Хранение и защита</b>\n"
        "Данные хранятся на защищённых серверах. Доступ - только у администрации бота. "
        "Передача третьим лицам без согласия пользователя не осуществляется, "
        "за исключением случаев, предусмотренных законодательством.\n\n"
        "<b>4. Права пользователя</b>\n"
        "Вы вправе в любой момент:\n"
        "• запросить доступ к своим данным\n"
        "• потребовать исправления или удаления данных\n"
        "• отозвать согласие на обработку\n\n"
        "Для этого напишите: @{}\n\n"
        "<b>5. Изменения</b>\n"
        "Политика может обновляться. Актуальная версия всегда доступна по команде /privacy.".format(
            PERSONAL_USERNAME
        ),
        parse_mode="HTML"
    )


@dp.message(F.text == "/publicoffer", StateFilter("*"))
async def cmd_publicoffer(message: Message):
    await message.answer(
        "\U0001f4cb <b>Публичная оферта @Neirosetkaa_bot</b>\n"
        "<i>Дата публикации: 13.04.2026</i>\n\n"
        "Используя бот и совершая оплату, вы соглашаетесь с условиями настоящей оферты. "
        "Акцептом считается первая успешная оплата.\n\n"
        "<b>1. Предмет договора</b>\n"
        "Исполнитель предоставляет доступ к сервису генерации изображений и видео с помощью AI-моделей (Imagen, Veo, Grok, GPT Image, Seedance, Kling, Wan и других). "
        "Заказчик обязуется принять и оплатить услуги.\n\n"
        "<b>2. Права и обязанности</b>\n"
        "Заказчик обязуется:\n"
        "• предоставлять достоверные данные\n"
        "• своевременно оплачивать услуги\n"
        "• не использовать сервис для незаконных целей\n\n"
        "Исполнитель обязуется:\n"
        "• обеспечивать работу сервиса\n"
        "• информировать о сбоях и изменениях\n"
        "• рассматривать претензии в течение 3 рабочих дней\n\n"
        "<b>3. Порядок оказания услуг</b>\n"
        "• Услуга считается оказанной в момент успешной генерации контента\n"
        "• Кредиты списываются автоматически при генерации\n"
        "• Претензии принимаются в течение 3 дней после оплаты\n"
        "• Исполнитель не несёт ответственности за сбои в работе API Google\n\n"
        "<b>4. Стоимость и оплата</b>\n"
        "• Стоимость кредитов указана в боте перед оплатой\n"
        "• Оплата через СБП, карту РФ или Telegram Stars\n"
        "• Обязательства считаются исполненными при поступлении средств\n\n"
        "<b>5. Ответственность</b>\n"
        "Исполнитель не отвечает за форс-мажор: сбои связи, действия третьих лиц, изменения в API провайдеров.\n\n"
        "<b>6. Контакты</b>\n"
        "\U0001f4ac Поддержка: @{}\n"
        "\U0001f4e7 По вопросам оферты: @{}\n\n"
        "<i>Совершая оплату, вы подтверждаете согласие с данной офертой.</i>".format(
            PERSONAL_USERNAME, PERSONAL_USERNAME
        ),
        parse_mode="HTML"
    )



# ══════════════════════════════════════════════════════════
#  АНИМАЦИЯ ФОТО (image-to-video через Veo 3.1)
# ══════════════════════════════════════════════════════════

@dp.message(F.text == "/myip", StateFilter("*"))
async def cmd_myip(message: Message, state: FSMContext):
    # Проверяем по ID или по username (на случай если ADMIN_ID не задан)
    uid      = message.from_user.id
    uname    = (message.from_user.username or "").lower()
    is_me    = (ADMIN_ID and uid == ADMIN_ID) or uname == ADMIN_USERNAME.lower()
    if not is_me:
        return
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get("https://api.ipify.org", timeout=aiohttp.ClientTimeout(total=5)) as r:
                ip = await r.text()
        await message.answer(f"🌐 Outbound IP сервера:\n<code>{ip.strip()}</code>\n\nДобавь этот IP в whitelist NS Gifts.", parse_mode="HTML")
    except Exception as e:
        await message.answer(f"❌ Не удалось получить IP: {e}")


# ─── /emoji — захватить символ кастомного эмодзи для вставки в кнопки ────────
@dp.message(F.text.startswith("/emoji"), StateFilter("*"))
async def cmd_emoji_capture(message: Message, state: FSMContext):
    """Отправь /emoji и следом (или в одном сообщении) кастомный эмодзи.
    Бот ответит точным символом и Unicode-repr для вставки в button text."""
    uid   = message.from_user.id
    uname = (message.from_user.username or "").lower()
    is_me = (ADMIN_ID and uid == ADMIN_ID) or uname == ADMIN_USERNAME.lower()
    if not is_me:
        return

    # Берём текст после команды
    raw = message.text[len("/emoji"):].strip()
    if not raw:
        await message.answer(
            "Отправь команду вместе с эмодзи, например:\n"
            "<code>/emoji 🐱</code>\n\n"
            "Или пришли следующим сообщением — ответь реплаем на него командой /emoji_reply",
            parse_mode="HTML"
        )
        return

    lines = []
    for i, ch in enumerate(raw):
        cp  = ord(ch)
        esc = f"\\U{cp:08X}" if cp > 0xFFFF else f"\\u{cp:04X}"
        lines.append(f"[{i}] <code>{ch}</code>  U+{cp:04X}  <code>{esc}</code>")

    # Также показываем repr всей строки — для прямой вставки в Python
    full_repr = repr(raw)
    await message.answer(
        f"🔡 <b>Символы ({len(raw)} шт.):</b>\n" + "\n".join(lines) +
        f"\n\n📋 <b>Python repr (вставь в код):</b>\n<code>{full_repr}</code>",
        parse_mode="HTML"
    )


# ─── /shopkeys — показать реальные ключи SHOP_CATALOG и их emoji_id ──────────
@dp.message(F.text == "/shopkeys", StateFilter("*"))
async def cmd_shopkeys(message: Message, state: FSMContext):
    uid   = message.from_user.id
    uname = (message.from_user.username or "").lower()
    is_me = (ADMIN_ID and uid == ADMIN_ID) or uname == ADMIN_USERNAME.lower()
    if not is_me:
        return
    lines = []
    for key, s in SHOP_CATALOG.items():
        eid = s.get("emoji_id", "")
        eid_display = f"✅ {eid}" if eid else "❌ нет"
        lines.append(f"<code>{key}</code> — {s.get('name','')} | emoji_id: {eid_display}")
    await message.answer(
        f"🗝 <b>SHOP_CATALOG ключи ({len(SHOP_CATALOG)} шт.):</b>\n\n" + "\n".join(lines),
        parse_mode="HTML"
    )


# ──────────────────────────────────────────────────────────────────────────────
#  Хелперы настроек NS Gifts
# ──────────────────────────────────────────────────────────────────────────────

