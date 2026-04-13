import asyncio
import logging
import asyncpg
import aiohttp
import base64
import os
import re
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
import anthropic
import hashlib
from aiohttp import web
from dotenv import load_dotenv

load_dotenv()

# ─── Конфиг ───────────────────────────────────────────────
BOT_TOKEN      = os.getenv("BOT_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
CHANNEL_ID     = os.getenv("CHANNEL_ID")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "AleksandrOii")      # канал
PERSONAL_USERNAME = os.getenv("PERSONAL_USERNAME", "neirosetkaalex")  # личка Александра
ADMIN_ID       = int(os.getenv("ADMIN_ID", "0"))

# ─── FreeKassa ────────────────────────────────────────────
FK_MERCHANT_ID = os.getenv("FK_MERCHANT_ID", "")
FK_SECRET_1    = os.getenv("FK_SECRET_1", "")
FK_SECRET_2    = os.getenv("FK_SECRET_2", "")
FK_WEBHOOK_PORT = int(os.getenv("PORT", "8080"))  # Railway использует PORT

FREE_CREDITS   = 50  # кредитов при первом /start
DATABASE_URL   = os.getenv("DATABASE_URL")  # Railway PostgreSQL

_pool = None  # глобальный connection pool

logging.basicConfig(level=logging.INFO)

bot           = Bot(token=BOT_TOKEN)
dp            = Dispatcher(storage=MemoryStorage())
claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

user_conversations = {}   # история чата с консультантом
user_orig_images = {}     # оригинальные байты последнего фото {user_id: bytes}

# ─── Модели изображений ───────────────────────────────────
IMAGE_MODELS = {
    # ── Imagen 4 ──────────────────────────────────────────
    "img_fast": {
        "name": "⚡ Imagen 4 Fast",
        "model_id": "imagen-4.0-fast-generate-001",
        "api": "imagen",
        "credits": 7,
        "price": "4₽",
        "speed": "~2 сек",
        "desc": "Быстро и качественно",
    },
    "img_std": {
        "name": "✨ Imagen 4",
        "model_id": "imagen-4.0-generate-001",
        "api": "imagen",
        "credits": 10,
        "price": "6₽",
        "speed": "~5 сек",
        "desc": "Флагман, чёткий текст",
    },
    "img_ultra": {
        "name": "💎 Imagen 4 Ultra",
        "model_id": "imagen-4.0-ultra-generate-001",
        "api": "imagen",
        "credits": 13,
        "price": "8₽",
        "speed": "~8 сек",
        "desc": "Максимальная точность",
    },
    # ── Nano Banana (Gemini Image) ─────────────────────────
    "nb_flash": {
        "name": "🍌 Nano Banana",
        "model_id": "gemini-2.5-flash-image",
        "api": "gemini",
        "credits": 10,
        "price": "5₽",
        "speed": "~3 сек",
        "desc": "Быстрый, диалоговый",
    },
    "nb_2": {
        "name": "🍌✨ Nano Banana 2",
        "model_id": "gemini-3.1-flash-image-preview",
        "api": "gemini",
        "credits": 13,
        "price": "6₽",
        "speed": "~4 сек",
        "desc": "Новейший, лучшее качество",
    },
    "nb_pro": {
        "name": "🍌💎 Nano Banana Pro",
        "model_id": "gemini-3-pro-image-preview",
        "api": "gemini",
        "credits": 30,
        "price": "14₽",
        "speed": "~8 сек",
        "desc": "4K, точный текст в картинке",
    },
}

# ─── Модели видео ─────────────────────────────────────────
VIDEO_MODELS = {
    "vid_lite": {
        "name": "💰 Veo 3.1 Lite",
        "model_id": "veo-3.1-lite-generate-preview",
        "credits": 100,
        "price": "60₽",
        "res": "720p",
        "desc": "Бюджет, быстро",
    },
    "vid_fast": {
        "name": "⚡ Veo 3.1 Fast",
        "model_id": "veo-3.1-fast-generate-preview",
        "credits": 175,
        "price": "120₽",
        "res": "1080p",
        "desc": "Баланс цены и качества",
    },
    "vid_pro": {
        "name": "🎬 Veo 3.1",
        "model_id": "veo-3.1-generate-preview",
        "credits": 390,
        "price": "225₽",
        "res": "4K + аудио",
        "desc": "Кино-качество",
    },
}

# ─── Пакеты кредитов ──────────────────────────────────────
CREDIT_PACKS = {
    "p25": {
        "name": "🎯 Пробный", "credits": 250, "price": 149, "stars": 30,
        "desc": "35 фото / 2 видео Lite / 1 видео Fast",
        "badge": "Попробовать за 149₽",
    },
    "p50": {
        "name": "🥉 Старт", "credits": 500, "price": 279, "stars": 56,
        "desc": "70 фото / 5 видео Lite / 2 видео Fast / 1 видео Pro",
        "badge": "Популярный старт",
    },
    "p150": {
        "name": "🥈 Базовый", "credits": 1500, "price": 799, "stars": 160,
        "desc": "210 фото / 15 видео Lite / 8 видео Fast / 3 видео Pro",
        "badge": "Хорошая экономия",
    },
    "p500": {
        "name": "🥇 Про", "credits": 5000, "price": 2490, "stars": 498,
        "desc": "700 фото / 50 видео Lite / 28 видео Fast / 12 видео Pro",
        "badge": "Выгоднее на 13%",
    },
    "p1200": {
        "name": "💎 Бизнес", "credits": 12000, "price": 5790, "stars": 1160,
        "desc": "1700 фото / 120 видео Lite / 68 видео Fast / 30 видео Pro",
        "badge": "Максимум",
    },
}

REF_BONUS = 200  # кредитов за реферал

# ══════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ (PostgreSQL через asyncpg)
# ══════════════════════════════════════════════════════════

async def get_pool():
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL не задан! Добавь переменную в Railway.")
        # Railway PostgreSQL требует SSL
        _pool = await asyncpg.create_pool(
            DATABASE_URL,
            min_size=1,
            max_size=5,
            ssl="require",
            statement_cache_size=0,  # совместимость с pgbouncer
        )
        logging.info("✅ PostgreSQL pool создан")
    return _pool

