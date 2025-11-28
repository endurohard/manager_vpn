"""
Веб-сервер для Telegram Mini App с функцией заказа ключей
"""
import os
import json
import logging
import uuid
import aiosqlite
from datetime import datetime
from pathlib import Path
from aiohttp import web
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton

logger = logging.getLogger(__name__)

# Путь к директории webapp
WEBAPP_DIR = Path(__file__).parent
STATIC_DIR = WEBAPP_DIR / 'static'
TEMPLATES_DIR = WEBAPP_DIR / 'templates'
BASE_DIR = Path(__file__).parent.parent.parent

# Файлы данных
PRICES_FILE = BASE_DIR / 'prices.json'
PAYMENT_FILE = BASE_DIR / 'payment_details.json'
ORDERS_DB = BASE_DIR / 'web_orders.db'
UPLOADS_DIR = BASE_DIR / 'uploads'

# Создаём директорию для загрузок
UPLOADS_DIR.mkdir(exist_ok=True)

# Глобальная ссылка на бота для уведомлений
bot_instance = None
admin_id = None


def set_bot_instance(bot, admin):
    """Установить экземпляр бота для уведомлений"""
    global bot_instance, admin_id
    bot_instance = bot
    admin_id = admin


async def init_orders_db():
    """Инициализация базы данных заказов"""
    async with aiosqlite.connect(ORDERS_DB) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS web_orders (
                id TEXT PRIMARY KEY,
                tariff_id TEXT NOT NULL,
                tariff_name TEXT NOT NULL,
                price INTEGER NOT NULL,
                days INTEGER NOT NULL,
                contact TEXT NOT NULL,
                contact_type TEXT DEFAULT 'telegram',
                status TEXT DEFAULT 'pending',
                payment_proof TEXT,
                vless_key TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                confirmed_at TIMESTAMP,
                admin_comment TEXT
            )
        ''')
        await db.commit()
    logger.info("Web orders database initialized")


def load_prices():
    """Загрузить тарифы"""
    if PRICES_FILE.exists():
        with open(PRICES_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def load_payment_details():
    """Загрузить реквизиты оплаты"""
    if PAYMENT_FILE.exists():
        with open(PAYMENT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"active": False}


def save_payment_details(data):
    """Сохранить реквизиты оплаты"""
    with open(PAYMENT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


async def index_handler(request):
    """Обработчик главной страницы"""
    index_file = TEMPLATES_DIR / 'index.html'

    if not index_file.exists():
        logger.error(f"Index file not found: {index_file}")
        return web.Response(text="Mini App not found", status=404)

    with open(index_file, 'r', encoding='utf-8') as f:
        html_content = f.read()

    return web.Response(text=html_content, content_type='text/html')


async def api_tariffs(request):
    """API: Получить список тарифов"""
    prices = load_prices()
    tariffs = []
    for key, value in prices.items():
        tariffs.append({
            "id": key,
            "name": value["name"],
            "days": value["days"],
            "price": value["price"]
        })
    return web.json_response({"tariffs": tariffs})


async def api_payment_details(request):
    """API: Получить реквизиты оплаты"""
    details = load_payment_details()
    if not details.get("active", False):
        return web.json_response({"error": "Оплата временно недоступна"}, status=503)

    # Не отправляем флаг active клиенту
    safe_details = {k: v for k, v in details.items() if k != "active"}
    return web.json_response(safe_details)


async def api_create_order(request):
    """API: Создать заказ"""
    try:
        data = await request.json()
    except:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    tariff_id = data.get("tariff_id")
    contact = data.get("contact", "").strip()
    contact_type = data.get("contact_type", "telegram")

    if not tariff_id or not contact:
        return web.json_response({"error": "Укажите тариф и контакт"}, status=400)

    prices = load_prices()
    if tariff_id not in prices:
        return web.json_response({"error": "Неверный тариф"}, status=400)

    tariff = prices[tariff_id]
    order_id = str(uuid.uuid4())[:8].upper()

    async with aiosqlite.connect(ORDERS_DB) as db:
        await db.execute('''
            INSERT INTO web_orders (id, tariff_id, tariff_name, price, days, contact, contact_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (order_id, tariff_id, tariff["name"], tariff["price"], tariff["days"], contact, contact_type))
        await db.commit()

    # Получаем реквизиты
    payment = load_payment_details()

    return web.json_response({
        "order_id": order_id,
        "tariff": tariff["name"],
        "price": tariff["price"],
        "days": tariff["days"],
        "payment": {k: v for k, v in payment.items() if k != "active"}
    })


