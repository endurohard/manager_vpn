"""
Система мониторинга здоровья VPN Manager

Мониторинг:
- Состояние серверов X-UI
- Состояние базы данных
- Метрики производительности
- Алерты при проблемах
"""
import asyncio
import aiohttp
import ssl
import logging
import json
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass, field
from enum import Enum
from collections import deque

logger = logging.getLogger(__name__)


class HealthStatus(Enum):
    """Статусы здоровья"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class HealthCheckResult:
    """Результат проверки здоровья"""
    name: str
    status: HealthStatus
    message: str
    latency_ms: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'status': self.status.value,
            'message': self.message,
            'latency_ms': round(self.latency_ms, 2),
            'details': self.details,
            'timestamp': self.timestamp.isoformat()
        }


@dataclass
class Alert:
    """Алерт о проблеме"""
    severity: str  # critical, warning, info
    component: str
    message: str
    timestamp: datetime = field(default_factory=datetime.now)
    resolved: bool = False
    resolved_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            'severity': self.severity,
            'component': self.component,
            'message': self.message,
            'timestamp': self.timestamp.isoformat(),
            'resolved': self.resolved,
            'resolved_at': self.resolved_at.isoformat() if self.resolved_at else None
        }


class MetricsCollector:
    """
    Сборщик метрик

    Использование:
        metrics = MetricsCollector()

        # Записать метрику
        metrics.record('api_requests', 1)
        metrics.record('response_time', 0.5)

        # Получить статистику
        stats = metrics.get_stats('api_requests')
    """

    def __init__(self, window_size: int = 1000):
        self._metrics: Dict[str, deque] = {}
        self._window_size = window_size
        self._counters: Dict[str, int] = {}

    def record(self, name: str, value: float):
        """Записать значение метрики"""
        if name not in self._metrics:
            self._metrics[name] = deque(maxlen=self._window_size)

        self._metrics[name].append({
            'value': value,
            'timestamp': datetime.now()
        })

    def increment(self, name: str, value: int = 1):
        """Увеличить счётчик"""
        self._counters[name] = self._counters.get(name, 0) + value

    def get_counter(self, name: str) -> int:
        """Получить значение счётчика"""
        return self._counters.get(name, 0)

    def get_stats(self, name: str) -> Dict[str, Any]:
        """Получить статистику по метрике"""
        if name not in self._metrics or not self._metrics[name]:
            return {'count': 0}

        values = [m['value'] for m in self._metrics[name]]

        return {
            'count': len(values),
            'min': min(values),
            'max': max(values),
            'avg': sum(values) / len(values),
            'sum': sum(values),
            'last': values[-1] if values else None
        }

    def get_recent(self, name: str, seconds: int = 60) -> List[Dict]:
        """Получить недавние значения"""
        if name not in self._metrics:
            return []

        cutoff = datetime.now() - timedelta(seconds=seconds)
        return [
            m for m in self._metrics[name]
            if m['timestamp'] > cutoff
        ]

    def get_all_stats(self) -> Dict[str, Any]:
        """Получить всю статистику"""
        return {
            name: self.get_stats(name)
            for name in self._metrics
        }


class HealthChecker:
    """
    Проверка здоровья компонентов системы

    Использование:
        checker = HealthChecker()

        # Регистрация проверок
        checker.register_check('database', check_database)
        checker.register_check('server_1', lambda: check_server(config_1))

        # Запуск всех проверок
        results = await checker.run_all_checks()

        # Получить общий статус
        status = checker.get_overall_status()
    """

    def __init__(self):
        self._checks: Dict[str, Callable] = {}
        self._results: Dict[str, HealthCheckResult] = {}
        self._alerts: deque[Alert] = deque(maxlen=100)
        self._metrics = MetricsCollector()
        self._alert_callbacks: List[Callable] = []

    def register_check(self, name: str, check_fn: Callable):
        """Зарегистрировать проверку"""
        self._checks[name] = check_fn

    def add_alert_callback(self, callback: Callable[[Alert], None]):
        """Добавить callback для алертов"""
        self._alert_callbacks.append(callback)

    async def run_check(self, name: str) -> HealthCheckResult:
        """Выполнить одну проверку"""
        if name not in self._checks:
            return HealthCheckResult(
                name=name,
                status=HealthStatus.UNKNOWN,
                message="Check not registered"
            )

        start_time = asyncio.get_event_loop().time()

        try:
            result = await self._checks[name]()

            if not isinstance(result, HealthCheckResult):
                result = HealthCheckResult(
                    name=name,
                    status=HealthStatus.HEALTHY if result else HealthStatus.UNHEALTHY,
                    message="OK" if result else "Failed"
                )

            result.latency_ms = (asyncio.get_event_loop().time() - start_time) * 1000

        except Exception as e:
            result = HealthCheckResult(
                name=name,
                status=HealthStatus.UNHEALTHY,
                message=str(e),
                latency_ms=(asyncio.get_event_loop().time() - start_time) * 1000
            )

        # Сохраняем результат
        self._results[name] = result

        # Записываем метрику
        self._metrics.record(f'health_{name}_latency', result.latency_ms)
        self._metrics.increment(f'health_{name}_checks')

        if result.status != HealthStatus.HEALTHY:
            self._metrics.increment(f'health_{name}_failures')

        # Проверяем нужен ли алерт
        await self._check_for_alert(result)

        return result

    async def run_all_checks(self) -> Dict[str, HealthCheckResult]:
        """Выполнить все проверки"""
        tasks = [
            self.run_check(name)
            for name in self._checks
        ]

        await asyncio.gather(*tasks, return_exceptions=True)
        return self._results.copy()

    async def _check_for_alert(self, result: HealthCheckResult):
        """Проверить нужно ли создать алерт"""
        if result.status == HealthStatus.UNHEALTHY:
            alert = Alert(
                severity='critical',
                component=result.name,
                message=result.message
            )
            self._alerts.append(alert)

            for callback in self._alert_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(alert)
                    else:
                        callback(alert)
                except Exception as e:
                    logger.error(f"Alert callback error: {e}")

        elif result.status == HealthStatus.DEGRADED:
            alert = Alert(
                severity='warning',
                component=result.name,
                message=result.message
            )
            self._alerts.append(alert)

    def get_overall_status(self) -> HealthStatus:
        """Получить общий статус системы"""
        if not self._results:
            return HealthStatus.UNKNOWN

        statuses = [r.status for r in self._results.values()]

        if any(s == HealthStatus.UNHEALTHY for s in statuses):
            return HealthStatus.UNHEALTHY
        elif any(s == HealthStatus.DEGRADED for s in statuses):
            return HealthStatus.DEGRADED
        elif all(s == HealthStatus.HEALTHY for s in statuses):
            return HealthStatus.HEALTHY

        return HealthStatus.UNKNOWN

    def get_status_summary(self) -> Dict[str, Any]:
        """Получить сводку по статусу"""
        return {
            'overall': self.get_overall_status().value,
            'checks': {
                name: result.to_dict()
                for name, result in self._results.items()
            },
            'timestamp': datetime.now().isoformat()
        }

    def get_alerts(self, unresolved_only: bool = True) -> List[Alert]:
        """Получить алерты"""
        if unresolved_only:
            return [a for a in self._alerts if not a.resolved]
        return list(self._alerts)

    def resolve_alert(self, component: str):
        """Разрешить алерт"""
        for alert in self._alerts:
            if alert.component == component and not alert.resolved:
                alert.resolved = True
                alert.resolved_at = datetime.now()

    def get_metrics(self) -> Dict[str, Any]:
        """Получить метрики"""
        return self._metrics.get_all_stats()


# ============================================================================
# Готовые проверки
# ============================================================================

async def check_server_health(
    ip: str,
    port: int,
    panel_path: str = "",
    username: str = "",
    password: str = "",
    timeout: float = 10.0
) -> HealthCheckResult:
    """Проверить здоровье X-UI сервера"""
    name = f"server_{ip}"
    base_url = f"https://{ip}:{port}/{panel_path}".rstrip('/')

    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    connector = aiohttp.TCPConnector(ssl=ssl_ctx)
    client_timeout = aiohttp.ClientTimeout(total=timeout)

    try:
        async with aiohttp.ClientSession(
            connector=connector,
            timeout=client_timeout
        ) as session:
            # Проверяем доступность
            start = asyncio.get_event_loop().time()

            async with session.post(
                f"{base_url}/login",
                data={'username': username, 'password': password}
            ) as resp:
                latency = (asyncio.get_event_loop().time() - start) * 1000

                if resp.status == 200:
                    data = await resp.json()
                    if data.get('success'):
                        return HealthCheckResult(
                            name=name,
                            status=HealthStatus.HEALTHY,
                            message="Server is healthy",
                            latency_ms=latency,
                            details={'login': True}
                        )
                    else:
                        return HealthCheckResult(
                            name=name,
                            status=HealthStatus.DEGRADED,
                            message="Auth failed",
                            latency_ms=latency,
                            details={'login': False}
                        )
                else:
                    return HealthCheckResult(
                        name=name,
                        status=HealthStatus.UNHEALTHY,
                        message=f"HTTP {resp.status}",
                        latency_ms=latency
                    )

    except asyncio.TimeoutError:
        return HealthCheckResult(
            name=name,
            status=HealthStatus.UNHEALTHY,
            message=f"Timeout after {timeout}s"
        )
    except aiohttp.ClientConnectorError as e:
        return HealthCheckResult(
            name=name,
            status=HealthStatus.UNHEALTHY,
            message=f"Connection failed: {e}"
        )
    except Exception as e:
        return HealthCheckResult(
            name=name,
            status=HealthStatus.UNHEALTHY,
            message=str(e)
        )


async def check_database_health(db_path: str) -> HealthCheckResult:
    """Проверить здоровье базы данных"""
    import aiosqlite

    try:
        start = asyncio.get_event_loop().time()

        async with aiosqlite.connect(db_path) as db:
            # Простой запрос для проверки
            cursor = await db.execute("SELECT 1")
            await cursor.fetchone()

            # Получаем статистику
            cursor = await db.execute("SELECT COUNT(*) FROM users")
            users_count = (await cursor.fetchone())[0]

            cursor = await db.execute("SELECT COUNT(*) FROM keys")
            keys_count = (await cursor.fetchone())[0]

        latency = (asyncio.get_event_loop().time() - start) * 1000

        return HealthCheckResult(
            name="database",
            status=HealthStatus.HEALTHY,
            message="Database is healthy",
            latency_ms=latency,
            details={
                'users_count': users_count,
                'keys_count': keys_count
            }
        )

    except Exception as e:
        return HealthCheckResult(
            name="database",
            status=HealthStatus.UNHEALTHY,
            message=str(e)
        )


class HealthMonitor:
    """
    Фоновый мониторинг здоровья

    Использование:
        monitor = HealthMonitor(check_interval=60)

        # Добавляем проверки
        monitor.add_server_check(server_config)
        monitor.add_database_check(db_path)

        # Запускаем мониторинг
        await monitor.start()

        # Получаем статус
        status = monitor.get_status()

        # Останавливаем
        await monitor.stop()
    """

    def __init__(self, check_interval: float = 60.0):
        self.check_interval = check_interval
        self.checker = HealthChecker()
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def add_server_check(self, server_config: Dict):
        """Добавить проверку сервера"""
        name = f"server_{server_config.get('name', server_config.get('ip'))}"
        panel = server_config.get('panel', {})

        async def check():
            return await check_server_health(
                ip=server_config.get('ip'),
                port=server_config.get('port', 443),
                panel_path=panel.get('path', ''),
                username=panel.get('username', ''),
                password=panel.get('password', ''),
                timeout=panel.get('timeout', 10.0)
            )

        self.checker.register_check(name, check)

    def add_database_check(self, db_path: str):
        """Добавить проверку БД"""
        async def check():
            return await check_database_health(db_path)

        self.checker.register_check('database', check)

    def add_custom_check(self, name: str, check_fn: Callable):
        """Добавить пользовательскую проверку"""
        self.checker.register_check(name, check_fn)

    def add_alert_callback(self, callback: Callable[[Alert], None]):
        """Добавить callback для алертов"""
        self.checker.add_alert_callback(callback)

    async def _monitor_loop(self):
        """Цикл мониторинга"""
        while self._running:
            try:
                await self.checker.run_all_checks()
                logger.debug(f"Health check completed: {self.checker.get_overall_status().value}")
            except Exception as e:
                logger.error(f"Health check error: {e}")

            await asyncio.sleep(self.check_interval)

    async def start(self):
        """Запустить мониторинг"""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info(f"Health monitor started (interval: {self.check_interval}s)")

    async def stop(self):
        """Остановить мониторинг"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Health monitor stopped")

    def get_status(self) -> Dict[str, Any]:
        """Получить текущий статус"""
        return self.checker.get_status_summary()

    def get_alerts(self, unresolved_only: bool = True) -> List[Dict]:
        """Получить алерты"""
        return [a.to_dict() for a in self.checker.get_alerts(unresolved_only)]

    def get_metrics(self) -> Dict[str, Any]:
        """Получить метрики"""
        return self.checker.get_metrics()


# Глобальный экземпляр монитора
_monitor: Optional[HealthMonitor] = None


def get_health_monitor() -> HealthMonitor:
    """Получить глобальный монитор"""
    global _monitor
    if _monitor is None:
        _monitor = HealthMonitor()
    return _monitor


__all__ = [
    'HealthStatus',
    'HealthCheckResult',
    'Alert',
    'MetricsCollector',
    'HealthChecker',
    'HealthMonitor',
    'check_server_health',
    'check_database_health',
    'get_health_monitor',
]
