import asyncio
import logging
import re
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
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Neirosetkaalex")

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

user_conversations = {}

SYSTEM_PROMPT = """Ты — AI-консультант Александра, эксперта по нейросетям и AI-инструментам.
Ты помогаешь клиентам разобраться в нейросетях, выбрать нужный сервис и оформить подписку через Александра.
Ты также помогаешь пользователям из России и СНГ с VPN и обходом блокировок.

Твой характер: дружелюбный, экспертный, без лишней воды. Отвечаешь по делу, но тепло.
Используй эмодзи умеренно. Пиши на русском языке.

ВАЖНО ПРО ФОРМАТИРОВАНИЕ:
Никогда не используй markdown разметку: никаких решёток (#), звёздочек (*), подчёркиваний (_), обратных кавычек (`).
Пиши обычным текстом. Для структуры используй только эмодзи и переносы строк.

ВАЖНО: АКТУАЛЬНОСТЬ ИНФОРМАЦИИ
У тебя есть инструмент web_search для поиска актуальной информации.
ВСЕГДА используй его когда клиент спрашивает про цены, тарифы или функции любого сервиса.

Алгоритм ответа на вопрос о тарифах:
1. Сначала сделай поиск: "[название сервиса] тарифы цены 2025"
2. Изучи результаты
3. Дай актуальный ответ на основе найденного

СЕРВИСЫ КОТОРЫЕ МЫ ПРОДАЁМ:
ChatGPT, Claude, Grok, Midjourney, Cursor, Perplexity, Krea, Zoom, Suno, Kling AI, Runway, ElevenLabs, Gemini.
По каждому ищи актуальные тарифы через поиск перед ответом.

VPN И ОБХОД БЛОКИРОВОК:
Перед ответом про VPN ищи актуальную информацию какие VPN работают в России прямо сейчас.
Известные варианты: Outline, Lantern, Psiphon, Proton VPN, Windscribe, Warp (1.1.1.1).
Outline на своём VPS — самый надёжный вариант.

КАК ОФОРМИТЬ ПОДПИСКУ:
Клиент пишет Александру в личку (@Neirosetkaalex), называет нужный сервис и тариф.
Александр сам всё оформляет — не нужна иностранная карта или VPN.
Оплата в рублях / тенге, быстро и без лишних сложностей.

ПРАВИЛА ОТВЕТОВ:
1. ВСЕГДА ищи актуальные цены перед ответом о тарифах.
2. Если клиент не знает что выбрать — задай уточняющий вопрос: для чего нужна нейросеть?
3. Для оформления подписки всегда направляй к Александру: @Neirosetkaalex
4. По VPN — давай конкретные рабочие советы.
5. Если не знаешь ответа — честно скажи и предложи спросить Александра напрямую.
"""

WELCOME_MESSAGE = """Привет! 👋 Я — AI-консультант Александра, эксперта по нейросетям.
Помогу тебе разобраться в нейросетях и подобрать подходящий сервис 🤖

Что я могу:
✅ Рассказать про ChatGPT, Claude, Midjourney, Grok и другие AI-инструменты
✅ Помочь выбрать тариф под твои задачи
✅ Объяснить разницу между сервисами
✅ Направить к Александру для оформления подписки

Что тебя интересует? Или нужна помощь с выбором? 😊"""

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search"
}

def get_welcome_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать Александру", url=f"https://t.me/{ADMIN_USERNAME}")],
        [InlineKeyboardButton(text="🤖 Помочь с выбором нейросети", callback_data="help_choose")]
    ])

def get_contact_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Написать Александру", url=f"https://t.me/{ADMIN_USERNAME}")]
    ])

def clean_text(text):
    """Убираем markdown символы которые портят вид в Telegram"""
    # Убираем решётки заголовков
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    # Убираем жирный **текст** и ***текст***
    text = re.sub(r'\*{2,3}(.+?)\*{2,3}', r'\1', text)
    # Убираем курсив *текст*
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    # Убираем подчёркивание __текст__
    text = re.sub(r'__(.+?)__', r'\1', text)
    # Убираем инлайн код `текст`
    text = re.sub(r'`(.+?)`', r'\1', text)
    # Убираем блоки кода ```
    text = re.sub(r'```[\s\S]*?```', '', text)
    # Убираем горизонтальные линии ---
    text = re.sub(r'^[-_*]{3,}$', '', text, flags=re.MULTILINE)
    # Убираем лишние пустые строки
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def extract_text_from_response(response):
    """Извлекаем текст из ответа Claude"""
    text_parts = []
    for block in response.content:
        if hasattr(block, 'type') and block.type == 'text':
            text_parts.append(block.text)
    return clean_text("\n".join(text_parts))

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
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=[WEB_SEARCH_TOOL],
            messages=user_conversations[user_id]
        )

        messages = list(user_conversations[user_id])
        while response.stop_reason == "tool_use":
            await bot.send_chat_action(message.chat.id, "typing")
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            for block in response.content:
                if hasattr(block, 'type') and block.type == 'tool_use':
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Поиск выполнен"
                    })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            response = claude_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=[WEB_SEARCH_TOOL],
                messages=messages
            )

        reply_text = extract_text_from_response(response)

        if not reply_text:
            reply_text = "Не удалось получить ответ. Попробуй ещё раз или напиши Александру."

        user_conversations[user_id].append({"role": "assistant", "content": reply_text})
        await message.answer(reply_text, reply_markup=get_contact_keyboard())

    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await message.answer(
            "Что-то пошло не так 😅 Напиши Александру напрямую.",
            reply_markup=get_contact_keyboard()
        )

async def main():
    logging.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
