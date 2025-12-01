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
OLD_XUI_BACKUP = BASE_DIR / 'backups' / 'x-ui_backup_2025-12-01_02-00.db'

# Создаём директорию для загрузок
UPLOADS_DIR.mkdir(exist_ok=True)

# Глобальная ссылка на бота для уведомлений
bot_instance = None
admin_id = None
xui_client = None


def set_bot_instance(bot, admin, xui=None):
    """Установить экземпляр бота и xui клиента для уведомлений"""
    global bot_instance, admin_id, xui_client
    bot_instance = bot
    admin_id = admin
    xui_client = xui


def set_xui_client(xui):
    """Установить экземпляр XUI клиента"""
    global xui_client
    xui_client = xui


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
    """Загрузить конфиг xray из базы данных X-UI"""
    import sqlite3
    try:
        conn = sqlite3.connect('/etc/x-ui/x-ui.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id, port, protocol, settings, stream_settings, tag, sniffing FROM inbounds WHERE enable = 1")
        rows = cursor.fetchall()
        conn.close()

        inbounds = []
        for row in rows:
            inbound_id, port, protocol, settings_str, stream_str, tag, sniffing_str = row

            settings = json.loads(settings_str) if settings_str else {}
            stream_settings = json.loads(stream_str) if stream_str else {}
            sniffing = json.loads(sniffing_str) if sniffing_str else {}

            inbounds.append({
                'id': inbound_id,
                'port': port,
                'protocol': protocol,
                'settings': settings,
                'streamSettings': stream_settings,
                'tag': tag or f'inbound-{port}',
                'sniffing': sniffing
            })

        return {'inbounds': inbounds}
    except Exception as e:
        logger.error(f"Error loading xray config from DB: {e}")
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


def get_reality_public_key(reality_settings):
    """Получить публичный ключ из настроек REALITY"""
    # Сначала ищем в settings (X-UI хранит там)
    settings = reality_settings.get('settings', {})
    public_key = settings.get('publicKey', '')
    if public_key:
        return public_key

    # Если нет - генерируем из приватного
    private_key = reality_settings.get('privateKey', '')
    if private_key:
        return generate_public_key(private_key)

    return None


def load_inbound_settings_from_db(inbound_id=12):
    """Загрузить настройки inbound из базы данных X-UI"""
    import sqlite3
    try:
        conn = sqlite3.connect('/etc/x-ui/x-ui.db')
        cursor = conn.cursor()
        cursor.execute("SELECT stream_settings FROM inbounds WHERE id=?", (inbound_id,))
        row = cursor.fetchone()
        conn.close()
        if row:
            return json.loads(row[0])
    except Exception as e:
        logger.error(f"Error loading inbound from DB: {e}")
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
            security = stream.get('security', 'none')

            # Загружаем настройки из БД для более точных данных REALITY
            db_stream = load_inbound_settings_from_db(12)
            reality = stream.get('realitySettings') or {}
            if db_stream:
                reality = db_stream.get('realitySettings') or reality

            # Исправляем security
            if params.get('security') != security:
                old_sec = params.get('security', 'none')
                params['security'] = security
                fixes.append(f"Исправлен security: {old_sec} → {security}")

            if security == 'reality':
                # Исправляем flow из настроек клиента
                client_flow = client.get('flow', '')
                current_flow = params.get('flow', '')

                if client_flow:
                    # Если flow установлен в базе - применяем его
                    if current_flow != client_flow:
                        old_flow = current_flow or 'отсутствует'
                        params['flow'] = client_flow
                        fixes.append(f"Исправлен flow: {old_flow} → {client_flow}")
                else:
                    # Если flow пустой в базе - убираем из ключа
                    if current_flow:
                        del params['flow']
                        fixes.append(f"Убран flow: {current_flow} (отключен на сервере)")

                # Исправляем SNI
                server_names = reality.get('serverNames', [])
                current_sni = params.get('sni', '')
                if server_names and current_sni not in server_names:
                    params['sni'] = server_names[0]
                    fixes.append(f"Исправлен SNI: {current_sni} → {server_names[0]}")

                # Исправляем public key (используем новую функцию)
                public_key = get_reality_public_key(reality)
                if public_key and params.get('pbk') != public_key:
                    old_pbk = params.get('pbk', 'отсутствует')[:10] + '...' if params.get('pbk') else 'отсутствует'
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

                # Убеждаемся что spx есть
                if 'spx' not in params:
                    params['spx'] = '%2F'
                    fixes.append("Добавлен spx: /")

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
            # UUID не найден - попробуем найти по email/фрагменту
            search_term = None
            found_by_email = None

            # Пробуем поискать по фрагменту ключа (часто содержит номер телефона)
            if fragment:
                import urllib.parse
                import re
                decoded_fragment = urllib.parse.unquote(fragment)

                # Ищем номер телефона в фрагменте (+7..., 7..., 8...)
                phone_match = re.search(r'[\+]?[78][\d\s\-]{9,}', decoded_fragment)
                if phone_match:
                    search_term = phone_match.group(0)
                    # Нормализуем: оставляем только цифры
                    search_digits = re.sub(r'[^\d]', '', search_term)
                    if len(search_digits) >= 10:
                        # Ищем по последним 10 цифрам
                        search_term = search_digits[-10:]
                else:
                    # Пробуем найти по частям фрагмента
                    parts = re.split(r'[-_\s]', decoded_fragment)
                    for part in parts:
                        if len(part) >= 4 and part not in ['VPNPULSE', 'VPN', 'PULSE', 'direct', 'lte']:
                            search_term = part
                            break

                if search_term and len(search_term) >= 3:
                    email_results = find_client_by_email(search_term)
                    if email_results:
                        found_by_email = email_results[0]

            if found_by_email:
                # Нашли клиента по email - генерируем правильный ключ
                real_client = found_by_email['client']
                real_inbound = found_by_email['inbound']

                issues.append(f"UUID в ключе неверный! Найден клиент по email '{search_term}'")
                fixes.append(f"UUID заменён на актуальный: {real_client.get('id')[:8]}...")

                # Генерируем полностью новый правильный ключ
                correct_link = generate_vless_link(real_client, real_inbound)

                client_info = {
                    "email": real_client.get('email', 'N/A'),
                    "inbound": real_inbound.get('tag', 'N/A'),
                    "port": real_inbound.get('port', 'N/A'),
                    "found_by": "email_search"
                }

                result = {
                    "original": vless_link,
                    "fixed": correct_link,
                    "changed": True,
                    "fixes": fixes,
                    "issues": issues,
                    "found_in_db": True,
                    "found_by_email": True,
                    "search_term": search_term,
                    "client_info": client_info,
                    "subscription_url": f"https://zov-gor.ru/sub/{real_client.get('id', '')}",
                    "params": {
                        "uuid": real_client.get('id', '')[:8] + "...",
                        "host": "raphaelvpn.ru",
                        "port": "443",
                        "security": "reality",
                        "sni": "mirror.yandex.ru",
                        "flow": real_client.get('flow', 'xtls-rprx-vision')
                    }
                }

                return web.json_response(result)
            else:
                issues.append("UUID не найден в базе xray! Ключ может быть недействительным.")
                if fragment:
                    issues.append(f"Также не удалось найти клиента по email из фрагмента '{search_term}'")

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


async def api_find_key(request):
    """API: Найти клиента по email и сгенерировать vless ключ"""
    try:
        data = await request.json()
    except:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    query = data.get("query", data.get("email", "")).strip()

    if not query:
        return web.json_response({"error": "Укажите email клиента"}, status=400)

    if len(query) < 3:
        return web.json_response({"error": "Минимум 3 символа для поиска"}, status=400)

    # Ищем клиентов по email
    results = find_client_by_email(query)

    if not results:
        return web.json_response({
            "found": False,
            "error": f"Клиент с email '{query}' не найден в базе",
            "clients": []
        })

    # Генерируем vless ссылки для найденных клиентов
    clients_data = []
    for item in results:
        client = item['client']
        inbound = item['inbound']

        # Генерируем ссылку
        vless_link = generate_vless_link(client, inbound)

        clients_data.append({
            "email": client.get('email', ''),
            "uuid": client.get('id', ''),
            "flow": client.get('flow', ''),
            "inbound_tag": inbound.get('tag', ''),
            "vless_link": vless_link,
            "subscription_url": f"https://zov-gor.ru/sub/{client.get('id', '')}"
        })

    return web.json_response({
        "found": True,
        "count": len(clients_data),
        "query": query,
        "clients": clients_data
    })


async def api_migrate_client(request):
    """
    API: Миграция клиента из старой базы X-UI
    Ищет клиента по старому ключу/UUID в бэкапе, создаёт нового клиента с теми же параметрами
    """
    try:
        data = await request.json()
    except:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    vless_link = data.get("key", data.get("vless_link", "")).strip()

    if not vless_link:
        return web.json_response({"error": "Укажите VLESS ключ"}, status=400)

    if not vless_link.startswith("vless://"):
        return web.json_response({"error": "Неверный формат ключа. Должен начинаться с vless://"}, status=400)

    # Парсим UUID из ключа
    try:
        link_without_proto = vless_link[8:]
        if '@' in link_without_proto:
            uuid_part = link_without_proto.split('@')[0]
        else:
            return web.json_response({"error": "Неверный формат ключа: отсутствует UUID"}, status=400)
    except Exception as e:
        return web.json_response({"error": f"Ошибка парсинга ключа: {str(e)}"}, status=400)

    # Ищем клиента в старом бэкапе
    old_client = find_client_in_old_backup(uuid_part)

    if not old_client:
        return web.json_response({
            "error": "Клиент не найден в старой базе",
            "uuid": uuid_part[:8] + "...",
            "hint": "Возможно, ключ от другого сервера или уже был перенесён"
        }, status=404)

    # Проверяем что xui_client доступен
    if not xui_client:
        return web.json_response({"error": "XUI клиент не инициализирован"}, status=500)

    # Вычисляем оставшееся время подписки
    import time
    from datetime import datetime, timedelta

    expiry_time_ms = old_client.get('expiryTime', 0)

    # Если срок <= 0 или в прошлом - ставим 30 дней по умолчанию
    if expiry_time_ms <= 0:
        # Безлимит или не установлено - ставим 1 год
        days_left = 365
        expiry_date = datetime.now() + timedelta(days=365)
    else:
        expiry_timestamp = expiry_time_ms / 1000
        expiry_date = datetime.fromtimestamp(expiry_timestamp)
        now = datetime.now()

        if expiry_date > now:
            days_left = (expiry_date - now).days + 1
        else:
            # Подписка истекла - даём 7 дней
            days_left = 7
            expiry_date = now + timedelta(days=7)

    # Получаем данные клиента
    client_email = old_client.get('email', '')
    limit_ip = old_client.get('limitIp', 2)
    if limit_ip <= 0:
        limit_ip = 2  # Минимум 2 устройства

    # Создаём нового клиента через XUI API
    try:
        # Используем inbound_id = 1 (новый сервер)
        inbound_id = 1

        result = await xui_client.add_client(
            inbound_id=inbound_id,
            email=client_email,
            phone="",  # Нет телефона при миграции
            expire_days=days_left,
            ip_limit=limit_ip
        )

        if result and result.get('client_id'):
            new_uuid = result.get('client_id', '')

            # Генерируем VLESS ссылку
            new_vless_link = await xui_client.get_client_link(inbound_id, client_email)

            # Заменяем IP на домен
            if new_vless_link:
                new_vless_link = xui_client.replace_ip_with_domain(new_vless_link, 'raphaelvpn.ru', 443)

            return web.json_response({
                "success": True,
                "message": f"Клиент {client_email} успешно перенесён!",
                "old_client": {
                    "email": old_client.get('email'),
                    "uuid": old_client.get('uuid', '')[:8] + "...",
                    "expiry": expiry_date.strftime("%Y-%m-%d"),
                    "limitIp": old_client.get('limitIp', 2)
                },
                "new_client": {
                    "email": client_email,
                    "uuid": new_uuid[:8] + "..." if new_uuid else "N/A",
                    "days": days_left,
                    "limitIp": limit_ip,
                    "vless_link": new_vless_link,
                    "subscription_url": f"https://zov-gor.ru:8080/sub/{new_uuid}" if new_uuid else None
                }
            })
        elif result and result.get('error'):
            error_msg = result.get('message', 'Неизвестная ошибка')
            if result.get('is_duplicate'):
                return web.json_response({
                    "error": f"Клиент с таким именем уже существует: {client_email}",
                    "hint": "Возможно, вы уже переносили этот ключ ранее"
                }, status=409)
            return web.json_response({
                "error": f"Ошибка создания клиента: {error_msg}",
                "old_client": {
                    "email": old_client.get('email'),
                    "uuid": old_client.get('uuid', '')[:8] + "..."
                }
            }, status=500)
        else:
            return web.json_response({
                "error": "Не удалось создать клиента. Попробуйте позже.",
                "old_client": {
                    "email": old_client.get('email'),
                    "uuid": old_client.get('uuid', '')[:8] + "..."
                }
            }, status=500)

    except Exception as e:
        logger.error(f"Ошибка миграции клиента: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({"error": f"Ошибка миграции: {str(e)}"}, status=500)


async def api_search_old_clients(request):
    """
    API: Поиск клиентов в старой базе по email/телефону
    """
    try:
        data = await request.json()
    except:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    query = data.get("query", "").strip()

    if not query:
        return web.json_response({"error": "Укажите email или номер телефона"}, status=400)

    if len(query) < 3:
        return web.json_response({"error": "Минимум 3 символа для поиска"}, status=400)

    results = search_client_in_old_backup(query)

    if not results:
        return web.json_response({
            "found": False,
            "query": query,
            "clients": []
        })

    # Форматируем результаты
    from datetime import datetime
    clients_data = []
    for client in results[:20]:  # Максимум 20 результатов
        expiry_ms = client.get('expiryTime', 0)
        if expiry_ms > 0:
            expiry_date = datetime.fromtimestamp(expiry_ms / 1000).strftime("%Y-%m-%d")
        else:
            expiry_date = "Безлимит"

        clients_data.append({
            "email": client.get('email', ''),
            "uuid": client.get('uuid', ''),
            "expiry": expiry_date,
            "limitIp": client.get('limitIp', 2),
            "enable": client.get('enable', True)
        })

    return web.json_response({
        "found": True,
        "count": len(clients_data),
        "query": query,
        "clients": clients_data
    })


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


def find_client_by_email(email_query):
    """Найти клиента по email (точное или частичное совпадение, включая поиск по цифрам телефона)"""
    import re
    config = load_xray_config()
    if not config:
        return []

    results = []
    email_query_lower = email_query.lower().strip()

    # Если запрос состоит только из цифр - это поиск по телефону
    query_digits = re.sub(r'[^\d]', '', email_query)
    is_phone_search = len(query_digits) >= 7

    for inbound in config.get('inbounds', []):
        settings = inbound.get('settings') or {}
        clients = settings.get('clients') or []

        for client in clients:
            client_email = client.get('email', '').lower()

            # Точное совпадение или частичное по строке
            if client_email == email_query_lower or email_query_lower in client_email:
                results.append({
                    'client': client,
                    'inbound': inbound
                })
                continue

            # Поиск по цифрам телефона
            if is_phone_search:
                client_digits = re.sub(r'[^\d]', '', client_email)
                # Совпадение по последним N цифрам
                if len(client_digits) >= 10 and len(query_digits) >= 7:
                    if client_digits[-10:] == query_digits[-10:] or query_digits in client_digits:
                        results.append({
                            'client': client,
                            'inbound': inbound
                        })

    return results


def find_client_in_old_backup(uuid_str):
    """
    Найти клиента по UUID в старой резервной копии X-UI
    Возвращает данные клиента: email, expiryTime, limitIp
    """
    import sqlite3

    if not OLD_XUI_BACKUP.exists():
        logger.error(f"Файл бэкапа не найден: {OLD_XUI_BACKUP}")
        return None

    try:
        conn = sqlite3.connect(OLD_XUI_BACKUP)
        cursor = conn.cursor()

        # Получаем settings из inbounds
        cursor.execute("SELECT settings FROM inbounds WHERE id = 12")
        row = cursor.fetchone()
        conn.close()

        if not row:
            return None

        settings = json.loads(row[0])
        clients = settings.get('clients', [])

        # Ищем клиента по UUID
        for client in clients:
            if client.get('id') == uuid_str:
                return {
                    'email': client.get('email', ''),
                    'uuid': client.get('id', ''),
                    'expiryTime': client.get('expiryTime', 0),
                    'limitIp': client.get('limitIp', 2),
                    'flow': client.get('flow', ''),
                    'enable': client.get('enable', True),
                    'totalGB': client.get('totalGB', 0)
                }

        return None
    except Exception as e:
        logger.error(f"Ошибка поиска в старом бэкапе: {e}")
        return None


def search_client_in_old_backup(search_term):
    """
    Поиск клиента в старом бэкапе по email или номеру телефона
    """
    import sqlite3
    import re

    if not OLD_XUI_BACKUP.exists():
        logger.error(f"Файл бэкапа не найден: {OLD_XUI_BACKUP}")
        return []

    try:
        conn = sqlite3.connect(OLD_XUI_BACKUP)
        cursor = conn.cursor()

        cursor.execute("SELECT settings FROM inbounds WHERE id = 12")
        row = cursor.fetchone()
        conn.close()

        if not row:
            return []

        settings = json.loads(row[0])
        clients = settings.get('clients', [])

        results = []
        search_lower = search_term.lower().strip()
        search_digits = re.sub(r'[^\d]', '', search_term)
        is_phone_search = len(search_digits) >= 7

        for client in clients:
            client_email = client.get('email', '').lower()

            # Точное или частичное совпадение по email
            if search_lower in client_email or client_email == search_lower:
                results.append({
                    'email': client.get('email', ''),
                    'uuid': client.get('id', ''),
                    'expiryTime': client.get('expiryTime', 0),
                    'limitIp': client.get('limitIp', 2),
                    'flow': client.get('flow', ''),
                    'enable': client.get('enable', True),
                    'totalGB': client.get('totalGB', 0)
                })
                continue

            # Поиск по цифрам телефона
            if is_phone_search:
                client_digits = re.sub(r'[^\d]', '', client_email)
                if len(client_digits) >= 7 and (search_digits in client_digits or client_digits in search_digits):
                    results.append({
                        'email': client.get('email', ''),
                        'uuid': client.get('id', ''),
                        'expiryTime': client.get('expiryTime', 0),
                        'limitIp': client.get('limitIp', 2),
                        'flow': client.get('flow', ''),
                        'enable': client.get('enable', True),
                        'totalGB': client.get('totalGB', 0)
                    })

        return results
    except Exception as e:
        logger.error(f"Ошибка поиска в старом бэкапе: {e}")
        return []


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

        if server_names:
            params.append(f"sni={server_names[0]}")

        # Получаем публичный ключ из settings (X-UI хранит его там)
        public_key = get_reality_public_key(reality)
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

    # Получаем email клиента для именования
    client_email = client_keys[0]['client'].get('email', 'client') if client_keys else 'client'

    # Генерируем ссылки для локального сервера
    links = []
    for item in client_keys:
        link = generate_vless_link(item['client'], item['inbound'])
        links.append(link)

    # Загружаем конфиг внешних серверов и добавляем их ключи
    servers_config = load_servers_config()
    for server in servers_config.get('servers', []):
        if not server.get('enabled', True):
            continue
        if server.get('local', False):
            continue  # Пропускаем локальный сервер - уже обработан

        # Генерируем ключи для каждого inbound внешнего сервера
        for inbound_name in server.get('inbounds', {}).keys():
            link = generate_vless_link_for_server(client_id, client_email, server, inbound_name)
            if link:
                links.append(link)

    # Кодируем в base64 (стандартный формат подписки)
    import base64

    # Название подписки
    profile_name = "ZoVGoR"
    profile_name_b64 = base64.b64encode(profile_name.encode()).decode()

    subscription_content = '\n'.join(links)
    encoded = base64.b64encode(subscription_content.encode()).decode()

    # Возвращаем с правильными заголовками для VPN клиентов
    return web.Response(
        text=encoded,
        content_type='text/plain',
        headers={
            'Content-Disposition': f'attachment; filename="{profile_name}.txt"',
            'Profile-Title': profile_name_b64,
            'Profile-Update-Interval': '12',  # Обновление каждые 12 часов
            'Subscription-Userinfo': f'upload=0; download=0; total=0; expire=0',
            'Support-URL': 'https://t.me/bagamedovit',
            'Profile-Web-Page-URL': 'https://zov-gor.ru/static/profile.html',
            'Homepage': 'https://zov-gor.ru',
            'Profile-Icon': 'https://zov-gor.ru/static/logo.png'
        }
    )


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
    app.router.add_post('/api/find-key', api_find_key)
    app.router.add_post('/api/migrate-client', api_migrate_client)
    app.router.add_post('/api/search-old-clients', api_search_old_clients)

    # Subscription endpoints
    app.router.add_get('/sub/{client_id}', subscription_handler)
    app.router.add_get('/sub/{client_id}/json', subscription_json_handler)

    # Статические файлы
    app.router.add_static('/static', STATIC_DIR, name='static')

    logger.info(f"WebApp initialized. Static dir: {STATIC_DIR}")
    logger.info(f"Templates dir: {TEMPLATES_DIR}")

    return app


async def start_webapp_server(host='0.0.0.0', port=9090, ssl_cert=None, ssl_key=None):
    """Запуск веб-сервера"""
    import ssl as ssl_module

    app = await create_webapp()
    runner = web.AppRunner(app)
    await runner.setup()

    ssl_context = None
    if ssl_cert and ssl_key:
        import os
        if os.path.exists(ssl_cert) and os.path.exists(ssl_key):
            ssl_context = ssl_module.create_default_context(ssl_module.Purpose.CLIENT_AUTH)
            ssl_context.load_cert_chain(ssl_cert, ssl_key)
            logger.info(f"SSL enabled with cert: {ssl_cert}")

    site = web.TCPSite(runner, host, port, ssl_context=ssl_context)
    await site.start()

    protocol = "https" if ssl_context else "http"
    logger.info(f"WebApp server started on {protocol}://{host}:{port}")

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
