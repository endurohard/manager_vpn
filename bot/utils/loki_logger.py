"""
Модуль для отправки логов в Grafana Loki

Конфигурация через переменные окружения:
- LOKI_URL: URL Loki API (например, http://localhost:3100)
- LOKI_ENABLED: включить/выключить отправку в Loki (true/false)
- LOKI_LABELS: дополнительные labels в формате key=value,key2=value2
"""
import os
import json
import time
import logging
import queue
import threading
import atexit
from datetime import datetime
from typing import Dict, Optional, Any
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logger = logging.getLogger(__name__)


class LokiHandler(logging.Handler):
    """
    Logging handler для отправки логов в Grafana Loki

    Использует буферизацию и фоновую отправку для минимального влияния на производительность
    """

    def __init__(
        self,
        url: str,
        labels: Dict[str, str] = None,
        batch_size: int = 100,
        flush_interval: float = 5.0,
        timeout: float = 10.0
    ):
        """
        :param url: URL Loki push API (например, http://localhost:3100/loki/api/v1/push)
        :param labels: Статические labels для всех логов
        :param batch_size: Размер батча перед отправкой
        :param flush_interval: Интервал принудительной отправки (секунды)
        :param timeout: Таймаут HTTP запроса
        """
        super().__init__()

        self.url = url.rstrip('/') + '/loki/api/v1/push'
        self.labels = labels or {}
        self.labels.setdefault('app', 'vpn_manager_bot')
        self.labels.setdefault('host', os.uname().nodename)

        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.timeout = timeout

        self._queue = queue.Queue()
        self._shutdown = threading.Event()

        # Фоновый поток для отправки
        self._sender_thread = threading.Thread(target=self._sender_loop, daemon=True)
        self._sender_thread.start()

        # Отправка при завершении
        atexit.register(self.close)

    def emit(self, record: logging.LogRecord):
        """Добавить лог в очередь"""
        try:
            # Формируем лог entry
            entry = {
                'timestamp': int(time.time_ns()),
                'line': self.format(record),
                'level': record.levelname,
                'logger': record.name,
                'func': record.funcName,
                'file': f"{record.filename}:{record.lineno}"
            }

            # Добавляем extra поля
            if hasattr(record, 'user_id'):
                entry['user_id'] = str(record.user_id)
            if hasattr(record, 'action'):
                entry['action'] = record.action
            if hasattr(record, 'server'):
                entry['server'] = record.server

            self._queue.put(entry)

        except Exception as e:
            self.handleError(record)

    def _sender_loop(self):
        """Фоновый цикл отправки логов"""
        batch = []
        last_flush = time.time()

        while not self._shutdown.is_set():
            try:
                # Получаем элемент с таймаутом
                try:
                    entry = self._queue.get(timeout=1.0)
                    batch.append(entry)
                except queue.Empty:
                    pass

                # Проверяем, нужно ли отправлять
                should_flush = (
                    len(batch) >= self.batch_size or
                    (batch and time.time() - last_flush >= self.flush_interval)
                )

                if should_flush and batch:
                    self._send_batch(batch)
                    batch = []
                    last_flush = time.time()

            except Exception as e:
                logger.warning(f"Loki sender error: {e}")

        # Отправляем оставшиеся логи при завершении
        if batch:
            self._send_batch(batch)

    def _send_batch(self, batch: list):
        """Отправить батч логов в Loki"""
        try:
            # Формируем payload в формате Loki
            streams = {}

            for entry in batch:
                # Формируем labels для этого entry
                labels = dict(self.labels)
                labels['level'] = entry.get('level', 'INFO')
                labels['logger'] = entry.get('logger', 'root')

                if 'action' in entry:
                    labels['action'] = entry['action']
                if 'server' in entry:
                    labels['server'] = entry['server']

                # Ключ для группировки
                label_key = json.dumps(labels, sort_keys=True)

                if label_key not in streams:
                    streams[label_key] = {
                        'stream': labels,
                        'values': []
                    }

                # Добавляем значение
                timestamp_ns = str(entry['timestamp'])
                line = entry.get('line', '')

                # Добавляем метаданные в строку если есть
                meta_parts = []
                if 'user_id' in entry:
                    meta_parts.append(f"user_id={entry['user_id']}")
                if 'file' in entry:
                    meta_parts.append(f"file={entry['file']}")

                if meta_parts:
                    line = f"[{' '.join(meta_parts)}] {line}"

                streams[label_key]['values'].append([timestamp_ns, line])

            payload = {
                'streams': list(streams.values())
            }

            # Отправляем
            data = json.dumps(payload).encode('utf-8')
            req = Request(
                self.url,
                data=data,
                headers={'Content-Type': 'application/json'},
                method='POST'
            )

            with urlopen(req, timeout=self.timeout) as resp:
                if resp.status not in (200, 204):
                    logger.warning(f"Loki returned status {resp.status}")

        except (URLError, HTTPError) as e:
            logger.warning(f"Failed to send logs to Loki: {e}")
        except Exception as e:
            logger.warning(f"Loki send error: {e}")

    def close(self):
        """Закрыть handler и отправить оставшиеся логи"""
        self._shutdown.set()
        if self._sender_thread.is_alive():
            self._sender_thread.join(timeout=5.0)
        super().close()


