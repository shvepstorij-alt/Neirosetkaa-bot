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
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "AleksandrOii")       # канал
PERSONAL_USERNAME = os.getenv("PERSONAL_USERNAME", "AleksandrOii") # личный аккаунт для покупки
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

user_conversations = {}

MY_PRICES = """
ЦЕНЫ (только эти называй клиентам, в рублях):

ChatGPT Plus — 2000 руб/мес
Claude Pro — 2000 руб/мес
SuperGrok — 2000 руб/мес
Gamma Basic — 1200 руб/мес
Gamma Pro — 2300 руб/мес
Midjourney Basic — 1000 руб/мес
Midjourney Standard — 3000 руб/мес
Cursor Pro — 2300 руб/мес
Kling AI Standard — 1000 руб/мес
Kling AI Pro — 2700 руб/мес
Perplexity Pro — 2000 руб/мес
HeyGen Creator — 3000 руб/мес
Higgsfield Starter — 1700 руб/мес
Higgsfield Plus — 5100 руб/мес
Runway Standard — 1700 руб/мес
Runway Pro — 3700 руб/мес
Krea Basic — 1000 руб/мес
Krea Pro — 3200 руб/мес
Lovable Pro — 2700 руб/мес
Suno Pro — 1000 руб/мес
Suno Premier — 3000 руб/мес
Zoom Pro — 2000 руб/мес
Zoom Business — 2300 руб/мес

Оплата только в рублях. Без иностранных карт. Оформление за 5-15 минут.
"""

SYSTEM_PROMPT = f"""Ты — AI-консультант по нейросетям и AI-инструментам.

Характер: дружелюбный, экспертный, по делу. Пишешь на русском.
ФОРМАТИРОВАНИЕ: только эмодзи умеренно. НИКОГДА не используй markdown: никаких **, *, ##, ___ и подобных символов. Пиши простым текстом.

ЦЕНЫ (только эти называй):
{MY_PRICES}

ПРАВИЛА:
1. Цены — ТОЛЬКО из списка выше. Никогда не называй официальные $ цены как итоговую стоимость.
2. ВСЕГДА используй web_search перед ответом о возможностях любого сервиса — никогда не отвечай по памяти, информация устаревает быстро.
3. Если клиент не знает что выбрать — спроси: для каких задач нужна нейросеть?
4. Для покупки направляй: @AleksandrOii (личный аккаунт, не канал).
5. Упоминай про оформление подписки естественно, только когда это уместно — в конце ответа или когда клиент сам спрашивает про покупку. Не навязывай.
6. Оплата только в рублях. Оформление за 5-15 минут.
7. По VPN — помогай советами, рекомендуй бесплатные варианты (Outline, Lantern, Windscribe).
8. СТРОГО ЗАПРЕЩЕНО отвечать на темы: политика, экономика, новости, отношения, медицина, юриспруденция и всё что НЕ связано с AI-инструментами, подписками и VPN.
7. На запрещённые темы отвечай: "Я консультирую только по нейросетям и AI-инструментам 🤖 Спроси меня про это!"
8. Никогда не придумывай цены — только из списка выше.
"""

BOT_PLANS = {
    "starter": {"name": "Старт", "price_stars": 150, "price_rub": 299, "credits": 50, "days": 0, "description": "50 кредитов (разово) — 10 изображений"},
    "basic": {"name": "Базовый", "price_stars": 350, "price_rub": 699, "credits": 200, "days": 30, "description": "200 кредитов на 30 дней — 40 изображений"},
    "pro": {"name": "Про", "price_stars": 750, "price_rub": 1490, "credits": 600, "days": 30, "description": "600 кредитов на 30 дней — 120 изображений"}
}

CREDIT_COSTS = {"image": 5, "consultation": 0}

def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Помочь с выбором", callback_data="help_choose"),
         InlineKeyboardButton(text="💳 Все цены", callback_data="show_prices")],
        [InlineKeyboardButton(text="🎨 Генерация изображений", callback_data="gen_image"),
         InlineKeyboardButton(text="🔒 VPN для РФ", callback_data="vpn_help")],
        [InlineKeyboardButton(text="💰 Мой баланс", callback_data="my_balance"),
         InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")],
        [InlineKeyboardButton(text="✍️ Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}")]
    ])

