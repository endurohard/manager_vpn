"""
Сервис мониторинга подключённых устройств.

Периодически проверяет количество уникальных IP-адресов на ВСЕХ серверах
для каждого клиента. Если суммарное количество устройств превышает ip_limit
клиента — отключает его на всех панелях и уведомляет админа.
"""
import asyncio
import functools
import json
import logging
import time
import urllib.request
from typing import Dict, List, Optional, Set, Tuple

import aiosqlite

from bot.config import ADMIN_ID, DATABASE_PATH
from bot.api.remote_xui import (
    load_servers_config,
    _get_panel_opener,
    _panel_login,
    PANEL_REQUEST_TIMEOUT,
)

logger = logging.getLogger(__name__)

# Интервал проверки (секунды)
CHECK_INTERVAL = 120  # 2 минуты

# Кеш заблокированных клиентов (UUID -> timestamp блокировки)
# Чтобы не слать повторные уведомления
_blocked_clients: Dict[str, float] = {}


async def get_client_ips_from_panel(server_config: dict, email: str) -> Set[str]:
    """
    Получить множество IP-адресов клиента с конкретной панели.

    :param server_config: Конфигурация сервера
    :param email: Email клиента на панели
    :return: Множество IP-адресов
    """
    server_name = server_config.get('name', 'Unknown')
    panel = server_config.get('panel', {})

    if not panel:
        return set()

    session = await _get_panel_opener(server_name)
    if not session.get('logged_in'):
        if not await _panel_login(server_config):
            return set()

    base_url = session.get('base_url', '')
    opener = session.get('opener')

    try:
        url = f"{base_url}/panel/api/inbounds/clientIps/{email}"
        req = urllib.request.Request(url)

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, functools.partial(opener.open, req, timeout=PANEL_REQUEST_TIMEOUT)
        )
        data = json.loads(response.read().decode())

        if data.get('success'):
            obj = data.get('obj', '')
            if not obj or obj == 'No IP Record':
                return set()
            # obj может быть строкой IP через запятую или \n
            ips = set()
            for part in str(obj).replace('\n', ',').split(','):
                ip = part.strip()
                if ip and ip != 'No IP Record':
                    ips.add(ip)
            return ips
        return set()

    except urllib.error.HTTPError as e:
        if e.code == 401:
            session['logged_in'] = False
        logger.debug(f"Ошибка получения IP с {server_name} для {email}: HTTP {e.code}")
        return set()
    except Exception as e:
        logger.debug(f"Ошибка получения IP с {server_name} для {email}: {e}")
        return set()


async def toggle_client_on_panel(
    server_config: dict,
    client_uuid: str,
    inbound_id: int,
    client_data: dict,
    enable: bool,
) -> bool:
    """
    Включить/выключить клиента на конкретной панели.

    :param server_config: Конфигурация сервера
    :param client_uuid: UUID клиента
    :param inbound_id: ID inbound
    :param client_data: Текущие данные клиента (dict из settings.clients[])
    :param enable: True для включения, False для выключения
    :return: Успешность операции
    """
    server_name = server_config.get('name', 'Unknown')
    session = await _get_panel_opener(server_name)
    if not session.get('logged_in'):
        if not await _panel_login(server_config):
            return False

    base_url = session.get('base_url', '')
    opener = session.get('opener')

    try:
        updated_client = dict(client_data)
        updated_client['enable'] = enable

        payload = json.dumps({
            'id': inbound_id,
            'settings': json.dumps({'clients': [updated_client]})
        }).encode()

        url = f"{base_url}/panel/api/inbounds/updateClient/{client_uuid}"
        req = urllib.request.Request(url, data=payload, method='POST')
        req.add_header('Content-Type', 'application/json')

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None, functools.partial(opener.open, req, timeout=PANEL_REQUEST_TIMEOUT)
        )
        result = json.loads(response.read().decode())

        if result.get('success'):
            action = "включён" if enable else "отключён"
            logger.info(f"Клиент {client_uuid} {action} на {server_name}")
            return True
        else:
            logger.error(f"Не удалось обновить клиента {client_uuid} на {server_name}: {result.get('msg')}")
            return False

    except urllib.error.HTTPError as e:
        if e.code == 401:
            session['logged_in'] = False
        logger.error(f"HTTP ошибка при toggle клиента на {server_name}: {e.code}")
        return False
    except Exception as e:
        logger.error(f"Ошибка toggle клиента на {server_name}: {e}")
        return False