async def init_db():
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id        BIGINT PRIMARY KEY,
                credits        INTEGER DEFAULT 0,
                is_blocked     INTEGER DEFAULT 0,
                username       TEXT DEFAULT '',
                full_name      TEXT DEFAULT '',
                last_active    TIMESTAMP DEFAULT NOW(),
                created_at     TIMESTAMP DEFAULT NOW(),
                referred_by    BIGINT DEFAULT NULL,
                ref_bonus_paid BOOLEAN DEFAULT FALSE
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS generations (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT,
                type       TEXT,
                model      TEXT,
                credits    INTEGER,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id         SERIAL PRIMARY KEY,
                user_id    BIGINT,
                credits    INTEGER,
                amount_rub INTEGER,
                method     TEXT,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS payments_fk (
                id         SERIAL PRIMARY KEY,
                order_id   TEXT UNIQUE,
                user_id    BIGINT,
                credits    INTEGER,
                amount_rub INTEGER,
                pack_key   TEXT,
                status     TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT NOW()
            )
        """)
        for col, dfn in [("referred_by","BIGINT DEFAULT NULL"),("ref_bonus_paid","BOOLEAN DEFAULT FALSE")]:
            try:
                await conn.execute(f"ALTER TABLE users ADD COLUMN {col} {dfn}")
            except Exception:
                pass
        # Дефолтные настройки
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ('maintenance', '0') ON CONFLICT DO NOTHING"
        )
    logging.info("✅ PostgreSQL инициализирован")

async def ensure_user(user_id: int, username: str = "", full_name: str = "", referred_by: int = None):
    pool = await get_pool()
    async with pool.acquire() as conn:
        if referred_by and referred_by != user_id:
            await conn.execute("""
                INSERT INTO users (user_id, credits, username, full_name, referred_by)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (user_id) DO UPDATE
                SET username=$3, full_name=$4, last_active=NOW()
            """, user_id, REF_BONUS, username, full_name, referred_by)
        else:
            await conn.execute("""
                INSERT INTO users (user_id, credits, username, full_name)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (user_id) DO UPDATE
                SET username=$3, full_name=$4, last_active=NOW()
            """, user_id, FREE_CREDITS, username, full_name)

async def get_setting(key: str, default: str = "") -> str:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key=$1", key)
        return row["value"] if row else default

async def set_setting(key: str, value: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value=$2",
            key, value
        )

async def get_user(user_id: int) -> dict | None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM users WHERE user_id=$1", user_id)
        return dict(row) if row else None

async def get_credits(user_id: int) -> int:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT credits FROM users WHERE user_id=$1", user_id)
        return row["credits"] if row else 0

async def log_payment(user_id: int, credits: int, amount_rub: int, method: str):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO payments (user_id, credits, amount_rub, method) VALUES ($1,$2,$3,$4)",
            user_id, credits, amount_rub, method
        )

async def deduct(user_id: int, amount: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT credits FROM users WHERE user_id=$1 FOR UPDATE", user_id
            )
            if not row or row["credits"] < amount:
                return False
            await conn.execute(
                "UPDATE users SET credits = credits - $1 WHERE user_id = $2",
                amount, user_id
            )
    return True

async def add_credits(user_id: int, amount: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET credits = credits + $1 WHERE user_id = $2",
            amount, user_id
        )

async def block_user(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_blocked=1 WHERE user_id=$1", user_id)

async def unblock_user(user_id: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("UPDATE users SET is_blocked=0 WHERE user_id=$1", user_id)

async def is_blocked(user_id: int) -> bool:
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT is_blocked FROM users WHERE user_id=$1", user_id)
        return bool(row and row["is_blocked"])

async def log_gen(user_id: int, gen_type: str, model: str, credits: int):
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO generations (user_id, type, model, credits) VALUES ($1,$2,$3,$4)",
            user_id, gen_type, model, credits
        )

# ══════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎨 Изображение", callback_data="menu_image"),
            InlineKeyboardButton(text="🎥 Видео",        callback_data="menu_video"),
        ],
        [
            InlineKeyboardButton(text="🖌️ Редактировать фото", callback_data="menu_edit"),
            InlineKeyboardButton(text="🎞️ Анимировать фото",   callback_data="menu_anim"),
        ],
        [
            InlineKeyboardButton(text="🤖 Консультант AI", callback_data="menu_chat"),
        ],
        [
            InlineKeyboardButton(text="💎 Баланс",         callback_data="menu_balance"),
            InlineKeyboardButton(text="⚡ Купить кредиты", callback_data="menu_buy"),
        ],
        [
            InlineKeyboardButton(text="🤝 Пригласить друга", callback_data="menu_ref"),
        ],
        [
            InlineKeyboardButton(text="💌 Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}"),
        ],
    ])

def kb_image_models():
    imagen_keys = ["img_fast", "img_std", "img_ultra"]
    nano_keys   = ["nb_flash", "nb_2", "nb_pro"]
    rows = []
    for key in imagen_keys:
        m = IMAGE_MODELS[key]
        rows.append([InlineKeyboardButton(
            text=f"{m['name']} — {m['credits']} кр",
            callback_data=f"imodel:{key}"
        )])
    # Разделитель
    rows.append([InlineKeyboardButton(text="─── 🍌 Nano Banana ───", callback_data="noop")])
    for key in nano_keys:
        m = IMAGE_MODELS[key]
        rows.append([InlineKeyboardButton(
            text=f"{m['name']} — {m['credits']} кр",
            callback_data=f"imodel:{key}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_video_models():
    rows = []
    for key, m in VIDEO_MODELS.items():
        rows.append([InlineKeyboardButton(
            text=f"{m['name']} — {m['credits']} кредитов",
            callback_data=f"vmodel:{key}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_confirm(prefix: str, key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🚀 Генерировать", callback_data=f"go:{prefix}:{key}"),
            InlineKeyboardButton(text="✍️ Изменить промт",    callback_data=f"chprompt:{prefix}:{key}"),
        ],
        [InlineKeyboardButton(text="🚫 Отмена", callback_data="back_main")],
    ])

def kb_buy():
    rows = []
    for key, p in CREDIT_PACKS.items():
        rows.append([InlineKeyboardButton(
            text=f"{p['name']} — {p['credits']} кредитов | {p['price']}₽",
            callback_data=f"buy:{key}"
        )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_pay_method(pack_key: str):
    p = CREDIT_PACKS[pack_key]
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🏦 СБП / Карта — {p['price']}₽",
            callback_data=f"payfk:{pack_key}"
        )],
        [InlineKeyboardButton(
            text=f"⭐ Telegram Stars — {p['stars']} ⭐",
            callback_data=f"paystars:{pack_key}"
        )],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_buy")],
    ])

def kb_after(menu: str, model_key: str = ""):
    rows = [
        [
            InlineKeyboardButton(text="🍌 Ещё раз",      callback_data=f"again:{menu}:{model_key}"),
            InlineKeyboardButton(text="🎯 Сменить модель", callback_data=f"menu_{menu}"),
        ],
        [
            InlineKeyboardButton(text="🏡 Главное", callback_data="new_main"),
        ],
        [InlineKeyboardButton(text="⚡ Купить кредиты", callback_data="menu_buy")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_cancel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Отмена", callback_data="back_main")]
    ])


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
    rows.append([InlineKeyboardButton(text="🚫 Отмена", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def kb_aspect_video(model_key: str):
    """Выбор формата для видео."""
    ratios = [
        ("16:9 Горизонталь", "16:9"),
        ("9:16 Вертикаль",   "9:16"),
        ("1:1 Квадрат",      "1:1"),
    ]
    rows = [[InlineKeyboardButton(text=label, callback_data=f"vaspect:{model_key}:{ratio}") for label, ratio in ratios]]
    rows.append([InlineKeyboardButton(text="🚫 Отмена", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏡 Главное меню", callback_data="back_main")]
    ])

def kb_contact():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💌 Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}")]
    ])


def kb_reply(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Постоянная нижняя панель кнопок."""
    rows = [
        [KeyboardButton(text="✨ Создать фото"), KeyboardButton(text="🎥 Создать видео")],
        [KeyboardButton(text="🪪 Мой профиль"), KeyboardButton(text="🏡 Главное меню")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="🛠️ Админ панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, persistent=True)

# ══════════════════════════════════════════════════════════
#  FSM СОСТОЯНИЯ
# ══════════════════════════════════════════════════════════

class ImgState(StatesGroup):
    waiting_aspect = State()
    waiting_prompt = State()

class EditState(StatesGroup):
    waiting_photo  = State()
    waiting_prompt = State()

class AnimState(StatesGroup):
    waiting_mode       = State()   # выбор режима (1 или 2 кадра)
    waiting_first_photo = State()  # первый кадр
    waiting_last_photo  = State()  # последний кадр (если 2 кадра)
    waiting_aspect     = State()   # формат
    waiting_prompt     = State()   # промт

class VidState(StatesGroup):
    waiting_aspect = State()
    waiting_prompt = State()

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

# ══════════════════════════════════════════════════════════
#  СИСТЕМНЫЙ ПРОМТ + ВЕБ-ПОИСК
# ══════════════════════════════════════════════════════════

SYSTEM_PROMPT = """Ты — AI-ассистент Telegram бота Александра (@AleksandrOii).

ГЛАВНОЕ — ТЫ РАБОТАЕШЬ ВНУТРИ БОТА КОТОРЫЙ УМЕЕТ:
- Генерировать изображения (Imagen 4) — кнопка "🎨 Изображение" в меню
- Создавать видео (Veo 3.1) — кнопка "🎥 Видео" в меню
- Оформлять подписки на любые нейросети — оплата в рублях, без иностранных карт

Если спрашивают "можешь создать изображение/видео?" — отвечай:
"Да! Нажми кнопку 🖼️ Изображение в главном меню — и создашь прямо здесь. Напиши /start если не видишь меню."

НИКОГДА не говори что не умеешь создавать изображения или видео.

ВАЖНО — РАСПОЗНАВАНИЕ НАЗВАНИЙ:
Когда пользователь пишет "гамма" или "gamma" — это Gamma AI (gamma.app), нейросеть для создания презентаций, документов и лендингов.
Когда пишет "гемини" или "джемини" — это Gemini (Google AI).
Когда пишет "клод" или "клауд" — это Claude (Anthropic).
Когда пишет "чатгпт", "чат гпт", "гпт" — это ChatGPT (OpenAI).
Когда пишет "мидджорни", "миджорни" — это Midjourney.
Когда пишет "перплексити", "перплекс" — это Perplexity.
Когда пишет "курсор" — это Cursor (AI редактор кода).
Всегда отвечай в контексте нейросетей и AI-инструментов.

Gamma AI (gamma.app) — тарифы 2026:
Это AI-инструмент для создания презентаций, документов, лендингов и сайтов из текстового промта.
Free: 400 AI-кредитов при регистрации (разово), базовые шаблоны, экспорт с водяным знаком Gamma
Plus: $10/мес ($8/мес при годовой) — безлимитные генерации, без водяного знака, экспорт в PPTX/PDF, брендирование
Pro: $25/мес ($15-18/мес при годовой) — премиум AI-модели, кастомный брендинг, аналитика, API, 10 своих доменов
Ultra: вводная цена — самые продвинутые модели, 100 доменов, Studio Mode, ранний доступ к фичам
Teams: $20/польз/мес — командная работа
Кредиты: 1 презентация ≈ 40 кредитов. Неиспользованные кредиты переходят до 2x лимита плана.
Российские карты не принимаются — нужен посредник или зарубежная карта.

ФОРМАТИРОВАНИЕ — СТРОГО:
- Используй HTML теги для выделения: <b>жирный текст</b>
- НЕ используй звёздочки ** никогда — только <b>тег</b>
- Максимум 3-4 предложения на ответ
- Никаких длинных списков
- Если можно коротко — пиши коротко
- Пиши на русском, используй эмодзи умеренно

АКТУАЛЬНЫЕ МОДЕЛИ (апрель 2026):
Claude: Sonnet 4.5 (быстрая), Sonnet 4.6 (новейшая), Opus 4 (максимум) — все доступны в Claude Pro $20/мес
ChatGPT: GPT-5, GPT-5.3, GPT-5.4 (новейшие) — GPT-4o ВЫВЕДЕН из обращения в феврале 2026
Grok: Grok 3 (free), Grok 3.5 (SuperGrok Lite $10), Grok 4 (SuperGrok $30), Grok 4 Heavy ($300)
Gemini: Gemini 2.5 Flash, Gemini 3.1 Pro — в Google One AI Premium $20/мес

━━━━━━━━━━━━━━━━━━━━━━
ВАЖНО: АКТУАЛЬНОСТЬ ИНФОРМАЦИИ
━━━━━━━━━━━━━━━━━━━━━━
У тебя есть инструмент web_search. ВСЕГДА используй его когда клиент спрашивает про:
- тарифы, цены, планы любого сервиса
- новые модели или функции
- сравнение сервисов
- что нового в какой-либо нейросети

Алгоритм: сначала поищи актуальную информацию, потом отвечай.
Запросы для поиска делай на русском: "[сервис] тарифы 2026" или "[сервис] новые модели 2026"

━━━━━━━━━━━━━━━━━━━━━━
АКТУАЛЬНЫЕ ТАРИФЫ (апрель 2026)
━━━━━━━━━━━━━━━━━━━━━━

ChatGPT (OpenAI):
Модели: GPT-5, GPT-5.3, GPT-5.4 (GPT-4o выведен из обращения в феврале 2026)
Free — базовый GPT-5.3 с лимитами (10 сообщений каждые 5 часов), с рекламой
Go — $8/мес, больше лимитов, но нет Sora/Codex/Deep Research, есть реклама — не рекомендуется
Plus — $20/мес, лучший выбор: полный GPT-5, Sora, DALL-E/GPT Image, Deep Research (10/мес), Codex, Agent Mode, без рекламы
Pro — $200/мес: GPT-5.4 Pro, 250 Deep Research/мес, двойной контекст — для профессионалов

Claude (Anthropic):
Модели: Sonnet 4.5 (быстрая), Sonnet 4.6 (новейшая), Opus 4 (максимальное качество)
Free — Claude Sonnet 4.5 с лимитами
Pro — $20/мес: Claude Opus 4, Sonnet 4.6, Projects, большие документы, приоритет
Team — $25/мес/чел: совместная работа команды

Grok (xAI):
Модели: Grok 3, Grok 3.5, Grok 4, Grok 4 Heavy
Free — Grok 3 с лимитами (~10 запросов каждые 2 часа), Aurora генерация изображений
SuperGrok Lite — $10/мес: Grok 3.5 с расширенными лимитами
SuperGrok — $30/мес: Grok 4, DeepSearch, безлимит изображений, Big Brain Mode, голос
SuperGrok Heavy — $300/мес: Grok 4 Heavy, максимальное качество рассуждений
(Также через X Premium $8/мес и X Premium+ $40/мес — вместе с фичами соцсети X)
Особенность: самый низкий процент галлюцинаций (~4%), реальное время из X/Twitter, контекст 2М токенов

Cursor (AI-редактор кода):
Модели: GPT-5, Claude Sonnet 4.6, Gemini 3 и другие на выбор
Hobby — бесплатно: 2000 автодополнений/мес, базовый доступ
Pro — $20/мес ($16 при годовой оплате): безлимит Tab, $20 кредитов на AI-агенты, все топ-модели
Pro+ — $60/мес: 3x больше кредитов для активных пользователей
Ultra — $200/мес: 20x кредитов, для тех кто в Cursor весь рабочий день
Teams — $40/польз./мес: командный доступ, SSO, общий биллинг
Лучший AI-редактор кода в 2026 году

Krea AI (генерация изображений в реальном времени):
Free — лимитированный доступ
Pro — $35/мес: безлимит генераций, upscale, real-time режим, видео

Suno (генерация музыки):
Версия: v4.5 — студийное качество, все жанры
Free — несколько треков в день
Pro — $8/мес: 2500 кредитов, коммерческое использование
Premier — $24/мес: 10000 кредитов, приоритет

Kling AI (генерация видео):
Версия: Kling 2.1, Kling 3.0
Free — 66 кредитов/день
Standard — $8/мес: 660 кредитов/мес
Pro — $27/мес: 3000 кредитов/мес
Лучшее соотношение качество/цена для видео в 2026 году

Runway (генерация видео):
Версия: Gen-4 — кинематографическое качество
Free — 125 кредитов (разово)
Standard — $12/мес: 625 кредитов
Pro — $28/мес: 2250 кредитов
Лучше Kling по кинематографичности, но дороже

ElevenLabs (синтез речи и клонирование голоса):
Версия: движок v3 — неотличим от живого голоса, 70+ языков
Free — 10 000 символов/мес (≈10 мин аудио), без коммерческих прав
Starter — $5/мес: 30 000 символов, коммерческие права, клонирование голоса
Creator — $22/мес: 100 000 символов, профессиональное клонирование

HeyGen (AI-аватары и видео):
Free — ограниченный доступ
Creator — $24/мес: AI-аватары, перевод видео с сохранением голоса
Business — $72/мес: командный доступ, API

━━━━━━━━━━━━━━━━━━━━━━
TELEGRAM И VPN В России (апрель 2026)
━━━━━━━━━━━━━━━━━━━━━━
ВАЖНО: 4 апреля 2026 года Telegram официально заблокирован в России.
Павел Дуров подтвердил блокировку. Роскомнадзор ввёл ограничения поэтапно:
- август 2025 — заблокированы звонки
- февраль 2026 — замедление трафика по всей стране
- 1 апреля 2026 — полная блокировка

65 млн россиян продолжают пользоваться Telegram через обходы. Дуров обещал адаптировать трафик чтобы его было сложнее обнаружить.

Как обойти блокировку Telegram:
1. Встроенный прокси в Telegram: Настройки → Данные и хранилище → Прокси → включить
2. VPN: Outline на своём VPS (самый надёжный), Proton VPN, Windscribe
3. MTProto прокси — специальный протокол Telegram, труднее блокировать
Важно: обычные VPN-протоколы РКН научился блокировать, Outline/MTProto работают лучше

Для ChatGPT, Claude, Midjourney и других нейросетей тоже нужен VPN.
Лучшие варианты: Outline на своём VPS, Proton VPN, Windscribe, 1.1.1.1 (Warp)

━━━━━━━━━━━━━━━━━━━━━━
ЦЕНЫ АЛЕКСАНДРА (через @neirosetkaalex)
━━━━━━━━━━━━━━━━━━━━━━
Все подписки оформляются с оплатой в рублях/тенге, без иностранных карт.
ВАЖНО: для использования ChatGPT, Claude, Midjourney и других зарубежных сервисов нужен VPN — это отдельный вопрос от оплаты.

ChatGPT Plus — 2000₽/мес (GPT-5, Sora, DALL-E, Deep Research)
Claude Pro — 2000₽/мес (Claude Opus 4, Projects, большие документы)
Gemini Advanced — 2000₽/мес (Gemini 3.1 Pro, Deep Research, Google интеграции)
SuperGrok — 2000₽/мес (Grok 4, данные X в реальном времени, Aurora изображения)
Cursor Pro — 2300₽/мес (AI редактор кода, все топ-модели)
Midjourney Basic — 1000₽/мес (~200 изображений), Standard — 3000₽/мес (безлимит relax)
Kling AI Standard — 900₽/мес, Pro — 2700₽/мес (генерация видео до 2 мин)
Canva Pro — 1200₽/мес (безлимитные шаблоны, AI-инструменты)
ElevenLabs Starter — 600₽/мес, Creator — 2300₽/мес (клонирование голоса)
Perplexity Pro — 2000₽/мес (AI-поиск с источниками, GPT-5+Claude+Gemini)
HeyGen Creator — 2700₽/мес (AI-аватар, перевод видео с заменой губ)
Runway Standard — 1700₽/мес, Pro — 3700₽/мес (генерация видео Gen-4)
Suno Pro — 1000₽/мес, Premier — 3000₽/мес (генерация музыки с вокалом)
Lovable Pro — 2700₽/мес (создание веб-приложений из текста)
Gamma Plus — 1000₽/мес, Gamma Pro — 2300₽/мес (AI презентации, документы, лендинги)

━━━━━━━━━━━━━━━━━━━━━━
КАК ОФОРМИТЬ ПОДПИСКУ
━━━━━━━━━━━━━━━━━━━━━━
Написать Александру в личку: @neirosetkaalex
Назвать нужный сервис и тариф — он оформит быстро, оплата в рублях.
Не нужна иностранная карта — оплата в рублях. VPN для использования сервиса может потребоваться отдельно.

━━━━━━━━━━━━━━━━━━━━━━
ПРАВИЛА ОТВЕТОВ
━━━━━━━━━━━━━━━━━━━━━━
1. ВСЕГДА называй цены Александра из раздела "ЦЕНЫ АЛЕКСАНДРА" — это твои цены, не официальные!
2. Когда клиент спрашивает "сколько стоит" — сразу называй цену в рублях из прайса выше
3. НЕ ГОВОРИ "уточните цену у Александра" — цены уже известны, назови их
4. Если клиент не знает что выбрать — задай уточняющий вопрос: для чего нужна нейросеть?
5. Для оформления направляй к Александру: @neirosetkaalex (не @AleksandrOii — это канал)
6. Никогда не называй устаревшие модели как текущие — GPT-4o выведен, актуальны GPT-5.x
7. Используй web_search только для вопросов про функции/новости нейросетей, но не для цен — цены уже есть
"""

# Инструмент веб-поиска для Claude API
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
}

# ══════════════════════════════════════════════════════════
#  GOOGLE AI СЕРВИСЫ
# ══════════════════════════════════════════════════════════

async def api_generate_image(prompt: str, model_id: str, aspect_ratio: str = "1:1", api_type: str = "imagen") -> bytes:
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    async with aiohttp.ClientSession() as s:

        if api_type == "gemini":
            # ── Nano Banana (generateContent) ─────────────────
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:generateContent"
            payload = {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseModalities": ["IMAGE", "TEXT"],
                    "imageConfig": {"aspectRatio": aspect_ratio},
                }
            }
            async with s.post(url, json=payload, headers=headers) as r:
                if r.status != 200:
                    raise Exception(f"Nano Banana API {r.status}: {(await r.text())[:300]}")
                data = await r.json()
                # Ищем inline image в частях ответа
                for part in data.get("candidates", [{}])[0].get("content", {}).get("parts", []):
                    if "inlineData" in part:
                        return base64.b64decode(part["inlineData"]["data"])
                raise Exception("Nano Banana: изображение не найдено в ответе")

        else:
            # ── Imagen (predict) ──────────────────────────────
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:predict"
            payload = {
                "instances": [{"prompt": prompt}],
                "parameters": {
                    "sampleCount": 1,
                    "aspectRatio": aspect_ratio,
                    "safetyFilterLevel": "block_few",
                }
            }
            async with s.post(url, json=payload, headers=headers) as r:
                if r.status != 200:
                    raise Exception(f"Imagen API {r.status}: {(await r.text())[:200]}")
                data = await r.json()
                return base64.b64decode(data["predictions"][0]["bytesBase64Encoded"])


async def api_edit_image(image_bytes: bytes, prompt: str, aspect_ratio: str = "1:1") -> bytes:
    """Редактирование фото по референсу через Gemini. Пробует несколько моделей."""
    img_b64 = base64.b64encode(image_bytes).decode()
    # Список моделей — пробуем по очереди
    models = [
        "gemini-2.5-flash-image",
        "gemini-2.0-flash-exp-image-generation",
    ]
    payload = {
        "contents": [{
            "parts": [
                {"inlineData": {"mimeType": "image/jpeg", "data": img_b64}},
                {"text": prompt}
            ]
        }],
        "generationConfig": {"responseModalities": ["TEXT", "IMAGE"]}
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    last_error = None
    async with aiohttp.ClientSession() as s:
        for model in models:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            for attempt in range(3):  # 3 попытки на каждую модель
                try:
                    async with s.post(url, json=payload, headers=headers) as r:
                        if r.status == 503:
                            await asyncio.sleep(3 * (attempt + 1))
                            continue
                        if r.status != 200:
                            last_error = f"API {r.status}: {(await r.text())[:150]}"
                            break
                        data = await r.json()
                        for part in data["candidates"][0]["content"]["parts"]:
                            if "inlineData" in part:
                                return base64.b64decode(part["inlineData"]["data"])
                        last_error = "Gemini не вернул изображение. Попробуй другой промт."
                        break
                except Exception as e:
                    last_error = str(e)
                    await asyncio.sleep(2)
    raise Exception(last_error or "Все модели недоступны. Попробуй позже.")


async def api_animate_image(
    first_bytes: bytes,
    prompt: str,
    aspect_ratio: str = "16:9",
    last_bytes: bytes | None = None,
) -> bytes:
    """Анимация фото через Veo 3.1 (первый кадр + опционально последний)."""
    base = "https://generativelanguage.googleapis.com/v1beta"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    first_b64 = base64.b64encode(first_bytes).decode()
    instance = {
        "prompt": prompt,
        "image": {"bytesBase64Encoded": first_b64, "mimeType": "image/jpeg"},
    }
    params = {"durationSeconds": 8, "aspectRatio": aspect_ratio, "sampleCount": 1}
    if last_bytes:
        last_b64 = base64.b64encode(last_bytes).decode()
        instance["lastFrame"] = {"bytesBase64Encoded": last_b64, "mimeType": "image/jpeg"}

    payload = {"instances": [instance], "parameters": params}

    async with aiohttp.ClientSession() as s:
        async with s.post(
            f"{base}/models/veo-3.1-generate-preview:predictLongRunning",
            json=payload, headers=headers
        ) as r:
            if r.status != 200:
                raise Exception(f"Veo Anim API {r.status}: {(await r.text())[:200]}")
            op_data = await r.json()
            op_name = op_data.get("name")
            if not op_name:
                raise Exception(f"Veo Anim: нет operation name: {op_data}")
            logging.info(f"Veo Anim operation: {op_name}")

        for _ in range(72):
            await asyncio.sleep(5)
            async with s.get(f"{base}/{op_name}", headers=headers) as pr:
                if pr.status != 200:
                    continue
                pd = await pr.json()
                if not pd.get("done"):
                    continue
                if "error" in pd:
                    raise Exception(pd["error"].get("message", "Veo Anim error"))
                # Парсим ответ
                gen_resp = pd.get("response", {}).get("generateVideoResponse", {})
                samples = gen_resp.get("generatedSamples", [])
                if samples:
                    video = samples[0].get("video", {})
                    if video.get("bytesBase64Encoded"):
                        return base64.b64decode(video["bytesBase64Encoded"])
                    uri = video.get("uri") or video.get("videoUri")
                    if uri:
                        vid_headers = {"x-goog-api-key": GEMINI_API_KEY}
                        scheme = "https://storage.googleapis.com/" if uri.startswith("gs://") else None
                        url = uri.replace("gs://", "https://storage.googleapis.com/") if scheme else uri
                        async with s.get(url, headers=vid_headers) as vr:
                            data_bytes = await vr.read()
                            if len(data_bytes) > 1000:
                                return data_bytes
                logging.error(f"Veo Anim unknown response: {str(pd)[:300]}")
                raise Exception("Неизвестная структура ответа Veo Anim")
    raise Exception("Превышено время ожидания анимации (6 мин)")


async def api_generate_video(prompt: str, model_id: str, aspect_ratio: str = "16:9") -> bytes:
    base = "https://generativelanguage.googleapis.com/v1beta"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    payload = {
        "instances": [{"prompt": prompt}],
        "parameters": {"durationSeconds": 8, "aspectRatio": aspect_ratio, "sampleCount": 1}
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{base}/models/{model_id}:predictLongRunning",
                          json=payload, headers=headers) as r:
            if r.status != 200:
                raise Exception(f"Veo API {r.status}: {(await r.text())[:200]}")
            op_data = await r.json()
            op_name = op_data.get("name")
            logging.info(f"Veo operation started: {op_name}")
            if not op_name:
                raise Exception(f"Veo не вернул operation name: {op_data}")

        # polling до 6 минут
        for i in range(72):
            await asyncio.sleep(5)
            async with s.get(f"{base}/{op_name}", headers=headers) as pr:
                if pr.status != 200:
                    logging.warning(f"Poll status {pr.status}")
                    continue
                pd = await pr.json()
                if not pd.get("done"):
                    continue

                logging.info(f"Veo done response keys: {list(pd.keys())}")

                if "error" in pd:
                    raise Exception(pd["error"].get("message", "Veo error"))

                # Структура 1: response.predictions[]
                preds = pd.get("response", {}).get("predictions", [])
                if preds:
                    logging.info(f"Veo preds[0] keys: {list(preds[0].keys())}")
                    p = preds[0]
                    if p.get("bytesBase64Encoded"):
                        return base64.b64decode(p["bytesBase64Encoded"])
                    uri = p.get("videoUri") or p.get("gcsUri") or p.get("uri")
                    if uri and uri.startswith("https://"):
                        async with s.get(uri) as vr:
                            return await vr.read()
                    if uri:
                        raise Exception(f"GCS URI требует доп. настройки: {uri[:80]}")

                # Структура 2: response.generateVideoResponse.generatedSamples[]
                gen_resp = pd.get("response", {}).get("generateVideoResponse", {})
                samples = gen_resp.get("generatedSamples", [])
                if samples:
                    logging.info(f"Veo samples[0] keys: {list(samples[0].keys())}")
                    sample = samples[0]
                    # video.uri или video.bytesBase64Encoded
                    video = sample.get("video", {})
                    if video.get("bytesBase64Encoded"):
                        return base64.b64decode(video["bytesBase64Encoded"])
                    uri = video.get("uri") or video.get("videoUri")
                    logging.info(f"Veo video uri: {uri[:100] if uri else 'None'}")
                    if uri and uri.startswith("https://"):
                        vid_headers = {"x-goog-api-key": GEMINI_API_KEY}
                        async with s.get(uri, headers=vid_headers) as vr:
                            data_bytes = await vr.read()
                            logging.info(f"Veo video downloaded: {len(data_bytes)} bytes, status: {vr.status}")
                            if len(data_bytes) > 1000:
                                return data_bytes
                            raise Exception(f"Видео слишком маленькое ({len(data_bytes)} bytes). Попробуй ещё раз.")
                    if uri and uri.startswith("gs://"):
                        # Конвертируем GCS URI в HTTPS
                        https_uri = uri.replace("gs://", "https://storage.googleapis.com/")
                        async with s.get(https_uri) as vr:
                            data_bytes = await vr.read()
                            logging.info(f"Veo GCS download: {len(data_bytes)} bytes")
                            if len(data_bytes) > 1000:
                                return data_bytes
                    if uri:
                        raise Exception(f"Не удалось скачать видео: {uri[:80]}")
                    # Может быть напрямую в sample
                    if sample.get("bytesBase64Encoded"):
                        return base64.b64decode(sample["bytesBase64Encoded"])
                    uri = sample.get("uri") or sample.get("videoUri")
                    if uri and uri.startswith("https://"):
                        async with s.get(uri) as vr:
                            return await vr.read()

                # Структура 3: videos[] напрямую
                videos = pd.get("response", {}).get("videos", [])
                if videos:
                    v = videos[0]
                    if v.get("bytesBase64Encoded"):
                        return base64.b64decode(v["bytesBase64Encoded"])
                    uri = v.get("videoUri") or v.get("uri")
                    if uri and uri.startswith("https://"):
                        async with s.get(uri) as vr:
                            return await vr.read()

                # Структура 4: result.videos[]
                result_videos = pd.get("result", {}).get("videos", [])
                if result_videos:
                    v = result_videos[0]
                    if v.get("bytesBase64Encoded"):
                        return base64.b64decode(v["bytesBase64Encoded"])

                # Лог полного ответа для отладки
                resp_str = str(pd.get("response", pd))[:600]
                logging.error(f"Veo unknown response: {resp_str}")
                raise Exception(f"Неизвестная структура ответа Veo. Ключи: {list(pd.get('response', pd).keys())}")

    raise Exception("Превышено время ожидания (6 мин)")

# ══════════════════════════════════════════════════════════
#  ОБРАБОТЧИКИ — СТАРТ / МЕНЮ
# ══════════════════════════════════════════════════════════

WELCOME_NEW = """🌟 Привет, {name}!

Я — AI-ассистент Александра. Умею:
🖼️ Генерировать изображения (Imagen 4)
🎬 Создавать видео (Veo 3.1) 
💬 Консультировать по нейросетям и VPN
💳 Оформлять подписки без зарубежной карты

🎁 Тебе начислено <b>{credits} бесплатных кредитов</b>!

Выбери действие 👇"""

WELCOME_BACK = """👋 С возвращением, {name}!

💎 Баланс: <b>{credits} кредитов</b>

Выбери действие 👇"""


@dp.message(F.text.startswith("/start"), StateFilter("*"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    uid = message.from_user.id

    # Парсим реф-параметр: /start ref_123456
    parts = message.text.strip().split()
    referred_by = None
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            rid = int(parts[1][4:])
            if rid != uid:
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
            f"🌟 Привет, {message.from_user.first_name}!\n\n"
            f"🎁 Тебя пригласил друг — ты получил <b>+{REF_BONUS} бонусных кредитов!</b>\n\n"
            f"💎 Баланс: <b>{credits} кредитов</b>\n\n"
            f"Выбери что создать 👇"
        )
    else:
        text = (WELCOME_NEW if is_new else WELCOME_BACK).format(
            name=message.from_user.first_name,
            credits=credits
        )

    await message.answer("👇", reply_markup=kb_reply(is_admin))
    await message.answer(text, reply_markup=kb_main(), parse_mode="HTML")


async def show_admin_panel(message: Message):
    """Показать админ панель — используется и из /admin и из кнопки."""
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


@dp.message(F.text == "/admin", StateFilter("*"))
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    await state.clear()
    await show_admin_panel(message)


@dp.callback_query(F.data == "noop")
async def noop_handler(cb: CallbackQuery):
    await cb.answer()


@dp.callback_query(F.data == "back_main")
async def back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    credits = await get_credits(cb.from_user.id)
    await cb.message.edit_text(
        f"👋 {cb.from_user.first_name}, баланс: <b>{credits} кредитов</b>\n\nВыбери действие 👇",
        reply_markup=kb_main(), parse_mode="HTML"
    )
    await cb.answer()

# ══════════════════════════════════════════════════════════
#  БАЛАНС / ОПЛАТА
# ══════════════════════════════════════════════════════════

@dp.message(F.text == "/ref", StateFilter("*"))
async def cmd_ref(message: Message):
    """Команда /ref — показать реферальную ссылку."""
    uid = message.from_user.id
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_refs = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1", uid) or 0
        paid_refs  = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1 AND ref_bonus_paid=TRUE", uid) or 0
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{uid}"
    earned = paid_refs * REF_BONUS
    await message.answer(
        f"\U0001f91d <b>Пригласить друга</b>\n\n"
        f"За каждого друга — <b>+{REF_BONUS} кредитов</b> тебе и ему!\n\n"
        f"<b>Как работает:</b>\n"
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


@dp.callback_query(F.data == "menu_ref")
async def menu_ref(cb: CallbackQuery):
    uid = cb.from_user.id
    pool = await get_pool()
    async with pool.acquire() as conn:
        total_refs = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1", uid) or 0
        paid_refs  = await conn.fetchval("SELECT COUNT(*) FROM users WHERE referred_by=$1 AND ref_bonus_paid=TRUE", uid) or 0
    me = await bot.get_me()
    ref_link = f"https://t.me/{me.username}?start=ref_{uid}"
    earned = paid_refs * REF_BONUS
    text = (
        f"\U0001f91d <b>Пригласить друга</b>\n\n"
        f"За каждого друга — <b>+{REF_BONUS} кредитов</b> тебе и ему!\n\n"
        f"<b>Как работает:</b>\n"
        f"1\u20e3 Поделись своей ссылкой\n"
        f"2\u20e3 Друг регистрируется \u2192 он получает <b>+{REF_BONUS} кредитов</b>\n"
        f"3\u20e3 Друг делает первую покупку \u2192 ты получаешь <b>+{REF_BONUS} кредитов</b>\n\n"
        f"\U0001f4ca <b>Твоя статистика:</b>\n"
        f"\U0001f465 Приглашено: <b>{total_refs}</b>\n"
        f"\U0001f4b0 Купили: <b>{paid_refs}</b>\n"
        f"\U0001f381 Заработано: <b>{earned} кредитов ({earned * 5}\u20bd)</b>\n\n"
        f"\U0001f517 <b>Твоя ссылка:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"<i>Нажми на ссылку чтобы скопировать и отправь другу</i>"
    )
    try:
        await cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏡 Главное меню", callback_data="back_main")],
        ]), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏡 Главное меню", callback_data="back_main")],
        ]), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "menu_balance")
