"""
Сервис уведомлений для клиентов и менеджеров
"""
import aiosqlite
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from aiogram import Bot

logger = logging.getLogger(__name__)


class NotificationService:
    """Сервис уведомлений об истечении подписок"""

    def __init__(self, db_path: str, bot: Bot):
        self.db_path = db_path
        self.bot = bot
        self._settings_cache = {}

    async def get_settings(self) -> Dict[str, str]:
        """Получение настроек уведомлений"""
        if self._settings_cache:
            return self._settings_cache

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT setting_key, setting_value FROM notification_settings"
            )
            rows = await cursor.fetchall()
            self._settings_cache = {row['setting_key']: row['setting_value'] for row in rows}

        return self._settings_cache

    async def update_setting(self, key: str, value: str):
        """Обновление настройки"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO notification_settings
                   (setting_key, setting_value, updated_at)
                   VALUES (?, ?, ?)""",
                (key, value, datetime.now().isoformat())
            )
            await db.commit()

        self._settings_cache[key] = value

    async def get_expiry_warning_days(self) -> List[int]:
        """Получение списка дней для предупреждений"""
        settings = await self.get_settings()
        days_str = settings.get('expiry_warning_days', '7,3,1')
        return [int(d.strip()) for d in days_str.split(',') if d.strip().isdigit()]

    async def schedule_expiry_notifications(self, client_id: int, expire_time: int):
        """Планирование уведомлений об истечении для клиента"""
        warning_days = await self.get_expiry_warning_days()
        expire_dt = datetime.fromtimestamp(expire_time / 1000)

        async with aiosqlite.connect(self.db_path) as db:
            for days in warning_days:
                scheduled_at = expire_dt - timedelta(days=days)

                # Не планируем уведомления в прошлом
                if scheduled_at <= datetime.now():
                    continue

                # Проверяем, не запланировано ли уже
                cursor = await db.execute(
                    """SELECT id FROM notifications
                       WHERE client_id = ? AND type = 'expiry_warning'
                       AND days_before = ? AND status = 'pending'""",
                    (client_id, days)
                )
                if await cursor.fetchone():
                    continue

                await db.execute(
                    """INSERT INTO notifications
                       (client_id, type, title, message, days_before, scheduled_at, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        client_id,
                        'expiry_warning',
                        f'Подписка истекает через {days} дн.',
                        f'Ваша подписка истекает через {days} дней. Продлите её заранее!',
                        days,
                        scheduled_at.isoformat(),
                        'pending'
                    )
                )

            # Уведомление об истечении
            await db.execute(
                """INSERT INTO notifications
                   (client_id, type, title, message, scheduled_at, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    client_id,
                    'expired',
                    'Подписка истекла',
                    'Ваша подписка истекла. Продлите её для продолжения использования.',
                    expire_dt.isoformat(),
                    'pending'
                )
            )

            await db.commit()
            logger.info(f"Запланированы уведомления для клиента {client_id}")

    async def cancel_notifications(self, client_id: int):
        """Отмена всех запланированных уведомлений клиента"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE notifications SET status = 'cancelled'
                   WHERE client_id = ? AND status = 'pending'""",
                (client_id,)
            )
            await db.commit()

    async def get_pending_notifications(self) -> List[Dict[str, Any]]:
        """Получение уведомлений для отправки"""
        now = datetime.now().isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT n.*, c.telegram_id, c.email, c.name, c.created_by as manager_id
                   FROM notifications n
                   JOIN clients c ON n.client_id = c.id
                   WHERE n.status = 'pending' AND n.scheduled_at <= ?
                   ORDER BY n.scheduled_at
                   LIMIT 100""",
                (now,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def send_notification(self, notification: Dict[str, Any]) -> bool:
        """Отправка уведомления"""
        settings = await self.get_settings()
        success = True
        error_msg = None

        try:
            # Отправка клиенту
            if settings.get('send_to_client', 'true') == 'true':
                telegram_id = notification.get('telegram_id')
                if telegram_id:
                    try:
                        message = self._format_client_message(notification)
                        await self.bot.send_message(telegram_id, message)
                        logger.info(f"Уведомление отправлено клиенту {telegram_id}")
                    except Exception as e:
                        logger.warning(f"Не удалось отправить клиенту {telegram_id}: {e}")
                        error_msg = str(e)

            # Отправка менеджеру
            if settings.get('send_to_manager', 'true') == 'true':
                manager_id = notification.get('manager_id')
                if manager_id:
                    try:
                        message = self._format_manager_message(notification)
                        await self.bot.send_message(manager_id, message)
                        logger.info(f"Уведомление отправлено менеджеру {manager_id}")
                    except Exception as e:
                        logger.warning(f"Не удалось отправить менеджеру {manager_id}: {e}")
                        if not error_msg:
                            error_msg = str(e)

        except Exception as e:
            logger.error(f"Ошибка отправки уведомления: {e}")
            success = False
            error_msg = str(e)

        # Обновляем статус
        await self._update_notification_status(
            notification['id'],
            'sent' if success else 'failed',
            error_msg
        )

        return success

    def _format_client_message(self, notification: Dict[str, Any]) -> str:
        """Форматирование сообщения для клиента"""
        ntype = notification.get('type', '')

        if ntype == 'expiry_warning':
            days = notification.get('days_before', 0)
            return (
                f"⚠️ *Уведомление о подписке*\n\n"
                f"Ваша VPN подписка истекает через *{days}* дн.\n"
                f"Продлите её заранее, чтобы не потерять доступ!\n\n"
                f"Для продления обратитесь к менеджеру."
            )
        elif ntype == 'expired':
            return (
                f"❌ *Подписка истекла*\n\n"
                f"Ваша VPN подписка истекла.\n"
                f"Для продолжения использования продлите подписку.\n\n"
                f"Обратитесь к менеджеру для продления."
            )
        elif ntype == 'welcome':
            return notification.get('message', 'Добро пожаловать!')
        else:
            return notification.get('message', '')

    def _format_manager_message(self, notification: Dict[str, Any]) -> str:
        """Форматирование сообщения для менеджера"""
        ntype = notification.get('type', '')
        client_name = notification.get('name') or notification.get('email', 'Клиент')

        if ntype == 'expiry_warning':
            days = notification.get('days_before', 0)
            return (
                f"📋 *Напоминание*\n\n"
                f"У клиента *{client_name}* подписка истекает через *{days}* дн.\n"
                f"Email: `{notification.get('email', 'N/A')}`"
            )
        elif ntype == 'expired':
            return (
                f"⏰ *Подписка истекла*\n\n"
                f"У клиента *{client_name}* истекла подписка.\n"
                f"Email: `{notification.get('email', 'N/A')}`"
            )
        else:
            return f"Клиент {client_name}: {notification.get('message', '')}"

    async def _update_notification_status(self, notification_id: int, status: str, error: Optional[str] = None):
        """Обновление статуса уведомления"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE notifications
                   SET status = ?, sent_at = ?, error = ?
                   WHERE id = ?""",
                (status, datetime.now().isoformat(), error, notification_id)
            )
            await db.commit()

    async def send_welcome_notification(self, client_id: int, telegram_id: int, key_link: str):
        """Отправка приветственного уведомления"""
        settings = await self.get_settings()
        if settings.get('welcome_message', 'true') != 'true':
            return

        try:
            message = (
                f"🎉 *Добро пожаловать!*\n\n"
                f"Ваш VPN ключ успешно создан.\n\n"
                f"📱 *Как подключиться:*\n"
                f"1. Скачайте приложение V2RayNG (Android) или Streisand (iOS)\n"
                f"2. Скопируйте ключ ниже\n"
                f"3. Добавьте конфигурацию в приложении\n\n"
                f"🔑 Ваш ключ:\n`{key_link}`\n\n"
                f"По вопросам обращайтесь к менеджеру."
            )
            await self.bot.send_message(telegram_id, message, parse_mode='Markdown')

            # Записываем в БД
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO notifications
                       (client_id, type, title, message, sent_at, status)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (client_id, 'welcome', 'Добро пожаловать', message,
                     datetime.now().isoformat(), 'sent')
                )
                await db.commit()

            logger.info(f"Приветственное уведомление отправлено {telegram_id}")

        except Exception as e:
            logger.error(f"Ошибка отправки приветствия {telegram_id}: {e}")

    async def get_notification_stats(self) -> Dict[str, Any]:
        """Статистика уведомлений"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Общая статистика
            cursor = await db.execute(
                """SELECT status, COUNT(*) as cnt FROM notifications GROUP BY status"""
            )
            status_stats = {row['status']: row['cnt'] for row in await cursor.fetchall()}

            # По типам
            cursor = await db.execute(
                """SELECT type, COUNT(*) as cnt FROM notifications GROUP BY type"""
            )
            type_stats = {row['type']: row['cnt'] for row in await cursor.fetchall()}

            # За последние 7 дней
            week_ago = (datetime.now() - timedelta(days=7)).isoformat()
            cursor = await db.execute(
                """SELECT COUNT(*) FROM notifications WHERE sent_at >= ?""",
                (week_ago,)
            )
            sent_last_week = (await cursor.fetchone())[0]

            return {
                'by_status': status_stats,
                'by_type': type_stats,
                'sent_last_week': sent_last_week,
                'pending': status_stats.get('pending', 0),
                'total_sent': status_stats.get('sent', 0),
                'failed': status_stats.get('failed', 0)
            }

    async def process_pending_notifications(self) -> int:
        """Обработка всех ожидающих уведомлений"""
        notifications = await self.get_pending_notifications()
        sent_count = 0

        for notification in notifications:
            if await self.send_notification(notification):
                sent_count += 1

        if sent_count > 0:
            logger.info(f"Отправлено {sent_count} уведомлений")

        return sent_count
