"""
Клиент для создания клиентов на удалённых X-UI серверах через SSH или API панели
"""
import asyncio
import json
import logging
import ssl
import urllib.request
import urllib.parse
import http.cookiejar
from pathlib import Path
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


# Кэш сессий для API панелей
_panel_sessions = {}


def load_servers_config():
    """Загрузить конфигурацию серверов"""
    config_path = Path('/root/manager_vpn/servers_config.json')
    if config_path.exists():
        with open(config_path, 'r') as f:
            return json.load(f)
    return {"servers": []}


def _get_panel_opener(server_name: str):
    """Получить или создать opener для панели"""
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

    session = _get_panel_opener(server_name)
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


async def create_client_via_panel(
    server_config: dict,
    client_uuid: str,
    email: str,
    expire_days: int,
    ip_limit: int = 2,
    max_retries: int = 2
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

    expire_time = int((datetime.now() + timedelta(days=expire_days)).timestamp() * 1000)

    # Получаем inbound ID из конфигурации
    inbounds = server_config.get('inbounds', {})
    main_inbound = inbounds.get('main', {})
    inbound_id = main_inbound.get('id', 1)

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
            "flow": ""
        }]
    }

    for attempt in range(1, max_retries + 1):
        try:
            # Авторизуемся если нужно
            session = _get_panel_opener(server_name)
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
            session = _get_panel_opener(server_name)
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
    session = _get_panel_opener(server_name)

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
            except:
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

    session = _get_panel_opener(server_name)
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
    ip_limit: int = 2
) -> bool:
    """
    Создать клиента на удалённом сервере через SSH или API панели

    :param server_config: Конфигурация сервера из servers_config.json
    :param client_uuid: UUID клиента (должен совпадать с локальным)
    :param email: Email/ID клиента
    :param expire_days: Срок действия в днях
    :param ip_limit: Лимит IP
    :return: True если успешно
    """
    if server_config.get('local', False):
        return True  # Пропускаем локальный сервер

    if not server_config.get('enabled', True):
        return True  # Сервер отключен

    # Если есть конфигурация панели - используем API
    panel_config = server_config.get('panel', {})
    if panel_config:
        result = await create_client_via_panel(
            server_config=server_config,
            client_uuid=client_uuid,
            email=email,
            expire_days=expire_days,
            ip_limit=ip_limit
        )
        return result.get('success', False)

    # Иначе используем SSH
    ssh_config = server_config.get('ssh', {})
    if not ssh_config:
        logger.warning(f"Нет SSH или Panel конфигурации для сервера {server_config.get('name')}")
        return False

    host = server_config.get('ip', '')
    user = ssh_config.get('user', 'root')
    password = ssh_config.get('password', '')

    if not host or not password:
        logger.warning(f"Неполные SSH данные для сервера {server_config.get('name')}")
        return False

    # Вычисляем время истечения в миллисекундах
    expire_time = int((datetime.now() + timedelta(days=expire_days)).timestamp() * 1000)

    # Python скрипт для добавления клиента во все inbound'ы
    sql_script = f"""
import json
import sqlite3

conn = sqlite3.connect('/etc/x-ui/x-ui.db')
cursor = conn.cursor()

# Получаем все inbound'ы
cursor.execute("SELECT id, settings FROM inbounds")
rows = cursor.fetchall()

added_count = 0
exists_count = 0

for inbound_id, settings_str in rows:
    try:
        settings = json.loads(settings_str)
        clients = settings.get('clients', [])

        # Проверяем, существует ли клиент по UUID или email
        existing_by_uuid = [c for c in clients if c.get('id') == '{client_uuid}']
        existing_by_email = [c for c in clients if c.get('email') == '{email}']
        if not existing_by_uuid and not existing_by_email:
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
                "flow": ""
            }}
            clients.append(new_client)
            settings['clients'] = clients
            cursor.execute("UPDATE inbounds SET settings=? WHERE id=?", (json.dumps(settings), inbound_id))
            added_count += 1
        else:
            exists_count += 1
    except Exception as e:
        pass

conn.commit()
conn.close()

if added_count > 0:
    print(f"OK:{{added_count}}")
elif exists_count > 0:
    print("EXISTS")
else:
    print("ERROR")
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
            return True
        elif result == "EXISTS":
            logger.info(f"Клиент {email} уже существует на сервере {server_config.get('name')}")
            return True
        else:
            logger.error(f"Ошибка создания клиента на {server_config.get('name')}: {result} {stderr.decode()}")
            return False

    except asyncio.TimeoutError:
        logger.error(f"Таймаут при создании клиента на {server_config.get('name')}")
        return False
    except Exception as e:
        logger.error(f"Ошибка при создании клиента на {server_config.get('name')}: {e}")
        return False


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

    :return: Словарь с результатами для каждого сервера
    """
    config = load_servers_config()
    results = {}

    for server in config.get('servers', []):
        if server.get('local', False):
            continue
        if not server.get('enabled', True):
            continue

        server_name = server.get('name', 'Unknown')
        success = await create_client_on_remote_server(
            server_config=server,
            client_uuid=client_uuid,
            email=email,
            expire_days=expire_days,
            ip_limit=ip_limit
        )
        results[server_name] = success

    return results


