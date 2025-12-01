"""
Главный файл бота для управления VPN ключами
"""
import asyncio
import logging
import sys
import os
import shutil
from datetime import datetime, time
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.types import FSInputFile

from bot.config import BOT_TOKEN, XUI_HOST, XUI_USERNAME, XUI_PASSWORD, DATABASE_PATH, WEBAPP_HOST, WEBAPP_PORT, ADMIN_ID, SSL_CERT, SSL_KEY
from bot.database import DatabaseManager
from bot.api import XUIClient
from bot.handlers import common, manager, admin
from bot.webapp.server import start_webapp_server, set_bot_instance

# Путь к базе данных X-UI
XUI_DB_PATH = Path("/etc/x-ui/x-ui.db")

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


async def daily_backup_task(bot: Bot):
    """Ежедневный бэкап базы X-UI в 2:00"""
    while True:
        try:
            now = datetime.now()
            # Вычисляем время до 2:00
            target_time = datetime.combine(now.date(), time(2, 0))
            # Используем timedelta из datetime
            from datetime import timedelta
            if now >= target_time:
                # Если уже прошло 2:00, планируем на следующий день
                target_time = target_time + timedelta(days=1)

            wait_seconds = (target_time - now).total_seconds()
            if wait_seconds < 0:
                wait_seconds = 86400 + wait_seconds  # 24 часа

            logger.info(f"Следующий бэкап через {wait_seconds/3600:.1f} часов")
            await asyncio.sleep(wait_seconds)

            # Выполняем бэкап
            await send_xui_backup(bot)

        except asyncio.CancelledError:
            logger.info("Задача бэкапа отменена")
            break
        except Exception as e:
            logger.error(f"Ошибка в задаче бэкапа: {e}")
            await asyncio.sleep(3600)  # Повторить через час при ошибке


async def send_xui_backup(bot: Bot):
    """Отправить бэкап базы X-UI админу"""
    try:
        if not XUI_DB_PATH.exists():
            logger.warning(f"База X-UI не найдена: {XUI_DB_PATH}")
            await bot.send_message(ADMIN_ID, "⚠️ База X-UI не найдена для бэкапа")
            return

        # Копируем файл с датой в имени
        backup_dir = Path("/root/manager_vpn/backups")
        backup_dir.mkdir(exist_ok=True)

        date_str = datetime.now().strftime("%Y-%m-%d_%H-%M")
        backup_file = backup_dir / f"x-ui_backup_{date_str}.db"

        shutil.copy2(XUI_DB_PATH, backup_file)

        # Отправляем файл админу
        document = FSInputFile(backup_file)
        await bot.send_document(
            ADMIN_ID,
            document,
            caption=f"💾 <b>Ежедневный бэкап X-UI</b>\n\n"
                    f"📅 Дата: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                    f"📦 Размер: {backup_file.stat().st_size / 1024:.1f} KB",
            parse_mode="HTML"
        )

        logger.info(f"Бэкап X-UI отправлен: {backup_file}")

        # Удаляем старые бэкапы (оставляем только 7 последних)
        backups = sorted(backup_dir.glob("x-ui_backup_*.db"), key=lambda x: x.stat().st_mtime, reverse=True)
        for old_backup in backups[7:]:
            old_backup.unlink()
            logger.info(f"Удалён старый бэкап: {old_backup}")

    except Exception as e:
        logger.error(f"Ошибка отправки бэкапа: {e}")
        try:
            await bot.send_message(ADMIN_ID, f"❌ Ошибка бэкапа X-UI: {e}")
        except:
            pass


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
        # Передаем бота и xui_client для уведомлений и миграции клиентов
        set_bot_instance(bot, ADMIN_ID, xui_client)
        webapp_runner = await start_webapp_server(WEBAPP_HOST, WEBAPP_PORT, SSL_CERT, SSL_KEY)
        logger.info("WebApp сервер запущен успешно")
    except Exception as e:
        logger.error(f"Ошибка запуска WebApp сервера: {e}")
        webapp_runner = None

    # Запуск задачи ежедневного бэкапа
    backup_task = asyncio.create_task(daily_backup_task(bot))
    logger.info("Задача ежедневного бэкапа X-UI запущена (в 2:00)")

    # Запуск бота
    try:
        logger.info("Бот запущен и готов к работе")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        backup_task.cancel()
        try:
            await backup_task
        except asyncio.CancelledError:
            pass
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
