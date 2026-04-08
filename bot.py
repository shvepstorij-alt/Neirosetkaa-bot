import asyncio
import logging
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
# МОИ ЦЕНЫ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
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

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# СИСТЕМНЫЙ ПРОМПТ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SYSTEM_PROMPT = f"""Ты — AI-консультант по нейросетям и AI-инструментам.

Характер: дружелюбный, экспертный, по делу. Пишешь на русском.

ФОРМАТИРОВАНИЕ:
- Эмодзи умеренно
- Жирный текст (*текст*) можно использовать только для: названий сервисов, заголовков разделов, ключевых цифр/цен
- Никаких других markdown символов: никаких **, ##, ___, ---, и т.д.
- Пиши чисто и читабельно

{MY_PRICES}

ПРАВИЛА:
1. Цены — ТОЛЬКО из списка выше. Никогда не называй официальные $ цены как итоговую стоимость.
2. ВСЕГДА используй web_search перед ответом о возможностях любого сервиса. Никогда не отвечай по памяти — информация устаревает быстро.
3. Если клиент не знает что выбрать — спроси для каких задач нужна нейросеть.
4. Упоминай про оформление подписки естественно, только когда уместно. Не навязывай.
5. Для покупки направляй: @{PERSONAL_USERNAME}
6. Оплата только в рублях. Оформление за 5-15 минут.
7. По VPN — помогай советами, рекомендуй бесплатные варианты (Outline, Lantern, Windscribe).
8. СТРОГО ЗАПРЕЩЕНО отвечать на темы: политика, экономика, новости, отношения, медицина, юриспруденция и всё что НЕ связано с AI-инструментами, подписками и VPN.
9. На запрещённые темы отвечай: "Я консультирую только по нейросетям и AI-инструментам 🤖 Спроси меня про это!"
"""

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ТАРИФЫ БОТА
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
BOT_PLANS = {
    "starter": {"name": "⚡ Старт", "price_stars": 150, "price_rub": 299, "credits": 50, "days": 0, "description": "50 кредитов (разово) — 10 изображений"},
    "basic": {"name": "🔥 Базовый", "price_stars": 350, "price_rub": 699, "credits": 200, "days": 30, "description": "200 кредитов на 30 дней — 40 изображений"},
    "pro": {"name": "💎 Про", "price_stars": 750, "price_rub": 1490, "credits": 600, "days": 30, "description": "600 кредитов на 30 дней — 120 изображений"}
}
CREDIT_COSTS = {"image": 5, "consultation": 0}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# КЛАВИАТУРЫ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def main_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤖 Помочь с выбором", callback_data="help_choose"),
            InlineKeyboardButton(text="🔒 VPN для РФ", callback_data="vpn_help")
        ],
        [
            InlineKeyboardButton(text="🎨 Генерация изображений", callback_data="gen_image"),
            InlineKeyboardButton(text="💰 Мой баланс", callback_data="my_balance")
        ],
        [
            InlineKeyboardButton(text="✍️ Написать для покупки", url=f"https://t.me/{PERSONAL_USERNAME}")
        ]
    ])

def contact_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать для покупки", url=f"https://t.me/{PERSONAL_USERNAME}")],
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

def admin_keyboard():
    maintenance = get_maintenance_mode()
    maint_text = "✅ Техобслуживание ВКЛ" if maintenance else "🔄 Режим техобслуживания"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="adm_stats"),
         InlineKeyboardButton(text="📈 Активность", callback_data="adm_activity")],
        [InlineKeyboardButton(text="🔥 Популярные сервисы", callback_data="adm_popular"),
         InlineKeyboardButton(text="🏆 Топ активных", callback_data="adm_top_users")],
        [InlineKeyboardButton(text="👥 Пользователи", callback_data="adm_users"),
         InlineKeyboardButton(text="🔍 Найти по @username", callback_data="adm_find_user")],
        [InlineKeyboardButton(text="💳 Кредиты", callback_data="adm_credits"),
         InlineKeyboardButton(text="📜 История начислений", callback_data="adm_credits_history")],
        [InlineKeyboardButton(text="💰 Расход по юзерам", callback_data="adm_credits_spent"),
         InlineKeyboardButton(text="🚫 Блокировки", callback_data="adm_blocks")],
        [InlineKeyboardButton(text="✏️ Изменить приветствие", callback_data="adm_edit_welcome")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="adm_broadcast"),
         InlineKeyboardButton(text=maint_text, callback_data="adm_maintenance")],
    ])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ОТВЕТ CLAUDE С ВЕБ-ПОИСКОМ
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
        welcome_text = custom_msg if custom_msg else (
            f"👋 Привет, {user.first_name}!\n\n"
            "Я AI-консультант по нейросетям 🤖\n\n"
            "Помогу тебе:\n"
            "🔍 Выбрать нужную нейросеть под твои задачи\n"
            "💳 Узнать цены и условия подписок\n"
            "🎨 Сгенерировать изображения прямо здесь\n"
            "🔒 Разобраться с VPN для России\n\n"
            "Просто напиши мне или выбери ниже 👇"
        )
        await bot.send_message(user.id, welcome_text, parse_mode="Markdown", reply_markup=main_keyboard())
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
        "Я AI-консультант по нейросетям.\n\n"
        "Отвечу на любые вопросы об AI-инструментах, расскажу про тарифы и помогу выбрать нужный сервис 🚀\n\n"
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
        await message.answer(f"❌ Ошибка: {e}\nФормат: /add\\_credits [user\\_id] [amount]", parse_mode="Markdown")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ADMIN CALLBACKS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.callback_query(F.data == "adm_stats")
async def adm_stats(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    s = get_stats()
    text = (
        "📊 *Статистика пользователей*\n\n"
        f"👥 Всего пользователей: *{s['total_users']}*\n"
        f"🆕 Новых сегодня: *{s['new_today']}*\n"
        f"📅 Новых за неделю: *{s['new_week']}*\n"
        f"💳 Платных аккаунтов: *{s['paid_users']}*"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "adm_popular")
async def adm_popular(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    popular = get_popular_services(10)
    if popular:
        lines = "\n".join([f"{i+1}. *{name}* — {cnt} запросов" for i, (name, cnt) in enumerate(popular)])
    else:
        lines = "Пока нет данных"
    text = f"🔥 *Популярные сервисы*\n\n{lines}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "adm_users")
async def adm_users(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    users = get_all_users()
    text = "👥 *Последние пользователи:*\n\n"
    for u in users[:15]:
        uid, uname, fname, credits, plan, joined = u
        name = f"@{uname}" if uname else fname or str(uid)
        date = joined[:10] if joined else "?"
        text += f"• {name} | {credits} кр. | {plan} | {date}\n"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "adm_credits")
async def adm_credits(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    s = get_stats()
    text = (
        "💳 *Кредиты и генерации*\n\n"
        f"⚡ Всего потрачено кредитов: *{s['total_credits_spent']}*\n"
        f"🎨 Всего генераций: *{s['total_images']}*\n\n"
        "Нажми кнопку ниже чтобы выдать кредиты пользователю 👇"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Выдать кредиты", callback_data="adm_give_credits")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]
    ])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "adm_give_credits")
async def adm_give_credits(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    user_conversations[f"give_credits_step_{callback.from_user.id}"] = "awaiting_user_id"
    await callback.message.edit_text(
        "🎁 *Выдача кредитов — Шаг 1 из 2*\n\n"
        "Напиши *user\\_id* пользователя.\n\n"
        "_User ID видно в списке пользователей 👥_",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_credits")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_amount_"))
async def adm_choose_amount(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    parts = callback.data.split("_")
    target_uid = int(parts[2])
    amount = int(parts[3])
    user = get_user(target_uid)
    if not user:
        await callback.answer("Пользователь не найден")
        return
    add_credits(target_uid, amount, "Выдано админом")
    updated = get_user(target_uid)
    name = f"@{user['username']}" if user['username'] else user['first_name'] or str(target_uid)
    await callback.message.edit_text(
        f"✅ *Кредиты начислены!*\n\n"
        f"Пользователь: {name}\n"
        f"Начислено: *{amount}* кр.\n"
        f"Новый баланс: *{updated['credits']}* кр.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎁 Выдать ещё", callback_data="adm_give_credits")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data="adm_back")]
        ])
    )
    try:
        await bot.send_message(target_uid,
            f"🎁 Тебе начислено *{amount}* кредитов!\nБаланс: *{updated['credits']}* кр.",
            parse_mode="Markdown"
        )
    except:
        pass
    await callback.answer()

@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    user_conversations[f"broadcast_{callback.from_user.id}"] = True
    await callback.message.edit_text(
        "📣 *Рассылка*\n\n"
        "Напиши сообщение для рассылки всем пользователям бота.\n\n"
        "_Поддерживается жирный текст и эмодзи._\n\n"
        "Отправь текст сообщения 👇",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_activity")
async def adm_activity(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    a = get_today_activity()
    text = (
        "📈 *Активность за сегодня*\n\n"
        f"✉️ Сообщений: *{a['messages_today']}*\n"
        f"👤 Активных пользователей: *{a['active_users_today']}*"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "adm_top_users")
async def adm_top_users(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    top = get_top_active_users(10)
    if top:
        lines = []
        for i, (uid, uname, fname, cnt) in enumerate(top):
            name = f"@{uname}" if uname else fname or str(uid)
            lines.append(f"{i+1}. {name} — {cnt} действий")
        text = "🏆 *Топ активных пользователей*\n\n" + "\n".join(lines)
    else:
        text = "🏆 *Топ активных пользователей*\n\nПока нет данных"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "adm_find_user")
async def adm_find_user(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    user_conversations[f"find_user_{callback.from_user.id}"] = True
    await callback.message.edit_text(
        "🔍 *Поиск пользователя*\n\nНапиши @username или user_id:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_credits_history")
async def adm_credits_history(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    history = get_credits_history(15)
    if history:
        lines = []
        for uid, uname, fname, amount, desc, created in history:
            name = f"@{uname}" if uname else fname or str(uid)
            date = created[:10] if created else "?"
            lines.append(f"• {name} +{amount} кр. | {date}")
        text = "📜 *История начислений*\n\n" + "\n".join(lines)
    else:
        text = "📜 *История начислений*\n\nПока нет данных"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "adm_credits_spent")
async def adm_credits_spent(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    data = get_credits_spent_by_user(10)
    if data:
        lines = []
        for i, (uid, uname, fname, total) in enumerate(data):
            name = f"@{uname}" if uname else fname or str(uid)
            lines.append(f"{i+1}. {name} — {total} кр.")
        text = "💰 *Расход кредитов по пользователям*\n\n" + "\n".join(lines)
    else:
        text = "💰 *Расход кредитов*\n\nПока нет данных"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Назад", callback_data="adm_back")]])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data == "adm_blocks")
async def adm_blocks(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    user_conversations[f"block_step_{callback.from_user.id}"] = "awaiting_id"
    await callback.message.edit_text(
        "🚫 *Блокировка пользователя*\n\n"
        "Напиши @username или user_id пользователя:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_do_block_"))
async def adm_do_block(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    target_uid = int(callback.data.split("_")[3])
    block_user(target_uid)
    await callback.message.edit_text(
        f"🚫 Пользователь {target_uid} заблокирован.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data.startswith("adm_do_unblock_"))
async def adm_do_unblock(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    target_uid = int(callback.data.split("_")[3])
    unblock_user(target_uid)
    await callback.message.edit_text(
        f"✅ Пользователь {target_uid} разблокирован.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_maintenance")
async def adm_maintenance(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    current = get_maintenance_mode()
    set_maintenance_mode(not current)
    status = "ВКЛЮЧЁН 🔧" if not current else "ВЫКЛЮЧЕН ✅"
    await callback.answer(f"Режим техобслуживания {status}", show_alert=True)
    await callback.message.edit_text("🔧 *Админ панель*\n\nВыбери раздел:", parse_mode="Markdown", reply_markup=admin_keyboard())

@dp.callback_query(F.data == "adm_edit_welcome")
async def adm_edit_welcome(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    current = get_welcome_message()
    user_conversations[f"edit_welcome_{callback.from_user.id}"] = True
    preview = f"\n\nТекущее:\n_{current[:200]}_" if current else ""
    await callback.message.edit_text(
        f"✏️ *Изменить приветственное сообщение*{preview}\n\nНапиши новый текст приветствия:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="adm_back")]])
    )
    await callback.answer()

@dp.callback_query(F.data == "adm_back")
async def adm_back(callback: CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        await callback.answer("Нет доступа")
        return
    await callback.message.edit_text("🔧 *Админ панель*\n\nВыбери раздел:", parse_mode="Markdown", reply_markup=admin_keyboard())
    await callback.answer()

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ОСНОВНЫЕ CALLBACKS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню 👇", reply_markup=main_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "help_choose")
async def cb_help_choose(callback: CallbackQuery):
    await callback.message.edit_text(
        "Расскажи — для каких задач нужна нейросеть? 🤔\n\n"
        "Например:\n"
        "• Тексты / посты / статьи\n"
        "• Изображения или видео\n"
        "• Код / разработка\n"
        "• Анализ документов\n"
        "• Музыка / озвучка\n"
        "• Поиск информации\n\n"
        "Опиши своими словами — подберу лучший вариант 👇",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Все сервисы", callback_data="show_services")],
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

    # Логируем интерес к сервису
    log_question(callback.from_user.id, service_key)

    await callback.message.edit_text(f"🔍 Ищу актуальную информацию о *{name}*...", parse_mode="Markdown")
    messages = [{"role": "user", "content": f"Расскажи подробно что умеет {name}, для каких задач подходит и какая цена у Александра. Найди актуальную информацию через поиск."}]
    response = await get_claude_response(messages)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🛒 Купить {name}", url=f"https://t.me/{PERSONAL_USERNAME}")],
        [InlineKeyboardButton(text="◀️ К списку", callback_data="show_services")]
    ])
    await callback.message.edit_text(
        response or f"Не удалось загрузить. Напиши @{PERSONAL_USERNAME}",
        parse_mode="Markdown",
        reply_markup=kb
    )
    await callback.answer()

@dp.callback_query(F.data == "vpn_help")
async def cb_vpn(callback: CallbackQuery):
    log_question(callback.from_user.id, "vpn")
    await callback.message.edit_text(
        "🔒 *VPN для России*\n\n"
        "Для доступа к AI-сервисам из РФ нужен VPN.\n\n"
        "*Бесплатные варианты:*\n"
        "• Outline VPN — лучший выбор, стабильно работает в РФ\n"
        "• Lantern — работает без настроек\n"
        "• Windscribe — 10 GB/мес бесплатно\n\n"
        "*Платные (надёжнее):*\n"
        "• ExpressVPN — самый быстрый\n"
        "• NordVPN — много серверов\n\n"
        "💡 При покупке подписки через нас VPN не нужен — всё оформляем сами!\n\n"
        "Есть вопросы по настройке? 👇",
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
    expires = ""
    if user.get("plan_expires"):
        try:
            exp = datetime.fromisoformat(user["plan_expires"])
            expires = f"\nДействует до: {exp.strftime('%d.%m.%Y')}"
        except:
            pass
    await callback.message.edit_text(
        f"💰 *Мой баланс*\n\n"
        f"Тариф: {plan_labels.get(user['plan'], user['plan'])}{expires}\n"
        f"Кредиты: *{user['credits']}* кр.\n\n"
        f"• 1 изображение = {CREDIT_COSTS['image']} кр.\n"
        f"• Консультация = бесплатно 🆓",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🛒 Пополнить кредиты", callback_data="buy_credits")],
            [InlineKeyboardButton(text="◀️ Меню", callback_data="main_menu")]
        ])
    )
    await callback.answer()

@dp.callback_query(F.data == "buy_credits")
async def cb_buy_credits(callback: CallbackQuery):
    text = "💰 *Кредиты для генерации изображений*\n\nКонсультации — бесплатно 🆓\n1 изображение = 5 кредитов\n\n"
    for p in BOT_PLANS.values():
        text += f"*{p['name']}* — {p['price_rub']} ₽\n{p['description']}\n\n"
    text += "👇 Выбери тариф:"
    buttons = [[InlineKeyboardButton(text=f"{p['name']} — {p['price_rub']} ₽ ({p['credits']} кр.)", callback_data=f"buy_plan_{pid}")] for pid, p in BOT_PLANS.items()]
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")])
    await callback.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

@dp.callback_query(F.data.startswith("buy_plan_"))
async def cb_buy_plan(callback: CallbackQuery):
    plan_id = callback.data.replace("buy_plan_", "")
    plan = BOT_PLANS.get(plan_id)
    if not plan:
        await callback.answer("Тариф не найден")
        return
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title=plan["name"], description=plan["description"],
        payload=f"plan_{plan_id}_{callback.from_user.id}",
        currency="XTR",
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
    if credits < CREDIT_COSTS["image"]:
        await callback.message.edit_text(
            f"❌ *Недостаточно кредитов*\n\n"
            f"У тебя: {credits} кр. | Нужно: {CREDIT_COSTS['image']} кр.\n\n"
            f"Купи кредиты, чтобы начать 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="main_menu")]
            ])
        )
    else:
        user_conversations[f"mode_{user_id}"] = "image_gen"
        await callback.message.edit_text(
            f"🎨 *Генерация изображений*\n\n"
            f"Баланс: *{credits}* кр. | Стоимость: *{CREDIT_COSTS['image']}* кр./изображение\n\n"
            f"Опиши что хочешь сгенерировать 👇",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ Отмена", callback_data="main_menu")]])
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
    parts = message.successful_payment.invoice_payload.split("_")
    plan = BOT_PLANS.get(parts[1])
    if plan:
        expires = (datetime.now() + timedelta(days=plan["days"])).isoformat() if plan["days"] > 0 else None
        set_plan(message.from_user.id, parts[1], expires, plan["credits"])
        add_credits(message.from_user.id, plan["credits"], f"Покупка {plan['name']}")
        await message.answer(
            f"✅ *Оплата прошла!*\n\nТариф: *{plan['name']}*\nНачислено: *{plan['credits']}* кредитов 🎉",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ГЕНЕРАЦИЯ ИЗОБРАЖЕНИЙ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def generate_image(prompt: str, user_id: int):
    # TODO: подключить Google Imagen 3
    # import google.genai as genai
    # client = genai.Client(api_key=os.getenv("GOOGLE_API_KEY"))
    # response = client.models.generate_image(model="imagen-3.0-generate-002", prompt=prompt)
    # return response.generated_images[0].image.image_bytes
    return None

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ОСНОВНОЙ ОБРАБОТЧИК СООБЩЕНИЙ
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@dp.message()
async def handle_message(message: Message):
    if not message.text:
        return
    user_id = message.from_user.id
    create_user(user_id, message.from_user.username, message.from_user.first_name)

    # Проверка блокировки
    if is_blocked(user_id):
        return

    # Режим техобслуживания (кроме админов)
    if user_id not in ADMIN_IDS and get_maintenance_mode():
        await message.answer("🔧 Бот временно на техобслуживании. Скоро вернёмся!")
        return

    # Логируем сообщение для статистики активности
    log_message(user_id)

    # Режим выдачи кредитов: ожидаем user_id от админа
    if user_id in ADMIN_IDS and user_conversations.get(f"give_credits_step_{user_id}") == "awaiting_user_id":
        try:
            target_uid = int(message.text.strip())
            target_user = get_user(target_uid)
            if not target_user:
                await message.answer("❌ Пользователь не найден. Проверь ID и попробуй снова.")
                return
            user_conversations[f"give_credits_step_{user_id}"] = None
            name = f"@{target_user['username']}" if target_user['username'] else target_user['first_name'] or str(target_uid)
            await message.answer(
                f"🎁 *Выдача кредитов — Шаг 2 из 2*\n\n"
                f"Пользователь: {name}\n"
                f"Текущий баланс: *{target_user['credits']}* кр.\n\n"
                f"Выбери сколько кредитов начислить 👇",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="10 кр.", callback_data=f"adm_amount_{target_uid}_10"),
                        InlineKeyboardButton(text="20 кр.", callback_data=f"adm_amount_{target_uid}_20"),
                        InlineKeyboardButton(text="30 кр.", callback_data=f"adm_amount_{target_uid}_30"),
                    ],
                    [
                        InlineKeyboardButton(text="50 кр.", callback_data=f"adm_amount_{target_uid}_50"),
                        InlineKeyboardButton(text="100 кр.", callback_data=f"adm_amount_{target_uid}_100"),
                        InlineKeyboardButton(text="200 кр.", callback_data=f"adm_amount_{target_uid}_200"),
                    ],
                    [
                        InlineKeyboardButton(text="300 кр.", callback_data=f"adm_amount_{target_uid}_300"),
                        InlineKeyboardButton(text="500 кр.", callback_data=f"adm_amount_{target_uid}_500"),
                        InlineKeyboardButton(text="1000 кр.", callback_data=f"adm_amount_{target_uid}_1000"),
                    ],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="adm_credits")]
                ])
            )
        except ValueError:
            await message.answer("❌ Введи числовой user_id. Попробуй снова.")
        return

    # Поиск пользователя
    if user_id in ADMIN_IDS and user_conversations.get(f"find_user_{user_id}"):
        user_conversations.pop(f"find_user_{user_id}", None)
        query = message.text.strip()
        found = None
        if query.startswith("@"):
            found = find_user_by_username(query)
        else:
            try:
                found = get_user(int(query))
            except ValueError:
                found = find_user_by_username(query)
        if found:
            name = f"@{found['username']}" if found.get('username') else found.get('first_name') or str(found['user_id'])
            plan_label = found.get('plan', 'free')
            is_bl = plan_label == 'blocked'
            block_btn_text = "✅ Разблокировать" if is_bl else "🚫 Заблокировать"
            block_cb = f"adm_do_unblock_{found['user_id']}" if is_bl else f"adm_do_block_{found['user_id']}"
            await message.answer(
                f"🔍 *Пользователь найден*\n\n"
                f"Имя: {name}\n"
                f"ID: `{found['user_id']}`\n"
                f"Кредиты: *{found['credits']}* кр.\n"
                f"Тариф: {plan_label}",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"🎁 Выдать кредиты", callback_data=f"adm_give_credits")],
                    [InlineKeyboardButton(text=block_btn_text, callback_data=block_cb)],
                    [InlineKeyboardButton(text="◀️ В меню", callback_data="adm_back")]
                ])
            )
            user_conversations[f"give_credits_step_{user_id}"] = None
            user_conversations[f"give_target_{user_id}"] = found['user_id']
        else:
            await message.answer("❌ Пользователь не найден.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="adm_back")]]))
        return

    # Блокировка пользователя
    if user_id in ADMIN_IDS and user_conversations.get(f"block_step_{user_id}") == "awaiting_id":
        user_conversations.pop(f"block_step_{user_id}", None)
        query = message.text.strip()
        found = None
        if query.startswith("@"):
            found = find_user_by_username(query)
        else:
            try:
                found = get_user(int(query))
            except ValueError:
                found = find_user_by_username(query)
        if found:
            name = f"@{found['username']}" if found.get('username') else found.get('first_name') or str(found['user_id'])
            is_bl = found.get('plan') == 'blocked'
            await message.answer(
                f"Пользователь: {name}\nСтатус: {'🚫 Заблокирован' if is_bl else '✅ Активен'}\n\nЧто сделать?",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"adm_do_block_{found['user_id']}")],
                    [InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"adm_do_unblock_{found['user_id']}")],
                    [InlineKeyboardButton(text="◀️ Отмена", callback_data="adm_back")]
                ])
            )
        else:
            await message.answer("❌ Пользователь не найден.")
        return

    # Изменение приветственного сообщения
    if user_id in ADMIN_IDS and user_conversations.get(f"edit_welcome_{user_id}"):
        user_conversations.pop(f"edit_welcome_{user_id}", None)
        set_welcome_message(message.text)
        await message.answer("✅ Приветственное сообщение обновлено!", reply_markup=admin_keyboard())
        return

    # Режим рассылки (только для админа)
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
        await message.answer(
            f"📣 *Рассылка завершена*\n\n✅ Отправлено: {sent}\n❌ Не доставлено: {failed}",
            parse_mode="Markdown", reply_markup=admin_keyboard()
        )
        return

    # Режим генерации изображений
    if user_conversations.get(f"mode_{user_id}") == "image_gen":
        user_conversations[f"mode_{user_id}"] = None
        user = get_user(user_id)
        if not user or user["credits"] < CREDIT_COSTS["image"]:
            await message.answer("❌ Недостаточно кредитов.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🛒 Купить кредиты", callback_data="buy_credits")]]))
            return
        await bot.send_chat_action(message.chat.id, "upload_photo")
        image_bytes = await generate_image(message.text, user_id)
        if image_bytes:
            if spend_credits(user_id, CREDIT_COSTS["image"], "image_generation"):
                from aiogram.types import BufferedInputFile
                await bot.send_photo(message.chat.id, BufferedInputFile(image_bytes, "image.png"),
                    caption=f"🎨 Готово! Списано {CREDIT_COSTS['image']} кр.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🎨 Ещё", callback_data="gen_image")],
                        [InlineKeyboardButton(text="🏠 Меню", callback_data="main_menu")]
                    ]))
        else:
            await message.answer("⚙️ Генерация изображений скоро будет доступна!", reply_markup=contact_keyboard())
        return

    # Логируем ключевые слова для статистики
    text_lower = message.text.lower()
    for keyword in ["chatgpt", "claude", "grok", "midjourney", "cursor", "perplexity", "krea", "suno", "kling", "runway", "heygen", "higgsfield", "lovable", "gamma", "zoom", "vpn"]:
        if keyword in text_lower:
            log_question(user_id, keyword)
            break

    # Обычная консультация через Claude + веб-поиск
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
        await message.answer("Что-то пошло не так 😅 Напиши напрямую.", reply_markup=contact_keyboard())

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# ЗАПУСК
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
async def main():
    init_db()
    logging.info("Bot started!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())

# PLACEHOLDER - этот блок будет вставлен через sed
