# -*- coding: utf-8 -*-
# Auto-split module "keyboards" — part of Neirosetkaa-bot (refactored from bot.py).
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
    ALTERNATIVE_MODELS, ANIM_MODELS, CREDIT_PACKS, CUSTOM_EMOJI_IDS, DISABLED_MODELS, EDIT_MODELS,
    IMAGE_BRAND_MODELS, IMAGE_MODELS, PERSONAL_USERNAME, SHOP_CATEGORIES, UI_EMOJI_IDS, VIDEO_BRAND_MODELS,
    VIDEO_MODELS, is_admin,
)

def kb_error_with_alt(menu: str, model_key: str):
    """Клавиатура для сообщения об ошибке с альтернативой и кнопкой Назад."""
    rows = []
    alt_data = ALTERNATIVE_MODELS.get(menu, {}).get(model_key)
    if alt_data:
        alt_menu, alt_key, _alt_desc = alt_data
        # Получаем имя альтернативной модели
        if alt_menu == "img" and alt_key in IMAGE_MODELS:
            alt_name = IMAGE_MODELS[alt_key]["name"]
            alt_credits = IMAGE_MODELS[alt_key]["credits"]
            rows.append([InlineKeyboardButton(
                text=f"💡 Попробовать {alt_name} ({alt_credits} кр)",
                callback_data=f"alt_img:{alt_key}"
            )])
        elif alt_menu == "vid" and alt_key in VIDEO_MODELS:
            alt_name = VIDEO_MODELS[alt_key]["name"]
            alt_credits = VIDEO_MODELS[alt_key]["credits"]
            rows.append([InlineKeyboardButton(
                text=f"💡 Попробовать {alt_name} ({alt_credits} кр)",
                callback_data=f"alt_vid:{alt_key}"
            )])
    rows.append([InlineKeyboardButton(text="🔄 Попробовать ту же модель", callback_data=f"retry_{menu}:{model_key}")])
    rows.append([_eib("Главное меню", "back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [_eib("Изображение",   "menu_image"),    _eib("Видео",     "menu_video")],
        [_eib("Редактировать...","menu_edit"),    _eib("Анимировать...","menu_anim")],
        [_eib("Консультант AI", "menu_chat"),     _eib("Избранное", "menu_favorites")],
        [_eib("Купить кредиты","menu_buy"),       _eib("Магазин",   "menu_shop")],
        [_eib("Мой профиль",   "menu_profile")],
    ])

def kb_image_brands():
    """Верхний уровень: выбор бренда моделей."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [_eib("GPT Image",    "iband:gptimg",   "iband_gptimg")],
        [_eib("Imagen",       "iband:imagen",   "iband_imagen")],
        [_eib("Nano Banana",  "iband:nano",     "iband_nano")],
        [_eib("Flux",         "iband:flux",     "iband_flux")],
        [_eib("Ideogram",     "iband:ideogram", "iband_ideogram")],
        [_eib("Grok Imagine", "iband:grok",     "iband_grok")],
        [_eib("Улучшить фото","menu_upscale")],
        [_eib("Главное меню", "back_main")],
    ])


# Маппинг бренда → ключи моделей (по возрастанию кредитов)
def kb_image_models_for_brand(brand: str):
    """Подменю конкретного бренда: список его моделей."""
    import re as _re
    brand_eid = UI_EMOJI_IDS.get(f"iband_{brand}", "")
    keys = IMAGE_BRAND_MODELS.get(brand, [])
    rows = []
    for key in keys:
        if key in IMAGE_MODELS and key not in DISABLED_MODELS:
            m = IMAGE_MODELS[key]
            clean_name = _re.sub(r'\s*\d+(\.\d+)*\s*', ' ', m['name']).strip()
            if brand_eid:
                # есть кастомная иконка → убираем старое эмодзи из названия (без дублей)
                clean_name = _re.sub(r'^[^\w\s]+\s*', '', clean_name).strip()
            btn_kwargs = {"icon_custom_emoji_id": brand_eid} if brand_eid else {}
            rows.append([InlineKeyboardButton(
                text=f"{clean_name} - {m['credits']} кр",
                callback_data=f"imodel:{key}",
                **btn_kwargs
            )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_img_brands")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Старое имя для обратной совместимости (используется в /again и т.п.)
def kb_image_models():
    return kb_image_brands()

def kb_video_brands():
    """Верхний уровень: выбор бренда видео-моделей."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [_eib("Veo",         "vband:veo",      "vband_veo")],
        [_eib("Kling",       "vband:kling",    "vband_kling")],
        [_eib("Seedance",    "vband:seedance", "vband_seedance")],
        [_eib("Wan",         "vband:wan",      "vband_wan")],
        [_eib("Grok",        "vband:grok",     "vband_grok")],
        [_eib("Главное меню","back_main")],
    ])


