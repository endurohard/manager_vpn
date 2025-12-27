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
    """Найти клиента по UUID в конфиге xray (локальный сервер)"""
    config = load_xray_config()
    if not config:
        return None, None, None

    servers_config = load_servers_config()
    local_server = None
    for srv in servers_config.get('servers', []):
        if srv.get('local'):
            local_server = srv
            break

    for inbound in config.get('inbounds', []):
        settings = inbound.get('settings') or {}
        clients = settings.get('clients') or []

        for client in clients:
            if client.get('id') == uuid_str:
                return client, inbound, local_server

    return None, None, None


def find_client_on_remote_server(uuid_str, server):
    """Найти клиента по UUID на удалённом сервере"""
    import subprocess

    ssh_config = server.get('ssh', {})
    if not ssh_config:
        return None, None

    ssh_user = ssh_config.get('user', 'root')
    ssh_pass = ssh_config.get('password', '')
    ssh_host = server.get('ip', '')

    if not ssh_pass or not ssh_host:
        return None, None

    try:
        # Получаем конфиг xray с удалённого сервера
        cmd = f"sshpass -p '{ssh_pass}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 {ssh_user}@{ssh_host} 'cat /usr/local/x-ui/bin/config.json 2>/dev/null || cat /etc/x-ui/bin/config.json 2>/dev/null'"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)

        if result.returncode != 0 or not result.stdout.strip():
            return None, None

        config = json.loads(result.stdout)

        for inbound in config.get('inbounds', []):
            settings = inbound.get('settings') or {}
            clients = settings.get('clients') or []

            for client in clients:
                if client.get('id') == uuid_str:
                    return client, inbound
    except Exception as e:
        logger.error(f"Error finding client on remote server {ssh_host}: {e}")

    return None, None


def find_client_on_all_servers(uuid_str):
    """Найти клиента по UUID на всех серверах"""
    servers_config = load_servers_config()

    # Сначала проверяем локальный сервер
    client, inbound, local_server = find_client_in_xray(uuid_str)
    if client and inbound:
        return client, inbound, local_server

    # Затем проверяем удалённые сервера
    for server in servers_config.get('servers', []):
        if server.get('local'):
            continue  # Уже проверили
        if not server.get('enabled'):
            continue

        client, inbound = find_client_on_remote_server(uuid_str, server)
        if client and inbound:
            return client, inbound, server

    return None, None, None


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
    """API: Миграция ключа с локального X-UI на активный сервер"""
    import aiohttp
    import ssl
    import time

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

        # Загружаем конфиг серверов
        servers_config = load_servers_config()

        # Находим активный сервер (active_for_new: true)
        target_server = None
        local_server = None

        for srv in servers_config.get('servers', []):
            if srv.get('active_for_new'):
                target_server = srv
            if srv.get('local'):
                local_server = srv

        if not target_server:
            return web.json_response({"error": "Активный сервер не найден в конфиге"}, status=500)

        # Ищем клиента на локальном X-UI
        config = load_xray_config()
        if not config:
            return web.json_response({"error": "Не удалось загрузить локальный конфиг X-UI"}, status=500)

        client_data = None
        source_inbound = None

        for inbound in config.get('inbounds', []):
            settings = inbound.get('settings') or {}
            clients = settings.get('clients') or []

            for client in clients:
                if client.get('id') == uuid_part:
                    client_data = client
                    source_inbound = inbound
                    break

            if client_data:
                break

        # Извлекаем данные клиента (если найден) или используем данные из ключа
        if client_data:
            email = client_data.get('email', '')
            limit_ip = client_data.get('limitIp', 2)
            expiry_time = client_data.get('expiryTime', 0)
        else:
            # Клиент не найден локально - извлекаем email из фрагмента ключа
            email = urllib.parse.unquote(fragment) if fragment else uuid_part[:8]
            limit_ip = 2
            expiry_time = 0
            logger.info(f"Клиент {uuid_part[:8]}... не найден локально, используем данные из ключа")

        # Используем оригинальный UUID - НЕ генерируем новый
        new_uuid = uuid_part

        # Формируем новый VLESS ключ
        target_domain = target_server.get('domain', target_server.get('ip'))
        target_port_final = target_server.get('port', 443)

        # Параметры из конфига целевого сервера
        main_inbound = target_server.get('inbounds', {}).get('main', {})
        network = main_inbound.get('network', 'tcp')

        # Формируем параметры в правильном порядке (как в remote_xui.py)
        params_list = [
            f"type={network}",
            "encryption=none"
        ]

        # Добавляем gRPC параметры если нужно
        if network == 'grpc':
            params_list.append(f"serviceName={main_inbound.get('serviceName', '')}")
            params_list.append(f"authority={main_inbound.get('authority', '')}")

        params_list.append(f"security={main_inbound.get('security', 'reality')}")

        if main_inbound.get('security', 'reality') == 'reality':
            if main_inbound.get('pbk'):
                params_list.append(f"pbk={main_inbound['pbk']}")
            params_list.append(f"fp={main_inbound.get('fp', 'chrome')}")
            if main_inbound.get('sni'):
                params_list.append(f"sni={main_inbound['sni']}")
            if main_inbound.get('sid'):
                params_list.append(f"sid={main_inbound['sid']}")
            if main_inbound.get('flow'):
                params_list.append(f"flow={main_inbound['flow']}")
            params_list.append("spx=%2F")

        new_query = '&'.join(params_list)

        # Используем оригинальное имя или создаём новое
        server_name = target_server.get('name', 'VPN')
        new_fragment = fragment if fragment else f"{server_name}-{email}"

        fixed_link = f"vless://{new_uuid}@{target_domain}:{target_port_final}?{new_query}#{new_fragment}"

        # Форматируем дату истечения
        expiry_str = "Безлимит"
        if expiry_time > 0:
            from datetime import datetime
            expiry_dt = datetime.fromtimestamp(expiry_time / 1000)
            expiry_str = expiry_dt.strftime("%d.%m.%Y %H:%M")

        local_name = local_server.get('name', 'Local') if local_server else 'Local'

        # Получаем значения для отображения
        security_val = main_inbound.get('security', 'reality')
        sni_val = main_inbound.get('sni', '')
        flow_val = main_inbound.get('flow', '')

        # Формируем список исправлений
        fixes_list = [
            f"Настройки обновлены по конфигу {target_server.get('name', 'Target')}",
            f"Хост: {target_domain}",
            f"SNI: {sni_val or 'N/A'}",
        ]
        if flow_val:
            fixes_list.append(f"Flow: {flow_val}")
        else:
            fixes_list.append("Flow: пусто (убран)")

        result = {
            "original": vless_link,
            "fixed": fixed_link,
            "changed": vless_link != fixed_link,
            "migrated": True,
            "fixes": fixes_list,
            "issues": [],
            "found_in_db": client_data is not None,
            "client_info": {
                "email": email,
                "limitIp": limit_ip,
                "expiryTime": expiry_time,
                "source_server": local_name if client_data else "Не найден",
                "target_server": target_server.get('name', 'Target')
            },
            "params": {
                "uuid": new_uuid[:8] + "...",
                "host": target_domain,
                "port": str(target_port_final),
                "security": security_val,
                "sni": sni_val or 'N/A',
                "flow": flow_val or 'пусто'
            }
        }

        return web.json_response(result)

    except Exception as e:
        logger.error(f"Error migrating key: {e}")
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
    network = inbound.get('network', 'tcp')

    params = [
        f"type={network}",
        "encryption=none"
    ]

    # Добавляем gRPC параметры если нужно
    if network == 'grpc':
        params.append(f"serviceName={inbound.get('serviceName', '')}")
        params.append(f"authority={inbound.get('authority', '')}")

    params.append(f"security={inbound.get('security', 'reality')}")

    if inbound.get('security') == 'reality':
        if inbound.get('pbk'):
            params.append(f"pbk={inbound['pbk']}")
        params.append(f"fp={inbound.get('fp', 'chrome')}")
        if inbound.get('sni'):
            params.append(f"sni={inbound['sni']}")
        if inbound.get('sid'):
            params.append(f"sid={inbound['sid']}")
        if inbound.get('flow'):
            params.append(f"flow={inbound['flow']}")
        params.append("spx=%2F")

    query = '&'.join(params)

    # Имя для ключа
    name_prefix = inbound.get('name_prefix', server_name)
    link_name = f"{name_prefix} {email}" if email else name_prefix

    return f"vless://{uuid}@{domain}:{port}?{query}#{link_name}"


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
        "encryption=none",
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

    return f"vless://{uuid}@raphaelvpn.ru:443?{query}#{link_name}"


