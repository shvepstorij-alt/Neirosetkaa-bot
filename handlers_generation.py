# -*- coding: utf-8 -*-
# Auto-split module "handlers_generation" — part of Neirosetkaa-bot (refactored from bot.py).
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
    ANIM_MODELS, DISABLED_MODELS, EDIT_MODELS, EVOLINK_API_KEY, FAL_API_KEY, IMAGE_BRAND_MODELS,
    IMAGE_BRAND_TITLES, IMAGE_MODELS, MOTION_MODEL_ID, MOTION_PRICES, UI_EMOJI_IDS, UPSCALE_CREDIT_COST,
    VIDEO_BRAND_MODELS, VIDEO_BRAND_TITLES, VIDEO_MODELS, _anim_history, _motion_history, _photo_history,
    _record_generation, _veo_semaphore, _video_history, bot, claude_client, dp,
    friendly_error, model_title_n, user_orig_images, validate_gen_prompt,
)
from states import (
    AnimState, EditState, ImgState, ImproveState, MotionState, UpscaleState,
    VidState,
)
from db import (
    add_credits, deduct, get_credits, log_event, log_gen,
)
from keyboards import (
    _eib, kb_after, kb_aspect_image, kb_aspect_video, kb_back, kb_cancel,
    kb_confirm, kb_error_with_alt, kb_image_brands, kb_image_models_for_brand, kb_main, kb_vid_duration,
    kb_video_brands, kb_video_models_for_brand, tg_emoji_ui,
)
from generation_api import (
    _tg_file_public_url, _with_retry, api_animate_image, api_edit_image, api_generate_fal_image, api_generate_image,
    api_generate_video, api_kling_motion_control, upload_large_file,
)
from common import (
    _check_can_generate, check_expiring_credits, mark_generation_active, notify_admin_error, safe_send_media, unmark_generation_active,
)

