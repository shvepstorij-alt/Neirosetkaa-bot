# -*- coding: utf-8 -*-
# Auto-split module "states" — part of Neirosetkaa-bot (refactored from bot.py).
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


class ImgState(StatesGroup):
    waiting_aspect = State()
    waiting_prompt = State()

class EditState(StatesGroup):
    waiting_photo  = State()
    waiting_prompt = State()
    waiting_confirm = State()

class AnimState(StatesGroup):
    waiting_mode       = State()   # выбор режима (1 или 2 кадра)
    waiting_first_photo = State()  # первый кадр
    waiting_last_photo  = State()  # последний кадр (если 2 кадра)
    waiting_aspect     = State()   # формат
    waiting_prompt     = State()   # промт
    waiting_confirm    = State()   # подтверждение

class VidState(StatesGroup):
    waiting_duration = State()   # выбор длительности (только для Kling)
    waiting_aspect = State()
    waiting_prompt = State()

class MotionState(StatesGroup):
    waiting_image    = State()   # референс-фото персонажа
    waiting_video    = State()   # референс-видео с движением
    waiting_duration = State()   # выбор длительности (5/8/10)
    waiting_prompt   = State()   # опциональный промт сцены

class UpscaleState(StatesGroup):
    waiting_photo = State()

class ImproveState(StatesGroup):
    waiting_prompt = State()   # текстовый промт для улучшения
    waiting_model  = State()   # выбор модели после улучшения

class ChatState(StatesGroup):
    chatting = State()

class AdminState(StatesGroup):
    waiting_user_id   = State()
    waiting_credits   = State()
    waiting_block_id  = State()
    waiting_find_user = State()
    waiting_broadcast = State()
    waiting_welcome   = State()
    waiting_spend_uid = State()
    # Управление балансами
    waiting_balance_uid      = State()   # введи UID (для любой операции с балансом)
    waiting_balance_set      = State()   # введи новую сумму (операция "установить")
    waiting_balance_deduct   = State()   # введи сколько снять (операция "снять")
    # Статистика за произвольный день
    waiting_stat_date        = State()   # введи дату в формате ДД.ММ.ГГГГ
    # Калькулятор прибыли
    waiting_plan_cost        = State()   # цена закупа тарифа в $
    waiting_cost_rate        = State()   # курс закупа доллара

    # Премиум-рефералка
    waiting_refp_pct         = State()   # глобальный %
    waiting_refp_cap         = State()   # месячный лимит, ₽
    waiting_refp_add         = State()   # добавить партнёра: "UID" или "UID %"
    waiting_refp_del         = State()   # убрать партнёра: ввод UID

    # Оплата по ссылке (link-pay)
    waiting_linkpay_clarify  = State()   # текст уточнения клиенту
    waiting_linkpay_instr    = State()   # ввод инструкции по сервису

# ══════════════════════════════════════════════════════════
#  СИСТЕМНЫЙ ПРОМТ + ВЕБ-ПОИСК
# ══════════════════════════════════════════════════════════

class ShopPromoState(StatesGroup):
    waiting_code = State()


class PromoState(StatesGroup):
    waiting_code = State()


class AdmPromoState(StatesGroup):
    waiting_code = State()
    waiting_kind = State()
    waiting_value = State()
    waiting_uses = State()
    waiting_days = State()





# ── РЕДАКТОР ЦЕН ──────────────────────────────────────────────────────────────

class AdminEditState(StatesGroup):
    waiting_value = State()


class GptAdminState(StatesGroup):
    waiting_codes = State()  # ожидаем коды для добавления
    waiting_plan  = State()  # ожидаем план


class ClaudeAdminState(StatesGroup):
    waiting_codes = State()
    waiting_plan  = State()


class PerplexityAdminState(StatesGroup):
    waiting_codes = State()
    waiting_plan  = State()


class AdmNsgState(StatesGroup):
    waiting_rate      = State()
    waiting_markup    = State()
    waiting_threshold = State()


class LinkPayState(StatesGroup):
    waiting_link = State()   # клиент присылает ссылку на оплату
