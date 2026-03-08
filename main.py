"""
Главный файл бота для управления VPN ключами
"""
import asyncio
import functools
import logging
import sys
import os
import shutil
from datetime import datetime, time
from pathlib import Path
import aiosqlite

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.types import FSInputFile

import json

from bot.config import BOT_TOKEN, XUI_HOST, XUI_USERNAME, XUI_PASSWORD, DATABASE_PATH, WEBAPP_HOST, WEBAPP_PORT, ADMIN_ID, INBOUND_ID, YANDEX_LOGIN, YANDEX_PASSWORD, BACKUP_KEEP_DAYS
from bot.database import DatabaseManager
from bot.api import XUIClient
from bot.handlers import common, manager, admin, extended
from bot.middlewares import BanCheckMiddleware, ThrottlingMiddleware, MaintenanceMiddleware
from bot.webapp.server import start_webapp_server, set_bot_instance
from bot.api.remote_xui import load_servers_config, get_client_link_from_active_server, get_all_clients_from_panel, reset_client_traffic_via_panel, PANEL_REQUEST_TIMEOUT

# Путь к базе данных X-UI
XUI_DB_PATH = Path("/etc/x-ui/x-ui.db")

# Настройка логирования
# Используем только StreamHandler — systemd сам записывает stdout в bot.log
# (FileHandler + systemd redirect давали дублирование строк)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)


import aiohttp as _aiohttp

WEBDAV_BASE = "https://webdav.yandex.ru"
WEBDAV_BACKUP_DIR = "/vpn_backups"


async def _webdav_request(method: str, path: str, **kwargs):
    """Выполнить WebDAV-запрос к Яндекс.Диску"""
    auth = _aiohttp.BasicAuth(YANDEX_LOGIN, YANDEX_PASSWORD)
    async with _aiohttp.ClientSession(auth=auth) as session:
        async with session.request(method, f"{WEBDAV_BASE}{path}", **kwargs) as resp:
            return resp.status, await resp.text()


async def _ensure_webdav_dir(path: str):
    """Создать директорию на Яндекс.Диске если не существует"""
    status, _ = await _webdav_request("MKCOL", path)
    # 201 = created, 405 = already exists
    return status in (201, 405)


async def _webdav_list_files(path: str) -> list:
    """Получить список файлов в директории через PROPFIND"""
    headers = {"Depth": "1", "Content-Type": "application/xml"}
    body = '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:getlastmodified/></d:prop></d:propfind>'
    status, text = await _webdav_request("PROPFIND", path, headers=headers, data=body)
    if status != 207:
        return []
    # Парсим href из multistatus ответа
    import re
    hrefs = re.findall(r'<d:href>([^<]+)</d:href>', text)
    # Первый href — сама директория, остальные — файлы
    return [h for h in hrefs[1:] if h != path and h != path + '/']


async def upload_to_yandex_disk(file_path: Path) -> bool:
    """Загрузить файл на Яндекс.Диск через WebDAV"""
    if not YANDEX_LOGIN or not YANDEX_PASSWORD:
        logger.info("Яндекс.Диск не настроен, пропускаем загрузку")
        return False

    try:
        # Создаём папку
        await _ensure_webdav_dir(WEBDAV_BACKUP_DIR)

        # Загружаем файл
        remote_path = f"{WEBDAV_BACKUP_DIR}/{file_path.name}"
        with open(file_path, 'rb') as f:
            data = f.read()

        status, _ = await _webdav_request("PUT", remote_path, data=data)
        if status in (200, 201, 204):
            logger.info(f"Загружен на Яндекс.Диск: {remote_path}")
        else:
            logger.error(f"Ошибка загрузки на Яндекс.Диск: HTTP {status}")
            return False

        # Ротация старых бэкапов
        files = await _webdav_list_files(WEBDAV_BACKUP_DIR)
        # Фильтруем по префиксу имени файла (clients_backup_ / bot_db_backup_ / x-ui_backup_)
        prefix = file_path.name.rsplit('_', 1)[0].rsplit('_', 1)[0]  # e.g. "clients_backup"
        # Берём stem до даты
        import re
        m = re.match(r'^(.+?)_\d{4}-\d{2}-\d{2}', file_path.name)
        prefix = m.group(1) if m else file_path.stem
        matching = sorted([f for f in files if prefix in f], reverse=True)

        for old_file in matching[BACKUP_KEEP_DAYS:]:
            await _webdav_request("DELETE", old_file)
            logger.info(f"Удалён старый бэкап с Яндекс.Диска: {old_file}")

        return True

    except Exception as e:
        logger.error(f"Ошибка загрузки на Яндекс.Диск: {e}")
        return False