@dp.callback_query(F.data == "menu_image")
async def menu_image(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cr = await get_credits(cb.from_user.id)
    text = (
        f"📷 <b>Создать изображение</b>\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n\n"
        f"<b>Выбери модель:</b>\n\n"
        f'{tg_emoji_ui("iband_gptimg", "🤖")} <b>GPT Image</b> - OpenAI, #1 в Image Arena\n'
        f'{tg_emoji_ui("iband_imagen", "🌟")} <b>Imagen</b> - флагман Google\n'
        f'{tg_emoji_ui("iband_nano", "🍌")} <b>Nano Banana</b> - Gemini, быстро и качественно\n'
        f'{tg_emoji_ui("iband_flux", "🎭")} <b>Flux</b> - художественный фотореализм\n'
        f'{tg_emoji_ui("iband_ideogram", "✒️")} <b>Ideogram</b> - идеальный текст в картинке\n'
        f'{tg_emoji_ui("iband_grok", "⚡")} <b>Grok Imagine</b> - xAI, высокий реализм'
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb_image_brands(), parse_mode="HTML")
    except Exception:
        # Не получилось отредактировать (напр. это сообщение с фото)
        await cb.message.answer(text, reply_markup=kb_image_brands(), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("iband:"))
async def choose_img_brand(cb: CallbackQuery, state: FSMContext):
    """Открыть подменю моделей выбранного бренда."""
    await state.clear()
    brand = cb.data.split(":")[1]
    if brand not in IMAGE_BRAND_MODELS:
        await cb.answer()
        return
    cr = await get_credits(cb.from_user.id)
    _brand_name = IMAGE_BRAND_TITLES.get(brand, brand)
    _brand_eid_key = f"iband_{brand}"
    _iband_title_fb = {"gptimg": "🤖", "imagen": "🌟", "nano": "🍌", "flux": "🎭", "ideogram": "✒️", "grok": "⚡"}.get(brand, "")
    title = f"{tg_emoji_ui(_brand_eid_key, _iband_title_fb)} <b>{_brand_name}</b>" if UI_EMOJI_IDS.get(_brand_eid_key) else f"<b>{_brand_name}</b>"

    # Список моделей бренда с описанием
    import re as _re_img
    _iband_eid = UI_EMOJI_IDS.get(_brand_eid_key, "")
    _iband_fallbacks = {
        "iband_gptimg": "🤖", "iband_imagen": "🌟", "iband_nano": "🍌",
        "iband_flux": "🎭", "iband_ideogram": "✒️", "iband_grok": "⚡",
    }
    _iband_fb = _iband_fallbacks.get(_brand_eid_key, "")
    lines = []
    for key in IMAGE_BRAND_MODELS[brand]:
        if key in IMAGE_MODELS:
            m = IMAGE_MODELS[key]
            icon = "🔹" if cr >= m['credits'] else "🔸"
            clean = _re_img.sub(r'^[^\w\s]+\s*', '', m['name']).strip()
            model_ename = f'<tg-emoji emoji-id="{_iband_eid}">{_iband_fb}</tg-emoji> ' if _iband_eid else ""
            lines.append(f"{icon} {model_ename}<b>{clean}</b> - {m['credits']} кр\n   <i>{m['desc']}</i>")

    text = (
        f"{title}\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n\n"
        + "\n\n".join(lines)
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb_image_models_for_brand(brand), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_image_models_for_brand(brand), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "back_img_brands")
async def back_to_img_brands(cb: CallbackQuery, state: FSMContext):
    """Возврат к выбору бренда из подменю."""
    await menu_image(cb, state)


@dp.callback_query(F.data.startswith("imodel:"))
async def choose_img_model(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    m = IMAGE_MODELS[key]
    cr = await get_credits(cb.from_user.id)
    if cr < m["credits"]:
        await cb.answer(f"💸 Нужно {m['credits']} кредитов, у тебя {cr}", show_alert=True)
        return
    await state.update_data(model_key=key)
    await state.set_state(ImgState.waiting_aspect)
    await cb.message.edit_text(
        f"{model_title_n(m['name'])} ✅\n\n"
        f"💳 Спишется: <b>{m['credits']} кредитов</b>\n\n"
        f"📐 <b>Выбери формат изображения:</b>",
        reply_markup=kb_aspect_image(key), parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("alt_img:"))
async def choose_alt_img_model(cb: CallbackQuery, state: FSMContext):
    """Клиент выбрал альтернативную фото-модель после ошибки 503."""
    key = cb.data.split(":")[1]
    if key not in IMAGE_MODELS:
        await cb.answer()
        return
    # Переиспользуем основной flow выбора модели
    cb.data = f"imodel:{key}"
    await choose_img_model(cb, state)


@dp.callback_query(F.data.startswith("retry_img:"))
async def retry_img_model(cb: CallbackQuery, state: FSMContext):
    """Клиент хочет попробовать ту же модель ещё раз после ошибки."""
    key = cb.data.split(":")[1]
    if key not in IMAGE_MODELS:
        await cb.answer()
        return
    cb.data = f"imodel:{key}"
    await choose_img_model(cb, state)


@dp.callback_query(F.data.startswith("iaspect:"))
async def choose_img_aspect(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    key = parts[1]
    ratio = ":".join(parts[2:])  # "9:16", "16:9" etc
    m = IMAGE_MODELS[key]
    labels = {"1:1": "Квадрат 1:1", "16:9": "Широкий 16:9",
              "9:16": "Сторис 9:16", "4:3": "Фото 4:3", "3:4": "Портрет 3:4"}
    await state.update_data(model_key=key, aspect_ratio=ratio)
    await state.set_state(ImgState.waiting_prompt)
    await cb.message.edit_text(
        f"{model_title_n(m['name'])} | 📐 {labels.get(ratio, ratio)}\n\n"
        f"💳 Спишется: <b>{m['credits']} кредитов</b>\n\n"
        f"💡 <b>Введи промт:</b>\n\n"
        f"<i>Пример: A futuristic city at night, neon lights, cyberpunk, 4k</i>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )
    await cb.answer()


@dp.message(ImgState.waiting_aspect)
async def img_aspect_text(message: Message):
    """Если написали текст вместо выбора формата."""
    await message.answer("👆 Выбери формат кнопкой выше")


async def _extract_prompt(message) -> str:
    """Текст промта: message.text, либо caption, либо содержимое .txt-файла
    (очень длинные промты >4096 симв. Telegram отправляет файлом)."""
    t = (message.text or message.caption or "").strip()
    if t:
        return t
    doc = getattr(message, "document", None)
    if doc and (doc.file_size or 0) < 30000 and (
        (doc.mime_type or "").startswith("text") or (doc.file_name or "").lower().endswith(".txt")):
        try:
            f = await bot.get_file(doc.file_id)
            buf = await bot.download_file(f.file_path)
            return buf.read().decode("utf-8", errors="ignore").strip()
        except Exception:
            return ""
    return ""


@dp.message(ImgState.waiting_prompt, F.photo)
async def img_prompt_photo(message: Message, state: FSMContext):
    """Клиент прислал ФОТО в текстовой генерации — значит хочет работу ПО ФОТО
    (вставить объект, сменить фон/стиль). Это режим редактирования (Nano Banana/Gemini).
    Плавно переводим туда с уже подставленным фото."""
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    fb = await bot.download_file(file.file_path)
    img_data = fb.read()
    await state.clear()
    _mk = "edit_gemini" if "edit_gemini" in EDIT_MODELS else next(iter(EDIT_MODELS))
    await state.update_data(photo_bytes=list(img_data), edit_model_key=_mk)
    await state.set_state(EditState.waiting_prompt)
    await message.answer(
        "📷 <b>Вижу фото!</b>\n\n"
        "Кнопка «📷 Изображение» рисует картинку по <b>тексту</b> и твоё фото не использует.\n"
        "Чтобы работать <b>по твоему фото</b> (вставить объект, сменить фон или стиль) — это "
        "<b>🖌 Редактирование</b> (Nano Banana). Фото я уже подставил 👇\n\n"
        "✏️ Напиши, что сделать с фото:",
        parse_mode="HTML", reply_markup=kb_cancel()
    )


@dp.message(ImgState.waiting_prompt)
async def img_prompt(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("model_key")
    if not key or key not in IMAGE_MODELS:
        await state.clear()
        await message.answer("⚠️ Сессия сброшена. Начни заново: нажми 📷 Изображение.")
        return
    m = IMAGE_MODELS[key]
    prompt = await _extract_prompt(message)

    # Валидация
    ok, err = validate_gen_prompt(prompt)
    if not ok:
        await message.answer(err)
        return

    await state.update_data(prompt=prompt)

    await message.answer(
        f"📝 <b>Проверь заказ:</b>\n\n"
        f"{model_title_n(m['name'])}\n"
        f"💳 <b>{m['credits']} кредитов</b>\n"
        f"⏱ {m['speed']}\n\n"
        f"📝 <i>{prompt}</i>",
        reply_markup=kb_confirm("img", key), parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("go:img:"))
async def go_image(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    m = IMAGE_MODELS[key]
    data = await state.get_data()
    prompt = data.get("prompt", "")
    uid = cb.from_user.id

    # Rate limit
    if not await _check_can_generate(cb, uid, kind="photo"):
        return

    ok = await deduct(uid, m["credits"])
    if not ok:
        await cb.answer("💸 Недостаточно кредитов!", show_alert=True)
        return

    await mark_generation_active(uid, "photo")
    await state.clear()
    wait = await cb.message.edit_text(
        f"⚙️ Генерирую...\n\n{model_title_n(m['name'])}\n<i>{prompt[:80]}</i>",
        parse_mode="HTML"
    )

    # Флаг чтобы избежать двойного возврата кредитов
    img_refunded = False

    async def img_refund_once(reason: str = ""):
        nonlocal img_refunded
        if img_refunded:
            logging.warning(f"img_refund_once SKIPPED uid={uid} reason={reason}")
            return
        img_refunded = True
        await add_credits(uid, m["credits"])
        logging.info(f"img_refund_once EXECUTED uid={uid} credits={m['credits']} reason={reason}")

    try:
        aspect = data.get("aspect_ratio", "1:1")

        # Callback для уведомления юзера между попытками retry (особенно важно при 503)
        async def notify_retry(attempt, delay, err):
            err_low = str(err).lower()
            is_overload = ("503" in str(err) or "unavailable" in err_low
                           or "high demand" in err_low)
            if is_overload:
                wait_msg = f"⏳ Модель перегружена, жду {int(delay)} сек и пробую ещё раз ({attempt}/3)..."
            else:
                wait_msg = f"⏳ Временный сбой, повтор через {int(delay)} сек ({attempt}/3)..."
            try:
                await wait.edit_text(
                    f"⚙️ Генерирую...\n\n{model_title_n(m['name'])}\n<i>{prompt[:80]}</i>\n\n{wait_msg}",
                    parse_mode="HTML"
                )
            except Exception:
                pass

        img_bytes = await _with_retry(
            lambda: api_generate_image(
                prompt, m["model_id"], aspect,
                m.get("api", "imagen"),
                quality=m.get("quality", "medium"),
            ),
            max_attempts=3, op_name=f"Imagen/Gemini {key}",
            on_retry=notify_retry
        )
        await log_gen(uid, "image", key, m["credits"])
        _record_generation(uid, _photo_history)
        await check_expiring_credits(uid)
        cr = await get_credits(uid)
        # Сохраняем оригинал в памяти для скачивания (с timestamp для автоочистки)
        user_orig_images[uid] = {"data": img_bytes, "ts": _time_module.time()}
        # Сначала отправляем оригинал как документ - с retry
        await safe_send_media(
            cb.message.answer_document,
            BufferedInputFile(img_bytes, "original.png"),
            caption="\U0001f4ce <b>Оригинал</b> - без сжатия, полное качество",
            parse_mode="HTML",
            op_name=f"img_document_{key}",
        )
        # Затем превью с кнопками - с retry
        await safe_send_media(
            cb.message.answer_photo,
            BufferedInputFile(img_bytes, "image.png"),
            caption=f"🎉 Готово! {model_title_n(m['name'])}\n💸 Списано {m['credits']} кредитов | Остаток: {cr} кредитов",
            parse_mode="HTML",
            reply_markup=kb_after("image", key),
            op_name=f"img_photo_{key}",
        )
        try:
            await wait.delete()
        except Exception:
            pass
    except Exception as e:
        await img_refund_once(f"exception:{type(e).__name__}")
        await notify_admin_error(f"Генерация фото uid={cb.from_user.id} model={key}", e)
        try:
            await cb.message.edit_text(
                f"⚠️ {friendly_error(e)}\n\n💳 Кредиты возвращены.",
                reply_markup=kb_error_with_alt("img", key),
                parse_mode="HTML"
            )
        except Exception:
            await cb.message.answer(
                f"⚠️ {friendly_error(e)}\n\n💳 Кредиты возвращены.",
                reply_markup=kb_error_with_alt("img", key),
                parse_mode="HTML"
            )
    finally:
        await unmark_generation_active(uid)
    await cb.answer()
async def download_original(cb: CallbackQuery):
    """Отправляет оригинальное фото как документ без сжатия."""
    uid = cb.from_user.id
    stored = user_orig_images.get(uid)
    img_bytes = stored["data"] if isinstance(stored, dict) else stored
    if not img_bytes:
        await cb.answer("❌ Оригинал не найден. Сгенерируй фото заново.", show_alert=True)
        return
    await cb.answer("⬇️ Отправляю оригинал...")
    await cb.message.answer_document(
        BufferedInputFile(img_bytes, "original_image.png"),
        caption="\U0001f4ce <b>Оригинал без сжатия</b>\n\n<i>Файл в полном качестве</i>",
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("chprompt:img:"))
async def change_img_prompt(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    await state.update_data(model_key=key)
    await state.set_state(ImgState.waiting_prompt)
    await cb.message.answer(
        f"💡 Введи новый промт для <b>{model_title_n(IMAGE_MODELS[key]['name'])}</b>:",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("improve_prompt:"))
async def improve_prompt_inline(cb: CallbackQuery, state: FSMContext):
    """Улучшает текущий промт через Claude и предлагает генерировать с ним."""
    parts = cb.data.split(":")
    prefix = parts[1]  # img или vid
    key = parts[2]

    data = await state.get_data()
    current_prompt = data.get("prompt", "")
    if not current_prompt:
        await cb.answer("Промт не найден. Введи промт снова.", show_alert=True)
        return

    await cb.answer()
    wait = await cb.message.answer("✨ Улучшаю промт...")

    try:
        system = (
            "Ты эксперт по промтам для AI-генерации изображений и видео. "
            "Улучши промт пользователя: сделай его более детальным, добавь стиль, освещение, "
            "настроение и технические детали. Отвечай ТОЛЬКО готовым промтом на английском, "
            "без объяснений и вводных слов. Максимум 120 слов."
        )
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=system,
                messages=[{"role": "user", "content": f"Улучши этот промт: {current_prompt}"}],
            )
        )
        improved = resp.content[0].text.strip().strip('"').strip("'")
        await state.update_data(prompt=improved)

        m = IMAGE_MODELS.get(key) or VIDEO_MODELS.get(key)
        model_name = m["name"] if m else key

        await wait.delete()
        await cb.message.answer(
            f"✨ <b>Промт улучшен!</b>\n\n"
            f"<b>Было:</b> <i>{current_prompt[:100]}</i>\n\n"
            f"<b>Стало:</b>\n<code>{improved}</code>\n\n"
            f"Модель: <b>{model_name}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Генерировать", callback_data=f"go:{prefix}:{key}")],
                [InlineKeyboardButton(text="✍️ Изменить ещё", callback_data=f"improve_prompt:{prefix}:{key}")],
                [InlineKeyboardButton(text="🔙 Оригинальный промт", callback_data=f"chprompt:{prefix}:{key}")],
                [_eib("Главное меню", "back_main")],
            ])
        )
    except Exception as e:
        logging.error(f"improve_prompt_inline error: {e}")
        try:
            await wait.delete()
        except Exception:
            pass
        await cb.message.answer(
            "⚠️ Не удалось улучшить промт. Попробуй снова.",
            reply_markup=kb_confirm(prefix, key)
        )


@dp.callback_query(F.data.startswith("again:") & ~F.data.startswith("again:edit:"))
async def after_gen_again(cb: CallbackQuery, state: FSMContext):
    """Ещё раз - та же модель, новый промт."""
    parts = cb.data.split(":")
    menu = parts[1]   # "image" или "video"
    key  = parts[2] if len(parts) > 2 else ""
    await state.clear()
    if menu == "image" and key in IMAGE_MODELS:
        m = IMAGE_MODELS[key]
        await state.update_data(model_key=key)
        await state.set_state(ImgState.waiting_prompt)
        await cb.message.answer(
            f"{model_title_n(m['name'])} - снова!\n\n"
            f"💳 Спишется: <b>{m['credits']} кредитов</b>\n\n"
            f"💡 Введи промт:",
            reply_markup=kb_cancel(), parse_mode="HTML"
        )
    elif menu == "video" and key in VIDEO_MODELS:
        m = VIDEO_MODELS[key]
        await state.update_data(model_key=key)
        await state.set_state(VidState.waiting_prompt)
        await cb.message.answer(
            f"{model_title_n(m['name'])} - снова!\n\n"
            f"💳 Спишется: <b>{m['credits']} кредитов</b>\n\n"
            f"💡 Введи промт:",
            reply_markup=kb_cancel(), parse_mode="HTML"
        )
    else:
        await cb.message.answer("Выбери действие 👇", reply_markup=kb_main())
    await cb.answer()


@dp.callback_query(F.data.startswith("improve:"))
async def after_gen_improve(cb: CallbackQuery, state: FSMContext):
    """Улучшить промт - предлагает написать уточнение."""
    parts = cb.data.split(":")
    menu = parts[1]
    key  = parts[2] if len(parts) > 2 else ""
    await state.clear()
    if menu == "image" and key in IMAGE_MODELS:
        await state.update_data(model_key=key)
        await state.set_state(ImgState.waiting_prompt)
        await cb.message.answer(
            f"✨ <b>Улучши промт</b>\n\n"
            f"Напиши более подробный запрос. Советы:\n"
            f"• Добавь стиль: <i>oil painting, photorealistic, anime</i>\n"
            f"• Добавь освещение: <i>golden hour, neon lights, studio light</i>\n"
            f"• Добавь детали: <i>4k, ultra detailed, cinematic</i>\n\n"
            f"✏️ Новый промт:",
            reply_markup=kb_cancel(), parse_mode="HTML"
        )
    elif menu == "video" and key in VIDEO_MODELS:
        await state.update_data(model_key=key)
        await state.set_state(VidState.waiting_prompt)
        await cb.message.answer(
            f"✨ <b>Улучши промт для видео</b>\n\n"
            f"Советы:\n"
            f"• Опиши движение: <i>camera slowly zooms in</i>\n"
            f"• Добавь атмосферу: <i>cinematic, dramatic lighting</i>\n"
            f"• Укажи детали сцены\n\n"
            f"✏️ Новый промт:",
            reply_markup=kb_cancel(), parse_mode="HTML"
        )
    await cb.answer()


@dp.callback_query(F.data == "menu_video")
async def menu_video(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cr = await get_credits(cb.from_user.id)
    text = (
        f"🎬 <b>Создать видео</b>\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n\n"
        f"<b>Выбери модель:</b>\n\n"
        f'{tg_emoji_ui("vband_veo", "🎥")} <b>Veo</b> - до 4K + аудио, от 239 кр\n'
        f'{tg_emoji_ui("vband_kling", "🎞")} <b>Kling</b> - плавная физика + аудио, от 109 кр\n'
        f'{tg_emoji_ui("vband_seedance", "🎬")} <b>Seedance</b> - нативное аудио, от 99 кр\n'
        f'{tg_emoji_ui("vband_wan", "🌊")} <b>Wan</b> - топ open-source, от 80 кр\n'
        f'{tg_emoji_ui("vband_grok", "⚡")} <b>Grok</b> - xAI, нативное аудио, от 99 кр\n\n'
        f"⏱ <i>Время генерации: 1–10 минут</i>"
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb_video_brands(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_video_brands(), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("vband:"))
async def choose_vid_brand(cb: CallbackQuery, state: FSMContext):
    """Открыть подменю моделей выбранного видео-бренда."""
    await state.clear()
    brand = cb.data.split(":")[1]
    if brand not in VIDEO_BRAND_MODELS:
        await cb.answer()
        return
    cr = await get_credits(cb.from_user.id)
    _vbrand_name = VIDEO_BRAND_TITLES.get(brand, brand)
    _vbrand_eid_key = f"vband_{brand}"
    _vbrand_fallbacks = {
        "vband_veo": "🎥", "vband_kling": "🎞", "vband_seedance": "🎬",
        "vband_wan": "🌊", "vband_grok": "⚡",
    }
    _vbrand_fb = _vbrand_fallbacks.get(_vbrand_eid_key, "")
    title = f"{tg_emoji_ui(_vbrand_eid_key, _vbrand_fb)} <b>{_vbrand_name}</b>" if UI_EMOJI_IDS.get(_vbrand_eid_key) else f"<b>{_vbrand_name}</b>"

    import re as _re_vid
    _vband_eid = UI_EMOJI_IDS.get(_vbrand_eid_key, "")
    lines = []
    for key in VIDEO_BRAND_MODELS[brand]:
        if key in VIDEO_MODELS:
            m = VIDEO_MODELS[key]
            icon = "🔹" if cr >= m['credits'] else "🔸"
            clean_vname = _re_vid.sub(r'^[^\w\s]+\s*', '', m['name']).strip()
            model_ename = f'<tg-emoji emoji-id="{_vband_eid}">{_vbrand_fb}</tg-emoji> ' if _vband_eid else ""
            lines.append(
                f"{icon} {model_ename}<b>{clean_vname}</b> - {m['credits']} кр\n"
                f"   <i>{m['res']} · {m['desc']}</i>"
            )

    # Для Kling добавляем описание Motion Control
    if brand == "kling":
        min_motion = min(MOTION_PRICES.values())
        icon = "🔹" if cr >= min_motion else "🔸"
        lines.append(
            f"{icon} <b>Motion Control</b> - от {min_motion} кр\n"
            f"   <i>Перенос движений с видео на твоего персонажа</i>"
        )

    text = (
        f"{title}\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n\n"
        + "\n\n".join(lines)
        + "\n\n⏱ <i>Время генерации: 1–6 минут</i>"
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb_video_models_for_brand(brand), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_video_models_for_brand(brand), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "back_vid_brands")
async def back_to_vid_brands(cb: CallbackQuery, state: FSMContext):
    """Возврат к выбору бренда из подменю."""
    await menu_video(cb, state)


@dp.callback_query(F.data.startswith("vmodel:"))
async def choose_vid_model(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    m = VIDEO_MODELS[key]
    cr = await get_credits(cb.from_user.id)

    # Если у модели есть выбор длительности (Kling) - сначала спрашиваем её
    if m.get("durations"):
        # Проверяем минимальную цену (по самой короткой длительности)
        min_credits = min(c for c, _ in m["durations"].values())
        if cr < min_credits:
            await cb.answer(f"💸 Нужно минимум {min_credits} кредитов. Пополни баланс!", show_alert=True)
            return
        await state.update_data(model_key=key)
        await state.set_state(VidState.waiting_duration)
        lines = ["<b>⏱ Выбери длительность видео:</b>\n"]
        for sec in sorted(m["durations"].keys()):
            credits, price = m["durations"][sec]
            icon = "🔹" if cr >= credits else "🔸"
            lines.append(f"{icon} <b>{sec} сек</b> - {credits} кр")
        await cb.message.edit_text(
            f"{model_title_n(m['name'])} ✅\n\n"
            f"📐 {m['res']}\n"
            f"💵 Баланс: <b>{cr} кр</b>\n\n"
            + "\n".join(lines) +
            "\n\n<i>🔹 доступно · 🔸 нужно пополнить</i>",
            reply_markup=kb_vid_duration(key), parse_mode="HTML"
        )
        await cb.answer()
        return

    # Veo - фиксированные 8 сек, сразу формат
    if cr < m["credits"]:
        await cb.answer(f"💸 Нужно {m['credits']} кредитов. Пополни баланс!", show_alert=True)
        return
    await state.update_data(model_key=key)
    await state.set_state(VidState.waiting_aspect)
    await cb.message.edit_text(
        f"{model_title_n(m['name'])} ✅\n\n"
        f"💳 Спишется: <b>{m['credits']} кредитов</b>\n"
        f"📐 {m['res']} | 8 сек\n\n"
        f"📐 <b>Выбери формат видео:</b>",
        reply_markup=kb_aspect_video(key), parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("vdur:"))
async def choose_vid_duration(cb: CallbackQuery, state: FSMContext):
    """Клиент выбрал длительность Kling-видео - переходим к выбору формата."""
    parts = cb.data.split(":")
    if len(parts) != 3:
        await cb.answer()
        return
    key = parts[1]
    try:
        duration = int(parts[2])
    except ValueError:
        await cb.answer()
        return

    m = VIDEO_MODELS.get(key)
    if not m or "durations" not in m or duration not in m["durations"]:
        await cb.answer("Эта длительность недоступна")
        return

    credits, price = m["durations"][duration]
    cr = await get_credits(cb.from_user.id)
    if cr < credits:
        await cb.answer(f"💸 Нужно {credits} кредитов, у тебя {cr}", show_alert=True)
        return

    await state.update_data(model_key=key, duration_sec=duration, credits_override=credits)
    await state.set_state(VidState.waiting_aspect)
    await cb.message.edit_text(
        f"{model_title_n(m['name'])} ✅\n\n"
        f"⏱ <b>{duration} секунд</b>\n"
        f"💳 Спишется: <b>{credits} кредитов</b> ({price})\n"
        f"📐 {m['res']}\n\n"
        f"📐 <b>Выбери формат видео:</b>",
        reply_markup=kb_aspect_video(key), parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("alt_vid:"))
async def choose_alt_vid_model(cb: CallbackQuery, state: FSMContext):
    """Клиент выбрал альтернативную видео-модель после ошибки 503."""
    key = cb.data.split(":")[1]
    if key not in VIDEO_MODELS:
        await cb.answer()
        return
    cb.data = f"vmodel:{key}"
    await choose_vid_model(cb, state)


@dp.callback_query(F.data.startswith("retry_vid:"))
async def retry_vid_model(cb: CallbackQuery, state: FSMContext):
    """Клиент хочет попробовать ту же видео-модель ещё раз после ошибки."""
    key = cb.data.split(":")[1]
    if key not in VIDEO_MODELS:
        await cb.answer()
        return
    cb.data = f"vmodel:{key}"
    await choose_vid_model(cb, state)


@dp.callback_query(F.data.startswith("vaspect:"))
async def choose_vid_aspect(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    key = parts[1]
    ratio = ":".join(parts[2:])  # "9:16", "16:9" etc
    m = VIDEO_MODELS[key]
    labels = {"16:9": "Горизонталь 16:9", "9:16": "Вертикаль 9:16", "1:1": "Квадрат 1:1"}
    await state.update_data(model_key=key, aspect_ratio=ratio)
    await state.set_state(VidState.waiting_prompt)
    await cb.message.edit_text(
        f"{model_title_n(m['name'])} | 📐 {labels.get(ratio, ratio)}\n\n"
        f"💳 Спишется: <b>{m['credits']} кредитов</b>\n"
        f"📐 {m['res']} | 8 сек\n\n"
        f"💡 <b>Введи промт:</b>\n\n"
        f"<i>Пример: A drone flies over Tokyo at night, cinematic, smooth motion</i>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )
    await cb.answer()


@dp.message(VidState.waiting_aspect)
async def vid_aspect_text(message: Message):
    """Если написали текст вместо выбора формата."""
    await message.answer("👆 Выбери формат кнопкой выше")


@dp.message(VidState.waiting_prompt, F.photo)
async def vid_prompt_photo(message: Message, state: FSMContext):
    """Клиент прислал фото в текстовом видео-флоу — он хочет видео ИЗ фото.
    Кнопка «Видео» = текст→видео; для фото→видео нужна анимация. Плавно переводим туда."""
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    fb = await bot.download_file(file.file_path)
    await state.update_data(
        first_photo=list(fb.read()),
        anim_model_key="anim_veo",
        anim_mode="one",
    )
    await state.set_state(AnimState.waiting_aspect)
    await message.answer(
        "📷 <b>Вижу фото!</b>\n\n"
        "Кнопка «🎬 Видео» делает ролик по <b>текстовому описанию</b> (без фото).\n"
        "А из твоего фото я сделаю <b>анимацию</b> — видео прямо из картинки 👇\n\n"
        "Выбери формат видео:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="16:9 Горизонталь", callback_data="anim_aspect:16:9")],
            [InlineKeyboardButton(text="9:16 Вертикаль",   callback_data="anim_aspect:9:16")],
            [InlineKeyboardButton(text="1:1 Квадрат",      callback_data="anim_aspect:1:1")],
            [_eib("Главное меню", "back_main")],
        ]),
        parse_mode="HTML"
    )


@dp.message(VidState.waiting_prompt)
async def vid_prompt(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("model_key")
    if not key or key not in VIDEO_MODELS:
        await state.clear()
        await message.answer("⚠️ Сессия сброшена. Начни заново: нажми 🎬 Видео.")
        return
    m = VIDEO_MODELS[key]
    prompt = await _extract_prompt(message)

    # Валидация
    ok, err = validate_gen_prompt(prompt)
    if not ok:
        await message.answer(err)
        return

    await state.update_data(prompt=prompt)

    # Используем выбранную длительность и цену (Kling) или базовую (Veo)
    credits_cost = data.get("credits_override") or m["credits"]
    duration_sec = data.get("duration_sec") or 8

    # Адаптивный текст времени в зависимости от модели
    api_type_ui = m.get("api", "veo")
    if api_type_ui == "fal":
        if "v3" in m.get("model_id", ""):
            time_text = "5–20 минут (качественная модель)"
        else:
            time_text = "3–10 минут"
    else:
        time_text = "1–6 минут"

    await message.answer(
        f"📝 <b>Проверь заказ:</b>\n\n"
        f"{model_title_n(m['name'])}\n"
        f"📐 {m['res']} | {duration_sec} сек\n"
        f"💳 <b>{credits_cost} кредитов</b>\n\n"
        f"📝 <i>{prompt}</i>\n\n"
        f"⏱ <i>Генерация занимает {time_text}</i>",
        reply_markup=kb_confirm("vid", key), parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("go:vid:"))
async def go_video(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    m = VIDEO_MODELS[key]
    data = await state.get_data()
    prompt = data.get("prompt", "")
    uid = cb.from_user.id

    # Для Kling-моделей используем выбранную длительность и её цену,
    # для Veo - базовую цену из VIDEO_MODELS
    credits_cost = data.get("credits_override") or m["credits"]
    duration_sec = data.get("duration_sec") or 8  # Veo всегда 8

    # Rate limit
    if not await _check_can_generate(cb, uid, kind="video"):
        return

    ok = await deduct(uid, credits_cost)
    if not ok:
        await cb.answer("💸 Недостаточно кредитов!", show_alert=True)
        return

    await mark_generation_active(uid, "video")
    await state.clear()

    # Адаптивный текст начального сообщения в зависимости от модели
    api_type_ui = m.get("api", "veo")
    if api_type_ui == "fal":
        if "v3" in m.get("model_id", ""):
            time_estimate = "5–20 минут"
        else:
            time_estimate = "3–10 минут"
    else:
        time_estimate = "1–6 минут"

    status_msg = await cb.message.edit_text(
        f"🎬 <b>Генерирую видео...</b>\n\n"
        f"{model_title_n(m['name'])} | {m['res']} | {duration_sec} сек\n"
        f"📝 <i>{prompt[:80]}</i>\n\n"
        f"🕐 Обычно {time_estimate}. Пришлю как только готово 👇",
        parse_mode="HTML"
    )

    # Фоновая задача: анимированный прогресс-бар с %
    # Оценочное среднее время генерации по моделям (в секундах)
    estimated_times = {
        "vid_lite":     120,   # Veo Lite ~ 2 мин
        "vid_fast":     180,   # Veo Fast ~ 3 мин
        "vid_pro":      240,   # Veo Pro ~ 4 мин
        "kling_turbo":  300,   # Kling 2.5 Turbo ~ 5 мин
        "kling_pro":    600,   # Kling 3.0 Pro ~ 10 мин
        "seedance_15":  180,   # Seedance 1.5 Pro ~ 3 мин
        "seedance_20":  240,   # Seedance 2.0 ~ 4 мин
        "wan_22":       120,   # Wan 2.2 ~ 2 мин
        "grok_vid":     120,   # Grok ~ 2 мин
    }

    async def progress_updates():
        """Показывает анимированный прогресс-бар с % на основе прошедшего времени."""
        # Эмодзи-спиннер для ощущения процесса
        spinner_frames = ["✨", "⚡", "💫", "🌟", "⭐", "🎇", "🎆"]
        # Оценочное время для этой модели
        estimated_sec = estimated_times.get(key, 180)

        try:
            start_time = asyncio.get_event_loop().time()
            iteration = 0

            while True:
                # Первую отрисовку делаем быстро - через 5 сек
                # Далее каждые 10 сек
                await asyncio.sleep(5 if iteration == 0 else 10)
                iteration += 1

                elapsed = asyncio.get_event_loop().time() - start_time

                # Прогресс - максимум 95% пока не завершено по-настоящему
                progress_pct = min(95, int(elapsed / estimated_sec * 100))

                # Прогресс-бар из 20 блоков
                bar_filled = int(progress_pct / 5)  # каждые 5% = 1 блок
                bar = "▰" * bar_filled + "▱" * (20 - bar_filled)

                # Форматирование времени
                elapsed_min = int(elapsed / 60)
                elapsed_sec = int(elapsed % 60)
                elapsed_text = f"{elapsed_min}:{elapsed_sec:02d}"

                # Остаточное время
                if progress_pct >= 95:
                    status_text = "🔄 Финализация видео..."
                elif progress_pct > 0:
                    remaining_sec = max(0, estimated_sec - elapsed)
                    remaining_min = remaining_sec / 60
                    if remaining_min >= 2:
                        status_text = f"⏱ Осталось ~{int(remaining_min)} мин"
                    elif remaining_min >= 1:
                        status_text = f"⏱ Осталось ~1 мин"
                    else:
                        status_text = "⏱ Скоро готово..."
                else:
                    status_text = "🔄 Подготовка..."

                spinner = spinner_frames[iteration % len(spinner_frames)]

                try:
                    await bot.edit_message_text(
                        chat_id=cb.message.chat.id,
                        message_id=status_msg.message_id,
                        text=(
                            f"🎬 <b>Генерирую видео - {progress_pct}%</b> {spinner}\n"
                            f"<code>{bar}</code>\n\n"
                            f"{model_title_n(m['name'])} | {m['res']}\n"
                            f"📝 <i>{prompt[:80]}</i>\n\n"
                            f"⏳ Прошло: <b>{elapsed_text}</b>\n"
                            f"{status_text}"
                        ),
                        parse_mode="HTML"
                    )
                except Exception as edit_err:
                    # "message is not modified", сеть моргнула, сообщение удалили - не критично
                    logging.debug(f"Progress update failed: {edit_err}")
        except asyncio.CancelledError:
            return

    progress_task = asyncio.create_task(progress_updates())

    # Флаг чтобы избежать двойного возврата кредитов при вложенных исключениях
    refunded = False

    async def refund_once(reason: str = ""):
        """Возвращает кредиты только один раз, логирует."""
        nonlocal refunded
        if refunded:
            logging.warning(f"refund_once SKIPPED (already refunded) uid={uid} reason={reason}")
            return
        refunded = True
        await add_credits(uid, credits_cost)
        logging.info(f"refund_once EXECUTED uid={uid} credits={credits_cost} reason={reason}")

    try:
        aspect = data.get("aspect_ratio", "16:9")
        api_type = m.get("api", "veo")
        # Семафор: не более 5 Veo генераций одновременно (клиенту не видно)
        # Для fal.ai - без семафора, параллельность там управляется самой платформой
        if api_type == "veo":
            async with _veo_semaphore:
                vid_bytes = await _with_retry(
                    lambda: api_generate_video(prompt, m["model_id"], aspect, api_type, duration_sec),
                    max_attempts=2, base_delay=5.0, op_name=f"Veo {key}"
                )
        else:
            # Для fal.ai - без retry, т.к. одна попытка уже до 25 минут
            vid_bytes = await api_generate_video(prompt, m["model_id"], aspect, api_type, duration_sec)
        size_mb = len(vid_bytes) / 1024 / 1024
        logging.info(f"Video ready: {len(vid_bytes)} bytes ({size_mb:.1f} MB), duration={duration_sec}s")
        await log_gen(uid, "video", key, credits_cost)
        _record_generation(uid, _video_history)
        await check_expiring_credits(uid)
        cr = await get_credits(uid)
        caption = f"🎉 Готово! {model_title_n(m['name'])} | {m['res']} | {duration_sec} сек\n💸 Списано {credits_cost} кредитов | Остаток: {cr} кредитов"
        # СНАЧАЛА удаляем сообщение прогресс-бара чтобы юзер не видел "90%" во время отправки
        if not progress_task.done():
            progress_task.cancel()
            try:
                await progress_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await status_msg.delete()
        except Exception:
            pass

        # Если видео > 48 МБ - сразу на хостинг, не пытаемся через Telegram
        if size_mb > 48:
            try:
                await cb.message.answer("⏳ Видео большое, загружаю на хостинг...")
                upload_url = await upload_large_file(vid_bytes, f"video_original_{key}.mp4")
                if upload_url:
                    await cb.message.answer(
                        f"🎉 <b>Готово! {model_title_n(m['name'])}</b>\n"
                        f"💸 Списано {credits_cost} кредитов | Остаток: {cr} кредитов\n\n"
                        f"📁 Файл {size_mb:.1f} МБ - слишком большой для Telegram.\n"
                        f"Скачай оригинал по ссылке (доступна 24 часа):\n"
                        f"<a href='{upload_url}'>{upload_url}</a>",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=kb_after("video", key),
                    )
                else:
                    await refund_once("upload_failed")
                    await cb.message.answer(
                        f"⚠️ Видео сгенерировано ({size_mb:.1f} МБ), но не удалось доставить.\n"
                        f"Кредиты возвращены 💳\nНапиши @neirosetkaalex.",
                        reply_markup=kb_back(),
                    )
            except Exception as up_err:
                logging.error(f"video large upload failed: {up_err}")
                await refund_once("upload_exception")
            return

        # Видео <= 48 МБ - отправляем через Telegram
        video_sent = False
        video_err = None
        for vid_attempt in range(1, 4):
            try:
                await cb.message.answer_video(
                    BufferedInputFile(vid_bytes, "video.mp4"),
                    caption=caption + "\n\n👇 Ниже - оригинал без сжатия",
                    parse_mode="HTML",
                    reply_markup=kb_after("video", key),
                    supports_streaming=True,
                )
                video_sent = True
                if vid_attempt > 1:
                    logging.info(f"answer_video succeeded on attempt {vid_attempt}/3 ({size_mb:.1f} MB)")
                break
            except Exception as ve:
                video_err = ve
                err_str = str(ve).lower()
                is_timeout = "timeout" in err_str or "timed out" in err_str
                if vid_attempt < 3 and is_timeout:
                    logging.warning(f"answer_video attempt {vid_attempt}/3 timed out ({size_mb:.1f} MB) - retrying")
                    await asyncio.sleep(3 * vid_attempt)
                    continue
                logging.error(f"answer_video failed ({size_mb:.1f} MB): {ve}")
                break

        if not video_sent:
            await notify_admin_error(f"Видео НЕ отправлено uid={uid} {size_mb:.1f}MB", video_err)
            try:
                await cb.message.answer(
                    f"⚠️ <b>Видео не загрузилось в Telegram</b>\n\n"
                    f"Кредиты возвращены 💳\nНапиши @neirosetkaalex.",
                    parse_mode="HTML",
                    reply_markup=kb_back()
                )
            except Exception:
                pass
            await refund_once("video_send_failed")
            return
        # 2. Оригинал без сжатия - если < 48 МБ отправляем файлом, иначе загружаем на хостинг
        if size_mb < 48:
            doc_sent = False
            doc_err = None
            for doc_attempt in range(1, 4):
                try:
                    await bot.send_document(
                        chat_id=cb.message.chat.id,
                        document=BufferedInputFile(vid_bytes, f"video_original_{key}.mp4"),
                        caption="📁 <b>Оригинал без сжатия</b> - максимальное качество",
                        parse_mode="HTML",
                        disable_content_type_detection=True,
                    )
                    doc_sent = True
                    break
                except Exception as de:
                    doc_err = de
                    err_str = str(de).lower()
                    if doc_attempt < 3 and ("timeout" in err_str or "timed out" in err_str):
                        await asyncio.sleep(2 * doc_attempt)
                        continue
                    break
            if not doc_sent:
                logging.error(f"video send_document FAILED ({size_mb:.1f} MB): {doc_err}")
                await notify_admin_error(f"Документ видео uid={uid} {size_mb:.1f}MB", doc_err)
        else:
            # Файл > 48 МБ - загружаем на временный хостинг и даём ссылку
            try:
                upload_url = await upload_large_file(vid_bytes, f"video_original_{key}.mp4")
                if upload_url:
                    await cb.message.answer(
                        f"📁 <b>Оригинал без сжатия</b>\n\n"
                        f"Файл {size_mb:.1f} МБ - слишком большой для Telegram.\n"
                        f"Скачай по ссылке (доступна 24 часа):\n"
                        f"<a href='{upload_url}'>{upload_url}</a>",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                else:
                    await cb.message.answer(
                        f"📁 Оригинал ({size_mb:.1f} МБ) - слишком большой для Telegram.\n"
                        f"Напиши @neirosetkaalex - пришлём файл напрямую."
                    )
            except Exception as up_err:
                logging.error(f"upload_large_file failed for video: {up_err}")
    except Exception as e:
        await refund_once(f"exception:{type(e).__name__}")
        await notify_admin_error(f"Генерация видео uid={uid} model={key} dur={duration_sec}s", e)
        try:
            await cb.message.answer(
                f"⚠️ {friendly_error(e)}\n\n💳 Кредиты возвращены.",
                reply_markup=kb_error_with_alt("vid", key),
                parse_mode="HTML"
            )
        except Exception as msg_err:
            logging.warning(f"Failed to send error message: {msg_err}")
    finally:
        await unmark_generation_active(uid)
        # Отменяем фоновую задачу обновления статуса
        if not progress_task.done():
            progress_task.cancel()
            try:
                await progress_task
            except (asyncio.CancelledError, Exception):
                pass
    await cb.answer()


@dp.callback_query(F.data.startswith("chprompt:vid:"))
async def change_vid_prompt(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    await state.update_data(model_key=key)
    await state.set_state(VidState.waiting_prompt)
    await cb.message.edit_text(
        f"💡 Введи новый промт для <b>{model_title_n(VIDEO_MODELS[key]['name'])}</b>:",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )
    await cb.answer()

# ══════════════════════════════════════════════════════════
#  КОНСУЛЬТАНТ (оригинальная логика сохранена)
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "menu_upscale")
async def menu_upscale(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cr = await get_credits(cb.from_user.id)
    if cr < UPSCALE_CREDIT_COST:
        try:
            await cb.message.edit_text(
                f"💸 Недостаточно кредитов\n\nНужно {UPSCALE_CREDIT_COST} кр, у тебя {cr} кр.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_eib("Купить кредиты", "menu_buy")],
                    [_eib("Главное меню", "back_main")],
                ]),
                parse_mode="HTML"
            )
        except Exception:
            pass
        await cb.answer()
        return
    text = (
        f"🔍 <b>Улучшить фото (4x)</b>\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n"
        f"💵 Стоимость: <b>{UPSCALE_CREDIT_COST} кр</b>\n\n"
        f"Увеличивает разрешение в 4 раза с сохранением деталей.\n"
        f"Идеально для: фото из бота, аватарок, постеров, принтов.\n\n"
        f"📎 Отправь фото для апскейла:"
    )
    await state.set_state(UpscaleState.waiting_photo)
    try:
        await cb.message.edit_text(text, reply_markup=kb_cancel(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_cancel(), parse_mode="HTML")
    await cb.answer()


@dp.message(UpscaleState.waiting_photo, F.photo)
async def do_upscale(message: Message, state: FSMContext):
    uid = message.from_user.id
    cr = await get_credits(uid)
    if cr < UPSCALE_CREDIT_COST:
        await message.answer(
            f"💸 Недостаточно кредитов. Нужно {UPSCALE_CREDIT_COST} кр.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [_eib("Купить кредиты", "menu_buy")],
            ])
        )
        await state.clear()
        return

    wait = await message.answer("⏳ Улучшаю фото... обычно 20–40 сек")
    _deducted = False
    try:
        # Скачиваем фото
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        buf = await bot.download_file(file.file_path)
        img_bytes = buf.read()

        # Конвертируем фото в base64 data URI - fal.ai принимает напрямую
        import base64 as _b64
        img_b64 = _b64.b64encode(img_bytes).decode("utf-8")
        image_data_uri = f"data:image/jpeg;base64,{img_b64}"

        # Запускаем апскейл через Clarity Upscaler
        upscale_headers = {
            "Authorization": f"Key {FAL_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {
            "image_url": image_data_uri,
            "scale": 4,
            "creativity": 0.35,
            "resemblance": 0.85,
            "prompt": "masterpiece, best quality, highres, detailed",
            "negative_prompt": "(worst quality, low quality, normal quality:2)",
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://fal.run/fal-ai/clarity-upscaler",
                headers=upscale_headers,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as r:
                if r.content_type and "json" in r.content_type:
                    result = await r.json()
                else:
                    raw = await r.text()
                    raise Exception(f"Upscale non-JSON response: {raw[:200]}")

        out_url = result.get("image", {}).get("url")
        if not out_url:
            raise Exception(f"Upscale failed: {str(result)[:200]}")

        # Скачиваем результат
        async with aiohttp.ClientSession() as s:
            async with s.get(out_url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                out_bytes = await r.read()

        # Списываем кредиты
        success = await deduct(uid, UPSCALE_CREDIT_COST)
        if not success:
            await wait.delete()
            await message.answer("💸 Недостаточно кредитов.")
            await state.clear()
            return
        _deducted = True

        new_cr = await get_credits(uid)
        await wait.delete()
        await message.answer_photo(
            BufferedInputFile(out_bytes, "upscaled_4x.jpg"),
            caption=(
                f"✅ <b>Фото улучшено!</b>\n"
                f"💸 Списано {UPSCALE_CREDIT_COST} кр | Остаток: {new_cr} кр"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Улучшить ещё фото", callback_data="menu_upscale"),
                 _eib("Главное меню", "back_main")],
                [InlineKeyboardButton(text="❤️ В избранное", callback_data="fav_save"),
                 _eib("Купить кредиты", "menu_buy")],
            ])
        )
        # Отправляем документом для скачивания в оригинале
        await message.answer_document(
            BufferedInputFile(out_bytes, "upscaled_4x.png"),
            caption="📁 Оригинал без сжатия",
        )
        await log_event(uid, "upscale", f"credits={UPSCALE_CREDIT_COST}")
        await check_expiring_credits(uid)

    except Exception as e:
        logging.error(f"Upscale error uid={uid}: {e}")
        if _deducted:
            try:
                await add_credits(uid, UPSCALE_CREDIT_COST)
            except Exception:
                pass
        try:
            await wait.delete()
        except Exception:
            pass
        await message.answer(
            f"⚠️ Ошибка апскейла. Кредиты возвращены. Попробуй ещё раз или напиши @neirosetkaalex.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Попробовать снова", callback_data="menu_upscale")],
                [_eib("Главное меню", "back_main")],
            ])
        )
    await state.clear()


@dp.message(UpscaleState.waiting_photo)
async def upscale_wrong_input(message: Message):
    await message.answer("📎 Пожалуйста, отправь фото (не файлом, а именно фото).")


# ─── ПРОМТ-АССИСТЕНТ ────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "menu_improve")
async def menu_improve(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text = (
        f"✨ <b>Промт-ассистент</b>\n\n"
        f"Напиши свою идею простыми словами - я улучшу её до профессионального промта\n"
        f"и сразу предложу выбрать модель для генерации.\n\n"
        f"<b>Примеры:</b>\n"
        f"• <i>девушка в кафе</i>\n"
        f"• <i>котик в космосе реалистично</i>\n"
        f"• <i>закат на море в стиле масляной живописи</i>\n\n"
        f"✏️ Напиши свой запрос:"
    )
    await state.set_state(ImproveState.waiting_prompt)
    try:
        await cb.message.edit_text(text, reply_markup=kb_cancel(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_cancel(), parse_mode="HTML")
    await cb.answer()


@dp.message(ImproveState.waiting_prompt, F.text)
async def do_improve_prompt(message: Message, state: FSMContext):
    uid = message.from_user.id
    user_idea = message.text.strip()

    if len(user_idea) < 3:
        await message.answer("Напиши чуть подробнее - хотя бы несколько слов.")
        return

    wait = await message.answer("✨ Улучшаю промт...")

    try:
        system = (
            "Ты эксперт по промтам для AI-генерации изображений. "
            "Твоя задача: взять простую идею пользователя и превратить её в профессиональный промт "
            "на английском языке для генерации изображения. "
            "Промт должен быть детальным, описывать стиль, освещение, настроение, технические детали. "
            "Отвечай ТОЛЬКО готовым промтом без объяснений, без кавычек, без вводных слов. "
            "Максимум 150 слов."
        )
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                system=system,
                messages=[{"role": "user", "content": f"Идея пользователя: {user_idea}"}],
            )
        )
        improved = resp.content[0].text
        improved = improved.strip().strip('"').strip("'")

        await state.update_data(improved_prompt=improved, original_idea=user_idea)
        await state.set_state(ImproveState.waiting_model)

        await wait.delete()
        await message.answer(
            f"✨ <b>Улучшенный промт:</b>\n\n"
            f"<code>{improved}</code>\n\n"
            f"Выбери модель для генерации 👇",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Imagen Fast - 7 кр",  callback_data="improve_gen:img_fast",    **{"icon_custom_emoji_id": UI_EMOJI_IDS["iband_imagen"]} if UI_EMOJI_IDS.get("iband_imagen") else {})],
                [InlineKeyboardButton(text="GPT Image - 10 кр",   callback_data="improve_gen:gptimg_fast", **{"icon_custom_emoji_id": UI_EMOJI_IDS["iband_gptimg"]} if UI_EMOJI_IDS.get("iband_gptimg") else {})],
                [InlineKeyboardButton(text="Nano Banana - 13 кр", callback_data="improve_gen:nb_flash",    **{"icon_custom_emoji_id": UI_EMOJI_IDS["iband_nano"]} if UI_EMOJI_IDS.get("iband_nano") else {})],
                [InlineKeyboardButton(text="Flux Pro - 12 кр",    callback_data="improve_gen:flux_pro",    **{"icon_custom_emoji_id": UI_EMOJI_IDS["iband_flux"]} if UI_EMOJI_IDS.get("iband_flux") else {})],
                [InlineKeyboardButton(text="✏️ Изменить промт",      callback_data="menu_improve")],
                [_eib("Главное меню", "back_main")],
            ])
        )
    except Exception as e:
        logging.error(f"Improve prompt error uid={uid}: {e}")
        try:
            await wait.delete()
        except Exception:
            pass
        await message.answer(
            "⚠️ Не удалось улучшить промт. Попробуй ещё раз.",
            reply_markup=kb_cancel()
        )
        await state.clear()