def contact_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать Александру", url=f"https://t.me/{PERSONAL_USERNAME}")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])

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

@dp.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated):
    if str(event.chat.id) != str(CHANNEL_ID):
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return
    create_user(user.id, user.username, user.first_name)
    try:
        await bot.send_message(user.id,
            f"👋 Привет, {user.first_name}! Рад видеть тебя в канале!\n\n"
            "Я — AI-консультант Александра 🤖\n\n"
            "Помогу тебе:\n"
            "🔍 Выбрать нужную нейросеть\n"
            "💳 Узнать цены и оформить подписку без VPN\n"
            "🎨 Сгенерировать изображения\n"
            "🔒 Разобраться с VPN для России\n\n"
            "Просто напиши мне или выбери ниже 👇",
            reply_markup=main_keyboard()
        )
    except Exception as e:
        logging.warning(f"Не удалось отправить {user.id}: {e}")

@dp.message(Command("start"))
async def cmd_start(message: Message):
    create_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    await message.answer(
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "Я AI-консультант по нейросетям.\n\n"
        "Отвечу на любые вопросы об AI-инструментах и помогу выбрать нужный сервис 🚀\n\n"
        "Выбери что тебя интересует 👇",
        reply_markup=main_keyboard()
    )

@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    stats = get_stats()
    await message.answer(
        "🔧 *Админ панель*\n\n"
        f"👥 Всего пользователей: {stats['total_users']}\n"
        f"💳 Платных: {stats['paid_users']}\n"
        f"⚡ Потрачено кредитов: {stats['total_credits_spent']}\n"
        f"🎨 Генераций: {stats['total_images']}\n\n"
        "*Команды:*\n"
        "`/add_credits [user_id] [amount]`\n"
        "`/users` — список пользователей",
        parse_mode="Markdown"
    )

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

@dp.message(Command("users"))
async def cmd_users(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    users = get_all_users()
    text = "👥 *Пользователи:*\n\n"
    for u in users[:20]:
        uid, uname, fname, credits, plan, joined = u
        name = f"@{uname}" if uname else fname or str(uid)
        text += f"• {name} | {credits} кр. | {plan}\n"
    await message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню 👇", reply_markup=main_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "help_choose")
async def cb_help_choose(callback: CallbackQuery):
    await callback.message.edit_text(
        "Расскажи — для каких задач нужна нейросеть? 🤔\n\n"
        "Например:\n• Тексты / посты / статьи\n• Изображения или видео\n"
        "• Код / разработка\n• Анализ документов\n• Музыка\n"
        "• Поиск информации\n• Видео-аватар / озвучка\n\n"
        "Опиши своими словами — подберу лучший вариант 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Все сервисы и цены", callback_data="show_prices")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "show_prices")
