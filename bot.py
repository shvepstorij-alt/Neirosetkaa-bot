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

SYSTEM_PROMPT = """Ты — AI-консультант Александра, эксперта по нейросетям.
Помогаешь клиентам выбрать сервис и оформить подписку через Александра (@Neirosetkaalex).
Также помогаешь с VPN и обходом блокировок для пользователей из России и СНГ.

СТИЛЬ ОТВЕТОВ — это самое важное:
- Пиши коротко и по делу. Максимум 5-7 строк на ответ.
- Одна пустая строка между абзацами — не больше.
- Никаких markdown символов: никаких #, *, _, `, ---.
- Никаких длинных вступлений и заключений.
- Используй эмодзи умеренно — только там где они реально нужны.
- Не повторяй одно и то же разными словами.
- Если нужно перечислить — пиши через перенос строки с символом •

ПРИМЕР ХОРОШЕГО ОТВЕТА на вопрос про Midjourney:
"Midjourney — нейросеть для генерации изображений по текстовому описанию.

Тарифы:
• Basic — $10/мес (200 картинок)
• Standard — $30/мес (безлимит в режиме Relax)
• Pro — $60/мес (+ режим Stealth)
• Mega — $120/мес (для студий и агентств)

Оформить без иностранной карты — пиши @Neirosetkaalex 😊"

ПРИМЕР ПЛОХОГО ОТВЕТА (так нельзя):
Длинные вступления, повторы, много пустых строк, обрывы на середине предложения, текст разбитый на мелкие куски.

АКТУАЛЬНОСТЬ ИНФОРМАЦИИ:
Используй web_search когда клиент спрашивает про цены или тарифы — ищи актуальные данные.
После поиска давай ответ в коротком формате как в примере выше.

СЕРВИСЫ: ChatGPT, Claude, Grok, Midjourney, Cursor, Perplexity, Krea, Zoom, Suno, Kling AI, Runway, ElevenLabs, Gemini.

КАК ОФОРМИТЬ ПОДПИСКУ:
Клиент пишет @Neirosetkaalex, называет сервис и тариф. Оплата в рублях/тенге, без иностранной карты и VPN.

ПРАВИЛА:
1. Для оформления всегда направляй к @Neirosetkaalex
2. Если не знаешь — честно скажи и предложи спросить Александра
3. По VPN — давай конкретные короткие советы
"""

WELCOME_MESSAGE = """Привет! 👋 Я — AI-консультант Александра, эксперта по нейросетям.

Могу помочь:
• Подобрать нейросеть под твои задачи
• Рассказать про тарифы ChatGPT, Claude, Midjourney и других
• Помочь с VPN и доступом к сервисам из России
• Направить к Александру для оформления подписки

Что тебя интересует? 😊"""

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
    """Убираем markdown символы и лишние пробелы"""
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'\*{2,3}(.+?)\*{2,3}', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'^[-_*]{3,}$', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

def extract_text_from_response(response):
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
        "Для каких задач нужна нейросеть?\n\n"
        "• Писать тексты / посты\n"
        "• Генерировать картинки\n"
        "• Программирование\n"
        "• Анализ документов\n"
        "• Видео / музыка\n\n"
        "Напиши своими словами 👇"
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
                max_tokens=1024,
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