async def backup_remote_panels(bot: Bot):
    """Бэкап баз X-UI со всех доступных удалённых серверов"""
    config = load_servers_config()
    active_servers = [
        s for s in config.get('servers', [])
        if s.get('enabled', True) and s.get('panel')
    ]

    if not active_servers:
        logger.info("Нет доступных серверов с панелями для бэкапа")
        return

    backup_dir = Path("/root/manager_vpn/backups")
    backup_dir.mkdir(exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d_%H-%M")

    results = []
    for server in active_servers:
        server_name = server.get('name', 'Unknown')
        panel = server.get('panel', {})
        ip = server.get('ip', '')
        port = panel.get('port', 1020)
        path = panel.get('path', '')
        username = panel.get('username', '')
        password = panel.get('password', '')

        if not all([ip, username, password]):
            results.append(f"⚠️ {server_name}: неполные данные панели")
            continue

        try:
            import ssl
            import http.cookiejar
            import urllib.request
            import urllib.parse

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
                f"{base_url}/login", data=login_data, method='POST'
            )
            login_req.add_header('Content-Type', 'application/x-www-form-urlencoded')

            loop = asyncio.get_event_loop()
            login_resp = await loop.run_in_executor(
                None, functools.partial(opener.open, login_req, timeout=PANEL_REQUEST_TIMEOUT)
            )
            login_result = json.loads(login_resp.read())
            if not login_result.get('success'):
                results.append(f"❌ {server_name}: ошибка авторизации")
                continue

            # Скачиваем базу
            db_req = urllib.request.Request(f"{base_url}/panel/api/server/getDb", method='GET')
            db_resp = await loop.run_in_executor(
                None, functools.partial(opener.open, db_req, timeout=30)
            )
            db_data = db_resp.read()

            if len(db_data) < 100:
                results.append(f"❌ {server_name}: пустой ответ ({len(db_data)} байт)")
                continue

            # Безопасное имя файла
            safe_name = server_name.replace(' ', '_').replace('/', '_').replace(':', '')
            backup_file = backup_dir / f"xui_{safe_name}_{date_str}.db"
            backup_file.write_bytes(db_data)

            size_kb = len(db_data) / 1024
            results.append(f"✅ {server_name}: {size_kb:.1f} KB")
            logger.info(f"Бэкап панели {server_name} сохранён: {backup_file}")

            # Загрузка на Яндекс.Диск
            await upload_to_yandex_disk(backup_file)

            # Ротация локальных бэкапов этого сервера
            pattern = f"xui_{safe_name}_*.db"
            old_backups = sorted(backup_dir.glob(pattern), key=lambda x: x.stat().st_mtime, reverse=True)
            for old in old_backups[7:]:
                old.unlink()
                logger.info(f"Удалён старый бэкап панели: {old}")

        except Exception as e:
            results.append(f"❌ {server_name}: {e}")
            logger.error(f"Ошибка бэкапа панели {server_name}: {e}")

    # Отчёт админу
    if results:
        report = (
            f"💾 <b>Бэкап панелей X-UI</b>\n"
            f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
            + "\n".join(results)
        )
        try:
            await bot.send_message(ADMIN_ID, report, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Не удалось отправить отчёт о бэкапе панелей: {e}")


async def daily_backup_task(bot: Bot):
    """Ежедневный бэкап базы X-UI в 2:00"""
    while True:
        try:
            now = datetime.now()
            # Вычисляем время до 2:00
            target_time = datetime.combine(now.date(), time(2, 0))
            # Используем timedelta из datetime
            from datetime import timedelta
            if now >= target_time:
                # Если уже прошло 2:00, планируем на следующий день
                target_time = target_time + timedelta(days=1)

            wait_seconds = (target_time - now).total_seconds()
            if wait_seconds < 0:
                wait_seconds = 86400 + wait_seconds  # 24 часа

            logger.info(f"Следующий бэкап через {wait_seconds/3600:.1f} часов")
            await asyncio.sleep(wait_seconds)

            # Выполняем бэкап
            await send_xui_backup(bot)
            await backup_remote_panels(bot)
            await create_clients_backup(bot)

        except asyncio.CancelledError:
            logger.info("Задача бэкапа отменена")
            break
        except Exception as e:
            logger.error(f"Ошибка в задаче бэкапа: {e}")
            await asyncio.sleep(3600)  # Повторить через час при ошибке


async def send_xui_backup(bot: Bot):
    """Отправить бэкап базы X-UI админу"""
    try:
        if not XUI_DB_PATH.exists():
            logger.warning(f"База X-UI не найдена: {XUI_DB_PATH}")
            await bot.send_message(ADMIN_ID, "⚠️ База X-UI не найдена для бэкапа")
            return

        # Копируем файл с датой в имени
        backup_dir = Path("/root/manager_vpn/backups")
        backup_dir.mkdir(exist_ok=True)

        date_str = datetime.now().strftime("%Y-%m-%d_%H-%M")
        backup_file = backup_dir / f"x-ui_backup_{date_str}.db"

        shutil.copy2(XUI_DB_PATH, backup_file)

        # Отправляем файл админу
        document = FSInputFile(backup_file)
        await bot.send_document(
            ADMIN_ID,
            document,
            caption=f"💾 <b>Ежедневный бэкап X-UI</b>\n\n"
                    f"📅 Дата: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                    f"📦 Размер: {backup_file.stat().st_size / 1024:.1f} KB",
            parse_mode="HTML"
        )

        logger.info(f"Бэкап X-UI отправлен: {backup_file}")

        # Загрузка на Яндекс.Диск
        await upload_to_yandex_disk(backup_file)

        # Удаляем старые бэкапы (оставляем только 7 последних)
        backups = sorted(backup_dir.glob("x-ui_backup_*.db"), key=lambda x: x.stat().st_mtime, reverse=True)
        for old_backup in backups[7:]:
            old_backup.unlink()
            logger.info(f"Удалён старый бэкап: {old_backup}")

    except Exception as e:
        logger.error(f"Ошибка отправки бэкапа X-UI: {e}")
        try:
            await bot.send_message(ADMIN_ID, f"❌ Ошибка бэкапа X-UI: {e}")
        except:
            pass

    # Бэкап bot_database.db
    try:
        bot_db_path = Path(DATABASE_PATH)
        if bot_db_path.exists():
            backup_dir = Path("/root/manager_vpn/backups")
            backup_dir.mkdir(exist_ok=True)

            date_str = datetime.now().strftime("%Y-%m-%d_%H-%M")
            bot_db_backup = backup_dir / f"bot_db_backup_{date_str}.db"

            shutil.copy2(bot_db_path, bot_db_backup)

            document = FSInputFile(bot_db_backup)
            await bot.send_document(
                ADMIN_ID,
                document,
                caption=f"💾 <b>Бэкап bot_database.db</b>\n\n"
                        f"📅 Дата: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                        f"📦 Размер: {bot_db_backup.stat().st_size / 1024:.1f} KB",
                parse_mode="HTML"
            )
            logger.info(f"Бэкап bot_database отправлен: {bot_db_backup}")

            # Загрузка на Яндекс.Диск
            await upload_to_yandex_disk(bot_db_backup)

            # Ротация: оставляем 7 последних
            bot_backups = sorted(backup_dir.glob("bot_db_backup_*.db"), key=lambda x: x.stat().st_mtime, reverse=True)
            for old in bot_backups[7:]:
                old.unlink()
                logger.info(f"Удалён старый бэкап bot_db: {old}")
        else:
            logger.warning(f"bot_database.db не найдена: {bot_db_path}")
    except Exception as e:
        logger.error(f"Ошибка бэкапа bot_database: {e}")
        try:
            await bot.send_message(ADMIN_ID, f"❌ Ошибка бэкапа bot_database: {e}")
        except:
            pass


async def create_clients_backup(bot: Bot):
    """Полный JSON-бэкап клиентов со списком серверов"""
    try:
        backup_dir = Path("/root/manager_vpn/backups")
        backup_dir.mkdir(exist_ok=True)

        # 1. Читаем все записи из keys_history
        keys_data = []
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                'SELECT id, manager_id, client_email, phone_number, period, expire_days, '
                'client_id, price, server_name, created_at FROM keys_history ORDER BY created_at DESC'
            )
            keys_data = [dict(row) for row in await cursor.fetchall()]

        # 2. Для каждого активного сервера получаем список клиентов
        config = load_servers_config()
        active_servers = [s for s in config.get('servers', []) if s.get('enabled', True)]

        servers_info = {}
        all_panel_clients = {}  # email -> list of server_names
        for server in active_servers:
            server_name = server.get('name', 'Unknown')
            try:
                clients = await get_all_clients_from_panel(server)
                client_emails = [c.get('email', '') for c in clients if c.get('email')]
                servers_info[server_name] = {
                    'total_clients': len(client_emails),
                    'clients': client_emails
                }
                for email in client_emails:
                    if email not in all_panel_clients:
                        all_panel_clients[email] = []
                    all_panel_clients[email].append(server_name)
            except Exception as e:
                logger.error(f"Ошибка получения клиентов с {server_name}: {e}")
                servers_info[server_name] = {'total_clients': 0, 'clients': [], 'error': str(e)}

        # 3. Формируем JSON
        clients_list = []
        for key in keys_data:
            email = key.get('client_email', '')
            clients_list.append({
                'uuid': key.get('client_id'),
                'email': email,
                'phone': key.get('phone_number'),
                'period': key.get('period'),
                'expire_days': key.get('expire_days'),
                'created_at': key.get('created_at'),
                'price': key.get('price'),
                'manager_id': key.get('manager_id'),
                'server_name': key.get('server_name'),
                'servers_found_on': all_panel_clients.get(email, [])
            })

        # Получаем linked_clients
        linked_clients = []
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute('SELECT master_uuid, linked_uuid, linked_at FROM linked_clients')
            linked_clients = [dict(row) for row in await cursor.fetchall()]

        # Получаем client_servers (привязка клиентов к серверам)
        client_servers = []
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                'SELECT client_uuid, client_email, server_name, inbound_id, '
                'expire_days, expire_timestamp, total_gb, ip_limit, created_at '
                'FROM client_servers ORDER BY created_at DESC'
            )
            client_servers = [dict(row) for row in await cursor.fetchall()]

        backup_data = {
            'backup_date': datetime.now().isoformat(),
            'clients': clients_list,
            'servers': servers_info,
            'linked_clients': linked_clients,
            'client_servers': client_servers,
            'stats': {
                'total_keys': len(keys_data),
                'active_servers': len(active_servers),
                'client_server_records': len(client_servers),
            }
        }

        # 4. Сохраняем JSON
        date_str = datetime.now().strftime("%Y-%m-%d")
        json_file = backup_dir / f"clients_backup_{date_str}.json"
        json_content = json.dumps(backup_data, ensure_ascii=False, indent=2, default=str)
        json_file.write_text(json_content, encoding='utf-8')

        backup_data['stats']['backup_size_kb'] = round(json_file.stat().st_size / 1024, 1)
        json_content = json.dumps(backup_data, ensure_ascii=False, indent=2, default=str)
        json_file.write_text(json_content, encoding='utf-8')

        # 5. Отправляем файл админу
        document = FSInputFile(json_file)
        await bot.send_document(
            ADMIN_ID,
            document,
            caption=f"📋 <b>Бэкап клиентов со списком серверов</b>\n\n"
                    f"📅 Дата: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
                    f"🔑 Ключей: {len(keys_data)}\n"
                    f"🌐 Серверов: {len(active_servers)}\n"
                    f"📦 Размер: {json_file.stat().st_size / 1024:.1f} KB",
            parse_mode="HTML"
        )
        logger.info(f"JSON бэкап клиентов отправлен: {json_file}")

        # 5.1 Загрузка на Яндекс.Диск
        yd_ok = await upload_to_yandex_disk(json_file)
        if yd_ok:
            await bot.send_message(ADMIN_ID, f"☁️ Бэкап клиентов загружен на Яндекс.Диск")

        # 6. Ротация: 7 последних JSON файлов
        json_backups = sorted(backup_dir.glob("clients_backup_*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
        for old in json_backups[7:]:
            old.unlink()
            logger.info(f"Удалён старый JSON бэкап: {old}")

    except Exception as e:
        logger.error(f"Ошибка JSON бэкапа клиентов: {e}")
        try:
            await bot.send_message(ADMIN_ID, f"❌ Ошибка JSON бэкапа клиентов: {e}")
        except:
            pass


async def monthly_traffic_reset_task(bot: Bot):
    """Ежемесячный сброс трафика для серверов с лимитом (1-го числа в 3:00)"""
    while True:
        try:
            now = datetime.now()
            from datetime import timedelta
            # Вычисляем дату 1-го числа следующего месяца в 3:00
            if now.month == 12:
                target = datetime(now.year + 1, 1, 1, 3, 0)
            else:
                target = datetime(now.year, now.month + 1, 1, 3, 0)

            wait_seconds = (target - now).total_seconds()
            logger.info(f"Следующий сброс трафика через {wait_seconds/3600:.1f} часов ({target.strftime('%Y-%m-%d %H:%M')})")
            await asyncio.sleep(wait_seconds)

            # Загружаем конфиг серверов
            config = load_servers_config()
            servers_with_limit = [
                s for s in config.get('servers', [])
                if s.get('enabled', True) and s.get('traffic_limit_gb', 0) > 0
            ]

            if not servers_with_limit:
                logger.info("Нет серверов с лимитом трафика для сброса")
                continue

            report_lines = []
            for server in servers_with_limit:
                server_name = server.get('name', 'Unknown')
                limit_gb = server.get('traffic_limit_gb', 0)
                logger.info(f"Сброс трафика на сервере {server_name} (лимит: {limit_gb} ГБ)")

                # Получаем всех клиентов
                clients = await get_all_clients_from_panel(server)
                if not clients:
                    report_lines.append(f"⚠️ {server_name}: нет клиентов или ошибка получения списка")
                    continue

                success_count = 0
                fail_count = 0
                for client in clients:
                    email = client.get('email', '')
                    inbound_id = client.get('inbound_id')
                    ok = await reset_client_traffic_via_panel(server, email, inbound_id)
                    if ok:
                        success_count += 1
                    else:
                        fail_count += 1
                    await asyncio.sleep(0.1)  # Небольшая пауза между запросами

                line = f"✅ {server_name}: сброшено {success_count}/{len(clients)}"
                if fail_count > 0:
                    line += f" (ошибок: {fail_count})"
                report_lines.append(line)
                logger.info(f"Сброс трафика на {server_name}: {success_count} успешно, {fail_count} ошибок")

            # Отправляем отчёт админу
            report = (
                f"🔄 <b>Ежемесячный сброс трафика</b>\n"
                f"📅 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                + "\n".join(report_lines)
            )
            try:
                await bot.send_message(ADMIN_ID, report, parse_mode="HTML")
            except Exception as e:
                logger.error(f"Не удалось отправить отчёт о сбросе трафика: {e}")

        except asyncio.CancelledError:
            logger.info("Задача сброса трафика отменена")
            break
        except Exception as e:
            logger.error(f"Ошибка в задаче сброса трафика: {e}")
            await asyncio.sleep(3600)  # Повторить через час при ошибке


async def expiry_notification_task(bot: Bot, db: DatabaseManager):
    """Ежедневные уведомления менеджерам об истекающих ключах (в 10:00)"""
    while True:
        try:
            now = datetime.now()
            target_time = datetime.combine(now.date(), time(10, 0))
            from datetime import timedelta
            if now >= target_time:
                target_time = target_time + timedelta(days=1)

            wait_seconds = (target_time - now).total_seconds()
            logger.info(f"Следующая проверка истекающих ключей через {wait_seconds/3600:.1f} часов")
            await asyncio.sleep(wait_seconds)

            # Ищем ключи, истекающие в ближайшие 7 дней, ещё не уведомлённые
            async with aiosqlite.connect(DATABASE_PATH) as conn:
                conn.row_factory = aiosqlite.Row
                cursor = await conn.execute('''
                    SELECT kh.id, kh.manager_id, kh.client_email, kh.phone_number,
                           kh.expire_days, kh.created_at,
                           DATE(kh.created_at, '+' || kh.expire_days || ' days') as expire_date
                    FROM keys_history kh
                    WHERE kh.expiry_notified = 0
                      AND kh.expire_days > 0
                      AND DATE(kh.created_at, '+' || kh.expire_days || ' days') BETWEEN DATE('now') AND DATE('now', '+7 days')
                ''')
                expiring_keys = [dict(row) for row in await cursor.fetchall()]

            if not expiring_keys:
                logger.info("Нет истекающих ключей для уведомления")
                continue

            # Группируем по manager_id
            by_manager = {}
            for key in expiring_keys:
                mid = key['manager_id']
                if mid not in by_manager:
                    by_manager[mid] = []
                by_manager[mid].append(key)

            # Отправляем уведомления каждому менеджеру
            notified_ids = []
            for manager_id, keys in by_manager.items():
                lines = []
                for k in keys:
                    name = k.get('client_email') or k.get('phone_number') or 'Без имени'
                    expire_date = k.get('expire_date', '?')
                    try:
                        dt = datetime.strptime(expire_date, '%Y-%m-%d')
                        expire_formatted = dt.strftime('%d.%m.%Y')
                    except Exception:
                        expire_formatted = expire_date
                    lines.append(f"• {name} ({k['expire_days']} дн.) — истекает {expire_formatted}")

                text = "⏰ <b>Скоро заканчиваются подписки:</b>\n\n"
                text += "\n".join(lines)
                text += "\n\nСвяжитесь с клиентами для продления!"

                try:
                    await bot.send_message(manager_id, text, parse_mode="HTML")
                    notified_ids.extend([k['id'] for k in keys])
                    logger.info(f"Уведомление об истечении отправлено менеджеру {manager_id} ({len(keys)} ключей)")
                except Exception as e:
                    logger.error(f"Не удалось отправить уведомление менеджеру {manager_id}: {e}")

            # Помечаем уведомлённые ключи
            if notified_ids:
                async with aiosqlite.connect(DATABASE_PATH) as conn:
                    placeholders = ','.join('?' * len(notified_ids))
                    await conn.execute(
                        f'UPDATE keys_history SET expiry_notified = 1 WHERE id IN ({placeholders})',
                        notified_ids
                    )
                    await conn.commit()
                logger.info(f"Помечено {len(notified_ids)} ключей как уведомлённые")

        except asyncio.CancelledError:
            logger.info("Задача уведомлений об истечении отменена")
            break
        except Exception as e:
            logger.error(f"Ошибка в задаче уведомлений об истечении: {e}")
            await asyncio.sleep(3600)


async def retry_pending_keys_task(bot: Bot, db: DatabaseManager, xui_client: XUIClient):
    """Фоновая задача для повторной попытки создания ключей"""
    # Ждём 30 секунд после старта бота перед первой проверкой
    await asyncio.sleep(30)

    while True:
        try:
            # Получаем список отложенных ключей
            pending_keys = await db.get_pending_keys(limit=5)

            for pending in pending_keys:
                try:
                    logger.info(f"Retry создания ключа #{pending['id']} для {pending['phone']}")

                    # Пытаемся создать ключ
                    client_data = await xui_client.add_client(
                        inbound_id=pending['inbound_id'] or INBOUND_ID,
                        email=pending['phone'],
                        phone=pending['phone'],
                        expire_days=pending['period_days'],
                        ip_limit=2
                    )

                    if client_data and not client_data.get('error'):
                        # Успешно создан
                        client_uuid = client_data.get('client_id', '')

                        # Получаем ссылку
                        vless_link = await get_client_link_from_active_server(
                            client_uuid=client_uuid,
                            client_email=pending['phone']
                        )

                        # Отмечаем как выполненный
                        await db.mark_pending_key_completed(pending['id'], client_uuid)

                        # Сохраняем в историю
                        await db.add_key_to_history(
                            manager_id=pending['telegram_id'],
                            client_email=pending['phone'],
                            phone_number=pending['phone'],
                            period=pending['period_name'],
                            expire_days=pending['period_days'],
                            client_id=client_uuid,
                            price=pending['period_price'] or 0
                        )

                        # Отправляем уведомление пользователю
                        try:
                            if vless_link:
                                await bot.send_message(
                                    pending['telegram_id'],
                                    f"✅ <b>Ваш ключ готов!</b>\n\n"
                                    f"🆔 ID: <code>{pending['phone']}</code>\n"
                                    f"📦 Тариф: {pending['period_name']}\n"
                                    f"⏱ Срок: {pending['period_days']} дней\n\n"
                                    f"🔑 <b>Ваш ключ:</b>\n<code>{vless_link}</code>\n\n"
                                    f"📋 Нажмите на ключ чтобы скопировать",
                                    parse_mode="HTML"
                                )
                            else:
                                await bot.send_message(
                                    pending['telegram_id'],
                                    f"✅ <b>Ваш ключ создан!</b>\n\n"
                                    f"🆔 ID: <code>{pending['phone']}</code>\n"
                                    f"📦 Тариф: {pending['period_name']}\n\n"
                                    f"⚠️ Не удалось получить ссылку. Обратитесь к администратору.",
                                    parse_mode="HTML"
                                )
                        except Exception as e:
                            logger.error(f"Не удалось отправить уведомление пользователю {pending['telegram_id']}: {e}")

                        logger.info(f"Ключ #{pending['id']} успешно создан для {pending['phone']}")

                    elif client_data and client_data.get('is_duplicate'):
                        # Дубликат - отмечаем как завершённый
                        await db.mark_pending_key_completed(pending['id'])
                        try:
                            await bot.send_message(
                                pending['telegram_id'],
                                f"⚠️ Клиент <code>{pending['phone']}</code> уже существует в системе.",
                                parse_mode="HTML"
                            )
                        except:
                            pass
                        logger.info(f"Ключ #{pending['id']} - дубликат")

                    else:
                        # Ошибка - обновляем счётчик retry
                        error = client_data.get('message', 'Unknown error') if client_data else 'Server unavailable'
                        await db.update_pending_key_retry(pending['id'], error)

                        # Проверяем, достигнут ли лимит попыток
                        if pending['retry_count'] + 1 >= pending['max_retries']:
                            await db.mark_pending_key_failed(pending['id'])
                            try:
                                await bot.send_message(
                                    pending['telegram_id'],
                                    f"❌ <b>Не удалось создать ключ</b>\n\n"
                                    f"🆔 ID: <code>{pending['phone']}</code>\n"
                                    f"📦 Тариф: {pending['period_name']}\n\n"
                                    f"После нескольких попыток ключ не удалось создать.\n"
                                    f"Пожалуйста, обратитесь к администратору.",
                                    parse_mode="HTML"
                                )
                                # Уведомляем админа
                                await bot.send_message(
                                    ADMIN_ID,
                                    f"🚨 <b>Ключ не создан после {pending['max_retries']} попыток</b>\n\n"
                                    f"👤 User: {pending['telegram_id']} (@{pending['username']})\n"
                                    f"🆔 ID: <code>{pending['phone']}</code>\n"
                                    f"📦 Тариф: {pending['period_name']}\n"
                                    f"❌ Ошибка: {error}",
                                    parse_mode="HTML"
                                )
                            except:
                                pass
                            logger.error(f"Ключ #{pending['id']} - достигнут лимит retry")
                        else:
                            logger.warning(f"Ключ #{pending['id']} - попытка {pending['retry_count']+1}/{pending['max_retries']}")

                    # Небольшая пауза между ключами
                    await asyncio.sleep(2)

                except Exception as e:
                    logger.error(f"Ошибка обработки pending key #{pending['id']}: {e}")
                    await db.update_pending_key_retry(pending['id'], str(e))

            # Ждём 2 минуты перед следующей проверкой
            await asyncio.sleep(120)

        except asyncio.CancelledError:
            logger.info("Задача retry отменена")
            break
        except Exception as e:
            logger.error(f"Ошибка в задаче retry: {e}")
            await asyncio.sleep(60)


async def restore_clients_from_backup(backup_path: str, target_server_name: str = None, bot: Bot = None) -> dict:
    """
    Восстановить клиентов на серверах из JSON-бэкапа.

    :param backup_path: путь к JSON-файлу бэкапа
    :param target_server_name: если указано — восстановить только на этот сервер
    :param bot: бот для отправки отчёта
    :return: {'restored': int, 'skipped': int, 'errors': int, 'details': [...]}
    """
    from bot.api.remote_xui import create_client_on_remote_server, load_servers_config

    with open(backup_path, 'r', encoding='utf-8') as f:
        backup_data = json.load(f)

    client_servers_data = backup_data.get('client_servers', [])
    if not client_servers_data:
        return {'restored': 0, 'skipped': 0, 'errors': 0, 'details': ['Нет данных client_servers в бэкапе']}

    # Загружаем конфиг серверов
    config = load_servers_config()
    servers_map = {s.get('name'): s for s in config.get('servers', []) if s.get('enabled', True) and s.get('panel')}

    restored = 0
    skipped = 0
    errors = 0
    details = []

    for record in client_servers_data:
        client_uuid = record.get('client_uuid')
        client_email = record.get('client_email', '')
        server_name = record.get('server_name', '')
        expire_days = record.get('expire_days', 30)
        expire_timestamp = record.get('expire_timestamp', 0)
        total_gb = record.get('total_gb', 0)
        ip_limit = record.get('ip_limit', 2)
        inbound_id = record.get('inbound_id', 1)

        if not client_uuid or not server_name:
            skipped += 1
            continue

        # Пропускаем истёкшие подписки
        if expire_timestamp > 0:
            now_ms = int(datetime.now().timestamp() * 1000)
            if expire_timestamp < now_ms:
                skipped += 1
                details.append(f"⏭ {client_email}: подписка истекла")
                continue

        # Если указан целевой сервер — восстанавливаем только на него
        actual_server_name = target_server_name or server_name

        server_config = servers_map.get(actual_server_name)
        if not server_config:
            skipped += 1
            details.append(f"⏭ {client_email}: сервер {actual_server_name} не найден/отключен")
            continue

        try:
            result = await create_client_on_remote_server(
                server_config=server_config,
                client_uuid=client_uuid,
                email=client_email,
                expire_days=expire_days,
                ip_limit=ip_limit,
                inbound_id=inbound_id,
                total_gb=total_gb,
                expire_time_ms=expire_timestamp if expire_timestamp > 0 else None
            )
            if result.get('success') or result.get('existing'):
                restored += 1
                logger.info(f"Восстановлен {client_email} на {actual_server_name}")
            else:
                errors += 1
                details.append(f"❌ {client_email} → {actual_server_name}: {result.get('error', 'unknown')}")
        except Exception as e:
            errors += 1
            details.append(f"❌ {client_email} → {actual_server_name}: {e}")

        await asyncio.sleep(0.2)  # Пауза между запросами

    summary = f"Восстановлено: {restored}, пропущено: {skipped}, ошибок: {errors}"
    logger.info(f"Восстановление из бэкапа завершено: {summary}")

    return {'restored': restored, 'skipped': skipped, 'errors': errors, 'details': details}


async def backfill_server_names(db: DatabaseManager):
    """Одноразовое ретроспективное заполнение server_name для существующих ключей"""
    flag = await db.get_setting('server_name_backfill_done')
    if flag == '1':
        return

    logger.info("Запуск ретроспективного заполнения server_name...")

    try:
        # Получаем ключи без server_name
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                'SELECT id, client_id FROM keys_history WHERE server_name IS NULL AND client_id IS NOT NULL'
            )
            keys_without_server = [dict(row) for row in await cursor.fetchall()]

        if not keys_without_server:
            logger.info("Нет ключей без server_name, пропускаем")
            await db.set_setting('server_name_backfill_done', '1')
            return

        logger.info(f"Найдено {len(keys_without_server)} ключей без server_name")

        config = load_servers_config()
        active_servers = [s for s in config.get('servers', []) if s.get('enabled', True) and s.get('panel')]

        # Получаем все клиенты со всех серверов разом
        server_clients_map = {}  # email -> server_name
        for server in active_servers:
            server_name = server.get('name', 'Unknown')
            try:
                clients = await get_all_clients_from_panel(server)
                for c in clients:
                    email = c.get('email', '')
                    if email and email not in server_clients_map:
                        server_clients_map[email] = server_name
            except Exception as e:
                logger.error(f"Ошибка получения клиентов с {server_name} при backfill: {e}")

        # Получаем email для каждого ключа
        updated = 0
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                'SELECT id, client_id, client_email FROM keys_history WHERE server_name IS NULL AND client_id IS NOT NULL'
            )
            keys = [dict(row) for row in await cursor.fetchall()]

            for key in keys:
                email = key.get('client_email', '')
                found_server = server_clients_map.get(email)
                if found_server:
                    await conn.execute(
                        'UPDATE keys_history SET server_name = ? WHERE id = ?',
                        (found_server, key['id'])
                    )
                    updated += 1

            await conn.commit()

        logger.info(f"Ретроспективное заполнение завершено: обновлено {updated}/{len(keys_without_server)} ключей")
        await db.set_setting('server_name_backfill_done', '1')

    except Exception as e:
        logger.error(f"Ошибка ретроспективного заполнения server_name: {e}")


