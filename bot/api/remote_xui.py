"""
Клиент для создания клиентов на удалённых X-UI серверах через SSH или API панели
"""
import asyncio
import json
import logging
import os
import ssl
import urllib.request
import urllib.parse
import http.cookiejar
from pathlib import Path
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# Кэш сессий для API панелей с блокировкой для потокобезопасности
_panel_sessions = {}
_panel_sessions_lock = asyncio.Lock()


def load_servers_config():
    """
    Загрузить конфигурацию серверов.

    Пароли могут быть переопределены через переменные окружения:
    - VPN_SERVER_{NAME}_SSH_PASSWORD - SSH пароль для сервера
    - VPN_SERVER_{NAME}_PANEL_PASSWORD - пароль панели для сервера

    Где {NAME} - имя сервера в верхнем регистре (например: VPNPULSE, GERMANY)
    """
    config_path = Path('/root/manager_vpn/servers_config.json')
    if not config_path.exists():
        return {"servers": []}

    with open(config_path, 'r') as f:
        config = json.load(f)

    # Подставляем пароли из переменных окружения, если они установлены
    for server in config.get('servers', []):
        server_name = server.get('name', '').upper().replace(' ', '_').replace('-', '_')

        # SSH пароль
        ssh_config = server.get('ssh', {})
        if ssh_config:
            env_ssh_pass = os.environ.get(f'VPN_SERVER_{server_name}_SSH_PASSWORD')
            if env_ssh_pass:
                ssh_config['password'] = env_ssh_pass
                logger.debug(f"SSH пароль для {server.get('name')} загружен из переменной окружения")

        # Panel пароль
        panel_config = server.get('panel', {})
        if panel_config:
            env_panel_pass = os.environ.get(f'VPN_SERVER_{server_name}_PANEL_PASSWORD')
            if env_panel_pass:
                panel_config['password'] = env_panel_pass
                logger.debug(f"Panel пароль для {server.get('name')} загружен из переменной окружения")

    return config


async def _get_panel_opener(server_name: str):
    """Получить или создать opener для панели (потокобезопасно)"""
    async with _panel_sessions_lock:
        if server_name not in _panel_sessions:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            cookie_jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(cookie_jar),
                urllib.request.HTTPSHandler(context=ctx)
            )
            _panel_sessions[server_name] = {
                'opener': opener,
                'logged_in': False
            }
        return _panel_sessions[server_name]


async def _panel_login(server_config: dict) -> bool:
    """Авторизация в панели X-UI"""
    panel = server_config.get('panel', {})
    if not panel:
        return False

    server_name = server_config.get('name', 'Unknown')
    ip = server_config.get('ip', '')
    port = panel.get('port', 1020)
    path = panel.get('path', '')
    username = panel.get('username', '')
    password = panel.get('password', '')

    if not all([ip, username, password]):
        logger.warning(f"Неполные данные панели для {server_name}")
        return False

    session = await _get_panel_opener(server_name)
    base_url = f"https://{ip}:{port}{path}"

    try:
        login_data = urllib.parse.urlencode({
            'username': username,
            'password': password
        }).encode()

        login_req = urllib.request.Request(
            f"{base_url}/login",
            data=login_data,
            method='POST'
        )
        login_req.add_header('Content-Type', 'application/x-www-form-urlencoded')

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, session['opener'].open, login_req)
        result = json.loads(resp.read())

        if result.get('success'):
            session['logged_in'] = True
            session['base_url'] = base_url
            logger.info(f"Авторизация в панели {server_name} успешна")
            return True
        else:
            logger.error(f"Ошибка авторизации в панели {server_name}: {result.get('msg')}")
            return False

    except Exception as e:
        logger.error(f"Ошибка подключения к панели {server_name}: {e}")
        return False


async def get_inbound_settings_from_panel(
    server_config: dict,
    inbound_id: int = None
) -> dict:
    """
    Получить актуальные настройки inbound с панели сервера.

    Возвращает реальные параметры reality/tls/grpc с сервера, а не из статического конфига.

    :param server_config: Конфигурация сервера из servers_config.json
    :param inbound_id: ID inbound (если не указан, используется из конфига)
    :return: dict с настройками или None
    """
    server_name = server_config.get('name', 'Unknown')
    panel = server_config.get('panel', {})

    if not panel:
        logger.debug(f"Нет конфигурации панели для {server_name}, используем статический конфиг")
        return None

    # Определяем inbound_id
    if inbound_id is None:
        inbounds = server_config.get('inbounds', {})
        main_inbound = inbounds.get('main', {})
        inbound_id = main_inbound.get('id', 1)

    session = await _get_panel_opener(server_name)
    if not session.get('logged_in'):
        if not await _panel_login(server_config):
            logger.warning(f"Не удалось авторизоваться в панели {server_name}")
            return None

    base_url = session.get('base_url', '')
    opener = session.get('opener')

    try:
        # Получаем список всех inbound'ов
        list_url = f"{base_url}/panel/api/inbounds/list"
        list_req = urllib.request.Request(list_url)

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, opener.open, list_req)
        data = json.loads(response.read().decode())

        if not data.get('success'):
            logger.warning(f"Ошибка получения inbounds с {server_name}")
            return None

        # Ищем нужный inbound
        for inbound in data.get('obj', []):
            if inbound.get('id') == inbound_id:
                stream_str = inbound.get('streamSettings', '{}')
                settings_str = inbound.get('settings', '{}')

                try:
                    stream = json.loads(stream_str)
                    settings = json.loads(settings_str)

                    # Извлекаем реальные параметры
                    security = stream.get('security', 'reality')
                    network = stream.get('network', 'tcp')

                    result = {
                        'security': security,
                        'network': network,
                        'fp': 'chrome'  # default
                    }

                    # Получаем flow из первого клиента (все клиенты в inbound обычно используют одинаковый flow)
                    clients = settings.get('clients', [])
                    if clients and clients[0].get('flow'):
                        result['flow'] = clients[0].get('flow')

                    # Reality настройки
                    if security == 'reality':
                        reality = stream.get('realitySettings', {})
                        reality_settings = reality.get('settings', {})

                        sni_list = reality.get('serverNames', [])
                        short_ids = reality.get('shortIds', [])

                        result['sni'] = sni_list[0] if sni_list else ''
                        result['pbk'] = reality_settings.get('publicKey', '')
                        result['sid'] = short_ids[0] if short_ids else ''
                        result['fp'] = reality.get('settings', {}).get('fingerprint') or reality.get('fingerprint') or 'chrome'

                    # gRPC настройки
                    if network == 'grpc':
                        grpc_settings = stream.get('grpcSettings', {})
                        result['serviceName'] = grpc_settings.get('serviceName', '')
                        result['authority'] = grpc_settings.get('authority', '')

                    # Сохраняем name_prefix из статического конфига (его нет на панели)
                    inbounds_config = server_config.get('inbounds', {})
                    for name, cfg in inbounds_config.items():
                        if cfg.get('id') == inbound_id:
                            result['name_prefix'] = cfg.get('name_prefix', server_name)
                            break
                    else:
                        result['name_prefix'] = server_name

                    logger.info(f"Получены актуальные настройки inbound {inbound_id} с {server_name}: sni={result.get('sni')}, flow={result.get('flow', '')}")
                    return result

                except (json.JSONDecodeError, KeyError, TypeError) as e:
                    logger.error(f"Ошибка парсинга streamSettings inbound {inbound_id}: {e}")
                    return None

        logger.warning(f"Inbound {inbound_id} не найден на {server_name}")
        return None

    except Exception as e:
        logger.error(f"Ошибка получения настроек inbound с {server_name}: {e}")
        session['logged_in'] = False
        return None


