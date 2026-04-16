import asyncio
import logging
import asyncpg
import aiohttp
import base64
import hashlib
import hmac
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
FK_SHOP_ID     = os.getenv("FK_SHOP_ID", "72106")
FK_API_KEY     = os.getenv("FK_API_KEY", "")
FK_SECRET1     = os.getenv("FK_SECRET1", "")
FK_SECRET2     = os.getenv("FK_SECRET2", "")
FK_WEBHOOK_URL = os.getenv("FK_WEBHOOK_URL", "")  # https://yourbot.up.railway.app/fk-notify

# ─── FreeKassa ────────────────────────────────────────────
FK_MERCHANT_ID = os.getenv("FK_MERCHANT_ID", "")
FK_SECRET_1    = os.getenv("FK_SECRET_1", "")
FK_SECRET_2    = os.getenv("FK_SECRET_2", "")
FK_WEBHOOK_PORT = int(os.getenv("PORT", "8080"))  # Railway использует PORT

FREE_CREDITS   = 150  # кредитов при первом /start без рефералки
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
        "name": "· Imagen 4 Fast",
        "model_id": "imagen-4.0-fast-generate-001",
        "api": "imagen",
        "credits": 7,
        "price": "4₽",
        "speed": "~2 сек",
        "desc": "Быстро и качественно",
    },
    "img_std": {
        "name": "· Imagen 4",
        "model_id": "imagen-4.0-generate-001",
        "api": "imagen",
        "credits": 10,
        "price": "6₽",
        "speed": "~5 сек",
        "desc": "Флагман, чёткий текст",
    },
    "img_ultra": {
        "name": "◆ Imagen 4 Ultra",
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
        "name": "🍌 Nano Banana 2",
        "model_id": "gemini-3.1-flash-image-preview",
        "api": "gemini",
        "credits": 13,
        "price": "6₽",
        "speed": "~4 сек",
        "desc": "Новейший, лучшее качество",
    },
    "nb_pro": {
        "name": "🍌 Nano Banana Pro",
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
        "name": "🎯 Пробный", "credits": 250, "price": 149, "stars": 60,
        "desc": "35 фото / 2 видео Lite / 1 видео Fast",
        "badge": "Попробовать за 149₽",
    },
    "p50": {
        "name": "🥉 Старт", "credits": 500, "price": 279, "stars": 112,
        "desc": "70 фото / 5 видео Lite / 2 видео Fast / 1 видео Pro",
        "badge": "Популярный старт",
    },
    "p150": {
        "name": "🥈 Базовый", "credits": 1500, "price": 799, "stars": 320,
        "desc": "210 фото / 15 видео Lite / 8 видео Fast / 3 видео Pro",
        "badge": "Хорошая экономия",
    },
    "p500": {
        "name": "🥇 Про", "credits": 5000, "price": 2490, "stars": 996,
        "desc": "700 фото / 50 видео Lite / 28 видео Fast / 12 видео Pro",
        "badge": "Выгоднее на 13%",
    },
    "p1200": {
        "name": "💎 Бизнес", "credits": 12000, "price": 5790, "stars": 2316,
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
            CREATE TABLE IF NOT EXISTS fk_orders (
                order_id   TEXT PRIMARY KEY,
                user_id    BIGINT NOT NULL,
                credits    INTEGER NOT NULL,
                amount_rub INTEGER NOT NULL,
                pack       TEXT,
                status     TEXT DEFAULT 'pending',
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
#  FREEKASSA — ГЕНЕРАЦИЯ ССЫЛОК И ВЕБХУК
# ══════════════════════════════════════════════════════════

def fk_pay_url(amount: float, order_id: str, currency: str = "RUB", method_id: str = "") -> str:
    """Формирует ссылку на оплату FreeKassa.
    Подпись: MD5(shopId:amount:secret1:currency:orderId)
    """
    amount_str = f"{float(amount):.2f}"  # FreeKassa требует формат "199.00"
    sign_str = f"{FK_SHOP_ID}:{amount_str}:{FK_SECRET1}:{currency}:{order_id}"
    sign = hashlib.md5(sign_str.encode()).hexdigest()
    url = (
        f"https://pay.fk.money/?m={FK_SHOP_ID}"
        f"&oa={amount_str}"
        f"&currency={currency}"
        f"&o={order_id}"
        f"&s={sign}"
        f"&lang=ru"
    )
    if method_id:
        url += f"&i={method_id}"
    return url


async def fk_create_order(amount: float, order_id: str, user_id: int,
                         payment_id: int = 36, currency: str = "RUB") -> str:
    """Создаёт заказ через FreeKassa API и возвращает ссылку на оплату.
    payment_id: 36 = Card RUB API, 44 = СБП API
    """
    import time as _time
    nonce = str(int(_time.time() * 1000))
    amount_str = f"{float(amount):.2f}"  # "2490.00"

    # Только нужные поля — без дублей
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


def fk_verify_webhook(data: dict) -> bool:
    """Проверяет подпись вебхука от FreeKassa.
    Подпись: MD5(MERCHANT_ID:AMOUNT:SECRET2:MERCHANT_ORDER_ID)
    """
    sign = hashlib.md5(
        f"{data['MERCHANT_ID']}:{data['AMOUNT']}:{FK_SECRET2}:{data['MERCHANT_ORDER_ID']}"
        .encode()
    ).hexdigest()
    return sign == data.get("SIGN", "")


def fk_api_signature(params: dict) -> str:
    """HMAC-SHA256 подпись для API запросов."""
    sorted_vals = [str(v) for k, v in sorted(params.items())]
    sign_str = "|".join(sorted_vals)
    return hmac.new(FK_API_KEY.encode(), sign_str.encode(), hashlib.sha256).hexdigest()


# pending_fk_payments — резервный кеш в памяти (основное хранилище — PostgreSQL fk_orders)
pending_fk_payments: dict = {}


async def fk_save_order(order_id: str, user_id: int, credits: int, amount: int, pack: str):
    """Сохраняем заказ в БД (защита от потери при перезапуске)."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO fk_orders (order_id, user_id, credits, amount_rub, pack)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (order_id) DO NOTHING
        """, order_id, user_id, credits, amount, pack)


async def fk_get_order(order_id: str) -> dict | None:
    """Получаем заказ из БД."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM fk_orders WHERE order_id=$1", order_id
        )
        return dict(row) if row else None


async def fk_mark_paid(order_id: str):
    """Помечаем заказ как оплаченный."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE fk_orders SET status='paid' WHERE order_id=$1", order_id
        )


# ══════════════════════════════════════════════════════════
#  ОБРАБОТКА ОШИБОК
# ══════════════════════════════════════════════════════════

def friendly_error(e: Exception) -> str:
    """Возвращает понятное сообщение для клиента."""
    err = str(e)
    if "429" in err or "spending cap" in err or "quota" in err.lower():
        return "⚠️ Временно превышен лимит запросов. Попробуй через несколько минут."
    if "503" in err or "unavailable" in err.lower() or "overloaded" in err.lower():
        return "⚠️ Сервис временно перегружен. Попробуй через 1–2 минуты."
    if "timeout" in err.lower() or "timed out" in err.lower():
        return "⚠️ Превышено время ожидания. Попробуй ещё раз."
    if "400" in err:
        return "⚠️ Промт не принят системой. Попробуй переформулировать запрос."
    if "403" in err or "permission" in err.lower():
        return "⚠️ Нет доступа к сервису. Мы уже разбираемся."
    return "⚠️ Небольшая техническая проблемка. Попробуй ещё раз или напиши @neirosetkaalex"


async def notify_admin_error(context: str, e: Exception):
    """Отправляет реальную ошибку админу."""
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔴 <b>Ошибка</b> | {context}\n\n<code>{str(e)[:800]}</code>",
            parse_mode="HTML"
        )
    except Exception:
        pass


