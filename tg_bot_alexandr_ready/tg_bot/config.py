import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")           # Токен от @BotFather
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY") # Ключ Anthropic API
CHANNEL_ID = os.getenv("CHANNEL_ID")         # ID канала, например -1001234567890
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "AleksandrOii")  # Без @