async def create_client_via_panel(
    server_config: dict,
    client_uuid: str,
    email: str,
    expire_days: int,
    ip_limit: int = 2,
    max_retries: int = 2,
    inbound_id: int = None,
    expire_time_ms: int = None
) -> dict:
    """
    Создать клиента через API панели X-UI с retry при ошибках
    Возвращает: {'success': bool, 'uuid': str, 'existing': bool}
    - success=True, existing=False - клиент создан с переданным UUID
    - success=True, existing=True, uuid=... - клиент с таким email уже есть, возвращаем его UUID
    - success=False - ошибка
    """
    server_name = server_config.get('name', 'Unknown')
    panel = server_config.get('panel', {})

    if not panel:
        logger.warning(f"Нет конфигурации панели для {server_name}")
        return {'success': False}

    if expire_time_ms is not None:
        expire_time = expire_time_ms
    else:
        expire_time = int((datetime.now() + timedelta(days=expire_days)).timestamp() * 1000)

    # Получаем inbound ID из конфигурации
    inbounds = server_config.get('inbounds', {})
    main_inbound = inbounds.get('main', {})
    if inbound_id is None:
        inbound_id = main_inbound.get('id', 1)

    # Получаем flow из существующих клиентов на сервере (приоритет)
    # или из конфигурации (fallback)
    flow = ''
    try:
        inbound_settings = await get_inbound_settings_from_panel(server_config, inbound_id)
        if inbound_settings and inbound_settings.get('flow'):
            flow = inbound_settings.get('flow')
            logger.debug(f"Flow '{flow}' получен из существующих клиентов на {server_name}")
        else:
            # Fallback на статический конфиг
            flow = main_inbound.get('flow', '')
            if flow:
                logger.debug(f"Flow '{flow}' получен из статического конфига для {server_name}")
    except Exception as e:
        logger.warning(f"Не удалось получить flow с сервера {server_name}, используем конфиг: {e}")
        flow = main_inbound.get('flow', '')

    client_settings = {
        "clients": [{
            "id": client_uuid,
            "alterId": 0,
            "email": email,
            "limitIp": ip_limit,
            "totalGB": 0,
            "expiryTime": expire_time,
            "enable": True,
            "tgId": "",
            "subId": "",
            "flow": flow
        }]
    }

    for attempt in range(1, max_retries + 1):
        try:
            # Авторизуемся если нужно
            session = await _get_panel_opener(server_name)
            if not session.get('logged_in'):
                if not await _panel_login(server_config):
                    if attempt < max_retries:
                        logger.warning(f"Не удалось авторизоваться в панели {server_name}, попытка {attempt}/{max_retries}")
                        await asyncio.sleep(1)
                        continue
                    return {'success': False}

            base_url = session.get('base_url', '')
            add_url = f"{base_url}/panel/api/inbounds/addClient"
            payload = urllib.parse.urlencode({
                'id': inbound_id,
                'settings': json.dumps(client_settings)
            }).encode()

            add_req = urllib.request.Request(add_url, data=payload, method='POST')
            add_req.add_header('Content-Type', 'application/x-www-form-urlencoded')

            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, session['opener'].open, add_req)
            result = json.loads(resp.read())

            if result.get('success'):
                logger.info(f"Клиент {email} создан на {server_name} через API")
                return {'success': True, 'uuid': client_uuid, 'existing': False}
            else:
                error_msg = result.get('msg', '')
                if 'Duplicate' in error_msg or 'exist' in error_msg.lower():
                    # Клиент с таким email уже существует - ищем его UUID
                    logger.info(f"Клиент {email} уже существует на {server_name}, ищем UUID...")
                    existing_uuid = await _find_client_uuid_by_email(server_config, email)
                    if existing_uuid:
                        logger.info(f"Найден существующий клиент {email} с UUID {existing_uuid}")
                        return {'success': True, 'uuid': existing_uuid, 'existing': True}
                    else:
                        logger.warning(f"Не удалось найти UUID клиента {email}")
                        return {'success': False}
                logger.error(f"Ошибка создания клиента на {server_name}: {error_msg}")
                # Сбрасываем сессию на случай истечения
                session['logged_in'] = False
                if attempt < max_retries:
                    await asyncio.sleep(1)
                    continue
                return {'success': False}

        except Exception as e:
            logger.warning(f"Ошибка создания клиента через панель {server_name} (попытка {attempt}/{max_retries}): {e}")
            session = await _get_panel_opener(server_name)
            session['logged_in'] = False
            if attempt < max_retries:
                await asyncio.sleep(1)
                continue
            logger.error(f"Не удалось создать клиента через панель {server_name} после {max_retries} попыток")
            return {'success': False}

    return {'success': False}


async def _find_client_uuid_by_email(server_config: dict, email: str) -> str:
    """Найти UUID клиента по email на сервере"""
    server_name = server_config.get('name', 'Unknown')
    session = await _get_panel_opener(server_name)

    if not session.get('logged_in'):
        return None

    base_url = session.get('base_url', '')
    opener = session.get('opener')

    try:
        list_url = f"{base_url}/panel/api/inbounds/list"
        list_req = urllib.request.Request(list_url)

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, opener.open, list_req)
        data = json.loads(response.read().decode())

        if not data.get('success'):
            return None

        for inbound in data.get('obj', []):
            settings_str = inbound.get('settings', '{}')
            try:
                settings = json.loads(settings_str)
                for client in settings.get('clients', []):
                    if client.get('email') == email:
                        return client.get('id')
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.debug(f"Ошибка при разборе settings inbound: {e}")
                continue

        return None
    except Exception as e:
        logger.error(f"Ошибка поиска клиента по email: {e}")
        return None