async def menu_balance(cb: CallbackQuery):
    cr = await get_credits(cb.from_user.id)
    lines = []
    for k, m in IMAGE_MODELS.items():
        icon = "🟢" if cr >= m['credits'] else "🔴"
        lines.append(f"{icon} {m['name']} — {m['credits']} кредитов")
    for k, m in VIDEO_MODELS.items():
        icon = "🟢" if cr >= m['credits'] else "🔴"
        lines.append(f"{icon} {m['name']} — {m['credits']} кредитов")

    try:
        await cb.message.edit_text(
            f"💎 <b>Баланс: {cr} кредитов</b>\n\n"
            f"<b>Доступные модели:</b>\n" + "\n".join(lines),
            reply_markup=kb_buy(), parse_mode="HTML"
        )
    except Exception:
        await cb.message.answer(
            f"💎 <b>Баланс: {cr} кредитов</b>\n\n"
            f"<b>Доступные модели:</b>\n" + "\n".join(lines),
            reply_markup=kb_buy(), parse_mode="HTML"
        )
    await cb.answer()


@dp.callback_query(F.data == "menu_buy")
async def menu_buy(cb: CallbackQuery):
    cr = await get_credits(cb.from_user.id)
    lines = [f"💎 <b>Баланс: {cr} кредитов</b>\n"]
    for p in CREDIT_PACKS.values():
        lines.append(
            f"<b>{p['name']} — {p['credits']} кредитов — {p['price']}₽</b>\n"
            f"<i>{p['desc']}</i>"
        )
    text = "\n\n".join(lines)
    try:
        await cb.message.edit_text(text, reply_markup=kb_buy(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_buy(), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def buy_pack(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    p = CREDIT_PACKS[key]
    msg = (
        f"{p['name']} — <b>{p.get('badge', '')}</b>\n\n"
        f"💎 <b>{p['credits']} кредитов</b>\n"
        f"💰 Цена: <b>{p['price']}₽</b>\n\n"
        f"📦 <i>{p['desc']}</i>\n\n"
        f"Выбери способ оплаты:"
    )
    try:
        await cb.message.edit_text(msg, reply_markup=kb_pay_method(key), parse_mode="HTML")
    except Exception:
        await cb.message.answer(msg, reply_markup=kb_pay_method(key), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("paystars:"))
async def pay_stars(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    p = CREDIT_PACKS[key]
    await cb.message.answer_invoice(
        title=f"{p['name']} — {p['credits']} кредитов",
        description=f"Пополнение баланса AI-бота: {p['credits']} кредитов",
        payload=f"stars:{key}:{cb.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(label=p['name'], amount=p['stars'])],
    )
    await cb.answer()


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


async def process_referral_bonus(user_id: int):
    """Начисляет бонус пригласившему при первой покупке реферала."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT referred_by, ref_bonus_paid FROM users WHERE user_id=$1", user_id
        )
        if not row or not row["referred_by"] or row["ref_bonus_paid"]:
            return
        referrer_id = row["referred_by"]
        await conn.execute(
            "UPDATE users SET ref_bonus_paid=TRUE WHERE user_id=$1", user_id
        )
    await add_credits(referrer_id, REF_BONUS)
    try:
        new_bal = await get_credits(referrer_id)
        await bot.send_message(
            referrer_id,
            f"🎉 <b>Реферальный бонус!</b>\n\n"
            f"Твой друг сделал первую покупку.\n"
            f"✨ Начислено: <b>+{REF_BONUS} кредитов</b>\n"
            f"💎 Баланс: <b>{new_bal} кредитов</b>",
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"ref bonus notify error: {e}")


@dp.message(F.successful_payment)
async def on_payment(message: Message):
    parts = message.successful_payment.invoice_payload.split(":")
    key = parts[1]
    user_id = message.from_user.id
    p = CREDIT_PACKS[key]
    await add_credits(user_id, p["credits"])
    await log_payment(user_id, p["credits"], p["stars"], "stars")
    await process_referral_bonus(user_id)
    cr = await get_credits(user_id)
    await message.answer(
        f"🎉 <b>Оплата прошла!</b>\n\n"
        f"💎 Начислено: +{p['credits']} кредитов\n"
        f"💎 Баланс: <b>{cr} кредитов</b>",
        reply_markup=kb_back(), parse_mode="HTML"
    )

# ══════════════════════════════════════════════════════════
#  ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "menu_image")
async def menu_image(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cr = await get_credits(cb.from_user.id)
    text = (
        f"🎨 <b>Создать изображение</b>\n\n"
        f"💎 Баланс: <b>{cr} кредитов</b>\n\n"
        f"⚡ <b>Imagen 4 Fast</b> — 7 кредитов\n"
        f"✨ <b>Imagen 4</b> — 10 кредитов\n"
        f"💎 <b>Imagen 4 Ultra</b> — 13 кредитов"
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb_image_models(), parse_mode="HTML")
    except Exception:
        # Не получилось отредактировать (напр. это сообщение с фото)
        await cb.message.answer(text, reply_markup=kb_image_models(), parse_mode="HTML")
    await cb.answer()


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
        f"{m['name']} ✅\n\n"
        f"💳 Спишется: <b>{m['credits']} кредитов</b>\n\n"
        f"📐 <b>Выбери формат изображения:</b>",
        reply_markup=kb_aspect_image(key), parse_mode="HTML"
    )
    await cb.answer()


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
        f"{m['name']} | 📐 {labels.get(ratio, ratio)}\n\n"
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


@dp.message(ImgState.waiting_prompt)
async def img_prompt(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data["model_key"]
    m = IMAGE_MODELS[key]
    prompt = message.text.strip()
    await state.update_data(prompt=prompt)

    await message.answer(
        f"📝 <b>Проверь заказ:</b>\n\n"
        f"🤖 {m['name']}\n"
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

    ok = await deduct(cb.from_user.id, m["credits"])
    if not ok:
        await cb.answer("💸 Недостаточно кредитов!", show_alert=True)
        return

    await state.clear()
    wait = await cb.message.edit_text(
        f"⚙️ Генерирую...\n\n🤖 {m['name']}\n<i>{prompt[:80]}</i>",
        parse_mode="HTML"
    )

    try:
        aspect = data.get("aspect_ratio", "1:1")
        img_bytes = await api_generate_image(prompt, m["model_id"], aspect, m.get("api", "imagen"))
        await log_gen(cb.from_user.id, "image", key, m["credits"])
        cr = await get_credits(cb.from_user.id)
        # Сохраняем оригинал в памяти для скачивания
        user_orig_images[cb.from_user.id] = img_bytes
        # Сначала отправляем оригинал как документ
        await cb.message.answer_document(
            BufferedInputFile(img_bytes, "original.png"),
            caption="\U0001f4ce <b>Оригинал</b> — без сжатия, полное качество",
            parse_mode="HTML"
        )
        # Затем превью с кнопками
        await cb.message.answer_photo(
            BufferedInputFile(img_bytes, "image.png"),
            caption=f"🎉 Готово! {m['name']}\n💸 Списано {m['credits']} кредитов | Остаток: {cr} кредитов",
            reply_markup=kb_after("image", key)
        )
        await wait.delete()
    except Exception as e:
        await add_credits(cb.from_user.id, m["credits"])
        await cb.message.edit_text(
            f"⛔ Ошибка: {e}\n\nКредиты возвращены.",
            reply_markup=kb_back()
        )
    await cb.answer()


@dp.callback_query(F.data.startswith("download_orig:"))
async def download_original(cb: CallbackQuery):
    """Отправляет оригинальное фото как документ без сжатия."""
    uid = cb.from_user.id
    img_bytes = user_orig_images.get(uid)
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
        f"💡 Введи новый промт для <b>{IMAGE_MODELS[key]['name']}</b>:",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("again:"))
async def after_gen_again(cb: CallbackQuery, state: FSMContext):
    """Ещё раз — та же модель, новый промт."""
    parts = cb.data.split(":")
    menu = parts[1]   # "image" или "video"
    key  = parts[2] if len(parts) > 2 else ""
    await state.clear()
    if menu == "image" and key in IMAGE_MODELS:
        m = IMAGE_MODELS[key]
        await state.update_data(model_key=key)
        await state.set_state(ImgState.waiting_prompt)
        await cb.message.answer(
            f"{m['name']} — снова!\n\n"
            f"💳 Спишется: <b>{m['credits']} кредитов</b>\n\n"
            f"💡 Введи промт:",
            reply_markup=kb_cancel(), parse_mode="HTML"
        )
    elif menu == "video" and key in VIDEO_MODELS:
        m = VIDEO_MODELS[key]
        await state.update_data(model_key=key)
        await state.set_state(VidState.waiting_prompt)
        await cb.message.answer(
            f"{m['name']} — снова!\n\n"
            f"💳 Спишется: <b>{m['credits']} кредитов</b>\n\n"
            f"💡 Введи промт:",
            reply_markup=kb_cancel(), parse_mode="HTML"
        )
    else:
        await cb.message.answer("Выбери действие 👇", reply_markup=kb_main())
    await cb.answer()


@dp.callback_query(F.data.startswith("improve:"))
async def after_gen_improve(cb: CallbackQuery, state: FSMContext):
    """Улучшить промт — предлагает написать уточнение."""
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

@dp.callback_query(F.data == "menu_video")
async def menu_video(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cr = await get_credits(cb.from_user.id)
    text = (
        f"🎬 <b>Создать видео (8 сек)</b>\n\n"
        f"💎 Баланс: <b>{cr} кредитов</b>\n\n"
        f"💰 <b>Veo 3.1 Lite</b> — 100 кредитов\n"
        f"⚡ <b>Veo 3.1 Fast</b> — 200 кредитов\n"
        f"🎬 <b>Veo 3.1</b> — 450 кредитов\n\n"
        f"⏱ <i>Время генерации: 1–6 минут</i>"
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb_video_models(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_video_models(), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("vmodel:"))
async def choose_vid_model(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    m = VIDEO_MODELS[key]
    cr = await get_credits(cb.from_user.id)
    if cr < m["credits"]:
        await cb.answer(f"💸 Нужно {m['credits']} кредитов. Пополни баланс!", show_alert=True)
        return
    await state.update_data(model_key=key)
    await state.set_state(VidState.waiting_aspect)
    await cb.message.edit_text(
        f"{m['name']} ✅\n\n"
        f"💳 Спишется: <b>{m['credits']} кредитов</b>\n"
        f"📐 {m['res']} | 8 сек\n\n"
        f"📐 <b>Выбери формат видео:</b>",
        reply_markup=kb_aspect_video(key), parse_mode="HTML"
    )
    await cb.answer()


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
        f"{m['name']} | 📐 {labels.get(ratio, ratio)}\n\n"
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


@dp.message(VidState.waiting_prompt)
async def vid_prompt(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data["model_key"]
    m = VIDEO_MODELS[key]
    prompt = message.text.strip()
    await state.update_data(prompt=prompt)

    await message.answer(
        f"📝 <b>Проверь заказ:</b>\n\n"
        f"🤖 {m['name']}\n"
        f"📐 {m['res']} | 8 сек\n"
        f"💳 <b>{m['credits']} кредитов</b> ({m['price']})\n\n"
        f"📝 <i>{prompt}</i>\n\n"
        f"⏱ <i>Генерация занимает 1–6 минут</i>",
        reply_markup=kb_confirm("vid", key), parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("go:vid:"))
async def go_video(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    m = VIDEO_MODELS[key]
    data = await state.get_data()
    prompt = data.get("prompt", "")

    ok = await deduct(cb.from_user.id, m["credits"])
    if not ok:
        await cb.answer("💸 Недостаточно кредитов!", show_alert=True)
        return

    await state.clear()
    await cb.message.edit_text(
        f"🎬 <b>Генерирую видео...</b>\n\n"
        f"🤖 {m['name']} | {m['res']}\n"
        f"📝 <i>{prompt[:80]}</i>\n\n"
        f"🕐 Обычно 1–6 минут. Пришлю как только готово 👇",
        parse_mode="HTML"
    )

    try:
        aspect = data.get("aspect_ratio", "16:9")
        vid_bytes = await api_generate_video(prompt, m["model_id"], aspect)
        logging.info(f"Video ready: {len(vid_bytes)} bytes")
        await log_gen(cb.from_user.id, "video", key, m["credits"])
        cr = await get_credits(cb.from_user.id)
        caption = f"🎉 Готово! {m['name']} | {m['res']}\n💸 Списано {m['credits']} кредитов | Остаток: {cr} кредитов"
        # Оригинал как файл — меняем расширение чтобы Telegram не сжимал как видео
        await cb.message.answer_document(
            BufferedInputFile(vid_bytes, "original_video.mp4.file"),
            caption="\U0001f4ce <b>Оригинал без сжатия</b> — переименуй в .mp4 для воспроизведения",
            parse_mode="HTML",
        )
        # Затем превью с кнопками
        try:
            await cb.message.answer_video(
                BufferedInputFile(vid_bytes, "video.mp4"),
                caption=caption,
                reply_markup=kb_after("video", key),
                supports_streaming=True,
            )
        except Exception as video_err:
            logging.warning(f"answer_video failed: {video_err}, trying as document")
            await cb.message.answer_document(
                BufferedInputFile(vid_bytes, "video.mp4"),
                caption=caption + "\n\n<i>Нажми для воспроизведения</i>",
                reply_markup=kb_after("video", key),
                parse_mode="HTML"
            )
    except Exception as e:
        await add_credits(cb.from_user.id, m["credits"])
        await cb.message.answer(
            f"⛔ Ошибка: {e}\n\nКредиты возвращены.",
            reply_markup=kb_back()
        )
    await cb.answer()


@dp.callback_query(F.data.startswith("chprompt:vid:"))
async def change_vid_prompt(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    await state.update_data(model_key=key)
    await state.set_state(VidState.waiting_prompt)
    await cb.message.edit_text(
        f"💡 Введи новый промт для <b>{VIDEO_MODELS[key]['name']}</b>:",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )
    await cb.answer()

# ══════════════════════════════════════════════════════════
#  КОНСУЛЬТАНТ (оригинальная логика сохранена)
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "menu_chat")
async def menu_chat(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ChatState.chatting)
    await cb.message.edit_text(
        "🤖 <b>Консультант AI</b>\n\n"
        "Задай любой вопрос о нейросетях, VPN, подписках.\n"
        "Это бесплатно 🎁\n\n"
        "<i>Напиши вопрос:</i>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data == "help_choose")
async def help_choose(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ChatState.chatting)
    await cb.message.answer(
        "Расскажи — для каких задач нужна нейросеть?\n\n"
        "• Писать тексты / посты\n"
        "• Генерировать картинки\n"
        "• Программирование\n"
        "• Анализ документов\n"
        "• Видео / музыка\n\n"
        "Опиши своими словами 👇"
    )
    await cb.answer()


@dp.message(ChatState.chatting)
async def chat_message(message: Message, state: FSMContext):
    if not message.text:
        return
    # Команды не перехватываем — передаём дальше
    if message.text.startswith("/"):
        return
    await bot.send_chat_action(message.chat.id, "typing")
    uid = message.from_user.id
    reply = await claude_with_search(uid, message.text)
    try:
        await message.answer(reply, reply_markup=kb_cancel(), parse_mode="HTML")
    except Exception:
        await message.answer(reply, reply_markup=kb_cancel())

# ══════════════════════════════════════════════════════════
#  ПРИВЕТСТВИЕ НОВЫХ ПОДПИСЧИКОВ (оригинал сохранён)
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
                 f"Я — AI-ассистент Александра. Помогу:\n"
                 f"🎨 Создать изображение (Imagen 4)\n"
                 f"🎥 Создать видео (Veo 3.1)\n"
                 f"💬 Разобраться в нейросетях\n"
                 f"💳 Оформить подписку — оплата в рублях\n\n"
                 f"🎁 Тебе начислено <b>{FREE_CREDITS} бесплатных кредитов</b>!\n\n"
                 f"Напиши /start чтобы начать 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✨ Начать", callback_data="back_main")],
                [InlineKeyboardButton(text="💌 Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}")],
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Не удалось отправить приветствие {user.id}: {e}")


# ══════════════════════════════════════════════════════════
#  ФУНКЦИЯ CLAUDE С ВЕБ-ПОИСКОМ
# ══════════════════════════════════════════════════════════

def clean_reply(text: str) -> str:
    """Убирает служебные теги и невалидный HTML из ответа."""
    import re
    # Убираем <search>...</search> теги
    text = re.sub(r'<search>.*?</search>', '', text, flags=re.DOTALL)
    # Убираем любые XML/HTML теги кроме разрешённых Telegram
    allowed = {'b', '/b', 'i', '/i', 'code', '/code', 'pre', '/pre', 'a', '/a', 's', '/s', 'u', '/u'}
    def replace_tag(m):
        tag = m.group(1).strip().lower().split()[0]
        return m.group(0) if tag in allowed else ''
    text = re.sub(r'<([^>]+)>', replace_tag, text)
    # Убираем лишние пустые строки
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


async def claude_with_search(uid: int, user_text: str) -> str:
    if uid not in user_conversations:
        user_conversations[uid] = []

    # Сохраняем только текстовые сообщения в истории (не tool_use блоки)
    user_conversations[uid].append({"role": "user", "content": user_text})
    if len(user_conversations[uid]) > 20:
        user_conversations[uid] = user_conversations[uid][-20:]

    try:
        # Для API используем отдельную копию — не портим историю
        api_messages = list(user_conversations[uid])

        resp = claude_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=api_messages,
        )

        # Обрабатываем tool_use если Claude решил искать
        max_iterations = 3
        iterations = 0
        while resp.stop_reason == "tool_use" and iterations < max_iterations:
            iterations += 1
            assistant_content = resp.content
            tool_results = []
            for block in assistant_content:
                if hasattr(block, "type") and block.type == "tool_use":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": [{"type": "text", "text": "Search completed."}],
                    })

            # Обновляем только api_messages, НЕ user_conversations
            api_messages.append({"role": "assistant", "content": assistant_content})
            if tool_results:
                api_messages.append({"role": "user", "content": tool_results})
            else:
                break

            resp = claude_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=api_messages,
            )

        # Собираем текстовый ответ
        reply = ""
        for block in resp.content:
            if hasattr(block, "text"):
                reply += block.text

        if not reply:
            reply = "Попробуй переформулировать вопрос 🙏"

        reply = clean_reply(reply)

        # Сохраняем только текст в историю (без tool блоков)
        user_conversations[uid].append({"role": "assistant", "content": reply})
        return reply

    except Exception as e:
        logging.error(f"Claude API error: {e}")
        # Fallback без поиска — используем чистую историю
        try:
            clean_history = [
                m for m in user_conversations[uid]
                if isinstance(m.get("content"), str)
            ]
            resp = claude_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=clean_history,
            )
            reply = clean_reply(resp.content[0].text)
            user_conversations[uid].append({"role": "assistant", "content": reply})
            return reply
        except Exception as e2:
            logging.error(f"Fallback error: {e2}")
            return "Что-то пошло не так 😅 Попробуй ещё раз или напиши @neirosetkaalex"


# ══════════════════════════════════════════════════════════
#  REPLY KEYBOARD HANDLERS
# ══════════════════════════════════════════════════════════

@dp.message(F.text == "🏡 Главное меню", StateFilter("*"))
async def reply_main_menu(message: Message, state: FSMContext):
    await state.clear()
    credits = await get_credits(message.from_user.id)
    await message.answer(
        f"👋 {message.from_user.first_name}, баланс: <b>{credits} кредитов</b>\n\nВыбери действие 👇",
        reply_markup=kb_main(), parse_mode="HTML"
    )


@dp.message(F.text == "✨ Создать фото", StateFilter("*"))
async def reply_create_photo(message: Message, state: FSMContext):
    await state.clear()
    cr = await get_credits(message.from_user.id)
    await message.answer(
        f"🎨 <b>Создать изображение</b>\n\n"
        f"💎 Баланс: <b>{cr} кредитов</b>\n\n"
        f"⚡ <b>Imagen 4 Fast</b> — 7 кредитов\n"
        f"✨ <b>Imagen 4</b> — 10 кредитов\n"
        f"💎 <b>Imagen 4 Ultra</b> — 13 кредитов",
        reply_markup=kb_image_models(), parse_mode="HTML"
    )


@dp.message(F.text == "🎥 Создать видео", StateFilter("*"))
async def reply_create_video(message: Message, state: FSMContext):
    await state.clear()
    cr = await get_credits(message.from_user.id)
    await message.answer(
        f"🎬 <b>Создать видео (8 сек)</b>\n\n"
        f"💎 Баланс: <b>{cr} кредитов</b>\n\n"
        f"💰 <b>Veo 3.1 Lite</b> — 100 кредитов\n"
        f"⚡ <b>Veo 3.1 Fast</b> — 200 кредитов\n"
        f"🎬 <b>Veo 3.1</b> — 450 кредитов\n\n"
        f"⏱ <i>Время генерации: 1–6 минут</i>",
        reply_markup=kb_video_models(), parse_mode="HTML"
    )


@dp.message(F.text == "🪪 Мой профиль", StateFilter("*"))
async def reply_profile(message: Message):
    uid = message.from_user.id
    await ensure_user(uid)
    cr = await get_credits(uid)

    # Считаем генерации
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE user_id=$1", uid
        )
        total_gens = row[0] or 0
        total_credits_spent = row[1] or 0

    # Что доступно
    can = []
    if cr >= 1: can.append("✅ Imagen 4 Fast")
    if cr >= 2: can.append("✅ Imagen 4")
    if cr >= 3: can.append("✅ Imagen 4 Ultra")
    if cr >= 15: can.append("✅ Veo 3.1 Lite")
    if cr >= 25: can.append("✅ Veo 3.1 Fast")
    if cr >= 65: can.append("✅ Veo 3.1 Pro")
    if not can: can.append("❌ Пополни баланс")

    text = (
        f"🪪 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"👋 Имя: {message.from_user.full_name}\n\n"
        f"💎 <b>Баланс: {cr} кредитов</b>\n"
        f"🎨 Генераций сделано: {total_gens}\n"
        f"💸 Кредитов потрачено: {total_credits_spent}\n\n"
        f"<b>Доступно сейчас:</b>\n" + "\n".join(can)
    )
    await message.answer(text, reply_markup=kb_buy(), parse_mode="HTML")



async def get_admin_stats() -> dict:
    pool = await get_pool()
    async with pool.acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM users")
        gens = await conn.fetchval("SELECT COUNT(*) FROM generations") or 0
        credits_used = await conn.fetchval("SELECT COALESCE(SUM(credits),0) FROM generations") or 0
        payments = await conn.fetchval("SELECT COUNT(*) FROM payments") or 0
        revenue = await conn.fetchval("SELECT COALESCE(SUM(amount_rub),0) FROM payments") or 0
        top = await conn.fetch("SELECT user_id, credits FROM users ORDER BY credits DESC LIMIT 5")
    top_text = "\n".join([f"  {i+1}. ID {r['user_id']} — {r['credits']} кредитов" for i, r in enumerate(top)])
    return dict(users=users, gens=gens, credits_used=credits_used,
                payments=payments, revenue=revenue, top_text=top_text)

def kb_admin_panel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика",        callback_data="adm_stat_day"),
         InlineKeyboardButton(text="📈 Активность",        callback_data="adm_activity")],
        [InlineKeyboardButton(text="🔥 Топ моделей", callback_data="adm_popular"),
         InlineKeyboardButton(text="👑 Топ юзеров",      callback_data="adm_top_users")],
        [InlineKeyboardButton(text="👤 Пользователи",      callback_data="adm_users"),
         InlineKeyboardButton(text="🔎 Найти по ID",       callback_data="adm_find")],
        [InlineKeyboardButton(text="💰 Начислить кредиты", callback_data="adm_give_credits"),
         InlineKeyboardButton(text="🧾 История платежей",  callback_data="adm_payments")],
        [InlineKeyboardButton(text="📉 Расход по юзеру",   callback_data="adm_spend"),
         InlineKeyboardButton(text="🔒 Блокировки",        callback_data="adm_blocks")],
        [InlineKeyboardButton(text="📝 Изменить приветствие", callback_data="adm_welcome")],
        [InlineKeyboardButton(text="📣 Рассылка",          callback_data="adm_broadcast"),
         InlineKeyboardButton(text="⚙️ Техобслуживание",   callback_data="adm_maintenance")],
        [InlineKeyboardButton(text="🏡 Главное меню",      callback_data="back_main")],
    ])

def kb_block_actions(target_id: int, currently_blocked: bool):
    action = "adm_unblock" if currently_blocked else "adm_block"
    label = "✅ Разблокировать" if currently_blocked else "🚫 Заблокировать"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"{action}:{target_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_blocks")],
    ])


@dp.message(F.text == "🛠️ Админ панель", StateFilter("*"))
async def reply_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    await state.clear()
    await show_admin_panel(message)


@dp.callback_query(F.data == "adm_stat_day")
async def adm_stat_day(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        new_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at >= CURRENT_DATE") or 0
        row = await conn.fetchrow("SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE created_at >= CURRENT_DATE")
        gens, credits_used = row[0] or 0, row[1] or 0
        row2 = await conn.fetchrow("SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM payments WHERE created_at >= CURRENT_DATE")
        pays, revenue = row2[0] or 0, row2[1] or 0
        by_type = await conn.fetch("SELECT type, COUNT(*) FROM generations WHERE created_at >= CURRENT_DATE GROUP BY type")

    by_type_text = "\n".join([f"  • {r[0]}: {r[1]} шт" for r in by_type]) or "  нет данных"

    await cb.message.answer(
        f"📊 <b>Статистика за сегодня</b>\n\n"
        f"🆕 Новых пользователей: <b>{new_users}</b>\n"
        f"🎨 Генераций: <b>{gens}</b>\n"
        f"💸 Кредитов потрачено: <b>{credits_used}</b>\n"
        f"💳 Оплат: <b>{pays}</b>\n"
        f"💰 Выручка: <b>{revenue}₽</b>\n\n"
        f"<b>По типу:</b>\n{by_type_text}",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data == "adm_stat_week")
async def adm_stat_week(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return

    pool = await get_pool()
    async with pool.acquire() as conn:
        new_users = await conn.fetchval("SELECT COUNT(*) FROM users WHERE created_at >= NOW() - INTERVAL '7 days'") or 0
        row = await conn.fetchrow("SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE created_at >= NOW() - INTERVAL '7 days'")
        gens, credits_used = row[0] or 0, row[1] or 0
        row2 = await conn.fetchrow("SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM payments WHERE created_at >= NOW() - INTERVAL '7 days'")
        pays, revenue = row2[0] or 0, row2[1] or 0
        by_day = await conn.fetch("SELECT DATE(created_at), COUNT(*) FROM generations WHERE created_at >= NOW() - INTERVAL '7 days' GROUP BY DATE(created_at) ORDER BY 1")

    by_day_text = "\n".join([f"  {r[0]}: {r[1]} ген." for r in by_day]) or "  нет данных"

    await cb.message.answer(
        f"📈 <b>Статистика за 7 дней</b>\n\n"
        f"🆕 Новых пользователей: <b>{new_users}</b>\n"
        f"🎨 Генераций: <b>{gens}</b>\n"
        f"💸 Кредитов потрачено: <b>{credits_used}</b>\n"
        f"💳 Оплат: <b>{pays}</b>\n"
        f"💰 Выручка: <b>{revenue}₽</b>\n\n"
        f"<b>По дням:</b>\n{by_day_text}",
        parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data == "adm_give_credits")
async def adm_give_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    await state.set_state(AdminState.waiting_user_id)
    await cb.message.answer(
        "➕ <b>Начислить кредиты</b>\n\nВведи Telegram ID пользователя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_user_id)
async def adm_get_user_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    if not message.text:
        await message.answer("❌ Отправь Telegram ID текстом")
        return
    txt = message.text.strip()
    logging.info(f"ADMIN give_credits input: '{txt}'")
    try:
        target_id = int(txt)
    except (ValueError, TypeError):
        await message.answer(
            f"⛔ <code>{txt}</code> — не числовой ID\n"
            f"Введи только цифры, например: <code>123456789</code>",
            parse_mode="HTML"
        )
        return
    try:
        user = await get_user(target_id)
        credits_balance = user["credits"] if user else 0
        status = "✅ Зарегистрирован" if user else "⚠️ Не в базе (создам при начислении)"
        await state.update_data(target_id=target_id)
        await state.set_state(AdminState.waiting_credits)
        await message.answer(
            f"👤 ID: <code>{target_id}</code>\n"
            f"Статус: {status}\n"
            f"Баланс: <b>{credits_balance} кредитов</b>\n\n"
            f"Сколько кредитов начислить?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.error(f"adm_get_user_id error: {e}")
        await message.answer(f"⛔ Ошибка: {e}")
        await state.clear()


@dp.message(AdminState.waiting_credits)
async def adm_give_credits_confirm(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    txt = message.text.strip() if message.text else ""
    try:
        amount = int(txt)
        if amount <= 0:
            await message.answer("❌ Введи положительное число:")
            return
    except (ValueError, TypeError):
        await message.answer("❌ Введи число, например: <code>50</code>", parse_mode="HTML")
        return
    data = await state.get_data()
    target_id = data["target_id"]
    # Создаём пользователя если его нет
    user = await get_user(target_id)
    if not user:
        pool = await get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO users (user_id, credits) VALUES ($1, 0) ON CONFLICT DO NOTHING",
                target_id
            )
    await add_credits(target_id, amount)
    new_balance = await get_credits(target_id)
    await state.clear()
    await message.answer(
        f"✨ <b>Кредиты начислены!</b>\n\n"
        f"👤 ID: <code>{target_id}</code>\n"
        f"✨ Начислено: <b>{amount} кредитов</b>\n"
        f"💳 Новый баланс: <b>{new_balance} кредитов</b>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Ещё начислить", callback_data="adm_give_credits")],
            [InlineKeyboardButton(text="◀️ Панель",        callback_data="adm_back")],
        ]),
        parse_mode="HTML"
    )
    try:
        await bot.send_message(
            target_id,
            f"🎁 Тебе начислено <b>{amount} кредитов</b> от администратора!\n"
            f"💎 Баланс: <b>{new_balance} кредитов</b>",
            parse_mode="HTML"
        )
    except Exception:
        pass


@dp.callback_query(F.data == "adm_cancel")
async def adm_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("❌ Отменено. Нажми /admin чтобы вернуться в панель.")
    except Exception:
        await cb.message.answer("❌ Отменено.")
    await cb.answer()


# ─── Блокировки ───────────────────────────────────────────

@dp.callback_query(F.data == "adm_blocks")
async def adm_blocks_menu(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            blocked_count = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_blocked=1") or 0
            blocked_list = await conn.fetch("SELECT user_id FROM users WHERE is_blocked=1 LIMIT 10")

        blocked_text = ", ".join([str(r["user_id"]) for r in blocked_list]) or "нет"

        await cb.message.answer(
            f"🚫 <b>Блокировки</b>\n\n"
            f"Заблокировано пользователей: <b>{blocked_count}</b>\n"
            f"ID: {blocked_text}\n\n"
            f"Введи ID пользователя чтобы заблокировать или разблокировать:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
            ]),
            parse_mode="HTML"
        )
        await state.set_state(AdminState.waiting_block_id)
    except Exception as e:
        logging.error(f"adm_blocks error: {e}")
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


@dp.message(AdminState.waiting_block_id)
async def adm_block_check_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    txt = message.text.strip() if message.text else ""
    try:
        target_id = int(txt)
    except (ValueError, TypeError):
        await message.answer("❌ Введи числовой Telegram ID, например: <code>123456789</code>", parse_mode="HTML")
        return
    user = await get_user(target_id)
    if not user:
        await message.answer(
            f"🔍 Пользователь <code>{target_id}</code> не найден в базе.\n"
            f"Он ещё не использовал бота.",
            parse_mode="HTML"
        )
        await state.clear()
        return
    blocked = bool(user.get("is_blocked", 0))
    status = "🚫 Заблокирован" if blocked else "✅ Активен"
    await state.clear()
    await message.answer(
        f"👤 ID: <code>{target_id}</code>\n"
        f"Статус: {status}\n"
        f"Баланс: <b>{user['credits']} кредитов</b>",
        reply_markup=kb_block_actions(target_id, blocked),
        parse_mode="HTML"
    )


@dp.callback_query(F.data.startswith("adm_block:"))
async def adm_do_block(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        target_id = int(cb.data.split(":")[1])
        await block_user(target_id)
        await cb.message.edit_text(
            f"🚫 Пользователь <code>{target_id}</code> заблокирован.\n"
            f"Он больше не сможет пользоваться ботом.",
            reply_markup=kb_block_actions(target_id, True),
            parse_mode="HTML"
        )
        try:
            await bot.send_message(target_id, "🚫 Ваш доступ к боту ограничен администратором.")
        except Exception:
            pass
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


@dp.callback_query(F.data.startswith("adm_unblock:"))
async def adm_do_unblock(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        target_id = int(cb.data.split(":")[1])
        await unblock_user(target_id)
        await cb.message.edit_text(
            f"✅ Пользователь <code>{target_id}</code> разблокирован.",
            reply_markup=kb_block_actions(target_id, False),
            parse_mode="HTML"
        )
        try:
            await bot.send_message(target_id, "✅ Ваш доступ к боту восстановлен!")
        except Exception:
            pass
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()



# ─── Активность ───────────────────────────────────────────

@dp.callback_query(F.data == "adm_activity")
async def adm_activity(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            periods = [
                ("Сегодня",  "NOW() - INTERVAL '1 day'",  "CURRENT_DATE"),
                ("Вчера",    "NOW() - INTERVAL '2 days'", "NOW() - INTERVAL '1 day'"),
                ("7 дней",   "NOW() - INTERVAL '7 days'", "NOW()"),
                ("30 дней",  "NOW() - INTERVAL '30 days'","NOW()"),
            ]
            lines = []
            for label, since, _ in periods:
                row = await conn.fetchrow(
                    f"SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE created_at >= {since}"
                )
                new_u = await conn.fetchval(
                    f"SELECT COUNT(*) FROM users WHERE created_at >= {since}"
                ) or 0
                lines.append(f"<b>{label}:</b> {row[0]} ген, +{new_u} юз, {row[1]} кредитов")
        await cb.message.answer(
            "📈 <b>Активность</b>\n\n" + "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


# ─── Популярные модели ────────────────────────────────────

@dp.callback_query(F.data == "adm_popular")
async def adm_popular(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    # Словарь ключ → читаемое название
    MODEL_NAMES = {
        "img_fast":          "⚡ Imagen 4 Fast",
        "img_std":           "✨ Imagen 4",
        "img_ultra":         "💎 Imagen 4 Ultra",
        "vid_lite":          "💰 Veo 3.1 Lite",
        "vid_fast":          "⚡ Veo 3.1 Fast",
        "vid_pro":           "🎬 Veo 3.1 Pro",
        "gemini-flash-image":"✏️ Редактирование фото",
        "edit":              "✏️ Редактирование фото",
    }
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT model, COUNT(*), SUM(credits) FROM generations GROUP BY model ORDER BY COUNT(*) DESC"
            )
        if not rows:
            text = "🔥 <b>Популярные модели</b>\n\nПока нет генераций."
        else:
            lines = []
            for i, r in enumerate(rows):
                name = MODEL_NAMES.get(r[0], r[0])
                lines.append(f"  {i+1}. {name}: <b>{r[1]} ген</b> ({r[2]} кредитов)")
            text = "🔥 <b>Популярные модели</b>\n\n" + "\n".join(lines)
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
        ]), parse_mode="HTML")
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


# ─── Топ активных пользователей ───────────────────────────

@dp.callback_query(F.data == "adm_top_users")
async def adm_top_users(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT g.user_id, u.username, COUNT(*) as cnt, COALESCE(SUM(g.credits),0) as total_credits
                FROM generations g LEFT JOIN users u ON g.user_id=u.user_id
                GROUP BY g.user_id, u.username ORDER BY cnt DESC LIMIT 10
            """)
        if not rows:
            text = "🏆 <b>Топ активных</b>\n\nПока нет данных."
        else:
            lines = []
            for i, r in enumerate(rows):
                uname = f"@{r[1]}" if r[1] else f"ID {r[0]}"
                lines.append(f"  {i+1}. {uname}: {r[2]} ген, {r[3]} кредитов")
            text = "🏆 <b>Топ активных пользователей</b>\n\n" + "\n".join(lines)
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
        ]), parse_mode="HTML")
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


