# -*- coding: utf-8 -*-
# Auto-split module "handlers_chat" — part of Neirosetkaa-bot (refactored from bot.py).
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
    CHAT_PRESETS, bot, detect_consultant_intent, dp,
)
from states import (
    ChatState,
)
from keyboards import (
    _eib, kb_after_consultant_reply, kb_chat_presets,
)
from common import (
    _send_long_reply, claude_with_search,
)

@dp.callback_query(F.data == "menu_chat")
async def menu_chat(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ChatState.chatting)
    await cb.message.edit_text(
        "🤖 <b>AI-Консультант</b>\n\n"
        "Я эксперт по нейросетям, VPN и промптингу.\n"
        "Помогу составить промт, настроить VPN, выбрать подходящую нейросеть.\n\n"
        "Это <b>бесплатно</b> 🎁\n\n"
        "<b>Выбери быстрый пресет</b> или просто напиши свой вопрос 👇",
        reply_markup=kb_chat_presets(), parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data == "chat_presets_again")
async def chat_presets_again(cb: CallbackQuery, state: FSMContext):
    """Показать пресеты снова во время диалога."""
    await state.set_state(ChatState.chatting)
    await cb.message.answer(
        "📋 <b>Быстрые пресеты</b>\n\n"
        "Или просто напиши вопрос своими словами 👇",
        reply_markup=kb_chat_presets(), parse_mode="HTML"
    )
    await cb.answer()


# Скрытые сообщения-пресеты - отправляются в Claude как будто юзер написал
@dp.callback_query(F.data == "chat_free_question")
async def chat_free_question(cb: CallbackQuery, state: FSMContext):
    """Клиент хочет задать свой вопрос - просим его написать."""
    await state.set_state(ChatState.chatting)
    try:
        await cb.message.edit_text(
            "💬 <b>Задай свой вопрос</b>\n\n"
            "Я помогу с:\n"
            "• Настройкой любой нейросети или VPN\n"
            "• Промтами для фото и видео\n"
            "• Сравнением тарифов и моделей\n"
            "• Оформлением подписок в рублях\n\n"
            "<i>Просто напиши что интересует 👇</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 Вернуться к пресетам", callback_data="chat_presets_again")],
                [_eib("Главное меню", "back_main")],
            ]),
            parse_mode="HTML",
        )
    except Exception:
        await cb.message.answer(
            "💬 <b>Задай свой вопрос</b>\n\n<i>Просто напиши что интересует 👇</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📋 К пресетам", callback_data="chat_presets_again")],
                [_eib("Главное меню", "back_main")],
            ]),
            parse_mode="HTML",
        )
    await cb.answer()


@dp.callback_query(F.data.startswith("chat_preset:"))
async def chat_preset_handler(cb: CallbackQuery, state: FSMContext):
    """Обработчик клика по пресету - отправляет заранее заготовленный запрос в Claude."""
    preset_key = cb.data.split(":", 1)[1]
    preset_message = CHAT_PRESETS.get(preset_key)
    if not preset_message:
        await cb.answer()
        return

    await state.set_state(ChatState.chatting)
    # Показываем юзеру что он "выбрал"
    preset_labels = {
        "prompt_img": "🎨 Помоги с промтом для фото",
        "prompt_vid": "🎬 Помоги с промтом для видео",
        "vpn":        "🛡 Настройка VPN",
        "register":   "📱 Как зарегистрироваться в нейросети",
        "compare":    "⚖️ Сравнить нейросети",
        "choose":     "💡 Что выбрать для моей задачи",
    }
    label = preset_labels.get(preset_key, "Пресет")
    try:
        # Оставляем клавиатуру с пресетами - чтобы юзер мог выбрать другой пока ждёт
        await cb.message.edit_text(
            f"<i>Ты выбрал: {label}</i>\n\n⏳ Готовлю ответ...",
            parse_mode="HTML",
            reply_markup=kb_chat_presets(),
        )
    except Exception:
        pass

    await cb.answer("Готовлю ответ...")
    await bot.send_chat_action(cb.message.chat.id, "typing")
    uid = cb.from_user.id

    try:
        reply = await claude_with_search(uid, preset_message)
    except Exception as e:
        logging.error(f"chat_preset_handler claude call failed: {e}")
        reply = (
            "⚠️ Не удалось получить ответ от консультанта.\n\n"
            "Попробуй ещё раз через минуту или выбери другой пресет."
        )

    # Детект намерения для умной кнопки под ответом
    intent, model_hint = detect_consultant_intent(preset_message, reply)
    kb = kb_after_consultant_reply(intent, model_hint)
    # Отправляем с разбивкой на части - клавиатура всегда на последнем куске
    await _send_long_reply(cb.message, reply, reply_markup=kb)


@dp.callback_query(F.data == "help_choose")
async def help_choose(cb: CallbackQuery, state: FSMContext):
    """Устаревший хэндлер - редиректит на пресет 'choose'."""
    cb.data = "chat_preset:choose"
    await chat_preset_handler(cb, state)


@dp.message(ChatState.chatting, ~F.text.startswith("/"))
async def chat_message(message: Message, state: FSMContext):
    if not message.text:
        return
    # Команды не перехватываем - передаём дальше
    if message.text.startswith("/"):
        return
    await bot.send_chat_action(message.chat.id, "typing")
    uid = message.from_user.id
    reply = await claude_with_search(uid, message.text)

    # Детект намерения - если клиент явно хочет что-то сгенерировать,
    # добавим кнопку «Сгенерировать это в боте»
    intent, model_hint = detect_consultant_intent(message.text, reply)
    kb = kb_after_consultant_reply(intent, model_hint)
    # Отправляем с разбивкой на части - клавиатура всегда на последнем куске
    await _send_long_reply(message, reply, reply_markup=kb)


