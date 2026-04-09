import asyncio
import logging
import aiosqlite
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
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import anthropic
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

FREE_CREDITS   = 5   # кредитов при первом /start
DB_PATH        = "bot.db"

logging.basicConfig(level=logging.INFO)

bot           = Bot(token=BOT_TOKEN)
dp            = Dispatcher(storage=MemoryStorage())
claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

user_conversations = {}   # история чата с консультантом

# ─── Модели изображений ───────────────────────────────────
IMAGE_MODELS = {
    "img_fast": {
        "name": "⚡ Imagen 4 Fast",
        "model_id": "imagen-4.0-fast-generate-001",
        "credits": 1,
        "price": "5₽",
        "speed": "~2 сек",
        "desc": "Быстро и качественно",
    },
    "img_std": {
        "name": "✨ Imagen 4",
        "model_id": "imagen-4.0-generate-001",
        "credits": 2,
        "price": "10₽",
        "speed": "~5 сек",
        "desc": "Флагман, чёткий текст",
    },
    "img_ultra": {
        "name": "💎 Imagen 4 Ultra",
        "model_id": "imagen-4.0-ultra-generate-001",
        "credits": 3,
        "price": "15₽",
        "speed": "~8 сек",
        "desc": "Максимальная точность",
    },
}

# ─── Модели видео ─────────────────────────────────────────
VIDEO_MODELS = {
    "vid_lite": {
        "name": "💰 Veo 3.1 Lite",
        "model_id": "veo-3.1-lite-generate-preview",
        "credits": 15,
        "price": "75₽",
        "res": "720p",
        "desc": "Бюджет, быстро",
    },
    "vid_fast": {
        "name": "⚡ Veo 3.1 Fast",
        "model_id": "veo-3.1-fast-generate-preview",
        "credits": 25,
        "price": "125₽",
        "res": "1080p",
        "desc": "Баланс цены и качества",
    },
    "vid_pro": {
        "name": "🎬 Veo 3.1",
        "model_id": "veo-3.1-generate-preview",
        "credits": 65,
        "price": "325₽",
        "res": "4K + аудио",
        "desc": "Кино-качество",
    },
}

# ─── Пакеты кредитов ──────────────────────────────────────
CREDIT_PACKS = {
    "p50":  {"name": "🥉 Старт",    "credits": 50,  "price": 199,  "stars": 40},
    "p150": {"name": "🥈 Стандарт", "credits": 150, "price": 499,  "stars": 100},
    "p500": {"name": "🥇 Про",      "credits": 500, "price": 1490, "stars": 300},
}

# ══════════════════════════════════════════════════════════
#  БАЗА ДАННЫХ
# ══════════════════════════════════════════════════════════

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                credits    INTEGER DEFAULT 0,
                is_blocked INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # Добавляем колонку если её нет (миграция)
        try:
            await db.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0")
            await db.commit()
        except Exception:
            pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS generations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER,
                type       TEXT,
                model      TEXT,
                credits    INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        await db.commit()

async def ensure_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, credits) VALUES (?, ?)",
            (user_id, FREE_CREDITS)
        )
        await db.commit()

async def get_credits(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT credits FROM users WHERE user_id=?", (user_id,)) as c:
            row = await c.fetchone()
            return row[0] if row else 0

async def deduct(user_id: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT credits FROM users WHERE user_id=?", (user_id,)) as c:
            row = await c.fetchone()
            if not row or row[0] < amount:
                return False
        await db.execute(
            "UPDATE users SET credits = credits - ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()
    return True

async def add_credits(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET credits = credits + ? WHERE user_id = ?",
            (amount, user_id)
        )
        await db.commit()

async def block_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_blocked=1 WHERE user_id=?", (user_id,))
        await db.commit()

async def unblock_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET is_blocked=0 WHERE user_id=?", (user_id,))
        await db.commit()

async def is_blocked(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_blocked FROM users WHERE user_id=?", (user_id,)) as c:
            row = await c.fetchone()
            return bool(row and row[0])

async def log_gen(user_id: int, gen_type: str, model: str, credits: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO generations (user_id, type, model, credits) VALUES (?,?,?,?)",
            (user_id, gen_type, model, credits)
        )
        await db.commit()

# ══════════════════════════════════════════════════════════
#  КЛАВИАТУРЫ
# ══════════════════════════════════════════════════════════

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🖼️ Изображение", callback_data="menu_image"),
            InlineKeyboardButton(text="🎬 Видео",        callback_data="menu_video"),
        ],
        [
            InlineKeyboardButton(text="💬 Консультант AI", callback_data="menu_chat"),
        ],
        [
            InlineKeyboardButton(text="💳 Баланс",         callback_data="menu_balance"),
            InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="menu_buy"),
        ],
        [
            InlineKeyboardButton(text="✍️ Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}"),
        ],
    ])

