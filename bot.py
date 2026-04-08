import asyncio
import logging
import json
import os
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, ChatMemberUpdated, InlineKeyboardMarkup,
    InlineKeyboardButton, CallbackQuery, LabeledPrice, PreCheckoutQuery
)
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION, Command
import anthropic
from dotenv import load_dotenv

from database import (
    init_db, get_user, create_user, add_credits,
    spend_credits, set_plan, get_stats, get_all_users
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "AleksandrOii")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

user_conversations = {}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Загрузка базы знаний
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def load_knowledge() -> str:
    try:
        with open("knowledge.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        
        text = "АКТУАЛЬНАЯ БАЗА ЗНАНИЙ (используй ТОЛЬКО эти данные для цен и тарифов):\n\n"
        
        for key, service in data["services"].items():
            text += f"{service['emoji']} {service['name']}\n"
            text += f"Описание: {service['description']}\n"
            text += "Тарифы:\n"
            for plan_key, plan in service["plans"].items():
                price_info = ""
                if plan.get("price_usd"):
                    price_info = f"${plan['price_usd']}/мес (≈{plan.get('price_rub', '?')} ₽ / {plan.get('price_kzt', '?')} ₸)"
                elif plan.get("price") == 0:
                    price_info = "Бесплатно"
                features = ", ".join(plan.get("features", []))
                text += f"  • {plan['name']}: {price_info} — {features}\n"
            text += f"Лучше всего для: {', '.join(service.get('best_for', []))}\n\n"
        
        vpn = data.get("vpn_info", {})
        text += f"\n🔒 VPN INFO:\n{vpn.get('description', '')}\n"
        for rec in vpn.get("recommendations", []):
            text += f"  • {rec['name']} ({rec['price']}): {rec['note']}\n"
        
        how = data.get("how_to_buy", {})
        text += "\n💳 КАК КУПИТЬ ПОДПИСКУ ЧЕРЕЗ АЛЕКСАНДРА:\n"
        for i, step in enumerate(how.get("steps", []), 1):
            text += f"  {i}. {step}\n"
        text += "Преимущества: " + " | ".join(how.get("advantages", [])) + "\n"
        
        return text
    except Exception as e:
        logging.error(f"Ошибка загрузки knowledge.json: {e}")
        return ""

KNOWLEDGE_BASE = load_knowledge()

SYSTEM_PROMPT = f"""Ты — AI-консультант Александра, эксперта по нейросетям и AI-инструментам.
Канал: @AleksandrOii | Telegram: t.me/AleksandrOii

Твой характер: дружелюбный, экспертный, без лишней воды. Отвечаешь по делу, но тепло.
Используй эмодзи умеренно. Пиши на русском языке.

{KNOWLEDGE_BASE}

━━━━━━━━━━━━━━━━━━━━━━
ПРАВИЛА ОТВЕТОВ:
━━━━━━━━━━━━━━━━━━━━━━
1. Цены и тарифы — ТОЛЬКО из базы знаний выше. Никогда не придумывай цены.
2. Если спрашивают про конкретный сервис — расскажи что он даёт, тарифы, отличия.
3. Если клиент не знает что выбрать — задай уточняющий вопрос: для чего нужна нейросеть?
4. Для оформления подписки ВСЕГДА направляй к Александру: @AleksandrOii
5. По VPN — давай конкретные советы из базы знаний.
6. Если не знаешь ответа — честно скажи и предложи спросить Александра напрямую.
7. Никогда не обещай то, чего нет в базе знаний.
"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Тарифные планы бота
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOT_PLANS = {
    "starter": {
        "name": "⚡ Старт",
        "price_stars": 150,
        "price_rub": 299,
        "credits": 50,
        "days": 0,
        "description": "50 кредитов (разово)"
    },
    "basic": {
        "name": "🔥 Базовый",
        "price_stars": 350,
        "price_rub": 699,
        "credits": 200,
        "days": 30,
        "description": "200 кредитов на 30 дней"
    },
    "pro": {
        "name": "💎 Про",
        "price_stars": 750,
        "price_rub": 1490,
        "credits": 600,
        "days": 30,
        "description": "600 кредитов на 30 дней"
    }
}

CREDIT_COSTS = {
    "image": 5,        # 1 изображение = 5 кредитов
    "consultation": 0  # Консультация бесплатно
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Клавиатуры
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Помочь с выбором", callback_data="help_choose"),
         InlineKeyboardButton(text="💳 Тарифы и цены", callback_data="show_prices")],
        [InlineKeyboardButton(text="🎨 Генерация изображений", callback_data="gen_image"),
         InlineKeyboardButton(text="🔒 VPN для РФ", callback_data="vpn_help")],
        [InlineKeyboardButton(text="💰 Мой баланс", callback_data="my_balance"),
         InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")],
        [InlineKeyboardButton(text="✍️ Написать Александру", url=f"https://t.me/{ADMIN_USERNAME}")]
    ])

def contact_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать Александру", url=f"https://t.me/{ADMIN_USERNAME}")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])

def plans_keyboard():
    buttons = []
    for plan_id, plan in BOT_PLANS.items():
        buttons.append([InlineKeyboardButton(
            text=f"{plan['name']} — {plan['price_rub']} ₽ ({plan['credits']} кр.)",
            callback_data=f"buy_plan_{plan_id}"
        )])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def services_keyboard():
    services = [
        ("🤖 ChatGPT", "info_chatgpt"), ("🧠 Claude", "info_claude"),
        ("⚡ Grok", "info_grok"), ("🎨 Midjourney", "info_midjourney"),
        ("💻 Cursor", "info_cursor"), ("🔍 Perplexity", "info_perplexity"),
        ("✨ Krea AI", "info_krea"), ("🎵 Suno", "info_suno"),
    ]
    rows = [[InlineKeyboardButton(text=s[0], callback_data=s[1]) for s in services[i:i+2]]
            for i in range(0, len(services), 2)]
    rows.append([InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Приветствие новых подписчиков
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated):
    if str(event.chat.id) != str(CHANNEL_ID):
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return
    create_user(user.id, user.username, user.first_name)
    welcome = (
        f"👋 Привет, {user.first_name}! Рад видеть тебя в канале!\n\n"
        "Я — AI-ассистент Александра. Помогу тебе:\n"
        "🤖 Разобраться в нейросетях\n"
        "💡 Выбрать подходящий сервис\n"
        "💳 Узнать актуальные цены и тарифы\n"
        "🎨 Сгенерировать изображения\n"
        "🔒 Разобраться с VPN\n\n"
        "Просто напиши мне или выбери ниже 👇"
    )
    try:
        await bot.send_message(user.id, welcome, reply_markup=main_keyboard())
    except Exception as e:
        logging.warning(f"Не удалось отправить {user.id}: {e}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Команды
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.message(Command("start"))
async def cmd_start(message: Message):
    user = message.from_user
    create_user(user.id, user.username, user.first_name)
    await message.answer(
        f"👋 Привет, {user.first_name}!\n\n"
        "Я AI-консультант по нейросетям. Помогу выбрать сервис, расскажу о тарифах и отвечу на любые вопросы об AI-инструментах.\n\n"
        "Выбери что тебя интересует 👇",
        reply_markup=main_keyboard()
    )

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    await show_balance(message.from_user.id, message)

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    await show_admin_panel(message)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Callback обработчики
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "Главное меню — выбери что тебя интересует 👇",
        reply_markup=main_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "help_choose")
async def cb_help_choose(callback: CallbackQuery):
    await callback.message.edit_text(
        "Расскажи — для каких задач нужна нейросеть?\n\n"
        "Например:\n"
        "• Писать тексты / посты / статьи\n"
        "• Генерировать картинки\n"
        "• Помощь с кодом\n"
        "• Анализ документов\n"
        "• Создание видео / музыки\n"
        "• Поиск информации в интернете\n\n"
        "Или выбери интересующий сервис 👇",
        reply_markup=services_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "show_prices")
async def cb_show_prices(callback: CallbackQuery):
    await callback.message.edit_text(
        "📋 Выбери сервис, чтобы узнать актуальные тарифы:",
        reply_markup=services_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("info_"))
async def cb_service_info(callback: CallbackQuery):
    service_key = callback.data.replace("info_", "")
    try:
        with open("knowledge.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        service = data["services"].get(service_key)
        if not service:
            await callback.answer("Сервис не найден")
            return
        
        text = f"{service['emoji']} *{service['name']}*\n\n"
        text += f"{service['description']}\n\n"
        text += "📊 *Тарифы:*\n"
        for plan in service["plans"].values():
            price_info = ""
            if plan.get("price_usd"):
                price_info = f"${plan['price_usd']}/мес (≈{plan.get('price_rub', '?')} ₽)"
            elif plan.get("price") == 0:
                price_info = "Бесплатно"
            text += f"\n• *{plan['name']}* — {price_info}\n"
            for feat in plan.get("features", []):
                text += f"  ✓ {feat}\n"
        
        text += f"\n✅ *Лучше всего для:* {', '.join(service.get('best_for', []))}"
        text += f"\n\n💳 Оформить: @{ADMIN_USERNAME}"
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"Купить {service['name']}", url=f"https://t.me/{ADMIN_USERNAME}")],
            [InlineKeyboardButton(text="◀️ К списку сервисов", callback_data="show_prices")]
        ])
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception as e:
        logging.error(f"Ошибка info: {e}")
    await callback.answer()

@dp.callback_query(F.data == "vpn_help")
async def cb_vpn(callback: CallbackQuery):
    try:
        with open("knowledge.json", "r", encoding="utf-8") as f:
            data = json.load(f)
        vpn = data["vpn_info"]
        text = f"🔒 *VPN для России*\n\n{vpn['description']}\n\n*Рекомендации:*\n"
        for rec in vpn["recommendations"]:
            text += f"\n• *{rec['name']}* ({rec['price']})\n  {rec['note']}\n"
        text += f"\n\n{vpn['note']}"
        await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=contact_keyboard())
    except Exception as e:
        logging.error(f"Ошибка vpn: {e}")
    await callback.answer()

@dp.callback_query(F.data == "my_balance")
async def cb_balance(callback: CallbackQuery):
    await show_balance(callback.from_user.id, callback.message, edit=True)
    await callback.answer()

@dp.callback_query(F.data == "buy_credits")
async def cb_buy_credits(callback: CallbackQuery):
    text = (
        "💰 *Тарифы для генерации изображений*\n\n"
        "Кредиты используются для генерации изображений.\n"
        "Консультации — *всегда бесплатно* 🆓\n\n"
        "1 изображение = 5 кредитов\n\n"
    )
    for plan_id, plan in BOT_PLANS.items():
        text += f"{plan['name']}\n"
        text += f"  💳 {plan['price_rub']} ₽ | {plan['credits']} кредитов\n"
        text += f"  {plan['description']}\n\n"
    
    text += "👇 Выбери тариф для оплаты через Telegram Stars"
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=plans_keyboard())
    await callback.answer()

@dp.callback_query(F.data.startswith("buy_plan_"))
async def cb_buy_plan(callback: CallbackQuery):
    plan_id = callback.data.replace("buy_plan_", "")
    plan = BOT_PLANS.get(plan_id)
    if not plan:
        await callback.answer("Тариф не найден")
        return
    
    # Оплата через Telegram Stars
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=plan["name"],
        description=plan["description"],
        payload=f"plan_{plan_id}_{callback.from_user.id}",
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(label=plan["name"], amount=plan["price_stars"])],
        provider_token=""
    )
    await callback.answer()

@dp.callback_query(F.data == "gen_image")
async def cb_gen_image(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = get_user(user_id)
    if not user:
        create_user(user_id, callback.from_user.username, callback.from_user.first_name)
        user = get_user(user_id)
    
    credits = user["credits"] if user else 0
    cost = CREDIT_COSTS["image"]
    
    if credits < cost:
        await callback.message.edit_text(
            f"❌ Недостаточно кредитов\n\n"
            f"У тебя: {credits} кр.\n"
            f"Нужно: {cost} кр. (1 изображение)\n\n"
            f"Купи кредиты, чтобы начать генерацию 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")]
            ])
        )
        await callback.answer()
        return
    
    await callback.message.edit_text(
        f"🎨 *Генерация изображений*\n\n"
        f"Баланс: {credits} кр. | Стоимость: {cost} кр. за изображение\n\n"
        f"Опиши что хочешь сгенерировать — напиши описание на русском или английском.\n\n"
        f"Пример: _портрет девушки в стиле аниме, синие волосы, закат_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Отмена", callback_data="main_menu")]
        ])
    )
    user_conversations[user_id] = user_conversations.get(user_id, [])
    user_conversations[f"mode_{user_id}"] = "image_gen"
    await callback.answer()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Оплата
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    payload = message.successful_payment.invoice_payload
    parts = payload.split("_")
    plan_id = parts[1]
    plan = BOT_PLANS.get(plan_id)
    
    if plan:
        user_id = message.from_user.id
        expires = (datetime.now() + timedelta(days=plan["days"])).isoformat() if plan["days"] > 0 else None
        set_plan(user_id, plan_id, expires, plan["credits"])
        add_credits(user_id, plan["credits"], f"Покупка тарифа {plan['name']}")
        
        await message.answer(
            f"✅ *Оплата прошла успешно!*\n\n"
            f"Тариф: {plan['name']}\n"
            f"Начислено: {plan['credits']} кредитов\n\n"
            f"Можешь начать генерировать изображения! 🎨",
            parse_mode="Markdown",
            reply_markup=main_keyboard()
        )
        logging.info(f"Оплата: user {user_id}, plan {plan_id}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Вспомогательные функции
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def show_balance(user_id: int, message, edit=False):
    user = get_user(user_id)
    if not user:
        create_user(user_id, "", "")
        user = get_user(user_id)
    
    plan_name = {"free": "Бесплатный", "starter": "⚡ Старт", "basic": "🔥 Базовый", "pro": "💎 Про"}.get(user["plan"], user["plan"])
    expires = ""
    if user["plan_expires"]:
        try:
            exp = datetime.fromisoformat(user["plan_expires"])
            expires = f"\nДействует до: {exp.strftime('%d.%m.%Y')}"
        except:
            pass
    
    text = (
        f"💰 *Мой баланс*\n\n"
        f"Тариф: {plan_name}{expires}\n"
        f"Кредиты: {user['credits']} кр.\n\n"
        f"📊 Стоимость генерации:\n"
        f"• 1 изображение = {CREDIT_COSTS['image']} кр.\n"
        f"• Консультация = бесплатно 🆓"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🛒 Пополнить кредиты", callback_data="buy_credits")],
        [InlineKeyboardButton(text="◀️ Главное меню", callback_data="main_menu")]
    ])
    
    if edit:
        await message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="Markdown", reply_markup=kb)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Генерация изображений (заглушка — подключи Google Imagen API)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def generate_image(prompt: str, user_id: int) -> str | None:
    """
    Здесь подключаем Google Imagen API или другой.
    Пока возвращаем None — подключи свой API ключ.
    
    Пример для Google Imagen:
    from google.cloud import aiplatform
    ...
    """
    # TODO: Подключить Google Imagen 3 API
    # Временная заглушка
    return None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Основной обработчик сообщений
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.message()
async def handle_message(message: Message):
    if not message.text:
        return
    
    user_id = message.from_user.id
    create_user(user_id, message.from_user.username, message.from_user.first_name)
    
    # Режим генерации изображений
    if user_conversations.get(f"mode_{user_id}") == "image_gen":
        user_conversations[f"mode_{user_id}"] = None
        
        user = get_user(user_id)
        cost = CREDIT_COSTS["image"]
        
        if not user or user["credits"] < cost:
            await message.answer(
                "❌ Недостаточно кредитов. Купи кредиты для генерации.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")]
                ])
            )
            return
        
        await bot.send_chat_action(message.chat.id, "upload_photo")
        
        # Пытаемся сгенерировать
        image_url = await generate_image(message.text, user_id)
        
        if image_url:
            if spend_credits(user_id, cost, "image_generation"):
                await bot.send_photo(message.chat.id, image_url,
                    caption=f"🎨 Готово! Списано {cost} кредитов.\nОстаток: {user['credits'] - cost} кр.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎨 Ещё изображение", callback_data="gen_image")],
                        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
                    ])
                )
        else:
            await message.answer(
                "⚙️ Генерация изображений скоро будет доступна!\n\n"
                "Пока обратись к Александру — он поможет с генерацией 🎨",
                reply_markup=contact_keyboard()
            )
        return
    
    # Обычная консультация через Claude
    if user_id not in user_conversations:
        user_conversations[user_id] = []
    
    user_conversations[user_id].append({"role": "user", "content": message.text})
    
    if len(user_conversations[user_id]) > 20:
        user_conversations[user_id] = user_conversations[user_id][-20:]
    
    await bot.send_chat_action(message.chat.id, "typing")
    
    try:
        response = claude_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=user_conversations[user_id]
        )
        reply_text = response.content[0].text
        user_conversations[user_id].append({"role": "assistant", "content": reply_text})
        await message.answer(reply_text, reply_markup=contact_keyboard())
    except Exception as e:
        logging.error(f"Ошибка Claude API: {e}")
        await message.answer(
            "Что-то пошло не так 😅 Напиши Александру напрямую.",
            reply_markup=contact_keyboard()
        )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Админ панель
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def show_admin_panel(message: Message):
    stats = get_stats()
    text = (
        "🔧 *Админ панель*\n\n"
        f"👥 Всего пользователей: {stats['total_users']}\n"
        f"💳 Платных пользователей: {stats['paid_users']}\n"
        f"⚡ Потрачено кредитов: {stats['total_credits_spent']}\n"
        f"🎨 Генераций изображений: {stats['total_images']}\n\n"
        "Команды:\n"
        "`/add_credits [user_id] [amount]` — добавить кредиты\n"
        "`/users` — список пользователей"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("add_credits"))
async def cmd_add_credits(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    try:
        parts = message.text.split()
        user_id = int(parts[1])
        amount = int(parts[2])
        add_credits(user_id, amount, f"Ручное начисление от админа")
        await message.answer(f"✅ Начислено {amount} кредитов пользователю {user_id}")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}\nФормат: /add_credits [user_id] [amount]")

@dp.message(Command("users"))
async def cmd_users(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    users = get_all_users()
    text = "👥 *Последние пользователи:*\n\n"
    for u in users[:20]:
        user_id, username, first_name, credits, plan, joined = u
        uname = f"@{username}" if username else first_name
        text += f"• {uname} | {credits} кр. | {plan}\n"
    await message.answer(text, parse_mode="Markdown")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Запуск
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def main():
    init_db()
    logging.info("✅ Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
