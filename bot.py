import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, ChatMemberUpdated, InlineKeyboardMarkup,
    InlineKeyboardButton, CallbackQuery
)
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION
import anthropic
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "AleksandrOii")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

user_conversations = {}

SYSTEM_PROMPT = """Ты — AI-консультант Александра, эксперта по нейросетям и AI-инструментам.
Ты помогаешь клиентам разобраться в нейросетях, выбрать нужный сервис и оформить подписку через Александра.

Твой характер: дружелюбный, экспертный, без лишней воды. Отвечаешь по делу, но тепло.
Используй эмодзи умеренно. Пиши на русском языке.

━━━━━━━━━━━━━━━━━━━━━━
СЕРВИСЫ И ТАРИФЫ
━━━━━━━━━━━━━━━━━━━━━━

ChatGPT (OpenAI):
- Бесплатный: базовый доступ с лимитами
- Plus ($20/мес): GPT-4o, генерация изображений DALL-E, анализ файлов
- Pro ($200/мес): безлимитный доступ, o1 pro mode

Claude (Anthropic):
- Бесплатный: базовый доступ с лимитами
- Pro ($20/мес): в 5 раз больше сообщений, работа с большими документами, приоритетный доступ

Grok (xAI):
- Бесплатный: базовый доступ
- Premium ($16/мес): Grok 2, генерация изображений
- Premium+ ($50/мес): максимальный доступ, все функции

Midjourney:
- Basic ($10/мес): 200 генераций/мес
- Standard ($30/мес): безлимит в режиме relax
- Pro ($60/мес): безлимит + fast hours
- Mega ($120/мес): максимальный пакет

Cursor (AI-редактор кода):
- Бесплатный: 2000 автодополнений
- Pro ($20/мес): безлимит автодополнений, GPT-4, Claude

Perplexity:
- Бесплатный: базовый поиск
- Pro ($20/мес): неограниченный поиск, GPT-4, Claude, загрузка файлов

Krea AI:
- Бесплатный: лимитированный доступ
- Pro ($35/мес): безлимит генераций, upscale, real-time режим

Zoom:
- Бесплатный: до 40 мин на встречу
- Pro ($15/мес): безлимит по времени, 5 ГБ облако
- Business ($20/мес): до 300 участников, транскрипция

━━━━━━━━━━━━━━━━━━━━━━
КАК ОФОРМИТЬ ПОДПИСКУ
━━━━━━━━━━━━━━━━━━━━━━
Клиент пишет Александру в личку (@AleksandrOii), называет нужный сервис и тариф.
Александр сам всё оформляет — не нужна иностранная карта или VPN.
Оплата в рублях / тенге, быстро и без лишних сложностей.

━━━━━━━━━━━━━━━━━━━━━━
ПРАВИЛА ОТВЕТОВ
━━━━━━━━━━━━━━━━━━━━━━
1. Если клиент спрашивает про конкретный сервис — расскажи что он даёт, тарифы, отличия.
2. Если клиент не знает что выбрать — задай уточняющий вопрос: для чего нужна нейросеть?
3. Для оформления подписки всегда направляй к Александру: @AleksandrOii
4. Если не знаешь ответа — честно скажи и предложи спросить Александра напрямую.
"""

WELCOME_MESSAGE = """👋 Привет! Рад приветствовать тебя в канале!

Я — AI-ассистент Александра. Помогу тебе:
🤖 Разобраться в нейросетях
💡 Выбрать подходящий сервис под твои задачи
💳 Узнать цены и тарифы
📋 Оформить подписку без VPN и иностранных карт

Просто напиши мне что тебя интересует 👇"""

def get_welcome_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать Александру", url=f"https://t.me/{ADMIN_USERNAME}")],
        [InlineKeyboardButton(text="🤖 Помочь с выбором нейросети", callback_data="help_choose")]
    ])

def get_contact_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать Александру", url=f"https://t.me/{ADMIN_USERNAME}")]
    ])

@dp.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated):
    if str(event.chat.id) != str(CHANNEL_ID):
        return
    user = event.new_chat_member.user
    if user.is_bot:
        return
    try:
        await bot.send_message(chat_id=user.id, text=WELCOME_MESSAGE, reply_markup=get_welcome_keyboard())
        logging.info(f"Приветствие отправлено: {user.id} @{user.username}")
    except Exception as e:
        logging.warning(f"Не удалось отправить {user.id}: {e}")

@dp.callback_query(F.data == "help_choose")
async def help_choose_callback(callback: CallbackQuery):
    await callback.message.answer(
        "Отлично! Расскажи — для каких задач тебе нужна нейросеть?\n\n"
        "Например:\n"
        "• Писать тексты / посты\n"
        "• Генерировать картинки\n"
        "• Программирование\n"
        "• Анализ документов\n"
        "• Видео / музыка\n\n"
        "Опиши своими словами — подберу лучший вариант 👇"
    )
    await callback.answer()

@dp.message()
async def handle_message(message: Message):
    user_id = message.from_user.id
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
        await message.answer(reply_text, reply_markup=get_contact_keyboard())
    except Exception as e:
        logging.error(f"Ошибка Claude API: {e}")
        await message.answer(
            "Что-то пошло не так 😅 Напиши Александру напрямую.",
            reply_markup=get_contact_keyboard()
        )

async def main():
    logging.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
