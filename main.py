"""
Главный файл бота для управления VPN ключами
"""
import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties

from bot.config import BOT_TOKEN, XUI_HOST, XUI_USERNAME, XUI_PASSWORD, DATABASE_PATH, WEBAPP_HOST, WEBAPP_PORT, ADMIN_ID
from bot.database import DatabaseManager
from bot.api import XUIClient
from bot.handlers import common, manager, admin
from bot.webapp.server import start_webapp_server

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)


async def main():
    """Основная функция запуска бота"""

    # Проверка конфигурации
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не установлен в .env файле!")
        return

    if not XUI_HOST:
        logger.error("XUI_HOST не установлен в .env файле!")
        return

    logger.info("Запуск бота...")

    # Инициализация бота и диспетчера
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Инициализация базы данных
    db = DatabaseManager(DATABASE_PATH)
    await db.init_db()
    logger.info("База данных инициализирована")

    # Автоматически добавляем админа как менеджера, если его нет
    if not await db.is_manager(ADMIN_ID):
        await db.add_manager(
            user_id=ADMIN_ID,
            username="admin",
            full_name="Администратор",
            added_by=ADMIN_ID
        )
        logger.info(f"Админ (ID: {ADMIN_ID}) автоматически добавлен в менеджеры")

    # Инициализация X-UI клиента
    xui_client = XUIClient(XUI_HOST, XUI_USERNAME, XUI_PASSWORD)

    # Проверка подключения к X-UI
    try:
        login_success = await xui_client.login()
        if login_success:
            logger.info("Успешное подключение к X-UI панели")
        else:
            logger.warning("Не удалось подключиться к X-UI панели. Проверьте настройки.")
    except Exception as e:
        logger.error(f"Ошибка подключения к X-UI: {e}")

    # Middleware для передачи зависимостей
    @dp.update.middleware()
    async def db_middleware(handler, event, data):
        data['db'] = db
        data['xui_client'] = xui_client
        data['bot'] = bot
        return await handler(event, data)

    # Регистрация роутеров
    dp.include_router(common.router)
    dp.include_router(manager.router)
    dp.include_router(admin.router)

    logger.info("Обработчики зарегистрированы")

    # Запуск веб-сервера для Mini App
    try:
        webapp_runner = await start_webapp_server(WEBAPP_HOST, WEBAPP_PORT)
        logger.info("WebApp сервер запущен успешно")
    except Exception as e:
        logger.error(f"Ошибка запуска WebApp сервера: {e}")
        webapp_runner = None

    # Запуск бота
    try:
        logger.info("Бот запущен и готов к работе")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        if xui_client.session:
            await xui_client.session.close()
        if webapp_runner:
            await webapp_runner.cleanup()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