# Маппинг бренда → ключи моделей видео (по возрастанию кредитов)
def kb_video_models_for_brand(brand: str):
    """Подменю конкретного видео-бренда: список его моделей."""
    import re as _re
    brand_eid = UI_EMOJI_IDS.get(f"vband_{brand}", "")
    keys = VIDEO_BRAND_MODELS.get(brand, [])
    rows = []
    for key in keys:
        if key in VIDEO_MODELS and key not in DISABLED_MODELS:
            m = VIDEO_MODELS[key]
            clean_name = _re.sub(r'\s*\d+(\.\d+)*\s*', ' ', m['name']).strip()
            if brand_eid:
                # есть кастомная иконка → убираем старое эмодзи из названия (без дублей)
                clean_name = _re.sub(r'^[^\w\s]+\s*', '', clean_name).strip()
            btn_kwargs = {"icon_custom_emoji_id": brand_eid} if brand_eid else {}
            rows.append([InlineKeyboardButton(
                text=f"{clean_name} - {m['credits']} кр",
                callback_data=f"vmodel:{key}",
                **btn_kwargs
            )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_vid_brands")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# Старое имя для обратной совместимости
def kb_video_models():
    return kb_video_brands()

def kb_confirm(prefix: str, key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Генерировать", callback_data=f"go:{prefix}:{key}"),
            InlineKeyboardButton(text="✍️ Изменить промт", callback_data=f"chprompt:{prefix}:{key}"),
        ],
        [InlineKeyboardButton(text="✨ Улучшить промт с AI", callback_data=f"improve_prompt:{prefix}:{key}")],
        [_eib("Главное меню", "back_main")],
    ])

def kb_buy():
    rows = []
    for key, p in CREDIT_PACKS.items():
        eid = UI_EMOJI_IDS.get(f"pack_{key}", "")
        # Strip leading emoji from name since icon_custom_emoji_id is set separately
        raw_name = p['name'].split(' ', 1)[-1] if ' ' in p['name'] else p['name']
        btn = InlineKeyboardButton(
            text=f"{raw_name} - {p['credits']} кредитов | {p['price']}₽",
            callback_data=f"buy:{key}",
            **{"icon_custom_emoji_id": eid} if eid else {}
        )
        rows.append([btn])
    rows.append([_eib("Я оплатил, но не пришло", "payment_issue")])
    rows.append([_eib("Главное меню", "back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_pay_method(pack_key: str):
    p = CREDIT_PACKS[pack_key]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"СБП - {p['price']}₽",
            callback_data=f"payfk:{pack_key}:sbp",
            **pay_btn_kwargs()
        )],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_buy")],
    ])

def kb_after(menu: str, model_key: str = ""):
    rows = [
        [
            InlineKeyboardButton(text="🔄 Ещё раз",       callback_data=f"again:{menu}:{model_key}"),
            InlineKeyboardButton(text="🎯 Сменить модель", callback_data=f"menu_{menu}"),
        ],
        [
            InlineKeyboardButton(text="❤️ В избранное",    callback_data="fav_save"),
            _eib("Главное меню", "back_main"),
        ],
        [_eib("Купить кредиты", "menu_buy")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_cancel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [_eib("Главное меню", "back_main")]
    ])