async def cb_show_prices(callback: CallbackQuery):
    await callback.message.edit_text(
        "💳 *Цены Александра (₽/мес):*\n\n"
        "🤖 ChatGPT Plus — 2000 ₽\n🧠 Claude Pro — 2000 ₽\n⚡ SuperGrok — 2000 ₽\n"
        "📊 Gamma Basic — 1200 ₽\n📊 Gamma Pro — 2300 ₽\n"
        "🎨 Midjourney Basic — 1000 ₽\n🎨 Midjourney Standard — 3000 ₽\n"
        "💻 Cursor Pro — 2300 ₽\n🎬 Kling AI Standard — 1000 ₽\n🎬 Kling AI Pro — 2700 ₽\n"
        "🔍 Perplexity Pro — 2000 ₽\n🎥 HeyGen Creator — 3000 ₽\n"
        "🎬 Higgsfield Starter — 1700 ₽\n🎬 Higgsfield Plus — 5100 ₽\n"
        "🎥 Runway Standard — 1700 ₽\n🎥 Runway Pro — 3700 ₽\n"
        "✨ Krea Basic — 1000 ₽\n✨ Krea Pro — 3200 ₽\n"
        "🌐 Lovable Pro — 2700 ₽\n🎵 Suno Pro — 1000 ₽\n🎵 Suno Premier — 3000 ₽\n"
        "📹 Zoom Pro — 2000 ₽\n📹 Zoom Business — 2300 ₽\n\n"
        "✅ Оплата в рублях | ✅ Без VPN | ✅ Оформление за 5–15 мин",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Купить подписку", url=f"https://t.me/{PERSONAL_USERNAME}")],
            [InlineKeyboardButton(text="🔍 Подробнее о сервисах", callback_data="show_services")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "show_services")
async def cb_show_services(callback: CallbackQuery):
    await callback.message.edit_text("Выбери сервис для подробной информации 👇", reply_markup=services_keyboard())
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
    await callback.message.edit_text(f"🔍 Ищу актуальную информацию о {name}...")

    messages = [{"role": "user", "content": f"Расскажи подробно что умеет {name}, для каких задач подходит, какие тарифы у Александра. Найди актуальную информацию."}]
    response = await get_claude_response(messages)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🛒 Купить {name}", url=f"https://t.me/{PERSONAL_USERNAME}")],
        [InlineKeyboardButton(text="◀️ К списку", callback_data="show_services")]
    ])
    await callback.message.edit_text(response or f"Не удалось загрузить. Напиши @{PERSONAL_USERNAME}", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "vpn_help")
async def cb_vpn(callback: CallbackQuery):
    await callback.message.edit_text(
        "🔒 *VPN для России*\n\n"
        "Для доступа к AI-сервисам из РФ нужен VPN.\n\n"
        "*Бесплатные:*\n• Outline VPN — лучший выбор\n• Lantern — работает без настроек\n• Windscribe — 10 GB/мес\n\n"
        "*Платные:*\n• ExpressVPN — самый быстрый\n• NordVPN — много серверов\n\n"
        "💡 При покупке через Александра VPN не нужен — он всё оформляет сам!\n\n"
        "Вопросы по настройке — пиши Александру 👇",
        parse_mode="Markdown",
        reply_markup=contact_keyboard()
    )
    await callback.answer()

@dp.callback_query(F.data == "my_balance")
async def cb_balance(callback: CallbackQuery):
    user = get_user(callback.from_user.id)
    if not user:
        create_user(callback.from_user.id, callback.from_user.username, callback.from_user.first_name)
        user = get_user(callback.from_user.id)
    plan_labels = {"free": "Бесплатный", "starter": "⚡ Старт", "basic": "🔥 Базовый", "pro": "💎 Про"}
    await callback.message.edit_text(
        f"💰 *Мой баланс*\n\n"
        f"Тариф: {plan_labels.get(user['plan'], user['plan'])}\n"
        f"Кредиты: {user['credits']} кр.\n\n"
        f"• 1 изображение = {CREDIT_COSTS['image']} кр.\n• Консультация = бесплатно 🆓",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Пополнить", callback_data="buy_credits")],
            [InlineKeyboardButton(text="◀️ Меню", callback_data="main_menu")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "buy_credits")
async def cb_buy_credits(callback: CallbackQuery):
    text = "💰 *Кредиты для генерации изображений*\n\nКонсультации — бесплатно 🆓\n1 изображение = 5 кредитов\n\n"
    for p in BOT_PLANS.values():
        text += f"{p['name']} — {p['price_rub']} ₽ | {p['description']}\n\n"
    text += "👇 Выбери тариф:"
    buttons = [[InlineKeyboardButton(text=f"{p['name']} — {p['price_rub']} ₽ ({p['credits']} кр.)", callback_data=f"buy_plan_{pid}")] for pid, p in BOT_PLANS.items()]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("buy_plan_"))