async def get_active_clients_with_limits() -> List[dict]:
    """
    Получить всех активных клиентов с их ip_limit из БД.

    :return: [{'uuid': ..., 'email': ..., 'ip_limit': ..., 'telegram_id': ...}, ...]
    """
    clients = []
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                """
                SELECT c.uuid, c.email, c.ip_limit, c.telegram_id, c.status
                FROM clients c
                WHERE c.status = 'active' AND c.ip_limit > 0
                """
            ) as cursor:
                async for row in cursor:
                    clients.append({
                        'uuid': row['uuid'],
                        'email': row['email'],
                        'ip_limit': row['ip_limit'],
                        'telegram_id': row['telegram_id'],
                    })
    except Exception as e:
        logger.error(f"Ошибка получения клиентов из БД: {e}")
    return clients


async def collect_client_presence_on_servers(servers: list) -> Dict[str, List[dict]]:
    """
    Собрать информацию о всех клиентах на всех серверах.
    Возвращает dict: uuid -> [{server_name, email, inbound_id, client_data}, ...]
    """
    uuid_to_servers: Dict[str, List[dict]] = {}

    for server_config in servers:
        server_name = server_config.get('name', 'Unknown')
        panel = server_config.get('panel', {})
        if not panel:
            continue

        session = await _get_panel_opener(server_name)
        if not session.get('logged_in'):
            if not await _panel_login(server_config):
                continue

        base_url = session.get('base_url', '')
        opener = session.get('opener')

        try:
            list_url = f"{base_url}/panel/api/inbounds/list"
            list_req = urllib.request.Request(list_url)

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None, functools.partial(opener.open, list_req, timeout=PANEL_REQUEST_TIMEOUT)
            )
            data = json.loads(response.read().decode())

            if not data.get('success'):
                continue

            for inbound in data.get('obj', []):
                inbound_id = inbound.get('id')
                settings_str = inbound.get('settings', '{}')
                try:
                    settings = json.loads(settings_str)
                    for client in settings.get('clients', []):
                        uuid = client.get('id', '')
                        email = client.get('email', '')
                        if uuid and email:
                            if uuid not in uuid_to_servers:
                                uuid_to_servers[uuid] = []
                            uuid_to_servers[uuid].append({
                                'server_name': server_name,
                                'server_config': server_config,
                                'email': email,
                                'inbound_id': inbound_id,
                                'client_data': client,
                            })
                except (json.JSONDecodeError, KeyError, TypeError):
                    continue

        except urllib.error.HTTPError as e:
            if e.code == 401:
                session['logged_in'] = False
            logger.error(f"Ошибка сбора клиентов с {server_name}: HTTP {e.code}")
        except Exception as e:
            logger.error(f"Ошибка сбора клиентов с {server_name}: {e}")

    return uuid_to_servers