async def create_client_on_active_servers(
    client_uuid: str,
    email: str,
    expire_days: int,
    ip_limit: int = 2
) -> dict:
    """
    Создать клиента только на активных для новых подписок серверах

    :return: Словарь с результатами для каждого сервера
    """
    config = load_servers_config()
    results = {}

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

        server_name = server.get('name', 'Unknown')
        success = await create_client_on_remote_server(
            server_config=server,
            client_uuid=client_uuid,
            email=email,
            expire_days=expire_days,
            ip_limit=ip_limit
        )
        results[server_name] = success

    return results


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
    except:
        pass

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

    session = _get_panel_opener(server_name)
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
                        reality = stream.get('realitySettings', {})
                        reality_settings = reality.get('settings', {})

                        sni_list = reality.get('serverNames', [])
                        short_ids = reality.get('shortIds', [])
                        pbk = reality_settings.get('publicKey', '')
                        fp = reality.get('fingerprint', 'chrome')

                        return {
                            'email': client.get('email', ''),
                            'inbound_name': inbound_name,
                            'inbound_remark': inbound.get('remark', ''),
                            'inbound_id': inbound_id,
                            'expiry_time': client.get('expiryTime', 0),
                            'limit_ip': client.get('limitIp', 2),
                            'flow': client.get('flow', ''),
                            'enable': client.get('enable', True),
                            # Реальные параметры inbound с сервера
                            'inbound_settings': {
                                'security': security,
                                'sni': sni_list[0] if sni_list else '',
                                'pbk': pbk,
                                'sid': short_ids[0] if short_ids else '',
                                'fp': fp
                            }
                        }
            except:
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
    except:
        pass
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
                                'fp': reality.get('settings', {}).get('fingerprint', 'chrome'),
                                'flow': client.get('flow', '')
                            }
                        except:
                            pass

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
            except:
                continue
        return None
    except Exception as e:
        logger.error(f"Ошибка поиска в локальной базе: {e}")
        return None


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

        # Формируем VLESS ссылку
        vless_link = f"vless://{client_uuid}@{domain}:{port}"

        params = [
            "type=tcp",
            f"security={security}",
            "encryption=none"
        ]

        if security == 'reality':
            if pbk:
                params.append(f"pbk={pbk}")
            if fp:
                params.append(f"fp={fp}")
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

        vless_link = f"vless://{client_uuid}@{domain}:{port}"

        params = [
            "type=tcp",
            f"security={security}",
            "encryption=none"
        ]

        if security == 'reality':
            if pbk:
                params.append(f"pbk={pbk}")
            if fp:
                params.append(f"fp={fp}")
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
