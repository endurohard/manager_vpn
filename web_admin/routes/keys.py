"""
Маршруты для управления ключами (keys_history)
"""
import json
import os
import logging
import aiosqlite
from urllib.parse import quote
from datetime import datetime, timedelta
from fastapi import APIRouter, Request, Query, Form
from fastapi.responses import HTMLResponse, JSONResponse
import uuid as uuid_lib
import asyncio
import ssl
import urllib.request
import urllib.parse
import http.cookiejar
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

keys_router = APIRouter()
templates: Jinja2Templates = None

# Кэш конфигурации серверов
_servers_config = None

# Кэш сессий для API панелей
_panel_sessions = {}


def setup_keys_router(tpl: Jinja2Templates):
    global templates
    templates = tpl


def load_servers_config(force_reload: bool = False):
    """Загрузить конфигурацию серверов"""
    global _servers_config
    if _servers_config is None or force_reload:
        config_path = os.path.join(os.path.dirname(__file__), '..', '..', 'servers_config.json')
        try:
            with open(config_path, 'r') as f:
                _servers_config = json.load(f)
        except:
            _servers_config = {'servers': []}
    return _servers_config


def load_prices():
    """Загрузить цены"""
    prices_path = os.path.join(os.path.dirname(__file__), '..', '..', 'prices.json')
    default_prices = {
        "1_month": {"name": "Месяц", "days": 30, "price": 300},
        "3_months": {"name": "3 месяца", "days": 90, "price": 800},
        "6_months": {"name": "6 месяцев", "days": 180, "price": 1500},
        "1_year": {"name": "Год", "days": 365, "price": 2500}
    }
    try:
        with open(prices_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except:
        return default_prices


def get_panel_opener(server_name: str):
    """Получить opener для панели"""
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


async def panel_login(server_config: dict) -> bool:
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
        return False

    session = get_panel_opener(server_name)
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
            return True
        return False
    except Exception as e:
        logger.error(f"Panel login error for {server_name}: {e}")
        return False


async def create_client_on_server(server_config: dict, client_uuid: str, email: str,
                                   expire_days: int, ip_limit: int = 2, inbound_id: int = None) -> bool:
    """Создать клиента на сервере через API панели"""
    server_name = server_config.get('name', 'Unknown')
    panel = server_config.get('panel', {})

    if not panel:
        logger.warning(f"No panel config for {server_name}")
        return False

    session = get_panel_opener(server_name)
    if not session.get('logged_in'):
        if not await panel_login(server_config):
            return False

    base_url = session.get('base_url', '')
    expire_time = int((datetime.now() + timedelta(days=expire_days)).timestamp() * 1000)

    if inbound_id is None:
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
        response = await loop.run_in_executor(None, session['opener'].open, add_req)
        result = json.loads(response.read())

        if result.get('success'):
            logger.info(f"Client {email} created on {server_name}")
            return True
        else:
            error = result.get('msg', 'Unknown error')
            if 'Duplicate' in error:
                logger.info(f"Client {email} already exists on {server_name}")
                return True
            logger.error(f"Error creating client on {server_name}: {error}")
            return False
    except Exception as e:
        logger.error(f"Error creating client on {server_name}: {e}")
        return False


async def delete_client_on_server(server_config: dict, client_uuid: str) -> bool:
    """Удалить клиента на сервере через API панели"""
    server_name = server_config.get('name', 'Unknown')
    panel = server_config.get('panel', {})

    if not panel:
        return False

    session = get_panel_opener(server_name)
    if not session.get('logged_in'):
        if not await panel_login(server_config):
            return False

    base_url = session.get('base_url', '')

    # Получаем все inbound'ы
    inbounds = server_config.get('inbounds', {})
    deleted = False

    for inbound_name, inbound in inbounds.items():
        inbound_id = inbound.get('id', 1)
        try:
            del_url = f"{base_url}/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}"
            del_req = urllib.request.Request(del_url, method='POST')

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, session['opener'].open, del_req)
            result = json.loads(response.read())

            if result.get('success'):
                deleted = True
                logger.info(f"Client {client_uuid[:8]}... deleted from {server_name}/{inbound_name}")
        except Exception as e:
            logger.error(f"Error deleting from {server_name}/{inbound_name}: {e}")

    return deleted