async def delete_client_via_panel(
    server_config: dict,
    client_uuid: str
) -> bool:
    """
    Удалить клиента через API панели X-UI
    """
    server_name = server_config.get('name', 'Unknown')
    panel = server_config.get('panel', {})

    if not panel:
        return False

    session = await _get_panel_opener(server_name)
    if not session.get('logged_in'):
        if not await _panel_login(server_config):
            return False

    base_url = session.get('base_url', '')

    # Получаем inbound ID
    inbounds = server_config.get('inbounds', {})
    main_inbound = inbounds.get('main', {})
    inbound_id = main_inbound.get('id', 1)

    try:
        # X-UI API для удаления клиента
        del_url = f"{base_url}/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}"
        del_req = urllib.request.Request(del_url, method='POST')

        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, session['opener'].open, del_req)
        result = json.loads(resp.read())

        if result.get('success'):
            logger.info(f"Клиент {client_uuid} удалён с {server_name}")
            return True
        else:
            logger.error(f"Ошибка удаления клиента с {server_name}: {result.get('msg')}")
            return False

    except Exception as e:
        logger.error(f"Ошибка удаления клиента через панель {server_name}: {e}")
        session['logged_in'] = False
        return False


async def create_client_on_remote_server(
    server_config: dict,
    client_uuid: str,
    email: str,
    expire_days: int,
    ip_limit: int = 2,
    inbound_id: int = None
) -> dict:
    """
    Создать клиента на удалённом сервере через SSH или API панели

    :param server_config: Конфигурация сервера из servers_config.json
    :param client_uuid: UUID клиента (должен совпадать с локальным)
    :param email: Email/ID клиента
    :param expire_days: Срок действия в днях
    :param ip_limit: Лимит IP
    :return: {'success': bool, 'uuid': str} - uuid может отличаться если клиент уже существовал
    """
    if server_config.get('local', False):
        return {'success': True, 'uuid': client_uuid}  # Пропускаем локальный сервер

    if not server_config.get('enabled', True):
        return {'success': True, 'uuid': client_uuid}  # Сервер отключен

    # Если есть конфигурация панели - используем API
    panel_config = server_config.get('panel', {})
    if panel_config:
        result = await create_client_via_panel(
            server_config=server_config,
            client_uuid=client_uuid,
            email=email,
            expire_days=expire_days,
            ip_limit=ip_limit,
            inbound_id=inbound_id
        )
        # Возвращаем UUID - либо переданный, либо существующий на сервере
        return {
            'success': result.get('success', False),
            'uuid': result.get('uuid', client_uuid),
            'existing': result.get('existing', False)
        }

    # Иначе используем SSH
    ssh_config = server_config.get('ssh', {})
    if not ssh_config:
        logger.warning(f"Нет SSH или Panel конфигурации для сервера {server_config.get('name')}")
        return {'success': False, 'uuid': client_uuid}

    host = server_config.get('ip', '')
    user = ssh_config.get('user', 'root')
    password = ssh_config.get('password', '')

    if not host or not password:
        logger.warning(f"Неполные SSH данные для сервера {server_config.get('name')}")
        return {'success': False, 'uuid': client_uuid}

    # Вычисляем время истечения в миллисекундах
    expire_time = int((datetime.now() + timedelta(days=expire_days)).timestamp() * 1000)

    # Получаем inbound_id из конфигурации если не указан
    inbounds = server_config.get('inbounds', {})
    main_inbound = inbounds.get('main', {})
    if inbound_id is None:
        inbound_id = main_inbound.get('id', 1)  # По умолчанию id=1

    # Python скрипт для добавления клиента в конкретный inbound
    # Flow берётся из первого существующего клиента на сервере
    sql_script = f"""
import json
import sqlite3

target_inbound_id = {inbound_id}

conn = sqlite3.connect('/etc/x-ui/x-ui.db')
cursor = conn.cursor()

# Получаем указанный inbound
cursor.execute("SELECT id, settings FROM inbounds WHERE id=?", (target_inbound_id,))
row = cursor.fetchone()

if not row:
    print("ERROR:INBOUND_NOT_FOUND")
    conn.close()
    exit()

inbound_id, settings_str = row

try:
    settings = json.loads(settings_str)
    clients = settings.get('clients', [])

    # Проверяем, существует ли клиент по UUID или email
    existing_by_uuid = [c for c in clients if c.get('id') == '{client_uuid}']
    existing_by_email = [c for c in clients if c.get('email') == '{email}']

    if existing_by_uuid or existing_by_email:
        print("EXISTS")
    else:
        # Получаем flow из первого существующего клиента
        flow = ''
        for c in clients:
            if c.get('flow'):
                flow = c.get('flow')
                break

        new_client = {{
            "id": "{client_uuid}",
            "alterId": 0,
            "email": "{email}",
            "limitIp": {ip_limit},
            "totalGB": 0,
            "expiryTime": {expire_time},
            "enable": True,
            "tgId": "",
            "subId": "",
            "flow": flow
        }}
        clients.append(new_client)
        settings['clients'] = clients
        cursor.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(settings), inbound_id))
        conn.commit()
        print("OK:1")
except Exception as e:
    print(f"ERROR:{{e}}")

conn.close()
"""

    try:
        # Кодируем скрипт в base64
        import base64
        script_b64 = base64.b64encode(sql_script.encode()).decode()

        # Выполняем через SSH с декодированием base64
        cmd = f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {user}@{host} \"echo {script_b64} | base64 -d | python3\""

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        result = stdout.decode().strip()

        if result.startswith("OK"):
            # OK:N где N - количество inbound'ов куда добавлен клиент
            count = result.split(":")[1] if ":" in result else "1"
            logger.info(f"Клиент {email} создан на сервере {server_config.get('name')} в {count} inbound(ах)")
            # Перезапускаем x-ui на удалённом сервере
            await restart_remote_xui(server_config)
            return {'success': True, 'uuid': client_uuid}
        elif result == "EXISTS":
            logger.info(f"Клиент {email} уже существует на сервере {server_config.get('name')}")
            # При SSH клиент существует с тем же UUID (скрипт проверяет по UUID)
            return {'success': True, 'uuid': client_uuid, 'existing': True}
        else:
            logger.error(f"Ошибка создания клиента на {server_config.get('name')}: {result} {stderr.decode()}")
            return {'success': False, 'uuid': client_uuid}

    except asyncio.TimeoutError:
        logger.error(f"Таймаут при создании клиента на {server_config.get('name')}")
        return {'success': False, 'uuid': client_uuid}
    except Exception as e:
        logger.error(f"Ошибка при создании клиента на {server_config.get('name')}: {e}")
        return {'success': False, 'uuid': client_uuid}


async def restart_remote_xui(server_config: dict) -> bool:
    """Перезапустить x-ui на удалённом сервере"""
    ssh_config = server_config.get('ssh', {})
    host = server_config.get('ip', '')
    user = ssh_config.get('user', 'root')
    password = ssh_config.get('password', '')

    try:
        cmd = f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {user}@{host} 'x-ui restart' 2>&1"

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)

        logger.info(f"X-UI перезапущен на {server_config.get('name')}")
        return True
    except Exception as e:
        logger.error(f"Ошибка перезапуска x-ui на {server_config.get('name')}: {e}")
        return False