@dp.callback_query(F.data.startswith("improve_gen:"))
async def improve_gen(cb: CallbackQuery, state: FSMContext):
    model_key = cb.data.split(":")[1]
    data = await state.get_data()
    improved_prompt = data.get("improved_prompt", "")
    if not improved_prompt:
        await cb.answer("Промт не найден, начни заново.", show_alert=True)
        await state.clear()
        return

    m = IMAGE_MODELS.get(model_key)
    if not m:
        await cb.answer("Модель не найдена.", show_alert=True)
        return

    uid = cb.from_user.id
    cr = await get_credits(uid)
    if cr < m["credits"]:
        await cb.answer(f"Недостаточно кредитов. Нужно {m['credits']} кр.", show_alert=True)
        return

    await state.clear()
    wait = await cb.message.answer(f"🎨 Генерирую с улучшенным промтом...\n<i>{improved_prompt[:80]}...</i>", parse_mode="HTML")
    _charged = False
    try:
        # Генерируем изображение
        cb.data = f"imodel:{model_key}"
        # Используем общую функцию генерации через FSM-стейт
        img_bytes = await api_generate_image(
            improved_prompt, m["model_id"], "1:1", m["api"],
            quality=m.get("quality", "medium"))

        success = await deduct(uid, m["credits"])
        if not success:
            await wait.delete()
            await cb.message.answer("💸 Недостаточно кредитов.")
            return
        _charged = True

        new_cr = await get_credits(uid)
        await wait.delete()
        await cb.message.answer_photo(
            BufferedInputFile(img_bytes, "generated.jpg"),
            caption=(
                f"✅ <b>{model_title_n(m['name'])}</b>\n"
                f"✨ Промт улучшен ассистентом\n"
                f"💸 Списано {m['credits']} кр | Остаток: {new_cr} кр"
            ),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✨ Улучшить другой промт", callback_data="menu_improve"),
                 _eib("Главное меню", "back_main")],
                [InlineKeyboardButton(text="❤️ В избранное", callback_data="fav_save"),
                 _eib("Купить кредиты", "menu_buy")],
            ])
        )
        await log_event(uid, "improve_gen", f"model={model_key} credits={m['credits']}")
        await check_expiring_credits(uid)

    except Exception as e:
        logging.error(f"improve_gen error uid={uid} model={model_key}: {e}")
        if _charged:
            try:
                await add_credits(uid, m["credits"])
            except Exception:
                pass
        try:
            await wait.delete()
        except Exception:
            pass
        await cb.message.answer(
            "⚠️ Ошибка генерации. Кредиты возвращены, если были списаны. Попробуй другую модель.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✨ Попробовать снова", callback_data="menu_improve")],
                [_eib("Главное меню", "back_main")],
            ])
        )
    await cb.answer()