def setup_loki_logging(
    loki_url: str = None,
    labels: Dict[str, str] = None,
    log_level: int = logging.INFO
) -> Optional[LokiHandler]:
    """
    Настроить отправку логов в Loki

    :param loki_url: URL Loki (если не указан, берётся из LOKI_URL)
    :param labels: Дополнительные labels
    :param log_level: Уровень логирования для Loki
    :return: LokiHandler или None если отключено
    """
    # Проверяем, включен ли Loki
    loki_enabled = os.getenv('LOKI_ENABLED', 'false').lower() == 'true'
    if not loki_enabled:
        logger.info("Loki logging is disabled")
        return None

    # Получаем URL
    url = loki_url or os.getenv('LOKI_URL')
    if not url:
        logger.warning("LOKI_URL not configured, Loki logging disabled")
        return None

    # Парсим дополнительные labels из env
    env_labels = {}
    labels_str = os.getenv('LOKI_LABELS', '')
    if labels_str:
        for pair in labels_str.split(','):
            if '=' in pair:
                key, value = pair.split('=', 1)
                env_labels[key.strip()] = value.strip()

    # Объединяем labels
    all_labels = {**env_labels, **(labels or {})}

    # Создаём handler
    handler = LokiHandler(url=url, labels=all_labels)
    handler.setLevel(log_level)
    handler.setFormatter(logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    ))

    # Добавляем к root logger
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)

    logger.info(f"Loki logging configured: {url}")
    return handler


class LogContext:
    """
    Контекстный менеджер для добавления метаданных к логам

    Пример использования:
        with LogContext(user_id=123, action='create_key'):
            logger.info("Creating key")  # будет отправлено в Loki с метаданными
    """

    _context = threading.local()

    def __init__(self, **kwargs):
        self.data = kwargs
        self._old_factory = None

    def __enter__(self):
        # Сохраняем текущий контекст
        if not hasattr(self._context, 'stack'):
            self._context.stack = []
        self._context.stack.append(self.data)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if hasattr(self._context, 'stack') and self._context.stack:
            self._context.stack.pop()

    @classmethod
    def get_current(cls) -> Dict[str, Any]:
        """Получить текущий контекст"""
        if not hasattr(cls._context, 'stack'):
            return {}

        result = {}
        for ctx in cls._context.stack:
            result.update(ctx)
        return result


class ContextFilter(logging.Filter):
    """Фильтр для добавления контекстных данных к записям логов"""

    def filter(self, record: logging.LogRecord) -> bool:
        context = LogContext.get_current()
        for key, value in context.items():
            setattr(record, key, value)
        return True


def add_context_filter():
    """Добавить контекстный фильтр к root logger"""
    root_logger = logging.getLogger()
    root_logger.addFilter(ContextFilter())


# Вспомогательные функции для логирования действий
def log_action(action: str, user_id: int = None, **kwargs):
    """
    Залогировать действие с метаданными

    :param action: Название действия
    :param user_id: ID пользователя
    :param kwargs: Дополнительные параметры
    """
    extra = {'action': action}
    if user_id:
        extra['user_id'] = user_id
    extra.update(kwargs)

    msg_parts = [f"Action: {action}"]
    if user_id:
        msg_parts.append(f"user={user_id}")
    for k, v in kwargs.items():
        msg_parts.append(f"{k}={v}")

    logger.info(' | '.join(msg_parts), extra=extra)


def log_key_created(user_id: int, email: str, period: str, price: int = 0, server: str = None):
    """Залогировать создание ключа"""
    log_action(
        action='key_created',
        user_id=user_id,
        email=email,
        period=period,
        price=price,
        server=server or 'local'
    )


def log_key_deleted(user_id: int, email: str, server: str = None):
    """Залогировать удаление ключа"""
    log_action(
        action='key_deleted',
        user_id=user_id,
        email=email,
        server=server or 'all'
    )


def log_xui_error(operation: str, error: str, server: str = None):
    """Залогировать ошибку X-UI"""
    extra = {
        'action': 'xui_error',
        'operation': operation,
        'server': server or 'local'
    }
    logger.error(f"X-UI Error: {operation} - {error}", extra=extra)


def log_api_request(method: str, url: str, status: int, duration_ms: float):
    """Залогировать API запрос"""
    extra = {
        'action': 'api_request',
        'method': method,
        'status': status,
        'duration_ms': duration_ms
    }
    logger.debug(f"API {method} {url} -> {status} ({duration_ms:.1f}ms)", extra=extra)