def kb_image_models():
    rows = []
    for key, m in IMAGE_MODELS.items():
        rows.append([InlineKeyboardButton(
            text=f"{m['name']} — {m['credits']} кр",
            callback_data=f"imodel:{key}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_video_models():
    rows = []
    for key, m in VIDEO_MODELS.items():
        rows.append([InlineKeyboardButton(
            text=f"{m['name']} — {m['credits']} кр",
            callback_data=f"vmodel:{key}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_confirm(prefix: str, key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Генерировать", callback_data=f"go:{prefix}:{key}"),
            InlineKeyboardButton(text="✏️ Изменить",    callback_data=f"chprompt:{prefix}:{key}"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_main")],
    ])

def kb_buy():
    rows = []
    for key, p in CREDIT_PACKS.items():
        rows.append([InlineKeyboardButton(
            text=f"{p['name']} — {p['credits']} кр за {p['price']}₽",
            callback_data=f"buy:{key}"
        )])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_pay_method(pack_key: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Telegram Stars", callback_data=f"paystars:{pack_key}")],
        [InlineKeyboardButton(text="◀️ Назад",          callback_data="menu_buy")],
    ])

def kb_after(menu: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔄 Ещё раз",    callback_data=f"menu_{menu}"),
            InlineKeyboardButton(text="🏠 Главное",    callback_data="back_main"),
        ],
        [InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="menu_buy")],
    ])

def kb_cancel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="back_main")]
    ])

def kb_back():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_main")]
    ])

def kb_contact():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}")]
    ])


