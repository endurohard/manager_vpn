"""
Конфигурация бота
"""
import os
from dotenv import load_dotenv

load_dotenv()

# Telegram Bot
BOT_TOKEN = os.getenv('BOT_TOKEN')
ADMIN_ID = int(os.getenv('ADMIN_ID', 0))

# X-UI Panel
XUI_HOST = os.getenv('XUI_HOST', '')
XUI_USERNAME = os.getenv('XUI_USERNAME', 'admin')
XUI_PASSWORD = os.getenv('XUI_PASSWORD', 'admin')
INBOUND_ID = int(os.getenv('INBOUND_ID', 12))

# Server
SERVER_IP = os.getenv('SERVER_IP', '185.128.104.219')  # Публичный IP сервера

# Domain
DOMAIN = os.getenv('DOMAIN', 'raphaelvpn.ru')

# Database
DATABASE_PATH = 'bot_database.db'

# WebApp (Mini App)
WEBAPP_HOST = os.getenv('WEBAPP_HOST', '0.0.0.0')
WEBAPP_PORT = int(os.getenv('WEBAPP_PORT', 8080))
WEBAPP_URL = os.getenv('WEBAPP_URL', f'https://{DOMAIN}:8080')  # URL для доступа к mini app

# Импортируем функцию для получения актуальных цен
from bot.price_config import get_subscription_periods

# Функция для получения периодов подписки (цены загружаются динамически)
def get_periods():
    """Получить актуальные периоды подписки с ценами"""
    return get_subscription_periods()

# Для обратной совместимости
SUBSCRIPTION_PERIODS = get_subscription_periods()