async def api_confirm_payment(request):
    """API: Подтвердить оплату (с возможностью загрузки файла)"""
    order_id = None
    payment_info = ""
    file_path = None

    # Проверяем тип контента
    content_type = request.content_type

    if 'multipart/form-data' in content_type:
        # Обработка multipart формы с файлом
        reader = await request.multipart()
        async for field in reader:
            if field.name == 'order_id':
                order_id = (await field.read()).decode('utf-8').strip().upper()
            elif field.name == 'payment_info':
                payment_info = (await field.read()).decode('utf-8').strip()
            elif field.name == 'payment_proof':
                # Сохраняем файл
                if field.filename:
                    # Генерируем уникальное имя файла
                    ext = Path(field.filename).suffix.lower()
                    if ext not in ['.jpg', '.jpeg', '.png', '.gif', '.pdf', '.webp']:
                        return web.json_response({"error": "Поддерживаются только изображения и PDF"}, status=400)

                    filename = f"{uuid.uuid4().hex}{ext}"
                    file_path = UPLOADS_DIR / filename

                    # Сохраняем файл
                    size = 0
                    with open(file_path, 'wb') as f:
                        while True:
                            chunk = await field.read_chunk()
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > 10 * 1024 * 1024:  # Лимит 10MB
                                f.close()
                                file_path.unlink(missing_ok=True)
                                return web.json_response({"error": "Файл слишком большой (макс. 10MB)"}, status=400)
                            f.write(chunk)
    else:
        # Обработка JSON
        try:
            data = await request.json()
            order_id = data.get("order_id", "").strip().upper()
            payment_info = data.get("payment_info", "").strip()
        except:
            return web.json_response({"error": "Invalid data"}, status=400)

    if not order_id:
        return web.json_response({"error": "Укажите номер заказа"}, status=400)

    async with aiosqlite.connect(ORDERS_DB) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM web_orders WHERE id = ?', (order_id,))
        order = await cursor.fetchone()

        if not order:
            if file_path:
                file_path.unlink(missing_ok=True)
            return web.json_response({"error": "Заказ не найден"}, status=404)

        if order["status"] != "pending":
            if file_path:
                file_path.unlink(missing_ok=True)
            return web.json_response({"error": "Заказ уже обработан"}, status=400)

        # Сохраняем путь к файлу если есть
        proof_info = str(file_path) if file_path else payment_info
        await db.execute('''
            UPDATE web_orders SET status = 'paid', payment_proof = ? WHERE id = ?
        ''', (proof_info, order_id))
        await db.commit()

        order_dict = dict(order)

    # Отправляем уведомление админу
    if bot_instance and admin_id:
        try:
            message = (
                f"💰 <b>Новая оплата с сайта!</b>\n\n"
                f"🆔 Заказ: <code>{order_id}</code>\n"
                f"📦 Тариф: {order_dict['tariff_name']}\n"
                f"💵 Сумма: {order_dict['price']}₽\n"
                f"📅 Дней: {order_dict['days']}\n"
                f"📱 Контакт: {order_dict['contact']}\n"
            )

            if file_path:
                message += f"📎 Скриншот оплаты: прикреплён\n\n"
            elif payment_info:
                message += f"💳 Инфо об оплате: {payment_info}\n\n"
            else:
                message += f"💳 Инфо об оплате: не указано\n\n"

            # Кнопки подтверждения/отказа
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"web_approve_{order_id}"),
                    InlineKeyboardButton(text="❌ Отказать", callback_data=f"web_reject_{order_id}")
                ]
            ])

            # Отправляем сообщение с файлом или без
            if file_path and file_path.exists():
                document = FSInputFile(file_path)
                if file_path.suffix.lower() in ['.jpg', '.jpeg', '.png', '.gif', '.webp']:
                    await bot_instance.send_photo(admin_id, document, caption=message, parse_mode='HTML', reply_markup=keyboard)
                else:
                    await bot_instance.send_document(admin_id, document, caption=message, parse_mode='HTML', reply_markup=keyboard)
            else:
                await bot_instance.send_message(admin_id, message, parse_mode='HTML', reply_markup=keyboard)
        except Exception as e:
            logger.error(f"Failed to send admin notification: {e}")

    return web.json_response({
        "success": True,
        "message": "Оплата отправлена на проверку. Ожидайте ключ!"
    })