def kb_reply(is_admin: bool = False) -> ReplyKeyboardMarkup:
    """Постоянная нижняя панель кнопок."""
    rows = [
        [KeyboardButton(text="🎨 Создать фото"), KeyboardButton(text="🎬 Создать видео")],
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🏠 Главное меню")],
    ]
    if is_admin:
        rows.append([KeyboardButton(text="⚙️ Админ панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, persistent=True)

# ══════════════════════════════════════════════════════════
#  FSM СОСТОЯНИЯ
# ══════════════════════════════════════════════════════════

class ImgState(StatesGroup):
    waiting_prompt = State()

class VidState(StatesGroup):
    waiting_prompt = State()

class ChatState(StatesGroup):
    chatting = State()

class AdminState(StatesGroup):
    waiting_user_id = State()
    waiting_credits = State()
    waiting_block_id = State()

# ══════════════════════════════════════════════════════════
#  СИСТЕМНЫЙ ПРОМТ + ВЕБ-ПОИСК
# ══════════════════════════════════════════════════════════

SYSTEM_PROMPT = """Ты — AI-ассистент Telegram бота Александра (@AleksandrOii).

ГЛАВНОЕ — ТЫ РАБОТАЕШЬ ВНУТРИ БОТА КОТОРЫЙ УМЕЕТ:
- Генерировать изображения (Imagen 4) — кнопка "🖼️ Изображение" в меню
- Создавать видео (Veo 3.1) — кнопка "🎬 Видео" в меню
- Оформлять подписки на любые нейросети без VPN и иностранных карт

Если спрашивают "можешь создать изображение/видео?" — отвечай:
"Да! Нажми кнопку 🖼️ Изображение в главном меню — и создашь прямо здесь. Напиши /start если не видишь меню."

НИКОГДА не говори что не умеешь создавать изображения или видео.

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
КАК ОФОРМИТЬ ПОДПИСКУ
━━━━━━━━━━━━━━━━━━━━━━
Клиент пишет Александру в личку (@AleksandrOii), называет нужный сервис и тариф.
Александр сам всё оформляет — не нужна иностранная карта или VPN.
Оплата в рублях / тенге, быстро и без лишних сложностей.

━━━━━━━━━━━━━━━━━━━━━━
ПРАВИЛА ОТВЕТОВ
━━━━━━━━━━━━━━━━━━━━━━
1. ВСЕГДА используй web_search перед ответом о тарифах или моделях — информация обновляется часто
2. Если клиент спрашивает про конкретный сервис — расскажи что он даёт, актуальные тарифы, отличия
3. Если клиент не знает что выбрать — задай уточняющий вопрос: для чего нужна нейросеть?
4. Для оформления подписки всегда направляй к Александру: @AleksandrOii
5. Никогда не называй устаревшие модели (GPT-4o, GPT-4 Turbo) как текущие — сейчас актуальны GPT-5.x
6. Если не знаешь — честно скажи и предложи спросить Александра напрямую
"""

# Инструмент веб-поиска для Claude API
WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
}

# ══════════════════════════════════════════════════════════
#  GOOGLE AI СЕРВИСЫ
# ══════════════════════════════════════════════════════════

async def api_generate_image(prompt: str, model_id: str) -> bytes:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_id}:predict"
    payload = {
        "instances": [{"prompt": prompt}],
        "parameters": {
            "sampleCount": 1,
            "aspectRatio": "1:1",
            "safetyFilterLevel": "block_few",
        }
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, headers=headers) as r:
            if r.status != 200:
                raise Exception(f"Imagen API {r.status}: {(await r.text())[:200]}")
            data = await r.json()
            return base64.b64decode(data["predictions"][0]["bytesBase64Encoded"])


async def api_generate_video(prompt: str, model_id: str) -> bytes:
    base = "https://generativelanguage.googleapis.com/v1beta"
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    payload = {
        "instances": [{"prompt": prompt}],
        "parameters": {"durationSeconds": 8, "aspectRatio": "16:9", "sampleCount": 1}
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{base}/models/{model_id}:predictLongRunning",
                          json=payload, headers=headers) as r:
            if r.status != 200:
                raise Exception(f"Veo API {r.status}: {(await r.text())[:200]}")
            op_name = (await r.json()).get("name")

        # polling
        for _ in range(72):
            await asyncio.sleep(5)
            async with s.get(f"{base}/{op_name}", headers=headers) as pr:
                if pr.status != 200:
                    continue
                pd = await pr.json()
                if not pd.get("done"):
                    continue
                if "error" in pd:
                    raise Exception(pd["error"].get("message", "Veo error"))
                preds = pd.get("response", {}).get("predictions", [])
                if not preds:
                    raise Exception("Пустой ответ Veo API")
                if preds[0].get("bytesBase64Encoded"):
                    return base64.b64decode(preds[0]["bytesBase64Encoded"])
                uri = preds[0].get("videoUri") or preds[0].get("gcsUri")
                if uri:
                    async with s.get(uri) as vr:
                        return await vr.read()
                raise Exception("Нет данных видео в ответе")
    raise Exception("Превышено время ожидания (6 мин)")

# ══════════════════════════════════════════════════════════
#  ОБРАБОТЧИКИ — СТАРТ / МЕНЮ
# ══════════════════════════════════════════════════════════

WELCOME_NEW = """👋 Привет, {name}!

Я — AI-ассистент Александра. Умею:
🖼️ Генерировать изображения (Imagen 4)
🎬 Создавать видео (Veo 3.1) 
💬 Консультировать по нейросетям и VPN
💳 Оформлять подписки без зарубежной карты

🎁 Тебе начислено <b>{credits} бесплатных кредитов</b>!

Выбери действие 👇"""

WELCOME_BACK = """👋 С возвращением, {name}!

💳 Баланс: <b>{credits} кредитов</b>

Выбери действие 👇"""


@dp.message(F.text == "/start")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await ensure_user(message.from_user.id)
    credits = await get_credits(message.from_user.id)
    is_new = credits == FREE_CREDITS

    text = (WELCOME_NEW if is_new else WELCOME_BACK).format(
        name=message.from_user.first_name,
        credits=credits
    )
    is_admin = (message.from_user.id == ADMIN_ID)
    await message.answer("👇", reply_markup=kb_reply(is_admin))
    await message.answer(text, reply_markup=kb_main(), parse_mode="HTML")


@dp.message(F.text == "/admin")
async def cmd_admin(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    # Показываем нижнее меню с кнопкой Админ
    await message.answer("👇", reply_markup=kb_reply(is_admin=True))
    # Показываем админ панель
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations") as c:
            row = await c.fetchone()
            gens, credits_used = row[0], row[1]
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM payments") as c:
            row = await c.fetchone()
            payments, revenue = row[0], row[1]
        async with db.execute(
            "SELECT user_id, credits FROM users ORDER BY credits DESC LIMIT 5"
        ) as c:
            top = await c.fetchall()
    top_text = "\n".join([f"  {i+1}. ID {r[0]} — {r[1]} кр" for i, r in enumerate(top)])
    await message.answer(
        f"⚙️ <b>Админ панель</b>\n\n"
        f"👥 Всего пользователей: <b>{users}</b>\n"
        f"🎨 Всего генераций: <b>{gens}</b>\n"
        f"💸 Кредитов использовано: <b>{credits_used}</b>\n"
        f"💳 Платежей: <b>{payments}</b>\n"
        f"💰 Выручка: <b>{revenue}₽</b>\n\n"
        f"<b>Топ по балансу:</b>\n{top_text}",
        reply_markup=kb_admin_panel(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "back_main")
async def back_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    credits = await get_credits(cb.from_user.id)
    await cb.message.edit_text(
        f"👋 {cb.from_user.first_name}, баланс: <b>{credits} кр</b>\n\nВыбери действие 👇",
        reply_markup=kb_main(), parse_mode="HTML"
    )
    await cb.answer()

# ══════════════════════════════════════════════════════════
#  БАЛАНС / ОПЛАТА
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "menu_balance")
async def menu_balance(cb: CallbackQuery):
    cr = await get_credits(cb.from_user.id)
    lines = []
    for k, m in IMAGE_MODELS.items():
        lines.append(f"{'✅' if cr >= m['credits'] else '❌'} {m['name']} — {m['credits']} кр")
    for k, m in VIDEO_MODELS.items():
        lines.append(f"{'✅' if cr >= m['credits'] else '❌'} {m['name']} — {m['credits']} кр")

    await cb.message.edit_text(
        f"💳 <b>Баланс: {cr} кредитов</b>\n\n"
        f"<b>Доступно:</b>\n" + "\n".join(lines),
        reply_markup=kb_buy(), parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data == "menu_buy")
async def menu_buy(cb: CallbackQuery):
    cr = await get_credits(cb.from_user.id)
    await cb.message.edit_text(
        f"🛒 <b>Купить кредиты</b>\n\n💳 Баланс: <b>{cr} кр</b>\n\n"
        f"🥉 Старт — 50 кр → 199₽\n"
        f"🥈 Стандарт — 150 кр → 499₽\n"
        f"🥇 Про — 500 кр → 1490₽",
        reply_markup=kb_buy(), parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("buy:"))
async def buy_pack(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    p = CREDIT_PACKS[key]
    await cb.message.edit_text(
        f"{p['name']}\n\n💎 <b>{p['credits']} кредитов</b>\n💰 {p['price']}₽\n\nВыбери способ оплаты:",
        reply_markup=kb_pay_method(key), parse_mode="HTML"
    )
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


@dp.message(F.successful_payment)
async def on_payment(message: Message):
    parts = message.successful_payment.invoice_payload.split(":")
    key = parts[1]
    p = CREDIT_PACKS[key]
    await add_credits(message.from_user.id, p["credits"])
    cr = await get_credits(message.from_user.id)
    await message.answer(
        f"✅ <b>Оплата прошла!</b>\n\n"
        f"💎 Начислено: +{p['credits']} кредитов\n"
        f"💳 Баланс: <b>{cr} кр</b>",
        reply_markup=kb_back(), parse_mode="HTML"
    )

# ══════════════════════════════════════════════════════════
#  ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "menu_image")
async def menu_image(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cr = await get_credits(cb.from_user.id)
    await cb.message.edit_text(
        f"🖼️ <b>Создать изображение</b>\n\n"
        f"💳 Баланс: <b>{cr} кр</b>\n\n"
        f"⚡ <b>Imagen 4 Fast</b> — 1 кр | ~2 сек\n"
        f"✨ <b>Imagen 4</b> — 2 кр | ~5 сек\n"
        f"💎 <b>Imagen 4 Ultra</b> — 3 кр | ~8 сек",
        reply_markup=kb_image_models(), parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("imodel:"))
async def choose_img_model(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    m = IMAGE_MODELS[key]
    cr = await get_credits(cb.from_user.id)
    if cr < m["credits"]:
        await cb.answer(f"❌ Нужно {m['credits']} кр, у тебя {cr}", show_alert=True)
        return
    await state.update_data(model_key=key)
    await state.set_state(ImgState.waiting_prompt)
    await cb.message.edit_text(
        f"{m['name']} ✅\n\n"
        f"💳 Спишется: <b>{m['credits']} кр</b>\n"
        f"⏱ Время: {m['speed']}\n\n"
        f"✏️ <b>Введи промт:</b>\n\n"
        f"<i>Пример: A futuristic city at night, neon lights, cyberpunk, 4k</i>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )
    await cb.answer()


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
        f"💳 <b>{m['credits']} кр</b>\n"
        f"⏱ {m['speed']}\n\n"
        f"📄 <i>{prompt}</i>",
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
        await cb.answer("❌ Недостаточно кредитов!", show_alert=True)
        return

    await state.clear()
    wait = await cb.message.edit_text(
        f"⏳ Генерирую...\n\n🤖 {m['name']}\n<i>{prompt[:80]}</i>",
        parse_mode="HTML"
    )

    try:
        img_bytes = await api_generate_image(prompt, m["model_id"])
        await log_gen(cb.from_user.id, "image", key, m["credits"])
        cr = await get_credits(cb.from_user.id)
        await cb.message.answer_photo(
            BufferedInputFile(img_bytes, "image.png"),
            caption=f"✅ Готово! {m['name']}\n💳 Списано {m['credits']} кр | Остаток: {cr} кр",
            reply_markup=kb_after("image")
        )
        await wait.delete()
    except Exception as e:
        await add_credits(cb.from_user.id, m["credits"])
        await cb.message.edit_text(
            f"❌ Ошибка: {e}\n\nКредиты возвращены.",
            reply_markup=kb_back()
        )
    await cb.answer()


@dp.callback_query(F.data.startswith("chprompt:img:"))
async def change_img_prompt(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    await state.update_data(model_key=key)
    await state.set_state(ImgState.waiting_prompt)
    await cb.message.edit_text(
        f"✏️ Введи новый промт для <b>{IMAGE_MODELS[key]['name']}</b>:",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )
    await cb.answer()

# ══════════════════════════════════════════════════════════
#  ГЕНЕРАЦИЯ ВИДЕО
# ══════════════════════════════════════════════════════════

@dp.callback_query(F.data == "menu_video")
async def menu_video(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    cr = await get_credits(cb.from_user.id)
    await cb.message.edit_text(
        f"🎬 <b>Создать видео (8 сек)</b>\n\n"
        f"💳 Баланс: <b>{cr} кр</b>\n\n"
        f"💰 <b>Veo 3.1 Lite</b> — 15 кр | 720p\n"
        f"⚡ <b>Veo 3.1 Fast</b> — 25 кр | 1080p\n"
        f"🎬 <b>Veo 3.1</b> — 65 кр | 4K + аудио\n\n"
        f"⏱ <i>Время генерации: 1–6 минут</i>",
        reply_markup=kb_video_models(), parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data.startswith("vmodel:"))
async def choose_vid_model(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    m = VIDEO_MODELS[key]
    cr = await get_credits(cb.from_user.id)
    if cr < m["credits"]:
        await cb.answer(f"❌ Нужно {m['credits']} кр, у тебя {cr}. Пополни баланс!", show_alert=True)
        return
    await state.update_data(model_key=key)
    await state.set_state(VidState.waiting_prompt)
    await cb.message.edit_text(
        f"{m['name']} ✅\n\n"
        f"💳 Спишется: <b>{m['credits']} кр</b>\n"
        f"📐 {m['res']} | 8 сек\n\n"
        f"✏️ <b>Введи промт:</b>\n\n"
        f"<i>Пример: A drone flies over Tokyo at night, cinematic, smooth motion</i>",
        reply_markup=kb_cancel(), parse_mode="HTML"
    )
    await cb.answer()


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
        f"💳 <b>{m['credits']} кр</b> ({m['price']})\n\n"
        f"📄 <i>{prompt}</i>\n\n"
        f"⚠️ <i>Генерация занимает 1–6 минут</i>",
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
        await cb.answer("❌ Недостаточно кредитов!", show_alert=True)
        return

    await state.clear()
    await cb.message.edit_text(
        f"⏳ <b>Генерирую видео...</b>\n\n"
        f"🤖 {m['name']} | {m['res']}\n"
        f"📄 <i>{prompt[:80]}</i>\n\n"
        f"⏱ Обычно 1–6 минут. Пришлю как только готово 👇",
        parse_mode="HTML"
    )

    try:
        vid_bytes = await api_generate_video(prompt, m["model_id"])
        await log_gen(cb.from_user.id, "video", key, m["credits"])
        cr = await get_credits(cb.from_user.id)
        await cb.message.answer_video(
            BufferedInputFile(vid_bytes, "video.mp4"),
            caption=f"✅ Готово! {m['name']} | {m['res']}\n💳 Списано {m['credits']} кр | Остаток: {cr} кр",
            reply_markup=kb_after("video")
        )
    except Exception as e:
        await add_credits(cb.from_user.id, m["credits"])
        await cb.message.answer(
            f"❌ Ошибка: {e}\n\nКредиты возвращены.",
            reply_markup=kb_back()
        )
    await cb.answer()


@dp.callback_query(F.data.startswith("chprompt:vid:"))
async def change_vid_prompt(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[2]
    await state.update_data(model_key=key)
    await state.set_state(VidState.waiting_prompt)
    await cb.message.edit_text(
        f"✏️ Введи новый промт для <b>{VIDEO_MODELS[key]['name']}</b>:",
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
        "💬 <b>Консультант AI</b>\n\n"
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
    await bot.send_chat_action(message.chat.id, "typing")
    uid = message.from_user.id
    reply = await claude_with_search(uid, message.text)
    await message.answer(reply, reply_markup=kb_cancel(), parse_mode="HTML")

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
                 f"🖼️ Создать изображение (Imagen 4)\n"
                 f"🎬 Создать видео (Veo 3.1)\n"
                 f"💬 Разобраться в нейросетях\n"
                 f"💳 Оформить подписку без VPN и карты\n\n"
                 f"🎁 Тебе начислено <b>{FREE_CREDITS} бесплатных кредитов</b>!\n\n"
                 f"Напиши /start чтобы начать 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 Начать", callback_data="back_main")],
                [InlineKeyboardButton(text="💬 Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}")],
            ]),
            parse_mode="HTML"
        )
    except Exception as e:
        logging.warning(f"Не удалось отправить приветствие {user.id}: {e}")


# ══════════════════════════════════════════════════════════
#  ФУНКЦИЯ CLAUDE С ВЕБ-ПОИСКОМ
# ══════════════════════════════════════════════════════════

async def claude_with_search(uid: int, user_text: str) -> str:
    if uid not in user_conversations:
        user_conversations[uid] = []

    user_conversations[uid].append({"role": "user", "content": user_text})
    if len(user_conversations[uid]) > 20:
        user_conversations[uid] = user_conversations[uid][-20:]

    try:
        messages = list(user_conversations[uid])

        resp = claude_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=messages,
        )

        # Обрабатываем tool_use если Claude решил искать
        while resp.stop_reason == "tool_use":
            assistant_content = resp.content
            tool_results = []
            for block in assistant_content:
                if block.type == "tool_result":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.tool_use_id,
                        "content": block.content,
                    })

            messages.append({"role": "assistant", "content": assistant_content})
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            resp = claude_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                tools=[{"type": "web_search_20250305", "name": "web_search"}],
                messages=messages,
            )

        reply = ""
        for block in resp.content:
            if hasattr(block, "text"):
                reply += block.text

        if not reply:
            reply = "Попробуй переформулировать вопрос 🙏"

        user_conversations[uid].append({"role": "assistant", "content": reply})
        return reply

    except Exception as e:
        logging.error(f"Claude API error: {e}")
        # Fallback без поиска
        try:
            resp = claude_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                messages=user_conversations[uid],
            )
            reply = resp.content[0].text
            user_conversations[uid].append({"role": "assistant", "content": reply})
            return reply
        except Exception as e2:
            logging.error(f"Fallback error: {e2}")
            return "Что-то пошло не так 😅 Попробуй ещё раз или напиши @neirosetkaalex"


# ══════════════════════════════════════════════════════════
#  REPLY KEYBOARD HANDLERS
# ══════════════════════════════════════════════════════════

@dp.message(F.text == "🏠 Главное меню")
async def reply_main_menu(message: Message, state: FSMContext):
    await state.clear()
    credits = await get_credits(message.from_user.id)
    await message.answer(
        f"👋 {message.from_user.first_name}, баланс: <b>{credits} кр</b>\n\nВыбери действие 👇",
        reply_markup=kb_main(), parse_mode="HTML"
    )


@dp.message(F.text == "🎨 Создать фото")
async def reply_create_photo(message: Message, state: FSMContext):
    await state.clear()
    cr = await get_credits(message.from_user.id)
    await message.answer(
        f"🖼️ <b>Создать изображение</b>\n\n"
        f"💳 Баланс: <b>{cr} кр</b>\n\n"
        f"⚡ <b>Imagen 4 Fast</b> — 1 кр | ~2 сек\n"
        f"✨ <b>Imagen 4</b> — 2 кр | ~5 сек\n"
        f"💎 <b>Imagen 4 Ultra</b> — 3 кр | ~8 сек",
        reply_markup=kb_image_models(), parse_mode="HTML"
    )


@dp.message(F.text == "🎬 Создать видео")
async def reply_create_video(message: Message, state: FSMContext):
    await state.clear()
    cr = await get_credits(message.from_user.id)
    await message.answer(
        f"🎬 <b>Создать видео (8 сек)</b>\n\n"
        f"💳 Баланс: <b>{cr} кр</b>\n\n"
        f"💰 <b>Veo 3.1 Lite</b> — 15 кр | 720p\n"
        f"⚡ <b>Veo 3.1 Fast</b> — 25 кр | 1080p\n"
        f"🎬 <b>Veo 3.1</b> — 65 кр | 4K + аудио\n\n"
        f"⏱ <i>Время генерации: 1–6 минут</i>",
        reply_markup=kb_video_models(), parse_mode="HTML"
    )


@dp.message(F.text == "👤 Мой профиль")
async def reply_profile(message: Message):
    uid = message.from_user.id
    await ensure_user(uid)
    cr = await get_credits(uid)

    # Считаем генерации
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE user_id=?", (uid,)
        ) as c:
            row = await c.fetchone()
            total_gens = row[0]
            total_credits_spent = row[1]

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
        f"👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"👋 Имя: {message.from_user.full_name}\n\n"
        f"💳 <b>Баланс: {cr} кредитов</b>\n"
        f"🎨 Генераций сделано: {total_gens}\n"
        f"💸 Кредитов потрачено: {total_credits_spent}\n\n"
        f"<b>Доступно сейчас:</b>\n" + "\n".join(can)
    )
    await message.answer(text, reply_markup=kb_buy(), parse_mode="HTML")