async def extend_client_on_server(server_config: dict, client_uuid: str,
                                   new_expire_time: int, inbound_id: int = None) -> bool:
    """Продлить ключ клиента на сервере"""
    server_name = server_config.get('name', 'Unknown')
    panel = server_config.get('panel', {})

    if not panel:
        return False

    session = get_panel_opener(server_name)
    if not session.get('logged_in'):
        if not await panel_login(server_config):
            return False

    base_url = session.get('base_url', '')

    # Получаем текущие данные клиента
    inbounds = server_config.get('inbounds', {})

    for inbound_name, inbound_cfg in inbounds.items():
        inbound_id = inbound_cfg.get('id', 1)
        try:
            # Получаем список клиентов
            list_url = f"{base_url}/panel/api/inbounds/get/{inbound_id}"
            list_req = urllib.request.Request(list_url)

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, session['opener'].open, list_req)
            data = json.loads(response.read())

            if not data.get('success'):
                continue

            inbound_obj = data.get('obj', {})
            settings = json.loads(inbound_obj.get('settings', '{}'))
            clients = settings.get('clients', [])

            # Находим клиента
            for client in clients:
                if client.get('id') == client_uuid:
                    # Обновляем время
                    client['expiryTime'] = new_expire_time

                    # Отправляем обновление
                    update_url = f"{base_url}/panel/api/inbounds/updateClient/{client_uuid}"
                    update_payload = urllib.parse.urlencode({
                        'id': inbound_id,
                        'settings': json.dumps({'clients': [client]})
                    }).encode()

                    update_req = urllib.request.Request(update_url, data=update_payload, method='POST')
                    update_req.add_header('Content-Type', 'application/x-www-form-urlencoded')

                    resp = await loop.run_in_executor(None, session['opener'].open, update_req)
                    result = json.loads(resp.read())

                    if result.get('success'):
                        logger.info(f"Extended client {client_uuid[:8]}... on {server_name}")
                        return True

        except Exception as e:
            logger.error(f"Error extending on {server_name}: {e}")

    return False


def generate_vless_url(uuid: str, server: dict, inbound_name: str = 'main') -> str:
    """Генерация VLESS URL для клиента"""
    inbound = server.get('inbounds', {}).get(inbound_name, {})
    if not inbound:
        return None

    domain = server.get('domain', server.get('ip', ''))
    port = server.get('port', 443)

    security = inbound.get('security', 'reality')
    sni = inbound.get('sni', '')
    pbk = inbound.get('pbk', '')
    sid = inbound.get('sid', '')
    fp = inbound.get('fp', 'chrome')
    flow = inbound.get('flow', '')
    network = inbound.get('network', 'tcp')
    name_prefix = inbound.get('name_prefix', server.get('name', 'VPN'))

    # Базовый URL
    url = f"vless://{uuid}@{domain}:{port}?"

    # Параметры
    params = [
        f"type={network}",
        f"security={security}",
    ]

    if security == 'reality':
        params.extend([
            f"pbk={pbk}",
            f"fp={fp}",
            f"sni={sni}",
            f"sid={sid}",
            "spx=%2F"
        ])

    if flow:
        params.append(f"flow={flow}")

    if network == 'grpc':
        params.append("serviceName=")
        params.append("mode=gun")

    url += "&".join(params)
    url += f"#{quote(name_prefix)}"

    return url


