# -*- coding: utf-8 -*-
"""Старые кнопки консультанта больше не вызывают Claude.

Каталог и кнопка «Написать Александру» остаются. Продажа и активация
подписок Claude живут в handlers_claude.py и сюда не входят.
"""
from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from common import CONSULT_MOVED_TEXT, _assistant_off_kb
from config import dp
from states import ChatState


async def _show_moved(message):
    kb = _assistant_off_kb()
    try:
        await message.edit_text(CONSULT_MOVED_TEXT, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await message.answer(CONSULT_MOVED_TEXT, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "menu_chat")
async def menu_chat(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_moved(cb.message)
    await cb.answer()


@dp.callback_query(F.data == "chat_presets_again")
async def chat_presets_again(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_moved(cb.message)
    await cb.answer()


@dp.callback_query(F.data == "chat_free_question")
async def chat_free_question(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_moved(cb.message)
    await cb.answer()


@dp.callback_query(F.data.startswith("chat_preset:"))
async def chat_preset_handler(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_moved(cb.message)
    await cb.answer()


@dp.callback_query(F.data == "help_choose")
async def help_choose(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_moved(cb.message)
    await cb.answer()


@dp.message(ChatState.chatting, ~F.text.startswith("/"))
async def chat_message(message: Message, state: FSMContext):
    if not message.text or message.text.startswith("/"):
        return
    await state.clear()
    await message.answer(CONSULT_MOVED_TEXT, reply_markup=_assistant_off_kb(), parse_mode="HTML")