def kb_admin_panel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика сегодня", callback_data="adm_stat_day"),
         InlineKeyboardButton(text="📈 За неделю",          callback_data="adm_stat_week")],
        [InlineKeyboardButton(text="➕ Начислить кредиты",  callback_data="adm_give_credits")],
        [InlineKeyboardButton(text="🚫 Блокировки",         callback_data="adm_blocks")],
        [InlineKeyboardButton(text="🏠 Главное меню",        callback_data="back_main")],
    ])

def kb_block_actions(target_id: int, currently_blocked: bool):
    action = "adm_unblock" if currently_blocked else "adm_block"
    label = "✅ Разблокировать" if currently_blocked else "🚫 Заблокировать"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label, callback_data=f"{action}:{target_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_blocks")],
    ])


@dp.message(F.text == "⚙️ Админ панель")
async def reply_admin(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            users = (await c.fetchone())[0]
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations") as c:
            row = await c.fetchone()
            gens, credits_used = row[0], row[1]
        async with db.execute("SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM payments") as c:
            row = await c.fetchone()
            payments, revenue = row[0], row[1]
        async with db.execute(
            "SELECT user_id, credits FROM users ORDER BY credits DESC LIMIT 5"
        ) as c:
            top = await c.fetchall()

    top_text = "\n".join([f"  {i+1}. ID {r[0]} — {r[1]} кр" for i, r in enumerate(top)])

    await message.answer(
        f"⚙️ <b>Админ панель</b>\n\n"
        f"👥 Всего пользователей: <b>{users}</b>\n"
        f"🎨 Всего генераций: <b>{gens}</b>\n"
        f"💸 Кредитов использовано: <b>{credits_used}</b>\n"
        f"💳 Платежей: <b>{payments}</b>\n"
        f"💰 Выручка: <b>{revenue}₽</b>\n\n"
        f"<b>Топ по балансу:</b>\n{top_text}",
        reply_markup=kb_admin_panel(),
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "adm_stat_day")
async def adm_stat_day(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= date('now')"
        ) as c:
            new_users = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE created_at >= date('now')"
        ) as c:
            row = await c.fetchone()
            gens, credits_used = row[0], row[1]
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM payments WHERE created_at >= date('now')"
        ) as c:
            row = await c.fetchone()
            pays, revenue = row[0], row[1]
        async with db.execute(
            "SELECT type, COUNT(*) FROM generations WHERE created_at >= date('now') GROUP BY type"
        ) as c:
            by_type = await c.fetchall()

    by_type_text = "\n".join([f"  • {r[0]}: {r[1]} шт" for r in by_type]) or "  нет данных"

    await cb.message.answer(
        f"📊 <b>Статистика за сегодня</b>\n\n"
        f"👤 Новых пользователей: <b>{new_users}</b>\n"
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

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= date('now', '-7 days')"
        ) as c:
            new_users = (await c.fetchone())[0]
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(credits),0) FROM generations WHERE created_at >= date('now', '-7 days')"
        ) as c:
            row = await c.fetchone()
            gens, credits_used = row[0], row[1]
        async with db.execute(
            "SELECT COUNT(*), COALESCE(SUM(amount_rub),0) FROM payments WHERE created_at >= date('now', '-7 days')"
        ) as c:
            row = await c.fetchone()
            pays, revenue = row[0], row[1]
        async with db.execute(
            "SELECT date(created_at), COUNT(*) FROM generations "
            "WHERE created_at >= date('now', '-7 days') GROUP BY date(created_at) ORDER BY 1"
        ) as c:
            by_day = await c.fetchall()

    by_day_text = "\n".join([f"  {r[0]}: {r[1]} ген." for r in by_day]) or "  нет данных"

    await cb.message.answer(
        f"📈 <b>Статистика за 7 дней</b>\n\n"
        f"👤 Новых пользователей: <b>{new_users}</b>\n"
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
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_cancel")]
        ]),
        parse_mode="HTML"
    )
    await cb.answer()


