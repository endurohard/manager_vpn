"""
Унифицированная система retry, кэширования и async утилит
"""
import asyncio
import functools
import time
import logging
from typing import TypeVar, Callable, Any, Optional, Dict
from dataclasses import dataclass, field
from collections import OrderedDict
from enum import Enum

logger = logging.getLogger(__name__)

T = TypeVar('T')


class RetryStrategy(Enum):
    """Стратегии повторных попыток"""
    EXPONENTIAL = "exponential"
    LINEAR = "linear"
    CONSTANT = "constant"


@dataclass
class RetryConfig:
    """Конфигурация retry"""
    max_attempts: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL
    exceptions: tuple = (Exception,)
    on_retry: Optional[Callable] = None


def calculate_delay(attempt: int, config: RetryConfig) -> float:
    """Вычислить задержку перед следующей попыткой"""
    if config.strategy == RetryStrategy.EXPONENTIAL:
        delay = config.base_delay * (2 ** attempt)
    elif config.strategy == RetryStrategy.LINEAR:
        delay = config.base_delay * (attempt + 1)
    else:
        delay = config.base_delay

    return min(delay, config.max_delay)


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL,
    exceptions: tuple = (Exception,),
    on_retry: Optional[Callable] = None
):
    """
    Декоратор для автоматического retry с настраиваемой стратегией

    Использование:
        @retry(max_attempts=3, base_delay=1.0)
        async def fetch_data():
            ...
    """
    config = RetryConfig(
        max_attempts=max_attempts,
        base_delay=base_delay,
        max_delay=max_delay,
        strategy=strategy,
        exceptions=exceptions,
        on_retry=on_retry
    )

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            last_exception = None

            for attempt in range(config.max_attempts):
                try:
                    return await func(*args, **kwargs)
                except config.exceptions as e:
                    last_exception = e

                    if attempt < config.max_attempts - 1:
                        delay = calculate_delay(attempt, config)

                        logger.warning(
                            f"Retry {attempt + 1}/{config.max_attempts} for {func.__name__}: {e}. "
                            f"Waiting {delay:.1f}s"
                        )

                        if config.on_retry:
                            try:
                                config.on_retry(attempt, e, func.__name__)
                            except Exception:
                                pass

                        await asyncio.sleep(delay)
                    else:
                        logger.error(
                            f"All {config.max_attempts} attempts failed for {func.__name__}: {e}"
                        )

            raise last_exception

        return wrapper
    return decorator


@dataclass
class CacheEntry:
    """Запись в кэше"""
    value: Any
    expires_at: float
    hits: int = 0