@keys_router.get('/keys', response_class=HTMLResponse)
async def keys_list(
    request: Request,
    page: int = Query(1, ge=1),
    search: str = Query(''),
    sort: str = Query('newest')
):
    """Список ключей из keys_history"""
    db_path = request.app.state.db_path
    limit = 25
    offset = (page - 1) * limit

    # Валидация sort
    if sort not in ('newest', 'expiring'):
        sort = 'newest'

    base_query = '''
        SELECT kh.*, m.full_name as manager_name, m.custom_name
        FROM keys_history kh
        LEFT JOIN managers m ON kh.manager_id = m.user_id
    '''
    count_query = 'SELECT COUNT(*) FROM keys_history'
    params = []

    if search:
        search_pattern = f'%{search}%'
        where_clause = '''
            WHERE client_email LIKE ? OR phone_number LIKE ? OR client_id LIKE ?
        '''
        base_query += where_clause
        count_query += where_clause
        params = [search_pattern, search_pattern, search_pattern]

    if sort == 'expiring':
        base_query += " ORDER BY DATE(kh.created_at, '+' || kh.expire_days || ' days') ASC LIMIT ? OFFSET ?"
    else:
        base_query += ' ORDER BY kh.id DESC LIMIT ? OFFSET ?'

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute(base_query, params + [limit, offset])
        keys = [dict(row) for row in await cursor.fetchall()]

        cursor = await db.execute(count_query, params)
        total = (await cursor.fetchone())[0]

    total_pages = (total + limit - 1) // limit

    # Пагинация
    def build_pages(current, total_p):
        pages = []
        for p in range(1, total_p + 1):
            if p == 1 or p == total_p or abs(p - current) <= 2:
                if pages and pages[-1] != '...' and p - (pages[-1] if isinstance(pages[-1], int) else 0) > 1:
                    pages.append('...')
                pages.append(p)
        return pages

    pages = build_pages(page, total_pages) if total_pages > 0 else []

    # Форматируем данные
    for key in keys:
        if key.get('expire_days') and key.get('created_at'):
            try:
                created = datetime.fromisoformat(key['created_at'].replace('Z', '+00:00'))
                from datetime import timedelta
                expire_dt = created + timedelta(days=key['expire_days'])
                now = datetime.now()
                if expire_dt.tzinfo:
                    now = now.replace(tzinfo=expire_dt.tzinfo)
                days_left = (expire_dt - now).days
                if days_left < 0:
                    key['status'] = 'expired'
                    key['days_left'] = f'Истёк {abs(days_left)} дн. назад'
                else:
                    key['status'] = 'active'
                    key['days_left'] = f'{days_left} дн.'
            except:
                key['status'] = 'unknown'
                key['days_left'] = '?'
        else:
            key['status'] = 'unknown'
            key['days_left'] = '?'

        # Имя менеджера
        key['manager_display'] = key.get('custom_name') or key.get('manager_name') or f"ID: {key.get('manager_id', '?')}"

    return templates.TemplateResponse('keys.html', {
        'request': request,
        'keys': keys,
        'page': page,
        'total_pages': total_pages,
        'total': total,
        'search': search,
        'sort': sort,
        'pages': pages,
        'active': 'keys'
    })


