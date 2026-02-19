"""
Иерархия ошибок и система трекинга для VPN Manager

Централизованная обработка всех типов ошибок в проекте.
"""
import logging
import traceback
import asyncio
from datetime import datetime
from typing import Optional, Dict, Any, List, Callable
from dataclasses import dataclass, field
from enum import Enum
from collections import deque

logger = logging.getLogger(__name__)


# ============================================================================
# Базовые классы ошибок
# ============================================================================

class VPNManagerError(Exception):
    """Базовая ошибка VPN Manager"""

    def __init__(self, message: str, code: str = "UNKNOWN", details: Dict[str, Any] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.details = details or {}
        self.timestamp = datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": self.__class__.__name__,
            "code": self.code,
            "message": self.message,
            "details": self.details,
            "timestamp": self.timestamp.isoformat()
        }

    def __str__(self):
        return f"[{self.code}] {self.message}"


# ============================================================================
# Ошибки API
# ============================================================================

class APIError(VPNManagerError):
    """Базовая ошибка API"""

    def __init__(self, message: str, code: str = "API_ERROR",
                 status_code: int = None, response: Any = None, **kwargs):
        super().__init__(message, code, kwargs.get('details', {}))
        self.status_code = status_code
        self.response = response


class AuthenticationError(APIError):
    """Ошибка аутентификации в панели"""

    def __init__(self, message: str = "Ошибка аутентификации", server: str = None):
        super().__init__(message, code="AUTH_ERROR", details={'server': server})


class ConnectionError(APIError):
    """Ошибка подключения к серверу"""

    def __init__(self, message: str = "Не удалось подключиться", server: str = None, original: Exception = None):
        details = {'server': server}
        if original:
            details['original_error'] = str(original)
        super().__init__(message, code="CONNECTION_ERROR", details=details)


class TimeoutError(APIError):
    """Таймаут операции"""

    def __init__(self, message: str = "Превышено время ожидания", operation: str = None, timeout: float = None):
        super().__init__(message, code="TIMEOUT_ERROR",
                         details={'operation': operation, 'timeout': timeout})


class RateLimitError(APIError):
    """Превышен лимит запросов"""

    def __init__(self, message: str = "Превышен лимит запросов", retry_after: float = None):
        super().__init__(message, code="RATE_LIMIT", details={'retry_after': retry_after})


class PanelAPIError(APIError):
    """Ошибка API X-UI панели"""

    def __init__(self, message: str, server: str = None, endpoint: str = None,
                 status_code: int = None, response: Any = None):
        super().__init__(
            message,
            code="PANEL_API_ERROR",
            status_code=status_code,
            response=response,
            details={'server': server, 'endpoint': endpoint}
        )


# ============================================================================
# Ошибки базы данных
# ============================================================================

class DatabaseError(VPNManagerError):
    """Базовая ошибка БД"""

    def __init__(self, message: str, code: str = "DB_ERROR", query: str = None, **kwargs):
        super().__init__(message, code, kwargs.get('details', {}))
        self.query = query


class RecordNotFoundError(DatabaseError):
    """Запись не найдена"""

    def __init__(self, table: str, identifier: Any):
        super().__init__(
            f"Запись не найдена в {table}",
            code="NOT_FOUND",
            details={'table': table, 'identifier': identifier}
        )


class DuplicateError(DatabaseError):
    """Дубликат записи"""

    def __init__(self, table: str, field: str, value: Any):
        super().__init__(
            f"Дубликат {field}={value} в {table}",
            code="DUPLICATE",
            details={'table': table, 'field': field, 'value': value}
        )


class IntegrityError(DatabaseError):
    """Нарушение целостности данных"""

    def __init__(self, message: str, constraint: str = None):
        super().__init__(message, code="INTEGRITY_ERROR", details={'constraint': constraint})


# ============================================================================
# Ошибки клиентов/ключей
# ============================================================================

class ClientError(VPNManagerError):
    """Ошибки связанные с клиентами"""
    pass