async def create_client_on_all_remote_servers(
    client_uuid: str,
    email: str,
    expire_days: int,
    ip_limit: int = 2
) -> dict:
    """
    Создать клиента на всех удалённых серверах (устаревшая функция)

    :return: Словарь с результатами для каждого сервера (bool для совместимости)
    """
    config = load_servers_config()
    results = {}

    for server in config.get('servers', []):
        if server.get('local', False):
            continue
        if not server.get('enabled', True):
            continue

        server_name = server.get('name', 'Unknown')
        result = await create_client_on_remote_server(
            server_config=server,
            client_uuid=client_uuid,
            email=email,
            expire_days=expire_days,
            ip_limit=ip_limit
        )
        # Для совместимости возвращаем только bool
        results[server_name] = result.get('success', False)

    return results


async def create_client_on_active_servers(
    client_uuid: str,
    email: str,
    expire_days: int,
    ip_limit: int = 2,
    rollback_on_failure: bool = True
) -> dict:
    """
    Создать клиента только на активных для новых подписок серверах

    :param rollback_on_failure: Если True, при неудаче на одном сервере удаляет клиента со всех успешных
    :return: {
        'results': {server_name: bool},  # Успешность для каждого сервера
        'uuid': str,  # Реальный UUID (может отличаться если клиент существовал)
        'any_existing': bool,  # True если клиент уже существовал хотя бы на одном сервере
        'all_success': bool,  # True если все серверы успешны
        'rollback_performed': bool  # True если был выполнен rollback
    }
    """
    config = load_servers_config()
    results = {}
    final_uuid = client_uuid
    any_existing = False
    successful_servers = []  # Серверы, где клиент успешно создан (для rollback)

    active_servers = []
    for server in config.get('servers', []):
        # Пропускаем локальный сервер - он обрабатывается отдельно
        if server.get('local', False):
            continue
        # Пропускаем отключенные серверы
        if not server.get('enabled', True):
            continue
        # Пропускаем серверы, не активные для новых подписок
        if not server.get('active_for_new', True):
            logger.info(f"Сервер {server.get('name')} отключен для новых подписок, пропускаем")
            continue
        active_servers.append(server)

    has_failure = False
    for server in active_servers:
        server_name = server.get('name', 'Unknown')
        result = await create_client_on_remote_server(
            server_config=server,
            client_uuid=client_uuid,
            email=email,
            expire_days=expire_days,
            ip_limit=ip_limit
        )
        success = result.get('success', False)
        results[server_name] = success

        if success:
            successful_servers.append(server)
            # Если клиент уже существовал на сервере, используем его UUID
            if result.get('existing', False) and result.get('uuid'):
                final_uuid = result.get('uuid')
                any_existing = True
                logger.info(f"Клиент {email} существует на {server_name} с UUID {final_uuid}")
        else:
            has_failure = True
            logger.error(f"Не удалось создать клиента {email} на сервере {server_name}")

    # Выполняем rollback при неудаче, если требуется
    rollback_performed = False
    if has_failure and rollback_on_failure and successful_servers:
        logger.warning(f"Выполняется rollback создания клиента {email} на {len(successful_servers)} серверах")
        for server in successful_servers:
            server_name = server.get('name', 'Unknown')
            try:
                deleted = await delete_client_on_remote_server(server, client_uuid)
                if deleted:
                    logger.info(f"Rollback: клиент {email} удалён с {server_name}")
                    results[server_name] = False  # Отмечаем как неуспешный после rollback
                else:
                    logger.warning(f"Rollback: не удалось удалить клиента {email} с {server_name}")
            except Exception as e:
                logger.error(f"Rollback: ошибка удаления клиента {email} с {server_name}: {e}")
        rollback_performed = True

    return {
        'results': results,
        'uuid': final_uuid,
        'any_existing': any_existing,
        'all_success': not has_failure,
        'rollback_performed': rollback_performed
    }


async def delete_client_on_remote_server(server_config: dict, client_uuid: str) -> bool:
    """Удалить клиента на удалённом сервере через SSH или API панели"""
    if server_config.get('local', False):
        return True

    # Если есть конфигурация панели - используем API
    panel_config = server_config.get('panel', {})
    if panel_config:
        return await delete_client_via_panel(server_config, client_uuid)

    # Иначе используем SSH
    ssh_config = server_config.get('ssh', {})
    if not ssh_config:
        logger.warning(f"Нет SSH или Panel конфигурации для {server_config.get('name')}")
        return False

    host = server_config.get('ip', '')
    user = ssh_config.get('user', 'root')
    password = ssh_config.get('password', '')

    sql_script = f'''
import json
import sqlite3

conn = sqlite3.connect('/etc/x-ui/x-ui.db')
cursor = conn.cursor()

# Удаляем клиента из всех inbound'ов
cursor.execute("SELECT id, settings FROM inbounds")
rows = cursor.fetchall()

deleted_count = 0
for inbound_id, settings_str in rows:
    try:
        settings = json.loads(settings_str)
        clients = settings.get('clients', [])
        original_len = len(clients)
        clients = [c for c in clients if c.get('id') != '{client_uuid}']
        if len(clients) < original_len:
            settings['clients'] = clients
            cursor.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(settings), inbound_id))
            deleted_count += 1
    except Exception as e:
        print(f"Error processing inbound {{inbound_id}}: {{e}}", file=__import__('sys').stderr)

conn.commit()
conn.close()
print("OK" if deleted_count > 0 else "NOT_FOUND")
'''

    try:
        cmd = f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {user}@{host} \"python3 -c '{sql_script}'\""

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if stdout.decode().strip() == "OK":
            await restart_remote_xui(server_config)
            return True
        return False
    except Exception as e:
        logger.error(f"Ошибка удаления клиента на {server_config.get('name')}: {e}")
        return False


async def delete_client_on_all_remote_servers(client_uuid: str) -> dict:
    """Удалить клиента на всех удалённых серверах"""
    config = load_servers_config()
    results = {}

    for server in config.get('servers', []):
        if server.get('local', False):
            continue
        if not server.get('enabled', True):
            continue

        server_name = server.get('name', 'Unknown')
        success = await delete_client_on_remote_server(server, client_uuid)
        results[server_name] = success

    return results


