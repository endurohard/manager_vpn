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

        return web.json_response(response)


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
