"""
Аудит логирование действий пользователей и системы
"""
import aiosqlite
import json
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from enum import Enum

logger = logging.getLogger(__name__)


class AuditAction(Enum):
    """Типы действий для аудита"""
    # Клиенты
    CLIENT_CREATED = "client_created"
    CLIENT_UPDATED = "client_updated"
    CLIENT_DELETED = "client_deleted"
    CLIENT_SUSPENDED = "client_suspended"
    CLIENT_REACTIVATED = "client_reactivated"

    # Ключи
    KEY_CREATED = "key_created"
    KEY_EXTENDED = "key_extended"
    KEY_DELETED = "key_deleted"
    KEY_REPLACED = "key_replaced"

    # Серверы
    SERVER_ADDED = "server_added"
    SERVER_REMOVED = "server_removed"
    SERVER_SYNC = "server_sync"

    # Промокоды
    PROMO_CREATED = "promo_created"
    PROMO_USED = "promo_used"
    PROMO_DELETED = "promo_deleted"

    # Рефералы
    REFERRAL_CREATED = "referral_created"
    REFERRAL_BONUS_APPLIED = "referral_bonus_applied"

    # Менеджеры
    MANAGER_LOGIN = "manager_login"
    MANAGER_CREATED = "manager_created"
    MANAGER_UPDATED = "manager_updated"
    MANAGER_DELETED = "manager_deleted"

    # Настройки
    SETTINGS_UPDATED = "settings_updated"

    # Система
    SYSTEM_ERROR = "system_error"
    BACKUP_CREATED = "backup_created"


class EntityType(Enum):
    """Типы сущностей"""
    CLIENT = "client"
    KEY = "key"
    SERVER = "server"
    PROMO = "promo"
    REFERRAL = "referral"
    MANAGER = "manager"
    SETTINGS = "settings"
    SYSTEM = "system"