# ─── Список пользователей ─────────────────────────────────

@dp.callback_query(F.data == "adm_users")
async def adm_users(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM users") or 0
            rows = await conn.fetch(
                "SELECT user_id, username, full_name, credits, created_at FROM users ORDER BY created_at DESC LIMIT 10"
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
            lines.append(f"• {uname} — {r['credits']} кредитов ({str(r['created_at'])[:10]})")
        text = f"👥 <b>Пользователи</b> (всего: {total})\n\n<b>Последние 10:</b>\n" + "\n".join(lines)
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
        ]), parse_mode="HTML")
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


# ─── Найти пользователя ───────────────────────────────────

@dp.callback_query(F.data == "adm_find")
async def adm_find_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    await state.set_state(AdminState.waiting_find_user)
    await cb.message.answer(
        "🔍 <b>Найти пользователя</b>\n\nВведи Telegram ID:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_find_user)
async def adm_find_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    txt = message.text.strip() if message.text else ""
    try:
        uid = int(txt)
    except (ValueError, TypeError):
        await message.answer(
            "❌ Введи числовой Telegram ID\n<i>Пример: 123456789</i>",
            parse_mode="HTML"
        )
        return
    await state.clear()
    user = await get_user(uid)
    if not user:
        await message.answer(
            f"🔍 Пользователь <code>{uid}</code> не найден.\n"
            f"Он ещё не запускал бота.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
            ]),
            parse_mode="HTML"
        )
        return
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE user_id=$1", uid
        )
        pay_row = await conn.fetchrow(
            "SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM payments WHERE user_id=$1", uid
        )
        last_gen = await conn.fetchrow(
            "SELECT model, created_at FROM generations WHERE user_id=$1 ORDER BY created_at DESC LIMIT 1", uid
        )
    blocked = "🚫 Да" if user.get("is_blocked") else "✅ Нет"
    username = (user.get("username") or "").strip()
    full_name = (user.get("full_name") or "").strip()
    uname = f"@{username}" if username else (full_name or "—")
    last_active = str(user.get("last_active", ""))[:16].replace("T", " ")
    created_at = str(user.get("created_at", ""))[:10]
    last_gen_text = f"{last_gen['model']} ({str(last_gen['created_at'])[:10]})" if last_gen else "—"

    kb_rows = [
        [InlineKeyboardButton(
            text="✍️ Написать пользователю",
            url=f"tg://user?id={uid}"
        )],
        [InlineKeyboardButton(
            text="💰 Начислить кредиты",
            callback_data=f"adm_give_to:{uid}"
        )],
        [InlineKeyboardButton(
            text="🚫 Заблокировать" if not user.get("is_blocked") else "✅ Разблокировать",
            callback_data=f"adm_block:{uid}" if not user.get("is_blocked") else f"adm_unblock:{uid}"
        )],
        [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")],
    ]
    await message.answer(
        f"🪪 <b>Пользователь</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📋 Имя: {full_name or '—'}\n"
        f"📧 Username: {('@' + username) if username else '—'}\n"
        f"💎 Баланс: <b>{user['credits']} кредитов</b>\n"
        f"🎨 Генераций: <b>{row[0]}</b> ({row[1]} кредитов потрачено)\n"
        f"💰 Платежей: {pay_row[0]} на {pay_row[1]}₽\n"
        f"🕐 Последняя активность: {last_active or '—'}\n"
        f"🎯 Последняя генерация: {last_gen_text}\n"
        f"🚫 Заблокирован: {blocked}\n"
        f"📅 Регистрация: {created_at}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="HTML"
    )


# ─── Быстрое начисление из карточки пользователя ──────────

@dp.callback_query(F.data.startswith("adm_give_to:"))
async def adm_give_to(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    uid = int(cb.data.split(":")[1])
    await state.update_data(target_user_id=uid)
    await state.set_state(AdminState.waiting_credits)
    await cb.message.answer(
        f"\U0001f4b3 Начислить кредиты пользователю <code>{uid}</code>\n\nВведи количество кредитов:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


# ─── История платежей ─────────────────────────────────────

@dp.callback_query(F.data == "adm_payments")
async def adm_payments(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, credits, amount_rub, method, created_at FROM payments ORDER BY created_at DESC LIMIT 15"
            )
            total_row = await conn.fetchrow("SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM payments")
        if not rows:
            text = "📋 <b>История платежей</b>\n\nПлатежей пока нет."
        else:
            lines = [f"• ID {r['user_id']}: +{r['credits']} кредитов, {r['amount_rub']}₽ ({str(r['created_at'])[:10]})" for r in rows]
            text = (f"📋 <b>История платежей</b>\n"
                    f"Всего: {total_row[0]} платежей, {total_row[1]}₽\n\n"
                    + "\n".join(lines))
        await cb.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
        ]), parse_mode="HTML")
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