# ══════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📷 Изображение", callback_data="menu_image"),
            InlineKeyboardButton(text="🎬 Видео",        callback_data="menu_video"),
        ],
        [
            InlineKeyboardButton(text="🖌️ Редактировать фото", callback_data="menu_edit"),
            InlineKeyboardButton(text="🏃 Анимировать фото",   callback_data="menu_anim"),
        ],
        [
            InlineKeyboardButton(text="🤖 Консультант AI", callback_data="menu_chat"),
        ],
        [
            InlineKeyboardButton(text="💵 Баланс",         callback_data="menu_balance"),
            InlineKeyboardButton(text="⚡ Купить кредиты", callback_data="menu_buy"),
        ],
        [
            InlineKeyboardButton(text="🤝 Пригласить друга", callback_data="menu_ref"),
            InlineKeyboardButton(text="🛍 Магазин",           callback_data="menu_shop"),
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
            text=f"🏦 СБП — {p['price']}₽",
            callback_data=f"payfk:{pack_key}:sbp"
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
        [KeyboardButton(text="📷 Создать фото"), KeyboardButton(text="🎬 Создать видео")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🏡 Главное меню")],
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
Claude: Haiku 4.5, Sonnet 4.6 (новейшая), Opus 4 — доступны в Claude Pro $20/мес
ChatGPT: GPT-5.2 Instant, GPT-5.3, GPT-5.4 Pro — GPT-4o ВЫВЕДЕН апрель 2026
Grok: Grok 4, Grok 4.1, Grok 4.20, Grok 4 Heavy — в SuperGrok $30/мес
Gemini: Gemini 2.5 Flash, Gemini 3.1 Pro — в Google One AI Premium $20/мес
Midjourney: v7 (текущая) — Basic $10, Standard $30, Pro $60, Mega $120/мес

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
Модели: GPT-5.2 Instant (быстрая), GPT-5.3 (стандартная), GPT-5.4 Pro (топ) — GPT-4o ВЫВЕДЕН апрель 2026
Free — GPT-5.3 с жёсткими лимитами, с рекламой (США с февраля 2026)
Go — $8/мес: GPT-5.2 Instant, лимиты х10 от Free, расширенная память, с рекламой, без Codex/Deep Research/Agent Mode — для бытовых задач
Plus — $20/мес: GPT-5 полный, DALL-E/GPT Image, Deep Research 10/мес, Codex, Agent Mode, без рекламы — лучший выбор для работы
Pro 5x — $100/мес: как Plus, но Codex 5х лимиты (временно 10х до 31.05.2026)
Pro 20x — $200/мес: GPT-5.4 Pro, Deep Research 250/мес, Codex 20х лимиты

ВАЖНО — Sora ЗАКРЫТА с 24 марта 2026:
Приложение отключается 26 апреля 2026, API — 24 сентября 2026.
Причина: $1 млн/день расходов при аудитории менее 500к, смена стратегии перед IPO.
В Plus Sora больше НЕТ. Альтернативы: Runway Gen-4, Kling 3.0, Veo 3.1 (в этом боте!)

Claude (Anthropic):
Модели: Haiku 4.5 (быстрая), Sonnet 4.6 (стандартная/новейшая), Opus 4 (максимум)
Free — Sonnet 4.6 с лимитами, веб-поиск, создание файлов
Pro — $20/мес ($17 при годовой): Opus 4, Sonnet 4.6, Projects, Claude Code, приоритет
Max 5x — $100/мес: всё из Pro, лимиты в 5 раз выше, Opus 4.6 с контекстом 1М токенов
Max 20x — $200/мес: всё из Max 5x, лимиты в 20 раз выше Pro
Team — $30/чел/мес ($20 при годовой): совместная работа, общий биллинг

Grok (xAI):
Модели: Grok 4, Grok 4.1, Grok 4.20, Grok 4 Heavy (новейшие 2026)
Free — Grok 3, ~10 запросов каждые 2 часа, базовая Aurora генерация изображений
SuperGrok Lite — $10/мес: Grok 4 базовый, больше лимитов, 1 AI-агент
SuperGrok — $30/мес ($300/год): Grok 4/4.1, DeepSearch, 4 AI-агента, безлимит изображений, Big Brain Mode, голос, 128К контекст
SuperGrok Heavy — $300/мес: Grok 4 Heavy, 8 агентов параллельно, 256К+ контекст, максимальные лимиты
X Premium+ — $40/мес: Grok доступ + фичи X (синяя галочка, без рекламы)
Особенность: данные X/Twitter в реальном времени, Aurora изображения бесплатно

Cursor (AI-редактор кода):
Модели: Claude Sonnet 4.6, GPT-5, Gemini и другие на выбор (кредитная система с июня 2025)
Hobby — бесплатно: ограниченные Tab и Agent запросы, без кредитной карты
Pro — $20/мес ($16 при годовой): безлимит Tab автодополнений, $20/мес кредитов на агентов
Pro+ — $60/мес: $60/мес кредитов (3х от Pro), фоновые агенты
Ultra — $200/мес: $400/мес кредитов (20х от Pro), приоритет
Teams — $40/польз./мес: всё из Pro + централизованный биллинг, SSO, аналитика
Важно: Auto режим безлимитный, но ручной выбор премиум-моделей тратит кредиты

Krea AI (генерация изображений в реальном времени):
Free — ограниченный доступ, базовые функции
Pro — $36/мес: безлимит генераций, upscale 4K, real-time режим, видео, Flux модели

Suno (генерация музыки):
Версия: v4.5 — студийное качество, все жанры, реалистичные голоса
Free — 50 кредитов/день (~5 треков), без коммерческих прав
Pro — $8/мес: 2500 кредитов/мес, коммерческие права, без рекламы
Premier — $24/мес: 10000 кредитов/мес, приоритетная генерация

Kling AI (генерация видео):
Версия: Kling 3.0 Omni (текущая), Kling 2.1
Free — 66 кредитов/день (~3 видео по 5 сек)
Standard — $8/мес: 660 кредитов/мес
Pro — $27/мес: 3000 кредитов/мес, приоритет, Pro режим
Лучшее соотношение качество/цена для видео без Sora в 2026 году

Runway (генерация видео):
Версия: Gen-4 Turbo (2026) — кинематографическое качество, генерация в реальном времени
Free — 125 кредитов (разово, ~25 сек видео)
Standard — $15/мес: 625 кредитов
Pro — $35/мес: 2250 кредитов
Enterprise — $95/мес: безлимит
Лучший выбор для кинематографичного видео после закрытия Sora

ElevenLabs (синтез речи и клонирование голоса):
Версия: движок v3 — неотличим от живого голоса, 70+ языков
Free — 10 000 кредитов/мес (≈10 мин аудио), без коммерческих прав
Starter — $5/мес: 30 000 кредитов, коммерческие права, мгновенное клонирование
Creator — $22/мес ($11 первый мес со скидкой 50%): 100 000 кредитов, профессиональное клонирование, 192kbps
Pro — $99/мес: 500 000 кредитов, студийное качество, API

HeyGen (AI-аватары и видео):
Версия: HeyGen 3.0 — реалистичные аватары, синхронизация губ
Free — 3 видео/мес, водяной знак
Creator — $29/мес: без водяного знака, перевод видео (Video Translate), 5 аватаров
Business — $89/мес: командный доступ, API, приоритет

━━━━━━━━━━━━━━━━━━━━━━
VPN ДЛЯ России — ПОЛНЫЙ ГАЙД (апрель 2026)
━━━━━━━━━━━━━━━━━━━━━━
СИТУАЦИЯ апрель 2026:
• Telegram заблокирован 4 апреля 2026, 100% блокировка с 10 апреля
• Instagram, Facebook, X, LinkedIn, Discord — заблокированы
• С 15 апреля 2026 крупные российские платформы (Wildberries, Ozon, Яндекс) начали блокировать пользователей с VPN
• РКН блокирует: обычный WireGuard, OpenVPN, L2TP, большинство платных VPN-сервисов
• Работают: AmneziaWG 2.0, VLESS+XHTTP, TrustTunnel (AdGuard), Shadowsocks
• Apple удалила из российского App Store: Streisand, V2Box, v2RayTun, Happ
• Использование VPN в России не запрещено и не штрафуется для физлиц

━━━━ РЕЙТИНГ РАБОЧИХ VPN ДЛЯ России (апрель 2026) ━━━━

🏆 1. AMNEZIA VPN — лучший выбор
Сайт: https://amnezia.org | GitHub: https://github.com/amnezia-vpn/amnezia-client
Что это: открытый исходный код, разворачивает VPN на твоём VPS-сервере
Почему лучший: протоколы AmneziaWG 2.0 и XRay неотличимы от обычного трафика
Бесплатно: Amnezia Free (amnezia.org/free) — готовые серверы без VPS
Платно: Amnezia Premium — надёжнее, быстрее
Установка Android: Google Play → поиск "Amnezia VPN" → или APK с GitHub (для Android 9+)
Установка iPhone/iPad: App Store → "Amnezia VPN" (если нет — скачать через иностранный Apple ID)
Установка Windows/Mac: amnezia.org/downloads
Протоколы: AmneziaWG (лучший), XRay VLESS+Reality, OpenVPN, WireGuard, Shadowsocks

🥈 2. ADGUARD VPN — лучший платный с блокировкой рекламы
Сайт: https://adguard-vpn.com
Протокол: TrustTunnel (маскируется под HTTPS, ТСПУ не видит)
Цена: бесплатно 3 ГБ/мес | Personal ~$2.5/мес при годовой
Особенность: встроенный блокировщик рекламы, интерфейс на русском
Установка Android: Google Play → "AdGuard VPN"
Установка iPhone: App Store → "AdGuard VPN"
Установка Windows: adguard-vpn.com/ru/windows.html
Установка Mac: adguard-vpn.com/ru/mac.html
Работает: стабильно, скорость до 500 Мбит/с по тестам

🥉 3. ZOOGVPN — стабильный, есть бесплатный план
Сайт: https://zoogvpn.com
Протоколы: Shadow, ZoogTLS (обходят DPI), также OpenVPN, WireGuard, IKEv2
Цена: бесплатно 10 ГБ/мес | от $1.99/мес при годовой
Установка: zoogvpn.com → Downloads → выбрать платформу
Особенность: один из немногих с рабочим бесплатным планом в РФ

4. OUTLINE (на своём VPS) — максимальная надёжность
Что это: Google Jigsaw создали Outline, трафик выглядит как обычный HTTPS
Цена: VPS ~$5/мес (Hetzner, DigitalOcean, Vultr)
Как настроить:
  1. Купить VPS (Hetzner: hetzner.com, DigitalOcean: digitalocean.com)
  2. Скачать Outline Manager: getoutline.org → Get Outline Manager
  3. Запустить, вставить IP сервера, нажать "Set up Outline"
  4. Скачать Outline Client на телефон/ПК
  5. Скопировать ключ из Manager и вставить в Client
Установка Outline Client:
  Android: Google Play → "Outline"
  iPhone: App Store → "Outline"
  Windows/Mac: getoutline.org

5. PROTON VPN — бесплатный, без лимита трафика
Сайт: https://protonvpn.com
Цена: бесплатно (3 страны, медленнее) | Plus от $4/мес
Протокол: Stealth (обходит DPI в России)
Важно: в настройках включить Stealth протокол!
Установка Android: Google Play → "Proton VPN"
Установка iPhone: нужен иностранный Apple ID (ProtonVPN удалён из RU App Store)
Установка Windows: protonvpn.com/download

6. WINDSCRIBE — 10 ГБ/мес бесплатно
Сайт: https://windscribe.com
Цена: бесплатно 10 ГБ/мес | Pro от $3/мес при годовой
Протокол: Stealth/SOCKS5 для обхода DPI
Установка: windscribe.com → Download

━━━━ ЧТО ЗАБЛОКИРОВАНО В App STORE RUSSIA ━━━━
Удалены по требованию РКН: Streisand, V2Box, v2RayTun, Happ, некоторые другие
Как скачать заблокированные iOS VPN:
1. Зарегистрировать иностранный Apple ID (США/Германия)
2. Выйти из российского Apple ID в App Store
3. Войти с иностранным и скачать нужное приложение
4. Вернуться в свой российский Apple ID

━━━━ НАСТРОЙКА TELEGRAM ПРОКСИ ━━━━
Telegram → Настройки → Данные и хранилище → Использовать прокси
→ SOCKS5 или MTProto → найти рабочие прокси на @ProxyMTProto или @socks5_proxy_list

━━━━ ПРОТОКОЛЫ: ЧТО РАБОТАЕТ В РФ 2026 ━━━━
✅ Работают стабильно: AmneziaWG 2.0, VLESS+XHTTP, TrustTunnel, Shadowsocks, Outline
⚠️ Нестабильно: VLESS+Reality (ИИ-модуль ТСПУ учится блокировать)
❌ Заблокированы: обычный WireGuard, OpenVPN, L2TP/IPsec

━━━━ ДЛЯ НЕЙРОСЕТЕЙ ━━━━
ChatGPT, Claude, Midjourney — нужен VPN с европейским/американским сервером
Рекомендую: Amnezia Free или AdGuard VPN — просто, бесплатно, работает
После включения VPN зайти через браузер — мобильные приложения могут блокироваться отдельно

━━━━━━━━━━━━━━━━━━━━━━
НАСТРОЙКА НЕЙРОСЕТЕЙ — СОВЕТЫ
━━━━━━━━━━━━━━━━━━━━━━
ChatGPT — как использовать эффективно:
• Custom Instructions: Настройки → Персонализация → Инструкции — задай роль и стиль ответов раз и навсегда
• GPT-5 Projects: создавай проекты под разные задачи (работа, учёба, контент) — у каждого своя память
• Memory: Настройки → Персонализация → Память — включи, ChatGPT будет помнить тебя
• Промт-совет: добавляй "отвечай на русском, кратко" в конце — режим ответа меняется
• Canvas режим: для работы с документами и кодом — лучше чем просто чат
• Voice Mode: полноценный голосовой ассистент на телефоне
• ВАЖНО: Sora удалена из ChatGPT Plus — закрыта 24.03.2026. Альтернативы: Kling, Runway, Veo

Claude — как использовать эффективно:
• Projects: создай проект, загрузи туда документы — Claude будет помнить контекст всего проекта
• Большой контекст 200к токенов: можно загрузить целую книгу или кодовую базу
• Режим мышления (extended thinking): для сложных задач пиши "думай подробно перед ответом"
• Лучшая нейросеть для: написания текстов, анализа документов, кода, рассуждений
• Артефакты: Claude создаёт интерактивные сайты, таблицы, коды прямо в чате

Midjourney — настройка и промпты:
• /settings — включи Midjourney v7 (последняя версия 2026)
• Структура промта: [объект], [стиль], [освещение], [качество] — например: "cat, oil painting style, golden hour lighting, 4k"
• Параметры: --ar 16:9 (соотношение сторон), --s 750 (стилизация), --chaos 20 (вариативность)
• --no [что убрать]: --no text, --no watermark
• Режим Niji: для аниме-стиля добавь --niji 7
• Remix режим: изменяй уже созданные изображения меняя промт

Grok — особенности:
• DeepSearch: глубокий поиск с анализом множества источников
• Данные X/Twitter в реальном времени — уникальная фича, нет у конкурентов
• Think Mode (Big Brain): для сложных задач включи в настройках — думает дольше, отвечает точнее
• Aurora: лучший бесплатный генератор изображений (доступен в Free!)
• Контекст 2М токенов — можно загрузить огромный документ

Perplexity — советы:
• Режимы поиска: Quick (быстро), Pro (глубоко), Research (очень глубоко — только Pro план)
• Spaces: создавай пространства под темы — Perplexity будет знать твой контекст
• Focus: выбирай источники (YouTube, Reddit, Academic) — качество ответов растёт
• Лучшая альтернатива Google для поиска актуальной информации

Cursor — советы для разработчиков:
• .cursorrules файл в корне проекта — задаёт правила для AI на весь проект
• Cmd+K (или Ctrl+K): быстрое редактирование выделенного кода
• Cmd+L: открыть чат с контекстом текущего файла
• @-упоминания: @file, @web, @docs — даёт AI нужный контекст
• Agent Mode: AI сам пишет код, запускает тесты, исправляет ошибки — почти автопилот

ElevenLabs — советы:
• Клонирование голоса: загрузи 1-3 мин чистой записи голоса → готово за 30 сек
• Stability: чем ниже — тем эмоциональнее, чем выше — тем ровнее
• Similarity: насколько голос похож на оригинал (0.75-0.85 оптимально)
• Dubbing Studio: автоматический перевод видео с сохранением голоса — для YouTube/Reels

Промптинг — универсальные советы (работает везде):
• Дай роль: "Ты опытный маркетолог..." — качество ответа вырастет в 2 раза
• Примеры: покажи что хочешь получить — "напиши как в этом примере: [пример]"
• Шаг за шагом: "объясни пошагово" — для сложных тем
• Формат: "ответь списком из 5 пунктов", "таблицей", "в виде JSON"
• Итерации: не принимай первый ответ — проси "улучши", "сделай короче", "добавь примеры"

━━━━━━━━━━━━━━━━━━━━━━
ЦЕНЫ АЛЕКСАНДРА (через @neirosetkaalex)
━━━━━━━━━━━━━━━━━━━━━━
Все подписки оформляются с оплатой в рублях/тенге, без иностранных карт.
ВАЖНО: для использования ChatGPT, Claude, Midjourney и других зарубежных сервисов нужен VPN — это отдельный вопрос от оплаты.

ChatGPT Plus — 2000₽/мес (GPT-5, DALL-E, Deep Research, Codex)
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
ГЛУБОКИЕ ЗНАНИЯ — РЕГИСТРАЦИЯ, НАВИГАЦИЯ, ПРОМПТЫ
━━━━━━━━━━━━━━━━━━━━━━

★ CHATGPT (chat.openai.com)
Регистрация: openai.com → Sign up → email или Google/Apple → подтвердить почту → выбрать план
Из России: нужен VPN (любой европейский сервер). Номер телефона РФ не принимается — нужен виртуальный номер (sms-activate.org, OnlineSim) или попросить друга за рубежом
Навигация сайта:
• Новый чат — кнопка "+" слева вверху или Ctrl+Shift+O
• Модель — выпадающий список вверху по центру (GPT-5.3 по умолчанию, GPT-5 на Plus)
• Projects — левая панель, создать папку для постоянного контекста
• Memory — Settings → Personalization → Memory → Enable
• Custom Instructions — Settings → Personalization → Custom Instructions (задаёт роль постоянно)
• Deep Research — кнопка "Research" внизу чата (только Plus+)
• Codex — отдельная вкладка в левом меню (только Plus+)
• DALL-E/GPT Image — напиши "нарисуй..." или нажми иконку изображения внизу
• Agent Mode — кнопка Tools → выбрать нужные инструменты
• Voice Mode — иконка наушников справа снизу
• Canvas — кнопка "Canvas" внизу (для документов и кода)
Промпты для ChatGPT:
• Добавляй роль: "Ты опытный [профессия]. Помоги мне..."
• Для длинных задач: "Делай шаг за шагом, не торопись"
• Для кода: "Напиши код на Python. После каждого блока объясни что он делает"
• Для текстов: "Пиши в стиле [пример]. Тон: дружелюбный, без канцеляризмов"
• Итерации: "Улучши это. Сделай короче/длиннее/проще"

★ CLAUDE (claude.ai)
Регистрация: claude.ai → Continue with Google/email → из России нужен VPN
Из России: нужен VPN. Оплата только иностранной картой — Александр оформит за тебя: @neirosetkaalex
Навигация:
• Новый чат — кнопка "New chat" слева
• Projects — левая панель → New Project → загрузи файлы, Claude запомнит их
• Артефакты — Claude сам создаёт при просьбе (код, сайты, таблицы — кликабельны справа)
• Extended thinking — "Думай подробно" или кнопка Extended thinking (только Pro/Max)
• Claude Code — отдельный терминальный агент, скачать через npm install -g @anthropic-ai/claude-code
• Cowork — claude.com/cowork — для совместной работы
• Загрузка файлов — иконка скрепки внизу (PDF, код, изображения, до 200К токенов)
Промпты для Claude:
• Лучший для длинных документов: "Прочитай весь файл и..."
• Для анализа: "Найди противоречия / ключевые идеи / структуру аргументов"
• Для кода: "Напиши, потом объясни каждую строку, потом предложи улучшения"
• Для творчества: "Напиши в стиле [автор]. Главное — [характеристика]"

★ MIDJOURNEY (midjourney.com)
Регистрация: midjourney.com → Sign In → Discord или Google → подписка от $10/мес
Из России: VPN + иностранная карта (Александр оформит: @neirosetkaalex)
Работает через Discord ИЛИ через сайт midjourney.com (web-интерфейс с 2024)
Навигация сайта:
• Explore — посмотреть работы других, скопировать промпты
• Create — основная страница генерации
• Imagine bar — поле ввода промта внизу
• Archive — все твои работы
• /settings в Discord — настройки модели, качества, стиля
Ключевые параметры:
• --ar 16:9 / 9:16 / 1:1 — соотношение сторон
• --v 7 — версия модели (актуальная)
• --s 0-1000 — стилизация (750 = стандарт)
• --chaos 0-100 — вариативность (0 = предсказуемо, 100 = дико)
• --q 1 / 2 — качество (2 = лучше, медленнее)
• --no text, logos, watermark — исключить элементы
• --niji 7 — аниме стиль
• --tile — повторяющийся паттерн
Структура промта: [Объект/сцена], [стиль/художник], [освещение], [цвет/настроение], [детали], [параметры]
Пример: "a woman reading book in cozy cafe, cinematic photography, golden hour light, warm tones, bokeh background --ar 3:4 --v 7"

★ GROK (grok.com)
Регистрация: grok.com → Sign up → аккаунт X/Twitter или email → подписка SuperGrok $30/мес
Из России: работает без VPN! X/Twitter доступен. Оплата — карта иностранная или через Александра
Навигация:
• Think Mode — кнопка "Think" под полем ввода (глубокое рассуждение)
• DeepSearch — кнопка "DeepSearch" (глубокий поиск по сети и X, 2-5 мин)
• Aurora Images — вкладка "Images" слева или /imagine в чате
• Aurora Video — вкладка "Videos" (720p, до 10 сек на SuperGrok)
• Voice — иконка микрофона (разговорный режим)
• Big Brain Mode — для очень сложных задач (SuperGrok Heavy)
• Agents — несколько AI работают параллельно (SuperGrok 4 агента)
Особенности Grok:
• Знает что происходит на X/Twitter прямо сейчас — уникальная фича
• Самый "свободный" в ответах среди топ-моделей
• Aurora — лучший БЕСПЛАТНЫЙ генератор изображений (реалистичные люди)

★ GEMINI (gemini.google.com)
Регистрация: gemini.google.com → войти через Google аккаунт → Google One AI Premium $20/мес для Gemini 3.1 Pro
Из России: нужен VPN. Google аккаунт создать на VPN
Навигация:
• Gemini Advanced — включается автоматически на платном плане
• Gems — левое меню → My Gems → создать кастомного ассистента
• Extensions — настройки → Extensions → подключить Gmail, Drive, Docs, YouTube
• NotebookLM — отдельный инструмент Google, notebooklm.google.com — загружай документы, задавай вопросы
• Deep Research — кнопка внизу чата (изучает тему несколько минут)
• Image generation — Imagen 3 встроена, попроси "нарисуй..."
Интеграции:
• Gmail — "Найди письма от [имя] за последний месяц"
• Google Drive — "Проанализируй мои последние документы"
• YouTube — "Summarize this video: [ссылка]"

★ PERPLEXITY (perplexity.ai)
Регистрация: perplexity.ai → Sign up → email или Google → Pro $20/мес
Из России: нужен VPN для регистрации. Работает нестабильно без VPN
Навигация:
• Search bar — центр главной страницы
• Focus — выбор источников: Web, Academic, YouTube, Reddit, Writing, Math
• Spaces — левое меню → + → создать пространство с постоянным контекстом
• Files — загрузить PDF, CSV для анализа (Pro)
• Deep Research — кнопка справа от поиска (длительный анализ, Pro)
• Collections — сохранять интересные поиски
Как использовать:
• Для поиска: просто пиши запрос как вопрос, Perplexity даёт ответ с источниками
• Для академических статей: выбери Focus → Academic
• Для сравнений: "Сравни X и Y по параметрам [список]"
• Лучшая альтернатива Google для актуальных новостей

★ CURSOR (cursor.com)
Регистрация: cursor.com → Download → установить → Sign in → Pro $20/мес
Работает как VS Code — весь привычный интерфейс, только с AI
Навигация:
• Cmd+K / Ctrl+K — редактировать выделенный код инлайн
• Cmd+L / Ctrl+L — открыть AI чат с контекстом файла
• Cmd+I / Ctrl+I — Composer для многофайловых задач
• Tab — автодополнение (умнее GitHub Copilot)
• @ — упоминания контекста: @file, @folder, @web, @docs, @git
• Agent Mode — в чате напечатай задачу, Cursor сам выполнит (пишет, тестирует, фиксит)
• Background Agents — выполняет задачи пока ты работаешь в другом файле (Pro+)
• .cursorrules — файл в корне проекта, задаёт правила для всего проекта
Советы:
• Начни с описания задачи на русском — Cursor поймёт
• "Напиши [функцию], добавь обработку ошибок, напиши тесты"
• После агента всегда проверяй изменения в Git diff

★ ELEVENLABS (elevenlabs.io)
Регистрация: elevenlabs.io → Sign up → email/Google → Starter $5/мес или Creator $22/мес
Из России: VPN + иностранная карта (Александра: @neirosetkaalex)
Навигация сайта:
• Speech Synthesis — главная вкладка, текст → голос
• Voice Library — огромная библиотека голосов, фильтр по языку/полу/возрасту
• Voice Cloning → Add Voice → Instant Clone (1-3 мин аудио) или Professional Clone (30+ мин)
• Projects — длинные аудиокниги/подкасты с главами
• Dubbing Studio — загрузить видео → перевод с сохранением голоса
• Speech to Speech — говоришь своим голосом, выходит другой
• Sound Effects — генерация звуков по тексту
• Conversational AI — создать голосового агента/бота
Настройки голоса:
• Stability 0-100: низкий = эмоциональный, высокий = ровный (рекомендую 50-70)
• Similarity: насколько похож на оригинал (75-85 оптимально)
• Style: интенсивность стиля (0-100)
• Speed: скорость речи (0.7-1.3)
Промпты для клонирования: запись должна быть без фоновых шумов, разнообразная интонация (вопросы, восклицания, пауза)

★ HEYGEN (heygen.com)
Регистрация: heygen.com → Sign up → email → Creator $29/мес
Из России: VPN + иностранная карта
Навигация:
• Video Templates — готовые шаблоны по категориям
• AI Avatar → Create — создать видео с аватаром (выбрать аватар → ввести скрипт)
• Video Translate — загрузить видео → выбрать язык → клон голоса + синхронизация губ
• Talking Photo — загрузить фото → текст → говорящее фото
• Streaming Avatar — для прямых эфиров и интеграций
Как сделать видео с аватаром:
1. AI Avatar → Browse Avatars → выбрать
2. Ввести текст скрипта (или загрузить аудио)
3. Выбрать язык, голос
4. Generate (2-5 минут)

★ RUNWAY (runwayml.com)
Регистрация: runwayml.com → Sign up → Standard $15/мес
Из России: VPN + иностранная карта
Навигация:
• Gen-4 — главная страница генерации видео
• Text to Video — промт → видео
• Image to Video — загрузить фото → промт движения → видео
• Video to Video — стилизация существующего видео
• Lip Sync — синхронизация губ с аудио
• Motion Brush — вручную задать где и как должно двигаться
• Camera Controls — управление движением камеры (Pan, Zoom, Rotate)
Промпты для Runway:
• Описывай движение: "camera slowly zooms in", "gentle breeze moves leaves"
• Стиль: "cinematic, 4K, film grain"
• Всегда добавляй: "no text, no subtitles, smooth motion"
• Начинай с описания объекта, потом действие, потом атмосфера

★ SUNO (suno.com)
Регистрация: suno.com → Sign in → Google/Discord → Pro $8/мес
Из России: работает без VPN! Оплата картой через Александра
Навигация:
• Create — главная страница, поле ввода промта
• Custom Mode — переключатель вверху → пиши отдельно текст и стиль
• Covers — переделать существующую песню
• Trending — популярные треки для вдохновения
Custom Mode поля:
• Lyrics: полный текст песни с метками [Verse], [Chorus], [Bridge], [Outro]
• Style of Music: "upbeat pop, female vocals, 90s synth, energetic"
• Title: название трека
Промпты для стиля: жанр + темп + голос + инструменты + эпоха
Пример: "dark trap, male vocals, slow 80 bpm, piano and 808 bass, melancholic"

★ KLING AI (klingai.com)
Регистрация: klingai.com → Sign up → Standard $8/мес
Из России: VPN. Оплата картой через Александра
Навигация:
• AI Video → Text to Video — промт → видео (5 или 10 сек)
• AI Video → Image to Video — фото → анимация
• AI Image — генерация изображений
• AI Camera — управление движением камеры
• Kling 3.0 Omni — выбрать в настройках модели
Режимы:
• Standard Mode — быстро, дешевле
• Pro Mode — лучше качество, больше кредитов
• Professional Mode — максимум (Kling Pro план)
Промпты для Kling:
• Описывай сцену → действие → детали камеры
• "Close-up shot, [subject], [action], cinematic lighting, 4K quality"
• Kling плохо рендерит кириллицу — промты только на английском
• Для реалистичных людей: опиши внешность детально

★ CANVA (canva.com)
Регистрация: canva.com → Sign up → бесплатно, Pro $17/мес
Из России: доступен без VPN! Оплата картой через Александра
Навигация:
• Home → Templates — выбрать шаблон по типу (пост, презентация, логотип...)
• Magic Studio — AI инструменты (левая панель при редактировании)
• Magic Design — опишешь идею, Canva создаст дизайн
• Magic Write — AI текст прямо в дизайне
• Text to Image — генерация изображений внутри Canva
• Background Remover — Pro, один клик
• Magic Eraser — убрать объект с фото
• Resize Magic — одним кликом изменить под все соцсети
• Brand Kit — Pro, загрузить логотип/цвета компании
Советы:
• Ctrl+Z — отмена, как везде
• Шрифты: скачай готовые брендовые наборы
• Экспорт: Share → Download → PDF Print (лучшее качество)

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

━━━━━━━━━━━━━━━━━━━━━━
ЭКСПЕРТНЫЕ ПРАВИЛА — СТРОГО ОБЯЗАТЕЛЬНО
━━━━━━━━━━━━━━━━━━━━━━
ТЫ ЭКСПЕРТ ПО НЕЙРОСЕТЯМ — это твоя главная роль. Ты знаешь всё: архитектуры, тарифы, промптинг, ограничения, отличия между моделями, реальные кейсы использования.

ЧЕСТНОСТЬ — АБСОЛЮТНЫЙ ПРИОРИТЕТ:
• Если не знаешь точного ответа — НИКОГДА не придумывай. Скажи: "Уточню" и используй web_search.
• Если информация могла устареть — предупреди: "По последним данным..." и проверь поиском.
• Не приукрашивай возможности нейросетей — говори реалистично.
• Если клиент говорит что-то неверное про нейросети — вежливо но чётко поправь с объяснением.

РАСПОЗНАВАНИЕ ФЕЙКОВ И МИФОВ:
• "ChatGPT слышит мои разговоры" — ФЕЙК. Объясни как это работает на самом деле.
• "Нейросеть стала живой/сознательной" — ФЕЙК. Объясни что это языковые модели.
• "GPT-5 лучше во всём чем Claude" — НЕТОЧНО. У каждой модели свои сильные стороны.
• "Midjourney v7 хуже v6" — ФЕЙК. v7 лучше по большинству метрик.
• Слухи о "секретных режимах", "разблокировке", "джейлбрейках" — объясняй риски и что они перестают работать.
• Если клиент верит в миф — не высмеивай, а доходчиво объясни как всё устроено.

АНАЛИЗ НОВОЙ ИНФОРМАЦИИ:
• Когда клиент присылает новость или скриншот — анализируй критически.
• Проверяй источник: официальный блог компании > крупные СМИ > твиты > анонимные каналы.
• Если новость сомнительная — сделай web_search для проверки.
• Отличай маркетинговые заявления от реальных возможностей.

СРАВНЕНИЕ НЕЙРОСЕТЕЙ — ЧЕСТНО:
Для текста/анализа: Claude Opus 4 > GPT-5.3 > Grok 4
Для кода: Claude Sonnet 4.6 > GPT-5 ≈ Grok 4 (по SWE-bench Sonnet 4.6 лидирует)
Для изображений: Midjourney v7 > Imagen 4 Ultra > Grok Aurora (бесплатно!) > DALL-E 3
Для видео: Veo 3.1 (Google) ≈ Runway Gen-4 > Kling 3.0 (Sora ЗАКРЫТА с 24.03.2026)
Для голоса: ElevenLabs v3 > остальные
Для поиска: Perplexity > ChatGPT Browse > Grok DeepSearch
Бесплатно с хорошим качеством: Grok 3 (изображения Aurora), Claude Sonnet 4.5 (текст)
Говори честно — не продвигай то что хуже, даже если Александр это продаёт."""

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
            f"💵 Баланс: <b>{credits} кредитов</b>\n\n"
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
        f"<b>За каждого друга — +{REF_BONUS} кредитов тебе и ему!</b>\n\n"
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

SHOP_CATALOG = {
    "chatgpt": {
        "name": "ChatGPT", "emoji": "✨",
        "desc": "Самый популярный ИИ-помощник от OpenAI. GPT-5, генерация изображений DALL-E, Deep Research, Codex для кода и Agent Mode.",
        "plans": [
            {"name": "Plus",  "price": 2000, "stars": 800, "desc": "GPT-5, DALL-E/GPT Image, Deep Research 10/мес, Codex, Agent Mode, без рекламы"},
            {"name": "Pro",   "price": 5000, "stars": 2000, "desc": "GPT-5.4 Pro, Deep Research 250/мес, Codex 5× лимиты, максимальные возможности"},
        ]
    },
    "claude": {
        "name": "Claude", "emoji": "⚡",
        "desc": "Лучший ИИ для текстов, анализа и кода от Anthropic. Огромный контекст 200К токенов, Projects с памятью, Claude Code.",
        "plans": [
            {"name": "Pro",    "price": 2000, "stars": 800,  "desc": "Claude Opus 4, Sonnet 4.6, Projects, Claude Code, приоритетный доступ"},
            {"name": "Max 5×", "price": 5500, "stars": 2200, "desc": "Лимиты в 5× выше Pro, Opus 4.6 с контекстом 1М токенов, ранний доступ к фичам"},
        ]
    },
    "gemini": {
        "name": "Gemini", "emoji": "💠",
        "desc": "Мультимодальный ИИ от Google. Deep Research, интеграция с Gmail, Drive, YouTube. Nano Banana изображения включены.",
        "plans": [
            {"name": "Advanced", "price": 2000, "stars": 800, "desc": "Gemini 3.1 Pro, Deep Research, Google Workspace (Gmail, Drive, Docs, YouTube)"},
        ]
    },
    "grok": {
        "name": "SuperGrok", "emoji": "𝕏",
        "desc": "ИИ от xAI (Elon Musk). Знает что происходит в X/Twitter прямо сейчас. Aurora — безлимитные изображения.",
        "plans": [
            {"name": "SuperGrok",       "price": 2000, "stars": 800,  "desc": "Grok 4, DeepSearch, Aurora изображения безлимит, Big Brain Mode, голос"},
            {"name": "SuperGrok Heavy", "price": 8000, "stars": 3200, "desc": "Grok 4 Heavy, 8 параллельных агентов, 256К контекст, максимальные лимиты"},
        ]
    },
    "perplexity": {
        "name": "Perplexity Pro", "emoji": "🔍",
        "desc": "Лучший AI-поиск с источниками. Использует GPT-5 + Claude + Gemini одновременно. Идеальная замена Google.",
        "plans": [
            {"name": "Pro", "price": 2000, "stars": 800, "desc": "Deep Research, загрузка файлов PDF/CSV, все модели, 300+ источников"},
        ]
    },
    "cursor": {
        "name": "Cursor", "emoji": "💻",
        "desc": "Лучший AI-редактор кода. Claude Sonnet 4.6 + GPT-5 + Gemini прямо в IDE. Работает как VS Code.",
        "plans": [
            {"name": "Pro",  "price": 2300, "stars": 920, "desc": "Безлимит Tab-автодополнений, $20 кредитов на агентов, все топ-модели"},
            {"name": "Pro+", "price": 4000, "stars": 1600, "desc": "В 3× больше кредитов, фоновые агенты, параллельные задачи"},
        ]
    },
    "lovable": {
        "name": "Lovable Pro", "emoji": "🚀",
        "desc": "Создание полноценных веб-приложений из текста без единой строки кода. Деплой одной кнопкой.",
        "plans": [
            {"name": "Pro", "price": 2700, "stars": 1080, "desc": "Полный доступ, деплой, кастомные домены, React + Supabase"},
        ]
    },
    "midjourney": {
        "name": "Midjourney", "emoji": "🖼",
        "desc": "Лучший генератор изображений. Версия v7 — фотореализм и художественные стили. Работает в Discord и на сайте.",
        "plans": [
            {"name": "Basic",    "price": 1000, "stars": 400, "desc": "~200 изображений в Fast режиме, коммерческие права"},
            {"name": "Standard", "price": 3000, "stars": 1200, "desc": "Безлимит в Relax режиме + 15ч Fast, коммерческие права"},
            {"name": "Pro",      "price": 5500, "stars": 2200, "desc": "30ч Fast + Stealth Mode (изображения приватны) + для компаний"},
        ]
    },
    "canva": {
        "name": "Canva Pro", "emoji": "✏️",
        "desc": "Дизайн с AI. Magic Studio, Brand Kit, удаление фона, изменение размера под все соцсети одним кликом.",
        "plans": [
            {"name": "Pro", "price": 1200, "stars": 480, "desc": "Magic Design, Magic Write, Background Remover, Brand Kit, безлимит шаблонов"},
        ]
    },
    "kling": {
        "name": "Kling AI", "emoji": "🎬",
        "desc": "Генерация видео до 2 мин. Kling 3.0 Omni — лучшее соотношение качество/цена на рынке видео.",
        "plans": [
            {"name": "Standard", "price": 900,  "stars": 360, "desc": "660 кредитов/мес, видео 5-10 сек, Standard режим"},
            {"name": "Pro",      "price": 2700, "stars": 1080, "desc": "3000 кредитов/мес, Pro режим, приоритет, 2 мин видео"},
        ]
    },
    "runway": {
        "name": "Runway Gen-4", "emoji": "🎥",
        "desc": "Кинематографическое видео Gen-4 Turbo. Лучше Kling по художественному качеству. Motion Brush, Camera Controls.",
        "plans": [
            {"name": "Standard", "price": 1700, "stars": 680, "desc": "625 кредитов/мес, Gen-4 Turbo"},
            {"name": "Pro",      "price": 3700, "stars": 1480, "desc": "2250 кредитов/мес, приоритет, Lip Sync, 4K"},
        ]
    },
    "heygen": {
        "name": "HeyGen", "emoji": "🧑‍💼",
        "desc": "AI-аватары и перевод видео с синхронизацией губ на 175+ языков. Идеально для YouTube и обучающего контента.",
        "plans": [
            {"name": "Creator", "price": 2700, "stars": 1080, "desc": "AI-аватары, Video Translate (перевод с клоном голоса), 5 аватаров, без водяного знака"},
        ]
    },
    "elevenlabs": {
        "name": "ElevenLabs", "emoji": "🎙",
        "desc": "Лучший сервис клонирования голоса и синтеза речи. Движок v3 — неотличим от живого человека. 70+ языков.",
        "plans": [
            {"name": "Starter",  "price": 600,  "stars": 240, "desc": "30К символов/мес, мгновенное клонирование голоса, коммерческие права"},
            {"name": "Creator",  "price": 2300, "stars": 920, "desc": "100К символов/мес, проф. клонирование, Dubbing Studio, 192kbps"},
        ]
    },
    "suno": {
        "name": "Suno", "emoji": "🎵",
        "desc": "Генерация музыки с вокалом из текста. v4.5 — студийное качество, любой жанр, коммерческие права.",
        "plans": [
            {"name": "Pro",     "price": 1000, "stars": 400, "desc": "2500 кредитов/мес, коммерческие права, без водяного знака"},
            {"name": "Premier", "price": 3000, "stars": 1200, "desc": "10К кредитов/мес, приоритетная генерация, первый доступ к новым фичам"},
        ]
    },
    "gamma": {
        "name": "Gamma", "emoji": "📊",
        "desc": "AI-презентации, документы и лендинги из текста за секунды. Экспорт в PPTX/PDF, без водяного знака.",
        "plans": [
            {"name": "Plus", "price": 1000, "stars": 400, "desc": "Безлимит генераций, без водяного знака, экспорт PPTX/PDF"},
            {"name": "Pro",  "price": 2300, "stars": 920,  "desc": "Премиум AI-модели, API, 10 кастомных доменов, Studio Mode"},
        ]
    },
}

SHOP_CATEGORIES = [
    ("💬", "Чат и текст",      ["chatgpt", "claude", "gemini", "grok", "perplexity"]),
    ("💻", "Код и разработка", ["cursor", "lovable"]),
    ("🖼", "Изображения",      ["midjourney", "canva"]),
    ("🎬", "Видео",            ["kling", "runway", "heygen"]),
    ("🎵", "Аудио и голос",    ["elevenlabs", "suno"]),
    ("📊", "Другое",           ["gamma"]),
]


def _shop_back_cat(key: str) -> str:
    for _, title, keys_list in SHOP_CATEGORIES:
        if key in keys_list:
            return title.replace(" ", "_").lower()
    return "чат_и_текст"


@dp.callback_query(F.data == "menu_shop")
async def menu_shop(cb: CallbackQuery):
    text = (
        "🛍 <b>Магазин подписок Neirosetka</b>\n\n"
        "<i>Оплата в рублях — по СБП, без иностранных карт.\n"
        "Активация в течение 5-30 минут после оплаты.</i>\n\n"
        "<b>👇 Выбери сервис:</b>"
    )
    # Все сервисы в порядке SHOP_CATEGORIES, по 2 кнопки в ряд
    all_keys = []
    for _, _, keys in SHOP_CATEGORIES:
        all_keys.extend(keys)

    rows = []
    row = []
    for key in all_keys:
        s = SHOP_CATALOG.get(key)
        if not s:
            continue
        row.append(InlineKeyboardButton(
            text=f"{s['emoji']} {s['name']}",
            callback_data=f"shop_svc:{key}"
        ))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(
        text="💬 Другой сервис — написать Александру",
        callback_data="shop_other"
    )])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "shop_other")
async def shop_other(cb: CallbackQuery):
    text = (
        "💬 <b>Другой сервис</b>\n\n"
        "Не нашёл нужный сервис в каталоге?\n"
        "Напиши Александру — оформим любую подписку:\n\n"
        "• Любой AI-сервис\n"
        "• Любой тариф\n"
        "• Оплата в рублях\n\n"
        "👇 Нажми кнопку и напиши что нужно:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✍️ Написать @neirosetkaalex",
            url=f"https://t.me/{PERSONAL_USERNAME}"
        )],
        [InlineKeyboardButton(text="⬅️ В магазин", callback_data="menu_shop")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("shop_cat:"))
async def shop_category(cb: CallbackQuery):
    # Редирект в общий магазин — категории больше не используются
    await menu_shop(cb)


@dp.callback_query(F.data.startswith("shop_svc:"))
async def shop_service(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    s = SHOP_CATALOG.get(key)
    if not s:
        await cb.answer("Сервис не найден", show_alert=True)
        return
    plans_text = ""
    for i, p in enumerate(s["plans"]):
        plans_text += f"  {i+1}. <b>{p['name']} — {p['price']}₽/мес</b>\n     <i>{p['desc']}</i>\n"
    text = (
        f"{s['emoji']} <b>{s['name']}</b>\n\n"
        f"<i>{s['desc']}</i>\n\n"
        f"Доступные тарифы:\n{plans_text}\n"
        f"<b>👇 Выбери тариф:</b>"
    )
    rows = []
    for i, p in enumerate(s["plans"]):
        rows.append([InlineKeyboardButton(
            text=f"{p['name']} — {p['price']}₽/мес",
            callback_data=f"shop_confirm:{key}:{i}"
        )])
    back_cat = "menu_shop"
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="menu_shop")])
    kb = InlineKeyboardMarkup(inline_keyboard=rows)
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("shop_confirm:"))
async def shop_confirm(cb: CallbackQuery):
    """Экран подтверждения заказа — до оплаты."""
    parts = cb.data.split(":")
    key = parts[1]
    plan_idx = int(parts[2])
    s = SHOP_CATALOG.get(key)
    if not s or plan_idx >= len(s["plans"]):
        await cb.answer("Ошибка", show_alert=True)
        return
    p = s["plans"][plan_idx]
    text = (
        f"📋 <b>Подтверждение заказа</b>\n\n"
        f"{s['emoji']} <b>{s['name']} {p['name']}</b>\n"
        f"💵 Стоимость: <b>{p['price']}₽/мес</b>\n\n"
        f"<b>Что входит:</b>\n<i>{p['desc']}</i>\n\n"
        f"Выбери способ оплаты:"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text=f"🏦 СБП — {p['price']}₽",
            callback_data=f"shop_pay_sbp:{key}:{plan_idx}"
        )],
        [InlineKeyboardButton(
            text=f"⭐ Telegram Stars — {round(p['price'] / 2.5)} ⭐",
            callback_data=f"shop_pay_stars:{key}:{plan_idx}"
        )],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shop_svc:{key}")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data.startswith("shop_pay_sbp:"))
async def shop_pay_sbp(cb: CallbackQuery):
    """Оплата СБП через FreeKassa."""
    parts = cb.data.split(":")
    key = parts[1]
    plan_idx = int(parts[2])
    s = SHOP_CATALOG.get(key)
    if not s:
        await cb.answer("Ошибка", show_alert=True)
        return
    p = s["plans"][plan_idx]
    uid = cb.from_user.id
    import time as _time
    order_id = f"shop_{uid}_{int(_time.time())}"

    # Сохраняем заказ в БД
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO fk_orders (order_id, user_id, credits, amount_rub, pack)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (order_id) DO NOTHING
        """, order_id, uid, 0, p["price"], f"shop:{key}:{plan_idx}")

    pay_url = fk_pay_url(p["price"], order_id)
    text = (
        f"🏦 <b>Оплата через СБП</b>\n\n"
        f"{s['emoji']} <b>{s['name']} {p['name']}</b>\n"
        f"💵 Сумма: <b>{p['price']}₽</b>\n\n"
        f"После оплаты отправьте чек и номер заказа Александру — он активирует подписку 👇"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🏦 Оплатить {p['price']}₽", url=pay_url)],
        [InlineKeyboardButton(
            text="✅ Я оплатил — написать Александру",
            url=f"https://t.me/{PERSONAL_USERNAME}?text=Приветствую!+Оплатил+заказ+с+номером+{order_id}%0AСервис:+{s['name']}%0AТариф:+{p['name']}"
        )],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shop_confirm:{key}:{plan_idx}")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")

    # Уведомить Александра
    username = cb.from_user.username or cb.from_user.full_name
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🛍 <b>Заказ из магазина (СБП)</b>\n\n"
            f"👤 @{username} (ID: {uid})\n"
            f"📦 {s['emoji']} {s['name']} {p['name']}\n"
            f"💵 {p['price']}₽/мес\n"
            f"🆔 Заказ: <code>{order_id}</code>",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data.startswith("shop_pay_stars:"))