class AsyncCache:
    """
    Асинхронный LRU кэш с TTL

    Использование:
        cache = AsyncCache(max_size=100, default_ttl=300)

        @cache.cached(ttl=60)
        async def get_data(key):
            ...
    """

    def __init__(self, max_size: int = 1000, default_ttl: float = 300):
        self.max_size = max_size
        self.default_ttl = default_ttl
        self._cache: OrderedDict[str, CacheEntry] = OrderedDict()
        self._lock = asyncio.Lock()
        self._stats = {'hits': 0, 'misses': 0, 'evictions': 0}

    def _make_key(self, func_name: str, args: tuple, kwargs: dict) -> str:
        """Создать ключ кэша из аргументов"""
        key_parts = [func_name]
        key_parts.extend(str(arg) for arg in args)
        key_parts.extend(f"{k}={v}" for k, v in sorted(kwargs.items()))
        return ":".join(key_parts)

    async def get(self, key: str) -> Optional[Any]:
        """Получить значение из кэша"""
        async with self._lock:
            if key not in self._cache:
                self._stats['misses'] += 1
                return None

            entry = self._cache[key]

            # Проверка TTL
            if time.time() > entry.expires_at:
                del self._cache[key]
                self._stats['misses'] += 1
                return None

            # LRU: перемещаем в конец
            self._cache.move_to_end(key)
            entry.hits += 1
            self._stats['hits'] += 1

            return entry.value

    async def set(self, key: str, value: Any, ttl: Optional[float] = None):
        """Установить значение в кэш"""
        async with self._lock:
            # Удаляем старые записи если превышен лимит
            while len(self._cache) >= self.max_size:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
                self._stats['evictions'] += 1

            expires_at = time.time() + (ttl or self.default_ttl)
            self._cache[key] = CacheEntry(value=value, expires_at=expires_at)

    async def delete(self, key: str) -> bool:
        """Удалить значение из кэша"""
        async with self._lock:
            if key in self._cache:
                del self._cache[key]
                return True
            return False

    async def clear(self):
        """Очистить кэш"""
        async with self._lock:
            self._cache.clear()

    async def cleanup_expired(self):
        """Удалить просроченные записи"""
        async with self._lock:
            now = time.time()
            expired_keys = [
                key for key, entry in self._cache.items()
                if now > entry.expires_at
            ]
            for key in expired_keys:
                del self._cache[key]
            return len(expired_keys)

    def get_stats(self) -> Dict[str, Any]:
        """Получить статистику кэша"""
        total = self._stats['hits'] + self._stats['misses']
        hit_rate = self._stats['hits'] / total if total > 0 else 0

        return {
            'size': len(self._cache),
            'max_size': self.max_size,
            'hits': self._stats['hits'],
            'misses': self._stats['misses'],
            'hit_rate': f"{hit_rate:.1%}",
            'evictions': self._stats['evictions']
        }

    def cached(self, ttl: Optional[float] = None, key_prefix: str = ""):
        """
        Декоратор для кэширования результатов функции

        Использование:
            @cache.cached(ttl=60)
            async def get_user(user_id):
                ...
        """
        def decorator(func: Callable[..., T]) -> Callable[..., T]:
            @functools.wraps(func)
            async def wrapper(*args, **kwargs) -> T:
                # Формируем ключ
                func_name = key_prefix or func.__name__
                cache_key = self._make_key(func_name, args, kwargs)

                # Пробуем получить из кэша
                cached_value = await self.get(cache_key)
                if cached_value is not None:
                    return cached_value

                # Вызываем функцию и кэшируем результат
                result = await func(*args, **kwargs)
                await self.set(cache_key, result, ttl)

                return result

            # Добавляем методы для управления кэшем конкретной функции
            wrapper.cache_clear = lambda: self.clear()
            wrapper.cache_stats = lambda: self.get_stats()

            return wrapper
        return decorator


# Глобальный экземпляр кэша
_global_cache = AsyncCache(max_size=1000, default_ttl=300)


def cached(ttl: float = 300, key_prefix: str = ""):
    """
    Глобальный декоратор кэширования

    Использование:
        @cached(ttl=60)
        async def get_config():
            ...
    """
    return _global_cache.cached(ttl=ttl, key_prefix=key_prefix)


class CircuitBreaker:
    """
    Circuit Breaker для защиты от каскадных сбоев

    Состояния:
    - CLOSED: нормальная работа
    - OPEN: сбой, все вызовы отклоняются
    - HALF_OPEN: тестовый режим после timeout
    """

    class State(Enum):
        CLOSED = "closed"
        OPEN = "open"
        HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        half_open_requests: int = 3
    ):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.half_open_requests = half_open_requests

        self._state = self.State.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._half_open_successes = 0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> State:
        return self._state

    async def _check_state(self):
        """Проверить и обновить состояние"""
        if self._state == self.State.OPEN:
            if time.time() - self._last_failure_time >= self.recovery_timeout:
                self._state = self.State.HALF_OPEN
                self._half_open_successes = 0
                logger.info("Circuit breaker: OPEN -> HALF_OPEN")

    async def call(self, func: Callable[..., T], *args, **kwargs) -> T:
        """Выполнить вызов через circuit breaker"""
        async with self._lock:
            await self._check_state()

            if self._state == self.State.OPEN:
                raise CircuitBreakerOpenError(
                    f"Circuit breaker is OPEN. Retry after "
                    f"{self.recovery_timeout - (time.time() - self._last_failure_time):.1f}s"
                )

        try:
            result = await func(*args, **kwargs)

            async with self._lock:
                if self._state == self.State.HALF_OPEN:
                    self._half_open_successes += 1
                    if self._half_open_successes >= self.half_open_requests:
                        self._state = self.State.CLOSED
                        self._failure_count = 0
                        logger.info("Circuit breaker: HALF_OPEN -> CLOSED")
                else:
                    self._failure_count = 0

            return result

        except Exception as e:
            async with self._lock:
                self._failure_count += 1
                self._last_failure_time = time.time()

                if self._state == self.State.HALF_OPEN:
                    self._state = self.State.OPEN
                    logger.warning("Circuit breaker: HALF_OPEN -> OPEN (failure)")
                elif self._failure_count >= self.failure_threshold:
                    self._state = self.State.OPEN
                    logger.warning(
                        f"Circuit breaker: CLOSED -> OPEN "
                        f"(failures: {self._failure_count})"
                    )

            raise


