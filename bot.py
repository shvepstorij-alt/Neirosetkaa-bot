import asyncio
import logging
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, ChatMemberUpdated, InlineKeyboardMarkup,
    InlineKeyboardButton, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    BufferedInputFile
)
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION, Command
import anthropic
from dotenv import load_dotenv

from database import (
    init_db, get_user, create_user, add_credits,
    spend_credits, set_plan, get_stats, get_all_users,
    get_all_user_ids, log_question, get_popular_services,
    block_user, unblock_user, is_blocked, find_user_by_username,
    get_today_activity, get_top_active_users, get_credits_history,
    get_credits_spent_by_user, get_maintenance_mode, set_maintenance_mode,
    get_welcome_message, set_welcome_message, log_message
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "AleksandrOii")
PERSONAL_USERNAME = os.getenv("PERSONAL_USERNAME", "AleksandrOii")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
user_conversations = {}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# МОДЕЛИ И КРЕДИТЫ (с наценкой 30%)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CREDIT_VALUE_RUB = 5  # 1 кредит = 5 рублей

IMAGE_MODELS = {
    "img4":    {"name": "Imagen 4",         "model": "imagen-4.0-generate-001",        "credits": 1,  "price_rub": 5,   "desc": "Высокое качество Google"},
    "nb2":     {"name": "Nano Banana 2",    "model": "gemini-3.1-flash-image-preview", "credits": 2,  "price_rub": 10,  "desc": "Gemini — быстро и чётко"},
    "nb_pro":  {"name": "Nano Banana Pro",  "model": "gemini-3-pro-image-preview",     "credits": 3,  "price_rub": 15,  "desc": "Лучшее качество + текст на фото"},
}