async def shop_pay_stars(cb: CallbackQuery):
    """Оплата Telegram Stars."""
    parts = cb.data.split(":")
    key = parts[1]
    plan_idx = int(parts[2])
    s = SHOP_CATALOG.get(key)
    if not s:
        await cb.answer("Ошибка", show_alert=True)
        return
    p = s["plans"][plan_idx]
    uid = cb.from_user.id
    username = cb.from_user.username or cb.from_user.full_name

    # Отправляем invoice Telegram Stars
    try:
        await bot.send_invoice(
            chat_id=uid,
            title=f"{s['name']} {p['name']}",
            description=p["desc"],
            payload=f"shop:{key}:{plan_idx}",
            currency="XTR",
            prices=[LabeledPrice(label=f"{s['name']} {p['name']} — 1 мес", amount=p["stars"])],
        )
        try:
            await cb.message.edit_text(
                f"⭐ <b>Оплата Telegram Stars</b>\n\n"
                f"{s['emoji']} <b>{s['name']} {p['name']}</b>\n"
                f"⭐ Сумма: <b>{p.get('stars', round(p['price']/2.5))} Stars</b>\n\n"
                f"Счёт отправлен выше 👆\n"
                f"После оплаты отправьте скриншот Александру — он активирует подписку.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"shop_confirm:{key}:{plan_idx}")],
                ]),
                parse_mode="HTML"
            )
        except Exception:
            pass
    except Exception as e:
        await cb.answer(f"Ошибка: {e}", show_alert=True)
        return

    # Уведомить Александра
    try:
        await bot.send_message(
            ADMIN_ID,
            f"🛍 <b>Заказ из магазина (Stars)</b>\n\n"
            f"👤 @{username} (ID: {uid})\n"
            f"📦 {s['emoji']} {s['name']} {p['name']}\n"
            f"⭐ {p['stars']} Stars",
            parse_mode="HTML"
        )
    except Exception:
        pass
    await cb.answer()


