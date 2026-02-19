"""
Менеджер клиентов - расширенное управление клиентами
"""
import aiosqlite
import logging
import json
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from .models import ClientStatus, SubscriptionAction, NotificationType

logger = logging.getLogger(__name__)


class ClientManager:
    """Менеджер для работы с клиентами"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    # ==================== CRUD КЛИЕНТОВ ====================

    async def create_client(
        self,
        uuid: str,
        email: str,
        phone: str = None,
        name: str = None,
        telegram_id: int = None,
        created_by: int = None,
        expire_days: int = 30,
        ip_limit: int = 2,
        group_id: int = None,
        referrer_code: str = None
    ) -> Optional[int]:
        """Создать нового клиента"""
        try:
            expire_time = int((datetime.now() + timedelta(days=expire_days)).timestamp() * 1000)

            async with aiosqlite.connect(self.db_path) as db:
                # Проверяем реферальный код
                referrer_id = None
                if referrer_code:
                    cursor = await db.execute(
                        "SELECT id FROM clients WHERE uuid = ? OR email = ?",
                        (referrer_code, referrer_code)
                    )
                    ref = await cursor.fetchone()
                    if ref:
                        referrer_id = ref[0]

                cursor = await db.execute(
                    """INSERT INTO clients
                       (uuid, email, phone, name, telegram_id, created_by,
                        expire_time, ip_limit, group_id, referrer_id, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')""",
                    (uuid, email, phone, name, telegram_id, created_by,
                     expire_time, ip_limit, group_id, referrer_id)
                )
                client_id = cursor.lastrowid

                # Записываем в историю
                await db.execute(
                    """INSERT INTO subscription_history
                       (client_id, action, days, new_expire, manager_id)
                       VALUES (?, 'created', ?, ?, ?)""",
                    (client_id, expire_days, expire_time, created_by)
                )

                # Если есть реферер - записываем
                if referrer_id:
                    await db.execute(
                        """INSERT INTO referrals (referrer_id, referred_id, referral_code)
                           VALUES (?, ?, ?)""",
                        (referrer_id, client_id, referrer_code)
                    )

                await db.commit()
                logger.info(f"Создан клиент {email} (ID: {client_id})")
                return client_id

        except Exception as e:
            logger.error(f"Ошибка создания клиента: {e}")
            return None

    async def get_client(self, client_id: int = None, uuid: str = None, email: str = None) -> Optional[Dict]:
        """Получить клиента по ID, UUID или email"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if client_id:
                cursor = await db.execute(
                    """SELECT c.*, g.name as group_name, g.discount_percent
                       FROM clients c
                       LEFT JOIN client_groups g ON c.group_id = g.id
                       WHERE c.id = ?""", (client_id,)
                )
            elif uuid:
                cursor = await db.execute(
                    """SELECT c.*, g.name as group_name, g.discount_percent
                       FROM clients c
                       LEFT JOIN client_groups g ON c.group_id = g.id
                       WHERE c.uuid = ?""", (uuid,)
                )
            elif email:
                cursor = await db.execute(
                    """SELECT c.*, g.name as group_name, g.discount_percent
                       FROM clients c
                       LEFT JOIN client_groups g ON c.group_id = g.id
                       WHERE c.email = ?""", (email,)
                )
            else:
                return None

            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_client(self, client_id: int, **kwargs) -> bool:
        """Обновить данные клиента"""
        if not kwargs:
            return False

        allowed_fields = ['phone', 'name', 'telegram_id', 'status', 'expire_time',
                          'ip_limit', 'group_id', 'current_server', 'total_traffic']
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}

        if not updates:
            return False

        try:
            async with aiosqlite.connect(self.db_path) as db:
                set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
                values = list(updates.values()) + [client_id]

                await db.execute(
                    f"UPDATE clients SET {set_clause}, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    values
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка обновления клиента {client_id}: {e}")
            return False

    async def delete_client(self, client_id: int, manager_id: int = None) -> bool:
        """Мягкое удаление клиента"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Получаем текущие данные
                cursor = await db.execute(
                    "SELECT expire_time FROM clients WHERE id = ?", (client_id,)
                )
                old = await cursor.fetchone()

                await db.execute(
                    "UPDATE clients SET status = 'deleted', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (client_id,)
                )

                # Записываем в историю
                await db.execute(
                    """INSERT INTO subscription_history
                       (client_id, action, old_expire, manager_id)
                       VALUES (?, 'deleted', ?, ?)""",
                    (client_id, old[0] if old else None, manager_id)
                )

                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка удаления клиента {client_id}: {e}")
            return False

    # ==================== ПРОДЛЕНИЕ ПОДПИСКИ ====================

    async def extend_subscription(
        self,
        client_id: int,
        days: int,
        price: int = 0,
        manager_id: int = None,
        promo_code: str = None,
        discount_amount: int = 0
    ) -> Optional[int]:
        """Продлить подписку клиента"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Получаем текущую дату истечения
                cursor = await db.execute(
                    "SELECT expire_time, status FROM clients WHERE id = ?", (client_id,)
                )
                row = await cursor.fetchone()
                if not row:
                    return None

                old_expire = row[0]
                current_status = row[1]

                # Вычисляем новую дату
                now_ms = int(datetime.now().timestamp() * 1000)
                if old_expire and old_expire > now_ms:
                    new_expire = old_expire + (days * 24 * 60 * 60 * 1000)
                else:
                    new_expire = now_ms + (days * 24 * 60 * 60 * 1000)

                # Обновляем клиента
                new_status = 'active' if current_status in ('expired', 'suspended') else current_status
                await db.execute(
                    """UPDATE clients
                       SET expire_time = ?, status = ?, updated_at = CURRENT_TIMESTAMP
                       WHERE id = ?""",
                    (new_expire, new_status, client_id)
                )

                # Записываем в историю
                await db.execute(
                    """INSERT INTO subscription_history
                       (client_id, action, days, price, old_expire, new_expire,
                        manager_id, promo_code, discount_amount)
                       VALUES (?, 'extended', ?, ?, ?, ?, ?, ?, ?)""",
                    (client_id, days, price, old_expire, new_expire,
                     manager_id, promo_code, discount_amount)
                )

                await db.commit()
                logger.info(f"Подписка клиента {client_id} продлена на {days} дней")
                return new_expire

        except Exception as e:
            logger.error(f"Ошибка продления подписки: {e}")
            return None

    async def suspend_client(self, client_id: int, manager_id: int = None, note: str = None) -> bool:
        """Приостановить подписку"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT expire_time FROM clients WHERE id = ?", (client_id,)
                )
                old = await cursor.fetchone()

                await db.execute(
                    "UPDATE clients SET status = 'suspended', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (client_id,)
                )

                await db.execute(
                    """INSERT INTO subscription_history
                       (client_id, action, old_expire, manager_id, note)
                       VALUES (?, 'suspended', ?, ?, ?)""",
                    (client_id, old[0] if old else None, manager_id, note)
                )

                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка приостановки клиента: {e}")
            return False

    async def reactivate_client(self, client_id: int, manager_id: int = None) -> bool:
        """Возобновить подписку"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    "SELECT expire_time FROM clients WHERE id = ?", (client_id,)
                )
                old = await cursor.fetchone()

                await db.execute(
                    "UPDATE clients SET status = 'active', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (client_id,)
                )

                await db.execute(
                    """INSERT INTO subscription_history
                       (client_id, action, old_expire, new_expire, manager_id)
                       VALUES (?, 'reactivated', ?, ?, ?)""",
                    (client_id, old[0] if old else None, old[0] if old else None, manager_id)
                )

                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка реактивации клиента: {e}")
            return False

    # ==================== СЕРВЕРЫ КЛИЕНТА ====================

    async def add_client_server(self, client_id: int, server_name: str, inbound_id: int = None) -> bool:
        """Добавить сервер клиенту"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    """INSERT OR REPLACE INTO client_servers (client_id, server_name, inbound_id, status)
                       VALUES (?, ?, ?, 'active')""",
                    (client_id, server_name, inbound_id)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка добавления сервера клиенту: {e}")
            return False

    async def get_client_servers(self, client_id: int) -> List[Dict]:
        """Получить серверы клиента"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM client_servers WHERE client_id = ? AND status = 'active'",
                (client_id,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def remove_client_server(self, client_id: int, server_name: str) -> bool:
        """Удалить сервер у клиента"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE client_servers SET status = 'deleted' WHERE client_id = ? AND server_name = ?",
                    (client_id, server_name)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка удаления сервера: {e}")
            return False

    # ==================== ПОИСК И ФИЛЬТРАЦИЯ ====================

    async def search_clients(
        self,
        query: str = None,
        status: str = None,
        manager_id: int = None,
        group_id: int = None,
        expiring_in_days: int = None,
        limit: int = 50,
        offset: int = 0
    ) -> List[Dict]:
        """Поиск клиентов с фильтрацией"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            conditions = []
            params = []

            if query:
                conditions.append("(c.email LIKE ? OR c.phone LIKE ? OR c.name LIKE ?)")
                pattern = f"%{query}%"
                params.extend([pattern, pattern, pattern])

            if status:
                conditions.append("c.status = ?")
                params.append(status)

            if manager_id:
                conditions.append("c.created_by = ?")
                params.append(manager_id)

            if group_id:
                conditions.append("c.group_id = ?")
                params.append(group_id)

            if expiring_in_days:
                future_ms = int((datetime.now() + timedelta(days=expiring_in_days)).timestamp() * 1000)
                now_ms = int(datetime.now().timestamp() * 1000)
                conditions.append("c.expire_time BETWEEN ? AND ?")
                params.extend([now_ms, future_ms])

            where_clause = " AND ".join(conditions) if conditions else "1=1"

            cursor = await db.execute(
                f"""SELECT c.*, g.name as group_name, m.username as manager_username
                    FROM clients c
                    LEFT JOIN client_groups g ON c.group_id = g.id
                    LEFT JOIN managers m ON c.created_by = m.user_id
                    WHERE {where_clause}
                    ORDER BY c.created_at DESC
                    LIMIT ? OFFSET ?""",
                params + [limit, offset]
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_expiring_clients(self, days: int = 7) -> List[Dict]:
        """Получить клиентов с истекающей подпиской"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            now_ms = int(datetime.now().timestamp() * 1000)
            future_ms = int((datetime.now() + timedelta(days=days)).timestamp() * 1000)

            cursor = await db.execute(
                """SELECT c.*, g.name as group_name
                   FROM clients c
                   LEFT JOIN client_groups g ON c.group_id = g.id
                   WHERE c.status = 'active'
                     AND c.expire_time BETWEEN ? AND ?
                   ORDER BY c.expire_time ASC""",
                (now_ms, future_ms)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_expired_clients(self) -> List[Dict]:
        """Получить клиентов с истёкшей подпиской"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            now_ms = int(datetime.now().timestamp() * 1000)

            cursor = await db.execute(
                """SELECT * FROM clients
                   WHERE status = 'active' AND expire_time < ?
                   ORDER BY expire_time ASC""",
                (now_ms,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== СТАТИСТИКА ====================

    async def get_client_stats(self, client_id: int) -> Dict:
        """Получить статистику клиента"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Основные данные
            cursor = await db.execute(
                "SELECT * FROM clients WHERE id = ?", (client_id,)
            )
            client = await cursor.fetchone()
            if not client:
                return {}

            # История подписок
            cursor = await db.execute(
                """SELECT action, COUNT(*) as count, SUM(price) as total_paid, SUM(days) as total_days
                   FROM subscription_history
                   WHERE client_id = ?
                   GROUP BY action""",
                (client_id,)
            )
            history_stats = {row['action']: dict(row) for row in await cursor.fetchall()}

            # Серверы
            cursor = await db.execute(
                "SELECT COUNT(*) FROM client_servers WHERE client_id = ? AND status = 'active'",
                (client_id,)
            )
            servers_count = (await cursor.fetchone())[0]

            # Рефералы (приглашённые этим клиентом)
            cursor = await db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?",
                (client_id,)
            )
            referrals_count = (await cursor.fetchone())[0]

            return {
                **dict(client),
                "history_stats": history_stats,
                "servers_count": servers_count,
                "referrals_count": referrals_count,
                "total_paid": sum(h.get('total_paid', 0) or 0 for h in history_stats.values()),
                "total_days": sum(h.get('total_days', 0) or 0 for h in history_stats.values())
            }

    async def get_clients_count(self, status: str = None) -> int:
        """Получить количество клиентов"""
        async with aiosqlite.connect(self.db_path) as db:
            if status:
                cursor = await db.execute(
                    "SELECT COUNT(*) FROM clients WHERE status = ?", (status,)
                )
            else:
                cursor = await db.execute("SELECT COUNT(*) FROM clients")
            return (await cursor.fetchone())[0]

    async def get_subscription_history(self, client_id: int, limit: int = 20) -> List[Dict]:
        """Получить историю подписок клиента"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT sh.*, m.username as manager_username
                   FROM subscription_history sh
                   LEFT JOIN managers m ON sh.manager_id = m.user_id
                   WHERE sh.client_id = ?
                   ORDER BY sh.created_at DESC
                   LIMIT ?""",
                (client_id, limit)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== ГРУППЫ ====================

    async def get_groups(self) -> List[Dict]:
        """Получить все группы клиентов"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM client_groups WHERE is_active = 1 ORDER BY priority DESC"
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def create_group(
        self,
        name: str,
        description: str = None,
        discount_percent: int = 0,
        priority: int = 0,
        color: str = None
    ) -> Optional[int]:
        """Создать группу клиентов"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    """INSERT INTO client_groups (name, description, discount_percent, priority, color)
                       VALUES (?, ?, ?, ?, ?)""",
                    (name, description, discount_percent, priority, color)
                )
                await db.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Ошибка создания группы: {e}")
            return None

    async def set_client_group(self, client_id: int, group_id: int) -> bool:
        """Установить группу клиенту"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE clients SET group_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (group_id, client_id)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка установки группы: {e}")
            return False