async def delete_client_by_email_via_panel(server_config: dict, email: str) -> bool:
    """
    Удалить клиента по email через API панели X-UI
    Сначала находит UUID клиента по email, затем удаляет
    """
    server_name = server_config.get('name', 'Unknown')
    panel = server_config.get('panel', {})

    if not panel:
        return False

    session = await _get_panel_opener(server_name)
    if not session.get('logged_in'):
        if not await _panel_login(server_config):
            return False

    base_url = session.get('base_url', '')
    opener = session.get('opener')

    try:
        # Получаем список всех inbound'ов
        list_url = f"{base_url}/panel/api/inbounds/list"
        list_req = urllib.request.Request(list_url)

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, opener.open, list_req)
        data = json.loads(response.read().decode())

        if not data.get('success'):
            return False

        # Ищем клиента по email во всех inbounds
        for inbound in data.get('obj', []):
            inbound_id = inbound.get('id')
            settings_str = inbound.get('settings', '{}')

            try:
                settings = json.loads(settings_str)
                for client in settings.get('clients', []):
                    if client.get('email') == email:
                        client_uuid = client.get('id')
                        if client_uuid:
                            # Удаляем клиента
                            del_url = f"{base_url}/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}"
                            del_req = urllib.request.Request(del_url, method='POST')
                            resp = await loop.run_in_executor(None, opener.open, del_req)
                            result = json.loads(resp.read())

                            if result.get('success'):
                                logger.info(f"Клиент {email} удалён с {server_name}")
                                return True
                            else:
                                logger.error(f"Ошибка удаления {email} с {server_name}: {result.get('msg')}")
                                return False
            except Exception as e:
                logger.warning(f"Ошибка при обработке inbound на {server_name}: {e}")
                continue

        logger.info(f"Клиент {email} не найден на {server_name}")
        return False

    except Exception as e:
        logger.error(f"Ошибка удаления клиента {email} через панель {server_name}: {e}")
        session['logged_in'] = False
        return False


async def delete_client_by_email_via_ssh(server_config: dict, email: str) -> bool:
    """Удалить клиента по email на удалённом сервере через SSH"""
    ssh_config = server_config.get('ssh', {})
    if not ssh_config:
        return False

    host = server_config.get('ip', '')
    user = ssh_config.get('user', 'root')
    password = ssh_config.get('password', '')

    if not host or not password:
        return False

    sql_script = f'''
import json
import sqlite3

conn = sqlite3.connect('/etc/x-ui/x-ui.db')
cursor = conn.cursor()

cursor.execute("SELECT id, settings FROM inbounds")
rows = cursor.fetchall()

deleted_count = 0
for inbound_id, settings_str in rows:
    try:
        settings = json.loads(settings_str)
        clients = settings.get('clients', [])
        original_len = len(clients)
        clients = [c for c in clients if c.get('email') != '{email}']
        if len(clients) < original_len:
            settings['clients'] = clients
            cursor.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(settings), inbound_id))
            deleted_count += 1
    except Exception as e:
        print(f"Error processing inbound {{inbound_id}}: {{e}}", file=__import__('sys').stderr)

conn.commit()
conn.close()
print("OK" if deleted_count > 0 else "NOT_FOUND")
'''

    try:
        import base64
        script_b64 = base64.b64encode(sql_script.encode()).decode()
        cmd = f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {user}@{host} \"echo {script_b64} | base64 -d | python3\""

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        if stdout.decode().strip() == "OK":
            await restart_remote_xui(server_config)
            logger.info(f"Клиент {email} удалён с {server_config.get('name')} через SSH")
            return True
        return False
    except Exception as e:
        logger.error(f"Ошибка удаления клиента {email} на {server_config.get('name')}: {e}")
        return False


async def delete_client_by_email_on_all_remote_servers(email: str) -> dict:
    """
    Удалить клиента по email на всех удалённых серверах

    :param email: Email клиента для удаления
    :return: Словарь с результатами {server_name: bool}
    """
    config = load_servers_config()
    results = {}

    for server in config.get('servers', []):
        if server.get('local', False):
            continue
        if not server.get('enabled', True):
            continue

        server_name = server.get('name', 'Unknown')

        # Пробуем через API панели
        panel_config = server.get('panel', {})
        if panel_config:
            success = await delete_client_by_email_via_panel(server, email)
        else:
            # Иначе через SSH
            success = await delete_client_by_email_via_ssh(server, email)

        results[server_name] = success

    return results


async def find_client_on_server(server_config: dict, client_uuid: str) -> dict:
    """
    Найти клиента по UUID на сервере через API панели
    Возвращает: {'email': ..., 'inbound_name': ..., 'inbound_id': ..., 'expiry_time': ..., 'limit_ip': ...,
                 'inbound_settings': {'sni': ..., 'pbk': ..., 'sid': ..., 'security': ..., 'fp': ...}}
    """
    server_name = server_config.get('name', 'Unknown')
    panel = server_config.get('panel', {})

    if not panel:
        # Нет API панели - пробуем через SSH
        return await _find_client_via_ssh(server_config, client_uuid)

    session = await _get_panel_opener(server_name)
    if not session.get('logged_in'):
        if not await _panel_login(server_config):
            return None

    base_url = session.get('base_url', '')
    opener = session.get('opener')

    try:
        # Получаем список всех inbound'ов
        list_url = f"{base_url}/panel/api/inbounds/list"
        list_req = urllib.request.Request(list_url)

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, opener.open, list_req)
        data = json.loads(response.read().decode())

        if not data.get('success'):
            return None

        # Карта inbound_id -> название из конфига
        inbounds_config = server_config.get('inbounds', {})
        id_to_name = {}
        for name, cfg in inbounds_config.items():
            inbound_id = cfg.get('id')
            if inbound_id:
                id_to_name[inbound_id] = name

        # Ищем клиента во всех inbounds
        for inbound in data.get('obj', []):
            inbound_id = inbound.get('id')
            settings_str = inbound.get('settings', '{}')
            stream_str = inbound.get('streamSettings', '{}')

            try:
                settings = json.loads(settings_str)
                stream = json.loads(stream_str)

                for client in settings.get('clients', []):
                    if client.get('id') == client_uuid:
                        inbound_name = id_to_name.get(inbound_id, inbound.get('remark', 'main'))

                        # Извлекаем реальные параметры inbound с сервера
                        security = stream.get('security', 'reality')
                        network = stream.get('network', 'tcp')
                        reality = stream.get('realitySettings', {})
                        reality_settings = reality.get('settings', {})

                        sni_list = reality.get('serverNames', [])
                        short_ids = reality.get('shortIds', [])
                        pbk = reality_settings.get('publicKey', '')
                        # fingerprint может быть в reality.settings.fingerprint или reality.fingerprint
                        fp = reality_settings.get('fingerprint') or reality.get('fingerprint') or 'chrome'

                        # Параметры для gRPC
                        grpc_settings = stream.get('grpcSettings', {})
                        service_name = grpc_settings.get('serviceName', '')
                        authority = grpc_settings.get('authority', '')

                        # Flow берём из клиента или из первого клиента inbound
                        client_flow = client.get('flow', '')
                        if not client_flow:
                            # Ищем flow у других клиентов в этом inbound
                            for c in settings.get('clients', []):
                                if c.get('flow'):
                                    client_flow = c.get('flow')
                                    break

                        return {
                            'email': client.get('email', ''),
                            'inbound_name': inbound_name,
                            'inbound_remark': inbound.get('remark', ''),
                            'inbound_id': inbound_id,
                            'inbound_port': inbound.get('port', 443),
                            'expiry_time': client.get('expiryTime', 0),
                            'limit_ip': client.get('limitIp', 2),
                            'flow': client_flow,
                            'enable': client.get('enable', True),
                            # Реальные параметры inbound с сервера
                            'inbound_settings': {
                                'security': security,
                                'network': network,
                                'sni': sni_list[0] if sni_list else '',
                                'pbk': pbk,
                                'sid': short_ids[0] if short_ids else '',
                                'fp': fp,
                                'flow': client_flow,
                                'serviceName': service_name,
                                'authority': authority
                            }
                        }
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.debug(f"Ошибка при разборе inbound {inbound_id}: {e}")
                continue

        return None

    except Exception as e:
        logger.error(f"Ошибка поиска клиента на {server_name}: {e}")
        session['logged_in'] = False
        return None