async def check_device_limits(bot=None):
    """
    Основная проверка: для каждого активного клиента считаем
    уникальные IP через все серверы и блокируем при превышении.
    """
    global _blocked_clients

    servers_config = load_servers_config()
    servers = [s for s in servers_config.get('servers', [])
               if s.get('enabled', True) and s.get('panel')]

    if not servers:
        return

    # 1. Получаем лимиты из БД
    db_clients = await get_active_clients_with_limits()
    if not db_clients:
        return

    uuid_to_limit = {c['uuid']: c for c in db_clients}

    # 2. Собираем присутствие клиентов на серверах
    uuid_to_servers = await collect_client_presence_on_servers(servers)

    # 3. Для каждого клиента собираем IP со всех серверов
    violations = []
    restored = []

    for uuid, client_info in uuid_to_limit.items():
        ip_limit = client_info['ip_limit']
        server_entries = uuid_to_servers.get(uuid, [])

        if not server_entries:
            # Клиент не найден на серверах — может быть, уже удалён
            # Если был заблокирован нами — убираем из кеша
            if uuid in _blocked_clients:
                del _blocked_clients[uuid]
            continue

        # Собираем уникальные IP со всех серверов
        all_ips: Set[str] = set()
        for entry in server_entries:
            ips = await get_client_ips_from_panel(
                entry['server_config'],
                entry['email']
            )
            all_ips.update(ips)

        total_devices = len(all_ips)

        if total_devices > ip_limit:
            # Превышение лимита
            if uuid not in _blocked_clients:
                # Новое нарушение — блокируем на ВСЕХ серверах
                logger.warning(
                    f"Клиент {uuid} ({client_info['email']}): "
                    f"{total_devices} устройств > лимит {ip_limit}. Блокировка."
                )
                blocked_ok = True
                for entry in server_entries:
                    success = await toggle_client_on_panel(
                        entry['server_config'],
                        uuid,
                        entry['inbound_id'],
                        entry['client_data'],
                        enable=False,
                    )
                    if not success:
                        blocked_ok = False

                if blocked_ok:
                    _blocked_clients[uuid] = time.time()
                    violations.append({
                        'uuid': uuid,
                        'email': client_info['email'],
                        'telegram_id': client_info.get('telegram_id'),
                        'ip_limit': ip_limit,
                        'devices': total_devices,
                        'ips': list(all_ips),
                        'servers': [e['server_name'] for e in server_entries],
                    })

        elif uuid in _blocked_clients:
            # Клиент был заблокирован, но теперь в пределах лимита — разблокируем
            logger.info(
                f"Клиент {uuid} ({client_info['email']}): "
                f"{total_devices} устройств <= лимит {ip_limit}. Разблокировка."
            )
            for entry in server_entries:
                await toggle_client_on_panel(
                    entry['server_config'],
                    uuid,
                    entry['inbound_id'],
                    entry['client_data'],
                    enable=True,
                )
            del _blocked_clients[uuid]
            restored.append({
                'uuid': uuid,
                'email': client_info['email'],
                'ip_limit': ip_limit,
                'devices': total_devices,
            })

    # 4. Уведомляем админа
    if bot and (violations or restored):
        await notify_admin(bot, violations, restored)


async def notify_admin(bot, violations: list, restored: list):
    """Отправить уведомление админу о нарушениях и восстановлениях."""
    lines = []

    if violations:
        lines.append("🚫 <b>Превышение лимита устройств:</b>\n")
        for v in violations:
            lines.append(
                f"• <b>{v['email']}</b>\n"
                f"  Устройств: <b>{v['devices']}</b> / лимит: <b>{v['ip_limit']}</b>\n"
                f"  IP: <code>{', '.join(v['ips'])}</code>\n"
                f"  Серверы: {', '.join(v['servers'])}\n"
                f"  ➜ Клиент <b>заблокирован</b> на всех серверах"
            )

    if restored:
        lines.append("\n✅ <b>Лимит восстановлен (разблокированы):</b>\n")
        for r in restored:
            lines.append(
                f"• <b>{r['email']}</b> — "
                f"{r['devices']}/{r['ip_limit']} устройств"
            )

    if lines:
        message = '\n'.join(lines)
        try:
            await bot.send_message(ADMIN_ID, message, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления админу: {e}")


async def device_monitor_loop(bot=None):
    """Основной цикл мониторинга устройств."""
    logger.info(f"Запуск мониторинга устройств (интервал: {CHECK_INTERVAL}с)")

    while True:
        try:
            await check_device_limits(bot)
        except Exception as e:
            logger.error(f"Ошибка в цикле мониторинга устройств: {e}", exc_info=True)

        await asyncio.sleep(CHECK_INTERVAL)


def get_blocked_clients() -> Dict[str, float]:
    """Получить текущий список заблокированных клиентов."""
    return dict(_blocked_clients)