async def cb_buy_plan(callback: CallbackQuery):
    plan = BOT_PLANS.get(callback.data.replace("buy_plan_", ""))
    if not plan:
        await callback.answer("Тариф не найден")
        return
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=plan["name"], description=plan["description"],
        payload=f"plan_{callback.data.replace('buy_plan_', '')}_{callback.from_user.id}",
        currency="XTR",
        prices=[LabeledPrice(label=plan["name"], amount=plan["price_stars"])],
        provider_token=""
    )
    await callback.answer()

@dp.callback_query(F.data == "gen_image")
async def cb_gen_image(callback: CallbackQuery):
    user_id = callback.from_user.id
    user = get_user(user_id) or (create_user(user_id, callback.from_user.username, callback.from_user.first_name) or get_user(user_id))
    credits = user["credits"] if user else 0
    if credits < CREDIT_COSTS["image"]:
        await callback.message.edit_text(
            f"❌ Недостаточно кредитов\n\nУ тебя: {credits} кр. | Нужно: {CREDIT_COSTS['image']} кр.\n\nКупи кредиты 👇",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")]
            ])
        )
    else:
        user_conversations[f"mode_{user_id}"] = "image_gen"
        await callback.message.edit_text(
            f"🎨 *Генерация изображений*\n\nБаланс: {credits} кр. | Стоимость: {CREDIT_COSTS['image']} кр./изображение\n\nОпиши что хочешь сгенерировать 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="main_menu")]])
        )
    await callback.answer()

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def successful_payment(message: Message):
    parts = message.successful_payment.invoice_payload.split("_")
    plan_id = parts[1]
    plan = BOT_PLANS.get(plan_id)
    if plan:
        expires = (datetime.now() + timedelta(days=plan["days"])).isoformat() if plan["days"] > 0 else None
        set_plan(message.from_user.id, plan_id, expires, plan["credits"])
        add_credits(message.from_user.id, plan["credits"], f"Покупка {plan['name']}")
        await message.answer(
            f"✅ *Оплата прошла!*\n\nТариф: {plan['name']}\nНачислено: {plan['credits']} кредитов 🎉",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

async def generate_image(prompt: str, user_id: int):
    # TODO: Подключи Google Imagen 3
    # import google.genai as genai
    # client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    # response = client.models.generate_image(model="imagen-3.0-generate-002", prompt=prompt)
    # return response.generated_images[0].image.image_bytes
    return None

@dp.message()
async def handle_message(message: Message):
    if not message.text:
        return
    user_id = message.from_user.id
    create_user(user_id, message.from_user.username, message.from_user.first_name)

    if user_conversations.get(f"mode_{user_id}") == "image_gen":
        user_conversations[f"mode_{user_id}"] = None
        user = get_user(user_id)
        if not user or user["credits"] < CREDIT_COSTS["image"]:
            await message.answer("❌ Недостаточно кредитов.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")]]))
            return
        await bot.send_chat_action(message.chat.id, "upload_photo")
        image_bytes = await generate_image(message.text, user_id)
        if image_bytes:
            if spend_credits(user_id, CREDIT_COSTS["image"], "image_generation"):
                from aiogram.types import BufferedInputFile
                await bot.send_photo(message.chat.id, BufferedInputFile(image_bytes, "image.png"),
                    caption=f"🎨 Готово! Списано {CREDIT_COSTS['image']} кр.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎨 Ещё", callback_data="gen_image")], [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]]))
        else:
            await message.answer("⚙️ Генерация изображений скоро будет доступна!\n\nПока обратись к Александру 🎨", reply_markup=contact_keyboard())
        return

    if user_id not in user_conversations:
        user_conversations[user_id] = []
    user_conversations[user_id].append({"role": "user", "content": message.text})
    if len(user_conversations[user_id]) > 20:
        user_conversations[user_id] = user_conversations[user_id][-20:]

    await bot.send_chat_action(message.chat.id, "typing")
    response = await get_claude_response(user_conversations[user_id])
    if response:
        user_conversations[user_id].append({"role": "assistant", "content": response})
        await message.answer(response, reply_markup=contact_keyboard())
    else:
        await message.answer("Что-то пошло не так 😅 Напиши Александру напрямую.", reply_markup=contact_keyboard())

async def main():
    init_db()
    logging.info("Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