def load_xray_config():
    """Загрузить конфиг xray"""
    xray_config_path = Path('/usr/local/x-ui/bin/config.json')
    if xray_config_path.exists():
        with open(xray_config_path, 'r') as f:
            return json.load(f)
    return None


def find_client_in_xray(uuid_str):
    """Найти клиента по UUID в конфиге xray"""
    config = load_xray_config()
    if not config:
        return None, None

    for inbound in config.get('inbounds', []):
        settings = inbound.get('settings') or {}
        clients = settings.get('clients') or []

        for client in clients:
            if client.get('id') == uuid_str:
                return client, inbound

    return None, None


def generate_public_key(private_key):
    """Сгенерировать публичный ключ из приватного"""
    import subprocess
    try:
        result = subprocess.run(
            ['/usr/local/x-ui/bin/xray-linux-amd64', 'x25519', '-i', private_key],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.split('\n'):
            if 'Password:' in line:
                return line.split(':', 1)[1].strip()
    except:
        pass
    return None


async def api_fix_key(request):
    """API: Проверить и исправить VLESS ключ по базе xray"""
    try:
        data = await request.json()
    except:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    vless_link = data.get("key", data.get("vless_link", "")).strip()

    if not vless_link:
        return web.json_response({"error": "Укажите VLESS ключ"}, status=400)

    if not vless_link.startswith("vless://"):
        return web.json_response({"error": "Неверный формат ключа. Должен начинаться с vless://"}, status=400)

    try:
        # Парсим ссылку
        link_without_proto = vless_link[8:]

        if '#' in link_without_proto:
            main_part, fragment = link_without_proto.rsplit('#', 1)
        else:
            main_part, fragment = link_without_proto, ""

        if '?' in main_part:
            address_part, query_string = main_part.split('?', 1)
        else:
            address_part, query_string = main_part, ""

        if '@' not in address_part:
            return web.json_response({"error": "Неверный формат: отсутствует UUID"}, status=400)

        uuid_part, host_port = address_part.rsplit('@', 1)

        if ':' in host_port:
            host, port = host_port.rsplit(':', 1)
        else:
            host, port = host_port, "443"

        # Парсим параметры
        params = {}
        if query_string:
            for param in query_string.split('&'):
                if '=' in param:
                    key, value = param.split('=', 1)
                    params[key] = value

        issues = []
        fixes = []
        client_info = None

        # Ищем клиента в базе xray
        client, inbound = find_client_in_xray(uuid_part)

        if client and inbound:
            client_info = {
                "email": client.get('email', 'N/A'),
                "inbound": inbound.get('tag', 'N/A'),
                "port": inbound.get('port', 'N/A')
            }

            stream = inbound.get('streamSettings') or {}
            reality = stream.get('realitySettings') or {}
            security = stream.get('security', 'none')

            # Исправляем security
            if params.get('security') != security:
                old_sec = params.get('security', 'none')
                params['security'] = security
                fixes.append(f"Исправлен security: {old_sec} → {security}")

            if security == 'reality':
                # Исправляем flow из настроек клиента
                client_flow = client.get('flow', '')
                if client_flow:
                    if params.get('flow') != client_flow:
                        old_flow = params.get('flow', 'отсутствует')
                        params['flow'] = client_flow
                        fixes.append(f"Исправлен flow: {old_flow} → {client_flow}")

                # Исправляем SNI
                server_names = reality.get('serverNames', [])
                current_sni = params.get('sni', '')
                if server_names and current_sni not in server_names:
                    params['sni'] = server_names[0]
                    fixes.append(f"Исправлен SNI: {current_sni} → {server_names[0]}")

                # Исправляем public key
                private_key = reality.get('privateKey', '')
                if private_key:
                    public_key = generate_public_key(private_key)
                    if public_key and params.get('pbk') != public_key:
                        old_pbk = params.get('pbk', 'отсутствует')[:10] + '...'
                        params['pbk'] = public_key
                        fixes.append(f"Исправлен pbk: {old_pbk} → {public_key[:10]}...")

                # Исправляем short id
                short_ids = reality.get('shortIds', [])
                current_sid = params.get('sid', '')
                if short_ids and current_sid not in short_ids:
                    params['sid'] = short_ids[0]
                    fixes.append(f"Исправлен sid: {current_sid} → {short_ids[0]}")

                # Исправляем fingerprint
                if params.get('fp') == 'random':
                    params['fp'] = 'chrome'
                    fixes.append("Исправлен fp: random → chrome")

            # Исправляем порт на 443
            if port != "443":
                old_port = port
                port = "443"
                fixes.append(f"Исправлен порт: {old_port} → 443")

            # Исправляем хост
            if host not in ['raphaelvpn.ru', 'zov-gor.ru', 'peakvip.ru']:
                old_host = host
                host = 'raphaelvpn.ru'
                fixes.append(f"Исправлен хост: {old_host} → raphaelvpn.ru")

        else:
            issues.append("UUID не найден в базе xray! Ключ может быть недействительным.")
            # Базовые исправления без базы
            if params.get('security') == 'reality':
                if 'flow' not in params:
                    params['flow'] = 'xtls-rprx-vision'
                    fixes.append("Добавлен flow=xtls-rprx-vision (стандартный)")
                if params.get('fp') == 'random':
                    params['fp'] = 'chrome'
                    fixes.append("Исправлен fp: random → chrome")

        # Собираем исправленную ссылку
        new_query = '&'.join([f"{k}={v}" for k, v in params.items()])
        fixed_link = f"vless://{uuid_part}@{host}:{port}?{new_query}"
        if fragment:
            fixed_link += f"#{fragment}"

        result = {
            "original": vless_link,
            "fixed": fixed_link,
            "changed": vless_link != fixed_link,
            "fixes": fixes,
            "issues": issues,
            "found_in_db": client is not None,
            "client_info": client_info,
            "params": {
                "uuid": uuid_part[:8] + "...",
                "host": host,
                "port": port,
                "security": params.get('security', 'none'),
                "sni": params.get('sni', 'N/A'),
                "flow": params.get('flow', 'N/A')
            }
        }

        return web.json_response(result)

    except Exception as e:
        logger.error(f"Error fixing key: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({"error": f"Ошибка обработки: {str(e)}"}, status=400)


async def api_order_status(request):
    """API: Проверить статус заказа"""
    order_id = request.match_info.get('order_id', '').upper()

    async with aiosqlite.connect(ORDERS_DB) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM web_orders WHERE id = ?', (order_id,))
        order = await cursor.fetchone()

        if not order:
            return web.json_response({"error": "Заказ не найден"}, status=404)

        response = {
            "order_id": order["id"],
            "status": order["status"],
            "tariff": order["tariff_name"],
            "price": order["price"]
        }

        if order["status"] == "completed" and order["vless_key"]:
            response["vless_key"] = order["vless_key"]
            # Извлекаем UUID из vless ключа для подписки
            vless_key = order["vless_key"]
            if vless_key.startswith("vless://"):
                try:
                    uuid_part = vless_key.split("://")[1].split("@")[0]
                    response["subscription_url"] = f"https://zov-gor.ru/sub/{uuid_part}"
                except:
                    pass

        return web.json_response(response)


def load_servers_config():
    """Загрузить конфигурацию серверов"""
    servers_file = BASE_DIR / 'servers_config.json'
    if servers_file.exists():
        with open(servers_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"servers": []}


def find_all_client_keys(uuid_str):
    """Найти все ключи клиента по UUID во всех inbound'ах"""
    config = load_xray_config()
    if not config:
        return []

    results = []
    for inbound in config.get('inbounds', []):
        settings = inbound.get('settings') or {}
        clients = settings.get('clients') or []

        for client in clients:
            if client.get('id') == uuid_str:
                results.append({
                    'client': client,
                    'inbound': inbound
                })
                break

    return results


def generate_vless_link_for_server(uuid, email, server_config, inbound_name='main'):
    """Генерация VLESS ссылки для внешнего сервера"""
    import urllib.parse

    inbound = server_config.get('inbounds', {}).get(inbound_name, {})
    if not inbound:
        return None

    domain = server_config.get('domain', server_config.get('ip', ''))
    port = server_config.get('port', 443)
    server_name = server_config.get('name', 'Server')

    params = [
        "type=tcp",
        f"security={inbound.get('security', 'reality')}"
    ]

    if inbound.get('security') == 'reality':
        if inbound.get('sni'):
            params.append(f"sni={inbound['sni']}")
        if inbound.get('pbk'):
            params.append(f"pbk={inbound['pbk']}")
        if inbound.get('sid'):
            params.append(f"sid={inbound['sid']}")
        params.append(f"fp={inbound.get('fp', 'chrome')}")
        if inbound.get('flow'):
            params.append(f"flow={inbound['flow']}")

    query = '&'.join(params)

    # Имя для ключа
    name_prefix = inbound.get('name_prefix', server_name)
    link_name = name_prefix
    encoded_name = urllib.parse.quote(link_name)

    return f"vless://{uuid}@{domain}:{port}?{query}#{encoded_name}"


def generate_vless_link(client, inbound):
    """Генерация VLESS ссылки для клиента из inbound"""
    uuid = client.get('id', '')
    email = client.get('email', 'client')
    flow = client.get('flow', '')

    port = inbound.get('port', 443)
    tag = inbound.get('tag', '')
    stream = inbound.get('streamSettings') or {}
    security = stream.get('security', 'none')
    network = stream.get('network', 'tcp')

    # Базовые параметры
    params = [
        f"type={network}",
        f"security={security}"
    ]

    # Reality настройки
    if security == 'reality':
        reality = stream.get('realitySettings') or {}
        server_names = reality.get('serverNames', [])
        short_ids = reality.get('shortIds', [])
        private_key = reality.get('privateKey', '')

        if server_names:
            params.append(f"sni={server_names[0]}")

        if private_key:
            public_key = generate_public_key(private_key)
            if public_key:
                params.append(f"pbk={public_key}")

        if short_ids:
            params.append(f"sid={short_ids[0]}")

        params.append("fp=chrome")

        if flow:
            params.append(f"flow={flow}")

    # gRPC настройки
    if network == 'grpc':
        grpc = stream.get('grpcSettings') or {}
        service_name = grpc.get('serviceName', '')
        if service_name:
            params.append(f"serviceName={service_name}")

    query = '&'.join(params)

    # Определяем имя для ключа из конфига
    servers_config = load_servers_config()
    local_server = next((s for s in servers_config.get('servers', []) if s.get('local')), None)
    name_prefix = "📶 Основной"
    if local_server:
        main_inbound = local_server.get('inbounds', {}).get('main', {})
        name_prefix = main_inbound.get('name_prefix', '📶 Основной')
    link_name = f"{name_prefix}"

    # URL encode имени
    import urllib.parse
    encoded_name = urllib.parse.quote(link_name)

    return f"vless://{uuid}@raphaelvpn.ru:443?{query}#{encoded_name}"


async def subscription_handler(request):
    """Обработчик подписки - возвращает все ключи клиента со всех серверов"""
    client_id = request.match_info.get('client_id', '')

    if not client_id:
        return web.Response(text="Client ID required", status=400)

    # Проверяем формат UUID
    import re
    uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

    if not uuid_pattern.match(client_id):
        return web.Response(text="Invalid client ID format", status=400)

    # Ищем ключи клиента на локальном сервере
    client_keys = find_all_client_keys(client_id)

    if not client_keys:
        return web.Response(text="Client not found", status=404)

    # Получаем email клиента для именования и данные о подписке
    client_email = client_keys[0]['client'].get('email', 'client') if client_keys else 'client'

    # Получаем данные клиента из базы данных (срок, трафик)
    upload_bytes = 0
    download_bytes = 0
    total_bytes = 0
    expire_timestamp = 0
    try:
        import sqlite3
        conn = sqlite3.connect('/etc/x-ui/x-ui.db')
        cursor = conn.cursor()
        cursor.execute("SELECT up, down, total, expiry_time FROM client_traffics WHERE email = ?", (client_email,))
        row = cursor.fetchone()
        if row:
            upload_bytes = row[0] or 0
            download_bytes = row[1] or 0
            total_bytes = row[2] or 0  # 0 = безлимит
            expire_time = row[3] or 0
            if expire_time:
                # Конвертируем из миллисекунд в секунды для заголовка
                expire_timestamp = int(expire_time / 1000) if expire_time > 9999999999 else expire_time
        conn.close()
    except Exception as e:
        logger.error(f"Error getting client data from DB: {e}")

    # Генерируем ссылки для локального сервера
    # Исключаем inbounds которые определены в servers_config (они будут добавлены отдельно)
    servers_config_inbound_tags = set()
    servers_config = load_servers_config()
    local_server = next((s for s in servers_config.get('servers', []) if s.get('local')), None)
    if local_server:
        for inbound_name in local_server.get('inbounds', {}).keys():
            if inbound_name != 'main':
                # Эти inbounds будут добавлены через generate_vless_link_for_server
                servers_config_inbound_tags.add(f"inbound-8452")  # megafon3 на порту 8452
                servers_config_inbound_tags.add(f"inbound-8453")  # megafon4 на порту 8453
                servers_config_inbound_tags.add(f"inbound-8454")  # megafon5 на порту 8454

    links = []
    for item in client_keys:
        # Пропускаем inbounds которые обрабатываются через servers_config
        tag = item['inbound'].get('tag', '')
        if tag in servers_config_inbound_tags:
            continue
        link = generate_vless_link(item['client'], item['inbound'])
        links.append(link)

    # Загружаем конфиг серверов
    servers_config = load_servers_config()

    # Обрабатываем ВСЕ серверы включая локальный для дополнительных inbounds
    for server in servers_config.get('servers', []):
        if not server.get('enabled', True):
            continue

        if server.get('local', False):
            # Для локального сервера - генерируем ключи только для НЕ-main inbounds
            # (main уже обработан через generate_vless_link выше)
            for inbound_name, inbound_config in server.get('inbounds', {}).items():
                if inbound_name == 'main':
                    continue  # main уже добавлен
                link = generate_vless_link_for_server(client_id, client_email, server, inbound_name)
                if link:
                    links.append(link)
        else:
            # Для внешних серверов - генерируем ключи для всех inbounds
            for inbound_name in server.get('inbounds', {}).keys():
                link = generate_vless_link_for_server(client_id, client_email, server, inbound_name)
                if link:
                    links.append(link)

    # Кодируем в base64 (стандартный формат подписки)
    import base64

    # Название подписки с именем клиента
    profile_name = f"ZoVGoR - {client_email}"
    profile_name_b64 = base64.b64encode(profile_name.encode()).decode()

    subscription_content = '\n'.join(links)
    encoded = base64.b64encode(subscription_content.encode()).decode()

    # Возвращаем с правильными заголовками для VPN клиентов
    # Announce с поддержкой для v2RayTun
    import base64
    announce_text = "Тех. поддержка: @bagamedovit"
    announce_b64 = "base64:" + base64.b64encode(announce_text.encode()).decode()

    # URL иконки
    icon_url = 'https://zov-gor.ru/static/logo.png'

    return web.Response(
        text=encoded,
        content_type='text/plain',
        headers={
            'Content-Disposition': f'attachment; filename="{profile_name}.txt"',
            'Profile-Title': f'base64:{profile_name_b64}',
            'Profile-Update-Interval': '12',
            'Subscription-Userinfo': f'upload={upload_bytes}; download={download_bytes}; total={total_bytes}; expire={expire_timestamp}',
            # v2RayTun specific
            'Announce': announce_b64,
            'Announce-URL': 'https://t.me/bagamedovit',
            # Icon in different formats (try all)
            'Icon': icon_url,
            'Icon-URL': icon_url,
            'Profile-Icon': icon_url,
            'Profile-Icon-URL': icon_url,
            # Other clients
            'Support-URL': 'https://t.me/bagamedovit',
            'Profile-Web-Page-URL': 'https://zov-gor.ru/static/profile.html',
            'Homepage': 'https://zov-gor.ru'
        }
    )


async def subscription_deeplink_handler(request):
    """Deep link для открытия подписки в v2RayTun"""
    client_id = request.match_info.get('client_id', '')

    if not client_id:
        return web.Response(text="Client ID required", status=400)

    import re
    uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

    if not uuid_pattern.match(client_id):
        return web.Response(text="Invalid client ID format", status=400)

    # Формируем ссылку на подписку
    import urllib.parse
    sub_url = f"https://zov-gor.ru/sub/{client_id}"
    encoded_url = urllib.parse.quote(sub_url, safe='')

    # Deep link для v2RayTun
    v2raytun_link = f"v2raytun://import/{sub_url}"

    # HTML страница с автоматическим редиректом и кнопками для разных клиентов
    html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ZoVGoR VPN - Подключение</title>
    <style>
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: white;
            min-height: 100vh;
            margin: 0;
            padding: 20px;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
        }}
        .container {{
            max-width: 400px;
            text-align: center;
        }}
        .logo {{
            width: 100px;
            height: 100px;
            margin-bottom: 20px;
        }}
        h1 {{
            margin: 0 0 10px 0;
            font-size: 28px;
        }}
        .subtitle {{
            color: #888;
            margin-bottom: 30px;
        }}
        .btn {{
            display: block;
            width: 100%;
            padding: 16px 24px;
            margin: 10px 0;
            border: none;
            border-radius: 12px;
            font-size: 16px;
            font-weight: 600;
            text-decoration: none;
            cursor: pointer;
            transition: transform 0.2s, opacity 0.2s;
        }}
        .btn:active {{
            transform: scale(0.98);
        }}
        .btn-primary {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
        }}
        .btn-secondary {{
            background: rgba(255,255,255,0.1);
            color: white;
            border: 1px solid rgba(255,255,255,0.2);
        }}
        .copy-link {{
            background: rgba(255,255,255,0.05);
            border-radius: 8px;
            padding: 12px;
            margin-top: 20px;
            word-break: break-all;
            font-size: 12px;
            color: #888;
        }}
        .copy-btn {{
            background: #4CAF50;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 6px;
            margin-top: 10px;
            cursor: pointer;
        }}
        .status {{
            margin-top: 20px;
            padding: 10px;
            border-radius: 8px;
            display: none;
        }}
        .status.success {{
            background: rgba(76, 175, 80, 0.2);
            color: #4CAF50;
            display: block;
        }}
    </style>