@dp.message(ImproveState.waiting_prompt)
async def improve_wrong_input(message: Message):
    await message.answer("✏️ Напиши свою идею текстом.")


@dp.callback_query(F.data == "menu_edit")
async def menu_edit(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cr = await get_credits(cb.from_user.id)
    active_edit = {k: v for k, v in EDIT_MODELS.items() if k not in DISABLED_MODELS}
    min_cost = min(m["credits"] for m in active_edit.values()) if active_edit else 0

    if cr < min_cost:
        try:
            await cb.message.edit_text(
                f"💸 Недостаточно кредитов\n\nНужно {min_cost} кредитов, у тебя {cr} кредитов.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_eib("Купить кредиты", "menu_buy")],
                    [_eib("Главное меню", "back_main")],
                ]),
                parse_mode="HTML"
            )
        except Exception:
            await cb.message.answer(
                f"💸 Недостаточно кредитов. Нужно {min_cost} кредитов.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_eib("Купить кредиты", "menu_buy")],
                ])
            )
        await cb.answer()
        return

    _EDIT_EMOJI_KEY = {
        "edit_gemini": "iband_nano",
        "edit_grok":   "iband_grok",
        "edit_gpt":    "iband_gptimg",
        "edit_flux":   "iband_flux",
    }
    _EDIT_EMOJI_FB = {
        "iband_nano": "🍌", "iband_grok": "⚡", "iband_gptimg": "🤖", "iband_flux": "🎭",
    }
    import re as _re_edit
    lines = []
    for key, m in active_edit.items():
        icon = "🔹" if cr >= m["credits"] else "🔸"
        ek = _EDIT_EMOJI_KEY.get(key, "")
        ename = tg_emoji_ui(ek, _EDIT_EMOJI_FB.get(ek, "")) if ek else ""
        clean_name = _re_edit.sub(r'^[^\w\s]+\s*', '', m['name']).strip()
        lines.append(f"{icon} {ename} <b>{clean_name}</b> - {m['credits']} кр\n   <i>{m['desc']}</i>")

    text = (
        f"🖌️ <b>Редактировать фото</b>\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n\n"
        + "\n\n".join(lines) +
        f"\n\n<i>Примеры: смени фон, добавь закат, сделай в стиле аниме</i>"
    )

    rows = []
    for key, m in active_edit.items():
        ek = _EDIT_EMOJI_KEY.get(key, "")
        eid = UI_EMOJI_IDS.get(ek, "")
        clean_name = _re_edit.sub(r'^[^\w\s]+\s*', '', m['name']).strip()
        btn = InlineKeyboardButton(
            text=f"{clean_name} - {m['credits']} кр",
            callback_data=f"edit_model:{key}",
            **{"icon_custom_emoji_id": eid} if eid else {}
        )
        rows.append([btn])
    rows.append([_eib("Главное меню", "back_main")])

    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("edit_model:"))