def kb_chat_presets():
    """Быстрые пресеты при входе в консультанта - типичные вопросы одним кликом."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [_eib("Помоги с промтом для фото",      "chat_preset:prompt_img", "chat_prompt_img")],
        [_eib("Помоги с промтом для видео",     "chat_preset:prompt_vid", "chat_prompt_vid")],
        [_eib("Настройка VPN",                  "chat_preset:vpn",        "chat_vpn")],
        [_eib("Как зарегистрироваться в нейросети", "chat_preset:register", "chat_register")],
        [_eib("Сравнить нейросети",             "chat_preset:compare",    "chat_compare")],
        [_eib("Другой вопрос",                  "chat_free_question",     "chat_other")],
        [_eib("Главное меню",                   "back_main")],
    ])


def kb_chat_ongoing():
    """Клавиатура во время активного диалога - чтобы можно было вернуться к пресетам."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Быстрые пресеты", callback_data="chat_presets_again")],
        [_eib("Главное меню", "back_main")],
    ])


def kb_after_consultant_reply(intent: str | None = None, model_hint: str | None = None):
    """Умная клавиатура под ответом консультанта.

    Всегда ведёт на МЕНЮ ВЫБОРА МОДЕЛИ, не на конкретную - клиент должен
    сам выбрать что использовать. model_hint игнорируется (оставлен в сигнатуре
    для обратной совместимости).

    intent: 'image' | 'video' | 'edit' | 'animate' | None
    """
    rows = []
    if intent == "image":
        rows.append([InlineKeyboardButton(
            text="🎨 Сгенерировать фото в боте",
            callback_data="menu_image"
        )])
    elif intent == "video":
        rows.append([InlineKeyboardButton(
            text="🎬 Сгенерировать видео в боте",
            callback_data="menu_video"
        )])
    elif intent == "edit":
        rows.append([InlineKeyboardButton(
            text="✏️ Отредактировать фото в боте",
            callback_data="menu_edit"
        )])
    elif intent == "animate":
        rows.append([InlineKeyboardButton(
            text="🏃 Анимировать фото в боте",
            callback_data="menu_anim"
        )])
    rows.append([InlineKeyboardButton(text="📋 Пресеты", callback_data="chat_presets_again"),
                 _eib("Главное меню", "back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_aspect_image(model_key: str):
    """Выбор формата для изображений."""
    ratios = [
        ("1:1 Квадрат",    "1:1"),
        ("16:9 Широкий",   "16:9"),
        ("9:16 Сторис",    "9:16"),
        ("4:3 Фото",       "4:3"),
        ("3:4 Портрет",    "3:4"),
    ]
    rows = []
    for i in range(0, len(ratios), 2):
        row = []
        for label, ratio in ratios[i:i+2]:
            row.append(InlineKeyboardButton(
                text=label,
                callback_data=f"iaspect:{model_key}:{ratio}"
            ))
        rows.append(row)
    rows.append([_eib("Главное меню", "back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_aspect_video(model_key: str):
    """Выбор формата для видео."""
    ratios = [
        ("16:9 Горизонталь", "16:9"),
        ("9:16 Вертикаль",   "9:16"),
        ("1:1 Квадрат",      "1:1"),
    ]
    rows = [[InlineKeyboardButton(text=label, callback_data=f"vaspect:{model_key}:{ratio}") for label, ratio in ratios]]
    rows.append([_eib("Главное меню", "back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [_eib("Главное меню", "back_main")]
    ])

def kb_contact():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💌 Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}")]
    ])


def kb_reply(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Постоянная нижняя панель кнопок."""
    rows = [
        [KeyboardButton(text="📷 Создать фото"), KeyboardButton(text="🎬 Создать видео")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🏡 Главное меню")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🛠️ Админ панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, persistent=True)

# ══════════════════════════════════════════════════════════
#  FSM СОСТОЯНИЯ
# ══════════════════════════════════════════════════════════

def _eib(text: str, callback_data: str, eid_key: str = "") -> "InlineKeyboardButton":
    """Создаёт InlineKeyboardButton с icon_custom_emoji_id из UI_EMOJI_IDS."""
    eid = UI_EMOJI_IDS.get(eid_key or callback_data, "")
    if eid:
        return InlineKeyboardButton(text=text, callback_data=callback_data, icon_custom_emoji_id=eid)
    return InlineKeyboardButton(text=text, callback_data=callback_data)


def _is_emoji_char(ch: str) -> bool:
    """True если первый символ — настоящий эмодзи. tg-emoji требует внутри РОВНО один
    эмодзи, иначе Telegram отклоняет всё сообщение (баг с '𝕏' у Grok)."""
    if not ch:
        return False
    o = ord(ch[0])
    return (
        0x1F300 <= o <= 0x1FAFF or 0x2600 <= o <= 0x27BF or 0x2190 <= o <= 0x21FF
        or 0x1F000 <= o <= 0x1F0FF or 0x1F1E6 <= o <= 0x1F1FF
        or o in (0x2B50, 0x2B55, 0x2705, 0x274C, 0x2764, 0x203C, 0x2049, 0x2122, 0x2139)
    )


def tg_emoji_ui(key: str, fallback: str = "") -> str:
    """Возвращает <tg-emoji> тег для UI-элемента по ключу из UI_EMOJI_IDS."""
    eid = UI_EMOJI_IDS.get(key, "")
    if eid and _is_emoji_char(fallback):
        return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'
    return fallback


def tg_emoji(svc: dict, fallback: str = "") -> str:
    """Возвращает <tg-emoji> тег если есть emoji_id и валидный фолбэк-эмодзи, иначе текст."""
    fb  = fallback or svc.get("emoji", "")
    key = svc.get("_key", "")
    eid = svc.get("emoji_id") or CUSTOM_EMOJI_IDS.get(key, "")
    if eid and _is_emoji_char(fb):
        return f'<tg-emoji emoji-id="{eid}">{fb}</tg-emoji>'
    return fb


def _btn_emoji_id(key: str, s: dict) -> str:
    """Возвращает emoji_id для icon_custom_emoji_id в InlineKeyboardButton, или ''."""
    return s.get("emoji_id") or CUSTOM_EMOJI_IDS.get(key, "") or CUSTOM_EMOJI_IDS.get(s.get("_key", ""), "")


def pay_btn_kwargs() -> dict:
    """Кастомное эмодзи для кнопок оплаты — то же, что на кнопке «Купить кредиты»."""
    eid = UI_EMOJI_IDS.get("menu_buy", "")
    return {"icon_custom_emoji_id": eid} if eid else {}


def _shop_back_cat(key: str) -> str:
    for _, title, keys_list in SHOP_CATEGORIES:
        if key in keys_list:
            return title.replace(" ", "_").lower()
    return "чат_и_текст"


def kb_vid_duration(model_key: str):
    """Клавиатура выбора длительности для Kling-моделей."""
    m = VIDEO_MODELS.get(model_key, {})
    durations = m.get("durations", {})
    rows = []
    # Сортируем по возрастанию длительности
    for sec in sorted(durations.keys()):
        credits, price = durations[sec]
        rows.append([InlineKeyboardButton(
            text=f"🎬 {sec} секунд - {credits} кр",
            callback_data=f"vdur:{model_key}:{sec}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_video")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_admin_panel():
    _gpt = {"icon_custom_emoji_id": CUSTOM_EMOJI_IDS["chatgpt"]} if CUSTOM_EMOJI_IDS.get("chatgpt") else {}
    _cl  = {"icon_custom_emoji_id": CUSTOM_EMOJI_IDS["claude"]} if CUSTOM_EMOJI_IDS.get("claude") else {}
    _ap  = {"icon_custom_emoji_id": CUSTOM_EMOJI_IDS["appstore"]} if CUSTOM_EMOJI_IDS.get("appstore") else {}
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Статистика",          callback_data="adm_stat_menu",      icon_custom_emoji_id="5190481218600189255"),
         InlineKeyboardButton(text="Аналитика",           callback_data="adm_analytics_menu", icon_custom_emoji_id="5296265790654264117")],
        [InlineKeyboardButton(text="💰 Прибыль / себестоимость", callback_data="adm_profit")],
        [InlineKeyboardButton(text="Найти по ID",         callback_data="adm_find",           icon_custom_emoji_id="5389071547065457874"),
         InlineKeyboardButton(text="💳 Балансы",          callback_data="adm_balance_menu")],
        [InlineKeyboardButton(text="🧾 История платежей",  callback_data="adm_payments"),
         InlineKeyboardButton(text="Блокировки",          callback_data="adm_blocks",         icon_custom_emoji_id="5361709792186351461")],
        [InlineKeyboardButton(text="🎟 Промокоды",         callback_data="adm_promos"),
         InlineKeyboardButton(text="🤖 Модели",           callback_data="adm_models")],
        [InlineKeyboardButton(text="🛍 Продажи магазина",  callback_data="adm_shop_sales"),
         InlineKeyboardButton(text="💵 Редактор цен",      callback_data="adm_prices")],
        [InlineKeyboardButton(text="Приветствие",         callback_data="adm_welcome",        icon_custom_emoji_id="5190859184312167965"),
         InlineKeyboardButton(text="Рассылка",            callback_data="adm_broadcast",      icon_custom_emoji_id="5907027384439148391")],
        [InlineKeyboardButton(text="Техобслуживание",     callback_data="adm_maintenance",    icon_custom_emoji_id="5458865216597012027")],
        [InlineKeyboardButton(text="ChatGPT Mini App",    callback_data="adm_gpt_webapp", **_gpt)],
        [InlineKeyboardButton(text="Claude Mini App",     callback_data="adm_claude_webapp", **_cl)],
        [InlineKeyboardButton(text="Настройка App Store", callback_data="adm_nsgifts", **_ap)],
        [_eib("Главное меню", "back_main")],
    ])

def kb_block_actions(target_id: int, currently_blocked: bool):
    action = "adm_unblock" if currently_blocked else "adm_block"
    label = "✅ Разблокировать" if currently_blocked else "🚫 Заблокировать"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"{action}:{target_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_blocks")],
    ])


def kb_stat_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Сегодня",      callback_data="adm_stat_day"),
         InlineKeyboardButton(text="📆 7 дней",        callback_data="adm_stat_week")],
        [InlineKeyboardButton(text="🗓 30 дней",       callback_data="adm_stat_month"),
         InlineKeyboardButton(text="🔎 Выбрать день",  callback_data="adm_stat_pick")],
        [InlineKeyboardButton(text="◀️ Панель",        callback_data="adm_back")],
    ])

def kb_balance_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Начислить кредиты", callback_data="adm_give_credits"),
         InlineKeyboardButton(text="📉 Расход по юзеру",   callback_data="adm_spend")],
        [InlineKeyboardButton(text="🔍 Аудит всех юзеров", callback_data="adm_bal_audit_all")],
        [InlineKeyboardButton(text="👤 Аудит юзера по ID",  callback_data="adm_bal_audit_one")],
        [InlineKeyboardButton(text="✏️ Установить баланс", callback_data="adm_bal_set")],
        [InlineKeyboardButton(text="➖ Снять кредиты",     callback_data="adm_bal_deduct")],
        [InlineKeyboardButton(text="🔧 Исправить все автоматом", callback_data="adm_bal_fix_all")],
        [InlineKeyboardButton(text="⬅️ Назад в админку",    callback_data="adm_back")],
    ])


def _all_models_map():
    return {"image": IMAGE_MODELS, "video": VIDEO_MODELS, "edit": EDIT_MODELS, "anim": ANIM_MODELS}

def _section_label(s: str) -> str:
    return {"image": "📷 Фото", "video": "🎬 Видео", "edit": "🖌 Редактирование", "anim": "🏃 Анимация"}.get(s, s)