# ─── Расход по пользователю ───────────────────────────────

@dp.callback_query(F.data == "adm_spend")
async def adm_spend_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    await state.set_state(AdminState.waiting_spend_uid)
    await cb.message.answer(
        "💰 <b>Расход по пользователю</b>\n\nВведи Telegram ID:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_spend_uid)
async def adm_spend_show(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    txt = message.text.strip() if message.text else ""
    try:
        uid = int(txt)
    except (ValueError, TypeError):
        await message.answer("❌ Введи числовой ID, например: <code>123456789</code>", parse_mode="HTML")
        return
    await state.clear()
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT model, COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE user_id=$1 GROUP BY model ORDER BY COUNT(*) DESC",
            uid
        )
        total = await conn.fetchrow(
            "SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE user_id=$1", uid
        )
    user = await get_user(uid)
    if not user:
        await message.answer(
            f"🔍 Пользователь <code>{uid}</code> не найден в базе.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
            ]),
            parse_mode="HTML"
        )
        return
    if not rows:
        await message.answer(
            f"💰 Пользователь <code>{uid}</code> ещё не делал генераций.\n"
            f"Баланс: <b>{user['credits']} кредитов</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
            ]),
            parse_mode="HTML"
        )
        return
    lines = [f"  • {r[0]}: {r[1]} раз, {r[2] or 0} кредитов" for r in rows]
    await message.answer(
        f"💰 <b>Расход пользователя</b> <code>{uid}</code>\n\n"
        f"Всего генераций: <b>{total[0]}</b>\n"
        f"Всего кредитов потрачено: <b>{total[1]}</b>\n"
        f"Текущий баланс: <b>{user['credits']} кредитов</b>\n\n"
        f"<b>По моделям:</b>\n" + "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
        ]),
        parse_mode="HTML"
    )


