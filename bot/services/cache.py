"""
Кэширование данных
"""
import aiosqlite
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional, Dict, List
from functools import wraps

logger = logging.getLogger(__name__)


class CacheManager:
    """Менеджер кэша с поддержкой SQLite и in-memory"""

    def __init__(self, db_path: str, use_memory_cache: bool = True):
        self.db_path = db_path
        self.use_memory_cache = use_memory_cache
        self._memory_cache: Dict[str, Dict] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str) -> Optional[Any]:
        """Получение значения из кэша"""
        # Сначала проверяем memory cache
        if self.use_memory_cache and key in self._memory_cache:
            cached = self._memory_cache[key]
            if cached['expires_at'] > datetime.now():
                return cached['value']
            else:
                del self._memory_cache[key]

        # Затем проверяем SQLite
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT value, expires_at FROM cache WHERE key = ?",
                (key,)
            )
            row = await cursor.fetchone()

            if row:
                expires_at = datetime.fromisoformat(row[1])
                if expires_at > datetime.now():
                    value = json.loads(row[0])

                    # Обновляем memory cache
                    if self.use_memory_cache:
                        self._memory_cache[key] = {
                            'value': value,
                            'expires_at': expires_at
                        }

                    return value
                else:
                    # Удаляем устаревшую запись
                    await db.execute("DELETE FROM cache WHERE key = ?", (key,))
                    await db.commit()

        return None

    async def set(self, key: str, value: Any, ttl_seconds: int = 3600):
        """Сохранение значения в кэш"""
        expires_at = datetime.now() + timedelta(seconds=ttl_seconds)
        json_value = json.dumps(value, default=str, ensure_ascii=False)

        # Сохраняем в memory cache
        if self.use_memory_cache:
            async with self._lock:
                self._memory_cache[key] = {
                    'value': value,
                    'expires_at': expires_at
                }

        # Сохраняем в SQLite
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """INSERT OR REPLACE INTO cache (key, value, expires_at, created_at)
                   VALUES (?, ?, ?, ?)""",
                (key, json_value, expires_at.isoformat(), datetime.now().isoformat())
            )
            await db.commit()

    async def delete(self, key: str):
        """Удаление из кэша"""
        if self.use_memory_cache and key in self._memory_cache:
            async with self._lock:
                del self._memory_cache[key]

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM cache WHERE key = ?", (key,))
            await db.commit()

    async def delete_pattern(self, pattern: str):
        """Удаление ключей по паттерну (LIKE)"""
        # Очищаем memory cache
        if self.use_memory_cache:
            async with self._lock:
                keys_to_delete = [
                    k for k in self._memory_cache.keys()
                    if pattern.replace('%', '') in k
                ]
                for k in keys_to_delete:
                    del self._memory_cache[k]

        # Очищаем SQLite
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                "DELETE FROM cache WHERE key LIKE ?",
                (pattern,)
            )
            await db.commit()

    async def clear(self):
        """Полная очистка кэша"""
        if self.use_memory_cache:
            async with self._lock:
                self._memory_cache.clear()

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("DELETE FROM cache")
            await db.commit()

        logger.info("Кэш полностью очищен")

    async def cleanup_expired(self) -> int:
        """Очистка устаревших записей"""
        now = datetime.now()

        # Очистка memory cache
        if self.use_memory_cache:
            async with self._lock:
                expired_keys = [
                    k for k, v in self._memory_cache.items()
                    if v['expires_at'] <= now
                ]
                for k in expired_keys:
                    del self._memory_cache[k]

        # Очистка SQLite
        async with aiosqlite.connect(self.db_path) as db:
            result = await db.execute(
                "DELETE FROM cache WHERE expires_at < ?",
                (now.isoformat(),)
            )
            await db.commit()
            deleted = result.rowcount

        if deleted > 0:
            logger.debug(f"Очищено {deleted} устаревших записей кэша")

        return deleted

    async def get_stats(self) -> Dict[str, Any]:
        """Статистика кэша"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute("SELECT COUNT(*) FROM cache")
            db_count = (await cursor.fetchone())[0]

            cursor = await db.execute(
                "SELECT COUNT(*) FROM cache WHERE expires_at > ?",
                (datetime.now().isoformat(),)
            )
            valid_count = (await cursor.fetchone())[0]

        return {
            'memory_cache_size': len(self._memory_cache),
            'db_cache_size': db_count,
            'valid_entries': valid_count,
            'expired_entries': db_count - valid_count
        }

    async def get_or_set(
        self,
        key: str,
        factory: callable,
        ttl_seconds: int = 3600
    ) -> Any:
        """Получение из кэша или вычисление и сохранение"""
        value = await self.get(key)
        if value is not None:
            return value

        # Вычисляем значение
        if asyncio.iscoroutinefunction(factory):
            value = await factory()
        else:
            value = factory()

        await self.set(key, value, ttl_seconds)
        return value


def cached(key_template: str, ttl_seconds: int = 3600):
    """Декоратор для кэширования результатов функции

    Использование:
    @cached("user:{user_id}", ttl_seconds=300)
    async def get_user(user_id: int): ...
    """
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # Формируем ключ
            cache_key = key_template.format(*args, **kwargs)

            # Проверяем наличие cache_manager
            cache_manager = getattr(self, 'cache_manager', None)
            if not cache_manager:
                return await func(self, *args, **kwargs)

            # Пробуем получить из кэша
            cached_value = await cache_manager.get(cache_key)
            if cached_value is not None:
                return cached_value

            # Вычисляем и кэшируем
            result = await func(self, *args, **kwargs)
            if result is not None:
                await cache_manager.set(cache_key, result, ttl_seconds)

            return result

        return wrapper
    return decorator


class CacheKeys:
    """Предопределённые ключи кэша"""

    # Клиенты
    CLIENT = "client:{uuid}"
    CLIENT_BY_EMAIL = "client:email:{email}"
    CLIENT_SERVERS = "client:{uuid}:servers"

    # Статистика
    DAILY_STATS = "stats:daily:{date}"
    MANAGER_STATS = "stats:manager:{manager_id}"
    TOTAL_STATS = "stats:total"

    # Промокоды
    PROMO = "promo:{code}"
    PROMO_STATS = "promo:{code}:stats"

    # Серверы
    SERVER_STATUS = "server:{name}:status"
    SERVER_CLIENTS = "server:{name}:clients"

    # Настройки
    SETTINGS = "settings:all"
    NOTIFICATION_SETTINGS = "settings:notifications"

    @staticmethod
    def client_key(uuid: str) -> str:
        return CacheKeys.CLIENT.format(uuid=uuid)

    @staticmethod
    def client_email_key(email: str) -> str:
        return CacheKeys.CLIENT_BY_EMAIL.format(email=email)

    @staticmethod
    def daily_stats_key(date: str = None) -> str:
        if not date:
            date = datetime.now().strftime('%Y-%m-%d')
        return CacheKeys.DAILY_STATS.format(date=date)

    @staticmethod
    def server_status_key(name: str) -> str:
        return CacheKeys.SERVER_STATUS.format(name=name)