async def _find_client_via_ssh(server_config: dict, client_uuid: str) -> dict:
    """Найти клиента через SSH (для серверов без API панели)"""
    ssh_config = server_config.get('ssh', {})
    host = server_config.get('ip', '')
    user = ssh_config.get('user', 'root')
    password = ssh_config.get('password', '')

    if not host or not password:
        return None

    sql_script = f'''
import json
import sqlite3

conn = sqlite3.connect('/etc/x-ui/x-ui.db')
cursor = conn.cursor()
cursor.execute("SELECT id, settings FROM inbounds WHERE enable=1")
rows = cursor.fetchall()
conn.close()

for inbound_id, settings_str in rows:
    try:
        settings = json.loads(settings_str)
        for client in settings.get('clients', []):
            if client.get('id') == '{client_uuid}':
                print(json.dumps({{
                    'email': client.get('email', ''),
                    'inbound_id': inbound_id,
                    'expiry_time': client.get('expiryTime', 0),
                    'limit_ip': client.get('limitIp', 2),
                    'flow': client.get('flow', ''),
                    'enable': client.get('enable', True)
                }}))
                exit()
    except Exception as e:
        print(f"Error: {{e}}", file=__import__('sys').stderr)
print('NOT_FOUND')
'''

    try:
        cmd = f"sshpass -p '{password}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {user}@{host} \"python3 -c '{sql_script}'\""

        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)

        result = stdout.decode().strip()
        if result and result != 'NOT_FOUND':
            data = json.loads(result)
            # Определяем имя inbound из конфига
            inbounds_config = server_config.get('inbounds', {})
            inbound_id = data.get('inbound_id')
            inbound_name = 'main'
            for name, cfg in inbounds_config.items():
                if cfg.get('id') == inbound_id:
                    inbound_name = name
                    break
            data['inbound_name'] = inbound_name
            return data
        return None
    except Exception as e:
        logger.error(f"Ошибка поиска клиента через SSH на {server_config.get('name')}: {e}")
        return None