# ─── Изменить приветствие ─────────────────────────────────

@dp.callback_query(F.data == "adm_welcome")
async def adm_welcome_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    current = await get_setting("welcome_extra", "")
    await state.set_state(AdminState.waiting_welcome)
    await cb.message.answer(
        f"✏️ <b>Изменить приветствие</b>\n\n"
        f"Текущий доп. текст:\n<i>{current or 'не задан'}</i>\n\n"
        f"Введи новый текст (добавится к стандартному приветствию):\n"
        f"Или напиши <b>убрать</b> чтобы удалить.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_welcome)
async def adm_welcome_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    text = "" if message.text.strip().lower() == "убрать" else message.text.strip()
    await set_setting("welcome_extra", text)
    await message.answer(
        f"✅ Приветствие {'удалено' if not text else 'обновлено'}!\n\n"
        f"<i>{text or 'пусто'}</i>",
        parse_mode="HTML"
    )


# ─── Рассылка ─────────────────────────────────────────────

@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    pool = await get_pool()
    async with pool.acquire() as conn:
        total = await conn.fetchval("SELECT COUNT(*) FROM users WHERE is_blocked=0") or 0
    await state.set_state(AdminState.waiting_broadcast)
    await cb.message.answer(
        f"📢 <b>Рассылка</b>\n\n"
        f"Получателей: <b>{total} пользователей</b>\n\n"
        f"Введи текст сообщения (поддерживается HTML):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚫 Отмена", callback_data="adm_cancel")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_broadcast)