</head>
<body>
    <div class="container">
        <img src="/static/logo.png" alt="ZoVGoR" class="logo">
        <h1>ZoVGoR VPN</h1>
        <p class="subtitle">Выберите приложение для подключения</p>

        <a href="{v2raytun_link}" class="btn btn-primary" id="v2raytun-btn">
            📱 Открыть в v2RayTun
        </a>

        <a href="streisand://import/{sub_url}" class="btn btn-secondary">
            🎭 Открыть в Streisand
        </a>

        <a href="v2rayng://install-sub?url={encoded_url}" class="btn btn-secondary">
            🤖 Открыть в v2rayNG (Android)
        </a>

        <a href="clash://install-config?url={encoded_url}" class="btn btn-secondary">
            ⚡ Открыть в Clash
        </a>

        <div class="copy-link">
            <div>Ссылка на подписку:</div>
            <code id="sub-url">{sub_url}</code>
            <br>
            <button class="copy-btn" onclick="copyLink()">📋 Скопировать</button>
        </div>

        <div class="status" id="status"></div>
    </div>

    <script>
        function copyLink() {{
            const url = document.getElementById('sub-url').textContent;
            navigator.clipboard.writeText(url).then(() => {{
                const status = document.getElementById('status');
                status.textContent = '✅ Ссылка скопирована!';
                status.className = 'status success';
                setTimeout(() => {{ status.className = 'status'; }}, 3000);
            }});
        }}

        // Автоматически пытаемся открыть v2RayTun через 1 секунду
        setTimeout(() => {{
            window.location.href = '{v2raytun_link}';
        }}, 1000);
    </script>
