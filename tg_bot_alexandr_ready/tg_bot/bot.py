import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, ChatMemberUpdated, InlineKeyboardMarkup,
    InlineKeyboardButton, CallbackQuery
)
from aiogram.filters import ChatMemberUpdatedFilter, JOIN_TRANSITION
from aiogram.enums import ChatMemberStatus
import anthropic
from config import BOT_TOKEN, CLAUDE_API_KEY, CHANNEL_ID, ADMIN_USERNAME

logging.basicConfig(level=logging.INFO)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
claude_client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

# Хранилище истории диалогов (в памяти, для продакшена используй Redis/БД)
user_conversations = {}

SYSTEM_PROMPT = """Ты — AI-консультант Александра, эксперта по нейросетям и AI-инструментам.
Ты помогаешь клиентам разобраться в нейросетях, выбрать нужный сервис и оформить подписку через Александра.

Твой характер: дружелюбный, экспертный, без лишней воды. Отвечаешь по делу, но тепло.
Используй эмодзи умеренно. Пиши на русском языке.

━━━━━━━━━━━━━━━━━━━━━━
СЕРВИСЫ И ТАРИФЫ (данные с neirosetka.ru)
━━━━━━━━━━━━━━━━━━━━━━

📌 ЗАПОЛНИ ЭТОТ РАЗДЕЛ СВОИМИ АКТУАЛЬНЫМИ ЦЕНАМИ:

ChatGPT (OpenAI):
- [Вставь тарифы и цены]

Claude (Anthropic):
- [Вставь тарифы и цены]

Grok (xAI):
- [Вставь тарифы и цены]

Midjourney:
- [Вставь тарифы и цены]

Cursor:
- [Вставь тарифы и цены]

Perplexity:
- [Вставь тарифы и цены]

Krea:
- [Вставь тарифы и цены]

Zoom:
- [Вставь тарифы и цены]

━━━━━━━━━━━━━━━━━━━━━━
КАК ОФОРМИТЬ ПОДПИСКУ
━━━━━━━━━━━━━━━━━━━━━━
Клиент пишет Александру в личку (@AleksandrOii), называет нужный сервис и тариф.
Александр сам всё оформляет — клиенту не нужна иностранная карта или VPN.
Оплата в рублях / тенге, без лишних сложностей.

━━━━━━━━━━━━━━━━━━━━━━
ПРАВИЛА ОТВЕТОВ
━━━━━━━━━━━━━━━━━━━━━━
1. Если клиент спрашивает про конкретный сервис — расскажи что он даёт, тарифы, отличия.
2. Если клиент не знает что выбрать — задай уточняющий вопрос: для чего нужна нейросеть?
3. Для оформления подписки всегда направляй к Александру: @AleksandrOii
4. Не придумывай цены, которых нет в этом промпте.
5. Если не знаешь ответа — честно скажи и предложи спросить Александра напрямую.
"""

def get_welcome_keyboard():
    """Кнопки под приветственным сообщением"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="💬 Написать Александру",
            url=f"https://t.me/{ADMIN_USERNAME}"
        )],
        [InlineKeyboardButton(
            text="🤖 Помочь с выбором нейросети",
            callback_data="help_choose"
        )]
    ])
    return keyboard

def get_contact_keyboard():
    """Кнопка для связи с Александром"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="✍️ Написать Александру",
            url=f"https://t.me/{ADMIN_USERNAME}"
        )]
    ])
    return keyboard

WELCOME_MESSAGE = """👋 Привет! Рад приветствовать тебя в канале!

Я — AI-ассистент Александра. Помогу тебе:
🤖 Разобраться в нейросетях
💡 Выбрать подходящий сервис под твои задачи
💳 Узнать цены и тарифы
📋 Оформить подписку без VPN и иностранных карт

Просто напиши мне что тебя интересует, или нажми кнопку ниже 👇"""


@dp.chat_member(ChatMemberUpdatedFilter(JOIN_TRANSITION))
async def on_new_member(event: ChatMemberUpdated):
    """Срабатывает когда кто-то подписывается на канал"""
    if str(event.chat.id) != str(CHANNEL_ID):
        return

    user = event.new_chat_member.user

    # Не отправляем боту сообщение самому себе
    if user.is_bot:
        return

    try:
        await bot.send_message(
            chat_id=user.id,
            text=WELCOME_MESSAGE,
            reply_markup=get_welcome_keyboard()
        )
        logging.info(f"Приветствие отправлено пользователю {user.id} (@{user.username})")
    except Exception as e:
        # Пользователь мог запретить сообщения от ботов
        logging.warning(f"Не удалось отправить сообщение {user.id}: {e}")


@dp.callback_query(F.data == "help_choose")
async def help_choose_callback(callback: CallbackQuery):
    """Кнопка 'Помочь с выбором'"""
    await callback.message.answer(
        "Отлично! Расскажи мне — для каких задач тебе нужна нейросеть?\n\n"
        "Например:\n"
        "• Писать тексты / посты\n"
        "• Генерировать картинки\n"
        "• Программирование\n"
        "• Анализ документов\n"
        "• Видео / музыка\n"
        "• Что-то другое?\n\n"
        "Опиши своими словами — подберу лучший вариант 👇"
    )
    await callback.answer()


@dp.message()
async def handle_message(message: Message):
    """Обрабатывает все входящие сообщения — отвечает через Claude"""
    user_id = message.from_user.id

    # Инициализируем историю диалога если нет
    if user_id not in user_conversations:
        user_conversations[user_id] = []

    # Добавляем сообщение пользователя в историю
    user_conversations[user_id].append({
        "role": "user",
        "content": message.text
    })

    # Ограничиваем историю последними 20 сообщениями (10 диалогов)
    if len(user_conversations[user_id]) > 20:
        user_conversations[user_id] = user_conversations[user_id][-20:]

    # Показываем "печатает..."
    await bot.send_chat_action(message.chat.id, "typing")

    try:
        response = claude_client.messages.create(
            model="claude-sonnet-4-5",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=user_conversations[user_id]
        )

        reply_text = response.content[0].text

        # Сохраняем ответ бота в историю
        user_conversations[user_id].append({
            "role": "assistant",
            "content": reply_text
        })

        # Добавляем кнопку к каждому ответу
        await message.answer(reply_text, reply_markup=get_contact_keyboard())

    except Exception as e:
        logging.error(f"Ошибка Claude API: {e}")
        await message.answer(
            "Что-то пошло не так 😅 Попробуй ещё раз или напиши Александру напрямую.",
            reply_markup=get_contact_keyboard()
        )


async def main():
    logging.info("Бот запущен!")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