async def find_client_on_local_server(client_uuid: str) -> dict:
    """Найти клиента в локальной базе X-UI с настройками inbound"""
    import sqlite3

    try:
        conn = sqlite3.connect('/etc/x-ui/x-ui.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id, remark, port, settings, stream_settings FROM inbounds WHERE enable=1")
        rows = cursor.fetchall()
        conn.close()

        for inbound_id, remark, port, settings_str, stream_str in rows:
            try:
                settings = json.loads(settings_str)
                for client in settings.get('clients', []):
                    if client.get('id') == client_uuid:
                        # Парсим stream_settings для получения reality параметров
                        inbound_settings = {}
                        try:
                            stream = json.loads(stream_str) if stream_str else {}
                            reality = stream.get('realitySettings', {})
                            server_names = reality.get('serverNames', [])
                            short_ids = reality.get('shortIds', [])
                            inbound_settings = {
                                'security': 'reality',
                                'sni': server_names[0] if server_names else '',
                                'pbk': reality.get('settings', {}).get('publicKey', ''),
                                'sid': short_ids[0] if short_ids else '',
                                'fp': reality.get('fingerprint') or 'chrome',
                                'flow': client.get('flow', '')
                            }
                        except (json.JSONDecodeError, KeyError, IndexError) as e:
                            logger.debug(f"Не удалось получить stream settings: {e}")

                        return {
                            'email': client.get('email', ''),
                            'inbound_id': inbound_id,
                            'inbound_remark': remark or f'Inbound-{inbound_id}',
                            'inbound_port': port,
                            'inbound_settings': inbound_settings,
                            'expiry_time': client.get('expiryTime', 0),
                            'limit_ip': client.get('limitIp', 2),
                            'flow': client.get('flow', ''),
                            'enable': client.get('enable', True)
                        }
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.debug(f"Ошибка обработки inbound: {e}")
                continue
        return None
    except Exception as e:
        logger.error(f"Ошибка поиска в локальной базе: {e}")
        return None


async def _create_client_local_with_uuid(
    client_uuid: str,
    email: str,
    expire_time_ms: int = 0,
    ip_limit: int = 2
) -> bool:
    """Создать клиента в локальной базе X-UI с заданным UUID"""
    import sqlite3

    try:
        conn = sqlite3.connect('/etc/x-ui/x-ui.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id, settings FROM inbounds WHERE enable=1 LIMIT 1")
        row = cursor.fetchone()

        if not row:
            conn.close()
            return False

        inbound_id, settings_str = row
        settings = json.loads(settings_str)
        clients = settings.get('clients', [])

        # Проверяем, не существует ли уже
        for c in clients:
            if c.get('id') == client_uuid or c.get('email') == email:
                conn.close()
                return True  # Уже существует — считаем успехом

        # Получаем flow из существующих клиентов
        flow = ''
        for c in clients:
            if c.get('flow'):
                flow = c.get('flow')
                break

        new_client = {
            "id": client_uuid,
            "alterId": 0,
            "email": email,
            "limitIp": ip_limit,
            "totalGB": 0,
            "expiryTime": expire_time_ms,
            "enable": True,
            "tgId": "",
            "subId": "",
            "flow": flow
        }
        clients.append(new_client)
        settings['clients'] = clients

        cursor.execute(
            "UPDATE inbounds SET settings=? WHERE id=?",
            (json.dumps(settings), inbound_id)
        )
        conn.commit()
        conn.close()

        # Перезапускаем x-ui
        import subprocess
        try:
            subprocess.run(['systemctl', 'restart', 'x-ui'], timeout=30, check=False)
            logger.info(f"Клиент {email} создан локально, x-ui перезапущен")
        except Exception as e:
            logger.warning(f"Не удалось перезапустить x-ui: {e}")

        return True
    except Exception as e:
        logger.error(f"Ошибка создания клиента локально: {e}")
        return False


async def find_client_presence_on_all_servers(client_uuid: str) -> dict:
    """
    Проверить наличие клиента на всех enabled серверах.

    :param client_uuid: UUID клиента
    :return: {'found_on': [...], 'not_found_on': [...]}
    """
    config = load_servers_config()
    found_on = []
    not_found_on = []

    for server in config.get('servers', []):
        if not server.get('enabled', True):
            continue

        server_name = server.get('name', 'Unknown')
        # Получаем name_prefix из main inbound
        main_inbound = server.get('inbounds', {}).get('main', {})
        name_prefix = main_inbound.get('name_prefix', server_name)

        try:
            if server.get('local', False):
                client_info = await find_client_on_local_server(client_uuid)
            else:
                client_info = await find_client_on_server(server, client_uuid)

            if client_info:
                found_on.append({
                    'server_name': server_name,
                    'name_prefix': name_prefix,
                    'server_config': server,
                    'email': client_info.get('email', ''),
                    'expiry_time': client_info.get('expiry_time', 0),
                    'ip_limit': client_info.get('limit_ip', 2),
                    'inbound_id': client_info.get('inbound_id', 1)
                })
            else:
                not_found_on.append({
                    'server_name': server_name,
                    'name_prefix': name_prefix,
                    'server_config': server
                })
        except Exception as e:
            logger.error(f"Ошибка проверки клиента на {server_name}: {e}")
            not_found_on.append({
                'server_name': server_name,
                'name_prefix': name_prefix,
                'server_config': server
            })

    return {
        'found_on': found_on,
        'not_found_on': not_found_on
    }


async def extend_client_expiry_via_panel(
    server_config: dict,
    client_uuid: str,
    extend_days: int,
    inbound_id: int = None
) -> dict:
    """
    Продлить срок действия клиента через API панели X-UI

    :param server_config: Конфигурация сервера
    :param client_uuid: UUID клиента
    :param extend_days: Количество дней для продления
    :param inbound_id: ID inbound (если не указан - ищем клиента во всех)
    :return: {'success': bool, 'new_expiry': timestamp, 'error': str}
    """
    server_name = server_config.get('name', 'Unknown')
    panel = server_config.get('panel', {})

    if not panel:
        return {'success': False, 'error': 'Нет конфигурации панели'}

    session = await _get_panel_opener(server_name)
    if not session.get('logged_in'):
        if not await _panel_login(server_config):
            return {'success': False, 'error': 'Ошибка авторизации'}

    base_url = session.get('base_url', '')
    opener = session.get('opener')

    try:
        # Получаем список inbounds чтобы найти клиента
        list_url = f"{base_url}/panel/api/inbounds/list"
        list_req = urllib.request.Request(list_url)

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, opener.open, list_req)
        data = json.loads(response.read().decode())

        if not data.get('success'):
            return {'success': False, 'error': 'Не удалось получить список inbounds'}

        # Ищем клиента
        for inbound in data.get('obj', []):
            current_inbound_id = inbound.get('id')
            if inbound_id is not None and current_inbound_id != inbound_id:
                continue

            settings_str = inbound.get('settings', '{}')
            try:
                settings = json.loads(settings_str)
                clients = settings.get('clients', [])

                for i, client in enumerate(clients):
                    if client.get('id') == client_uuid:
                        # Нашли клиента - вычисляем новое время истечения
                        current_expiry = client.get('expiryTime', 0)
                        now_ms = int(datetime.now().timestamp() * 1000)

                        # Если срок истёк - продлеваем от текущего момента
                        # Если срок не истёк - добавляем к текущему сроку
                        if current_expiry > 0 and current_expiry > now_ms:
                            new_expiry = current_expiry + (extend_days * 24 * 60 * 60 * 1000)
                        else:
                            new_expiry = int((datetime.now() + timedelta(days=extend_days)).timestamp() * 1000)

                        # Обновляем клиента
                        client['expiryTime'] = new_expiry

                        # Формируем payload для API
                        client_settings = {"clients": [client]}

                        update_url = f"{base_url}/panel/api/inbounds/updateClient/{client_uuid}"
                        payload = urllib.parse.urlencode({
                            'id': current_inbound_id,
                            'settings': json.dumps(client_settings)
                        }).encode()

                        update_req = urllib.request.Request(update_url, data=payload, method='POST')
                        update_req.add_header('Content-Type', 'application/x-www-form-urlencoded')

                        resp = await loop.run_in_executor(None, opener.open, update_req)
                        result = json.loads(resp.read())

                        if result.get('success'):
                            logger.info(f"Клиент {client_uuid} продлён на {extend_days} дней на {server_name}")
                            return {
                                'success': True,
                                'new_expiry': new_expiry,
                                'email': client.get('email', '')
                            }
                        else:
                            error_msg = result.get('msg', 'Unknown error')
                            logger.error(f"Ошибка продления клиента на {server_name}: {error_msg}")
                            return {'success': False, 'error': error_msg}
            except Exception as e:
                continue

        return {'success': False, 'error': 'Клиент не найден'}

    except Exception as e:
        logger.error(f"Ошибка продления клиента на {server_name}: {e}")
        session['logged_in'] = False
        return {'success': False, 'error': str(e)}


async def extend_client_on_all_servers(client_uuid: str, extend_days: int) -> dict:
    """
    Продлить срок действия клиента на всех серверах

    :param client_uuid: UUID клиента
    :param extend_days: Количество дней для продления
    :return: {'success': bool, 'results': dict, 'new_expiry': timestamp}
    """
    config = load_servers_config()
    results = {}
    new_expiry = None
    any_success = False

    for server in config.get('servers', []):
        if not server.get('enabled', True):
            continue

        server_name = server.get('name', 'Unknown')

        if server.get('local', False):
            # Локальный сервер - используем прямое подключение к SQLite
            result = await _extend_client_local(client_uuid, extend_days)
        else:
            # Удалённый сервер - через API панели
            result = await extend_client_expiry_via_panel(server, client_uuid, extend_days)

        results[server_name] = result.get('success', False)

        if result.get('success'):
            any_success = True
            if result.get('new_expiry'):
                new_expiry = result.get('new_expiry')

    return {
        'success': any_success,
        'results': results,
        'new_expiry': new_expiry
    }


async def extend_client_on_server(server_name: str, client_uuid: str, extend_days: int) -> dict:
    """
    Продлить срок действия клиента на конкретном сервере

    :param server_name: Имя сервера (или начало имени)
    :param client_uuid: UUID клиента
    :param extend_days: Количество дней для продления
    :return: {'success': bool, 'new_expiry': timestamp, 'error': str}
    """
    config = load_servers_config()

    # Ищем сервер по имени (поддержка частичного совпадения)
    target_server = None
    for server in config.get('servers', []):
        name = server.get('name', '')
        if name.lower().startswith(server_name.lower()) or server_name.lower() in name.lower():
            target_server = server
            break

    if not target_server:
        return {'success': False, 'error': f'Сервер "{server_name}" не найден'}

    if not target_server.get('enabled', True):
        return {'success': False, 'error': f'Сервер "{server_name}" отключён'}

    actual_name = target_server.get('name', server_name)

    if target_server.get('local', False):
        # Локальный сервер
        result = await _extend_client_local(client_uuid, extend_days)
    else:
        # Удалённый сервер через API
        result = await extend_client_expiry_via_panel(target_server, client_uuid, extend_days)

    if result.get('success'):
        logger.info(f"Клиент {client_uuid[:8]}... продлён на {extend_days} дней на сервере {actual_name}")

    return result


async def _extend_client_local(client_uuid: str, extend_days: int) -> dict:
    """Продлить клиента в локальной базе X-UI"""
    import sqlite3

    try:
        conn = sqlite3.connect('/etc/x-ui/x-ui.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id, settings FROM inbounds")
        rows = cursor.fetchall()

        updated = False
        new_expiry = None

        for inbound_id, settings_str in rows:
            try:
                settings = json.loads(settings_str)
                clients = settings.get('clients', [])

                for client in clients:
                    if client.get('id') == client_uuid:
                        current_expiry = client.get('expiryTime', 0)
                        now_ms = int(datetime.now().timestamp() * 1000)

                        if current_expiry > 0 and current_expiry > now_ms:
                            new_expiry = current_expiry + (extend_days * 24 * 60 * 60 * 1000)
                        else:
                            new_expiry = int((datetime.now() + timedelta(days=extend_days)).timestamp() * 1000)

                        client['expiryTime'] = new_expiry
                        cursor.execute(
                            "UPDATE inbounds SET settings=? WHERE id=?",
                            (json.dumps(settings), inbound_id)
                        )
                        updated = True
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.debug(f"Ошибка при обработке inbound {inbound_id}: {e}")
                continue

        conn.commit()
        conn.close()

        if updated:
            logger.info(f"Клиент {client_uuid} продлён на {extend_days} дней (локально)")
            # Перезапускаем x-ui для применения изменений
            import subprocess
            try:
                subprocess.run(['systemctl', 'restart', 'x-ui'], timeout=30, check=False)
                logger.info("X-UI перезапущен после продления (локально)")
            except Exception as restart_err:
                logger.warning(f"Не удалось перезапустить x-ui: {restart_err}")
            return {'success': True, 'new_expiry': new_expiry}
        return {'success': False, 'error': 'Клиент не найден'}

    except Exception as e:
        logger.error(f"Ошибка продления клиента локально: {e}")
        return {'success': False, 'error': str(e)}


async def get_client_link_from_active_server(client_uuid: str, client_email: str) -> str:
    """
    Получить VLESS ссылку для клиента с активного сервера

    :param client_uuid: UUID клиента
    :param client_email: Email клиента
    :return: VLESS ссылка или None
    """
    config = load_servers_config()

    # Ищем активный для новых подписок сервер
    for server in config.get('servers', []):
        if not server.get('enabled', True):
            continue
        if not server.get('active_for_new', False):
            continue

        server_name = server.get('name', 'Unknown')
        domain = server.get('domain', server.get('ip', ''))
        port = server.get('port', 443)

        # Получаем настройки inbound из конфига
        inbounds = server.get('inbounds', {})
        main_inbound = inbounds.get('main', {})

        sni = main_inbound.get('sni', '')
        pbk = main_inbound.get('pbk', '')
        sid = main_inbound.get('sid', '')
        fp = main_inbound.get('fp', 'chrome')
        security = main_inbound.get('security', 'reality')
        flow = main_inbound.get('flow', '')
        name_prefix = main_inbound.get('name_prefix', '')
        network = main_inbound.get('network', 'tcp')

        # Формируем VLESS ссылку
        vless_link = f"vless://{client_uuid}@{domain}:{port}"

        params = [
            f"type={network}",
            "encryption=none"
        ]

        # Добавляем gRPC параметры если нужно
        if network == 'grpc':
            params.append(f"serviceName={main_inbound.get('serviceName', '')}")
            params.append(f"authority={main_inbound.get('authority', '')}")

        params.append(f"security={security}")

        if security == 'reality':
            if pbk:
                params.append(f"pbk={pbk}")
            params.append(f"fp={fp or 'chrome'}")
            if sni:
                params.append(f"sni={sni}")
            if sid:
                params.append(f"sid={sid}")
            if flow:
                params.append(f"flow={flow}")
            params.append("spx=%2F")

        # Название ссылки
        link_name = f"{name_prefix} {client_email}" if name_prefix else client_email

        vless_link += "?" + "&".join(params) + f"#{link_name}"

        logger.info(f"Сгенерирована VLESS ссылка с сервера {server_name} для {client_email}")
        return vless_link

    # Если не нашли активный сервер, ищем любой включенный
    for server in config.get('servers', []):
        if not server.get('enabled', True):
            continue
        if server.get('local', False):
            continue

        server_name = server.get('name', 'Unknown')
        domain = server.get('domain', server.get('ip', ''))
        port = server.get('port', 443)

        inbounds = server.get('inbounds', {})
        main_inbound = inbounds.get('main', {})

        sni = main_inbound.get('sni', '')
        pbk = main_inbound.get('pbk', '')
        sid = main_inbound.get('sid', '')
        fp = main_inbound.get('fp', 'chrome')
        security = main_inbound.get('security', 'reality')
        flow = main_inbound.get('flow', '')
        name_prefix = main_inbound.get('name_prefix', '')
        network = main_inbound.get('network', 'tcp')

        vless_link = f"vless://{client_uuid}@{domain}:{port}"

        params = [
            f"type={network}",
            "encryption=none"
        ]

        # Добавляем gRPC параметры если нужно
        if network == 'grpc':
            params.append(f"serviceName={main_inbound.get('serviceName', '')}")
            params.append(f"authority={main_inbound.get('authority', '')}")

        params.append(f"security={security}")

        if security == 'reality':
            if pbk:
                params.append(f"pbk={pbk}")
            params.append(f"fp={fp or 'chrome'}")
            if sni:
                params.append(f"sni={sni}")
            if sid:
                params.append(f"sid={sid}")
            if flow:
                params.append(f"flow={flow}")
            params.append("spx=%2F")

        link_name = f"{name_prefix} {client_email}" if name_prefix else client_email
        vless_link += "?" + "&".join(params) + f"#{link_name}"

        logger.info(f"Сгенерирована VLESS ссылка с сервера {server_name} для {client_email}")
        return vless_link

    logger.error(f"Не найден активный сервер для генерации ссылки")
    return None