async def edit_model_select(cb: CallbackQuery, state: FSMContext):
    model_key = cb.data.split(":")[1]
    m = EDIT_MODELS.get(model_key)
    if not m:
        await cb.answer("Ошибка", show_alert=True)
        return
    cr = await get_credits(cb.from_user.id)
    if cr < m["credits"]:
        await cb.answer(f"Недостаточно кредитов. Нужно {m['credits']} кр.", show_alert=True)
        return

    await state.update_data(edit_model_key=model_key)
    await state.set_state(EditState.waiting_photo)
    text = (
        f"<b>{model_title_n(m['name'])}</b> - {m['desc']}\n\n"
        f"💵 Стоимость: <b>{m['credits']} кр</b>\n\n"
        f"📷 Отправь фото для редактирования:"
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb_cancel(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_cancel(), parse_mode="HTML")
    await cb.answer()


@dp.message(EditState.waiting_photo)
async def edit_get_photo(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("📷 Отправь <b>фотографию</b> - картинку из галереи или файл", parse_mode="HTML")
        return

    # Берём лучшее качество фото
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    file_bytes = await bot.download_file(file.file_path)
    img_data = file_bytes.read()

    await state.update_data(photo_bytes=list(img_data))
    await state.set_state(EditState.waiting_prompt)
    await message.answer(
        f"✅ Фото получено!\n\n"
        f"✏️ Теперь напиши <b>что изменить</b>:\n\n"
        f"<i>Примеры:\n"
        f"• Change background to sunset beach\n"
        f"• Make it look like anime art style\n"
        f"• Add snow falling\n"
        f"• Remove the background, keep only the person</i>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )


@dp.message(EditState.waiting_prompt)
async def edit_get_prompt(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("✏️ Напиши текстом что нужно изменить")
        return

    data = await state.get_data()
    prompt = await _extract_prompt(message)
    model_key = data.get("edit_model_key", "edit_gemini")
    m = EDIT_MODELS.get(model_key, EDIT_MODELS["edit_gemini"])
    edit_cost = m["credits"]
    uid = message.from_user.id

    ok_v, err = validate_gen_prompt(prompt)
    if not ok_v:
        await message.answer(err)
        return

    # Сохраняем промт и показываем подтверждение с кнопкой улучшить
    await state.update_data(edit_prompt=prompt)
    await state.set_state(EditState.waiting_confirm)
    await message.answer(
        f"🖌️ <b>Подтверди редактирование</b>\n\n"
        f"Модель: <b>{model_title_n(m['name'])}</b>\n"
        f"💵 Стоимость: <b>{edit_cost} кр</b>\n\n"
        f"📝 <i>{prompt[:150]}</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Редактировать", callback_data=f"go_edit:{model_key}")],
            [InlineKeyboardButton(text="✨ Улучшить промт с AI", callback_data=f"improve_edit:{model_key}")],
            [InlineKeyboardButton(text="✍️ Изменить промт", callback_data=f"edit_model:{model_key}")],
            [_eib("Главное меню", "back_main")],
        ])
    )


@dp.callback_query(F.data.startswith("improve_edit:"))
async def improve_edit_prompt(cb: CallbackQuery, state: FSMContext):
    model_key = cb.data.split(":")[1]
    data = await state.get_data()
    current_prompt = data.get("edit_prompt", "")
    if not current_prompt:
        await cb.answer("Промт не найден", show_alert=True)
        return
    await cb.answer()
    wait = await cb.message.answer("✨ Улучшаю промт...")
    try:
        system = (
            "Ты эксперт по промтам для AI-редактирования изображений. "
            "Улучши промт: сделай инструкцию чёткой, добавь детали стиля, освещения. "
            "Отвечай ТОЛЬКО готовым промтом на английском, без объяснений. Максимум 80 слов."
        )
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=system,
                messages=[{"role": "user", "content": f"Улучши промт для редактирования фото: {current_prompt}"}],
            )
        )
        improved = resp.content[0].text.strip().strip('"').strip("'")
        await state.update_data(edit_prompt=improved)
        m = EDIT_MODELS.get(model_key, EDIT_MODELS["edit_gemini"])
        await wait.delete()
        await cb.message.answer(
            f"✨ <b>Промт улучшен!</b>\n\n"
            f"<b>Было:</b> <i>{current_prompt[:80]}</i>\n\n"
            f"<b>Стало:</b>\n<code>{improved}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Редактировать", callback_data=f"go_edit:{model_key}")],
                [InlineKeyboardButton(text="✨ Улучшить ещё", callback_data=f"improve_edit:{model_key}")],
                [_eib("Главное меню", "back_main")],
            ])
        )
    except Exception as e:
        logging.error(f"improve_edit error: {e}")
        try:
            await wait.delete()
        except Exception:
            pass
        await cb.message.answer("⚠️ Не удалось улучшить. Попробуй снова.")


