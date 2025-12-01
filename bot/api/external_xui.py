"""
Клиент для работы с внешним X-UI Panel API
Для создания ключей на удалённом сервере через API (не SSH)
"""
import aiohttp
import json
import uuid
import ssl
import asyncio
import logging
from typing import Optional, Dict, List
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class ExternalXUIClient:
    """Клиент для подключения к внешней X-UI панели через API"""

    def __init__(self, host: str, username: str, password: str, base_path: str = ""):
        """
        Инициализация клиента внешнего X-UI сервера

        :param host: URL хоста X-UI (например, https://38.180.205.196:27450)
        :param username: Имя пользователя
        :param password: Пароль
        :param base_path: Базовый путь (например, /J6CkyRIalbUZdPd)
        """
        self.host = host.rstrip('/')
        self.username = username
        self.password = password
        self.base_path = base_path.rstrip('/') if base_path else ""
        self.session_cookie = None
        self.session = None

    @property
    def api_base(self) -> str:
        """Базовый URL для API запросов"""
        return f"{self.host}{self.base_path}"

    async def __aenter__(self):
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        # unsafe=True нужен для работы с IP адресами
        jar = aiohttp.CookieJar(unsafe=True)
        self.session = aiohttp.ClientSession(connector=connector, cookie_jar=jar)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def _ensure_session(self):
        """Гарантирует наличие сессии"""
        if not self.session:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE

            connector = aiohttp.TCPConnector(ssl=ssl_context)
            # unsafe=True нужен для работы с IP адресами
            jar = aiohttp.CookieJar(unsafe=True)
            self.session = aiohttp.ClientSession(connector=connector, cookie_jar=jar)

    async def login(self) -> bool:
        """Авторизация в X-UI Panel"""
        try:
            await self._ensure_session()

            url = f"{self.api_base}/login"
            payload = {
                "username": self.username,
                "password": self.password
            }
            # Важно: 3x-ui использует form-data, а не JSON
            headers = {
                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                'X-Requested-With': 'XMLHttpRequest'
            }

            logger.info(f"Попытка авторизации на внешнем сервере: {url}")

            async with self.session.post(url, data=payload, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('success'):
                        self.session_cookie = True  # Cookie сохраняется автоматически в CookieJar
                        logger.info(f"Успешная авторизация на внешнем X-UI сервере: {self.host}")
                        return True
                logger.warning(f"Ошибка авторизации на внешнем сервере: статус {response.status}")
                return False
        except Exception as e:
            logger.error(f"Ошибка подключения к внешнему X-UI при авторизации: {e}")
            return False

    async def _ensure_logged_in(self, max_retries: int = 3) -> bool:
        """Гарантирует авторизацию с повторными попытками"""
        for attempt in range(1, max_retries + 1):
            try:
                if self.session_cookie:
                    return True

                logger.info(f"Попытка авторизации на внешнем сервере {attempt}/{max_retries}")
                success = await self.login()

                if success:
                    return True

                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

            except Exception as e:
                logger.error(f"Ошибка при попытке авторизации {attempt}/{max_retries}: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        logger.error("Не удалось авторизоваться на внешнем сервере после всех попыток")
        return False

    async def list_inbounds(self, max_retries: int = 3) -> List[Dict]:
        """
        Получить список всех inbound'ов

        :param max_retries: Максимальное количество попыток
        :return: Список inbound'ов или []
        """
        for attempt in range(1, max_retries + 1):
            try:
                if not await self._ensure_logged_in():
                    logger.error("Не удалось авторизоваться для получения списка inbound'ов")
                    return []

                url = f"{self.api_base}/panel/api/inbounds/list"
                headers = {'X-Requested-With': 'XMLHttpRequest'}

                async with self.session.get(url, headers=headers) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '')

                        if 'application/json' not in content_type:
                            logger.warning(f"Получен ответ с Content-Type: {content_type} вместо JSON")
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return []

                        try:
                            data = await response.json()
                            if data.get('success'):
                                inbounds = data.get('obj', [])
                                logger.info(f"Получен список из {len(inbounds)} inbound'ов с внешнего сервера")
                                return inbounds
                        except aiohttp.ContentTypeError as e:
                            logger.warning(f"Не удалось распарсить JSON: {e}")
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return []

                    if response.status in [401, 403]:
                        logger.warning(f"Сессия истекла. Попытка {attempt}/{max_retries}")
                        self.session_cookie = None
                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue

                    logger.warning(f"Не удалось получить список inbound'ов, статус: {response.status}")
                    return []

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Сетевая ошибка (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Ошибка (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        return []

    async def get_inbound(self, inbound_id: int, max_retries: int = 3) -> Optional[Dict]:
        """Получить информацию об inbound"""
        for attempt in range(1, max_retries + 1):
            try:
                if not await self._ensure_logged_in():
                    return None

                url = f"{self.api_base}/panel/api/inbounds/get/{inbound_id}"
                headers = {'X-Requested-With': 'XMLHttpRequest'}

                async with self.session.get(url, headers=headers) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '')

                        if 'application/json' not in content_type:
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return None

                        try:
                            data = await response.json()
                            if data.get('success'):
                                return data.get('obj')
                        except aiohttp.ContentTypeError:
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return None

                    if response.status in [401, 403]:
                        self.session_cookie = None
                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue

                    return None

            except Exception as e:
                logger.error(f"Ошибка получения inbound {inbound_id}: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        return None

    async def add_client(self, inbound_id: int, email: str, phone: str,
                        expire_days: int, ip_limit: int = 2, max_retries: int = 3) -> Optional[Dict]:
        """
        Добавить клиента на внешний сервер

        :param inbound_id: ID inbound
        :param email: Email клиента
        :param phone: Номер телефона/ID
        :param expire_days: Количество дней до истечения
        :param ip_limit: Лимит IP
        :param max_retries: Максимальное количество попыток
        :return: Данные клиента или None
        """
        client_id = str(uuid.uuid4())
        expire_time = int((datetime.now() + timedelta(days=expire_days)).timestamp() * 1000)

        client_settings = {
            "clients": [{
                "id": client_id,
                "alterId": 0,
                "email": email,
                "limitIp": ip_limit,
                "totalGB": 0,
                "expiryTime": expire_time,
                "enable": True,
                "tgId": phone,
                "subId": "",
                "flow": "xtls-rprx-vision"
            }]
        }

        for attempt in range(1, max_retries + 1):
            try:
                if not await self._ensure_logged_in():
                    return None

                url = f"{self.api_base}/panel/api/inbounds/addClient"
                payload = {
                    "id": inbound_id,
                    "settings": json.dumps(client_settings)
                }
                headers = {
                    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                    'X-Requested-With': 'XMLHttpRequest'
                }

                async with self.session.post(url, data=payload, headers=headers) as response:
                    logger.info(f"Создание клиента на внешнем сервере, статус: {response.status}")

                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '')

                        if 'application/json' not in content_type:
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return None

                        try:
                            data = await response.json()
                            logger.info(f"Ответ при создании клиента: {data}")

                            if data.get('success'):
                                logger.info(f"Клиент {email} создан на внешнем сервере")
                                await self.restart_xray()

                                return {
                                    "client_id": client_id,
                                    "email": email,
                                    "phone": phone,
                                    "expire_time": expire_time,
                                    "ip_limit": ip_limit
                                }
                            else:
                                error_msg = data.get('msg', 'Unknown error')
                                logger.warning(f"Не удалось создать клиента: {error_msg}")
                                return {
                                    "error": True,
                                    "message": error_msg,
                                    "is_duplicate": "Duplicate email" in error_msg
                                }
                        except aiohttp.ContentTypeError:
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return None

                    elif response.status in [401, 403]:
                        self.session_cookie = None
                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue
                    else:
                        response_text = await response.text()
                        logger.error(f"HTTP ошибка: {response.status}, body: {response_text}")
                        if attempt < max_retries:
                            await asyncio.sleep(2)
                            continue

                    return None

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Сетевая ошибка (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Ошибка (попытка {attempt}/{max_retries}): {e}")
                import traceback
                traceback.print_exc()
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        return None

    async def get_client_link(self, inbound_id: int, client_email: str,
                              server_address: str = None, server_port: int = 443) -> Optional[str]:
        """
        Получить VLESS ссылку для клиента

        :param inbound_id: ID inbound
        :param client_email: Email клиента
        :param server_address: Адрес сервера для ссылки (IP или домен)
        :param server_port: Порт для ссылки
        :return: VLESS ссылка или None
        """
        try:
            inbound = await self.get_inbound(inbound_id)
            if not inbound:
                return None

            settings = json.loads(inbound.get('settings', '{}'))
            stream_settings = json.loads(inbound.get('streamSettings', '{}'))

            # Ищем клиента
            clients = settings.get('clients', [])
            client = None
            for c in clients:
                if c.get('email') == client_email:
                    client = c
                    break

            if not client:
                logger.warning(f"Клиент {client_email} не найден в inbound {inbound_id}")
                return None

            client_id = client.get('id')
            port = inbound.get('port')

            # Используем переданный адрес или IP из хоста
            if server_address:
                host = server_address
            else:
                # Извлекаем IP из self.host
                import re
                match = re.search(r'https?://([^:/]+)', self.host)
                host = match.group(1) if match else inbound.get('listen', '')

            network = stream_settings.get('network', 'tcp')
            security = stream_settings.get('security', 'none')

            vless_link = f"vless://{client_id}@{host}:{server_port}"

            params = [
                f"type={network}",
                f"security={security}",
                "encryption=none"
            ]

            # Параметры REALITY
            if security == 'reality':
                reality_settings = stream_settings.get('realitySettings', {})

                client_flow = client.get('flow', '')
                if client_flow:
                    params.append(f"flow={client_flow}")

                public_key = reality_settings.get('settings', {}).get('publicKey', '')
                if public_key:
                    params.append(f"pbk={public_key}")

                fingerprint = reality_settings.get('settings', {}).get('fingerprint', 'chrome')
                params.append(f"fp={fingerprint}")

                server_names = reality_settings.get('serverNames', [])
                if server_names:
                    sni = server_names[0]
                    params.append(f"sni={sni}")

                short_ids = reality_settings.get('shortIds', [])
                if short_ids:
                    sid = short_ids[0]
                    params.append(f"sid={sid}")

                spider_x = reality_settings.get('settings', {}).get('spiderX', '/')
                if spider_x:
                    import urllib.parse
                    params.append(f"spx={urllib.parse.quote(spider_x)}")

            # Параметры TLS
            elif security == 'tls':
                tls_settings = stream_settings.get('tlsSettings', {})
                sni = tls_settings.get('serverName', host)
                params.append(f"sni={sni}")

            vless_link += "?" + "&".join(params) + f"#{client_email}"

            return vless_link

        except Exception as e:
            logger.error(f"Ошибка при формировании VLESS ссылки: {e}")
            import traceback
            traceback.print_exc()
            return None

    async def restart_xray(self) -> bool:
        """Перезапустить xray для применения изменений"""
        try:
            if not await self._ensure_logged_in():
                return False

            url = f"{self.api_base}/panel/api/inbounds/restart"
            headers = {'X-Requested-With': 'XMLHttpRequest'}
            async with self.session.post(url, headers=headers) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('success'):
                        logger.info("Xray перезапущен на внешнем сервере")
                        return True
            return False
        except Exception as e:
            logger.error(f"Ошибка при рестарте xray на внешнем сервере: {e}")
            return False

    async def close(self):
        """Закрыть сессию"""
        if self.session:
            await self.session.close()
            self.session = None


# Конфигурация внешнего сервера
EXTERNAL_SERVER_CONFIG = {
    "host": "https://38.180.205.196:27450",
    "base_path": "/J6CkyRIalbUZdPd",
    "username": "itadmin",
    "password": "20TQNF_Srld",
    "server_name": "External Server",
    "server_address": "lte.vpnpulse.ru",  # Домен вместо IP для VLESS ссылок
    "server_port": 443
}


def get_external_xui_client() -> ExternalXUIClient:
    """Создать клиент для внешнего X-UI сервера"""
    config = EXTERNAL_SERVER_CONFIG
    return ExternalXUIClient(
        host=config["host"],
        username=config["username"],
        password=config["password"],
        base_path=config["base_path"]
    )
