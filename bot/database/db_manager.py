"""
Менеджер базы данных для хранения информации о менеджерах и ключах
"""
import aiosqlite
from datetime import datetime
from typing import List, Dict, Optional


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
            except:
                pass  # Колонка уже существует

            # Добавляем колонку custom_name для пользовательских имен менеджеров
            try:
                await db.execute('ALTER TABLE managers ADD COLUMN custom_name TEXT')
            except:
                pass  # Колонка уже существует

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
            print(f"Error adding manager: {e}")
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
            print(f"Error removing manager: {e}")
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
            print(f"Error updating manager info: {e}")
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
            print(f"Error setting custom name: {e}")
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
            print(f"Error adding key to history: {e}")
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
                '''SELECT client_email, phone_number, period, created_at
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
            print(f"Error deleting key record: {e}")
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