</body>
</html>'''

    return web.Response(text=html, content_type='text/html')


async def subscription_json_handler(request):
    """Подписка в JSON формате (для некоторых клиентов) со всех серверов"""
    client_id = request.match_info.get('client_id', '')

    if not client_id:
        return web.json_response({"error": "Client ID required"}, status=400)

    import re
    uuid_pattern = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

    if not uuid_pattern.match(client_id):
        return web.json_response({"error": "Invalid client ID format"}, status=400)

    client_keys = find_all_client_keys(client_id)

    if not client_keys:
        return web.json_response({"error": "Client not found"}, status=404)

    client_email = client_keys[0]['client'].get('email', 'client') if client_keys else 'client'

    # Локальные ключи
    links = []
    for item in client_keys:
        link = generate_vless_link(item['client'], item['inbound'])
        links.append({
            'name': item['client'].get('email', 'client'),
            'link': link,
            'port': item['inbound'].get('port', 443),
            'tag': item['inbound'].get('tag', ''),
            'server': 'ZoVGoR'
        })

    # Внешние серверы
    servers_config = load_servers_config()
    for server in servers_config.get('servers', []):
        if not server.get('enabled', True):
            continue
        if server.get('local', False):
            continue

        server_name = server.get('name', 'Server')
        for inbound_name, inbound_config in server.get('inbounds', {}).items():
            link = generate_vless_link_for_server(client_id, client_email, server, inbound_name)
            if link:
                name_prefix = inbound_config.get('name_prefix', server_name)
                links.append({
                    'name': f"{name_prefix}-{client_email}",
                    'link': link,
                    'port': server.get('port', 443),
                    'tag': inbound_name,
                    'server': server_name
                })

    return web.json_response({
        'count': len(links),
        'links': links
    })


async def create_webapp():
    """Создание веб-приложения"""
    # Инициализируем БД заказов
    await init_orders_db()

    app = web.Application()

    # Главная страница
    app.router.add_get('/', index_handler)
    app.router.add_get('/index.html', index_handler)

    # API endpoints
    app.router.add_get('/api/tariffs', api_tariffs)
    app.router.add_get('/api/payment', api_payment_details)
    app.router.add_post('/api/order', api_create_order)
    app.router.add_post('/api/confirm', api_confirm_payment)
    app.router.add_get('/api/order/{order_id}', api_order_status)
    app.router.add_post('/api/fix-key', api_fix_key)

    # Subscription endpoints
    app.router.add_get('/sub/{client_id}', subscription_handler)
    app.router.add_get('/sub/{client_id}/json', subscription_json_handler)
    app.router.add_get('/sub/{client_id}/open', subscription_deeplink_handler)
    app.router.add_get('/open/{client_id}', subscription_deeplink_handler)

    # Статические файлы
    app.router.add_static('/static', STATIC_DIR, name='static')

    logger.info(f"WebApp initialized. Static dir: {STATIC_DIR}")
    logger.info(f"Templates dir: {TEMPLATES_DIR}")

    return app


async def start_webapp_server(host='0.0.0.0', port=9090):
    """Запуск веб-сервера"""
    app = await create_webapp()
    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, host, port)
    await site.start()

    logger.info(f"WebApp server started on http://{host}:{port}")

    return runner


if __name__ == '__main__':
    # Для тестирования
    logging.basicConfig(level=logging.INFO)

    async def main():
        runner = await start_webapp_server()
        print("WebApp server is running. Press Ctrl+C to stop.")

        # Держим сервер запущенным
        try:
            import asyncio
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            await runner.cleanup()

    import asyncio
    asyncio.run(main())
