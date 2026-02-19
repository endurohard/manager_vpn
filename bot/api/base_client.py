"""
Базовый API клиент для работы с X-UI панелями

Предоставляет:
- Единый интерфейс для всех API операций
- Автоматический retry с экспоненциальной задержкой
- Кэширование сессий и авторизации
- Обработка ошибок с типизированными исключениями
- Поддержка SSL с возможностью отключения верификации
"""
import ssl
import json
import asyncio
import aiohttp
import logging
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urljoin

from bot.errors import (
    APIError,
    AuthenticationError,
    ConnectionError,
    TimeoutError,
    PanelAPIError,
    track_error
)
from bot.utils.async_utils import (
    retry,
    RetryStrategy,
    AsyncCache,
    CircuitBreaker,
    RateLimiter
)

logger = logging.getLogger(__name__)


@dataclass
class ServerConfig:
    """Конфигурация сервера X-UI"""
    name: str
    ip: str
    port: int = 443
    domain: Optional[str] = None
    panel_url: Optional[str] = None
    panel_path: str = ""
    username: str = "admin"
    password: str = ""
    ssl_verify: bool = False
    timeout: float = 30.0
    enabled: bool = True
    active_for_new: bool = True
    inbounds: Dict[str, Any] = field(default_factory=dict)

    @property
    def base_url(self) -> str:
        """Получить базовый URL панели"""
        if self.panel_url:
            return self.panel_url.rstrip('/')
        return f"https://{self.ip}:{self.port}/{self.panel_path}".rstrip('/')

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'ServerConfig':
        """Создать конфигурацию из словаря"""
        panel = data.get('panel', {})
        return cls(
            name=data.get('name', 'Unknown'),
            ip=data.get('ip', ''),
            port=data.get('port', 443),
            domain=data.get('domain'),
            panel_url=panel.get('url'),
            panel_path=panel.get('path', ''),
            username=panel.get('username', 'admin'),
            password=panel.get('password', ''),
            ssl_verify=panel.get('ssl_verify', False),
            timeout=panel.get('timeout', 30.0),
            enabled=data.get('enabled', True),
            active_for_new=data.get('active_for_new', True),
            inbounds=data.get('inbounds', {})
        )


@dataclass
class ClientSettings:
    """Настройки клиента для создания"""
    uuid: str
    email: str
    enable: bool = True
    expire_time: int = 0  # timestamp в ms, 0 = без ограничения
    total_traffic: int = 0  # bytes, 0 = без ограничения
    limit_ip: int = 2
    flow: str = ""
    sub_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.uuid,
            "email": self.email,
            "enable": self.enable,
            "expiryTime": self.expire_time,
            "totalGB": self.total_traffic,
            "limitIp": self.limit_ip,
            "flow": self.flow,
            "subId": self.sub_id
        }


class SessionManager:
    """
    Менеджер сессий для авторизации в панелях

    Кэширует cookies авторизации для каждого сервера
    """

    def __init__(self, cache_ttl: float = 3600):
        self._sessions: Dict[str, aiohttp.ClientSession] = {}
        self._cookies: Dict[str, Dict] = {}
        self._auth_times: Dict[str, datetime] = {}
        self._cache_ttl = cache_ttl
        self._lock = asyncio.Lock()

    def _create_ssl_context(self, verify: bool = False) -> ssl.SSLContext:
        """Создать SSL контекст"""
        if verify:
            return ssl.create_default_context()

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx

    async def get_session(self, server: ServerConfig) -> aiohttp.ClientSession:
        """Получить или создать сессию для сервера"""
        async with self._lock:
            key = server.name

            if key not in self._sessions or self._sessions[key].closed:
                connector = aiohttp.TCPConnector(
                    ssl=self._create_ssl_context(server.ssl_verify),
                    limit=10,
                    limit_per_host=5
                )
                timeout = aiohttp.ClientTimeout(total=server.timeout)
                self._sessions[key] = aiohttp.ClientSession(
                    connector=connector,
                    timeout=timeout
                )

            return self._sessions[key]

    def is_auth_valid(self, server_name: str) -> bool:
        """Проверить актуальность авторизации"""
        if server_name not in self._auth_times:
            return False

        age = datetime.now() - self._auth_times[server_name]
        return age.total_seconds() < self._cache_ttl

    def set_auth(self, server_name: str, cookies: Dict):
        """Сохранить данные авторизации"""
        self._cookies[server_name] = cookies
        self._auth_times[server_name] = datetime.now()

    def get_cookies(self, server_name: str) -> Optional[Dict]:
        """Получить сохраненные cookies"""
        if self.is_auth_valid(server_name):
            return self._cookies.get(server_name)
        return None

    def invalidate(self, server_name: str):
        """Инвалидировать авторизацию"""
        self._cookies.pop(server_name, None)
        self._auth_times.pop(server_name, None)

    async def close_all(self):
        """Закрыть все сессии"""
        async with self._lock:
            for session in self._sessions.values():
                if not session.closed:
                    await session.close()
            self._sessions.clear()