VIDEO_MODELS = {
    "veo31l":  {"name": "Veo 3.1 Lite",  "model": "veo-3.1-lite-generate-preview",  "credits": 10,  "price_rub": 50,  "desc": "Быстро, базовое качество"},
    "veo31f":  {"name": "Veo 3.1 Fast",  "model": "veo-3.1-fast-generate-preview",  "credits": 30,  "price_rub": 150, "desc": "Быстро + хорошее качество"},
    "veo31":   {"name": "Veo 3.1",       "model": "veo-3.1-generate-preview",        "credits": 80,  "price_rub": 400, "desc": "Максимальное качество + аудио"},
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# СИСТЕМНЫЙ ПРОМПТ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MY_PRICES = """
ChatGPT Plus — 2000 руб/мес | Claude Pro — 2000 руб/мес | SuperGrok — 2000 руб/мес
Gamma Basic — 1200 руб/мес | Gamma Pro — 2300 руб/мес
Midjourney Basic — 1000 руб/мес | Midjourney Standard — 3000 руб/мес
Cursor Pro — 2300 руб/мес | Kling AI Standard — 1000 руб/мес | Kling AI Pro — 2700 руб/мес
Perplexity Pro — 2000 руб/мес | HeyGen Creator — 3000 руб/мес
Higgsfield Starter — 1700 руб/мес | Higgsfield Plus — 5100 руб/мес
Runway Standard — 1700 руб/мес | Runway Pro — 3700 руб/мес
Krea Basic — 1000 руб/мес | Krea Pro — 3200 руб/мес
Lovable Pro — 2700 руб/мес | Suno Pro — 1000 руб/мес | Suno Premier — 3000 руб/мес
Zoom Pro — 2000 руб/мес | Zoom Business — 2300 руб/мес
"""

SYSTEM_PROMPT = f"""Ты — AI-консультант по нейросетям и AI-инструментам.

Характер: дружелюбный, экспертный, по делу. Пишешь на русском.
Форматирование: жирный текст только для названий сервисов и ключевых цифр. Никаких других markdown символов.

МОИ ЦЕНЫ (только эти называй клиентам, в рублях):
{MY_PRICES}

ПРАВИЛА:
1. Цены — ТОЛЬКО из списка выше.
2. ВСЕГДА используй web_search перед ответом о возможностях любого сервиса.
3. Если клиент не знает что выбрать — спроси для каких задач.
4. Упоминай @{PERSONAL_USERNAME} для покупки только когда уместно, ненавязчиво.
5. Оплата только в рублях. Оформление за 5-15 минут.
6. По VPN — рекомендуй Outline, Lantern, Windscribe.
7. ЗАПРЕЩЕНО: политика, экономика, медицина, новости, всё не связанное с AI.
8. На запрещённые темы: "Я консультирую только по нейросетям 🤖"
"""

# Пакеты кредитов (1 кредит = 5 рублей)
CREDIT_PACKS = {
    "pack50":   {"name": "50 кредитов",   "price_stars": 50,   "price_rub": 250,  "credits": 50,  "description": "50 кредитов — 250 ₽"},
    "pack100":  {"name": "100 кредитов",  "price_stars": 100,  "price_rub": 500,  "credits": 100, "description": "100 кредитов — 500 ₽"},
    "pack300":  {"name": "300 кредитов",  "price_stars": 300,  "price_rub": 1500, "credits": 300, "description": "300 кредитов — 1500 ₽"},
    "pack600":  {"name": "600 кредитов",  "price_stars": 600,  "price_rub": 3000, "credits": 600, "description": "600 кредитов — 3000 ₽"},
    "pack1000": {"name": "1000 кредитов", "price_stars": 1000, "price_rub": 5000, "credits": 1000,"description": "1000 кредитов — 5000 ₽"},
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# КЛАВИАТУРЫ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Помочь с выбором",       callback_data="help_choose"),
         InlineKeyboardButton(text="🔒 VPN для РФ",             callback_data="vpn_help")],
        [InlineKeyboardButton(text="🎨 Генерация изображений",  callback_data="choose_image_model"),
         InlineKeyboardButton(text="🎬 Создать видео",          callback_data="choose_video_model")],
        [InlineKeyboardButton(text="💰 Мой баланс",             callback_data="my_balance"),
         InlineKeyboardButton(text="🛒 Купить кредиты",         callback_data="buy_credits")],
        [InlineKeyboardButton(text="✍️ Написать для покупки",   url=f"https://t.me/{PERSONAL_USERNAME}")],
    ])

def contact_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать для покупки", url=f"https://t.me/{PERSONAL_USERNAME}")],
        [InlineKeyboardButton(text="🏠 Главное меню",         callback_data="main_menu")],
    ])

def image_model_keyboard(user_credits: int):
    rows = []
    for key, m in IMAGE_MODELS.items():
        enough = "✅" if user_credits >= m["credits"] else "❌"
        rows.append([InlineKeyboardButton(
            text=f"{enough} {m['name']} — {m['credits']} кр. | {m['desc']}",
            callback_data=f"img_model_{key}"
        )])
    rows.append([InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def video_model_keyboard(user_credits: int):
    rows = []
    for key, m in VIDEO_MODELS.items():
        enough = "✅" if user_credits >= m["credits"] else "❌"
        rows.append([InlineKeyboardButton(
            text=f"{enough} {m['name']} — {m['credits']} кр. | {m['desc']}",
            callback_data=f"vid_model_{key}"
        )])
    rows.append([InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")])
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def services_keyboard():
    services = [
        ("🤖 ChatGPT", "info_chatgpt"), ("🧠 Claude", "info_claude"),
        ("⚡ Grok", "info_grok"), ("🎨 Midjourney", "info_midjourney"),
        ("💻 Cursor", "info_cursor"), ("🔍 Perplexity", "info_perplexity"),
        ("✨ Krea AI", "info_krea"), ("🎵 Suno", "info_suno"),
        ("🎬 Kling AI", "info_kling"), ("🎥 Runway", "info_runway"),
        ("🎥 HeyGen", "info_heygen"), ("🎬 Higgsfield", "info_higgsfield"),
        ("🌐 Lovable", "info_lovable"), ("📊 Gamma", "info_gamma"),
        ("📹 Zoom", "info_zoom"),
    ]
    rows = [[InlineKeyboardButton(text=s[0], callback_data=s[1]) for s in services[i:i+2]]
            for i in range(0, len(services), 2)]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def admin_keyboard():
    maintenance = get_maintenance_mode()
    maint_text = "✅ Техобслуж. ВКЛ" if maintenance else "🔄 Техобслуживание"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика",          callback_data="adm_stats"),
         InlineKeyboardButton(text="📈 Активность",          callback_data="adm_activity")],
        [InlineKeyboardButton(text="🔥 Популярные сервисы",  callback_data="adm_popular"),
         InlineKeyboardButton(text="🏆 Топ активных",        callback_data="adm_top_users")],
        [InlineKeyboardButton(text="👥 Пользователи",        callback_data="adm_users"),
         InlineKeyboardButton(text="🔍 Найти по @username",  callback_data="adm_find_user")],
        [InlineKeyboardButton(text="💳 Кредиты",             callback_data="adm_credits"),
         InlineKeyboardButton(text="📜 История начислений",  callback_data="adm_credits_history")],
        [InlineKeyboardButton(text="💰 Расход по юзерам",   callback_data="adm_credits_spent"),
         InlineKeyboardButton(text="🚫 Блокировки",          callback_data="adm_blocks")],
        [InlineKeyboardButton(text="✏️ Изменить приветствие", callback_data="adm_edit_welcome")],
        [InlineKeyboardButton(text="📣 Рассылка",            callback_data="adm_broadcast"),
         InlineKeyboardButton(text=maint_text,               callback_data="adm_maintenance")],
    ])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def generate_image(prompt: str, model_key: str) -> bytes | None:
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=GOOGLE_API_KEY)
        model_info = IMAGE_MODELS.get(model_key, IMAGE_MODELS["nb"])
        model_name = model_info["model"]

        if "gemini" in model_name:
            # Nano Banana — через generate_content с modalities
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE", "TEXT"]
                )
            )
            for part in response.candidates[0].content.parts:
                if hasattr(part, "inline_data") and part.inline_data:
                    return part.inline_data.data
        else:
            # Imagen 4
            response = client.models.generate_images(
                model=model_name,
                prompt=prompt,
                config=types.GenerateImagesConfig(
                    number_of_images=1,
                    aspect_ratio="1:1",
                    safety_filter_level="BLOCK_MEDIUM_AND_ABOVE",
                    person_generation="ALLOW_ADULT"
                )
            )
            if response.generated_images:
                return response.generated_images[0].image.image_bytes
    except Exception as e:
        logging.error(f"Image generation error: {e}")
    return None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ГЕНЕРАЦИЯ ВИДЕО
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def generate_video(prompt: str, model_key: str) -> bytes | None:
    try:
        from google import genai

        client = genai.Client(api_key=GOOGLE_API_KEY)
        model_info = VIDEO_MODELS.get(model_key, VIDEO_MODELS["veo31f"])
        model_name = model_info["model"]

        operation = client.models.generate_video(
            model=model_name,
            prompt=prompt,
            config=genai.types.GenerateVideoConfig(
                aspect_ratio="9:16",
                number_of_videos=1
            )
        )

        # Ждём результат (до 5 минут)
        for _ in range(60):
            await asyncio.sleep(5)
            operation = client.operations.get(operation)
            if operation.done:
                break

        if operation.done and hasattr(operation, "response"):
            videos = operation.response.generated_videos
            if videos:
                return videos[0].video.video_bytes
    except Exception as e:
        logging.error(f"Video generation error: {e}")
    return None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CLAUDE С ВЕБ-ПОИСКОМ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def get_claude_response(messages: list) -> str:
    try:
        response = claude_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=messages
        )
        full_text = ""
        for block in response.content:
            if hasattr(block, "type") and block.type == "text":
                full_text += block.text
        return full_text.strip() or f"Не удалось получить ответ. Напиши: @{PERSONAL_USERNAME}"
    except Exception as e:
        logging.error(f"Claude API error: {e}")
        return None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ПРИВЕТСТВИЕ НОВЫХ ПОДПИСЧИКОВ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated):
    if str(event.chat.id) != str(CHANNEL_ID):
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return
    create_user(user.id, user.username, user.first_name)
    try:
        custom_msg = get_welcome_message()
        text = custom_msg if custom_msg else (
            f"👋 Привет, {user.first_name}!\n\n"
            "Я AI-консультант по нейросетям 🤖\n\n"
            "Помогу тебе:\n"
            "🔍 Выбрать нужную нейросеть\n"
            "💳 Узнать цены и оформить подписку без VPN\n"
            "🎨 Сгенерировать изображения и видео\n"
            "🔒 Разобраться с VPN для России\n\n"
            "Просто напиши или выбери ниже 👇"
        )
        await bot.send_message(user.id, text, parse_mode="Markdown", reply_markup=main_keyboard())
    except Exception as e:
        logging.warning(f"Не удалось отправить {user.id}: {e}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# КОМАНДЫ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.message(Command("start"))
async def cmd_start(message: Message):
    create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Я AI-консультант по нейросетям.\n"
        "Отвечу на вопросы, расскажу о тарифах, помогу сгенерировать изображение или видео 🚀\n\n"
        "Выбери что тебя интересует 👇",
        reply_markup=main_keyboard()
    )

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await message.answer("🔧 *Админ панель*\n\nВыбери раздел:", parse_mode="Markdown", reply_markup=admin_keyboard())

@dp.message(Command("add_credits"))
async def cmd_add_credits(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        parts = message.text.split()
        add_credits(int(parts[1]), int(parts[2]), "Ручное начисление")
        await message.answer(f"✅ Начислено {parts[2]} кредитов пользователю {parts[1]}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}\nФормат: /add_credits [user_id] [amount]")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CALLBACKS — ОСНОВНЫЕ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню 👇", reply_markup=main_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "help_choose")
async def cb_help_choose(callback: CallbackQuery):
    await callback.message.edit_text(
        "Для каких задач нужна нейросеть? 🤔\n\n"
        "• Тексты / посты\n• Изображения или видео\n• Код / разработка\n"
        "• Анализ документов\n• Музыка / озвучка\n• Поиск информации\n\n"
        "Опиши своими словами 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Все сервисы", callback_data="show_services")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "show_services")
async def cb_show_services(callback: CallbackQuery):
    await callback.message.edit_text("Выбери сервис 👇", reply_markup=services_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("info_"))
async def cb_service_info(callback: CallbackQuery):
    service_key = callback.data.replace("info_", "")
    service_names = {
        "chatgpt": "ChatGPT Plus", "claude": "Claude Pro", "grok": "SuperGrok",
        "midjourney": "Midjourney", "cursor": "Cursor Pro", "perplexity": "Perplexity Pro",
        "krea": "Krea AI", "suno": "Suno", "kling": "Kling AI", "runway": "Runway",
        "heygen": "HeyGen", "higgsfield": "Higgsfield", "lovable": "Lovable Pro",
        "gamma": "Gamma", "zoom": "Zoom",
    }
    name = service_names.get(service_key, service_key)
    log_question(callback.from_user.id, service_key)
    await callback.message.edit_text(f"🔍 Ищу актуальную информацию о *{name}*...", parse_mode="Markdown")
    messages = [{"role": "user", "content": f"Расскажи подробно что умеет {name}, для каких задач подходит и какая цена. Используй актуальную информацию."}]
    response = await get_claude_response(messages)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🛒 Купить {name}", url=f"https://t.me/{PERSONAL_USERNAME}")],
        [InlineKeyboardButton(text="◀️ К списку", callback_data="show_services")]
    ])
    await callback.message.edit_text(response or f"Не удалось загрузить. Напиши @{PERSONAL_USERNAME}", parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "vpn_help")
async def cb_vpn(callback: CallbackQuery):
    log_question(callback.from_user.id, "vpn")
    await callback.message.edit_text(
        "🔒 *VPN для России*\n\n"
        "*Бесплатные:*\n• Outline VPN — лучший выбор\n• Lantern — без настроек\n• Windscribe — 10 GB/мес\n\n"
        "*Платные:*\n• ExpressVPN — самый быстрый\n• NordVPN — много серверов\n\n"
        "💡 При покупке подписки через нас VPN не нужен — всё оформляем сами!",
        parse_mode="Markdown", reply_markup=contact_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "my_balance")
async def cb_balance(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        create_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
        user = get_user(callback.from_user.id)
    img_lines = "\n".join([f"• {m['name']}: {m['credits']} кр. ({m['price_rub']} ₽)" for m in IMAGE_MODELS.values()])
    vid_lines = "\n".join([f"• {m['name']}: {m['credits']} кр. ({m['price_rub']} ₽)" for m in VIDEO_MODELS.values()])
    text = (
        f"💰 *Мой баланс*\n\n"
        f"Кредиты: *{user['credits']}* кр. ({user['credits'] * 5} ₽)\n\n"
        f"*Изображения:*\n{img_lines}\n\n"
        f"*Видео (8 сек):*\n{vid_lines}\n\n"
        f"Консультации — бесплатно 🆓"
    )
    await callback.message.edit_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Пополнить", callback_data="buy_credits")],
            [InlineKeyboardButton(text="◀️ Меню", callback_data="main_menu")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "buy_credits")
async def cb_buy_credits(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    balance = user["credits"] if user else 0
    text = (
        "💰 *Купить кредиты*\n\n"
        f"Твой баланс: *{balance}* кр.\n"
        "1 кредит = 5 ₽\n\n"
        "*Что можно сделать:*\n"
        "• Imagen 4 — 1 кр. (5 ₽)\n"
        "• Nano Banana 2 — 2 кр. (10 ₽)\n"
        "• Nano Banana Pro — 3 кр. (15 ₽)\n"
        "• Veo 3.1 Lite — 10 кр. (50 ₽)\n"
        "• Veo 3.1 Fast — 30 кр. (150 ₽)\n"
        "• Veo 3.1 — 80 кр. (400 ₽)\n\n"
        "👇 Выбери пакет:"
    )
    buttons = [
        [InlineKeyboardButton(text=f"{p['name']} — {p['price_rub']} ₽", callback_data=f"buy_pack_{pid}")]
        for pid, p in CREDIT_PACKS.items()
    ]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("buy_pack_"))
async def cb_buy_pack(callback: CallbackQuery):
    pack_id = callback.data.replace("buy_pack_", "")
    pack = CREDIT_PACKS.get(pack_id)
    if not pack:
        await callback.answer("Пакет не найден")
        return
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=pack["name"], description=pack["description"],
        payload=f"pack_{pack_id}_{callback.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(label=pack["name"], amount=pack["price_stars"])],
        provider_token=""
    )
    await callback.answer()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CALLBACKS — ВЫБОР МОДЕЛИ ИЗОБРАЖЕНИЙ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.callback_query(F.data == "choose_image_model")
async def cb_choose_image_model(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        create_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
        user = get_user(callback.from_user.id)
    credits = user["credits"] if user else 0
    await callback.message.edit_text(
        f"🎨 *Генерация изображений*\n\nТвой баланс: *{credits}* кр.\n\nВыбери модель 👇",
        parse_mode="Markdown",
        reply_markup=image_model_keyboard(credits)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("img_model_"))
async def cb_img_model_selected(callback: CallbackQuery):
    model_key = callback.data.replace("img_model_", "")
    model_info = IMAGE_MODELS.get(model_key)
    if not model_info:
        await callback.answer("Модель не найдена")
        return
    user = get_user(callback.from_user.id)
    credits = user["credits"] if user else 0
    if credits < model_info["credits"]:
        await callback.message.edit_text(
            f"❌ Недостаточно кредитов\n\nНужно: *{model_info['credits']}* кр. | У тебя: *{credits}* кр.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="choose_image_model")]
            ])
        )
        await callback.answer()
        return
    user_conversations[f"gen_image_{callback.from_user.id}"] = model_key
    await callback.message.edit_text(
        f"🎨 *{model_info['name']}* — {model_info['credits']} кр.\n\n"
        f"Опиши что хочешь сгенерировать на русском или английском 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="choose_image_model")]
        ])
    )
    await callback.answer()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CALLBACKS — ВЫБОР МОДЕЛИ ВИДЕО
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.callback_query(F.data == "choose_video_model")
async def cb_choose_video_model(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        create_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
        user = get_user(callback.from_user.id)
    credits = user["credits"] if user else 0
    await callback.message.edit_text(
        f"🎬 *Генерация видео (8 сек)*\n\nТвой баланс: *{credits}* кр.\n\nВыбери модель 👇",
        parse_mode="Markdown",
        reply_markup=video_model_keyboard(credits)
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("vid_model_"))
async def cb_vid_model_selected(callback: CallbackQuery):
    model_key = callback.data.replace("vid_model_", "")
    model_info = VIDEO_MODELS.get(model_key)
    if not model_info:
        await callback.answer("Модель не найдена")
        return
    user = get_user(callback.from_user.id)
    credits = user["credits"] if user else 0
    if credits < model_info["credits"]:
        await callback.message.edit_text(
            f"❌ Недостаточно кредитов\n\nНужно: *{model_info['credits']}* кр. | У тебя: *{credits}* кр.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="choose_video_model")]
            ])
        )
        await callback.answer()
        return
    user_conversations[f"gen_video_{callback.from_user.id}"] = model_key
    await callback.message.edit_text(
        f"🎬 *{model_info['name']}* — {model_info['credits']} кр.\n\n"
        f"Опиши сцену для видео. Чем детальнее — тем лучше результат 👇\n\n"
        f"_Пример: закат над морем, волны бьются о скалы, сверху камера_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="choose_video_model")]
        ])
    )
    await callback.answer()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ОПЛАТА
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    parts = payload.split("_")
    pack_id = parts[1]
    pack = CREDIT_PACKS.get(pack_id)
    if pack:
        add_credits(message.from_user.id, pack["credits"], f"Покупка {pack['name']}")
        user = get_user(message.from_user.id)
        await message.answer(
            f"✅ *Оплата прошла!*\n\nНачислено: *{pack['credits']}* кредитов\nБаланс: *{user['credits']}* кр. 🎉",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CALLBACKS — АДМИН ПАНЕЛЬ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.callback_query(F.data == "adm_stats")
async def adm_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    s = get_stats()
    await callback.message.edit_text(
        "📊 *Статистика пользователей*\n\n"
        f"👥 Всего: *{s['total_users']}*\n"
        f"🆕 Новых сегодня: *{s['new_today']}*\n"
        f"📅 Новых за неделю: *{s['new_week']}*\n"
        f"💳 Платных: *{s['paid_users']}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_activity")
async def adm_activity(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    a = get_today_activity()
    await callback.message.edit_text(
        "📈 *Активность за сегодня*\n\n"
        f"✉️ Действий: *{a['messages_today']}*\n"
        f"👤 Активных пользователей: *{a['active_users_today']}*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_popular")
async def adm_popular(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    popular = get_popular_services(10)
    lines = "\n".join([f"{i+1}. *{n}* — {c} запросов" for i, (n, c) in enumerate(popular)]) if popular else "Пока нет данных"
    await callback.message.edit_text(
        f"🔥 *Популярные сервисы*\n\n{lines}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_top_users")
async def adm_top_users(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    top = get_top_active_users(10)
    lines = "\n".join([f"{i+1}. {'@'+u if u else f or str(uid)} — {c} действий" for i, (uid, u, f, c) in enumerate(top)]) if top else "Пока нет данных"
    await callback.message.edit_text(
        f"🏆 *Топ активных пользователей*\n\n{lines}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_users")
async def adm_users(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    users = get_all_users()
    lines = "\n".join([f"• {'@'+u if u else f or str(uid)} | {cr} кр. | {pl} | {(j or '')[:10]}" for uid, u, f, cr, pl, j in users[:15]])
    await callback.message.edit_text(
        f"👥 *Последние пользователи:*\n\n{lines or 'Пока никого'}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_find_user")
async def adm_find_user(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    user_conversations[f"find_user_{callback.from_user.id}"] = True
    await callback.message.edit_text(
        "🔍 *Поиск пользователя*\n\nНапиши @username или user_id:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_credits")
async def adm_credits(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    s = get_stats()
    await callback.message.edit_text(
        "💳 *Кредиты и генерации*\n\n"
        f"⚡ Потрачено кредитов: *{s['total_credits_spent']}*\n"
        f"🎨 Генераций изображений: *{s['total_images']}*\n\n"
        "Нажми кнопку чтобы выдать кредиты пользователю 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Выдать кредиты", callback_data="adm_give_credits")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_give_credits")
async def adm_give_credits(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    user_conversations[f"give_credits_step_{callback.from_user.id}"] = "awaiting_user_id"
    await callback.message.edit_text(
        "🎁 *Выдача кредитов — Шаг 1 из 2*\n\nНапиши user_id пользователя:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_credits")]])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_amount_"))
async def adm_choose_amount(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    parts = callback.data.split("_")
    target_uid = int(parts[2])
    amount = int(parts[3])
    user = get_user(target_uid)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    add_credits(target_uid, amount, "Выдано админом")
    updated = get_user(target_uid)
    name = f"@{user['username']}" if user.get('username') else user.get('first_name') or str(target_uid)
    await callback.message.edit_text(
        f"✅ *Кредиты начислены!*\n\nПользователь: {name}\nНачислено: *{amount}* кр.\nНовый баланс: *{updated['credits']}* кр.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Выдать ещё", callback_data="adm_give_credits")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data="adm_back")]
        ])
    )
    try:
        await bot.send_message(target_uid, f"🎁 Тебе начислено *{amount}* кредитов!\nБаланс: *{updated['credits']}* кр.", parse_mode="Markdown")
    except:
        pass
    await callback.answer()

@dp.callback_query(F.data == "adm_credits_history")
async def adm_credits_history(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    history = get_credits_history(15)
    lines = "\n".join([f"• {'@'+u if u else f or str(uid)} +{amt} | {(cr or '')[:10]}" for uid, u, f, amt, _, cr in history]) if history else "Пока нет данных"
    await callback.message.edit_text(
        f"📜 *История начислений*\n\n{lines}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_credits_spent")
async def adm_credits_spent(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    data = get_credits_spent_by_user(10)
    lines = "\n".join([f"{i+1}. {'@'+u if u else f or str(uid)} — {t} кр." for i, (uid, u, f, t) in enumerate(data)]) if data else "Пока нет данных"
    await callback.message.edit_text(
        f"💰 *Расход кредитов по пользователям*\n\n{lines}",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_blocks")
async def adm_blocks(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    user_conversations[f"block_step_{callback.from_user.id}"] = "awaiting_id"
    await callback.message.edit_text(
        "🚫 *Блокировка пользователя*\n\nНапиши @username или user_id:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_do_block_"))
async def adm_do_block(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    target_uid = int(callback.data.replace("adm_do_block_", ""))
    block_user(target_uid)
    await callback.message.edit_text(
        f"🚫 Пользователь {target_uid} заблокирован.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="adm_back")]])
    )
    await callback.answer("Заблокировано", show_alert=True)

@dp.callback_query(F.data.startswith("adm_do_unblock_"))
async def adm_do_unblock(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    target_uid = int(callback.data.replace("adm_do_unblock_", ""))
    unblock_user(target_uid)
    await callback.message.edit_text(
        f"✅ Пользователь {target_uid} разблокирован.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="adm_back")]])
    )
    await callback.answer("Разблокировано", show_alert=True)

@dp.callback_query(F.data == "adm_maintenance")
async def adm_maintenance(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    current = get_maintenance_mode()
    set_maintenance_mode(not current)
    status = "ВКЛЮЧЁН 🔧" if not current else "ВЫКЛЮЧЕН ✅"
    await callback.answer(f"Режим техобслуживания {status}", show_alert=True)
    await callback.message.edit_text("🔧 *Админ панель*\n\nВыбери раздел:", parse_mode="Markdown", reply_markup=admin_keyboard())

@dp.callback_query(F.data == "adm_edit_welcome")
async def adm_edit_welcome(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    user_conversations[f"edit_welcome_{callback.from_user.id}"] = True
    current = get_welcome_message()
    preview = f"\n\nТекущее:\n_{current[:150]}_" if current else ""
    await callback.message.edit_text(
        f"✏️ *Изменить приветствие*{preview}\n\nНапиши новый текст:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    user_conversations[f"broadcast_{callback.from_user.id}"] = True
    await callback.message.edit_text(
        "📣 *Рассылка*\n\nНапиши сообщение для всех пользователей бота:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_back")
async def adm_back(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа", show_alert=True)
        return
    # Очищаем все режимы ввода
    for key in [f"find_user_{callback.from_user.id}", f"block_step_{callback.from_user.id}",
                f"edit_welcome_{callback.from_user.id}", f"broadcast_{callback.from_user.id}",
                f"give_credits_step_{callback.from_user.id}"]:
        user_conversations.pop(key, None)
    await callback.message.edit_text("🔧 *Админ панель*\n\nВыбери раздел:", parse_mode="Markdown", reply_markup=admin_keyboard())
    await callback.answer()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.message()
async def handle_message(message: Message):
    if not message.text:
        return
    user_id = message.from_user.id
    create_user(user_id, message.from_user.username, message.from_user.first_name)

    if is_blocked(user_id):
        return

    if user_id not in ADMIN_IDS and get_maintenance_mode():
        await message.answer("🔧 Бот временно на техобслуживании. Скоро вернёмся!")
        return

    log_message(user_id)

    # ── АДМИН: поиск пользователя ──
    if user_id in ADMIN_IDS and user_conversations.get(f"find_user_{user_id}"):
        user_conversations.pop(f"find_user_{user_id}", None)
        q = message.text.strip()
        found = find_user_by_username(q) if q.startswith("@") else get_user(int(q)) if q.isdigit() else find_user_by_username(q)
        if found:
            name = f"@{found['username']}" if found.get('username') else found.get('first_name') or str(found['user_id'])
            is_bl = found.get('plan') == 'blocked'
            await message.answer(
                f"🔍 *Найден:* {name}\nID: `{found['user_id']}`\nКредиты: *{found['credits']}* кр.\nТариф: {found['plan']}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🎁 Выдать кредиты", callback_data="adm_give_credits")],
                    [InlineKeyboardButton(text="✅ Разблокировать" if is_bl else "🚫 Заблокировать",
                                         callback_data=f"adm_do_unblock_{found['user_id']}" if is_bl else f"adm_do_block_{found['user_id']}")],
                    [InlineKeyboardButton(text="◀️ В меню", callback_data="adm_back")]
                ])
            )
        else:
            await message.answer("❌ Пользователь не найден.")
        return

    # ── АДМИН: блокировка ──
    if user_id in ADMIN_IDS and user_conversations.get(f"block_step_{user_id}") == "awaiting_id":
        user_conversations.pop(f"block_step_{user_id}", None)
        q = message.text.strip()
        found = find_user_by_username(q) if q.startswith("@") else get_user(int(q)) if q.isdigit() else find_user_by_username(q)
        if found:
            name = f"@{found['username']}" if found.get('username') else found.get('first_name') or str(found['user_id'])
            is_bl = found.get('plan') == 'blocked'
            await message.answer(
                f"Пользователь: {name}\nСтатус: {'🚫 Заблокирован' if is_bl else '✅ Активен'}",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"adm_do_block_{found['user_id']}")],
                    [InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"adm_do_unblock_{found['user_id']}")],
                    [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_back")]
                ])
            )
        else:
            await message.answer("❌ Пользователь не найден.")
        return

    # ── АДМИН: изменение приветствия ──
    if user_id in ADMIN_IDS and user_conversations.get(f"edit_welcome_{user_id}"):
        user_conversations.pop(f"edit_welcome_{user_id}", None)
        set_welcome_message(message.text)
        await message.answer("✅ Приветственное сообщение обновлено!", reply_markup=admin_keyboard())
        return

    # ── АДМИН: рассылка ──
    if user_id in ADMIN_IDS and user_conversations.get(f"broadcast_{user_id}"):
        user_conversations.pop(f"broadcast_{user_id}", None)
        all_ids = get_all_user_ids()
        sent, failed = 0, 0
        for uid in all_ids:
            try:
                await bot.send_message(uid, message.text, parse_mode="Markdown")
                sent += 1
                await asyncio.sleep(0.05)
            except:
                failed += 1
        await message.answer(f"📣 *Рассылка завершена*\n\n✅ Отправлено: {sent}\n❌ Не доставлено: {failed}", parse_mode="Markdown", reply_markup=admin_keyboard())
        return

    # ── АДМИН: выдача кредитов ──
    if user_id in ADMIN_IDS and user_conversations.get(f"give_credits_step_{user_id}") == "awaiting_user_id":
        try:
            target_uid = int(message.text.strip())
            target_user = get_user(target_uid)
            if not target_user:
                await message.answer("❌ Пользователь не найден.")
                return
            user_conversations[f"give_credits_step_{user_id}"] = None
            name = f"@{target_user['username']}" if target_user.get('username') else target_user.get('first_name') or str(target_uid)
            await message.answer(
                f"🎁 *Шаг 2 из 2*\n\nПользователь: {name}\nБаланс: *{target_user['credits']}* кр.\n\nСколько кредитов выдать?",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="10", callback_data=f"adm_amount_{target_uid}_10"),
                     InlineKeyboardButton(text="20", callback_data=f"adm_amount_{target_uid}_20"),
                     InlineKeyboardButton(text="30", callback_data=f"adm_amount_{target_uid}_30")],
                    [InlineKeyboardButton(text="50", callback_data=f"adm_amount_{target_uid}_50"),
                     InlineKeyboardButton(text="100", callback_data=f"adm_amount_{target_uid}_100"),
                     InlineKeyboardButton(text="200", callback_data=f"adm_amount_{target_uid}_200")],
                    [InlineKeyboardButton(text="300", callback_data=f"adm_amount_{target_uid}_300"),
                     InlineKeyboardButton(text="500", callback_data=f"adm_amount_{target_uid}_500"),
                     InlineKeyboardButton(text="1000", callback_data=f"adm_amount_{target_uid}_1000")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_credits")]
                ])
            )
        except ValueError:
            await message.answer("❌ Введи числовой user_id.")
        return

    # ── ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЯ ──
    if user_conversations.get(f"gen_image_{user_id}"):
        model_key = user_conversations.pop(f"gen_image_{user_id}")
        model_info = IMAGE_MODELS[model_key]
        user = get_user(user_id)
        if not user or user["credits"] < model_info["credits"]:
            await message.answer("❌ Недостаточно кредитов.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")]]))
            return
        status_msg = await message.answer(f"🎨 Генерирую изображение через *{model_info['name']}*...", parse_mode="Markdown")
        await bot.send_chat_action(message.chat.id, "upload_photo")
        image_bytes = await generate_image(message.text, model_key)
        if image_bytes:
            if spend_credits(user_id, model_info["credits"], f"image_{model_key}"):
                updated = get_user(user_id)
                await bot.delete_message(message.chat.id, status_msg.message_id)
                await bot.send_photo(
                    message.chat.id,
                    BufferedInputFile(image_bytes, "image.png"),
                    caption=f"🎨 *{model_info['name']}*\nСписано: {model_info['credits']} кр. | Баланс: {updated['credits']} кр.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎨 Ещё изображение", callback_data="choose_image_model")],
                        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]
                    ])
                )
        else:
            await status_msg.edit_text("❌ Не удалось сгенерировать. Попробуй другой промт или модель.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="choose_image_model")]]))
        return

    # ── ГЕНЕРАЦИЯ ВИДЕО ──
    if user_conversations.get(f"gen_video_{user_id}"):
        model_key = user_conversations.pop(f"gen_video_{user_id}")
        model_info = VIDEO_MODELS[model_key]
        user = get_user(user_id)
        if not user or user["credits"] < model_info["credits"]:
            await message.answer("❌ Недостаточно кредитов.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")]]))
            return
        status_msg = await message.answer(f"🎬 Генерирую видео через *{model_info['name']}*...\n\n⏳ Это займёт 1-3 минуты, жди!", parse_mode="Markdown")
        await bot.send_chat_action(message.chat.id, "record_video")
        video_bytes = await generate_video(message.text, model_key)
        if video_bytes:
            if spend_credits(user_id, model_info["credits"], f"video_{model_key}"):
                updated = get_user(user_id)
                await bot.delete_message(message.chat.id, status_msg.message_id)
                await bot.send_video(
                    message.chat.id,
                    BufferedInputFile(video_bytes, "video.mp4"),
                    caption=f"🎬 *{model_info['name']}*\nСписано: {model_info['credits']} кр. | Баланс: {updated['credits']} кр.",
                    parse_mode="Markdown",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎬 Ещё видео", callback_data="choose_video_model")],
                        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]
                    ])
                )
        else:
            await status_msg.edit_text("❌ Не удалось создать видео. Попробуй другой промт.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="choose_video_model")]]))
        return

    # ── ОБЫЧНАЯ КОНСУЛЬТАЦИЯ ──
    text_lower = message.text.lower()
    for kw in ["chatgpt", "claude", "grok", "midjourney", "cursor", "perplexity", "krea", "suno", "kling", "runway", "heygen", "higgsfield", "lovable", "gamma", "zoom", "vpn"]:
        if kw in text_lower:
            log_question(user_id, kw)
            break

    if user_id not in user_conversations:
        user_conversations[user_id] = []
    user_conversations[user_id].append({"role": "user", "content": message.text})
    if len(user_conversations[user_id]) > 20:
        user_conversations[user_id] = user_conversations[user_id][-20:]

    await bot.send_chat_action(message.chat.id, "typing")
    response = await get_claude_response(user_conversations[user_id])
    if response:
        user_conversations[user_id].append({"role": "assistant", "content": response})
        await message.answer(response, parse_mode="Markdown", reply_markup=contact_keyboard())
    else:
        await message.answer("Что-то пошло не так 😅", reply_markup=contact_keyboard())

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ЗАПУСК
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def main():
    init_db()
    logging.info("Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