@dp.callback_query(F.data.startswith("go_edit:"))
async def go_edit_confirmed(cb: CallbackQuery, state: FSMContext):
    model_key = cb.data.split(":")[1]
    data = await state.get_data()
    prompt = data.get("edit_prompt", "")
    photo_bytes = bytes(data.get("photo_bytes", b""))
    if not prompt or not photo_bytes:
        await cb.answer("Данные потеряны. Начни заново.", show_alert=True)
        await state.clear()
        return
    m = EDIT_MODELS.get(model_key, EDIT_MODELS["edit_gemini"])
    edit_cost = m["credits"]
    uid = cb.from_user.id

    if not await _check_can_generate(cb.message, uid, kind="photo"):
        await state.clear()
        return

    cr = await get_credits(uid)
    if cr < edit_cost:
        await state.clear()
        await cb.message.answer(f"💸 Недостаточно кредитов. Нужно {edit_cost} кр.")
        return

    ok = await deduct(uid, edit_cost)
    if not ok:
        await state.clear()
        await cb.message.answer("⛔ Ошибка списания.")
        return

    await mark_generation_active(uid, "photo")
    await state.clear()
    await cb.answer()
    wait = await cb.message.answer(
        f"🖌️ Редактирую фото...\n\n"
        f"{model_title_n(m['name'])}\n"
        f"<i>{prompt[:80]}</i>",
        parse_mode="HTML"
    )

    edit_refunded = False

    async def edit_refund_once(reason: str = ""):
        nonlocal edit_refunded
        if edit_refunded:
            logging.warning(f"edit_refund_once SKIPPED uid={uid} reason={reason}")
            return
        edit_refunded = True
        await add_credits(uid, edit_cost)
        logging.info(f"edit_refund_once EXECUTED uid={uid} credits={edit_cost} reason={reason}")

    try:
        if m["api"] == "gemini":
            result_bytes = await _with_retry(
                lambda: api_edit_image(photo_bytes, prompt),
                max_attempts=3, op_name="Edit image Gemini"
            )
        else:
            # fal.ai редактирование (Grok, GPT Image, Flux Kontext)
            import base64 as _b64
            img_b64 = _b64.b64encode(photo_bytes).decode("utf-8")
            image_data_uri = f"data:image/jpeg;base64,{img_b64}"
            fal_headers = {
                "Authorization": f"Key {FAL_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            # Разные модели требуют разные поля
            if "gpt-image" in m['model_id']:
                # GPT Image 2 edit: image_urls (массив), не image_url
                payload = {
                    "prompt": prompt,
                    "image_urls": [image_data_uri],
                }
            elif "flux-kontext" in m['model_id']:
                # Flux Kontext: image_url + prompt
                payload = {
                    "prompt": prompt,
                    "image_url": image_data_uri,
                }
            else:
                # Grok и другие: image_url + prompt
                payload = {
                    "prompt": prompt,
                    "image_url": image_data_uri,
                }
            async with aiohttp.ClientSession() as s:
                # Все edit модели через queue (они могут быть медленными)
                async with s.post(
                    f"https://queue.fal.run/{m['model_id']}",
                    headers=fal_headers,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as r:
                    submit = await r.json()
                    request_id = submit.get("request_id")
                    if not request_id:
                        raise Exception(f"fal submit failed: {submit}")
                status_url = submit.get("status_url") or f"https://queue.fal.run/{m['model_id']}/requests/{request_id}/status"
                response_url = submit.get("response_url") or f"https://queue.fal.run/{m['model_id']}/requests/{request_id}"
                logging.info(f"fal edit submitted: {request_id} status_url={status_url[:80]} response_url={response_url[:80]}")
                for poll_i in range(60):
                    await asyncio.sleep(5)
                    try:
                        async with s.get(
                            status_url,
                            headers={"Authorization": f"Key {FAL_API_KEY}", "Accept": "application/json"},
                            timeout=aiohttp.ClientTimeout(total=15)
                        ) as sr:
                            if sr.content_type and "json" in sr.content_type:
                                st = await sr.json()
                            else:
                                raw = await sr.text()
                                logging.warning(f"fal edit poll non-JSON: {raw[:200]}")
                                continue
                            if st.get("status") == "COMPLETED":
                                break
                            if st.get("status") == "FAILED":
                                raise Exception(f"fal edit failed: {st}")
                    except aiohttp.ContentTypeError:
                        logging.warning(f"fal edit poll ContentTypeError attempt {poll_i}")
                        continue
                else:
                    raise Exception("fal edit timeout 5 min")
                # Get result - с retry
                result = None
                _res_headers = {"Authorization": f"Key {FAL_API_KEY}", "Accept": "application/json"}
                urls_to_try = [response_url]
                if "queue.fal.run" in response_url:
                    urls_to_try.append(response_url.replace("queue.fal.run", "fal.run"))
                elif "fal.run" in response_url:
                    urls_to_try.append(response_url.replace("fal.run", "queue.fal.run"))

                for try_url in urls_to_try:
                    for res_attempt in range(5):
                        try:
                            async with s.get(try_url, headers=_res_headers, timeout=aiohttp.ClientTimeout(total=30)) as rr:
                                if rr.content_type and "json" in rr.content_type:
                                    result = await rr.json()
                                    break
                                else:
                                    logging.warning(f"fal edit result non-JSON url={try_url[:60]} attempt {res_attempt}/5")
                                    await asyncio.sleep(5)
                        except aiohttp.ContentTypeError:
                            logging.warning(f"fal edit result CTE attempt {res_attempt}/5")
                            await asyncio.sleep(5)
                    if result:
                        break
                if not result:
                    raise Exception("fal edit result: не удалось получить JSON")
                out_url = (result.get("images") or [{}])[0].get("url")
                if not out_url:
                    out_url = result.get("image", {}).get("url")
                if not out_url:
                    result_str = str(result).lower()
                    if "content_policy_violation" in result_str or "flagged by a content" in result_str:
                        raise Exception(
                            "🛡 Контент заблокирован моделью.\n\n"
                            "Модель отклонила запрос по правилам безопасности.\n"
                            "Попробуй переформулировать."
                        )
                    raise Exception(f"No image URL: {str(result)[:200]}")
                async with s.get(out_url, timeout=aiohttp.ClientTimeout(total=60)) as dr:
                    result_bytes = await dr.read()

        await log_gen(uid, "edit", model_key, edit_cost)
        _record_generation(uid, _photo_history)
        await check_expiring_credits(uid)
        cr_left = await get_credits(uid)
        caption = f"🎉 Готово! 🖌️ Редактирование - {model_title_n(m['name'])}\n💸 Списано {edit_cost} кр | Остаток: {cr_left} кр"
        await safe_send_media(
            cb.message.answer_document,
            BufferedInputFile(result_bytes, "edited_original.png"),
            caption="📎 <b>Оригинал</b> - без сжатия, полное качество",
            parse_mode="HTML",
            op_name="edit_document",
        )
        await safe_send_media(
            cb.message.answer_photo,
            BufferedInputFile(result_bytes, "edited.png"),
            caption=caption,
            parse_mode="HTML",
            reply_markup=kb_after("edit", "edit"),
            op_name="edit_photo",
        )
        try:
            await wait.delete()
        except Exception:
            pass
    except Exception as e:
        await edit_refund_once(f"exception:{type(e).__name__}")
        await notify_admin_error(f"Редактирование фото uid={uid} model={model_key}", e)
        try:
            await wait.edit_text(
                f"⚠️ {friendly_error(e)}\n\nКредиты возвращены.",
                reply_markup=kb_back()
            )
        except Exception as msg_err:
            logging.warning(f"edit error message failed: {msg_err}")
    finally:
        await unmark_generation_active(uid)


@dp.callback_query(F.data.startswith("again:edit:"))
async def edit_again(cb: CallbackQuery, state: FSMContext):
    """Ещё раз редактировать - возвращаем в меню выбора модели."""
    await menu_edit(cb, state)


# ══════════════════════════════════════════════════════════
#  ПОДДЕРЖКА / ПОЛИТИКА / ОФЕРТА
# ══════════════════════════════════════════════════════════

# ── ИЗБРАННОЕ ──────────────────────────────────────────────────────────────────

@dp.callback_query(F.data == "menu_anim")
async def menu_anim(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cr = await get_credits(cb.from_user.id)

    active_anim = {k: v for k, v in ANIM_MODELS.items() if k not in DISABLED_MODELS}
    min_cost = min(m["credits"] for m in active_anim.values()) if active_anim else 0
    if cr < min_cost:
        try:
            await cb.message.edit_text(
                f"❌ Недостаточно кредитов\nНужно минимум {min_cost} кр, у тебя {cr} кр.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_eib("Купить кредиты", "menu_buy")],
                    [_eib("Главное меню", "back_main")],
                ]), parse_mode="HTML"
            )
        except Exception:
            await cb.message.answer(f"❌ Недостаточно кредитов. Нужно минимум {min_cost} кр.")
        await cb.answer()
        return

    _ANIM_EMOJI_KEY = {
        "anim_veo":   "vband_veo",
        "anim_grok":  "vband_grok",
        "anim_kling": "vband_kling",
        "anim_wan":   "vband_wan",
    }
    _ANIM_FALLBACK = {
        "anim_veo": "🎥", "anim_grok": "⚡", "anim_kling": "🎞", "anim_wan": "🌊",
    }
    import re as _re_anim

    # Строим список моделей
    lines = []
    for key, m in active_anim.items():
        icon = "🔹" if cr >= m["credits"] else "🔸"
        ek = _ANIM_EMOJI_KEY.get(key, "")
        ename = tg_emoji_ui(ek, _ANIM_FALLBACK.get(key, "")) if ek else ""
        clean_aname = _re_anim.sub(r'^[^\w\s]+\s*', '', m['name']).strip()
        lines.append(f"{icon} {ename} <b>{clean_aname}</b> - {m['credits']} кр\n   <i>{m['desc']}</i>")

    text = (
        f"🏃 <b>Анимировать фото</b>\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n\n"
        + "\n\n".join(lines) +
        f"\n\n⏱ <i>Время генерации: 1–6 минут</i>"
    )

    # Кнопки моделей
    rows = []
    for key, m in active_anim.items():
        ek = _ANIM_EMOJI_KEY.get(key, "")
        eid = UI_EMOJI_IDS.get(ek, "")
        clean_aname = _re_anim.sub(r'^[^\w\s]+\s*', '', m['name']).strip()
        btn = InlineKeyboardButton(
            text=f"{clean_aname} - {m['credits']} кр",
            callback_data=f"anim_model:{key}",
            **{"icon_custom_emoji_id": eid} if eid else {}
        )
        rows.append([btn])
    rows.append([_eib("Главное меню", "back_main")])

    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("anim_model:"))
async def anim_model_select(cb: CallbackQuery, state: FSMContext):
    model_key = cb.data.split(":")[1]
    m = ANIM_MODELS.get(model_key)
    if not m:
        await cb.answer("Ошибка", show_alert=True)
        return
    cr = await get_credits(cb.from_user.id)
    if cr < m["credits"]:
        await cb.answer(f"Недостаточно кредитов. Нужно {m['credits']} кр.", show_alert=True)
        return

    await state.update_data(anim_model_key=model_key)

    # Veo поддерживает два кадра, остальные - только один
    if model_key == "anim_veo":
        text = (
            f"<b>{model_title_n(m['name'])}</b> - {m['desc']}\n\n"
            f"💵 Стоимость: <b>{m['credits']} кр</b>\n\n"
            f"Выбери режим:"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="1️⃣ Один кадр",   callback_data="anim_mode:one")],
            [InlineKeyboardButton(text="2️⃣ Два кадра",   callback_data="anim_mode:two")],
            [InlineKeyboardButton(text="⬅️ Назад",        callback_data="menu_anim")],
        ])
    else:
        text = (
            f"<b>{model_title_n(m['name'])}</b> - {m['desc']}\n\n"
            f"💵 Стоимость: <b>{m['credits']} кр</b>\n\n"
            f"📷 Отправь фото для анимации:"
        )
        await state.update_data(anim_mode="one")
        await state.set_state(AnimState.waiting_first_photo)
        kb = kb_cancel()

    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("anim_mode:"))
async def anim_mode(cb: CallbackQuery, state: FSMContext):
    mode = cb.data.split(":")[1]  # "one" или "two"
    await state.update_data(anim_mode=mode)
    await state.set_state(AnimState.waiting_first_photo)
    text = (
        f"{'🖼️ Один кадр' if mode == 'one' else '🖼️🖼️ Два кадра'}\n\n"
        f"📷 Отправь {'начальное' if mode == 'two' else ''} фото:"
    )
    await cb.message.answer(text, reply_markup=kb_cancel(), parse_mode="HTML")
    await cb.answer()


@dp.message(AnimState.waiting_first_photo)
async def anim_first_photo(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("📷 Отправь фото (не файл)")
        return
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    fb = await bot.download_file(file.file_path)
    await state.update_data(first_photo=list(fb.read()))

    data = await state.get_data()
    mode = data.get("anim_mode", "one")

    if mode == "two":
        await state.set_state(AnimState.waiting_last_photo)
        await message.answer(
            "✅ Первый кадр получен!\n\n📷 Теперь отправь <b>конечное фото</b>:",
            reply_markup=kb_cancel(), parse_mode="HTML"
        )
    else:
        await state.set_state(AnimState.waiting_aspect)
        await message.answer(
            "✅ Фото получено! Выбери формат видео:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="16:9 Горизонталь", callback_data="anim_aspect:16:9")],
                [InlineKeyboardButton(text="9:16 Вертикаль",   callback_data="anim_aspect:9:16")],
                [InlineKeyboardButton(text="1:1 Квадрат",      callback_data="anim_aspect:1:1")],
                [_eib("Главное меню", "back_main")],
            ])
        )


@dp.message(AnimState.waiting_last_photo)
async def anim_last_photo(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("📷 Отправь фото (не файл)")
        return
    photo = message.photo[-1]
    file = await bot.get_file(photo.file_id)
    lb = await bot.download_file(file.file_path)
    await state.update_data(last_photo=list(lb.read()))
    await state.set_state(AnimState.waiting_aspect)
    await message.answer(
        "✅ Оба кадра получены! Выбери формат видео:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="16:9 Горизонталь", callback_data="anim_aspect:16:9")],
            [InlineKeyboardButton(text="9:16 Вертикаль",   callback_data="anim_aspect:9:16")],
            [InlineKeyboardButton(text="1:1 Квадрат",      callback_data="anim_aspect:1:1")],
            [_eib("Главное меню", "back_main")],
        ])
    )


@dp.callback_query(F.data.startswith("anim_aspect:"))
async def anim_aspect(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    ratio = ":".join(parts[1:])
    labels = {"16:9": "Горизонталь 16:9", "9:16": "Вертикаль 9:16", "1:1": "Квадрат 1:1"}
    await state.update_data(aspect_ratio=ratio)
    await state.set_state(AnimState.waiting_prompt)
    await cb.message.answer(
        f"📐 {labels.get(ratio, ratio)}\n\n"
        f"✏️ Опиши что должно происходить в видео:\n\n"
        f"<i>Примеры:\n"
        f"• Camera slowly zooms in, gentle wind moves the hair\n"
        f"• Flowers bloom and petals fall, soft light\n"
        f"• Ocean waves crash on the shore, cinematic</i>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AnimState.waiting_prompt)
async def anim_prompt(message: Message, state: FSMContext):
    if not message.text:
        await message.answer("✏️ Напиши промт текстом")
        return

    data = await state.get_data()
    prompt = await _extract_prompt(message)
    model_key = data.get("anim_model_key", "anim_veo")
    m = ANIM_MODELS.get(model_key, ANIM_MODELS["anim_veo"])

    ok_v, err = validate_gen_prompt(prompt)
    if not ok_v:
        await message.answer(err)
        return

    await state.update_data(anim_prompt_text=prompt)
    await state.set_state(AnimState.waiting_confirm)
    await message.answer(
        f"🏃 <b>Подтверди анимацию</b>\n\n"
        f"Модель: <b>{model_title_n(m['name'])}</b>\n"
        f"💵 Стоимость: <b>{m['credits']} кр</b>\n\n"
        f"📝 <i>{prompt[:150]}</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Анимировать", callback_data=f"go_anim:{model_key}")],
            [InlineKeyboardButton(text="✨ Улучшить промт с AI", callback_data=f"improve_anim:{model_key}")],
            [_eib("Главное меню", "back_main")],
        ])
    )


@dp.callback_query(F.data.startswith("improve_anim:"))
async def improve_anim_prompt(cb: CallbackQuery, state: FSMContext):
    model_key = cb.data.split(":")[1]
    data = await state.get_data()
    current_prompt = data.get("anim_prompt_text", "")
    if not current_prompt:
        await cb.answer("Промт не найден", show_alert=True)
        return
    await cb.answer()
    wait = await cb.message.answer("✨ Улучшаю промт...")
    try:
        system = (
            "Ты эксперт по промтам для AI-анимации фото. "
            "Улучши промт: опиши движение, камеру, атмосферу. "
            "Отвечай ТОЛЬКО готовым промтом на английском, без объяснений. Максимум 80 слов."
        )
        resp = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: claude_client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=200,
                system=system,
                messages=[{"role": "user", "content": f"Улучши промт для анимации фото: {current_prompt}"}],
            )
        )
        improved = resp.content[0].text.strip().strip('"').strip("'")
        await state.update_data(anim_prompt_text=improved)
        m = ANIM_MODELS.get(model_key, ANIM_MODELS["anim_veo"])
        await wait.delete()
        await cb.message.answer(
            f"✨ <b>Промт улучшен!</b>\n\n"
            f"<b>Было:</b> <i>{current_prompt[:80]}</i>\n\n"
            f"<b>Стало:</b>\n<code>{improved}</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Анимировать", callback_data=f"go_anim:{model_key}")],
                [InlineKeyboardButton(text="✨ Улучшить ещё", callback_data=f"improve_anim:{model_key}")],
                [_eib("Главное меню", "back_main")],
            ])
        )
    except Exception as e:
        logging.error(f"improve_anim error: {e}")
        try:
            await wait.delete()
        except Exception:
            pass
        await cb.message.answer("⚠️ Не удалось улучшить. Попробуй снова.")


