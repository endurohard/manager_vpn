"""
Клиент для создания клиентов на удалённых X-UI серверах через SSH
"""
import asyncio
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def load_servers_config():
    """Загрузить конфигурацию серверов"""
    config_path = Path('/root/manager_vpn/servers_config.json')
    if config_path.exists():
        with open(config_path, 'r') as f:
            return json.load(f)
    return {"servers": []}


async def create_client_on_remote_server(
    server_config: dict,
    client_uuid: str,
    email: str,
    expire_days: int,
    ip_limit: int = 2
) -> bool:
    """
    Создать клиента на удалённом сервере через SSH

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

    ssh_config = server_config.get('ssh', {})
    if not ssh_config:
        logger.warning(f"Нет SSH конфигурации для сервера {server_config.get('name')}")
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

        # Проверяем, существует ли клиент
        existing = [c for c in clients if c.get('id') == '{client_uuid}']
        if not existing:
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
                "flow": "xtls-rprx-vision"
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
    Создать клиента на всех удалённых серверах

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


async def delete_client_on_remote_server(server_config: dict, client_uuid: str) -> bool:
    """Удалить клиента на удалённом сервере"""
    if server_config.get('local', False):
        return True

    ssh_config = server_config.get('ssh', {})
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
