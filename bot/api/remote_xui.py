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
    ip_limit: int = 2
) -> bool:
    """
    Создать клиента через API панели X-UI
    """
    server_name = server_config.get('name', 'Unknown')
    panel = server_config.get('panel', {})

    if not panel:
        logger.warning(f"Нет конфигурации панели для {server_name}")
        return False

    # Авторизуемся если нужно
    session = _get_panel_opener(server_name)
    if not session.get('logged_in'):
        if not await _panel_login(server_config):
            return False

    base_url = session.get('base_url', '')
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

    try:
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
            return True
        else:
            error_msg = result.get('msg', '')
            if 'Duplicate' in error_msg or 'exist' in error_msg.lower():
                logger.info(f"Клиент {email} уже существует на {server_name}")
                return True
            logger.error(f"Ошибка создания клиента на {server_name}: {error_msg}")
            # Сбрасываем сессию на случай истечения
            session['logged_in'] = False
            return False

    except Exception as e:
        logger.error(f"Ошибка создания клиента через панель {server_name}: {e}")
        session['logged_in'] = False
        return False


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
        return await create_client_via_panel(
            server_config=server_config,
            client_uuid=client_uuid,
            email=email,
            expire_days=expire_days,
            ip_limit=ip_limit
        )

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