async def adm_broadcast_send(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID: return
    await state.clear()
    text = message.text.strip()
    pool = await get_pool()
    async with pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id FROM users WHERE is_blocked=0")
    sent = 0
    failed = 0
    status_msg = await message.answer(f"📢 Рассылка запущена... 0/{len(users)}")
    for i, r in enumerate(users):
        uid = r["user_id"]
        try:
            await bot.send_message(uid, text, parse_mode="HTML")
            sent += 1
        except Exception:
            failed += 1
        if (i + 1) % 20 == 0:
            try:
                await status_msg.edit_text(f"📢 Рассылка... {i+1}/{len(users)}")
            except Exception:
                pass
    await status_msg.edit_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"✅ Отправлено: {sent}\n"
        f"❌ Не доставлено: {failed}",
        parse_mode="HTML"
    )


# ─── Техобслуживание ──────────────────────────────────────

@dp.callback_query(F.data == "adm_maintenance")
async def adm_maintenance(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    try:
        current = await get_setting("maintenance", "0")
        new_val = "0" if current == "1" else "1"
        await set_setting("maintenance", new_val)
        status = "🔴 ВКЛЮЧЁН" if new_val == "1" else "🟢 ВЫКЛЮЧЕН"
        await cb.message.answer(
            f"🔧 <b>Техобслуживание {status}</b>\n\n"
            f"{'Пользователи видят сообщение о техработах.' if new_val == '1' else 'Бот работает в штатном режиме.'}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Панель", callback_data="adm_back")]
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        await cb.message.answer(f"⛔ Ошибка: {e}")
    finally:
        await cb.answer()


# ─── Кнопка "назад к панели" ──────────────────────────────