@dp.message(AdminState.waiting_user_id)
async def adm_get_user_id(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(message.text.strip())
        user = await get_user(target_id)
        if not user:
            await message.answer("❌ Пользователь не найден. Введи другой ID:")
            return
        await state.update_data(target_id=target_id)
        await state.set_state(AdminState.waiting_credits)
        await message.answer(
            f"👤 Пользователь найден\n"
            f"ID: <code>{target_id}</code>\n"
            f"Текущий баланс: <b>{user['credits']} кр</b>\n\n"
            f"Сколько кредитов начислить?",
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer("❌ Введи числовой ID:")


@dp.message(AdminState.waiting_credits)
async def adm_give_credits_confirm(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        amount = int(message.text.strip())
        if amount <= 0:
            await message.answer("❌ Введи положительное число:")
            return
        data = await state.get_data()
        target_id = data["target_id"]
        await add_credits(target_id, amount)
        new_balance = await get_credits(target_id)
        await state.clear()
        await message.answer(
            f"✅ <b>Кредиты начислены!</b>\n\n"
            f"👤 ID: <code>{target_id}</code>\n"
            f"➕ Начислено: <b>{amount} кр</b>\n"
            f"💳 Новый баланс: <b>{new_balance} кр</b>",
            parse_mode="HTML"
        )
        # Уведомляем пользователя
        try:
            await bot.send_message(
                target_id,
                f"🎁 Тебе начислено <b>{amount} кредитов</b> от администратора!\n"
                f"💳 Баланс: <b>{new_balance} кр</b>",
                parse_mode="HTML"
            )
        except Exception:
            pass
    except ValueError:
        await message.answer("❌ Введи числовое количество кредитов:")


@dp.callback_query(F.data == "adm_cancel")
async def adm_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Отменено")
    await cb.answer()


# ─── Блокировки ───────────────────────────────────────────

@dp.callback_query(F.data == "adm_blocks")
async def adm_blocks_menu(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("❌ Нет доступа", show_alert=True)
        return
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT COUNT(*) FROM users WHERE is_blocked=1"
            ) as c:
                blocked_count = (await c.fetchone())[0]
            async with db.execute(
                "SELECT user_id FROM users WHERE is_blocked=1 LIMIT 10"
            ) as c:
                blocked_list = await c.fetchall()

        blocked_text = ", ".join([str(r[0]) for r in blocked_list]) or "нет"

        await cb.message.answer(
            f"🚫 <b>Блокировки</b>\n\n"
            f"Заблокировано пользователей: <b>{blocked_count}</b>\n"
            f"ID: {blocked_text}\n\n"
            f"Введи ID пользователя чтобы заблокировать или разблокировать:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_cancel")]
            ]),
            parse_mode="HTML"
        )
        await state.set_state(AdminState.waiting_block_id)
    except Exception as e:
        logging.error(f"adm_blocks error: {e}")
        await cb.message.answer(f"❌ Ошибка: {e}")
    finally:
        await cb.answer()


