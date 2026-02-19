"""
Модуль интеграции новых сервисов с ботом
"""
import os
import logging
from typing import Optional
from aiogram import Bot

from bot.database import (
    init_extended_tables,
    migrate_existing_data,
    ClientManager,
    PromoManager,
    ReferralManager,
    AuditManager,
    AnalyticsManager
)
from bot.services import NotificationService, CacheManager
from bot.services.scheduler import SchedulerService

logger = logging.getLogger(__name__)


class BotServices:
    """Контейнер для всех сервисов бота"""

    def __init__(self, db_path: str, bot: Optional[Bot] = None):
        self.db_path = db_path
        self.bot = bot

        # Менеджеры данных
        self.client_manager = ClientManager(db_path)
        self.promo_manager = PromoManager(db_path)
        self.referral_manager = ReferralManager(db_path)
        self.audit_manager = AuditManager(db_path)
        self.analytics_manager = AnalyticsManager(db_path)

        # Кэш
        self.cache = CacheManager(db_path)

        # Уведомления (требуют bot)
        self.notification_service = None
        if bot:
            self.notification_service = NotificationService(db_path, bot)

        # Планировщик
        self.scheduler = None

    async def init_database(self):
        """Инициализация расширенных таблиц БД"""
        try:
            await init_extended_tables(self.db_path)
            logger.info("Расширенные таблицы БД инициализированы")

            # Миграция существующих данных
            await migrate_existing_data(self.db_path)
            logger.info("Миграция данных завершена")

        except Exception as e:
            logger.error(f"Ошибка инициализации БД: {e}")
            raise

    async def start_scheduler(self):
        """Запуск планировщика фоновых задач"""
        if not self.notification_service:
            logger.warning("NotificationService не настроен, планировщик будет ограничен")

        self.scheduler = SchedulerService(
            db_path=self.db_path,
            notification_service=self.notification_service,
            client_manager=self.client_manager
        )

        await self.scheduler.start()
        logger.info("Планировщик задач запущен")

    async def stop_scheduler(self):
        """Остановка планировщика"""
        if self.scheduler:
            await self.scheduler.stop()
            logger.info("Планировщик задач остановлен")

    async def start_rest_api(self, host: str = '0.0.0.0', port: int = 8081):
        """Запуск REST API сервера"""
        from bot.api.rest_api import RestAPI

        api_key = os.getenv('API_KEY')
        api = RestAPI(
            client_manager=self.client_manager,
            promo_manager=self.promo_manager,
            analytics_manager=self.analytics_manager,
            audit_manager=self.audit_manager,
            api_key=api_key
        )

        runner = await api.start_background(host, port)
        logger.info(f"REST API запущен на http://{host}:{port}")
        return runner

    def set_bot(self, bot: Bot):
        """Установка экземпляра бота для уведомлений"""
        self.bot = bot
        self.notification_service = NotificationService(self.db_path, bot)

        # Обновляем планировщик если он уже запущен
        if self.scheduler:
            self.scheduler.notification_service = self.notification_service


# Глобальный экземпляр сервисов (инициализируется в main.py)
services: Optional[BotServices] = None


async def init_services(db_path: str, bot: Optional[Bot] = None) -> BotServices:
    """Инициализация всех сервисов"""
    global services

    services = BotServices(db_path, bot)
    await services.init_database()

    logger.info("Сервисы бота инициализированы")
    return services


def get_services() -> Optional[BotServices]:
    """Получение экземпляра сервисов"""
    return services