async def backfill_client_servers(db: DatabaseManager):
    """Одноразовое заполнение таблицы client_servers для существующих клиентов"""
    flag = await db.get_setting('client_servers_backfill_done')
    if flag == '1':
        return

    logger.info("Запуск заполнения client_servers для существующих клиентов...")

    try:
        # Получаем все ключи с client_id
        async with aiosqlite.connect(db.db_path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                'SELECT client_id, client_email, expire_days FROM keys_history '
                'WHERE client_id IS NOT NULL AND client_id != ""'
            )
            keys = [dict(row) for row in await cursor.fetchall()]

        if not keys:
            logger.info("Нет ключей для заполнения client_servers")
            await db.set_setting('client_servers_backfill_done', '1')
            return

        logger.info(f"Найдено {len(keys)} ключей для проверки")

        config = load_servers_config()
        active_servers = [s for s in config.get('servers', []) if s.get('enabled', True) and s.get('panel')]

        # Строим карту: email -> список серверов, где клиент найден (с expiry)
        email_servers = {}
        for server in active_servers:
            server_name = server.get('name', 'Unknown')
            try:
                clients = await get_all_clients_from_panel(server)
                for c in clients:
                    email = c.get('email', '')
                    if email:
                        if email not in email_servers:
                            email_servers[email] = []
                        email_servers[email].append({
                            'server_name': server_name,
                            'inbound_id': c.get('inbound_id', 1),
                            'expiryTime': c.get('expiryTime', 0),
                            'totalGB': c.get('totalGB', 0),
                            'limitIp': c.get('limitIp', 2),
                        })
            except Exception as e:
                logger.error(f"Ошибка получения клиентов с {server_name}: {e}")

        # Заполняем client_servers
        added = 0
        for key in keys:
            email = key.get('client_email', '')
            client_uuid = key.get('client_id', '')
            expire_days = key.get('expire_days', 0)
            servers = email_servers.get(email, [])

            for srv_info in servers:
                try:
                    await db.add_client_server(
                        client_uuid=client_uuid,
                        client_email=email,
                        server_name=srv_info['server_name'],
                        inbound_id=srv_info.get('inbound_id', 1),
                        expire_days=expire_days,
                        expire_timestamp=srv_info.get('expiryTime', 0),
                        total_gb=srv_info.get('totalGB', 0),
                        ip_limit=srv_info.get('limitIp', 2)
                    )
                    added += 1
                except Exception:
                    pass

        logger.info(f"Заполнение client_servers завершено: добавлено {added} записей")
        await db.set_setting('client_servers_backfill_done', '1')

    except Exception as e:
        logger.error(f"Ошибка заполнения client_servers: {e}")