@dp.message(AdminState.waiting_block_id)
async def adm_block_check_user(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    try:
        target_id = int(message.text.strip())
        user = await get_user(target_id)
        if not user:
            await message.answer("❌ Пользователь не найден. Введи другой ID:")
            return
        blocked = bool(user.get("is_blocked", 0))
        status = "🚫 Заблокирован" if blocked else "✅ Активен"
        await state.clear()
        await message.answer(
            f"👤 ID: <code>{target_id}</code>\n"
            f"Статус: {status}\n"
            f"Баланс: <b>{user['credits']} кр</b>",
            reply_markup=kb_block_actions(target_id, blocked),
            parse_mode="HTML"
        )
    except ValueError:
        await message.answer("❌ Введи числовой ID:")


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
        await cb.message.answer(f"❌ Ошибка: {e}")
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
        await cb.message.answer(f"❌ Ошибка: {e}")
    finally:
        await cb.answer()


# ══════════════════════════════════════════════════════════
#  ОБЫЧНЫЕ СООБЩЕНИЯ (вне FSM — консультант по умолчанию)
# ══════════════════════════════════════════════════════════

@dp.message()
async def handle_message(message: Message, state: FSMContext):
    await ensure_user(message.from_user.id)
    uid = message.from_user.id
    if await is_blocked(uid):
        await message.answer("🚫 Ваш доступ к боту ограничен.")
        return
    await bot.send_chat_action(message.chat.id, "typing")
    reply = await claude_with_search(uid, message.text)
    await message.answer(reply, reply_markup=kb_contact(), parse_mode="HTML")

# ══════════════════════════════════════════════════════════
#  ЗАПУСК
# ══════════════════════════════════════════════════════════

async def main():
    await init_db()
    logging.info("✅ Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