async def subscription_handler(request):
    """Обработчик подписки - возвращает ключи клиента с активных серверов"""
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

    # Загружаем конфиг серверов
    servers_config = load_servers_config()
    local_server = next((s for s in servers_config.get('servers', []) if s.get('local')), None)
    local_active = local_server.get('active_for_new', True) if local_server else True

    # Получаем email клиента для именования
    client_email = 'client'
    if client_keys:
        client_email = client_keys[0]['client'].get('email', 'client')

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
                expire_timestamp = int(expire_time / 1000) if expire_time > 9999999999 else expire_time
        conn.close()
    except Exception as e:
        logger.error(f"Error getting client data from DB: {e}")

    links = []

    # Генерируем ссылки для локального сервера только если он активен и клиент найден
    if client_keys and local_active:
        servers_config_inbound_tags = set()
        if local_server:
            for inbound_name in local_server.get('inbounds', {}).keys():
                if inbound_name != 'main':
                    servers_config_inbound_tags.add(f"inbound-8452")
                    servers_config_inbound_tags.add(f"inbound-8453")
                    servers_config_inbound_tags.add(f"inbound-8454")

        for item in client_keys:
            tag = item['inbound'].get('tag', '')
            if tag in servers_config_inbound_tags:
                continue
            link = generate_vless_link(item['client'], item['inbound'])
            links.append(link)

    # Обрабатываем серверы из конфига - только активные
    for server in servers_config.get('servers', []):
        if not server.get('enabled', True):
            continue
        # Пропускаем неактивные для новых подписок серверы
        if not server.get('active_for_new', True):
            continue

        if server.get('local', False):
            # Для локального сервера - генерируем ключи только если клиент найден
            if client_keys:
                for inbound_name, inbound_config in server.get('inbounds', {}).items():
                    if inbound_name == 'main':
                        continue  # main уже добавлен выше
                    link = generate_vless_link_for_server(client_id, client_email, server, inbound_name)
                    if link:
                        links.append(link)
        else:
            # Для внешних серверов - генерируем ключи для всех inbounds
            for inbound_name in server.get('inbounds', {}).keys():
                link = generate_vless_link_for_server(client_id, client_email, server, inbound_name)
                if link:
                    links.append(link)

    # Если нет ключей - возвращаем 404
    if not links:
        return web.Response(text="Client not found or no active servers", status=404)

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
                # Формируем имя: PREFIX пробел EMAIL (как в get_client_link_from_active_server)
                links.append({
                    'name': f"{name_prefix} {client_email}",
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