@keys_router.get('/keys/search/api')
async def keys_search_api(
    request: Request,
    q: str = Query('', min_length=1)
):
    """API для поиска ключей"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        search_pattern = f'%{q}%'
        cursor = await db.execute('''
            SELECT id, client_email, phone_number, client_id, expire_days, created_at
            FROM keys_history
            WHERE client_email LIKE ? OR phone_number LIKE ? OR client_id LIKE ?
            ORDER BY id DESC
            LIMIT 15
        ''', (search_pattern, search_pattern, search_pattern))

        results = [dict(row) for row in await cursor.fetchall()]

    return JSONResponse({'results': results})


@keys_router.get('/keys/{key_id}', response_class=HTMLResponse)
async def key_detail(request: Request, key_id: int):
    """Детали ключа с VLESS URLs"""
    db_path = request.app.state.db_path

    linked_keys = []
    is_linked_to = None
    master_key_info = None

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute('''
            SELECT kh.*, m.full_name as manager_name, m.custom_name
            FROM keys_history kh
            LEFT JOIN managers m ON kh.manager_id = m.user_id
            WHERE kh.id = ?
        ''', (key_id,))
        key = await cursor.fetchone()

        if not key:
            return HTMLResponse("Ключ не найден", status_code=404)

        key = dict(key)
        master_uuid = key.get('client_id', '')

        # Получаем связанные ключи
        cursor = await db.execute('''
            SELECT lc.id as link_id, lc.linked_uuid, lc.linked_at,
                   kh.id as key_id, kh.client_email, kh.phone_number,
                   kh.expire_days, kh.created_at
            FROM linked_clients lc
            LEFT JOIN keys_history kh ON lc.linked_uuid = kh.client_id
            WHERE lc.master_uuid = ?
            ORDER BY lc.linked_at DESC
        ''', (master_uuid,))
        linked_keys = [dict(row) for row in await cursor.fetchall()]

        # Проверяем, является ли текущий ключ linked к другому master
        cursor = await db.execute(
            'SELECT master_uuid FROM linked_clients WHERE linked_uuid = ?',
            (master_uuid,)
        )
        is_linked_row = await cursor.fetchone()
        if is_linked_row:
            is_linked_to = is_linked_row['master_uuid']
            # Получаем информацию о master ключе
            cursor = await db.execute('''
                SELECT id, client_email, phone_number FROM keys_history WHERE client_id = ?
            ''', (is_linked_to,))
            master_row = await cursor.fetchone()
            if master_row:
                master_key_info = dict(master_row)

    # Генерируем VLESS URLs для всех серверов
    config = load_servers_config()
    vless_keys = []

    uuid = key.get('client_id', '')
    if uuid:
        for server in config.get('servers', []):
            if not server.get('enabled', True):
                continue

            server_name = server.get('name', 'Unknown')

            for inbound_name, inbound in server.get('inbounds', {}).items():
                vless_url = generate_vless_url(uuid, server, inbound_name)
                if vless_url:
                    vless_keys.append({
                        'server': server_name,
                        'inbound': inbound.get('name_prefix', inbound_name),
                        'url': vless_url
                    })

    # Статус ключа
    if key.get('expire_days') and key.get('created_at'):
        try:
            created = datetime.fromisoformat(key['created_at'].replace('Z', '+00:00'))
            from datetime import timedelta
            expire_dt = created + timedelta(days=key['expire_days'])
            now = datetime.now()
            if expire_dt.tzinfo:
                now = now.replace(tzinfo=expire_dt.tzinfo)
            days_left = (expire_dt - now).days
            if days_left < 0:
                key['status'] = 'expired'
                key['days_left'] = f'Истёк {abs(days_left)} дн. назад'
            else:
                key['status'] = 'active'
                key['days_left'] = f'{days_left} дн. осталось'
        except:
            key['status'] = 'unknown'
            key['days_left'] = '?'
    else:
        key['status'] = 'unknown'
        key['days_left'] = '?'

    key['manager_display'] = key.get('custom_name') or key.get('manager_name') or f"ID: {key.get('manager_id', '?')}"

    # Загружаем цены для продления
    prices = load_prices()

    return templates.TemplateResponse('key_detail.html', {
        'request': request,
        'key': key,
        'vless_keys': vless_keys,
        'prices': prices,
        'linked_keys': linked_keys,
        'is_linked_to': is_linked_to,
        'master_key_info': master_key_info,
        'active': 'keys'
    })


# ============ API для создания ключа ============

@keys_router.get('/keys/create', response_class=HTMLResponse)
async def create_key_page(request: Request):
    """Страница создания ключа"""
    config = load_servers_config(force_reload=True)
    servers = [s for s in config.get('servers', []) if s.get('enabled', True)]
    prices = load_prices()

    return templates.TemplateResponse('key_create.html', {
        'request': request,
        'servers': servers,
        'prices': prices,
        'active': 'keys'
    })


@keys_router.post('/keys/create')
async def create_key_api(
    request: Request,
    client_name: str = Form(...),
    server_name: str = Form(...),
    inbound_name: str = Form('main'),
    period: str = Form(...)
):
    """API создания ключа"""
    db_path = request.app.state.db_path
    config = load_servers_config()
    prices = load_prices()

    # Находим сервер
    server = None
    for s in config.get('servers', []):
        if s.get('name') == server_name:
            server = s
            break

    if not server:
        return JSONResponse({'success': False, 'error': 'Сервер не найден'})

    # Получаем период
    period_data = prices.get(period)
    if not period_data:
        return JSONResponse({'success': False, 'error': 'Неверный период'})

    expire_days = period_data['days']
    price = period_data['price']

    # Генерируем UUID
    client_uuid = str(uuid_lib.uuid4())

    # Получаем inbound_id
    inbounds = server.get('inbounds', {})
    inbound = inbounds.get(inbound_name, inbounds.get('main', {}))
    inbound_id = inbound.get('id', 1)

    # Создаём клиента на сервере
    success = await create_client_on_server(
        server, client_uuid, client_name, expire_days, ip_limit=2, inbound_id=inbound_id
    )

    if not success:
        return JSONResponse({'success': False, 'error': 'Ошибка создания на сервере'})

    # Сохраняем в БД
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute('''
                INSERT INTO keys_history (client_id, client_email, phone_number, expire_days, price, period, manager_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                client_uuid,
                client_name,
                '',
                expire_days,
                price,
                period_data['name'],
                0,  # admin
                datetime.now().isoformat()
            ))
            await db.commit()

            # Получаем ID созданной записи
            cursor = await db.execute('SELECT last_insert_rowid()')
            row = await cursor.fetchone()
            key_id = row[0] if row else None

    except Exception as e:
        logger.error(f"DB error: {e}")
        return JSONResponse({'success': False, 'error': f'Ошибка БД: {e}'})

    # Генерируем VLESS URL
    vless_url = generate_vless_url(client_uuid, server, inbound_name)

    return JSONResponse({
        'success': True,
        'key_id': key_id,
        'client_id': client_uuid,
        'vless_url': vless_url,
        'server': server_name,
        'period': period_data['name']
    })


# ============ API для удаления ключа ============

@keys_router.delete('/keys/{key_id}')
async def delete_key_api(request: Request, key_id: int):
    """Удалить ключ со всех серверов"""
    db_path = request.app.state.db_path

    # Получаем данные ключа
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT client_id, client_email FROM keys_history WHERE id = ?', (key_id,))
        key = await cursor.fetchone()

        if not key:
            return JSONResponse({'success': False, 'error': 'Ключ не найден'})

        client_uuid = key['client_id']
        client_email = key['client_email']

    # Удаляем со всех серверов
    config = load_servers_config()
    results = {}

    for server in config.get('servers', []):
        if not server.get('enabled', True):
            continue
        if server.get('local', False):
            continue

        server_name = server.get('name', 'Unknown')
        deleted = await delete_client_on_server(server, client_uuid)
        results[server_name] = deleted

    # Удаляем из БД
    async with aiosqlite.connect(db_path) as db:
        await db.execute('DELETE FROM keys_history WHERE id = ?', (key_id,))
        await db.commit()

    return JSONResponse({
        'success': True,
        'deleted_from': results,
        'message': f'Ключ {client_email or client_uuid[:8]} удалён'
    })


# ============ API для продления ключа ============

@keys_router.post('/keys/{key_id}/extend')
async def extend_key_api(
    request: Request,
    key_id: int,
    period: str = Form(...)
):
    """Продлить ключ"""
    db_path = request.app.state.db_path
    prices = load_prices()

    period_data = prices.get(period)
    if not period_data:
        return JSONResponse({'success': False, 'error': 'Неверный период'})

    extend_days = period_data['days']

    # Получаем данные ключа
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT client_id, client_email, expire_days, created_at
            FROM keys_history WHERE id = ?
        ''', (key_id,))
        key = await cursor.fetchone()

        if not key:
            return JSONResponse({'success': False, 'error': 'Ключ не найден'})

        client_uuid = key['client_id']
        old_expire_days = key['expire_days'] or 0
        created_at = key['created_at']

    # Вычисляем новое время истечения
    try:
        created = datetime.fromisoformat(created_at.replace('Z', '+00:00'))
    except:
        created = datetime.now()

    # Новый срок = текущий срок + продление
    new_expire_days = old_expire_days + extend_days
    new_expire_dt = created + timedelta(days=new_expire_days)
    new_expire_time = int(new_expire_dt.timestamp() * 1000)

    # Продлеваем на всех серверах
    config = load_servers_config()
    results = {}

    for server in config.get('servers', []):
        if not server.get('enabled', True):
            continue
        if server.get('local', False):
            continue

        server_name = server.get('name', 'Unknown')
        extended = await extend_client_on_server(server, client_uuid, new_expire_time)
        results[server_name] = extended

    # Обновляем в БД
    async with aiosqlite.connect(db_path) as db:
        await db.execute('''
            UPDATE keys_history
            SET expire_days = ?, price = COALESCE(price, 0) + ?
            WHERE id = ?
        ''', (new_expire_days, period_data['price'], key_id))
        await db.commit()

    return JSONResponse({
        'success': True,
        'new_expire_days': new_expire_days,
        'extended_on': results,
        'message': f'Ключ продлён на {extend_days} дней'
    })


# ============ API для получения серверов и inbound'ов ============

@keys_router.get('/api/servers')
async def get_servers_api():
    """Получить список серверов с inbound'ами"""
    config = load_servers_config(force_reload=True)
    servers = []

    for s in config.get('servers', []):
        if not s.get('enabled', True):
            continue

        inbounds = []
        for name, inbound in s.get('inbounds', {}).items():
            inbounds.append({
                'key': name,
                'name': inbound.get('name_prefix', name),
                'id': inbound.get('id', 1)
            })

        servers.append({
            'name': s.get('name', 'Unknown'),
            'active_for_new': s.get('active_for_new', True),
            'inbounds': inbounds
        })

    return JSONResponse({'servers': servers})


