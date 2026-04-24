"""
Менеджер брендов — CRUD для мульти-бот/мульти-бренд системы
"""
import aiosqlite
import json
import logging
from datetime import datetime
from typing import List, Dict, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class Brand:
    id: int
    name: str
    bot_token: str
    domain: str
    is_active: int = 1
    theme_color: str = '#007bff'
    logo_url: Optional[str] = None
    admin_id: Optional[int] = None
    allowed_servers: Optional[str] = None  # JSON list of server names, NULL = all
    created_at: Optional[str] = None

    def get_allowed_servers(self) -> Optional[list]:
        """Получить список разрешённых серверов (None = все)"""
        if self.allowed_servers:
            return json.loads(self.allowed_servers) if isinstance(self.allowed_servers, str) else self.allowed_servers
        return None


class BrandManager:
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init_brands_tables(self):
        """Создание таблиц brands и manager_brands"""
        async with aiosqlite.connect(self.db_path) as db:
            # Таблица брендов
            await db.execute('''
                CREATE TABLE IF NOT EXISTS brands (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    bot_token TEXT NOT NULL UNIQUE,
                    domain TEXT NOT NULL UNIQUE,
                    is_active INTEGER DEFAULT 1,
                    theme_color TEXT DEFAULT '#007bff',
                    logo_url TEXT DEFAULT NULL,
                    admin_id INTEGER DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            # Таблица связи менеджер-бренд (many-to-many с правами)
            await db.execute('''
                CREATE TABLE IF NOT EXISTS manager_brands (
                    manager_id INTEGER NOT NULL,
                    brand_id INTEGER NOT NULL,
                    allowed_servers TEXT DEFAULT NULL,
                    is_active INTEGER DEFAULT 1,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (manager_id, brand_id),
                    FOREIGN KEY (brand_id) REFERENCES brands(id) ON DELETE CASCADE
                )
            ''')

            # Добавляем key_limit и is_verified в manager_brands
            try:
                await db.execute('ALTER TABLE manager_brands ADD COLUMN key_limit INTEGER DEFAULT 5')
            except Exception:
                pass
            try:
                await db.execute('ALTER TABLE manager_brands ADD COLUMN is_verified INTEGER DEFAULT 0')
            except Exception:
                pass

            # Добавляем allowed_servers в brands
            try:
                await db.execute('ALTER TABLE brands ADD COLUMN allowed_servers TEXT DEFAULT NULL')
            except Exception:
                pass

            # Добавляем brand_id в существующие таблицы
            for table in ['clients', 'keys_history', 'client_servers']:
                try:
                    await db.execute(f'ALTER TABLE {table} ADD COLUMN brand_id INTEGER DEFAULT 1')
                    logger.info(f"Колонка brand_id добавлена в таблицу {table}")
                except Exception:
                    pass  # Колонка уже существует

            # Индексы
            await db.execute('CREATE INDEX IF NOT EXISTS idx_brands_domain ON brands(domain)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_brands_token ON brands(bot_token)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_manager_brands_brand ON manager_brands(brand_id)')
            await db.execute('CREATE INDEX IF NOT EXISTS idx_manager_brands_manager ON manager_brands(manager_id)')
            try:
                await db.execute('CREATE INDEX IF NOT EXISTS idx_clients_brand ON clients(brand_id)')
            except Exception:
                pass  # clients table may not exist
            await db.execute('CREATE INDEX IF NOT EXISTS idx_keys_history_brand ON keys_history(brand_id)')

            await db.commit()
            logger.info("Таблицы брендов инициализированы")

    async def ensure_default_brand(self, bot_token: str, domain: str, name: str = "ZoVGoR VPN", admin_id: int = None):
        """Создать бренд по умолчанию (brand_id=1) если его нет"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT id FROM brands WHERE id = 1")
            if await cursor.fetchone():
                return 1  # Уже существует

            await db.execute(
                """INSERT INTO brands (id, name, bot_token, domain, is_active, admin_id)
                   VALUES (1, ?, ?, ?, 1, ?)""",
                (name, bot_token, domain, admin_id)
            )
            await db.commit()
            logger.info(f"Бренд по умолчанию создан: {name} ({domain})")
            return 1

    # ==================== CRUD для брендов ====================

    async def create_brand(self, name: str, bot_token: str, domain: str,
                           theme_color: str = '#007bff', logo_url: str = None,
                           admin_id: int = None) -> Optional[int]:
        """Создать новый бренд"""
        async with aiosqlite.connect(self.db_path) as db:
            try:
                cursor = await db.execute(
                    """INSERT INTO brands (name, bot_token, domain, theme_color, logo_url, admin_id)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (name, bot_token, domain, theme_color, logo_url, admin_id)
                )
                await db.commit()
                brand_id = cursor.lastrowid
                logger.info(f"Бренд создан: {name} (id={brand_id}, domain={domain})")
                return brand_id
            except aiosqlite.IntegrityError as e:
                logger.error(f"Ошибка создания бренда: {e}")
                return None

    async def get_brand(self, brand_id: int) -> Optional[Brand]:
        """Получить бренд по ID"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM brands WHERE id = ?", (brand_id,))
            row = await cursor.fetchone()
            if row:
                return Brand(**dict(row))
            return None

    async def get_brand_by_token(self, bot_token: str) -> Optional[Brand]:
        """Получить бренд по токену бота"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM brands WHERE bot_token = ?", (bot_token,))
            row = await cursor.fetchone()
            if row:
                return Brand(**dict(row))
            return None

    async def get_brand_by_domain(self, domain: str) -> Optional[Brand]:
        """Получить бренд по домену"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute("SELECT * FROM brands WHERE domain = ?", (domain,))
            row = await cursor.fetchone()
            if row:
                return Brand(**dict(row))
            return None

    async def list_brands(self, active_only: bool = False) -> List[Brand]:
        """Получить список всех брендов"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            query = "SELECT * FROM brands"
            if active_only:
                query += " WHERE is_active = 1"
            query += " ORDER BY id"
            cursor = await db.execute(query)
            rows = await cursor.fetchall()
            return [Brand(**dict(row)) for row in rows]

    async def update_brand(self, brand_id: int, **kwargs) -> bool:
        """Обновить бренд"""
        allowed_fields = {'name', 'bot_token', 'domain', 'is_active', 'theme_color', 'logo_url', 'admin_id', 'allowed_servers'}
        fields = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not fields:
            return False

        set_clause = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [brand_id]

        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(f"UPDATE brands SET {set_clause} WHERE id = ?", values)
                await db.commit()
                return True
            except Exception as e:
                logger.error(f"Ошибка обновления бренда {brand_id}: {e}")
                return False

    async def toggle_brand(self, brand_id: int) -> Optional[bool]:
        """Переключить активность бренда. Возвращает новый статус."""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT is_active FROM brands WHERE id = ?", (brand_id,))
            row = await cursor.fetchone()
            if not row:
                return None
            new_status = 0 if row[0] else 1
            await db.execute("UPDATE brands SET is_active = ? WHERE id = ?", (new_status, brand_id))
            await db.commit()
            return bool(new_status)

    async def delete_brand(self, brand_id: int) -> bool:
        """Удалить бренд (нельзя удалить brand_id=1)"""
        if brand_id == 1:
            return False
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM manager_brands WHERE brand_id = ?", (brand_id,))
            await db.execute("DELETE FROM brands WHERE id = ?", (brand_id,))
            await db.commit()
            return True


    async def set_brand_servers(self, brand_id: int, servers: Optional[list]) -> bool:
        """Установить разрешённые серверы для бренда (None = все)"""
        servers_json = json.dumps(servers) if servers else None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "UPDATE brands SET allowed_servers = ? WHERE id = ?",
                (servers_json, brand_id)
            )
            await db.commit()
            return True

    async def get_manager_key_limit(self, manager_id: int, brand_id: int) -> dict:
        """Получить лимит ключей менеджера в бренде"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT key_limit, is_verified FROM manager_brands
                   WHERE manager_id = ? AND brand_id = ? AND is_active = 1""",
                (manager_id, brand_id)
            )
            row = await cursor.fetchone()
            if not row:
                return {'limit': 5, 'verified': False}
            return {'limit': row[0], 'verified': bool(row[1])}

    async def get_manager_keys_count(self, manager_id: int, brand_id: int) -> int:
        """Посчитать созданные ключи менеджером в бренде"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT COUNT(*) FROM keys_history
                   WHERE manager_id = ? AND brand_id = ?""",
                (manager_id, brand_id)
            )
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def set_manager_key_limit(self, brand_id: int, manager_id: int, limit: int) -> bool:
        """Установить лимит ключей (0 = безлимит)"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE manager_brands SET key_limit = ?
                   WHERE manager_id = ? AND brand_id = ?""",
                (limit, manager_id, brand_id)
            )
            await db.commit()
            return True

    async def verify_manager(self, brand_id: int, manager_id: int, verified: bool = True) -> bool:
        """Отметить менеджера как проверенного (снять лимит)"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE manager_brands SET is_verified = ?, key_limit = ?
                   WHERE manager_id = ? AND brand_id = ?""",
                (1 if verified else 0, 0 if verified else 5, manager_id, brand_id)
            )
            await db.commit()
            return True

    async def can_create_key(self, manager_id: int, brand_id: int) -> tuple:
        """Проверить может ли менеджер создать ключ. Returns: (can: bool, reason: str)"""
        info = await self.get_manager_key_limit(manager_id, brand_id)
        if info['verified'] or info['limit'] == 0:
            return True, ""
        count = await self.get_manager_keys_count(manager_id, brand_id)
        if count >= info['limit']:
            return False, f"Лимит ключей исчерпан ({count}/{info['limit']}). Обратитесь к администратору."
        return True, f"{count}/{info['limit']}"

    # ==================== Менеджеры брендов ====================

    async def assign_manager(self, brand_id: int, manager_id: int,
                             allowed_servers: Optional[List[str]] = None) -> bool:
        """Назначить менеджера на бренд"""
        servers_json = json.dumps(allowed_servers) if allowed_servers else None
        async with aiosqlite.connect(self.db_path) as db:
            try:
                await db.execute(
                    """INSERT OR REPLACE INTO manager_brands (manager_id, brand_id, allowed_servers, is_active)
                       VALUES (?, ?, ?, 1)""",
                    (manager_id, brand_id, servers_json)
                )
                await db.commit()
                logger.info(f"Менеджер {manager_id} назначен на бренд {brand_id}")
                return True
            except Exception as e:
                logger.error(f"Ошибка назначения менеджера: {e}")
                return False

    async def remove_manager(self, brand_id: int, manager_id: int) -> bool:
        """Убрать менеджера из бренда"""
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM manager_brands WHERE manager_id = ? AND brand_id = ?",
                (manager_id, brand_id)
            )
            await db.commit()
            return True

    async def get_brand_managers(self, brand_id: int) -> List[Dict]:
        """Получить список менеджеров бренда"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT mb.manager_id, mb.allowed_servers, mb.is_active, mb.added_at,
                          m.username, m.full_name, m.custom_name
                   FROM manager_brands mb
                   LEFT JOIN managers m ON mb.manager_id = m.user_id
                   WHERE mb.brand_id = ? AND mb.is_active = 1
                   ORDER BY mb.added_at""",
                (brand_id,)
            )
            rows = await cursor.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                if d.get('allowed_servers'):
                    d['allowed_servers'] = json.loads(d['allowed_servers'])
                result.append(d)
            return result

    async def get_manager_brands(self, manager_id: int) -> List[Brand]:
        """Получить список брендов менеджера"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT b.* FROM brands b
                   JOIN manager_brands mb ON b.id = mb.brand_id
                   WHERE mb.manager_id = ? AND mb.is_active = 1 AND b.is_active = 1
                   ORDER BY b.id""",
                (manager_id,)
            )
            rows = await cursor.fetchall()
            return [Brand(**dict(row)) for row in rows]

    async def is_manager_in_brand(self, manager_id: int, brand_id: int) -> bool:
        """Проверить, есть ли менеджер в бренде"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT 1 FROM manager_brands
                   WHERE manager_id = ? AND brand_id = ? AND is_active = 1""",
                (manager_id, brand_id)
            )
            return await cursor.fetchone() is not None

    async def get_manager_servers_in_brand(self, manager_id: int, brand_id: int) -> Optional[List[str]]:
        """Получить разрешённые серверы менеджера в конкретном бренде"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """SELECT allowed_servers FROM manager_brands
                   WHERE manager_id = ? AND brand_id = ? AND is_active = 1""",
                (manager_id, brand_id)
            )
            row = await cursor.fetchone()
            if not row:
                return None
            if row[0]:
                return json.loads(row[0])
            return None  # NULL = все серверы

    async def update_manager_servers(self, brand_id: int, manager_id: int,
                                     allowed_servers: Optional[List[str]]) -> bool:
        """Обновить серверы менеджера в бренде"""
        servers_json = json.dumps(allowed_servers) if allowed_servers else None
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """UPDATE manager_brands SET allowed_servers = ?
                   WHERE manager_id = ? AND brand_id = ?""",
                (servers_json, manager_id, brand_id)
            )
            await db.commit()
            return True
