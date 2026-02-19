"""
Менеджер базы данных для хранения информации о менеджерах и ключах
"""
import aiosqlite
import logging
from datetime import datetime
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init_db(self):
        """Инициализация базы данных"""
        async with aiosqlite.connect(self.db_path) as db:
            # Таблица менеджеров
            await db.execute('''
                CREATE TABLE IF NOT EXISTS managers (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    added_by INTEGER,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER DEFAULT 1
                )
            ''')

            # Таблица созданных ключей
            await db.execute('''
                CREATE TABLE IF NOT EXISTS keys_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    manager_id INTEGER,
                    client_email TEXT,
                    phone_number TEXT,
                    period TEXT,
                    expire_days INTEGER,
                    client_id TEXT,
                    price INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (manager_id) REFERENCES managers (user_id)
                )
            ''')

            # Добавляем колонку price если её нет (для обновления существующих баз)
            try:
                await db.execute('ALTER TABLE keys_history ADD COLUMN price INTEGER DEFAULT 0')
            except Exception:
                pass  # Колонка уже существует

            # Добавляем колонку custom_name для пользовательских имен менеджеров
            try:
                await db.execute('ALTER TABLE managers ADD COLUMN custom_name TEXT')
            except Exception:
                pass  # Колонка уже существует

            # Таблица замен ключей (без цены)
            await db.execute('''
                CREATE TABLE IF NOT EXISTS key_replacements (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    manager_id INTEGER,
                    client_email TEXT,
                    phone_number TEXT,
                    period TEXT,
                    expire_days INTEGER,
                    client_id TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (manager_id) REFERENCES managers (user_id)
                )
            ''')

            # Таблица отложенных ключей (для retry при ошибках)
            await db.execute('''
                CREATE TABLE IF NOT EXISTS pending_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER,
                    username TEXT,
                    phone TEXT,
                    period_key TEXT,
                    period_name TEXT,
                    period_days INTEGER,
                    period_price INTEGER DEFAULT 0,
                    inbound_id INTEGER,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 5,
                    last_error TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_retry_at TIMESTAMP,
                    completed_at TIMESTAMP
                )
            ''')

            # ==================== ИНДЕКСЫ ДЛЯ ОПТИМИЗАЦИИ ЗАПРОСОВ ====================
            # Индексы для keys_history - основная таблица с большим количеством записей
            await db.execute('CREATE INDEX IF NOT EXISTS idx_keys_history_manager_id ON keys_history(manager_id)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_keys_history_created_at ON keys_history(created_at)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_keys_history_client_email ON keys_history(client_email)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_keys_history_phone_number ON keys_history(phone_number)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_keys_history_manager_date ON keys_history(manager_id, created_at)')

            # Индексы для pending_keys - частые запросы по статусу
            await db.execute('CREATE INDEX IF NOT EXISTS idx_pending_keys_status ON pending_keys(status)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_pending_keys_telegram_id ON pending_keys(telegram_id)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_pending_keys_status_retry ON pending_keys(status, retry_count)')

            # Индексы для key_replacements
            await db.execute('CREATE INDEX IF NOT EXISTS idx_key_replacements_manager_id ON key_replacements(manager_id)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_key_replacements_created_at ON key_replacements(created_at)')

            # Индекс для managers
            await db.execute('CREATE INDEX IF NOT EXISTS idx_managers_is_active ON managers(is_active)')

            # Таблица связанных ключей (для привязки нескольких ключей к одной подписке)
            await db.execute('''
                CREATE TABLE IF NOT EXISTS linked_clients (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    master_uuid TEXT NOT NULL,
                    linked_uuid TEXT NOT NULL,
                    linked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(master_uuid, linked_uuid)
                )
            ''')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_linked_clients_master ON linked_clients(master_uuid)')

            logger.info("Индексы базы данных созданы/проверены")

            await db.commit()

    async def add_manager(self, user_id: int, username: str, full_name: str, added_by: int) -> bool:
        """Добавить нового менеджера"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    'INSERT OR REPLACE INTO managers (user_id, username, full_name, added_by) VALUES (?, ?, ?, ?)',
                    (user_id, username, full_name, added_by)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding manager: {e}")
            return False

    async def remove_manager(self, user_id: int) -> bool:
        """Удалить менеджера"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    'UPDATE managers SET is_active = 0 WHERE user_id = ?',
                    (user_id,)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error removing manager: {e}")
            return False

    async def is_manager(self, user_id: int) -> bool:
        """Проверить, является ли пользователь менеджером"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'SELECT user_id FROM managers WHERE user_id = ? AND is_active = 1',
                (user_id,)
            )
            result = await cursor.fetchone()
            return result is not None

    async def update_manager_info(self, user_id: int, username: str, full_name: str) -> bool:
        """Обновить информацию о менеджере (username и full_name)"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    'UPDATE managers SET username = ?, full_name = ? WHERE user_id = ?',
                    (username, full_name, user_id)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error updating manager info: {e}")
            return False

    async def set_manager_custom_name(self, user_id: int, custom_name: str) -> bool:
        """Установить пользовательское имя для менеджера"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    'UPDATE managers SET custom_name = ? WHERE user_id = ?',
                    (custom_name, user_id)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error setting custom name: {e}")
            return False

    async def get_all_managers(self) -> List[Dict]:
        """Получить список всех активных менеджеров"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                'SELECT user_id, username, full_name, custom_name, added_at FROM managers WHERE is_active = 1'
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def add_key_to_history(self, manager_id: int, client_email: str, phone_number: str,
                                  period: str, expire_days: int, client_id: str, price: int = 0) -> bool:
        """Добавить запись о созданном ключе"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    '''INSERT INTO keys_history
                       (manager_id, client_email, phone_number, period, expire_days, client_id, price)
                       VALUES (?, ?, ?, ?, ?, ?, ?)''',
                    (manager_id, client_email, phone_number, period, expire_days, client_id, price)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding key to history: {e}")
            return False

    async def get_manager_stats(self, manager_id: int) -> Dict:
        """Получить статистику менеджера"""
        async with aiosqlite.connect(self.db_path) as db:
            # Общее количество ключей
            cursor = await db.execute(
                'SELECT COUNT(*) as total FROM keys_history WHERE manager_id = ?',
                (manager_id,)
            )
            total = (await cursor.fetchone())[0]

            # Ключи за сегодня
            cursor = await db.execute(
                '''SELECT COUNT(*) as today FROM keys_history
                   WHERE manager_id = ? AND DATE(created_at) = DATE('now')''',
                (manager_id,)
            )
            today = (await cursor.fetchone())[0]

            # Ключи за месяц
            cursor = await db.execute(
                '''SELECT COUNT(*) as month FROM keys_history
                   WHERE manager_id = ? AND DATE(created_at) >= DATE('now', '-30 days')''',
                (manager_id,)
            )
            month = (await cursor.fetchone())[0]

            return {
                'total': total,
                'today': today,
                'month': month
            }

    async def get_all_stats(self) -> List[Dict]:
        """Получить статистику по всем менеджерам"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                '''SELECT
                    m.user_id,
                    m.username,
                    m.full_name,
                    m.custom_name,
                    COUNT(k.id) as total_keys,
                    SUM(CASE WHEN DATE(k.created_at) = DATE('now') THEN 1 ELSE 0 END) as today_keys,
                    SUM(CASE WHEN DATE(k.created_at) >= DATE('now', '-30 days') THEN 1 ELSE 0 END) as month_keys
                FROM managers m
                LEFT JOIN keys_history k ON m.user_id = k.manager_id
                WHERE m.is_active = 1
                GROUP BY m.user_id
                ORDER BY total_keys DESC'''
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_manager_history(self, manager_id: int, limit: int = 10) -> List[Dict]:
        """Получить историю созданных ключей менеджера"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                '''SELECT client_email, phone_number, period, created_at, expire_days
                   FROM keys_history
                   WHERE manager_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?''',
                (manager_id, limit)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_detailed_stats_by_day(self, days: int = 30) -> List[Dict]:
        """Получить детальную статистику по дням"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                '''SELECT
                    DATE(created_at) as date,
                    COUNT(*) as total_keys,
                    COUNT(DISTINCT manager_id) as active_managers
                FROM keys_history
                WHERE DATE(created_at) >= DATE('now', '-' || ? || ' days')
                GROUP BY DATE(created_at)
                ORDER BY date DESC''',
                (days,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_detailed_stats_by_month(self, months: int = 12) -> List[Dict]:
        """Получить детальную статистику по месяцам"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                '''SELECT
                    strftime('%Y-%m', created_at) as month,
                    COUNT(*) as total_keys,
                    COUNT(DISTINCT manager_id) as active_managers
                FROM keys_history
                WHERE created_at >= DATE('now', '-' || ? || ' months')
                GROUP BY strftime('%Y-%m', created_at)
                ORDER BY month DESC''',
                (months,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_manager_stats_for_period(self, manager_id: int, start_date: str, end_date: str = None) -> Dict:
        """Получить статистику менеджера за определенный период"""
        async with aiosqlite.connect(self.db_path) as db:
            if end_date:
                cursor = await db.execute(
                    '''SELECT COUNT(*) as total FROM keys_history
                       WHERE manager_id = ? AND DATE(created_at) >= ? AND DATE(created_at) <= ?''',
                    (manager_id, start_date, end_date)
                )
            else:
                cursor = await db.execute(
                    '''SELECT COUNT(*) as total FROM keys_history
                       WHERE manager_id = ? AND DATE(created_at) = ?''',
                    (manager_id, start_date)
                )
            total = (await cursor.fetchone())[0]
            return {'total': total, 'start_date': start_date, 'end_date': end_date}

    async def get_all_managers_with_stats_for_period(self, days: int = 30) -> List[Dict]:
        """Получить статистику всех менеджеров за период"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                '''SELECT
                    m.user_id,
                    m.username,
                    m.full_name,
                    m.custom_name,
                    COUNT(k.id) as total_keys
                FROM managers m
                LEFT JOIN keys_history k ON m.user_id = k.manager_id
                    AND DATE(k.created_at) >= DATE('now', '-' || ? || ' days')
                WHERE m.is_active = 1
                GROUP BY m.user_id
                ORDER BY total_keys DESC''',
                (days,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_keys_by_manager_and_period(self, manager_id: int, days: int = 30) -> List[Dict]:
        """Получить все ключи менеджера за период"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                '''SELECT client_email, phone_number, period, created_at
                   FROM keys_history
                   WHERE manager_id = ? AND DATE(created_at) >= DATE('now', '-' || ? || ' days')
                   ORDER BY created_at DESC''',
                (manager_id, days)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_stats_by_day_for_manager(self, manager_id: int, days: int = 30) -> List[Dict]:
        """Получить статистику по дням для конкретного менеджера"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                '''SELECT
                    DATE(created_at) as date,
                    COUNT(*) as total_keys
                FROM keys_history
                WHERE manager_id = ? AND DATE(created_at) >= DATE('now', '-' || ? || ' days')
                GROUP BY DATE(created_at)
                ORDER BY date DESC''',
                (manager_id, days)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_revenue_stats(self) -> Dict:
        """Получить общую статистику по доходам"""
        async with aiosqlite.connect(self.db_path) as db:
            # Общий доход
            cursor = await db.execute('SELECT SUM(price) as total FROM keys_history')
            total_revenue = (await cursor.fetchone())[0] or 0

            # Доход за сегодня
            cursor = await db.execute(
                '''SELECT SUM(price) as today FROM keys_history
                   WHERE DATE(created_at) = DATE('now')'''
            )
            today_revenue = (await cursor.fetchone())[0] or 0

            # Доход за месяц
            cursor = await db.execute(
                '''SELECT SUM(price) as month FROM keys_history
                   WHERE DATE(created_at) >= DATE('now', '-30 days')'''
            )
            month_revenue = (await cursor.fetchone())[0] or 0

            return {
                'total': total_revenue,
                'today': today_revenue,
                'month': month_revenue
            }

    async def get_manager_revenue_stats(self, manager_id: int) -> Dict:
        """Получить статистику по доходам менеджера"""
        async with aiosqlite.connect(self.db_path) as db:
            # Общий доход
            cursor = await db.execute(
                'SELECT SUM(price) as total FROM keys_history WHERE manager_id = ?',
                (manager_id,)
            )
            total_revenue = (await cursor.fetchone())[0] or 0

            # Доход за сегодня
            cursor = await db.execute(
                '''SELECT SUM(price) as today FROM keys_history
                   WHERE manager_id = ? AND DATE(created_at) = DATE('now')''',
                (manager_id,)
            )
            today_revenue = (await cursor.fetchone())[0] or 0

            # Доход за месяц
            cursor = await db.execute(
                '''SELECT SUM(price) as month FROM keys_history
                   WHERE manager_id = ? AND DATE(created_at) >= DATE('now', '-30 days')''',
                (manager_id,)
            )
            month_revenue = (await cursor.fetchone())[0] or 0

            return {
                'total': total_revenue,
                'today': today_revenue,
                'month': month_revenue
            }

    async def get_recent_keys(self, limit: int = 50, manager_id: Optional[int] = None) -> List[Dict]:
        """Получить последние созданные ключи"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if manager_id:
                cursor = await db.execute(
                    '''SELECT k.id, k.manager_id, k.client_email, k.phone_number,
                              k.period, k.expire_days, k.price, k.created_at,
                              m.username, m.full_name, m.custom_name
                       FROM keys_history k
                       LEFT JOIN managers m ON k.manager_id = m.user_id
                       WHERE k.manager_id = ?
                       ORDER BY k.created_at DESC
                       LIMIT ?''',
                    (manager_id, limit)
                )
            else:
                cursor = await db.execute(
                    '''SELECT k.id, k.manager_id, k.client_email, k.phone_number,
                              k.period, k.expire_days, k.price, k.created_at,
                              m.username, m.full_name, m.custom_name
                       FROM keys_history k
                       LEFT JOIN managers m ON k.manager_id = m.user_id
                       ORDER BY k.created_at DESC
                       LIMIT ?''',
                    (limit,)
                )

            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_key_by_id(self, key_id: int) -> Optional[Dict]:
        """Получить информацию о ключе по ID"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                '''SELECT k.id, k.manager_id, k.client_email, k.phone_number,
                          k.period, k.expire_days, k.price, k.created_at,
                          m.username, m.full_name, m.custom_name
                   FROM keys_history k
                   LEFT JOIN managers m ON k.manager_id = m.user_id
                   WHERE k.id = ?''',
                (key_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def delete_key_record(self, key_id: int) -> bool:
        """Удалить запись о ключе из истории (не удаляет ключ из X-UI!)"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    'DELETE FROM keys_history WHERE id = ?',
                    (key_id,)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error deleting key record: {e}")
            return False

    async def get_managers_detailed_stats(self) -> List[Dict]:
        """Получить детальную статистику по всем менеджерам с доходами"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                '''SELECT
                    m.user_id,
                    m.username,
                    m.full_name,
                    m.custom_name,
                    COUNT(k.id) as total_keys,
                    SUM(CASE WHEN DATE(k.created_at) = DATE('now') THEN 1 ELSE 0 END) as today_keys,
                    SUM(CASE WHEN DATE(k.created_at) >= DATE('now', '-30 days') THEN 1 ELSE 0 END) as month_keys,
                    COALESCE(SUM(k.price), 0) as total_revenue,
                    COALESCE(SUM(CASE WHEN DATE(k.created_at) = DATE('now') THEN k.price ELSE 0 END), 0) as today_revenue,
                    COALESCE(SUM(CASE WHEN DATE(k.created_at) >= DATE('now', '-30 days') THEN k.price ELSE 0 END), 0) as month_revenue
                FROM managers m
                LEFT JOIN keys_history k ON m.user_id = k.manager_id
                WHERE m.is_active = 1
                GROUP BY m.user_id
                ORDER BY total_keys DESC'''
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_admin_revenue_stats(self, admin_id: int) -> Dict:
        """Получить статистику по доходам админа"""
        async with aiosqlite.connect(self.db_path) as db:
            # Общий доход админа
            cursor = await db.execute(
                'SELECT SUM(price) as total FROM keys_history WHERE manager_id = ?',
                (admin_id,)
            )
            total_revenue = (await cursor.fetchone())[0] or 0

            # Доход за сегодня
            cursor = await db.execute(
                '''SELECT SUM(price) as today FROM keys_history
                   WHERE manager_id = ? AND DATE(created_at) = DATE('now')''',
                (admin_id,)
            )
            today_revenue = (await cursor.fetchone())[0] or 0

            # Доход за месяц
            cursor = await db.execute(
                '''SELECT SUM(price) as month FROM keys_history
                   WHERE manager_id = ? AND DATE(created_at) >= DATE('now', '-30 days')''',
                (admin_id,)
            )
            month_revenue = (await cursor.fetchone())[0] or 0

            # Количество ключей
            cursor = await db.execute(
                'SELECT COUNT(*) as total FROM keys_history WHERE manager_id = ?',
                (admin_id,)
            )
            total_keys = (await cursor.fetchone())[0] or 0

            cursor = await db.execute(
                '''SELECT COUNT(*) as today FROM keys_history
                   WHERE manager_id = ? AND DATE(created_at) = DATE('now')''',
                (admin_id,)
            )
            today_keys = (await cursor.fetchone())[0] or 0

            cursor = await db.execute(
                '''SELECT COUNT(*) as month FROM keys_history
                   WHERE manager_id = ? AND DATE(created_at) >= DATE('now', '-30 days')''',
                (admin_id,)
            )
            month_keys = (await cursor.fetchone())[0] or 0

            return {
                'total': total_revenue,
                'today': today_revenue,
                'month': month_revenue,
                'total_keys': total_keys,
                'today_keys': today_keys,
                'month_keys': month_keys
            }

    async def get_managers_only_revenue_stats(self, exclude_admin_id: int = None) -> Dict:
        """Получить статистику по доходам только активных менеджеров (можно исключить админа)"""
        async with aiosqlite.connect(self.db_path) as db:
            # Получаем ID активных менеджеров
            if exclude_admin_id:
                cursor = await db.execute(
                    'SELECT user_id FROM managers WHERE is_active = 1 AND user_id != ?',
                    (exclude_admin_id,)
                )
            else:
                cursor = await db.execute('SELECT user_id FROM managers WHERE is_active = 1')

            manager_ids = [row[0] for row in await cursor.fetchall()]

            if not manager_ids:
                return {'total': 0, 'today': 0, 'month': 0}

            # Формируем плейсхолдеры для SQL
            placeholders = ','.join('?' * len(manager_ids))

            # Общий доход менеджеров
            cursor = await db.execute(
                f'SELECT SUM(price) as total FROM keys_history WHERE manager_id IN ({placeholders})',
                manager_ids
            )
            total_revenue = (await cursor.fetchone())[0] or 0

            # Доход за сегодня
            cursor = await db.execute(
                f'''SELECT SUM(price) as today FROM keys_history
                   WHERE manager_id IN ({placeholders}) AND DATE(created_at) = DATE('now')''',
                manager_ids
            )
            today_revenue = (await cursor.fetchone())[0] or 0

            # Доход за месяц
            cursor = await db.execute(
                f'''SELECT SUM(price) as month FROM keys_history
                   WHERE manager_id IN ({placeholders}) AND DATE(created_at) >= DATE('now', '-30 days')''',
                manager_ids
            )
            month_revenue = (await cursor.fetchone())[0] or 0

            return {
                'total': total_revenue,
                'today': today_revenue,
                'month': month_revenue
            }

    async def search_keys(self, query: str, limit: int = 50) -> List[Dict]:
        """Поиск ключей по номеру телефона или имени клиента"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # Поиск по phone_number или client_email (содержит имя)
            search_pattern = f'%{query}%'
            cursor = await db.execute(
                '''SELECT k.id, k.manager_id, k.client_email, k.phone_number,
                          k.period, k.expire_days, k.price, k.created_at,
                          m.username, m.full_name, m.custom_name
                   FROM keys_history k
                   LEFT JOIN managers m ON k.manager_id = m.user_id
                   WHERE k.phone_number LIKE ? OR k.client_email LIKE ?
                   ORDER BY k.created_at DESC
                   LIMIT ?''',
                (search_pattern, search_pattern, limit)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== МЕТОДЫ ДЛЯ ЗАМЕНЫ КЛЮЧЕЙ ====================

    async def add_key_replacement(self, manager_id: int, client_email: str, phone_number: str,
                                   period: str, expire_days: int, client_id: str) -> bool:
        """Добавить запись о замене ключа"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    '''INSERT INTO key_replacements
                       (manager_id, client_email, phone_number, period, expire_days, client_id)
                       VALUES (?, ?, ?, ?, ?, ?)''',
                    (manager_id, client_email, phone_number, period, expire_days, client_id)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding key replacement: {e}")
            return False

    async def get_replacement_stats(self, manager_id: int) -> Dict:
        """Получить статистику замен менеджера"""
        async with aiosqlite.connect(self.db_path) as db:
            # Общее количество замен
            cursor = await db.execute(
                'SELECT COUNT(*) as total FROM key_replacements WHERE manager_id = ?',
                (manager_id,)
            )
            total = (await cursor.fetchone())[0]

            # Замены за сегодня
            cursor = await db.execute(
                '''SELECT COUNT(*) as today FROM key_replacements
                   WHERE manager_id = ? AND DATE(created_at) = DATE('now')''',
                (manager_id,)
            )
            today = (await cursor.fetchone())[0]

            # Замены за месяц
            cursor = await db.execute(
                '''SELECT COUNT(*) as month FROM key_replacements
                   WHERE manager_id = ? AND DATE(created_at) >= DATE('now', '-30 days')''',
                (manager_id,)
            )
            month = (await cursor.fetchone())[0]

            return {
                'total': total,
                'today': today,
                'month': month
            }

    async def get_all_replacement_stats(self) -> Dict:
        """Получить общую статистику по всем заменам"""
        async with aiosqlite.connect(self.db_path) as db:
            # Общее количество замен
            cursor = await db.execute('SELECT COUNT(*) as total FROM key_replacements')
            total = (await cursor.fetchone())[0]

            # Замены за сегодня
            cursor = await db.execute(
                '''SELECT COUNT(*) as today FROM key_replacements
                   WHERE DATE(created_at) = DATE('now')'''
            )
            today = (await cursor.fetchone())[0]

            # Замены за месяц
            cursor = await db.execute(
                '''SELECT COUNT(*) as month FROM key_replacements
                   WHERE DATE(created_at) >= DATE('now', '-30 days')'''
            )
            month = (await cursor.fetchone())[0]

            return {
                'total': total,
                'today': today,
                'month': month
            }

    async def get_replacement_history(self, manager_id: int, limit: int = 10) -> List[Dict]:
        """Получить историю замен ключей менеджера"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                '''SELECT client_email, phone_number, period, created_at, expire_days
                   FROM key_replacements
                   WHERE manager_id = ?
                   ORDER BY created_at DESC
                   LIMIT ?''',
                (manager_id, limit)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # ==================== МЕТОДЫ ДЛЯ ОТЛОЖЕННЫХ КЛЮЧЕЙ (RETRY) ====================

    async def add_pending_key(self, telegram_id: int, username: str, phone: str,
                              period_key: str, period_name: str, period_days: int,
                              period_price: int, inbound_id: int, error: str) -> int:
        """Добавить ключ в очередь на повторное создание"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    '''INSERT INTO pending_keys
                       (telegram_id, username, phone, period_key, period_name, period_days,
                        period_price, inbound_id, last_error, status)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')''',
                    (telegram_id, username, phone, period_key, period_name, period_days,
                     period_price, inbound_id, error)
                )
                await db.commit()
                return cursor.lastrowid
        except Exception as e:
            logger.error(f"Error adding pending key: {e}")
            return 0

    async def get_pending_keys(self, limit: int = 10) -> List[Dict]:
        """Получить список ключей для повторной попытки"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                '''SELECT * FROM pending_keys
                   WHERE status = 'pending' AND retry_count < max_retries
                   ORDER BY created_at ASC
                   LIMIT ?''',
                (limit,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def update_pending_key_retry(self, key_id: int, error: str) -> bool:
        """Обновить информацию о попытке retry"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    '''UPDATE pending_keys
                       SET retry_count = retry_count + 1,
                           last_error = ?,
                           last_retry_at = CURRENT_TIMESTAMP
                       WHERE id = ?''',
                    (error, key_id)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error updating pending key retry: {e}")
            return False

    async def mark_pending_key_completed(self, key_id: int, client_id: str = None) -> bool:
        """Отметить ключ как успешно созданный"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    '''UPDATE pending_keys
                       SET status = 'completed',
                           completed_at = CURRENT_TIMESTAMP
                       WHERE id = ?''',
                    (key_id,)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error marking pending key as completed: {e}")
            return False

    async def mark_pending_key_failed(self, key_id: int) -> bool:
        """Отметить ключ как окончательно неудавшийся (после всех retry)"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    '''UPDATE pending_keys
                       SET status = 'failed'
                       WHERE id = ?''',
                    (key_id,)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error marking pending key as failed: {e}")
            return False

    async def get_pending_keys_count(self) -> Dict:
        """Получить статистику по отложенным ключам"""
        async with aiosqlite.connect(self.db_path) as db:
            # Ожидающие
            cursor = await db.execute(
                "SELECT COUNT(*) FROM pending_keys WHERE status = 'pending'"
            )
            pending = (await cursor.fetchone())[0]

            # Выполненные
            cursor = await db.execute(
                "SELECT COUNT(*) FROM pending_keys WHERE status = 'completed'"
            )
            completed = (await cursor.fetchone())[0]

            # Неудачные
            cursor = await db.execute(
                "SELECT COUNT(*) FROM pending_keys WHERE status = 'failed'"
            )
            failed = (await cursor.fetchone())[0]

            return {
                'pending': pending,
                'completed': completed,
                'failed': failed
            }

    async def get_user_pending_keys(self, telegram_id: int) -> List[Dict]:
        """Получить отложенные ключи пользователя"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                '''SELECT * FROM pending_keys
                   WHERE telegram_id = ? AND status = 'pending'
                   ORDER BY created_at DESC''',
                (telegram_id,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_pending_key(self, key_id: int) -> bool:
        """Удалить отложенный ключ"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute('DELETE FROM pending_keys WHERE id = ?', (key_id,))
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error deleting pending key: {e}")
            return False

    # ==================== МЕТОДЫ ДЛЯ СВЯЗАННЫХ КЛЮЧЕЙ (LINKED CLIENTS) ====================

    async def add_linked_client(self, master_uuid: str, linked_uuid: str) -> bool:
        """Привязать ключ к главному ключу"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    'INSERT OR IGNORE INTO linked_clients (master_uuid, linked_uuid) VALUES (?, ?)',
                    (master_uuid, linked_uuid)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error adding linked client: {e}")
            return False

    async def remove_linked_client(self, master_uuid: str, linked_uuid: str) -> bool:
        """Отвязать ключ от главного ключа"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    'DELETE FROM linked_clients WHERE master_uuid = ? AND linked_uuid = ?',
                    (master_uuid, linked_uuid)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Error removing linked client: {e}")
            return False

    async def get_linked_clients(self, master_uuid: str) -> List[str]:
        """Получить список всех UUID, связанных с главным ключом"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'SELECT linked_uuid FROM linked_clients WHERE master_uuid = ?',
                (master_uuid,)
            )
            rows = await cursor.fetchall()
            return [row[0] for row in rows]

    async def get_linked_clients_with_info(self, master_uuid: str) -> List[Dict]:
        """Получить связанные ключи с информацией из keys_history"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('''
                SELECT lc.id as link_id, lc.linked_uuid, lc.linked_at,
                       kh.id as key_id, kh.client_email, kh.phone_number,
                       kh.expire_days, kh.created_at
                FROM linked_clients lc
                LEFT JOIN keys_history kh ON lc.linked_uuid = kh.client_id
                WHERE lc.master_uuid = ?
                ORDER BY lc.linked_at DESC
            ''', (master_uuid,))
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_master_for_linked(self, linked_uuid: str) -> Optional[str]:
        """Получить UUID главного ключа, к которому привязан данный ключ"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                'SELECT master_uuid FROM linked_clients WHERE linked_uuid = ?',
                (linked_uuid,)
            )
            row = await cursor.fetchone()
            return row[0] if row else None

    async def is_linked_to_any(self, uuid: str) -> bool:
        """Проверить, привязан ли ключ к какому-либо главному ключу"""
        master = await self.get_master_for_linked(uuid)
        return master is not None

    async def get_all_linked_uuids_for_subscription(self, master_uuid: str) -> List[str]:
        """Получить все UUID для подписки (включая master и все linked)"""
        linked = await self.get_linked_clients(master_uuid)
        return [master_uuid] + linked

    async def get_key_by_uuid(self, client_uuid: str) -> Optional[Dict]:
        """Получить информацию о ключе по UUID"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('''
                SELECT kh.*, m.full_name as manager_name, m.custom_name
                FROM keys_history kh
                LEFT JOIN managers m ON kh.manager_id = m.user_id
                WHERE kh.client_id = ?
            ''', (client_uuid,))
            row = await cursor.fetchone()
            return dict(row) if row else None
