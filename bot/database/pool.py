"""
Database Connection Pool для асинхронной работы с SQLite

Обеспечивает:
- Пул соединений для снижения накладных расходов
- Автоматическое управление транзакциями
- Поддержка контекстных менеджеров
- Мониторинг состояния пула
"""
import asyncio
import aiosqlite
import logging
from typing import Optional, Any, List, Dict
from dataclasses import dataclass, field
from contextlib import asynccontextmanager
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class PoolConfig:
    """Конфигурация пула соединений"""
    min_size: int = 2
    max_size: int = 10
    max_idle_time: float = 300.0  # 5 минут
    acquire_timeout: float = 10.0
    enable_wal: bool = True
    enable_foreign_keys: bool = True


@dataclass
class ConnectionInfo:
    """Информация о соединении"""
    connection: aiosqlite.Connection
    created_at: datetime
    last_used: datetime
    queries_count: int = 0
    in_use: bool = False


class DatabasePool:
    """
    Асинхронный пул соединений с SQLite

    Использование:
        pool = DatabasePool('/path/to/db.sqlite')
        await pool.init()

        async with pool.acquire() as conn:
            await conn.execute("SELECT * FROM users")

        await pool.close()

    Или через контекстный менеджер:
        async with DatabasePool('/path/to/db.sqlite') as pool:
            async with pool.acquire() as conn:
                ...
    """

    def __init__(self, db_path: str, config: PoolConfig = None):
        self.db_path = db_path
        self.config = config or PoolConfig()
        self._connections: List[ConnectionInfo] = []
        self._lock = asyncio.Lock()
        self._closed = False
        self._stats = {
            'total_acquires': 0,
            'total_releases': 0,
            'total_queries': 0,
            'peak_size': 0,
            'wait_time_total': 0.0
        }
        self._initialized = False

    async def init(self):
        """Инициализировать пул с минимальным количеством соединений"""
        if self._initialized:
            return

        async with self._lock:
            for _ in range(self.config.min_size):
                conn_info = await self._create_connection()
                self._connections.append(conn_info)
            self._initialized = True
            logger.info(f"Database pool initialized with {len(self._connections)} connections")

    async def _create_connection(self) -> ConnectionInfo:
        """Создать новое соединение"""
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row

        # Оптимизации SQLite
        if self.config.enable_wal:
            await conn.execute("PRAGMA journal_mode=WAL")

        if self.config.enable_foreign_keys:
            await conn.execute("PRAGMA foreign_keys=ON")

        # Дополнительные оптимизации
        await conn.execute("PRAGMA synchronous=NORMAL")
        await conn.execute("PRAGMA cache_size=-64000")  # 64MB cache
        await conn.execute("PRAGMA temp_store=MEMORY")

        return ConnectionInfo(
            connection=conn,
            created_at=datetime.now(),
            last_used=datetime.now()
        )

    @asynccontextmanager
    async def acquire(self):
        """
        Получить соединение из пула

        Использование:
            async with pool.acquire() as conn:
                await conn.execute(...)
        """
        if self._closed:
            raise RuntimeError("Pool is closed")

        if not self._initialized:
            await self.init()

        conn_info = None
        start_time = asyncio.get_event_loop().time()

        async with self._lock:
            # Ищем свободное соединение
            for info in self._connections:
                if not info.in_use:
                    conn_info = info
                    conn_info.in_use = True
                    break

            # Если нет свободных и можем создать новое
            if conn_info is None and len(self._connections) < self.config.max_size:
                conn_info = await self._create_connection()
                conn_info.in_use = True
                self._connections.append(conn_info)
                logger.debug(f"Pool expanded to {len(self._connections)} connections")

        # Если все еще нет соединения - ждем
        if conn_info is None:
            deadline = asyncio.get_event_loop().time() + self.config.acquire_timeout

            while True:
                await asyncio.sleep(0.1)

                if asyncio.get_event_loop().time() > deadline:
                    raise asyncio.TimeoutError(
                        f"Could not acquire connection within {self.config.acquire_timeout}s"
                    )

                async with self._lock:
                    for info in self._connections:
                        if not info.in_use:
                            conn_info = info
                            conn_info.in_use = True
                            break

                if conn_info:
                    break

        # Обновляем статистику
        wait_time = asyncio.get_event_loop().time() - start_time
        self._stats['total_acquires'] += 1
        self._stats['wait_time_total'] += wait_time
        self._stats['peak_size'] = max(
            self._stats['peak_size'],
            sum(1 for c in self._connections if c.in_use)
        )

        conn_info.last_used = datetime.now()

        try:
            yield conn_info.connection
            conn_info.queries_count += 1
            self._stats['total_queries'] += 1
        finally:
            async with self._lock:
                conn_info.in_use = False
                self._stats['total_releases'] += 1

    async def execute(self, query: str, params: tuple = None) -> aiosqlite.Cursor:
        """Выполнить запрос"""
        async with self.acquire() as conn:
            if params:
                return await conn.execute(query, params)
            return await conn.execute(query)

    async def execute_many(self, query: str, params_list: List[tuple]):
        """Выполнить множество запросов"""
        async with self.acquire() as conn:
            await conn.executemany(query, params_list)
            await conn.commit()

    async def fetch_one(self, query: str, params: tuple = None) -> Optional[Dict]:
        """Получить одну запись"""
        async with self.acquire() as conn:
            if params:
                cursor = await conn.execute(query, params)
            else:
                cursor = await conn.execute(query)
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def fetch_all(self, query: str, params: tuple = None) -> List[Dict]:
        """Получить все записи"""
        async with self.acquire() as conn:
            if params:
                cursor = await conn.execute(query, params)
            else:
                cursor = await conn.execute(query)
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def fetch_value(self, query: str, params: tuple = None) -> Any:
        """Получить одно значение"""
        async with self.acquire() as conn:
            if params:
                cursor = await conn.execute(query, params)
            else:
                cursor = await conn.execute(query)
            row = await cursor.fetchone()
            return row[0] if row else None

    @asynccontextmanager
    async def transaction(self):
        """
        Выполнить операции в транзакции

        Использование:
            async with pool.transaction() as conn:
                await conn.execute("INSERT ...")
                await conn.execute("UPDATE ...")
            # Автоматический commit при успехе, rollback при ошибке
        """
        async with self.acquire() as conn:
            try:
                yield conn
                await conn.commit()
            except Exception:
                await conn.rollback()
                raise

    async def _cleanup_idle(self):
        """Очистить неиспользуемые соединения"""
        now = datetime.now()
        to_remove = []

        async with self._lock:
            # Оставляем минимум min_size соединений
            active_count = len(self._connections)

            for info in self._connections:
                if active_count <= self.config.min_size:
                    break

                if not info.in_use:
                    idle_time = (now - info.last_used).total_seconds()
                    if idle_time > self.config.max_idle_time:
                        to_remove.append(info)
                        active_count -= 1

            for info in to_remove:
                try:
                    await info.connection.close()
                except Exception as e:
                    logger.warning(f"Error closing idle connection: {e}")
                self._connections.remove(info)

        if to_remove:
            logger.debug(f"Cleaned up {len(to_remove)} idle connections")

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику пула"""
        in_use = sum(1 for c in self._connections if c.in_use)
        idle = len(self._connections) - in_use

        avg_wait = (
            self._stats['wait_time_total'] / self._stats['total_acquires']
            if self._stats['total_acquires'] > 0 else 0
        )

        return {
            'size': len(self._connections),
            'in_use': in_use,
            'idle': idle,
            'max_size': self.config.max_size,
            'total_acquires': self._stats['total_acquires'],
            'total_queries': self._stats['total_queries'],
            'peak_size': self._stats['peak_size'],
            'avg_wait_time': f"{avg_wait*1000:.2f}ms"
        }

    async def close(self):
        """Закрыть все соединения пула"""
        self._closed = True

        async with self._lock:
            for info in self._connections:
                try:
                    await info.connection.close()
                except Exception as e:
                    logger.warning(f"Error closing connection: {e}")

            self._connections.clear()
            logger.info("Database pool closed")

    async def __aenter__(self):
        await self.init()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


# Глобальный пул для использования во всем приложении
_global_pool: Optional[DatabasePool] = None


async def get_pool(db_path: str = None) -> DatabasePool:
    """Получить глобальный пул соединений"""
    global _global_pool

    if _global_pool is None:
        if db_path is None:
            raise ValueError("db_path required for first initialization")
        _global_pool = DatabasePool(db_path)
        await _global_pool.init()

    return _global_pool


async def close_pool():
    """Закрыть глобальный пул"""
    global _global_pool

    if _global_pool:
        await _global_pool.close()
        _global_pool = None


class Repository:
    """
    Базовый класс репозитория для работы с БД

    Использование:
        class UserRepository(Repository):
            async def get_by_id(self, user_id: int):
                return await self.fetch_one(
                    "SELECT * FROM users WHERE id = ?",
                    (user_id,)
                )

        repo = UserRepository(pool)
        user = await repo.get_by_id(123)
    """

    def __init__(self, pool: DatabasePool):
        self.pool = pool

    async def execute(self, query: str, params: tuple = None) -> aiosqlite.Cursor:
        return await self.pool.execute(query, params)

    async def fetch_one(self, query: str, params: tuple = None) -> Optional[Dict]:
        return await self.pool.fetch_one(query, params)

    async def fetch_all(self, query: str, params: tuple = None) -> List[Dict]:
        return await self.pool.fetch_all(query, params)

    async def fetch_value(self, query: str, params: tuple = None) -> Any:
        return await self.pool.fetch_value(query, params)

    @asynccontextmanager
    async def transaction(self):
        async with self.pool.transaction() as conn:
            yield conn


__all__ = [
    'PoolConfig',
    'DatabasePool',
    'get_pool',
    'close_pool',
    'Repository',
]