class CircuitBreakerOpenError(Exception):
    """Circuit breaker в состоянии OPEN"""
    pass


def circuit_breaker(
    failure_threshold: int = 5,
    recovery_timeout: float = 30.0
):
    """
    Декоратор circuit breaker

    Использование:
        @circuit_breaker(failure_threshold=5, recovery_timeout=30)
        async def external_api_call():
            ...
    """
    breaker = CircuitBreaker(
        failure_threshold=failure_threshold,
        recovery_timeout=recovery_timeout
    )

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            return await breaker.call(func, *args, **kwargs)

        wrapper.circuit_breaker = breaker
        return wrapper

    return decorator


async def run_with_timeout(
    coro,
    timeout: float,
    default: Any = None,
    raise_on_timeout: bool = False
) -> Any:
    """
    Выполнить корутину с таймаутом

    Использование:
        result = await run_with_timeout(fetch_data(), timeout=5.0, default=[])
    """
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        if raise_on_timeout:
            raise
        logger.warning(f"Operation timed out after {timeout}s")
        return default


async def gather_with_concurrency(
    limit: int,
    *coros,
    return_exceptions: bool = False
) -> list:
    """
    asyncio.gather с ограничением одновременных операций

    Использование:
        results = await gather_with_concurrency(5, *[fetch(url) for url in urls])
    """
    semaphore = asyncio.Semaphore(limit)

    async def limited_coro(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(
        *[limited_coro(c) for c in coros],
        return_exceptions=return_exceptions
    )


class RateLimiter:
    """
    Rate limiter с использованием token bucket алгоритма

    Использование:
        limiter = RateLimiter(rate=10, per=1.0)  # 10 запросов в секунду

        async with limiter:
            await make_request()
    """

    def __init__(self, rate: int, per: float = 1.0):
        self.rate = rate
        self.per = per
        self.tokens = rate
        self.last_update = time.time()
        self._lock = asyncio.Lock()

    async def acquire(self):
        """Получить токен"""
        async with self._lock:
            now = time.time()
            elapsed = now - self.last_update

            # Добавляем токены
            self.tokens = min(
                self.rate,
                self.tokens + elapsed * (self.rate / self.per)
            )
            self.last_update = now

            if self.tokens < 1:
                # Ждем пока появится токен
                wait_time = (1 - self.tokens) * (self.per / self.rate)
                await asyncio.sleep(wait_time)
                self.tokens = 0
            else:
                self.tokens -= 1

    async def __aenter__(self):
        await self.acquire()
        return self

    async def __aexit__(self, *args):
        pass


def rate_limited(rate: int, per: float = 1.0):
    """
    Декоратор rate limiting

    Использование:
        @rate_limited(rate=10, per=1.0)  # 10 вызовов в секунду
        async def api_call():
            ...
    """
    limiter = RateLimiter(rate=rate, per=per)

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> T:
            async with limiter:
                return await func(*args, **kwargs)
        return wrapper

    return decorator


# Экспорт для удобного импорта
__all__ = [
    'retry',
    'RetryStrategy',
    'RetryConfig',
    'AsyncCache',
    'cached',
    'CircuitBreaker',
    'CircuitBreakerOpenError',
    'circuit_breaker',
    'run_with_timeout',
    'gather_with_concurrency',
    'RateLimiter',
    'rate_limited',
]