@dp.callback_query(F.data.startswith("go_anim:"))
async def go_anim_confirmed(cb: CallbackQuery, state: FSMContext):
    model_key = cb.data.split(":")[1]
    data = await state.get_data()
    prompt = data.get("anim_prompt_text", "")
    first_bytes = bytes(data.get("first_photo", []))
    last_bytes = bytes(data["last_photo"]) if data.get("last_photo") else None
    aspect = data.get("aspect_ratio", "16:9")
    mode = data.get("anim_mode", "one")
    m = ANIM_MODELS.get(model_key, ANIM_MODELS["anim_veo"])
    anim_cost = m["credits"]
    uid = cb.from_user.id

    if not prompt or not first_bytes:
        await cb.answer("Данные потеряны. Начни заново.", show_alert=True)
        await state.clear()
        return

    if not await _check_can_generate(cb.message, uid, kind="anim"):
        await state.clear()
        return

    cr = await get_credits(uid)
    if cr < anim_cost:
        await state.clear()
        await cb.message.answer(f"❌ Недостаточно кредитов. Нужно {anim_cost} кр.")
        return

    ok = await deduct(uid, anim_cost)
    if not ok:
        await state.clear()
        await cb.message.answer("❌ Ошибка списания.")
        return

    await mark_generation_active(uid, "anim")
    await state.clear()
    await cb.answer()
    mode_label = "2️⃣ Два кадра" if mode == "two" else "1️⃣ Один кадр"
    wait = await cb.message.answer(
        f"⏳ Анимирую фото...\n\n"
        f"{model_title_n(m['name'])} | {mode_label} | {aspect}\n"
        f"<i>{prompt[:80]}</i>\n\n"
        f"⏱ Обычно 1–6 минут. Пришлю как только готово 👇",
        parse_mode="HTML"
    )

    anim_refunded = False

    async def anim_refund_once(reason: str = ""):
        nonlocal anim_refunded
        if anim_refunded:
            logging.warning(f"anim_refund_once SKIPPED uid={uid} reason={reason}")
            return
        anim_refunded = True
        await add_credits(uid, anim_cost)
        logging.info(f"anim_refund_once EXECUTED uid={uid} credits={anim_cost} reason={reason}")

    try:
        # Генерация в зависимости от модели
        if m["api"] == "veo_anim":
            async with _veo_semaphore:
                vid_bytes = await _with_retry(
                    lambda: api_animate_image(first_bytes, prompt, aspect, last_bytes),
                    max_attempts=2, base_delay=5.0, op_name="Veo animate"
                )
        else:
            # fal.ai модели (Grok, Kling, Wan)
            import base64 as _b64
            img_b64 = _b64.b64encode(first_bytes).decode("utf-8")
            image_data_uri = f"data:image/jpeg;base64,{img_b64}"

            fal_headers = {
                "Authorization": f"Key {FAL_API_KEY}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
            fal_payload = {
                "image_url": image_data_uri,
                "prompt": prompt,
                "duration": m["duration"],
                "aspect_ratio": aspect,
            }

            async with aiohttp.ClientSession() as s:
                # Submit
                async with s.post(
                    f"https://queue.fal.run/{m['model_id']}",
                    headers=fal_headers,
                    json=fal_payload,
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as r:
                    submit = await r.json()
                    request_id = submit.get("request_id")
                    if not request_id:
                        raise Exception(f"fal submit failed: {submit}")

                # Poll
                poll_headers = {"Authorization": f"Key {FAL_API_KEY}", "Accept": "application/json"}
                status_url = submit.get("status_url") or f"https://queue.fal.run/{m['model_id']}/requests/{request_id}/status"
                response_url = submit.get("response_url") or f"https://queue.fal.run/{m['model_id']}/requests/{request_id}"
                logging.info(f"fal anim submitted: {request_id} status_url={status_url[:80]} response_url={response_url[:80]}")
                for poll_i in range(180):
                    await asyncio.sleep(5)
                    try:
                        async with s.get(status_url, headers=poll_headers, timeout=aiohttp.ClientTimeout(total=15)) as sr:
                            if sr.content_type and "json" in sr.content_type:
                                st = await sr.json()
                            else:
                                logging.warning(f"fal anim poll non-JSON attempt {poll_i}")
                                continue
                            if st.get("status") == "COMPLETED":
                                break
                            if st.get("status") == "FAILED":
                                raise Exception(f"fal failed: {st}")
                    except aiohttp.ContentTypeError:
                        continue
                else:
                    raise Exception("fal timeout 15min")

                # Get result - с retry и fallback URLs
                result = None
                # Пробуем разные форматы URL - fal иногда использует разные домены
                urls_to_try = [response_url]
                if f"queue.fal.run" in response_url:
                    alt = response_url.replace("queue.fal.run", "fal.run")
                    urls_to_try.append(alt)
                elif f"fal.run" in response_url:
                    alt = response_url.replace("fal.run", "queue.fal.run")
                    urls_to_try.append(alt)
                # Также пробуем gateway
                urls_to_try.append(f"https://gateway.fal.ai/{m['model_id']}/requests/{request_id}")

                for url_attempt, try_url in enumerate(urls_to_try):
                    for res_attempt in range(5):
                        try:
                            async with s.get(try_url, headers=poll_headers, timeout=aiohttp.ClientTimeout(total=30)) as rr:
                                if rr.content_type and "json" in rr.content_type:
                                    result = await rr.json()
                                    break
                                else:
                                    logging.warning(f"fal anim result non-JSON url={try_url[:60]} attempt {res_attempt}/5: {rr.content_type}")
                                    await asyncio.sleep(5)
                        except aiohttp.ContentTypeError:
                            logging.warning(f"fal anim result CTE url={try_url[:60]} attempt {res_attempt}/5")
                            await asyncio.sleep(5)
                    if result:
                        break
                if not result:
                    raise Exception(f"fal result: не удалось получить JSON. URLs tried: {[u[:60] for u in urls_to_try]}")

                video_url = (result.get("video") or {}).get("url")
                if not video_url:
                    # Проверяем - это content policy violation?
                    result_str = str(result).lower()
                    if "content_policy_violation" in result_str or "flagged by a content" in result_str:
                        raise Exception(
                            "🛡 Контент заблокирован моделью.\n\n"
                            "Модель отклонила запрос по правилам безопасности.\n"
                            "Попробуй переформулировать - избегай NSFW, насилия или знаменитостей."
                        )
                    raise Exception(f"No video URL: {str(result)[:200]}")

                # Download
                async with s.get(video_url, timeout=aiohttp.ClientTimeout(total=120)) as dv:
                    vid_bytes = await dv.read()

        size_mb = len(vid_bytes) / 1024 / 1024
        logging.info(f"Animation ready ({m['name']}): {size_mb:.1f} MB")
        await log_gen(uid, "animate", model_key, anim_cost)
        _record_generation(uid, _anim_history)
        await check_expiring_credits(uid)
        cr_left = await get_credits(uid)
        kb_after_anim = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Ещё раз", callback_data="menu_anim"),
             _eib("Главное меню", "back_main")],
            [InlineKeyboardButton(text="❤️ В избранное", callback_data="fav_save"),
             _eib("Купить кредиты", "menu_buy")],
        ])

        # Удаляем прогресс-сообщение ПЕРЕД отправкой видео
        try:
            await wait.delete()
        except Exception:
            pass

        # Если видео > 48 МБ - сразу на хостинг
        if size_mb > 48:
            try:
                await cb.message.answer("⏳ Видео большое, загружаю на хостинг...")
                upload_url = await upload_large_file(vid_bytes, "animation_original.mp4")
                if upload_url:
                    await cb.message.answer(
                        f"✅ <b>Готово! 🏃 Анимация фото</b>\n"
                        f"💵 Списано {anim_cost} кр | Остаток: {cr_left} кр\n\n"
                        f"📁 Файл {size_mb:.1f} МБ - слишком большой для Telegram.\n"
                        f"Скачай оригинал по ссылке (доступна 24 часа):\n"
                        f"<a href='{upload_url}'>{upload_url}</a>",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=kb_after_anim,
                    )
                else:
                    await anim_refund_once("upload_failed")
                    await cb.message.answer(
                        f"⚠️ Анимация создана ({size_mb:.1f} МБ), но не удалось доставить.\n"
                        f"Кредиты возвращены 💳\nНапиши @neirosetkaalex.",
                        reply_markup=kb_back(),
                    )
            except Exception as up_err:
                logging.error(f"anim large upload failed: {up_err}")
                await anim_refund_once("upload_exception")
            return

        # Видео <= 48 МБ - отправляем через Telegram
        video_sent = False
        try:
            await safe_send_media(
                cb.message.answer_video,
                BufferedInputFile(vid_bytes, "animation.mp4"),
                caption=(
                    f"✅ Готово! 🏃 Анимация фото\n"
                    f"💵 Списано {anim_cost} кр | Остаток: {cr_left} кр\n\n"
                    f"👇 Ниже - оригинал без сжатия"
                ),
                reply_markup=kb_after_anim,
                supports_streaming=True,
                op_name="anim_video",
            )
            video_sent = True
        except Exception as ve:
            logging.error(f"anim answer_video failed: {ve}")

        # Если видео не отправилось - возвращаем кредиты и выходим
        if not video_sent:
            await anim_refund_once("video_send_failed")
            try:
                await cb.message.answer(
                    f"⚠️ Видео сгенерировано ({size_mb:.1f} МБ), но не отправилось в Telegram.\n"
                    f"Кредиты возвращены 💳\n"
                    f"Напиши @neirosetkaalex - пришлём файл напрямую.",
                    reply_markup=kb_back(),
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return

        # 2. Оригинал без сжатия
        if size_mb < 48:
            try:
                await safe_send_media(
                    bot.send_document,
                    chat_id=cb.message.chat.id,
                    document=BufferedInputFile(vid_bytes, "animation_original.mp4"),
                    caption="📁 <b>Оригинал без сжатия</b> - скачай для максимального качества",
                    parse_mode="HTML",
                    disable_content_type_detection=True,
                    op_name="anim_document",
                )
            except Exception as de:
                logging.error(f"anim send_document failed ({size_mb:.1f} MB): {de}")
        else:
            try:
                upload_url = await upload_large_file(vid_bytes, "animation_original.mp4")
                if upload_url:
                    await cb.message.answer(
                        f"📁 <b>Оригинал без сжатия</b>\n\n"
                        f"Файл {size_mb:.1f} МБ - слишком большой для Telegram.\n"
                        f"Скачай по ссылке (доступна 24 часа):\n"
                        f"<a href='{upload_url}'>{upload_url}</a>",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                else:
                    await cb.message.answer(
                        f"📁 Оригинал ({size_mb:.1f} МБ) - слишком большой для Telegram.\n"
                        f"Напиши @neirosetkaalex - пришлём файл напрямую."
                    )
            except Exception as up_err:
                logging.error(f"upload_large_file anim failed: {up_err}")
    except Exception as e:
        await anim_refund_once(f"exception:{type(e).__name__}")
        await notify_admin_error(f"Анимация фото uid={uid}", e)
        try:
            await wait.edit_text(
                f"⚠️ {friendly_error(e)}\n\nКредиты возвращены.",
                reply_markup=kb_back()
            )
        except Exception as msg_err:
            logging.warning(f"anim error message failed: {msg_err}")
    finally:
        await unmark_generation_active(uid)


# ══════════════════════════════════════════════════════════
#  🎭 KLING MOTION CONTROL (через EvoLink)
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "menu_motion")
async def menu_motion(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cr = await get_credits(cb.from_user.id)
    min_price = min(MOTION_PRICES.values())

    text = (
        "🎭 <b>Motion Control (Kling 3.0)</b>\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n"
        f"💵 Стоимость: от <b>{min_price} кр</b>\n\n"
        "<b><i>🎬 Перенос движения и эмоций с видео на твоего персонажа</i></b>\n\n"
        "📸 <b>Шаг 1</b> - фото персонажа (кого анимируем)\n"
        "🎥 <b>Шаг 2</b> - видео с движениями/эмоциями\n"
        "⏱ <b>Шаг 3</b> - длительность (5/8/10 сек)\n"
        "✏️ <b>Шаг 4</b> - описание фона (опционально)\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "💡 <b>Для лучшего результата:</b>\n\n"
        "<b>Фото персонажа:</b>\n"
        "• чёткое, хорошо освещённое\n"
        "• видно всё тело или верхнюю часть\n"
        "• один человек в кадре\n"
        "• без обрезанных частей\n\n"
        "<b>Видео-референс:</b>\n"
        "• 3–30 сек, один человек в кадре\n"
        "• без резких склеек и движений камеры\n"
        "• чёткие движения (танец, жесты, мимика)\n"
        "• тот же ракурс что у фото (полный рост ↔ полный рост)\n\n"
        "⏱ <i>Генерация 3–10 минут</i>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "Готов? 👇"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="▶️ Начать", callback_data="mot_start")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="vband:kling")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "mot_start")
async def mot_start(cb: CallbackQuery, state: FSMContext):
    # Проверяем минимальный баланс (5 сек = 299 кр)
    cr = await get_credits(cb.from_user.id)
    min_price = min(MOTION_PRICES.values())
    if cr < min_price:
        try:
            await cb.message.edit_text(
                f"❌ Недостаточно кредитов\nНужно минимум {min_price} кр, у тебя {cr} кр.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [_eib("Купить кредиты", "menu_buy")],
                    [_eib("Главное меню", "back_main")],
                ])
            )
        except Exception:
            await cb.message.answer(f"❌ Недостаточно кредитов. Нужно {min_price} кр.")
        await cb.answer()
        return

    # Проверка что EvoLink API настроен
    if not EVOLINK_API_KEY:
        await cb.answer("⚙️ Функция временно недоступна. Напиши @neirosetkaalex", show_alert=True)
        return

    await state.set_state(MotionState.waiting_image)
    await cb.message.edit_text(
        "🎭 <b>Motion Control - шаг 1/4</b>\n\n"
        "📸 <b>Отправь фото персонажа</b>\n\n"
        "<i>Кого будем анимировать? Загрузи фото одного человека (или мультяшного героя) - "
        "на него будут перенесены движения с видео.</i>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )
    await cb.answer()


@dp.message(MotionState.waiting_image, F.photo | F.document)
async def mot_got_image(message: Message, state: FSMContext):
    # Принимаем фото (photo) или документ (uncompressed)
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("image/"):
        file_id = message.document.file_id
    else:
        await message.answer("📸 Отправь именно фото - JPG или PNG.")
        return

    await state.update_data(image_file_id=file_id)
    await state.set_state(MotionState.waiting_video)
    await message.answer(
        "✅ Фото принято!\n\n"
        "🎭 <b>Motion Control - шаг 2/4</b>\n\n"
        "🎥 <b>Отправь видео-референс</b>\n\n"
        "<i>Видео с движениями/эмоциями которые нужно перенести на персонажа.\n\n"
        "Требования:\n"
        "• длительность 3–30 сек\n"
        "• один человек в кадре\n"
        "• чёткие движения без резких склеек\n"
        "• тот же ракурс что у фото</i>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )


@dp.message(MotionState.waiting_image)
async def mot_image_wrong(message: Message):
    await message.answer("📸 Отправь фото персонажа (JPG или PNG), чтобы продолжить.")


@dp.message(MotionState.waiting_video, F.video | F.video_note | F.document)
async def mot_got_video(message: Message, state: FSMContext):
    if message.video:
        video = message.video
        file_id = video.file_id
        duration_sec = video.duration or 0
    elif message.video_note:
        video = message.video_note
        file_id = video.file_id
        duration_sec = video.duration or 0
    elif message.document and message.document.mime_type and message.document.mime_type.startswith("video/"):
        file_id = message.document.file_id
        duration_sec = 0  # не знаем, доверимся API
    else:
        await message.answer("🎥 Отправь именно видео - MP4, MOV.")
        return

    # Проверяем длительность (если известна)
    if duration_sec and (duration_sec < 3 or duration_sec > 30):
        await message.answer(
            f"⚠️ Длительность видео - <b>{duration_sec} сек</b>.\n"
            f"Нужно <b>от 3 до 30 секунд</b>. Загрузи другое видео.",
            parse_mode="HTML"
        )
        return

    # Проверяем размер (Telegram limit 20MB для bot downloads без session)
    file_size = getattr(message.video, "file_size", None) or getattr(message.document, "file_size", None) or 0
    if file_size and file_size > 20 * 1024 * 1024:
        await message.answer(
            f"⚠️ Файл слишком большой ({file_size // 1024 // 1024} МБ).\n"
            f"Максимум: 20 МБ. Сожми видео или уменьши разрешение.",
        )
        return

    await state.update_data(video_file_id=file_id)
    await state.set_state(MotionState.waiting_duration)

    # Показываем выбор длительности с ценами
    rows = []
    for dur, price in sorted(MOTION_PRICES.items()):
        rows.append([InlineKeyboardButton(
            text=f"⏱ {dur} секунд · {price} кр",
            callback_data=f"mot_dur:{dur}"
        )])
    rows.append([_eib("Главное меню", "back_main")])

    await message.answer(
        "✅ Видео принято!\n\n"
        "🎭 <b>Motion Control - шаг 3/4</b>\n\n"
        "⏱ <b>Выбери длительность видео:</b>\n\n"
        "<i>Чем длиннее - тем больше движений войдёт, но и дороже.</i>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML"
    )


@dp.message(MotionState.waiting_video)
async def mot_video_wrong(message: Message):
    await message.answer("🎥 Отправь видео (MP4/MOV), чтобы продолжить.")


@dp.callback_query(F.data.startswith("mot_dur:"), MotionState.waiting_duration)
async def mot_got_duration(cb: CallbackQuery, state: FSMContext):
    try:
        dur = int(cb.data.split(":")[1])
    except (ValueError, IndexError):
        await cb.answer("Ошибка"); return

    if dur not in MOTION_PRICES:
        await cb.answer("Неверная длительность"); return

    cr = await get_credits(cb.from_user.id)
    price = MOTION_PRICES[dur]
    if cr < price:
        await cb.answer(
            f"💸 Нужно {price} кр для {dur} секунд. У тебя {cr} кр.",
            show_alert=True
        )
        return

    await state.update_data(duration=dur, price=price)
    await state.set_state(MotionState.waiting_prompt)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Пропустить промт", callback_data="mot_skip_prompt")],
        [_eib("Главное меню", "back_main")],
    ])
    await cb.message.edit_text(
        f"✅ Выбрано: {dur} секунд · {price} кр\n\n"
        "🎭 <b>Motion Control - шаг 4/4</b>\n\n"
        "✏️ <b>Опиши сцену/фон (опционально)</b>\n\n"
        "<i>Движения будут взяты с видео, а этим промтом ты можешь задать фон, стиль, "
        "освещение или любые детали.\n\n"
        "Примеры:\n"
        "• Neon-lit Tokyo street at night, cinematic\n"
        "• Bright sunny beach, warm golden hour\n"
        "• Professional studio with soft lighting\n\n"
        "Или нажми «Пропустить» - будет использован фон с фото.</i>",
        reply_markup=kb, parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data == "mot_skip_prompt", MotionState.waiting_prompt)
async def mot_skip_prompt(cb: CallbackQuery, state: FSMContext):
    await state.update_data(prompt="")
    await _mot_confirm_and_run(cb.message, state, cb.from_user.id, edit=True)
    await cb.answer()


@dp.message(MotionState.waiting_prompt)
async def mot_got_prompt(message: Message, state: FSMContext):
    prompt = await _extract_prompt(message)
    ok_v, err = validate_gen_prompt(prompt) if prompt else (True, "")
    if not ok_v:
        await message.answer(err)
        return
    await state.update_data(prompt=prompt)
    await _mot_confirm_and_run(message, state, message.from_user.id, edit=False)


async def _mot_confirm_and_run(msg_obj, state: FSMContext, uid: int, edit: bool):
    """Запускает генерацию Motion Control после получения всех параметров."""
    data = await state.get_data()
    image_file_id = data.get("image_file_id")
    video_file_id = data.get("video_file_id")
    duration = data.get("duration", 8)
    price = data.get("price", MOTION_PRICES.get(duration, 349))
    prompt = data.get("prompt", "")

    if not image_file_id or not video_file_id:
        await msg_obj.answer("⚠️ Не хватает данных. Начни заново через меню.")
        await state.clear()
        return

    # Rate limit
    if not await _check_can_generate(msg_obj, uid, kind="motion"):
        await state.clear()
        return

    # Проверка баланса ещё раз (мог измениться)
    cr = await get_credits(uid)
    if cr < price:
        await state.clear()
        await msg_obj.answer(f"❌ Недостаточно кредитов. Нужно {price} кр, у тебя {cr}.")
        return

    # Списываем
    ok = await deduct(uid, price)
    if not ok:
        await state.clear()
        await msg_obj.answer("❌ Ошибка списания. Попробуй ещё раз.")
        return

    await mark_generation_active(uid, "motion")
    await state.clear()

    wait_text = (
        f"⏳ Запускаю Motion Control...\n\n"
        f"🎭 Kling 3.0 | {duration} сек | 720p\n"
        + (f"<i>{prompt[:80]}</i>\n" if prompt else "")
        + f"\n⏱ Обычно 3–10 минут. Пришлю как только готово 👇"
    )
    if edit:
        try:
            wait = await msg_obj.edit_text(wait_text, parse_mode="HTML")
        except Exception:
            wait = await msg_obj.answer(wait_text, parse_mode="HTML")
    else:
        wait = await msg_obj.answer(wait_text, parse_mode="HTML")

    # Защита от двойного возврата кредитов
    motion_refunded = False

    async def motion_refund_once(reason: str = ""):
        nonlocal motion_refunded
        if motion_refunded:
            logging.warning(f"motion_refund_once SKIPPED uid={uid} reason={reason}")
            return
        motion_refunded = True
        await add_credits(uid, price)
        logging.info(f"motion_refund_once EXECUTED uid={uid} credits={price} reason={reason}")

    try:
        # Получаем публичные URL файлов Telegram (EvoLink сам скачает)
        image_url = await _tg_file_public_url(image_file_id)
        video_url = await _tg_file_public_url(video_file_id)

        # Запускаем генерацию (без retry - safety блоки не ретраятся, а ошибки API итак долгие)
        vid_bytes = await api_kling_motion_control(
            image_url=image_url,
            video_url=video_url,
            duration=duration,
            prompt=prompt,
            aspect_ratio="16:9",
        )
        size_mb = len(vid_bytes) / 1024 / 1024
        logging.info(f"Motion Control ready: {len(vid_bytes)} bytes ({size_mb:.1f} MB)")
        await log_gen(uid, "motion", MOTION_MODEL_ID, price)
        _record_generation(uid, _motion_history)
        cr_left = await get_credits(uid)

        kb_after_mot = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Ещё раз", callback_data="menu_motion"),
             _eib("Главное меню", "back_main")],
            [InlineKeyboardButton(text="❤️ В избранное", callback_data="fav_save"),
             _eib("Купить кредиты", "menu_buy")],
        ])

        # Удаляем прогресс-сообщение ПЕРЕД отправкой (чистый UX)
        try:
            await wait.delete()
        except Exception:
            pass

        # Если видео > 48 МБ - сразу загружаем на хостинг, не пытаемся через Telegram
        if size_mb > 48:
            try:
                await msg_obj.answer("⏳ Видео большое, загружаю на хостинг...")
                upload_url = await upload_large_file(vid_bytes, "motion_control_original.mp4")
                if upload_url:
                    await msg_obj.answer(
                        f"🎭 <b>Готово! Motion Control · {duration} сек</b>\n"
                        f"💸 Списано {price} кр | Остаток: {cr_left} кр\n\n"
                        f"📁 Файл {size_mb:.1f} МБ - слишком большой для Telegram.\n"
                        f"Скачай оригинал по ссылке (доступна 24 часа):\n"
                        f"<a href='{upload_url}'>{upload_url}</a>",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                        reply_markup=kb_after_mot,
                    )
                else:
                    # Загрузка не удалась - возвращаем кредиты
                    await motion_refund_once("upload_failed")
                    await msg_obj.answer(
                        f"⚠️ Видео сгенерировано ({size_mb:.1f} МБ), но не удалось доставить.\n"
                        f"Кредиты возвращены 💳\n"
                        f"Напиши @neirosetkaalex - пришлём файл напрямую.",
                        reply_markup=kb_back(),
                    )
            except Exception as up_err:
                logging.error(f"motion large file handling failed: {up_err}")
                await motion_refund_once("upload_exception")
                await msg_obj.answer(
                    f"⚠️ Не удалось доставить видео. Кредиты возвращены 💳\n"
                    f"Напиши @neirosetkaalex - разберёмся.",
                    reply_markup=kb_back(),
                )
            return  # Дальше не идём - документ уже обработан выше

        # Видео <= 48 МБ - отправляем через Telegram
        video_sent = False
        try:
            await safe_send_media(
                bot.send_video,
                chat_id=msg_obj.chat.id,
                video=BufferedInputFile(vid_bytes, "motion_control.mp4"),
                caption=(
                    f"🎭 Готово! Motion Control · {duration} сек\n"
                    f"💸 Списано {price} кр | Остаток: {cr_left} кр\n\n"
                    f"👇 Ниже - оригинал без сжатия"
                ),
                reply_markup=kb_after_mot,
                supports_streaming=True,
                op_name="motion_video",
            )
            video_sent = True
        except Exception as ve:
            logging.error(f"motion send_video failed: {ve}")

        if not video_sent:
            await motion_refund_once("video_send_failed")
            try:
                await msg_obj.answer(
                    f"⚠️ Видео сгенерировано ({size_mb:.1f} МБ), но не отправилось в Telegram.\n"
                    f"Кредиты возвращены 💳\n"
                    f"Напиши @neirosetkaalex - пришлём файл напрямую.",
                    reply_markup=kb_back(),
                    parse_mode="HTML"
                )
            except Exception:
                pass
            return

        # 2. Оригинал без сжатия
        if size_mb < 48:
            try:
                await safe_send_media(
                    bot.send_document,
                    chat_id=msg_obj.chat.id,
                    document=BufferedInputFile(vid_bytes, "motion_control_original.mp4"),
                    caption="📁 <b>Оригинал без сжатия</b> - максимальное качество",
                    parse_mode="HTML",
                    disable_content_type_detection=True,
                    op_name="motion_document",
                )
            except Exception as de:
                logging.error(f"Motion Control send_document failed ({size_mb:.1f} MB): {de}")
        else:
            try:
                upload_url = await upload_large_file(vid_bytes, "motion_control_original.mp4")
                if upload_url:
                    await msg_obj.answer(
                        f"📁 <b>Оригинал без сжатия</b>\n\n"
                        f"Файл {size_mb:.1f} МБ - слишком большой для Telegram.\n"
                        f"Скачай по ссылке (доступна 24 часа):\n"
                        f"<a href='{upload_url}'>{upload_url}</a>",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                else:
                    await msg_obj.answer(
                        f"📁 Оригинал ({size_mb:.1f} МБ) - слишком большой для Telegram.\n"
                        f"Напиши @neirosetkaalex - пришлём файл напрямую."
                    )
            except Exception as up_err:
                logging.error(f"upload_large_file motion failed: {up_err}")

    except Exception as e:
        await motion_refund_once(f"exception:{type(e).__name__}")
        await notify_admin_error(f"Motion Control uid={uid} duration={duration}", e)
        try:
            await wait.edit_text(
                f"⚠️ {friendly_error(e)}\n\nКредиты возвращены.",
                reply_markup=kb_back()
            )
        except Exception:
            try:
                await msg_obj.answer(
                    f"⚠️ {friendly_error(e)}\n\nКредиты возвращены.",
                    reply_markup=kb_back()
                )
            except Exception as msg_err:
                logging.warning(f"motion error message failed: {msg_err}")
    finally:
        await unmark_generation_active(uid)


# ══════════════════════════════════════════════════════════
#  ОБЫЧНЫЕ СООБЩЕНИЯ (вне FSM - консультант по умолчанию)
# ══════════════════════════════════════════════════════════

# ── Admin команды GPT кодов ──────────────────────────────────────────────────

