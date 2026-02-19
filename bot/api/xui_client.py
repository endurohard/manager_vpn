"""
Клиент для работы с X-UI Panel API (локальный)
"""
import aiohttp
import json
import uuid
import ssl
import asyncio
import logging
import subprocess
from typing import Optional, Dict
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class XUIClient:
    def __init__(self, host: str, username: str, password: str):
        """
        Инициализация клиента X-UI

        :param host: URL хоста X-UI (например, http://localhost:54321)
        :param username: Имя пользователя
        :param password: Пароль
        """
        self.host = host.rstrip('/')
        self.username = username
        self.password = password
        self.session_cookie = None
        self.session = None

    async def __aenter__(self):
        # Создаем SSL context для работы с самоподписанными сертификатами
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        self.session = aiohttp.ClientSession(connector=connector)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()

    async def login(self) -> bool:
        """Авторизация в X-UI Panel"""
        try:
            if not self.session:
                # Создаем SSL context для работы с самоподписанными сертификатами
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                connector = aiohttp.TCPConnector(ssl=ssl_context)
                self.session = aiohttp.ClientSession(connector=connector)

            url = f"{self.host}/login"
            payload = {
                "username": self.username,
                "password": self.password
            }

            async with self.session.post(url, json=payload) as response:
                if response.status == 200:
                    data = await response.json()
                    if data.get('success'):
                        # Сохраняем cookie сессии
                        cookies = response.cookies
                        self.session_cookie = cookies
                        logger.info("Успешная авторизация в X-UI панели")
                        return True
                logger.warning(f"Ошибка авторизации в X-UI: статус {response.status}")
                return False
        except Exception as e:
            logger.error(f"Ошибка подключения к X-UI при авторизации: {e}")
            return False

    async def _ensure_logged_in(self, max_retries: int = 3) -> bool:
        """
        Гарантирует авторизацию с повторными попытками

        :param max_retries: Максимальное количество попыток
        :return: True если авторизован, False если не удалось
        """
        for attempt in range(1, max_retries + 1):
            try:
                if self.session_cookie:
                    return True

                logger.info(f"Попытка авторизации в X-UI {attempt}/{max_retries}")
                success = await self.login()

                if success:
                    return True

                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)  # Экспоненциальная задержка

            except Exception as e:
                logger.error(f"Ошибка при попытке авторизации {attempt}/{max_retries}: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        logger.error("Не удалось авторизоваться в X-UI после всех попыток")
        return False

    async def _make_request_with_retry(self, method: str, url: str, max_retries: int = 3, **kwargs) -> Optional[aiohttp.ClientResponse]:
        """
        Выполняет HTTP запрос с автоматическим переподключением при ошибках авторизации

        :param method: HTTP метод (GET, POST, etc.)
        :param url: URL для запроса
        :param max_retries: Максимальное количество попыток
        :return: Response или None
        """
        for attempt in range(1, max_retries + 1):
            try:
                # Проверяем авторизацию
                if not await self._ensure_logged_in():
                    logger.error("Не удалось авторизоваться для выполнения запроса")
                    return None

                # Выполняем запрос
                async with getattr(self.session, method.lower())(url, **kwargs) as response:
                    # Если получили 401/403, сбрасываем сессию и пробуем переподключиться
                    if response.status in [401, 403]:
                        logger.warning(f"Получен статус {response.status}, сессия истекла. Переподключаемся...")
                        self.session_cookie = None

                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue

                    return response

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Сетевая ошибка при попытке {attempt}/{max_retries}: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return None
            except Exception as e:
                logger.error(f"Неожиданная ошибка при запросе {attempt}/{max_retries}: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return None

        return None

    async def get_inbound(self, inbound_id: int, max_retries: int = 3) -> Optional[Dict]:
        """
        Получить информацию об inbound с автоматическим переподключением

        :param inbound_id: ID inbound
        :param max_retries: Максимальное количество попыток
        :return: Информация об inbound или None
        """
        for attempt in range(1, max_retries + 1):
            try:
                # Проверяем авторизацию
                if not await self._ensure_logged_in():
                    logger.error("Не удалось авторизоваться для получения inbound")
                    return None

                url = f"{self.host}/panel/api/inbounds/get/{inbound_id}"

                async with self.session.get(url) as response:
                    if response.status == 200:
                        # Проверяем Content-Type перед парсингом JSON
                        content_type = response.headers.get('Content-Type', '')

                        # Если получили HTML вместо JSON - сессия истекла
                        if 'application/json' not in content_type:
                            logger.warning(f"Получен ответ с Content-Type: {content_type} вместо JSON. Сессия истекла.")
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return None

                        try:
                            data = await response.json()
                            if data.get('success'):
                                logger.info(f"Успешно получены данные inbound {inbound_id}")
                                return data.get('obj')
                        except aiohttp.ContentTypeError as e:
                            # Если не смогли распарсить JSON - сессия истекла
                            logger.warning(f"Не удалось распарсить JSON ответ: {e}. Сессия истекла.")
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return None

                    # Если сессия истекла, пробуем переподключиться
                    if response.status in [401, 403]:
                        logger.warning(f"Сессия истекла при получении inbound. Попытка {attempt}/{max_retries}")
                        self.session_cookie = None
                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue

                    logger.warning(f"Не удалось получить inbound, статус: {response.status}")
                    return None

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Сетевая ошибка при получении inbound (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Ошибка при получении inbound (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        logger.error(f"Не удалось получить inbound {inbound_id} после {max_retries} попыток")
        return None

    async def add_client(self, inbound_id: int, email: str, phone: str,
                        expire_days: int, ip_limit: int = 2, max_retries: int = 3) -> Optional[Dict]:
        """
        Добавить клиента (создать ключ VLESS) с автоматическим переподключением
        Учитывает настройку active_for_new для серверов

        :param inbound_id: ID inbound
        :param email: Email клиента (будет использоваться номер телефона)
        :param phone: Номер телефона
        :param expire_days: Количество дней до истечения
        :param ip_limit: Лимит IP (по умолчанию 2)
        :param max_retries: Максимальное количество попыток
        :return: Данные клиента или None
        """
        # Генерируем UUID для клиента
        client_id = str(uuid.uuid4())

        # Вычисляем время истечения в миллисекундах
        expire_time = int((datetime.now() + timedelta(days=expire_days)).timestamp() * 1000)

        # Проверяем, активен ли локальный сервер для новых подписок
        from bot.api.remote_xui import load_servers_config
        servers_config = load_servers_config()
        local_server = next((s for s in servers_config.get('servers', []) if s.get('local')), None)
        local_active = local_server.get('active_for_new', True) if local_server else True

        # Получаем flow из существующих клиентов в локальной базе
        flow = ''
        try:
            result = subprocess.run([
                'sqlite3', '/etc/x-ui/x-ui.db',
                f'SELECT settings FROM inbounds WHERE id={inbound_id}'
            ], capture_output=True, text=True, timeout=10)

            if result.returncode == 0 and result.stdout.strip():
                settings = json.loads(result.stdout.strip())
                for client in settings.get('clients', []):
                    if client.get('flow'):
                        flow = client.get('flow')
                        logger.debug(f"Flow '{flow}' получен из существующих клиентов")
                        break
        except Exception as e:
            logger.warning(f"Не удалось получить flow из базы: {e}")
            # Fallback на статический конфиг
            if local_server:
                inbounds = local_server.get('inbounds', {})
                main_inbound = inbounds.get('main', {})
                flow = main_inbound.get('flow', '')

        # Настройки клиента
        client_settings = {
            "clients": [{
                "id": client_id,
                "alterId": 0,
                "email": email,
                "limitIp": ip_limit,
                "totalGB": 0,  # 0 = безлимит
                "expiryTime": expire_time,
                "enable": True,
                "tgId": phone,
                "subId": "",
                "flow": flow
            }]
        }

        # Создаём клиента на локальном сервере только если он активен
        local_created = False
        if local_active:
            for attempt in range(1, max_retries + 1):
                try:
                    # Проверяем авторизацию
                    if not await self._ensure_logged_in():
                        logger.error("Не удалось авторизоваться для создания клиента")
                        break

                    url = f"{self.host}/panel/api/inbounds/addClient"
                    payload = {
                        "id": inbound_id,
                        "settings": json.dumps(client_settings)
                    }

                    async with self.session.post(url, json=payload) as response:
                        logger.info(f"Создание клиента на локальном сервере, статус: {response.status}")

                        if response.status == 200:
                            content_type = response.headers.get('Content-Type', '')
                            if 'application/json' not in content_type:
                                logger.warning(f"Получен ответ с Content-Type: {content_type}. Сессия истекла.")
                                self.session_cookie = None
                                if attempt < max_retries:
                                    await asyncio.sleep(1)
                                    continue
                                break

                            try:
                                data = await response.json()
                                if data.get('success'):
                                    logger.info(f"Клиент {email} создан на локальном сервере")
                                    await self.restart_xray()
                                    local_created = True
                                    break
                                else:
                                    error_msg = data.get('msg', 'Unknown error')
                                    if "Duplicate email" in error_msg:
                                        return {"error": True, "message": error_msg, "is_duplicate": True}
                                    logger.warning(f"Ошибка создания на локальном: {error_msg}")
                                    break
                            except:
                                self.session_cookie = None
                                if attempt < max_retries:
                                    await asyncio.sleep(1)
                                    continue
                                break
                        elif response.status in [401, 403]:
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                        break
                except Exception as e:
                    logger.error(f"Ошибка создания на локальном (попытка {attempt}): {e}")
                    if attempt < max_retries:
                        await asyncio.sleep(2 ** attempt)
        else:
            logger.info(f"Локальный сервер отключен для новых подписок, пропускаем")

        # Создаём клиента на активных удалённых серверах
        try:
            from bot.api.remote_xui import create_client_on_active_servers
            remote_results = await create_client_on_active_servers(
                client_uuid=client_id,
                email=email,
                expire_days=expire_days,
                ip_limit=ip_limit
            )
            logger.info(f"Результаты создания на удалённых серверах: {remote_results}")

            # Получаем результаты по серверам
            server_results = remote_results.get('results', {})

            # Проверяем, создан ли клиент хотя бы на одном сервере
            any_created = local_created or any(server_results.values())
            if not any_created:
                logger.error("Клиент не создан ни на одном сервере!")
                return None

            # Если клиент уже существовал на сервере, используем его реальный UUID
            if remote_results.get('any_existing', False):
                real_uuid = remote_results.get('uuid', client_id)
                if real_uuid != client_id:
                    logger.info(f"Клиент {email} уже существовал на сервере с UUID {real_uuid}, используем его")
                    client_id = real_uuid

        except Exception as e:
            logger.error(f"Ошибка создания на удалённых серверах: {e}")
            if not local_created:
                return None

        return {
            "client_id": client_id,
            "email": email,
            "phone": phone,
            "expire_time": expire_time,
            "ip_limit": ip_limit,
            "local_created": local_created
        }

    async def _add_client_old_logic(self, inbound_id: int, email: str, phone: str,
                        expire_days: int, ip_limit: int = 2, max_retries: int = 3) -> Optional[Dict]:
        """Старая логика создания клиента (не используется)"""
        client_id = str(uuid.uuid4())
        expire_time = int((datetime.now() + timedelta(days=expire_days)).timestamp() * 1000)

        # Получаем flow из существующих клиентов в базе
        flow = ''
        try:
            result = subprocess.run([
                'sqlite3', '/etc/x-ui/x-ui.db',
                f'SELECT settings FROM inbounds WHERE id={inbound_id}'
            ], capture_output=True, text=True, timeout=10)

            if result.returncode == 0 and result.stdout.strip():
                settings = json.loads(result.stdout.strip())
                for client in settings.get('clients', []):
                    if client.get('flow'):
                        flow = client.get('flow')
                        break
        except Exception:
            pass

        client_settings = {
            "clients": [{
                "id": client_id, "alterId": 0, "email": email, "limitIp": ip_limit,
                "totalGB": 0, "expiryTime": expire_time, "enable": True,
                "tgId": phone, "subId": "", "flow": flow
            }]
        }

        for attempt in range(1, max_retries + 1):
            try:
                if not await self._ensure_logged_in():
                    return None
                url = f"{self.host}/panel/api/inbounds/addClient"
                payload = {"id": inbound_id, "settings": json.dumps(client_settings)}

                async with self.session.post(url, json=payload) as response:
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
                                await self.restart_xray()
                                try:
                                    from bot.api.remote_xui import create_client_on_all_remote_servers
                                    await create_client_on_all_remote_servers(
                                        client_uuid=client_id, email=email,
                                        expire_days=expire_days, ip_limit=ip_limit
                                    )
                                except Exception as e:
                                    logger.error(f"Ошибка на удалённых: {e}")
                                return {
                                    "client_id": client_id, "email": email, "phone": phone,
                                    "expire_time": expire_time, "ip_limit": ip_limit
                                }
                            else:
                                error_msg = data.get('msg', 'Unknown error')
                                return {
                                    "error": True,
                                    "message": error_msg,
                                    "is_duplicate": "Duplicate email" in error_msg
                                }
                        except aiohttp.ContentTypeError as e:
                            # Если не смогли распарсить JSON - сессия истекла
                            logger.warning(f"Не удалось распарсить JSON ответ: {e}. Сессия истекла.")
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return None

                    # Если сессия истекла, пробуем переподключиться
                    elif response.status in [401, 403]:
                        logger.warning(f"Сессия истекла при создании клиента. Попытка {attempt}/{max_retries}")
                        self.session_cookie = None
                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue
                    else:
                        response_text = await response.text()
                        logger.error(f"HTTP ошибка при создании клиента: {response.status}, body: {response_text}")
                        if attempt < max_retries:
                            await asyncio.sleep(2)
                            continue

                    return None

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Сетевая ошибка при создании клиента (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Ошибка при создании клиента (попытка {attempt}/{max_retries}): {e}")
                import traceback
                traceback.print_exc()
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        logger.error(f"Не удалось создать клиента {email} после {max_retries} попыток")
        return None

    async def find_client_by_uuid(self, client_uuid: str) -> Optional[dict]:
        """
        Найти клиента по UUID во всех inbound'ах локальной базы

        :param client_uuid: UUID клиента
        :return: Данные клиента или None
        """
        try:
            # Используем локальную базу x-ui
            result = subprocess.run([
                'sqlite3', '/etc/x-ui/x-ui.db',
                'SELECT settings FROM inbounds WHERE enable=1'
            ], capture_output=True, text=True, timeout=10)

            if result.returncode != 0:
                return None

            for settings_str in result.stdout.strip().split('\n'):
                if not settings_str:
                    continue
                try:
                    settings = json.loads(settings_str)
                    for client in settings.get('clients', []):
                        if client.get('id') == client_uuid:
                            return client
                except:
                    continue

            return None
        except Exception as e:
            logger.error(f"Ошибка поиска клиента по UUID: {e}")
            return None

    async def find_client_by_email(self, email: str) -> Optional[dict]:
        """
        Найти клиента по email во всех inbound'ах локальной базы

        :param email: Email клиента
        :return: Данные клиента или None
        """
        try:
            result = subprocess.run([
                'sqlite3', '/etc/x-ui/x-ui.db',
                'SELECT settings FROM inbounds WHERE enable=1'
            ], capture_output=True, text=True, timeout=10)

            if result.returncode != 0:
                return None

            for settings_str in result.stdout.strip().split('\n'):
                if not settings_str:
                    continue
                try:
                    settings = json.loads(settings_str)
                    for client in settings.get('clients', []):
                        if client.get('email') == email:
                            return client
                except:
                    continue

            return None
        except Exception as e:
            logger.error(f"Ошибка поиска клиента по email: {e}")
            return None

    async def get_client_link(self, inbound_id: int, client_email: str, use_domain: str = None, max_retries: int = 3) -> Optional[str]:
        """
        Получить VLESS ссылку для клиента с автоматическим переподключением

        :param inbound_id: ID inbound
        :param client_email: Email клиента
        :param use_domain: Домен для подключения (если None, использует IP из inbound)
        :param max_retries: Максимальное количество попыток
        :return: VLESS ссылка или None
        """
        try:
            # Получаем данные inbound (уже с retry механизмом)
            inbound = await self.get_inbound(inbound_id, max_retries=max_retries)
            if not inbound:
                logger.error(f"Не удалось получить inbound для создания ссылки клиента {client_email}")
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
                return None

            # Формируем VLESS ссылку
            client_id = client.get('id')
            port = inbound.get('port')

            # Используем домен если указан, иначе берем listen (IP) из inbound
            if use_domain:
                host = use_domain
            else:
                host = inbound.get('listen', '')
                # Если listen пустой или 0.0.0.0, используем публичный IP сервера
                if not host or host == '0.0.0.0' or host == '':
                    # Импортируем SERVER_IP из конфига
                    from bot.config import SERVER_IP
                    host = SERVER_IP

            # Параметры подключения
            network = stream_settings.get('network', 'tcp')
            security = stream_settings.get('security', 'none')

            # Базовая VLESS ссылка
            vless_link = f"vless://{client_id}@{host}:{port}"

            # Параметры
            params = [
                f"type={network}",
                f"security={security}",
                f"encryption=none"
            ]

            # Добавляем параметры TLS если есть
            if security == 'tls':
                tls_settings = stream_settings.get('tlsSettings', {})
                sni = tls_settings.get('serverName', use_domain if use_domain else host)
                params.append(f"sni={sni}")

            # Добавляем параметры REALITY если используется
            if security == 'reality':
                reality_settings = stream_settings.get('realitySettings', {})

                # Flow для XTLS Vision (для новых клиентов)
                client_flow = client.get('flow', '')
                if client_flow:
                    params.append(f"flow={client_flow}")

                # Public Key (pbk)
                public_key = reality_settings.get('settings', {}).get('publicKey', '')
                if public_key:
                    params.append(f"pbk={public_key}")

                # Fingerprint (fp)
                fingerprint = reality_settings.get('settings', {}).get('fingerprint', 'chrome')
                params.append(f"fp={fingerprint}")

                # Server Name (sni)
                server_names = reality_settings.get('serverNames', [])
                if server_names:
                    sni = server_names[0]  # Берем первый SNI
                    params.append(f"sni={sni}")

                # Short ID (sid)
                short_ids = reality_settings.get('shortIds', [])
                if short_ids:
                    sid = short_ids[0]  # Берем первый Short ID
                    params.append(f"sid={sid}")

                # Spider X (spx)
                spider_x = reality_settings.get('settings', {}).get('spiderX', '/')
                if spider_x:
                    import urllib.parse
                    params.append(f"spx={urllib.parse.quote(spider_x)}")

            # Параметры WebSocket если используется
            if network == 'ws':
                ws_settings = stream_settings.get('wsSettings', {})
                path = ws_settings.get('path', '/')
                params.append(f"path={path}")
                ws_host = ws_settings.get('headers', {}).get('Host', use_domain if use_domain else host)
                params.append(f"host={ws_host}")

            # Добавляем префикс для LTE inbound (ID=28, порт 8449)
            # Формат имени: PREFIX пробел EMAIL (как в get_client_link_from_active_server)
            if inbound_id == 28:
                link_name = f"LTE Все операторы {client_email}"
            else:
                link_name = client_email

            vless_link += "?" + "&".join(params) + f"#{link_name}"

            logger.info(f"Успешно сформирована VLESS ссылка для клиента {client_email}")
            return vless_link

        except Exception as e:
            logger.error(f"Ошибка при формировании VLESS ссылки для клиента {client_email}: {e}")
            import traceback
            traceback.print_exc()
            return None

    @staticmethod
    def replace_ip_with_domain(vless_link: str, domain: str, port: int = 443) -> str:
        """
        Заменить IP адрес и порт на домен в VLESS ссылке, сохраняя все параметры

        :param vless_link: Оригинальная VLESS ссылка с IP
        :param domain: Домен для замены
        :param port: Порт для замены (по умолчанию 443)
        :return: VLESS ссылка с доменом и портом
        """
        import re
        # Паттерн для поиска vless://uuid@IP:PORT?params
        # Группы: (1) vless://uuid@ (2) IP (3) :port (4) ?params и все остальное
        pattern = r'(vless://[^@]+@)([^:]+):(\d+)(\?.+)'
        replacement = r'\1' + domain + f':{port}' + r'\4'
        return re.sub(pattern, replacement, vless_link)

    async def list_clients(self, inbound_id: int) -> list:
        """Получить список всех клиентов inbound"""
        try:
            inbound = await self.get_inbound(inbound_id)
            if not inbound:
                return []

            settings = json.loads(inbound.get('settings', '{}'))
            return settings.get('clients', [])
        except Exception as e:
            logger.error(f"List clients error: {e}")
            return []

    async def list_inbounds(self, max_retries: int = 3) -> list:
        """
        Получить список всех inbound'ов с автоматическим переподключением

        :param max_retries: Максимальное количество попыток
        :return: Список inbound'ов или []
        """
        for attempt in range(1, max_retries + 1):
            try:
                # Проверяем авторизацию
                if not await self._ensure_logged_in():
                    logger.error("Не удалось авторизоваться для получения списка inbound'ов")
                    return []

                url = f"{self.host}/panel/api/inbounds/list"

                async with self.session.get(url) as response:
                    if response.status == 200:
                        # Проверяем Content-Type перед парсингом JSON
                        content_type = response.headers.get('Content-Type', '')

                        # Если получили HTML вместо JSON - сессия истекла
                        if 'application/json' not in content_type:
                            logger.warning(f"Получен ответ с Content-Type: {content_type} вместо JSON. Сессия истекла.")
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return []

                        try:
                            data = await response.json()
                            if data.get('success'):
                                inbounds = data.get('obj', [])
                                logger.info(f"Успешно получен список из {len(inbounds)} inbound'ов")
                                return inbounds
                        except aiohttp.ContentTypeError as e:
                            # Если не смогли распарсить JSON - сессия истекла
                            logger.warning(f"Не удалось распарсить JSON ответ: {e}. Сессия истекла.")
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return []

                    # Если сессия истекла, пробуем переподключиться
                    if response.status in [401, 403]:
                        logger.warning(f"Сессия истекла при получении списка inbound'ов. Попытка {attempt}/{max_retries}")
                        self.session_cookie = None
                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue

                    logger.warning(f"Не удалось получить список inbound'ов, статус: {response.status}")
                    return []

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Сетевая ошибка при получении списка inbound'ов (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Ошибка при получении списка inbound'ов (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        logger.error(f"Не удалось получить список inbound'ов после {max_retries} попыток")
        return []

    async def restart_xray(self) -> bool:
        """
        Перезапустить xray для применения изменений конфига.
        Использует системный рестарт x-ui для гарантированного обновления конфига.
        """
        try:
            # Системный рестарт x-ui - гарантирует обновление config.json
            result = subprocess.run(
                ['systemctl', 'restart', 'x-ui'],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                logger.info("X-UI успешно перезапущен через systemctl")
                # Даём время на запуск xray и генерацию конфига
                await asyncio.sleep(2)
                return True
            else:
                logger.error(f"Ошибка systemctl restart x-ui: {result.stderr}")

                # Fallback на API если systemctl не сработал
                if not await self._ensure_logged_in():
                    logger.error("Не удалось авторизоваться для рестарта xray через API")
                    return False

                url = f"{self.host}/panel/api/inbounds/restartPanel"
                async with self.session.post(url) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get('success'):
                            logger.info("Xray перезапущен через API (fallback)")
                            await asyncio.sleep(2)
                            return True
                return False

        except subprocess.TimeoutExpired:
            logger.error("Таймаут при перезапуске x-ui")
            return False
        except Exception as e:
            logger.error(f"Ошибка при рестарте xray: {e}")
            return False

    async def update_reality_settings(self, inbound_id: int, dest: str, server_names: list, max_retries: int = 3) -> bool:
        """
        Обновить REALITY параметры inbound с автоматическим переподключением

        :param inbound_id: ID inbound
        :param dest: Новый Dest (Target), например "vk.com:443"
        :param server_names: Список SNI (Server Names), например ["vk.com", "www.vk.com"]
        :param max_retries: Максимальное количество попыток
        :return: True если успешно, False если ошибка
        """
        for attempt in range(1, max_retries + 1):
            try:
                # Проверяем авторизацию
                if not await self._ensure_logged_in():
                    logger.error("Не удалось авторизоваться для обновления REALITY параметров")
                    return False

                # Получаем текущие данные inbound
                inbound = await self.get_inbound(inbound_id, max_retries=1)
                if not inbound:
                    logger.error(f"Не удалось получить inbound {inbound_id} для обновления")
                    return False

                # Парсим streamSettings
                stream_settings = json.loads(inbound.get('streamSettings', '{}'))
                reality_settings = stream_settings.get('realitySettings', {})

                # Обновляем dest и serverNames
                reality_settings['dest'] = dest
                reality_settings['serverNames'] = server_names

                # Обновляем streamSettings
                stream_settings['realitySettings'] = reality_settings
                inbound['streamSettings'] = json.dumps(stream_settings, ensure_ascii=False)

                # Также обновляем settings если это JSON строка
                if isinstance(inbound.get('settings'), str):
                    pass  # settings остается без изменений

                # Формируем данные для обновления
                update_data = {
                    "id": inbound_id,
                    "up": inbound.get('up', 0),
                    "down": inbound.get('down', 0),
                    "total": inbound.get('total', 0),
                    "remark": inbound.get('remark', ''),
                    "enable": inbound.get('enable', True),
                    "expiryTime": inbound.get('expiryTime', 0),
                    "listen": inbound.get('listen', ''),
                    "port": inbound.get('port'),
                    "protocol": inbound.get('protocol'),
                    "settings": inbound.get('settings'),
                    "streamSettings": inbound['streamSettings'],
                    "sniffing": inbound.get('sniffing', '{"enabled":true,"destOverride":["http","tls"]}')
                }

                url = f"{self.host}/panel/api/inbounds/update/{inbound_id}"

                async with self.session.post(url, json=update_data) as response:
                    if response.status == 200:
                        # Проверяем Content-Type
                        content_type = response.headers.get('Content-Type', '')

                        # Если получили HTML - сессия истекла
                        if 'text/html' in content_type:
                            logger.warning(f"Получен HTML вместо JSON при обновлении inbound (попытка {attempt}/{max_retries})")
                            self.cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return False

                        result = await response.json()

                        if result.get('success'):
                            logger.info(f"Успешно обновлены REALITY параметры для inbound {inbound_id}: dest={dest}, SNI={server_names}")
                            return True
                        else:
                            logger.error(f"Ошибка при обновлении REALITY параметров: {result.get('msg')}")
                            return False
                    else:
                        logger.warning(f"Не удалось обновить REALITY параметры, статус: {response.status}")
                        return False

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Сетевая ошибка при обновлении REALITY параметров (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Ошибка при обновлении REALITY параметров (попытка {attempt}/{max_retries}): {e}")
                import traceback
                traceback.print_exc()
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        logger.error(f"Не удалось обновить REALITY параметры после {max_retries} попыток")
        return False

    async def delete_client(self, inbound_id: int, client_email: str, max_retries: int = 3) -> bool:
        """
        Удалить клиента из inbound по email

        :param inbound_id: ID inbound
        :param client_email: Email клиента для удаления
        :param max_retries: Максимальное количество попыток
        :return: True если успешно, False если ошибка
        """
        for attempt in range(1, max_retries + 1):
            try:
                # Проверяем авторизацию
                if not await self._ensure_logged_in():
                    logger.error("Не удалось авторизоваться для удаления клиента")
                    return False

                # Получаем UUID клиента из inbound
                inbound = await self.get_inbound(inbound_id, max_retries=1)
                if not inbound:
                    logger.error(f"Не удалось получить inbound {inbound_id} для удаления клиента")
                    return False

                settings = json.loads(inbound.get('settings', '{}'))
                clients = settings.get('clients', [])

                # Ищем клиента по email
                client_uuid = None
                for client in clients:
                    if client.get('email') == client_email:
                        client_uuid = client.get('id')
                        break

                if not client_uuid:
                    logger.warning(f"Клиент {client_email} не найден в inbound {inbound_id}")
                    return False

                # Удаляем клиента
                url = f"{self.host}/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}"

                async with self.session.post(url) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '')

                        if 'application/json' not in content_type:
                            logger.warning(f"Получен ответ с Content-Type: {content_type} вместо JSON. Сессия истекла.")
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return False

                        try:
                            data = await response.json()
                            if data.get('success'):
                                logger.info(f"Клиент {client_email} успешно удален из inbound {inbound_id}")
                                return True
                            else:
                                logger.error(f"Ошибка удаления клиента: {data.get('msg')}")
                                return False
                        except aiohttp.ContentTypeError as e:
                            logger.warning(f"Не удалось распарсить JSON ответ: {e}")
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return False

                    elif response.status in [401, 403]:
                        logger.warning(f"Сессия истекла при удалении клиента. Попытка {attempt}/{max_retries}")
                        self.session_cookie = None
                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue

                    logger.warning(f"Не удалось удалить клиента, статус: {response.status}")
                    return False

            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                logger.error(f"Сетевая ошибка при удалении клиента (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
            except Exception as e:
                logger.error(f"Ошибка при удалении клиента (попытка {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        logger.error(f"Не удалось удалить клиента {client_email} после {max_retries} попыток")
        return False

    async def find_and_delete_client(self, client_email: str, max_retries: int = 3) -> bool:
        """
        Найти и удалить клиента из всех inbound-ов по email

        :param client_email: Email клиента для удаления
        :param max_retries: Максимальное количество попыток
        :return: True если найден и удален, False если не найден или ошибка
        """
        try:
            # Получаем список всех inbound-ов
            inbounds = await self.list_inbounds(max_retries=max_retries)
            if not inbounds:
                logger.error("Не удалось получить список inbound'ов для поиска клиента")
                return False

            # Ищем клиента во всех inbound-ах
            for inbound in inbounds:
                inbound_id = inbound.get('id')
                settings = json.loads(inbound.get('settings', '{}'))
                clients = settings.get('clients', [])

                for client in clients:
                    if client.get('email') == client_email:
                        # Нашли клиента, удаляем
                        logger.info(f"Найден клиент {client_email} в inbound {inbound_id}, удаляем...")
                        return await self.delete_client(inbound_id, client_email, max_retries)

            logger.warning(f"Клиент {client_email} не найден ни в одном inbound")
            return False

        except Exception as e:
            logger.error(f"Ошибка при поиске и удалении клиента {client_email}: {e}")
            return False

    async def update_client(self, inbound_id: int, client_uuid: str, client_dict: dict, max_retries: int = 3) -> bool:
        """
        Обновить данные клиента

        :param inbound_id: ID inbound
        :param client_uuid: UUID клиента
        :param client_dict: Новые данные клиента
        :param max_retries: Максимальное количество попыток
        :return: True если успешно, False если ошибка
        """
        for attempt in range(1, max_retries + 1):
            try:
                if not await self._ensure_logged_in():
                    return False

                new_settings = json.dumps({'clients': [client_dict]})
                url = f"{self.host}/panel/api/inbounds/updateClient/{client_uuid}"
                payload = {'id': inbound_id, 'settings': new_settings}

                async with self.session.post(url, json=payload) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '')
                        if 'application/json' not in content_type:
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return False

                        data = await response.json()
                        if data.get('success'):
                            logger.info(f"Клиент {client_uuid} успешно обновлен")
                            return True
                        else:
                            logger.error(f"Ошибка обновления клиента: {data.get('msg')}")
                            return False

                    elif response.status in [401, 403]:
                        self.session_cookie = None
                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue

                    return False

            except Exception as e:
                logger.error(f"Ошибка обновления клиента (попытка {attempt}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        return False

    async def get_client_traffic(self, email: str, max_retries: int = 3) -> Optional[Dict]:
        """
        Получить статистику трафика клиента

        :param email: Email клиента
        :param max_retries: Максимальное количество попыток
        :return: Данные о трафике или None
        """
        for attempt in range(1, max_retries + 1):
            try:
                if not await self._ensure_logged_in():
                    return None

                url = f"{self.host}/panel/api/inbounds/getClientTraffics/{email}"

                async with self.session.get(url) as response:
                    if response.status == 200:
                        content_type = response.headers.get('Content-Type', '')
                        if 'application/json' not in content_type:
                            self.session_cookie = None
                            if attempt < max_retries:
                                await asyncio.sleep(1)
                                continue
                            return None

                        data = await response.json()
                        if data.get('success'):
                            return data.get('obj')
                        return None

                    elif response.status in [401, 403]:
                        self.session_cookie = None
                        if attempt < max_retries:
                            await asyncio.sleep(1)
                            continue

                    return None

            except Exception as e:
                logger.error(f"Ошибка получения трафика (попытка {attempt}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)

        return None

    async def extend_client(self, inbound_id: int, client_email: str, days: int, max_retries: int = 3) -> bool:
        """
        Продлить подписку клиента на указанное количество дней

        :param inbound_id: ID inbound
        :param client_email: Email клиента
        :param days: Количество дней для продления
        :param max_retries: Максимальное количество попыток
        :return: True если успешно, False если ошибка
        """
        try:
            # Получаем текущие данные inbound
            inbound = await self.get_inbound(inbound_id, max_retries=max_retries)
            if not inbound:
                return False

            settings = json.loads(inbound.get('settings', '{}'))
            clients = settings.get('clients', [])

            # Ищем клиента
            client = None
            for c in clients:
                if c.get('email') == client_email:
                    client = c
                    break

            if not client:
                logger.error(f"Клиент {client_email} не найден в inbound {inbound_id}")
                return False

            # Вычисляем новое время истечения
            current_expiry = client.get('expiryTime', 0)
            now_ms = int(datetime.now().timestamp() * 1000)

            if current_expiry > now_ms:
                # Добавляем к текущему времени
                new_expiry = current_expiry + (days * 24 * 60 * 60 * 1000)
            else:
                # Подписка истекла, продлеваем от текущего момента
                new_expiry = now_ms + (days * 24 * 60 * 60 * 1000)

            # Обновляем данные клиента
            client['expiryTime'] = new_expiry

            return await self.update_client(inbound_id, client['id'], client, max_retries)

        except Exception as e:
            logger.error(f"Ошибка продления подписки: {e}")
            return False

    async def enable_client(self, inbound_id: int, client_email: str, enable: bool = True, max_retries: int = 3) -> bool:
        """
        Включить/выключить клиента

        :param inbound_id: ID inbound
        :param client_email: Email клиента
        :param enable: True для включения, False для выключения
        :param max_retries: Максимальное количество попыток
        :return: True если успешно, False если ошибка
        """
        try:
            inbound = await self.get_inbound(inbound_id, max_retries=max_retries)
            if not inbound:
                return False

            settings = json.loads(inbound.get('settings', '{}'))
            clients = settings.get('clients', [])

            client = None
            for c in clients:
                if c.get('email') == client_email:
                    client = c
                    break

            if not client:
                logger.error(f"Клиент {client_email} не найден")
                return False

            client['enable'] = enable

            return await self.update_client(inbound_id, client['id'], client, max_retries)

        except Exception as e:
            logger.error(f"Ошибка изменения статуса клиента: {e}")
            return False