@dp.callback_query(F.data == "adm_back")
async def adm_back(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌", show_alert=True); return
    await show_admin_panel(cb.message)
    await cb.answer()

# ══════════════════════════════════════════════════════════
#  РЕДАКТИРОВАНИЕ ФОТО ПО РЕФЕРЕНСУ
# ══════════════════════════════════════════════════════════

EDIT_CREDIT_COST = 10  # стоимость редактирования = 10 кредитов
ANIM_CREDIT_COST  = 360  # стоимость анимации фото = 360 кредитов

@dp.callback_query(F.data == "menu_edit")
async def menu_edit(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cr = await get_credits(cb.from_user.id)
    text = (
        f"✏️ <b>Редактировать фото по референсу</b>\n\n"
        f"💎 Баланс: <b>{cr} кредитов</b>\n"
        f"💰 Стоимость: <b>{EDIT_CREDIT_COST} кредитов</b>\n\n"
        f"Как это работает:\n"
        f"1️⃣ Отправь своё фото\n"
        f"2️⃣ Напиши что изменить\n"
        f"3️⃣ Получи результат\n\n"
        f"<i>Примеры: добавить закат, сменить фон, сделать в стиле аниме, убрать лишние объекты</i>"
    )
    if cr < EDIT_CREDIT_COST:
        try:
            await cb.message.edit_text(
                f"💸 Недостаточно кредитов\n\nНужно {EDIT_CREDIT_COST} кредитов, у тебя {cr} кредитов.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⚡ Купить кредиты", callback_data="menu_buy")],
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
                ]),
                parse_mode="HTML"
            )
        except Exception:
            await cb.message.answer(
                f"💸 Недостаточно кредитов. Нужно {EDIT_CREDIT_COST} кредитов.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⚡ Купить кредиты", callback_data="menu_buy")],
                ])
            )
        await cb.answer()
        return

    await state.set_state(EditState.waiting_photo)
    try:
        await cb.message.edit_text(text, reply_markup=kb_cancel(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_cancel(), parse_mode="HTML")
    await cb.answer()


@dp.message(EditState.waiting_photo)
async def edit_get_photo(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer("📷 Отправь <b>фотографию</b> — картинку из галереи или файл", parse_mode="HTML")
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
    photo_bytes = bytes(data["photo_bytes"])
    prompt = message.text.strip()

    # Проверяем кредиты
    cr = await get_credits(message.from_user.id)
    if cr < EDIT_CREDIT_COST:
        await state.clear()
        await message.answer(f"💸 Недостаточно кредитов. Нужно {EDIT_CREDIT_COST} кредитов, у тебя {cr}.")
        return

    # Списываем кредиты
    ok = await deduct(message.from_user.id, EDIT_CREDIT_COST)
    if not ok:
        await state.clear()
        await message.answer("⛔ Ошибка списания кредитов. Попробуй ещё раз.")
        return

    await state.clear()
    wait = await message.answer(
        f"🖌️ Редактирую фото...\n\n"
        f"🤖 Gemini Flash Image\n"
        f"<i>{prompt[:80]}</i>",
        parse_mode="HTML"
    )

    try:
        result_bytes = await api_edit_image(photo_bytes, prompt)
        await log_gen(message.from_user.id, "edit", "gemini-flash-image", EDIT_CREDIT_COST)
        cr_left = await get_credits(message.from_user.id)
        await message.answer_photo(
            BufferedInputFile(result_bytes, "edited.png"),
            caption=f"🎉 Готово! ✏️ Редактирование\n💸 Списано {EDIT_CREDIT_COST} кредитов | Остаток: {cr_left} кредитов",
            reply_markup=kb_after("edit", "edit")
        )
        await wait.delete()
    except Exception as e:
        await add_credits(message.from_user.id, EDIT_CREDIT_COST)
        await wait.edit_text(
            f"⛔ Ошибка: {e}\n\nКредиты возвращены.",
            reply_markup=kb_back()
        )


@dp.callback_query(F.data.startswith("again:edit:"))
async def edit_again(cb: CallbackQuery, state: FSMContext):
    """Ещё раз редактировать."""
    await state.clear()
    cr = await get_credits(cb.from_user.id)
    if cr < EDIT_CREDIT_COST:
        await cb.answer(f"❌ Нужно {EDIT_CREDIT_COST} кредитов, у тебя {cr} кредитов", show_alert=True)
        return
    await state.set_state(EditState.waiting_photo)
    await cb.message.answer(
        f"📷 Отправь новое фото для редактирования:",
        reply_markup=kb_cancel()
    )
    await cb.answer()


# ══════════════════════════════════════════════════════════
#  ПОДДЕРЖКА / ПОЛИТИКА / ОФЕРТА
# ══════════════════════════════════════════════════════════

@dp.message(F.text == "/help", StateFilter("*"))
async def cmd_help(message: Message):
    await message.answer(
        "\U0001f6e1\ufe0f <b>Поддержка Neirosetka</b>\n\n"
        "Если что-то пошло не так — мы всегда рядом!\n\n"
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
        "— обработки платежей и начисления кредитов\n"
        "— технической поддержки\n"
        "— уведомлений о работе сервиса\n\n"
        "<b>3. Хранение и защита</b>\n"
        "Данные хранятся на защищённых серверах. Доступ — только у администрации бота. "
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
        "Исполнитель предоставляет доступ к сервису генерации изображений и видео с помощью AI-моделей (Imagen 4, Veo 3.1). "
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

@dp.callback_query(F.data == "menu_anim")
async def menu_anim(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cr = await get_credits(cb.from_user.id)
    text = (
        f"🎞️ <b>Анимировать фото</b>\n\n"
        f"💳 Баланс: <b>{cr} кр</b>\n"
        f"💳 Стоимость: <b>{ANIM_CREDIT_COST} кр</b>\n\n"
        f"Выбери режим:\n"
        f"🖼️ <b>Один кадр</b> — анимируй фото по промту\n"
        f"🖼️🖼️ <b>Два кадра</b> — плавный переход между двумя фото"
    )
    if cr < ANIM_CREDIT_COST:
        try:
            await cb.message.edit_text(
                f"❌ Недостаточно кредитов\nНужно {ANIM_CREDIT_COST} кр, у тебя {cr} кр.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⚡ Купить кредиты", callback_data="menu_buy")],
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")],
                ]), parse_mode="HTML"
            )
        except Exception:
            await cb.message.answer(f"❌ Недостаточно кредитов. Нужно {ANIM_CREDIT_COST} кр.")
        await cb.answer()
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼️ Один кадр",       callback_data="anim_mode:one")],
        [InlineKeyboardButton(text="🖼️🖼️ Два кадра",     callback_data="anim_mode:two")],
        [InlineKeyboardButton(text="❌ Отмена",            callback_data="back_main")],
    ])
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
                [InlineKeyboardButton(text="❌ Отмена",         callback_data="back_main")],
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
            [InlineKeyboardButton(text="❌ Отмена",         callback_data="back_main")],
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
    prompt = message.text.strip()
    first_bytes = bytes(data["first_photo"])
    last_bytes = bytes(data["last_photo"]) if data.get("last_photo") else None
    aspect = data.get("aspect_ratio", "16:9")
    mode = data.get("anim_mode", "one")

    cr = await get_credits(message.from_user.id)
    if cr < ANIM_CREDIT_COST:
        await state.clear()
        await message.answer(f"❌ Недостаточно кредитов. Нужно {ANIM_CREDIT_COST} кр, у тебя {cr}.")
        return

    ok = await deduct(message.from_user.id, ANIM_CREDIT_COST)
    if not ok:
        await state.clear()
        await message.answer("❌ Ошибка списания. Попробуй ещё раз.")
        return

    await state.clear()
    mode_label = "🖼️🖼️ Два кадра" if mode == "two" else "🖼️ Один кадр"
    wait = await message.answer(
        f"⏳ Анимирую фото...\n\n"
        f"🎬 Veo 3.1 | {mode_label} | {aspect}\n"
        f"<i>{prompt[:80]}</i>\n\n"
        f"⏱ Обычно 1–6 минут. Пришлю как только готово 👇",
        parse_mode="HTML"
    )

    try:
        vid_bytes = await api_animate_image(first_bytes, prompt, aspect, last_bytes)
        await log_gen(message.from_user.id, "animate", "veo-3.1-animate", ANIM_CREDIT_COST)
        cr_left = await get_credits(message.from_user.id)
        caption = (
            f"✅ Готово! 🎞️ Анимация фото\n"
            f"💳 Списано {ANIM_CREDIT_COST} кр | Остаток: {cr_left} кр"
        )
        try:
            await message.answer_video(
                BufferedInputFile(vid_bytes, "animation.mp4"),
                caption=caption,
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔄 Ещё раз", callback_data="menu_anim"),
                     InlineKeyboardButton(text="🏠 Главное", callback_data="new_main")],
                ]),
                supports_streaming=True,
            )
        except Exception:
            await message.answer_document(
                BufferedInputFile(vid_bytes, "animation.mp4"),
                caption=caption + "\n<i>Нажми для воспроизведения</i>",
                parse_mode="HTML"
            )
        await wait.delete()
    except Exception as e:
        await add_credits(message.from_user.id, ANIM_CREDIT_COST)
        await wait.edit_text(
            f"❌ Ошибка: {e}\n\nКредиты возвращены.",
            reply_markup=kb_back()
        )


# ══════════════════════════════════════════════════════════
#  ОБЫЧНЫЕ СООБЩЕНИЯ (вне FSM — консультант по умолчанию)
# ══════════════════════════════════════════════════════════

@dp.message(~F.text.startswith("/privacy") & ~F.text.startswith("/publicoffer") & ~F.text.startswith("/help") & ~F.text.startswith("/ref") & ~F.text.startswith("/start") & ~F.text.startswith("/admin") & ~F.text.startswith("/publicoffer"))
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
    await bot.send_chat_action(message.chat.id, "typing")
    reply = await claude_with_search(uid, message.text)
    try:
        await message.answer(reply, reply_markup=kb_contact(), parse_mode="HTML")
    except Exception:
        await message.answer(reply, reply_markup=kb_contact())

# ══════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════
#  FREEKASSA — ОПЛАТА СБП
# ══════════════════════════════════════════════════════════

def fk_sign_form(amount: int, currency: str, order_id: str) -> str:
    s = f"{FK_MERCHANT_ID}:{amount}:{FK_SECRET_1}:{currency}:{order_id}"
    return hashlib.md5(s.encode()).hexdigest()

def fk_sign_notify(amount: str, order_id: str) -> str:
    s = f"{FK_MERCHANT_ID}:{amount}:{FK_SECRET_2}:{order_id}"
    return hashlib.md5(s.encode()).hexdigest()

def fk_payment_url(order_id: str, amount: int, user_id: int) -> str:
    sign = fk_sign_form(amount, "RUB", order_id)
    return (
        f"https://pay.fk.money/"
        f"?m={FK_MERCHANT_ID}"
        f"&oa={amount}"
        f"&currency=RUB"
        f"&o={order_id}"
        f"&s={sign}"
        f"&us_uid={user_id}"
        f"&lang=ru"
    )

async def fk_create_order(user_id: int, pack_key: str) -> str:
    import time, random
    p = CREDIT_PACKS[pack_key]
    order_id = f"fk_{user_id}_{int(time.time())}_{random.randint(100,999)}"
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO payments_fk (order_id, user_id, credits, amount_rub, pack_key)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (order_id) DO NOTHING
        """, order_id, user_id, p["credits"], p["price"], pack_key)
    return order_id

@dp.callback_query(F.data.startswith("payfk:"))
async def pay_fk(cb: CallbackQuery):
    pack_key = cb.data.split(":")[1]
    p = CREDIT_PACKS[pack_key]
    logging.info(f"payfk: pack={pack_key}, FK_MERCHANT_ID='{FK_MERCHANT_ID}'")
    if not FK_MERCHANT_ID:
        await cb.answer("❌ Оплата через СБП временно недоступна. Напиши @neirosetkaalex", show_alert=True)
        return
    order_id = await fk_create_order(cb.from_user.id, pack_key)
    pay_url = fk_payment_url(order_id, p["price"], cb.from_user.id)
    logging.info(f"payfk url: {pay_url}")
    msg = (
        f"\U0001f3e6 <b>Оплата через СБП / Карту</b>\n\n"
        f"\U0001f4e6 {p['name']}: <b>{p['credits']} кредитов</b>\n"
        f"\U0001f4b0 Сумма: <b>{p['price']}\u20bd</b>\n\n"
        f"Нажми кнопку ниже, выбери банк и оплати.\n"
        f"Кредиты зачислятся <b>автоматически</b> после оплаты \u2705"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"\U0001f4b3 Оплатить {p['price']}\u20bd", url=pay_url)],
        [InlineKeyboardButton(text="\u25c0\ufe0f Назад", callback_data=f"buy:{pack_key}")],
    ])
    try:
        await cb.message.edit_text(msg, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(msg, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# ══════════════════════════════════════════════════════════
#  WEBHOOK-СЕРВЕР ДЛЯ FREEKASSA
# ══════════════════════════════════════════════════════════

async def fk_webhook_handler(request: web.Request) -> web.Response:
    """Принимает уведомление от FreeKassa об успешной оплате."""
    try:
        data = await request.post()
        logging.info(f"FK webhook: {dict(data)}")

        merchant_id = data.get("MERCHANT_ID", "")
        amount      = data.get("AMOUNT", "")
        order_id    = data.get("MERCHANT_ORDER_ID", "")
        sign        = data.get("SIGN", "")

        # Проверяем подпись
        expected = fk_sign_notify(amount, order_id)
        if sign != expected:
            logging.warning(f"FK wrong sign: got {sign}, expected {expected}")
            return web.Response(text="WRONG SIGN")

        # Проверяем merchant_id
        if merchant_id != FK_MERCHANT_ID:
            return web.Response(text="WRONG MERCHANT")

        # Ищем заказ в БД
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM payments_fk WHERE order_id=$1", order_id
            )
            if not row:
                logging.warning(f"FK order not found: {order_id}")
                return web.Response(text="ORDER NOT FOUND")

            if row["status"] == "paid":
                return web.Response(text="YES")  # уже обработан

            # Зачисляем кредиты
            await conn.execute(
                "UPDATE payments_fk SET status='paid' WHERE order_id=$1", order_id
            )

        user_id = row["user_id"]
        credits = row["credits"]
        amount_rub = row["amount_rub"]

        await add_credits(user_id, credits)
        await log_payment(user_id, credits, amount_rub, "freekassa")
        await process_referral_bonus(user_id)

        # Уведомляем пользователя
        try:
            new_balance = await get_credits(user_id)
            await bot.send_message(
                user_id,
                "\u2705 <b>Оплата прошла успешно!</b>\n\n"
                f"\u2795 Начислено: <b>{credits} кредитов</b>\n"
                f"\U0001f4b3 Баланс: <b>{new_balance} кредитов</b>\n\n"
                "Можешь начинать генерацию! \U0001f680",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="\U0001f5bc\ufe0f Создать фото", callback_data="menu_image")],
                    [InlineKeyboardButton(text="\U0001f3ac Создать видео", callback_data="menu_video")],
                ])
            )
        except Exception as e:
            logging.error(f"FK notify user error: {e}")

        return web.Response(text="YES")

    except Exception as e:
        logging.error(f"FK webhook error: {e}")
        return web.Response(text="ERROR", status=500)


async def start_webhook_server():
    """Запускаем aiohttp сервер для FreeKassa webhook."""
    app = web.Application()
    app.router.add_post("/fk_webhook", fk_webhook_handler)
    app.router.add_get("/health", lambda r: web.Response(text="OK"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", FK_WEBHOOK_PORT)
    await site.start()
    logging.info(f"✅ FK webhook сервер запущен на порту {FK_WEBHOOK_PORT}")


async def main():
    await init_db()
    await start_webhook_server()
    logging.info("✅ Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