class XUIClient:
    """
    Асинхронный клиент для работы с X-UI панелью

    Использование:
        config = ServerConfig.from_dict(server_data)
        client = XUIClient(config)

        # Авторизация
        await client.login()

        # Получение списка inbounds
        inbounds = await client.get_inbounds()

        # Создание клиента
        await client.add_client(inbound_id=1, settings=ClientSettings(...))

        # Закрытие
        await client.close()
    """

    def __init__(
        self,
        config: ServerConfig,
        session_manager: SessionManager = None,
        rate_limiter: RateLimiter = None
    ):
        self.config = config
        self._session_manager = session_manager or SessionManager()
        self._rate_limiter = rate_limiter or RateLimiter(rate=10, per=1.0)
        self._circuit_breaker = CircuitBreaker(
            failure_threshold=5,
            recovery_timeout=30.0
        )
        self._cache = AsyncCache(max_size=100, default_ttl=60)

    @property
    def base_url(self) -> str:
        return self.config.base_url

    async def _get_session(self) -> aiohttp.ClientSession:
        return await self._session_manager.get_session(self.config)

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Dict = None,
        json_data: Dict = None,
        headers: Dict = None,
        require_auth: bool = True
    ) -> Dict[str, Any]:
        """Выполнить HTTP запрос к панели"""
        url = urljoin(self.base_url + '/', endpoint.lstrip('/'))

        async with self._rate_limiter:
            session = await self._get_session()

            # Получаем cookies если нужна авторизация
            cookies = None
            if require_auth:
                cookies = self._session_manager.get_cookies(self.config.name)
                if not cookies:
                    await self.login()
                    cookies = self._session_manager.get_cookies(self.config.name)

            try:
                async with session.request(
                    method,
                    url,
                    data=data,
                    json=json_data,
                    headers=headers,
                    cookies=cookies
                ) as response:
                    text = await response.text()

                    if response.status == 401:
                        self._session_manager.invalidate(self.config.name)
                        raise AuthenticationError(
                            "Ошибка авторизации",
                            server=self.config.name
                        )

                    if response.status >= 400:
                        raise PanelAPIError(
                            f"HTTP {response.status}: {text[:200]}",
                            server=self.config.name,
                            endpoint=endpoint,
                            status_code=response.status,
                            response=text
                        )

                    try:
                        return json.loads(text)
                    except json.JSONDecodeError:
                        return {'raw': text, 'success': response.status < 400}

            except aiohttp.ClientConnectorError as e:
                raise ConnectionError(
                    f"Не удалось подключиться к {self.config.name}",
                    server=self.config.name,
                    original=e
                )
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"Таймаут при запросе к {self.config.name}",
                    operation=endpoint,
                    timeout=self.config.timeout
                )

    @retry(max_attempts=3, base_delay=1.0, strategy=RetryStrategy.EXPONENTIAL)
    async def login(self) -> bool:
        """Авторизоваться в панели"""
        session = await self._get_session()

        try:
            async with session.post(
                f"{self.base_url}/login",
                data={
                    'username': self.config.username,
                    'password': self.config.password
                }
            ) as response:
                if response.status != 200:
                    raise AuthenticationError(
                        f"Ошибка авторизации: HTTP {response.status}",
                        server=self.config.name
                    )

                data = await response.json()
                if not data.get('success'):
                    raise AuthenticationError(
                        data.get('msg', 'Неверные учетные данные'),
                        server=self.config.name
                    )

                # Сохраняем cookies
                cookies = {
                    cookie.key: cookie.value
                    for cookie in session.cookie_jar
                }
                self._session_manager.set_auth(self.config.name, cookies)

                logger.info(f"Successfully logged in to {self.config.name}")
                return True

        except aiohttp.ClientError as e:
            raise ConnectionError(
                f"Ошибка подключения к {self.config.name}",
                server=self.config.name,
                original=e
            )

    async def get_inbounds(self, use_cache: bool = True) -> List[Dict]:
        """Получить список inbounds"""
        cache_key = f"inbounds:{self.config.name}"

        if use_cache:
            cached = await self._cache.get(cache_key)
            if cached:
                return cached

        result = await self._request('GET', '/panel/api/inbounds/list')

        if result.get('success'):
            inbounds = result.get('obj', [])
            await self._cache.set(cache_key, inbounds, ttl=60)
            return inbounds

        raise PanelAPIError(
            result.get('msg', 'Не удалось получить inbounds'),
            server=self.config.name,
            endpoint='/panel/api/inbounds/list'
        )

    async def get_inbound(self, inbound_id: int) -> Optional[Dict]:
        """Получить конкретный inbound"""
        result = await self._request('GET', f'/panel/api/inbounds/get/{inbound_id}')

        if result.get('success'):
            return result.get('obj')

        return None

    async def add_client(
        self,
        inbound_id: int,
        settings: ClientSettings
    ) -> bool:
        """Добавить клиента в inbound"""
        client_data = settings.to_dict()

        result = await self._request(
            'POST',
            f'/panel/api/inbounds/addClient',
            json_data={
                'id': inbound_id,
                'settings': json.dumps({"clients": [client_data]})
            }
        )

        if result.get('success'):
            # Инвалидируем кэш inbounds
            await self._cache.delete(f"inbounds:{self.config.name}")
            logger.info(f"Client {settings.email} added to {self.config.name}")
            return True

        raise PanelAPIError(
            result.get('msg', 'Не удалось добавить клиента'),
            server=self.config.name,
            endpoint='/panel/api/inbounds/addClient'
        )

    async def update_client(
        self,
        inbound_id: int,
        client_uuid: str,
        settings: ClientSettings
    ) -> bool:
        """Обновить клиента"""
        result = await self._request(
            'POST',
            f'/panel/api/inbounds/updateClient/{client_uuid}',
            json_data={
                'id': inbound_id,
                'settings': json.dumps({"clients": [settings.to_dict()]})
            }
        )

        if result.get('success'):
            await self._cache.delete(f"inbounds:{self.config.name}")
            return True

        raise PanelAPIError(
            result.get('msg', 'Не удалось обновить клиента'),
            server=self.config.name,
            endpoint=f'/panel/api/inbounds/updateClient/{client_uuid}'
        )

    async def delete_client(self, inbound_id: int, client_uuid: str) -> bool:
        """Удалить клиента"""
        result = await self._request(
            'POST',
            f'/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}'
        )

        if result.get('success'):
            await self._cache.delete(f"inbounds:{self.config.name}")
            logger.info(f"Client {client_uuid} deleted from {self.config.name}")
            return True

        raise PanelAPIError(
            result.get('msg', 'Не удалось удалить клиента'),
            server=self.config.name,
            endpoint=f'/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}'
        )

    async def get_client_traffic(self, email: str) -> Optional[Dict]:
        """Получить статистику трафика клиента"""
        result = await self._request('GET', f'/panel/api/inbounds/getClientTraffics/{email}')

        if result.get('success'):
            return result.get('obj')

        return None

    async def get_client_ips(self, email: str) -> Optional[str]:
        """Получить IP адреса клиента"""
        result = await self._request('GET', f'/panel/api/inbounds/clientIps/{email}')

        if result.get('success'):
            return result.get('obj')

        return None

    async def reset_client_traffic(self, inbound_id: int, email: str) -> bool:
        """Сбросить трафик клиента"""
        result = await self._request(
            'POST',
            f'/panel/api/inbounds/{inbound_id}/resetClientTraffic/{email}'
        )

        return result.get('success', False)

    async def find_client_by_uuid(self, client_uuid: str) -> Optional[Dict]:
        """Найти клиента по UUID на сервере"""
        inbounds = await self.get_inbounds()

        for inbound in inbounds:
            try:
                settings = json.loads(inbound.get('settings', '{}'))
                clients = settings.get('clients', [])

                for client in clients:
                    if client.get('id') == client_uuid:
                        return {
                            'client': client,
                            'inbound_id': inbound.get('id'),
                            'inbound_remark': inbound.get('remark')
                        }
            except json.JSONDecodeError:
                continue

        return None

    async def find_client_by_email(self, email: str) -> Optional[Dict]:
        """Найти клиента по email на сервере"""
        inbounds = await self.get_inbounds()

        for inbound in inbounds:
            try:
                settings = json.loads(inbound.get('settings', '{}'))
                clients = settings.get('clients', [])

                for client in clients:
                    if client.get('email') == email:
                        return {
                            'client': client,
                            'inbound_id': inbound.get('id'),
                            'inbound_remark': inbound.get('remark')
                        }
            except json.JSONDecodeError:
                continue

        return None

    async def server_status(self) -> Dict[str, Any]:
        """Получить статус сервера"""
        result = await self._request('POST', '/server/status', require_auth=True)
        return result.get('obj', {})

    async def close(self):
        """Закрыть клиент"""
        self._session_manager.invalidate(self.config.name)