class ClientNotFoundError(ClientError):
    """Клиент не найден"""

    def __init__(self, identifier: str, search_type: str = "uuid"):
        super().__init__(
            f"Клиент не найден: {identifier}",
            code="CLIENT_NOT_FOUND",
            details={'identifier': identifier, 'search_type': search_type}
        )


class KeyCreationError(ClientError):
    """Ошибка создания ключа"""

    def __init__(self, message: str, server: str = None, client_name: str = None):
        super().__init__(
            message,
            code="KEY_CREATION_ERROR",
            details={'server': server, 'client_name': client_name}
        )


class KeyDeletionError(ClientError):
    """Ошибка удаления ключа"""

    def __init__(self, message: str, server: str = None, client_uuid: str = None):
        super().__init__(
            message,
            code="KEY_DELETION_ERROR",
            details={'server': server, 'client_uuid': client_uuid}
        )


class KeyExpiredError(ClientError):
    """Ключ истёк"""

    def __init__(self, client_uuid: str, expired_at: datetime = None):
        super().__init__(
            "Срок действия ключа истёк",
            code="KEY_EXPIRED",
            details={'client_uuid': client_uuid, 'expired_at': expired_at.isoformat() if expired_at else None}
        )


class TrafficExceededError(ClientError):
    """Превышен лимит трафика"""

    def __init__(self, client_uuid: str, used: int, limit: int):
        super().__init__(
            f"Превышен лимит трафика: {used}/{limit} bytes",
            code="TRAFFIC_EXCEEDED",
            details={'client_uuid': client_uuid, 'used': used, 'limit': limit}
        )


# ============================================================================
# Ошибки серверов
# ============================================================================

class ServerError(VPNManagerError):
    """Ошибки связанные с серверами"""
    pass


class ServerUnavailableError(ServerError):
    """Сервер недоступен"""

    def __init__(self, server_name: str, reason: str = None):
        super().__init__(
            f"Сервер {server_name} недоступен",
            code="SERVER_UNAVAILABLE",
            details={'server': server_name, 'reason': reason}
        )


class ServerConfigError(ServerError):
    """Ошибка конфигурации сервера"""

    def __init__(self, server_name: str, issue: str):
        super().__init__(
            f"Ошибка конфигурации {server_name}: {issue}",
            code="SERVER_CONFIG_ERROR",
            details={'server': server_name, 'issue': issue}
        )


class NoAvailableServersError(ServerError):
    """Нет доступных серверов"""

    def __init__(self, message: str = "Нет доступных серверов для создания ключа"):
        super().__init__(message, code="NO_SERVERS")


# ============================================================================
# Ошибки валидации
# ============================================================================

class ValidationError(VPNManagerError):
    """Ошибка валидации данных"""

    def __init__(self, message: str, field: str = None, value: Any = None):
        super().__init__(
            message,
            code="VALIDATION_ERROR",
            details={'field': field, 'value': value}
        )


class InvalidInputError(ValidationError):
    """Неверные входные данные"""
    pass


class MissingRequiredFieldError(ValidationError):
    """Отсутствует обязательное поле"""

    def __init__(self, field: str):
        super().__init__(f"Отсутствует обязательное поле: {field}", field=field)


# ============================================================================
# Ошибки Telegram
# ============================================================================

class TelegramError(VPNManagerError):
    """Ошибки Telegram бота"""
    pass


class UserBlockedBotError(TelegramError):
    """Пользователь заблокировал бота"""

    def __init__(self, user_id: int):
        super().__init__(
            "Пользователь заблокировал бота",
            code="USER_BLOCKED",
            details={'user_id': user_id}
        )


class MessageSendError(TelegramError):
    """Ошибка отправки сообщения"""

    def __init__(self, user_id: int, reason: str = None):
        super().__init__(
            "Не удалось отправить сообщение",
            code="MESSAGE_SEND_ERROR",
            details={'user_id': user_id, 'reason': reason}
        )


