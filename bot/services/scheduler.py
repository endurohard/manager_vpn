"""
Планировщик фоновых задач
"""
import asyncio
import aiosqlite
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, Callable, Optional, List
from enum import Enum

logger = logging.getLogger(__name__)


class TaskType(Enum):
    SEND_NOTIFICATIONS = "send_notifications"
    CHECK_EXPIRED = "check_expired"
    CLEANUP_CACHE = "cleanup_cache"
    SYNC_SERVERS = "sync_servers"
    DAILY_STATS = "daily_stats"
    CUSTOM = "custom"


class Scheduler:
    """Планировщик фоновых задач"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._running = False
        self._tasks: Dict[str, asyncio.Task] = {}
        self._handlers: Dict[str, Callable] = {}
        self._intervals: Dict[str, int] = {
            TaskType.SEND_NOTIFICATIONS.value: 60,      # каждую минуту
            TaskType.CHECK_EXPIRED.value: 300,          # каждые 5 минут
            TaskType.CLEANUP_CACHE.value: 3600,         # каждый час
            TaskType.SYNC_SERVERS.value: 600,           # каждые 10 минут
            TaskType.DAILY_STATS.value: 86400,          # раз в сутки
        }

    def register_handler(self, task_type: str, handler: Callable):
        """Регистрация обработчика задачи"""
        self._handlers[task_type] = handler
        logger.info(f"Зарегистрирован обработчик для {task_type}")

    def set_interval(self, task_type: str, seconds: int):
        """Установка интервала для задачи"""
        self._intervals[task_type] = seconds

    async def start(self):
        """Запуск планировщика"""
        if self._running:
            logger.warning("Планировщик уже запущен")
            return

        self._running = True
        logger.info("Запуск планировщика задач")

        # Запускаем периодические задачи
        for task_type, interval in self._intervals.items():
            if task_type in self._handlers:
                self._tasks[task_type] = asyncio.create_task(
                    self._run_periodic(task_type, interval)
                )

        # Запускаем обработчик запланированных задач из БД
        self._tasks['db_scheduler'] = asyncio.create_task(self._run_db_scheduler())

    async def stop(self):
        """Остановка планировщика"""
        self._running = False
        logger.info("Остановка планировщика задач")

        for task_name, task in self._tasks.items():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        self._tasks.clear()

    async def _run_periodic(self, task_type: str, interval: int):
        """Периодический запуск задачи"""
        while self._running:
            try:
                handler = self._handlers.get(task_type)
                if handler:
                    logger.debug(f"Выполнение задачи {task_type}")
                    await handler()
            except Exception as e:
                logger.error(f"Ошибка выполнения задачи {task_type}: {e}")

            await asyncio.sleep(interval)

    async def _run_db_scheduler(self):
        """Обработка запланированных задач из БД"""
        while self._running:
            try:
                await self._process_scheduled_tasks()
            except Exception as e:
                logger.error(f"Ошибка обработки задач из БД: {e}")

            await asyncio.sleep(30)  # проверка каждые 30 секунд

    async def _process_scheduled_tasks(self):
        """Обработка задач из таблицы scheduled_tasks"""
        now = datetime.now().isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Получаем задачи для выполнения
            cursor = await db.execute(
                """SELECT id, task_type, task_data, retry_count
                   FROM scheduled_tasks
                   WHERE status = 'pending' AND scheduled_at <= ?
                   ORDER BY scheduled_at
                   LIMIT 50""",
                (now,)
            )
            tasks = await cursor.fetchall()

            for task in tasks:
                await self._execute_scheduled_task(db, dict(task))

    async def _execute_scheduled_task(self, db: aiosqlite.Connection, task: Dict[str, Any]):
        """Выполнение запланированной задачи"""
        task_id = task['id']
        task_type = task['task_type']

        try:
            # Парсим данные задачи
            task_data = {}
            if task['task_data']:
                task_data = json.loads(task['task_data'])

            # Получаем обработчик
            handler = self._handlers.get(task_type)
            if not handler:
                raise ValueError(f"Нет обработчика для {task_type}")

            # Выполняем
            result = await handler(task_data)

            # Успех
            await db.execute(
                """UPDATE scheduled_tasks
                   SET status = 'completed', executed_at = ?, result = ?
                   WHERE id = ?""",
                (datetime.now().isoformat(), json.dumps(result) if result else None, task_id)
            )
            await db.commit()

            logger.info(f"Задача {task_id} ({task_type}) выполнена успешно")

        except Exception as e:
            retry_count = task.get('retry_count', 0) + 1
            max_retries = 3

            if retry_count >= max_retries:
                status = 'failed'
            else:
                status = 'pending'

            await db.execute(
                """UPDATE scheduled_tasks
                   SET status = ?, error = ?, retry_count = ?
                   WHERE id = ?""",
                (status, str(e), retry_count, task_id)
            )
            await db.commit()

            logger.error(f"Ошибка задачи {task_id}: {e} (попытка {retry_count})")

    async def schedule_task(
        self,
        task_type: str,
        scheduled_at: datetime,
        task_data: Optional[Dict] = None
    ) -> int:
        """Планирование задачи"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """INSERT INTO scheduled_tasks
                   (task_type, task_data, scheduled_at, status)
                   VALUES (?, ?, ?, 'pending')""",
                (task_type, json.dumps(task_data) if task_data else None,
                 scheduled_at.isoformat())
            )
            await db.commit()
            task_id = cursor.lastrowid

        logger.info(f"Запланирована задача {task_id} ({task_type}) на {scheduled_at}")
        return task_id

    async def cancel_task(self, task_id: int) -> bool:
        """Отмена задачи"""
        async with aiosqlite.connect(self.db_path) as db:
            result = await db.execute(
                """UPDATE scheduled_tasks SET status = 'cancelled'
                   WHERE id = ? AND status = 'pending'""",
                (task_id,)
            )
            await db.commit()
            return result.rowcount > 0

    async def get_task_stats(self) -> Dict[str, Any]:
        """Статистика задач"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # По статусам
            cursor = await db.execute(
                "SELECT status, COUNT(*) as cnt FROM scheduled_tasks GROUP BY status"
            )
            status_stats = {row['status']: row['cnt'] for row in await cursor.fetchall()}

            # По типам
            cursor = await db.execute(
                "SELECT task_type, COUNT(*) as cnt FROM scheduled_tasks GROUP BY task_type"
            )
            type_stats = {row['task_type']: row['cnt'] for row in await cursor.fetchall()}

            # Ожидающие задачи
            cursor = await db.execute(
                """SELECT task_type, scheduled_at FROM scheduled_tasks
                   WHERE status = 'pending' ORDER BY scheduled_at LIMIT 10"""
            )
            pending = [dict(row) for row in await cursor.fetchall()]

            return {
                'by_status': status_stats,
                'by_type': type_stats,
                'pending_tasks': pending,
                'total_pending': status_stats.get('pending', 0),
                'total_completed': status_stats.get('completed', 0),
                'total_failed': status_stats.get('failed', 0)
            }

    async def cleanup_old_tasks(self, days: int = 30):
        """Очистка старых задач"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            result = await db.execute(
                """DELETE FROM scheduled_tasks
                   WHERE status IN ('completed', 'cancelled', 'failed')
                   AND executed_at < ?""",
                (cutoff,)
            )
            await db.commit()
            deleted = result.rowcount

        if deleted > 0:
            logger.info(f"Удалено {deleted} старых задач")

        return deleted


class SchedulerService:
    """Сервис планировщика с предустановленными задачами"""

    def __init__(self, db_path: str, notification_service=None, client_manager=None):
        self.scheduler = Scheduler(db_path)
        self.db_path = db_path
        self.notification_service = notification_service
        self.client_manager = client_manager
        self._setup_handlers()

    def _setup_handlers(self):
        """Настройка обработчиков задач"""
        # Отправка уведомлений
        if self.notification_service:
            self.scheduler.register_handler(
                TaskType.SEND_NOTIFICATIONS.value,
                self._handle_notifications
            )

        # Проверка истекших подписок
        if self.client_manager:
            self.scheduler.register_handler(
                TaskType.CHECK_EXPIRED.value,
                self._handle_check_expired
            )

        # Очистка кэша
        self.scheduler.register_handler(
            TaskType.CLEANUP_CACHE.value,
            self._handle_cleanup_cache
        )

        # Ежедневная статистика
        self.scheduler.register_handler(
            TaskType.DAILY_STATS.value,
            self._handle_daily_stats
        )

    async def _handle_notifications(self, data: Dict = None):
        """Обработка уведомлений"""
        if self.notification_service:
            return await self.notification_service.process_pending_notifications()

    async def _handle_check_expired(self, data: Dict = None):
        """Проверка истекших подписок"""
        if not self.client_manager:
            return

        expired = await self.client_manager.get_expired_clients()
        updated = 0

        for client in expired:
            if client.get('status') != 'expired':
                await self.client_manager.update_client(
                    client['id'],
                    status='expired'
                )
                updated += 1

        return {'checked': len(expired), 'updated': updated}

    async def _handle_cleanup_cache(self, data: Dict = None):
        """Очистка устаревшего кэша"""
        now = datetime.now().isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            result = await db.execute(
                "DELETE FROM cache WHERE expires_at < ?",
                (now,)
            )
            await db.commit()
            deleted = result.rowcount

        # Также очищаем старые задачи
        await self.scheduler.cleanup_old_tasks()

        return {'cache_cleaned': deleted}

    async def _handle_daily_stats(self, data: Dict = None):
        """Сбор ежедневной статистики"""
        today = datetime.now().strftime('%Y-%m-%d')

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Собираем статистику из subscription_history
            cursor = await db.execute(
                """SELECT
                     SUM(CASE WHEN action = 'created' THEN 1 ELSE 0 END) as keys_created,
                     SUM(CASE WHEN action = 'extended' THEN 1 ELSE 0 END) as keys_extended,
                     SUM(CASE WHEN action = 'deleted' THEN 1 ELSE 0 END) as keys_deleted,
                     SUM(price) as revenue
                   FROM subscription_history
                   WHERE DATE(created_at) = ?""",
                (today,)
            )
            row = await cursor.fetchone()
            stats = dict(row) if row else {}

            # Истекшие сегодня
            cursor = await db.execute(
                """SELECT COUNT(*) FROM clients
                   WHERE status = 'expired'
                   AND DATE(updated_at) = ?""",
                (today,)
            )
            stats['keys_expired'] = (await cursor.fetchone())[0]

            # Новые клиенты
            cursor = await db.execute(
                """SELECT COUNT(*) FROM clients
                   WHERE DATE(created_at) = ?""",
                (today,)
            )
            stats['new_clients'] = (await cursor.fetchone())[0]

            # Активные клиенты
            cursor = await db.execute(
                "SELECT COUNT(*) FROM clients WHERE status = 'active'"
            )
            stats['active_clients'] = (await cursor.fetchone())[0]

            # Промокоды
            cursor = await db.execute(
                """SELECT COUNT(*), COALESCE(SUM(discount_amount), 0)
                   FROM promo_uses WHERE DATE(used_at) = ?""",
                (today,)
            )
            promo_row = await cursor.fetchone()
            stats['promo_uses'] = promo_row[0]
            stats['promo_discount_total'] = promo_row[1]

            # Рефералы
            cursor = await db.execute(
                """SELECT COUNT(*) FROM referrals
                   WHERE DATE(created_at) = ?""",
                (today,)
            )
            stats['referrals_count'] = (await cursor.fetchone())[0]

            # Сохраняем
            await db.execute(
                """INSERT OR REPLACE INTO daily_stats
                   (date, keys_created, keys_extended, keys_expired, keys_deleted,
                    revenue, new_clients, active_clients, promo_uses,
                    promo_discount_total, referrals_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (today, stats.get('keys_created', 0), stats.get('keys_extended', 0),
                 stats.get('keys_expired', 0), stats.get('keys_deleted', 0),
                 stats.get('revenue', 0), stats.get('new_clients', 0),
                 stats.get('active_clients', 0), stats.get('promo_uses', 0),
                 stats.get('promo_discount_total', 0), stats.get('referrals_count', 0))
            )
            await db.commit()

        logger.info(f"Собрана дневная статистика за {today}")
        return stats

    async def start(self):
        """Запуск сервиса"""
        await self.scheduler.start()

    async def stop(self):
        """Остановка сервиса"""
        await self.scheduler.stop()