class AuditManager:
    """Менеджер аудит логов"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def log(
        self,
        user_id: int,
        action: str,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        old_value: Optional[Any] = None,
        new_value: Optional[Any] = None,
        details: Optional[str] = None,
        user_type: str = "manager",
        ip_address: Optional[str] = None
    ):
        """Записать событие в аудит лог"""
        try:
            # Сериализуем значения в JSON
            old_json = json.dumps(old_value, default=str, ensure_ascii=False) if old_value else None
            new_json = json.dumps(new_value, default=str, ensure_ascii=False) if new_value else None

            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT INTO audit_log
                       (user_id, user_type, action, entity_type, entity_id,
                        old_value, new_value, ip_address, details, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (user_id, user_type, action, entity_type, entity_id,
                     old_json, new_json, ip_address, details, datetime.now().isoformat())
                )
                await db.commit()

            logger.debug(f"Аудит: {action} пользователем {user_id}")

        except Exception as e:
            logger.error(f"Ошибка записи в аудит лог: {e}")

    async def log_client_action(
        self,
        manager_id: int,
        action: AuditAction,
        client_id: int,
        old_data: Optional[Dict] = None,
        new_data: Optional[Dict] = None,
        details: Optional[str] = None
    ):
        """Логирование действия с клиентом"""
        await self.log(
            user_id=manager_id,
            action=action.value,
            entity_type=EntityType.CLIENT.value,
            entity_id=client_id,
            old_value=old_data,
            new_value=new_data,
            details=details
        )

    async def log_key_action(
        self,
        manager_id: int,
        action: AuditAction,
        client_email: str,
        server: Optional[str] = None,
        details: Optional[str] = None
    ):
        """Логирование действия с ключом"""
        await self.log(
            user_id=manager_id,
            action=action.value,
            entity_type=EntityType.KEY.value,
            new_value={'email': client_email, 'server': server},
            details=details
        )

    async def log_promo_action(
        self,
        manager_id: int,
        action: AuditAction,
        promo_id: int,
        promo_code: str,
        details: Optional[str] = None
    ):
        """Логирование действия с промокодом"""
        await self.log(
            user_id=manager_id,
            action=action.value,
            entity_type=EntityType.PROMO.value,
            entity_id=promo_id,
            new_value={'code': promo_code},
            details=details
        )

    async def log_system_event(
        self,
        event: str,
        details: Optional[str] = None,
        error: Optional[str] = None
    ):
        """Логирование системного события"""
        await self.log(
            user_id=0,
            action=event,
            entity_type=EntityType.SYSTEM.value,
            user_type='system',
            details=details,
            new_value={'error': error} if error else None
        )

    async def get_logs(
        self,
        user_id: Optional[int] = None,
        action: Optional[str] = None,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
        limit: int = 100,
        offset: int = 0
    ) -> List[Dict[str, Any]]:
        """Получение логов с фильтрацией"""
        conditions = []
        params = []

        if user_id:
            conditions.append("user_id = ?")
            params.append(user_id)

        if action:
            conditions.append("action = ?")
            params.append(action)

        if entity_type:
            conditions.append("entity_type = ?")
            params.append(entity_type)

        if entity_id:
            conditions.append("entity_id = ?")
            params.append(entity_id)

        if from_date:
            conditions.append("created_at >= ?")
            params.append(from_date.isoformat())

        if to_date:
            conditions.append("created_at <= ?")
            params.append(to_date.isoformat())

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT * FROM audit_log
                    WHERE {where_clause}
                    ORDER BY created_at DESC
                    LIMIT ? OFFSET ?""",
                (*params, limit, offset)
            )
            rows = await cursor.fetchall()

            logs = []
            for row in rows:
                log_entry = dict(row)
                # Парсим JSON поля
                if log_entry.get('old_value'):
                    try:
                        log_entry['old_value'] = json.loads(log_entry['old_value'])
                    except json.JSONDecodeError:
                        pass
                if log_entry.get('new_value'):
                    try:
                        log_entry['new_value'] = json.loads(log_entry['new_value'])
                    except json.JSONDecodeError:
                        pass
                logs.append(log_entry)

            return logs

    async def get_user_activity(
        self,
        user_id: int,
        days: int = 30
    ) -> Dict[str, Any]:
        """Получение активности пользователя"""
        from_date = datetime.now() - timedelta(days=days)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Общее количество действий
            cursor = await db.execute(
                """SELECT COUNT(*) FROM audit_log
                   WHERE user_id = ? AND created_at >= ?""",
                (user_id, from_date.isoformat())
            )
            total_actions = (await cursor.fetchone())[0]

            # По типам действий
            cursor = await db.execute(
                """SELECT action, COUNT(*) as cnt FROM audit_log
                   WHERE user_id = ? AND created_at >= ?
                   GROUP BY action ORDER BY cnt DESC""",
                (user_id, from_date.isoformat())
            )
            actions_by_type = {row['action']: row['cnt'] for row in await cursor.fetchall()}

            # По дням
            cursor = await db.execute(
                """SELECT DATE(created_at) as date, COUNT(*) as cnt
                   FROM audit_log
                   WHERE user_id = ? AND created_at >= ?
                   GROUP BY DATE(created_at) ORDER BY date DESC""",
                (user_id, from_date.isoformat())
            )
            daily_activity = {row['date']: row['cnt'] for row in await cursor.fetchall()}

            # Последние действия
            cursor = await db.execute(
                """SELECT action, entity_type, created_at FROM audit_log
                   WHERE user_id = ?
                   ORDER BY created_at DESC LIMIT 10""",
                (user_id,)
            )
            recent = [dict(row) for row in await cursor.fetchall()]

            return {
                'user_id': user_id,
                'period_days': days,
                'total_actions': total_actions,
                'by_action': actions_by_type,
                'daily': daily_activity,
                'recent': recent
            }

    async def get_entity_history(
        self,
        entity_type: str,
        entity_id: int,
        limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Получение истории изменений сущности"""
        return await self.get_logs(
            entity_type=entity_type,
            entity_id=entity_id,
            limit=limit
        )

    async def get_stats(self, days: int = 7) -> Dict[str, Any]:
        """Статистика аудит логов"""
        from_date = datetime.now() - timedelta(days=days)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Общее количество
            cursor = await db.execute(
                "SELECT COUNT(*) FROM audit_log WHERE created_at >= ?",
                (from_date.isoformat(),)
            )
            total = (await cursor.fetchone())[0]

            # По действиям
            cursor = await db.execute(
                """SELECT action, COUNT(*) as cnt FROM audit_log
                   WHERE created_at >= ?
                   GROUP BY action ORDER BY cnt DESC LIMIT 10""",
                (from_date.isoformat(),)
            )
            top_actions = {row['action']: row['cnt'] for row in await cursor.fetchall()}

            # Самые активные пользователи
            cursor = await db.execute(
                """SELECT user_id, COUNT(*) as cnt FROM audit_log
                   WHERE created_at >= ? AND user_type = 'manager'
                   GROUP BY user_id ORDER BY cnt DESC LIMIT 5""",
                (from_date.isoformat(),)
            )
            top_users = {row['user_id']: row['cnt'] for row in await cursor.fetchall()}

            # Ошибки
            cursor = await db.execute(
                """SELECT COUNT(*) FROM audit_log
                   WHERE created_at >= ? AND action = 'system_error'""",
                (from_date.isoformat(),)
            )
            errors = (await cursor.fetchone())[0]

            return {
                'period_days': days,
                'total_events': total,
                'top_actions': top_actions,
                'top_users': top_users,
                'system_errors': errors
            }

    async def cleanup(self, days: int = 90) -> int:
        """Очистка старых логов"""
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        async with aiosqlite.connect(self.db_path) as db:
            result = await db.execute(
                "DELETE FROM audit_log WHERE created_at < ?",
                (cutoff,)
            )
            await db.commit()
            deleted = result.rowcount

        if deleted > 0:
            logger.info(f"Удалено {deleted} старых записей аудит лога")

        return deleted

    async def export_logs(
        self,
        from_date: datetime,
        to_date: datetime,
        format: str = 'json'
    ) -> str:
        """Экспорт логов"""
        logs = await self.get_logs(
            from_date=from_date,
            to_date=to_date,
            limit=10000
        )

        if format == 'json':
            return json.dumps(logs, default=str, ensure_ascii=False, indent=2)
        elif format == 'csv':
            import csv
            import io

            output = io.StringIO()
            if logs:
                writer = csv.DictWriter(output, fieldnames=logs[0].keys())
                writer.writeheader()
                writer.writerows(logs)

            return output.getvalue()
        else:
            raise ValueError(f"Неподдерживаемый формат: {format}")


# Удобный контекстный менеджер для аудита
class AuditContext:
    """Контекстный менеджер для группировки аудит событий"""

    def __init__(self, audit_manager: AuditManager, user_id: int):
        self.audit = audit_manager
        self.user_id = user_id
        self.events = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        # При ошибке логируем её
        if exc_type:
            await self.audit.log_system_event(
                'transaction_error',
                details=f"User: {self.user_id}",
                error=str(exc_val)
            )

    def log(self, action: str, **kwargs):
        """Добавить событие для логирования"""
        self.events.append({'action': action, **kwargs})