# ============================================================================
# Error Tracker
# ============================================================================

class ErrorSeverity(Enum):
    """Уровни серьезности ошибок"""
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class ErrorRecord:
    """Запись об ошибке"""
    error_type: str
    message: str
    code: str
    severity: ErrorSeverity
    timestamp: datetime
    details: Dict[str, Any] = field(default_factory=dict)
    traceback: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)
    resolved: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            'error_type': self.error_type,
            'message': self.message,
            'code': self.code,
            'severity': self.severity.value,
            'timestamp': self.timestamp.isoformat(),
            'details': self.details,
            'traceback': self.traceback,
            'context': self.context,
            'resolved': self.resolved
        }


class ErrorTracker:
    """
    Централизованный трекер ошибок

    Использование:
        tracker = ErrorTracker.get_instance()

        try:
            ...
        except Exception as e:
            tracker.track(e, context={'user_id': 123})

        # Получить последние ошибки
        recent = tracker.get_recent_errors(limit=10)

        # Получить статистику
        stats = tracker.get_error_stats()
    """

    _instance = None
    _lock = asyncio.Lock()

    def __init__(self, max_errors: int = 1000):
        self.max_errors = max_errors
        self._errors: deque[ErrorRecord] = deque(maxlen=max_errors)
        self._error_counts: Dict[str, int] = {}
        self._callbacks: List[Callable] = []

    @classmethod
    def get_instance(cls) -> 'ErrorTracker':
        """Получить singleton экземпляр"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _determine_severity(self, error: Exception) -> ErrorSeverity:
        """Определить серьезность ошибки"""
        if isinstance(error, (AuthenticationError, ServerUnavailableError)):
            return ErrorSeverity.CRITICAL
        elif isinstance(error, (PanelAPIError, DatabaseError)):
            return ErrorSeverity.ERROR
        elif isinstance(error, (TimeoutError, RateLimitError)):
            return ErrorSeverity.WARNING
        elif isinstance(error, ValidationError):
            return ErrorSeverity.INFO
        else:
            return ErrorSeverity.ERROR

    def track(
        self,
        error: Exception,
        context: Dict[str, Any] = None,
        severity: ErrorSeverity = None,
        include_traceback: bool = True
    ) -> ErrorRecord:
        """Записать ошибку"""
        error_type = type(error).__name__

        # Определяем код и детали
        if isinstance(error, VPNManagerError):
            code = error.code
            details = error.details
            message = error.message
        else:
            code = "UNKNOWN"
            details = {}
            message = str(error)

        # Создаем запись
        record = ErrorRecord(
            error_type=error_type,
            message=message,
            code=code,
            severity=severity or self._determine_severity(error),
            timestamp=datetime.now(),
            details=details,
            traceback=traceback.format_exc() if include_traceback else None,
            context=context or {}
        )

        # Сохраняем
        self._errors.append(record)

        # Обновляем счетчики
        self._error_counts[error_type] = self._error_counts.get(error_type, 0) + 1

        # Логируем
        log_message = f"[{record.severity.value.upper()}] {error_type}: {message}"
        if record.severity == ErrorSeverity.CRITICAL:
            logger.critical(log_message)
        elif record.severity == ErrorSeverity.ERROR:
            logger.error(log_message)
        elif record.severity == ErrorSeverity.WARNING:
            logger.warning(log_message)
        else:
            logger.info(log_message)

        # Вызываем callbacks
        for callback in self._callbacks:
            try:
                callback(record)
            except Exception as e:
                logger.error(f"Error in error tracker callback: {e}")

        return record

    def add_callback(self, callback: Callable[[ErrorRecord], None]):
        """Добавить callback для уведомления об ошибках"""
        self._callbacks.append(callback)

    def get_recent_errors(
        self,
        limit: int = 10,
        error_type: str = None,
        severity: ErrorSeverity = None,
        since: datetime = None
    ) -> List[ErrorRecord]:
        """Получить последние ошибки с фильтрацией"""
        errors = list(self._errors)

        if error_type:
            errors = [e for e in errors if e.error_type == error_type]

        if severity:
            errors = [e for e in errors if e.severity == severity]

        if since:
            errors = [e for e in errors if e.timestamp >= since]

        return list(reversed(errors))[:limit]

    def get_error_stats(self) -> Dict[str, Any]:
        """Получить статистику ошибок"""
        if not self._errors:
            return {
                'total': 0,
                'by_type': {},
                'by_severity': {},
                'recent_rate': 0
            }

        # По типам
        by_type = dict(self._error_counts)

        # По серьезности
        by_severity = {}
        for error in self._errors:
            sev = error.severity.value
            by_severity[sev] = by_severity.get(sev, 0) + 1

        # Частота за последний час
        one_hour_ago = datetime.now().timestamp() - 3600
        recent_count = sum(
            1 for e in self._errors
            if e.timestamp.timestamp() > one_hour_ago
        )

        return {
            'total': len(self._errors),
            'by_type': by_type,
            'by_severity': by_severity,
            'recent_rate': recent_count,
            'top_errors': sorted(by_type.items(), key=lambda x: x[1], reverse=True)[:5]
        }

    def clear(self):
        """Очистить историю ошибок"""
        self._errors.clear()
        self._error_counts.clear()


# Удобные функции для быстрого использования
_tracker = ErrorTracker.get_instance()


def track_error(error: Exception, context: Dict[str, Any] = None) -> ErrorRecord:
    """Быстрый трекинг ошибки"""
    return _tracker.track(error, context)


def get_error_stats() -> Dict[str, Any]:
    """Получить статистику ошибок"""
    return _tracker.get_error_stats()


# ============================================================================
# Контекстный менеджер для обработки ошибок
# ============================================================================

class error_handler:
    """
    Контекстный менеджер для обработки ошибок

    Использование:
        async with error_handler(context={'operation': 'create_key'}) as handler:
            result = await create_key()

        if handler.error:
            print(f"Произошла ошибка: {handler.error}")
    """

    def __init__(
        self,
        context: Dict[str, Any] = None,
        reraise: bool = True,
        default_value: Any = None,
        transform_error: Callable[[Exception], Exception] = None
    ):
        self.context = context or {}
        self.reraise = reraise
        self.default_value = default_value
        self.transform_error = transform_error
        self.error: Optional[Exception] = None
        self.record: Optional[ErrorRecord] = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if exc_val is not None:
            self.error = exc_val

            # Трансформируем ошибку если нужно
            if self.transform_error:
                try:
                    exc_val = self.transform_error(exc_val)
                except Exception:
                    pass

            # Записываем в трекер
            self.record = track_error(exc_val, self.context)

            if self.reraise:
                return False  # Пробрасываем ошибку
            else:
                return True  # Подавляем ошибку

        return False


__all__ = [
    # Базовые
    'VPNManagerError',

    # API ошибки
    'APIError',
    'AuthenticationError',
    'ConnectionError',
    'TimeoutError',
    'RateLimitError',
    'PanelAPIError',

    # БД ошибки
    'DatabaseError',
    'RecordNotFoundError',
    'DuplicateError',
    'IntegrityError',

    # Клиентские ошибки
    'ClientError',
    'ClientNotFoundError',
    'KeyCreationError',
    'KeyDeletionError',
    'KeyExpiredError',
    'TrafficExceededError',

    # Серверные ошибки
    'ServerError',
    'ServerUnavailableError',
    'ServerConfigError',
    'NoAvailableServersError',

    # Валидация
    'ValidationError',
    'InvalidInputError',
    'MissingRequiredFieldError',

    # Telegram
    'TelegramError',
    'UserBlockedBotError',
    'MessageSendError',

    # Error Tracker
    'ErrorSeverity',
    'ErrorRecord',
    'ErrorTracker',
    'track_error',
    'get_error_stats',
    'error_handler',
]