class XUIClientFactory:
    """
    Фабрика для создания клиентов X-UI

    Использование:
        factory = XUIClientFactory()

        # Загрузить конфигурацию
        factory.load_config('/path/to/servers_config.json')

        # Получить клиент для сервера
        client = await factory.get_client('server_name')

        # Получить клиенты для всех активных серверов
        clients = await factory.get_active_clients()
    """

    def __init__(self):
        self._session_manager = SessionManager()
        self._configs: Dict[str, ServerConfig] = {}
        self._clients: Dict[str, XUIClient] = {}

    def load_config(self, config_path: str):
        """Загрузить конфигурацию из файла"""
        try:
            with open(config_path, 'r') as f:
                data = json.load(f)

            for server_data in data.get('servers', []):
                config = ServerConfig.from_dict(server_data)
                self._configs[config.name] = config

            logger.info(f"Loaded {len(self._configs)} server configs")

        except Exception as e:
            logger.error(f"Error loading config: {e}")
            raise

    def add_server(self, config: ServerConfig):
        """Добавить сервер вручную"""
        self._configs[config.name] = config

    async def get_client(self, server_name: str) -> XUIClient:
        """Получить клиент для сервера"""
        if server_name not in self._configs:
            raise ValueError(f"Unknown server: {server_name}")

        if server_name not in self._clients:
            self._clients[server_name] = XUIClient(
                self._configs[server_name],
                session_manager=self._session_manager
            )

        return self._clients[server_name]

    async def get_active_clients(self) -> List[XUIClient]:
        """Получить клиенты для всех активных серверов"""
        clients = []
        for name, config in self._configs.items():
            if config.enabled:
                clients.append(await self.get_client(name))
        return clients

    async def get_new_key_clients(self) -> List[XUIClient]:
        """Получить клиенты для серверов, активных для новых ключей"""
        clients = []
        for name, config in self._configs.items():
            if config.enabled and config.active_for_new:
                clients.append(await self.get_client(name))
        return clients

    def get_server_config(self, server_name: str) -> Optional[ServerConfig]:
        """Получить конфигурацию сервера"""
        return self._configs.get(server_name)

    def list_servers(self) -> List[str]:
        """Получить список имен серверов"""
        return list(self._configs.keys())

    async def close_all(self):
        """Закрыть все клиенты"""
        for client in self._clients.values():
            await client.close()
        await self._session_manager.close_all()
        self._clients.clear()


# Глобальная фабрика
_factory: Optional[XUIClientFactory] = None


def get_client_factory() -> XUIClientFactory:
    """Получить глобальную фабрику клиентов"""
    global _factory
    if _factory is None:
        _factory = XUIClientFactory()
    return _factory


__all__ = [
    'ServerConfig',
    'ClientSettings',
    'SessionManager',
    'XUIClient',
    'XUIClientFactory',
    'get_client_factory',
]