@keys_router.get('/api/prices')
async def get_prices_api():
    """Получить цены"""
    prices = load_prices()
    return JSONResponse({'prices': prices})


# ============ API для связанных ключей (linked clients) ============

@keys_router.get('/keys/{key_id}/linked')
async def get_linked_keys(request: Request, key_id: int):
    """Получить список связанных ключей"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Получаем UUID главного ключа
        cursor = await db.execute('SELECT client_id FROM keys_history WHERE id = ?', (key_id,))
        key = await cursor.fetchone()

        if not key:
            return JSONResponse({'success': False, 'error': 'Ключ не найден'}, status_code=404)

        master_uuid = key['client_id']

        # Получаем связанные ключи с информацией
        cursor = await db.execute('''
            SELECT lc.id as link_id, lc.linked_uuid, lc.linked_at,
                   kh.id as key_id, kh.client_email, kh.phone_number,
                   kh.expire_days, kh.created_at
            FROM linked_clients lc
            LEFT JOIN keys_history kh ON lc.linked_uuid = kh.client_id
            WHERE lc.master_uuid = ?
            ORDER BY lc.linked_at DESC
        ''', (master_uuid,))
        linked = [dict(row) for row in await cursor.fetchall()]

        # Проверяем, является ли текущий ключ linked к другому master
        cursor = await db.execute(
            'SELECT master_uuid FROM linked_clients WHERE linked_uuid = ?',
            (master_uuid,)
        )
        is_linked_row = await cursor.fetchone()
        is_linked_to = is_linked_row['master_uuid'] if is_linked_row else None

    return JSONResponse({
        'success': True,
        'master_uuid': master_uuid,
        'linked': linked,
        'is_linked_to': is_linked_to,
        'count': len(linked)
    })


@keys_router.post('/keys/{key_id}/link')
async def link_key(
    request: Request,
    key_id: int,
    linked_uuid: str = Form(None),
    linked_key_id: int = Form(None)
):
    """Привязать ключ к главному ключу"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Получаем UUID главного ключа
        cursor = await db.execute('SELECT client_id FROM keys_history WHERE id = ?', (key_id,))
        master_key = await cursor.fetchone()

        if not master_key:
            return JSONResponse({'success': False, 'error': 'Главный ключ не найден'}, status_code=404)

        master_uuid = master_key['client_id']

        # Если передан linked_key_id, получаем UUID из него
        if linked_key_id:
            cursor = await db.execute('SELECT client_id FROM keys_history WHERE id = ?', (linked_key_id,))
            linked_key = await cursor.fetchone()
            if not linked_key:
                return JSONResponse({'success': False, 'error': 'Связываемый ключ не найден'}, status_code=404)
            linked_uuid = linked_key['client_id']
        elif not linked_uuid:
            return JSONResponse({'success': False, 'error': 'Укажите UUID или ID ключа для связи'}, status_code=400)

        # Проверяем, что не пытаемся связать ключ сам с собой
        if master_uuid == linked_uuid:
            return JSONResponse({'success': False, 'error': 'Нельзя связать ключ сам с собой'}, status_code=400)

        # Проверяем, что linked_uuid не является master'ом для других ключей
        cursor = await db.execute(
            'SELECT COUNT(*) FROM linked_clients WHERE master_uuid = ?',
            (linked_uuid,)
        )
        if (await cursor.fetchone())[0] > 0:
            return JSONResponse({
                'success': False,
                'error': 'Этот ключ уже является главным для других ключей. Сначала отвяжите его связи.'
            }, status_code=400)

        # Проверяем, что linked_uuid не привязан к другому master
        cursor = await db.execute(
            'SELECT master_uuid FROM linked_clients WHERE linked_uuid = ?',
            (linked_uuid,)
        )
        existing_master = await cursor.fetchone()
        if existing_master:
            return JSONResponse({
                'success': False,
                'error': f'Этот ключ уже привязан к другому ключу'
            }, status_code=400)

        # Добавляем связь
        try:
            await db.execute(
                'INSERT INTO linked_clients (master_uuid, linked_uuid) VALUES (?, ?)',
                (master_uuid, linked_uuid)
            )
            await db.commit()
        except Exception as e:
            logger.error(f"Error linking keys: {e}")
            return JSONResponse({'success': False, 'error': 'Ошибка при создании связи'}, status_code=500)

    return JSONResponse({
        'success': True,
        'message': f'Ключ {linked_uuid[:8]}... привязан к {master_uuid[:8]}...'
    })


