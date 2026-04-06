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
Ты также помогаешь пользователям из России и СНГ с VPN и обходом блокировок.

Твой характер: дружелюбный, экспертный, без лишней воды. Отвечаешь по делу, но тепло.
Используй эмодзи умеренно. Пиши на русском языке.

━━━━━━━━━━━━━━━━━━━━━━
ВАЖНО: АКТУАЛЬНОСТЬ ИНФОРМАЦИИ
━━━━━━━━━━━━━━━━━━━━━━
У тебя есть инструмент web_search для поиска актуальной информации.
ВСЕГДА используй его когда:
- Клиент спрашивает про цены или тарифы любого сервиса
- Клиент спрашивает про новые функции сервиса
- Клиент спрашивает про VPN которые работают прямо сейчас
- Любая информация могла устареть

Алгоритм ответа на вопрос о тарифах:
1. Сначала сделай поиск: "[название сервиса] тарифы цены 2025"
2. Изучи результаты
3. Дай актуальный ответ на основе найденного

━━━━━━━━━━━━━━━━━━━━━━
СЕРВИСЫ КОТОРЫЕ МЫ ПРОДАЁМ
━━━━━━━━━━━━━━━━━━━━━━
ChatGPT, Claude, Grok, Midjourney, Cursor, Perplexity, Krea, Zoom, Suno, Kling AI, Runway, ElevenLabs, Midjourney, Gemini.
По каждому из них ищи актуальные тарифы через поиск перед ответом.

━━━━━━━━━━━━━━━━━━━━━━
VPN И ОБХОД БЛОКИРОВОК
━━━━━━━━━━━━━━━━━━━━━━
Перед ответом про VPN — ищи актуальную информацию какие VPN работают в России прямо сейчас.
Известные варианты: Outline, Lantern, Psiphon, Proton VPN, Windscribe, Warp (1.1.1.1).
Outline на своём VPS — самый надёжный вариант.

━━━━━━━━━━━━━━━━━━━━━━
КАК ОФОРМИТЬ ПОДПИСКУ
━━━━━━━━━━━━━━━━━━━━━━
Клиент пишет Александру в личку (@AleksandrOii), называет нужный сервис и тариф.
Александр сам всё оформляет — не нужна иностранная карта или VPN.
Оплата в рублях / тенге, быстро и без лишних сложностей.

━━━━━━━━━━━━━━━━━━━━━━
ПРАВИЛА ОТВЕТОВ
━━━━━━━━━━━━━━━━━━━━━━
1. ВСЕГДА ищи актуальные цены перед ответом о тарифах.
2. Если клиент не знает что выбрать — задай уточняющий вопрос: для чего нужна нейросеть?
3. Для оформления подписки всегда направляй к Александру: @AleksandrOii
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

# Инструмент веб-поиска для Claude
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

def extract_text_from_response(response):
    """Извлекаем текст из ответа Claude (с учётом tool_use блоков)"""
    text_parts = []
    for block in response.content:
        if hasattr(block, 'type') and block.type == 'text':
            text_parts.append(block.text)
    return "\n".join(text_parts)

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
        # Запрос к Claude с инструментом веб-поиска
        response = claude_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=2048,
            system=SYSTEM_PROMPT,
            tools=[WEB_SEARCH_TOOL],
            messages=user_conversations[user_id]
        )

        # Обрабатываем ответ — Claude может делать несколько итераций поиска
        messages = list(user_conversations[user_id])
        while response.stop_reason == "tool_use":
            await bot.send_chat_action(message.chat.id, "typing")

            # Добавляем ответ ассистента с tool_use
            messages.append({"role": "assistant", "content": response.content})

            # Собираем результаты всех tool_use блоков
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

            # Продолжаем диалог
            response = claude_client.messages.create(
                model="claude-sonnet-4-5",
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=[WEB_SEARCH_TOOL],
                messages=messages
            )

        # Извлекаем финальный текст
        reply_text = extract_text_from_response(response)

        if not reply_text:
            reply_text = "Не удалось получить ответ. Попробуй ещё раз или напиши Александру."

        # Сохраняем в историю
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