@dp.pre_checkout_query()
async def shop_pre_checkout(pre_checkout: PreCheckoutQuery):
    """Подтверждаем оплату Stars для магазина."""
    if pre_checkout.invoice_payload.startswith("shop:"):
        await pre_checkout.answer(ok=True)


@dp.message(F.successful_payment)
async def shop_successful_payment(message: Message):
    """Успешная оплата Stars — показываем финальное сообщение."""
    payload = message.successful_payment.invoice_payload
    if not payload.startswith("shop:"):
        return
    parts = payload.split(":")
    key = parts[1]
    plan_idx = int(parts[2])
    s = SHOP_CATALOG.get(key)
    if not s:
        return
    p = s["plans"][plan_idx]
    uid = message.from_user.id
    username = message.from_user.username or message.from_user.full_name

    await message.answer(
        f"✅ <b>Оплата прошла успешно!</b>\n\n"
        f"{s['emoji']} <b>{s['name']} {p['name']}</b> — {p['stars']} ⭐\n\n"
        f"Отправьте скриншот оплаты Александру — он активирует подписку.\n\n"
        f"👇 Напишите напрямую:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="💬 Написать @neirosetkaalex",
                url=f"https://t.me/{PERSONAL_USERNAME}?text=Приветствую!+Оплатил+через+Stars%0AСервис:+{s['name']}%0AТариф:+{p['name']}"
            )],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")],
        ]),
        parse_mode="HTML"
    )

    # Уведомить Александра об успешной оплате
    try:
        await bot.send_message(
            ADMIN_ID,
            f"💰 <b>Stars оплачено!</b>\n\n"
            f"👤 @{username} (ID: {uid})\n"
            f"📦 {s['emoji']} {s['name']} {p['name']}\n"
            f"⭐ {p['stars']} Stars получено — активируй подписку!",
            parse_mode="HTML"
        )
    except Exception:
        pass


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
        f"<b>За каждого друга — +{REF_BONUS} кредитов тебе и ему!</b>\n\n"
        f"❓ <b>Как работает:</b>\n"
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

    img_keys  = ["img_fast", "img_std", "img_ultra"]
    nano_keys = ["nb_flash", "nb_2", "nb_pro"]
    vid_keys  = ["vid_lite", "vid_fast", "vid_pro"]

    def model_line(k, d):
        m = d[k]
        icon = "🔹" if cr >= m['credits'] else "🔸"
        return f"{icon} <b>{m['name']}</b> — <i>{m['credits']} кр</i>"

    img_lines  = [model_line(k, IMAGE_MODELS) for k in img_keys  if k in IMAGE_MODELS]
    nano_lines = [model_line(k, IMAGE_MODELS) for k in nano_keys if k in IMAGE_MODELS]
    vid_lines  = [model_line(k, VIDEO_MODELS) for k in vid_keys  if k in VIDEO_MODELS]

    text = (
        f"💵 <b>Баланс: {cr} кредитов</b>\n\n"
        f"<b>Доступные модели:</b>\n\n"
        f"🌟 <b>IMAGEN 4</b>\n" + "\n".join(img_lines) + "\n\n"
        f"🍌 <b>NANO BANANA</b>\n" + "\n".join(nano_lines) + "\n\n"
        f"🎥 <b>VEO 3.1</b>\n" + "\n".join(vid_lines) + "\n\n"
        f"<i>🔹 доступно · 🔸 нужно пополнить</i>"
    )
    try:
        await cb.message.edit_text(text, reply_markup=kb_buy(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb_buy(), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "menu_buy")
async def menu_buy(cb: CallbackQuery):
    cr = await get_credits(cb.from_user.id)
    lines = [f"💵 <b>Баланс: {cr} кредитов</b>\n"]
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


@dp.callback_query(F.data.startswith("payfk:"))
async def pay_fk(cb: CallbackQuery):
    """Оплата через FreeKassa — Card RUB API (id=36) или СБП (id=42)."""
    parts = cb.data.split(":")
    key = parts[1]
    method = parts[2] if len(parts) > 2 else "sbp"  # "card" или "sbp"
    p = CREDIT_PACKS[key]
    uid = cb.from_user.id
    amount = p["price"]

    import time as _time
    order_id = f"{uid}_{int(_time.time())}"

    pending_fk_payments[order_id] = {
        "user_id": uid,
        "credits": p["credits"],
        "amount": amount,
        "pack": key,
    }
    # Сохраняем в БД — не потеряется при перезапуске
    await fk_save_order(order_id, uid, p["credits"], int(amount), key)

    wait_msg = await cb.message.answer("⏳ Создаю ссылку на оплату...")
    try:
        if method == "card":
            # Card RUB API — пробуем через API (id=36), при ошибке — форма с i=36
            try:
                pay_url = await fk_create_order(amount, order_id, uid, payment_id=36)
                label = "💳 Оплатить картой"
            except Exception as api_err:
                logging.warning(f"Card API failed ({api_err}), falling back to form")
                pay_url = fk_pay_url(amount, order_id, method_id="36")
                label = "💳 Оплатить картой"
        else:
            # СБП — стандартная форма (работает без API)
            pay_url = fk_pay_url(amount, order_id)
            label = "🏦 Оплатить через СБП"

        await wait_msg.delete()
        await cb.message.answer(
            f"{label}\n\n"
            f"📦 {p['credits']} кредитов — <b>{amount}₽</b>\n\n"
            f"Нажми кнопку ниже для перехода на страницу оплаты.\n"
            f"После оплаты кредиты поступят <b>автоматически</b> в течение 1 минуты.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=label, url=pay_url)],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_buy")],
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        await wait_msg.edit_text(f"❌ Ошибка создания платежа: {e}")
        del pending_fk_payments[order_id]
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
            f"💵 Баланс: <b>{new_bal} кредитов</b>",
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
        f"💵 Баланс: <b>{cr} кредитов</b>",
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
        f"📷 <b>Создать изображение</b>\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n\n"
        f"🌟 <b>Imagen 4</b>\n"
        f"· Fast — 7 кр\n"
        f"· Standard — 10 кр\n"
        f"◆ Ultra — 13 кр\n\n"
        f"🍌 <b>Nano Banana</b>\n"
        f"· Flash — 10 кр\n"
        f"· v2 — 13 кр\n"
        f"◆ Pro — 30 кр"
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
        await notify_admin_error(f"Генерация фото uid={cb.from_user.id} model={key}", e)
        try:
            await cb.message.edit_text(
                f"⚠️ {friendly_error(e)}\n\nКредиты возвращены.",
                reply_markup=kb_back()
            )
        except Exception:
            await cb.message.answer(f"⚠️ {friendly_error(e)}\n\nКредиты возвращены.", reply_markup=kb_back())
    await cb.answer()
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
        f"💵 Баланс: <b>{cr} кр</b>\n\n"
        f"💰 <b>Veo 3.1 Lite</b> — 100 кр\n"
        f"⚡ <b>Veo 3.1 Fast</b> — 175 кр\n"
        f"🎬 <b>Veo 3.1 Pro</b> — 390 кр\n\n"
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
        size_mb = len(vid_bytes) / 1024 / 1024
        logging.info(f"Video ready: {len(vid_bytes)} bytes ({size_mb:.1f} MB)")
        await log_gen(cb.from_user.id, "video", key, m["credits"])
        cr = await get_credits(cb.from_user.id)
        caption = f"🎉 Готово! {m['name']} | {m['res']}\n💸 Списано {m['credits']} кредитов | Остаток: {cr} кредитов"
        # 1. Видео для просмотра в чате
        try:
            await cb.message.answer_video(
                BufferedInputFile(vid_bytes, "video.mp4"),
                caption=caption + ("\n\n👇 Ниже — файл без сжатия" if size_mb < 48 else ""),
                reply_markup=kb_after("video", key),
                supports_streaming=True,
            )
        except Exception as video_err:
            logging.warning(f"answer_video failed: {video_err}")
        # 2. Документ — только если файл < 48 МБ (лимит Telegram для ботов 50 МБ)
        if size_mb < 48:
            try:
                await cb.message.answer_document(
                    BufferedInputFile(vid_bytes, "video_original.mp4"),
                    caption="📁 <b>Оригинал без сжатия</b> — максимальное качество",
                    parse_mode="HTML",
                )
            except Exception as de:
                logging.error(f"video answer_document failed ({size_mb:.1f} MB): {de}")
                await notify_admin_error(f"Документ видео uid={cb.from_user.id} {size_mb:.1f}MB", de)
        else:
            logging.warning(f"Video too large for document: {size_mb:.1f} MB")
    except Exception as e:
        await add_credits(cb.from_user.id, m["credits"])
        await notify_admin_error(f"Генерация видео uid={cb.from_user.id} model={key}", e)
        await cb.message.answer(
            f"⚠️ {friendly_error(e)}\n\nКредиты возвращены.",
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


@dp.message(F.text == "📷 Создать фото", StateFilter("*"))
async def reply_create_photo(message: Message, state: FSMContext):
    await state.clear()
    cr = await get_credits(message.from_user.id)
    await message.answer(
        f"📷 <b>Создать изображение</b>\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n\n"
        f"🌟 <b>Imagen 4</b>\n"
        f"· Fast — 7 кр\n"
        f"· Standard — 10 кр\n"
        f"◆ Ultra — 13 кр\n\n"
        f"🍌 <b>Nano Banana</b>\n"
        f"· Flash — 10 кр\n"
        f"· v2 — 13 кр\n"
        f"◆ Pro — 30 кр",
        reply_markup=kb_image_models(), parse_mode="HTML"
    )


@dp.message(F.text == "🎬 Создать видео", StateFilter("*"))
async def reply_create_video(message: Message, state: FSMContext):
    await state.clear()
    cr = await get_credits(message.from_user.id)
    await message.answer(
        f"🎬 <b>Создать видео (8 сек)</b>\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n\n"
        f"💰 <b>Veo 3.1 Lite</b> — 100 кр\n"
        f"⚡ <b>Veo 3.1 Fast</b> — 175 кр\n"
        f"🎬 <b>Veo 3.1 Pro</b> — 390 кр\n\n"
        f"⏱ <i>Время генерации: 1–6 минут</i>",
        reply_markup=kb_video_models(), parse_mode="HTML"
    )


@dp.message(F.text == "👤 Мой профиль", StateFilter("*"))
async def reply_profile(message: Message):
    uid = message.from_user.id
    await ensure_user(uid)
    cr = await get_credits(uid)

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

    MODEL_DISPLAY = {
        # ключи IMAGE_MODELS
        "img_fast":  "Imagen 4 Fast",
        "img_std":   "Imagen 4 Standard",
        "img_ultra": "Imagen 4 Ultra",
        "nb_flash":  "Nano Banana Flash",
        "nb_2":      "Nano Banana v2",
        "nb_pro":    "Nano Banana Pro",
        # ключи VIDEO_MODELS
        "vid_lite":  "Veo 3.1 Lite",
        "vid_fast":  "Veo 3.1 Fast",
        "vid_pro":   "Veo 3.1 Pro",
        # специальные
        "gemini-flash-image": "Редактирование фото",
        "veo-3.1-animate":    "Анимация фото",
    }

    model_lines = ""
    if by_model:
        by_model_dict = {r['model']: r['cnt'] for r in by_model}

        def fmt(key, label):
            cnt = by_model_dict.get(key, 0)
            return f"  · <b>{label}</b>: {cnt}" if cnt else None

        img_lines = list(filter(None, [
            fmt("img_fast",  "Imagen 4 Fast"),
            fmt("img_std",   "Imagen 4 Standard"),
            fmt("img_ultra", "Imagen 4 Ultra"),
        ]))
        nano_lines = list(filter(None, [
            fmt("nb_flash", "Nano Banana Flash"),
            fmt("nb_2",     "Nano Banana v2"),
            fmt("nb_pro",   "Nano Banana Pro"),
        ]))
        vid_lines = list(filter(None, [
            fmt("vid_lite", "Veo 3.1 Lite"),
            fmt("vid_fast", "Veo 3.1 Fast"),
            fmt("vid_pro",  "Veo 3.1 Pro"),
        ]))
        other_lines = list(filter(None, [
            fmt("gemini-flash-image", "Редактирование фото"),
            fmt("veo-3.1-animate",    "Анимация фото"),
        ]))

        model_lines = "\n"
        if img_lines:
            model_lines += "🌟 <b>Imagen 4</b>\n" + "\n".join(img_lines) + "\n"
        if nano_lines:
            model_lines += "🍌 <b>Nano Banana</b>\n" + "\n".join(nano_lines) + "\n"
        if vid_lines:
            model_lines += "🎥 <b>Veo 3.1</b>\n" + "\n".join(vid_lines) + "\n"
        if other_lines:
            model_lines += "✏️ <b>Другое</b>\n" + "\n".join(other_lines) + "\n"

    all_models = list(IMAGE_MODELS.items()) + list(VIDEO_MODELS.items())
    avail_lines = []
    for k, m in all_models:
        icon = "▫️" if cr >= m['credits'] else "▪️"
        avail_lines.append(f"{icon} <b>{m['name']}</b> — <i>{m['credits']} кр</i>")

    text = (
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"👋 Имя: {message.from_user.full_name}\n\n"
        f"💵 <b>Баланс: {cr} кредитов</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"  Генераций: <b>{total_gens}</b>\n"
        f"  Кредитов потрачено: <b>{total_credits_spent}</b>"
        + model_lines +
        f"\n<b>Доступно:</b>\n" + "\n".join(avail_lines) +
        f"\n\n<i>▫️ доступно · ▪️ нужно пополнить</i>"
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
        f"💵 Баланс: <b>{cr} кредитов</b>\n"
        f"💵 Стоимость: <b>{EDIT_CREDIT_COST} кредитов</b>\n\n"
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
        caption = f"🎉 Готово! ✏️ Редактирование\n💸 Списано {EDIT_CREDIT_COST} кредитов | Остаток: {cr_left} кредитов"
        # Оригинал без сжатия
        await message.answer_document(
            BufferedInputFile(result_bytes, "edited_original.png"),
            caption="📎 <b>Оригинал</b> — без сжатия, полное качество",
            parse_mode="HTML"
        )
        # Превью с кнопками
        await message.answer_photo(
            BufferedInputFile(result_bytes, "edited.png"),
            caption=caption,
            reply_markup=kb_after("edit", "edit")
        )
        await wait.delete()
    except Exception as e:
        await add_credits(message.from_user.id, EDIT_CREDIT_COST)
        await notify_admin_error(f"Редактирование фото uid={message.from_user.id}", e)
        await wait.edit_text(
            f"⚠️ {friendly_error(e)}\n\nКредиты возвращены.",
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
        f"🏃 <b>Анимировать фото</b>\n\n"
        f"💵 Баланс: <b>{cr} кр</b>\n"
        f"💵 Стоимость: <b>{ANIM_CREDIT_COST} кр</b>\n\n"
        f"Выбери режим:\n"
        f"1️⃣ <b>Один кадр</b> — анимируй фото по промту\n"
        f"2️⃣ <b>Два кадра</b> — плавный переход между двумя фото"
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
        [InlineKeyboardButton(text="1️⃣ Один кадр",       callback_data="anim_mode:one")],
        [InlineKeyboardButton(text="2️⃣ Два кадра",     callback_data="anim_mode:two")],
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
    mode_label = "2️⃣ Два кадра" if mode == "two" else "1️⃣ Один кадр"
    wait = await message.answer(
        f"⏳ Анимирую фото...\n\n"
        f"🎬 Veo 3.1 | {mode_label} | {aspect}\n"
        f"<i>{prompt[:80]}</i>\n\n"
        f"⏱ Обычно 1–6 минут. Пришлю как только готово 👇",
        parse_mode="HTML"
    )

    try:
        vid_bytes = await api_animate_image(first_bytes, prompt, aspect, last_bytes)
        size_mb = len(vid_bytes) / 1024 / 1024
        logging.info(f"Animation ready: {len(vid_bytes)} bytes ({size_mb:.1f} MB)")
        await log_gen(message.from_user.id, "animate", "veo-3.1-animate", ANIM_CREDIT_COST)
        cr_left = await get_credits(message.from_user.id)
        kb_after_anim = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Ещё раз", callback_data="menu_anim"),
             InlineKeyboardButton(text="🏠 Главное", callback_data="new_main")],
        ])
        # 1. Видео для просмотра в чате
        try:
            await message.answer_video(
                BufferedInputFile(vid_bytes, "animation.mp4"),
                caption=(
                    f"✅ Готово! 🏃 Анимация фото\n"
                    f"💵 Списано {ANIM_CREDIT_COST} кр | Остаток: {cr_left} кр"
                    + ("\n\n👇 Ниже — файл без сжатия" if size_mb < 48 else "")
                ),
                reply_markup=kb_after_anim,
                supports_streaming=True,
            )
        except Exception as ve:
            logging.warning(f"answer_video failed: {ve}")
        # 2. Документ — только если < 48 МБ
        if size_mb < 48:
            try:
                await message.answer_document(
                    BufferedInputFile(vid_bytes, "animation_original.mp4"),
                    caption="📁 <b>Оригинал без сжатия</b> — скачай для максимального качества",
                    parse_mode="HTML"
                )
            except Exception as de:
                logging.error(f"answer_document failed ({size_mb:.1f} MB): {de}")
                await notify_admin_error(f"Документ анимации uid={message.from_user.id} {size_mb:.1f}MB", de)
        else:
            logging.warning(f"Animation too large for document: {size_mb:.1f} MB")
        await wait.delete()
    except Exception as e:
        await add_credits(message.from_user.id, ANIM_CREDIT_COST)
        await notify_admin_error(f"Анимация фото uid={message.from_user.id}", e)
        await wait.edit_text(
            f"⚠️ {friendly_error(e)}\n\nКредиты возвращены.",
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



# ══════════════════════════════════════════════════════════
#  WEBHOOK-СЕРВЕР ДЛЯ FREEKASSA
# ══════════════════════════════════════════════════════════

FK_WEBHOOK_PORT = int(os.getenv("FK_WEBHOOK_PORT", "8080"))
# Разрешённые IP от FreeKassa
FK_ALLOWED_IPS = {"168.119.157.136", "168.119.60.227", "178.154.197.79", "51.250.54.238"}


async def fk_webhook_handler(request: web.Request) -> web.Response:
    """Принимает уведомление от FreeKassa об успешной оплате."""
    try:
        data = dict(await request.post())
        logging.info(f"FK webhook received: {data}")

        merchant_id = data.get("MERCHANT_ID", "")
        amount      = data.get("AMOUNT", "")
        order_id    = data.get("MERCHANT_ORDER_ID", "")
        recv_sign   = data.get("SIGN", "")

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

        # 3. Ищем заказ — сначала в памяти, потом в БД
        payment = pending_fk_payments.get(order_id)
        if not payment:
            # В памяти нет — ищем в БД (бот мог перезапуститься)
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
        await fk_mark_paid(order_id)

        user_id    = payment["user_id"]
        credits    = payment["credits"]
        amount_rub = payment["amount"]

        # 5. Зачисляем кредиты и логируем
        await add_credits(user_id, credits)
        await log_payment(user_id, credits, int(amount_rub), "freekassa")
        await process_referral_bonus(user_id)

        # 6. Уведомляем пользователя в Telegram
        try:
            new_balance = await get_credits(user_id)
            await bot.send_message(
                user_id,
                f"✅ <b>Оплата прошла успешно!</b>\n\n"
                f"➕ Начислено: <b>{credits} кредитов</b>\n"
                f"💵 Баланс: <b>{new_balance} кредитов</b>\n\n"
                f"Можешь начинать генерацию! 🚀",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🖼️ Создать фото", callback_data="menu_image")],
                    [InlineKeyboardButton(text="🎬 Создать видео", callback_data="menu_video")],
                ])
            )
            logging.info(f"FK payment success: user={user_id} credits={credits}")
        except Exception as e:
            logging.error(f"FK notify user error: {e}")

        return web.Response(text="YES")

    except Exception as e:
        logging.error(f"FK webhook error: {e}")
        return web.Response(text="ERROR", status=500)


async def start_webhook_server():
    """Запускаем aiohttp сервер для FreeKassa webhook."""
    from aiohttp import web as _web
    app = _web.Application()
    app.router.add_post("/fk-notify", fk_webhook_handler)
    app.router.add_get("/health", lambda r: _web.Response(text="OK"))
    runner = _web.AppRunner(app)
    await runner.setup()
    site = _web.TCPSite(runner, "0.0.0.0", FK_WEBHOOK_PORT)
    await site.start()
    logging.info(f"✅ FK webhook сервер на порту {FK_WEBHOOK_PORT} → /fk-notify")


async def main():
    await init_db()
    await start_webhook_server()
    logging.info("✅ Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