@keys_router.delete('/keys/{key_id}/unlink/{link_id}')
async def unlink_key(request: Request, key_id: int, link_id: int):
    """Отвязать ключ от главного ключа"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Получаем UUID главного ключа
        cursor = await db.execute('SELECT client_id FROM keys_history WHERE id = ?', (key_id,))
        master_key = await cursor.fetchone()

        if not master_key:
            return JSONResponse({'success': False, 'error': 'Главный ключ не найден'}, status_code=404)

        master_uuid = master_key['client_id']

        # Получаем информацию о связи
        cursor = await db.execute(
            'SELECT linked_uuid FROM linked_clients WHERE id = ? AND master_uuid = ?',
            (link_id, master_uuid)
        )
        link = await cursor.fetchone()

        if not link:
            return JSONResponse({'success': False, 'error': 'Связь не найдена'}, status_code=404)

        linked_uuid = link['linked_uuid']

        # Удаляем связь
        await db.execute('DELETE FROM linked_clients WHERE id = ?', (link_id,))
        await db.commit()

    return JSONResponse({
        'success': True,
        'message': f'Ключ {linked_uuid[:8]}... отвязан'
    })


@keys_router.get('/keys/search/uuid')
async def search_keys_by_uuid(
    request: Request,
    q: str = Query('', min_length=3),
    exclude_id: int = Query(None)
):
    """Поиск ключей по UUID или email для связывания"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        search_pattern = f'%{q}%'
        query = '''
            SELECT id, client_id, client_email, phone_number, expire_days, created_at
            FROM keys_history
            WHERE (client_id LIKE ? OR client_email LIKE ? OR phone_number LIKE ?)
        '''
        params = [search_pattern, search_pattern, search_pattern]

        if exclude_id:
            query += ' AND id != ?'
            params.append(exclude_id)

        query += ' ORDER BY id DESC LIMIT 10'

        cursor = await db.execute(query, params)
        results = [dict(row) for row in await cursor.fetchall()]

    return JSONResponse({'results': results})
