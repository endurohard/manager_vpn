"""
Маршруты для управления серверами
"""
import json
import os
import asyncio
import socket
import ssl
import urllib.request
import urllib.parse
import http.cookiejar
from datetime import datetime
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

servers_router = APIRouter()
templates: Jinja2Templates = None

CONFIG_PATH = '/root/manager_vpn/servers_config.json'


def setup_servers_router(tpl: Jinja2Templates):
    global templates
    templates = tpl


def load_servers_config():
    """Загрузить конфигурацию серверов"""
    try:
        with open(CONFIG_PATH, 'r') as f:
            return json.load(f)
    except:
        return {'servers': []}


def save_servers_config(config):
    """Сохранить конфигурацию серверов"""
    with open(CONFIG_PATH, 'w') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)


async def check_port(host: str, port: int, timeout: float = 3.0) -> bool:
    """Проверить доступность порта"""
    try:
        loop = asyncio.get_event_loop()
        future = loop.run_in_executor(None, _check_port_sync, host, port, timeout)
        return await asyncio.wait_for(future, timeout=timeout + 1)
    except:
        return False


def _check_port_sync(host: str, port: int, timeout: float) -> bool:
    """Синхронная проверка порта"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except:
        return False


async def check_panel_api(server: dict) -> dict:
    """Проверить доступность API панели и получить количество клиентов"""
    panel = server.get('panel', {})
    if not panel:
        return {'available': False, 'clients': 0, 'error': 'No panel config'}

    ip = server.get('ip', '')
    port = panel.get('port', 1020)
    path = panel.get('path', '')
    username = panel.get('username', '')
    password = panel.get('password', '')

    if not all([ip, username, password]):
        return {'available': False, 'clients': 0, 'error': 'Incomplete config'}

    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        cookie_jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cookie_jar),
            urllib.request.HTTPSHandler(context=ctx)
        )

        base_url = f"https://{ip}:{port}{path}"

        # Авторизация
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
        resp = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: opener.open(login_req, timeout=5)),
            timeout=10
        )
        login_result = json.loads(resp.read())

        if not login_result.get('success'):
            return {'available': False, 'clients': 0, 'error': 'Auth failed'}

        # Получаем список inbounds для подсчёта клиентов
        list_req = urllib.request.Request(f"{base_url}/panel/api/inbounds/list")
        resp = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: opener.open(list_req, timeout=5)),
            timeout=10
        )
        data = json.loads(resp.read())

        if not data.get('success'):
            return {'available': True, 'clients': 0, 'error': 'Cannot get inbounds'}

        # Считаем клиентов
        total_clients = 0
        for inbound in data.get('obj', []):
            settings_str = inbound.get('settings', '{}')
            try:
                settings = json.loads(settings_str)
                total_clients += len(settings.get('clients', []))
            except:
                pass

        return {'available': True, 'clients': total_clients, 'error': None}

    except asyncio.TimeoutError:
        return {'available': False, 'clients': 0, 'error': 'Timeout'}
    except Exception as e:
        return {'available': False, 'clients': 0, 'error': str(e)[:50]}


async def get_server_status(server: dict) -> dict:
    """Получить полный статус сервера"""
    ip = server.get('ip', '')
    port = server.get('port', 443)
    name = server.get('name', 'Unknown')
    is_local = server.get('local', False)
    enabled = server.get('enabled', True)
    active_for_new = server.get('active_for_new', True)

    status = {
        'name': name,
        'ip': ip,
        'domain': server.get('domain', ip),
        'port': port,
        'is_local': is_local,
        'enabled': enabled,
        'active_for_new': active_for_new,
        'port_open': False,
        'panel_available': False,
        'clients': 0,
        'error': None,
        'inbounds': list(server.get('inbounds', {}).keys())
    }

    if not enabled:
        status['error'] = 'Сервер отключен'
        return status

    # Проверяем порт
    status['port_open'] = await check_port(ip, port)

    # Проверяем панель (если есть)
    if server.get('panel'):
        panel_status = await check_panel_api(server)
        status['panel_available'] = panel_status['available']
        status['clients'] = panel_status['clients']
        if panel_status['error'] and not status['port_open']:
            status['error'] = panel_status['error']
    elif is_local:
        # Для локального сервера считаем клиентов из локальной БД
        try:
            import sqlite3
            conn = sqlite3.connect('/etc/x-ui/x-ui.db')
            cursor = conn.cursor()
            cursor.execute("SELECT settings FROM inbounds")
            rows = cursor.fetchall()
            conn.close()

            total = 0
            for row in rows:
                try:
                    settings = json.loads(row[0])
                    total += len(settings.get('clients', []))
                except:
                    pass
            status['clients'] = total
            status['panel_available'] = True
        except Exception as e:
            status['error'] = str(e)[:50]

    return status


@servers_router.get('/servers', response_class=HTMLResponse)
async def servers_list(request: Request):
    """Страница со списком серверов"""
    config = load_servers_config()
    servers = config.get('servers', [])

    # Получаем статус всех серверов параллельно
    tasks = [get_server_status(s) for s in servers]
    statuses = await asyncio.gather(*tasks, return_exceptions=True)

    # Обрабатываем результаты
    servers_data = []
    for i, status in enumerate(statuses):
        if isinstance(status, Exception):
            servers_data.append({
                'name': servers[i].get('name', 'Unknown'),
                'error': str(status)[:50],
                'enabled': False
            })
        else:
            servers_data.append(status)

    # Считаем общую статистику
    total_clients = sum(s.get('clients', 0) for s in servers_data)
    online_count = sum(1 for s in servers_data if s.get('port_open') or s.get('panel_available'))
    active_count = sum(1 for s in servers_data if s.get('active_for_new'))

    return templates.TemplateResponse('servers.html', {
        'request': request,
        'servers': servers_data,
        'total_clients': total_clients,
        'online_count': online_count,
        'active_count': active_count,
        'total_count': len(servers_data),
        'active': 'servers'
    })


@servers_router.get('/servers/status')
async def servers_status_api(request: Request):
    """API для получения статуса серверов (для обновления без перезагрузки)"""
    config = load_servers_config()
    servers = config.get('servers', [])

    tasks = [get_server_status(s) for s in servers]
    statuses = await asyncio.gather(*tasks, return_exceptions=True)

    results = []
    for i, status in enumerate(statuses):
        if isinstance(status, Exception):
            results.append({
                'name': servers[i].get('name', 'Unknown'),
                'error': str(status)[:50],
                'enabled': False
            })
        else:
            results.append(status)

    return JSONResponse({'servers': results})


@servers_router.post('/servers/{server_name}/toggle')
async def toggle_server(request: Request, server_name: str):
    """Включить/выключить сервер для новых подписок"""
    config = load_servers_config()

    for server in config.get('servers', []):
        if server.get('name') == server_name:
            current = server.get('active_for_new', True)
            server['active_for_new'] = not current
            save_servers_config(config)
            return JSONResponse({
                'success': True,
                'active_for_new': not current
            })

    return JSONResponse({'success': False, 'error': 'Сервер не найден'})


@servers_router.post('/servers/{server_name}/enable')
async def enable_server(request: Request, server_name: str):
    """Включить сервер"""
    config = load_servers_config()

    for server in config.get('servers', []):
        if server.get('name') == server_name:
            server['enabled'] = True
            save_servers_config(config)
            return JSONResponse({'success': True})

    return JSONResponse({'success': False, 'error': 'Сервер не найден'})


@servers_router.post('/servers/{server_name}/disable')
async def disable_server(request: Request, server_name: str):
    """Отключить сервер"""
    config = load_servers_config()

    for server in config.get('servers', []):
        if server.get('name') == server_name:
            server['enabled'] = False
            server['active_for_new'] = False
            save_servers_config(config)
            return JSONResponse({'success': True})

    return JSONResponse({'success': False, 'error': 'Сервер не найден'})


@servers_router.get('/servers/{server_name}/check')
async def check_single_server(request: Request, server_name: str):
    """Проверить статус одного сервера"""
    config = load_servers_config()

    for server in config.get('servers', []):
        if server.get('name') == server_name:
            status = await get_server_status(server)
            return JSONResponse(status)

    return JSONResponse({'error': 'Сервер не найден'}, status_code=404)