async def main():
    """Основная функция запуска бота"""

    # Проверка конфигурации
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN не установлен в .env файле!")
        return

    if not XUI_HOST:
        logger.error("XUI_HOST не установлен в .env файле!")
        return

    logger.info("Запуск бота...")

    # Инициализация бота и диспетчера
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    storage = MemoryStorage()
    dp = Dispatcher(storage=storage)

    # Инициализация базы данных
    db = DatabaseManager(DATABASE_PATH)
    await db.init_db()
    logger.info("База данных инициализирована")

    # Ретроспективное заполнение server_name (одноразово)
    await backfill_server_names(db)
    await backfill_client_servers(db)

    # Автоматически добавляем админа как менеджера, если его нет
    if not await db.is_manager(ADMIN_ID):
        await db.add_manager(
            user_id=ADMIN_ID,
            username="admin",
            full_name="Администратор",
            added_by=ADMIN_ID
        )
        logger.info(f"Админ (ID: {ADMIN_ID}) автоматически добавлен в менеджеры")

    # Инициализация X-UI клиента
    xui_client = XUIClient(XUI_HOST, XUI_USERNAME, XUI_PASSWORD)

    # Проверка подключения к X-UI
    try:
        login_success = await xui_client.login()
        if login_success:
            logger.info("Успешное подключение к X-UI панели")
        else:
            logger.warning("Не удалось подключиться к X-UI панели. Проверьте настройки.")
    except Exception as e:
        logger.error(f"Ошибка подключения к X-UI: {e}")

    # Регистрация middleware
    dp.update.middleware(ThrottlingMiddleware(default_ttl=0.5))
    dp.update.middleware(BanCheckMiddleware(DATABASE_PATH))
    dp.update.middleware(MaintenanceMiddleware(admin_ids=[ADMIN_ID]))
    logger.info("Middleware зарегистрированы")

    # Middleware для передачи зависимостей
    @dp.update.middleware()
    async def db_middleware(handler, event, data):
        data['db'] = db
        data['xui_client'] = xui_client
        data['bot'] = bot
        return await handler(event, data)

    # Регистрация роутеров
    dp.include_router(common.router)
    dp.include_router(manager.router)
    dp.include_router(admin.router)
    dp.include_router(extended.router)

    logger.info("Обработчики зарегистрированы")

    # Запуск веб-сервера для Mini App
    try:
        # Передаем бота для уведомлений админу о веб-заказах
        set_bot_instance(bot, ADMIN_ID)
        webapp_runner = await start_webapp_server(WEBAPP_HOST, WEBAPP_PORT)
        logger.info("WebApp сервер запущен успешно")
    except Exception as e:
        logger.error(f"Ошибка запуска WebApp сервера: {e}")
        webapp_runner = None

    # Запуск задачи ежедневного бэкапа
    backup_task = asyncio.create_task(daily_backup_task(bot))
    logger.info("Задача ежедневного бэкапа X-UI запущена (в 2:00)")

    # Запуск задачи retry отложенных ключей
    retry_task = asyncio.create_task(retry_pending_keys_task(bot, db, xui_client))
    logger.info("Задача retry отложенных ключей запущена (каждые 2 минуты)")

    # Запуск задачи уведомлений об истечении ключей
    expiry_task = asyncio.create_task(expiry_notification_task(bot, db))
    logger.info("Задача уведомлений об истечении ключей запущена (ежедневно в 10:00)")

    # Запуск задачи ежемесячного сброса трафика
    traffic_reset_task = asyncio.create_task(monthly_traffic_reset_task(bot))
    logger.info("Задача ежемесячного сброса трафика запущена (1-го числа в 3:00)")

    # Запуск бота
    try:
        logger.info("Бот запущен и готов к работе")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        backup_task.cancel()
        retry_task.cancel()
        expiry_task.cancel()
        traffic_reset_task.cancel()
        try:
            await backup_task
        except asyncio.CancelledError:
            pass
        try:
            await retry_task
        except asyncio.CancelledError:
            pass
        try:
            await expiry_task
        except asyncio.CancelledError:
            pass
        try:
            await traffic_reset_task
        except asyncio.CancelledError:
            pass
        await bot.session.close()
        if xui_client.session:
            await xui_client.session.close()
        if webapp_runner:
            await webapp_runner.cleanup()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
