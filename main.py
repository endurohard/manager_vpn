"""
Главный файл бота для управления VPN ключами
"""
import asyncio
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

from bot.config import BOT_TOKEN, XUI_HOST, XUI_USERNAME, XUI_PASSWORD, DATABASE_PATH, WEBAPP_HOST, WEBAPP_PORT, ADMIN_ID, INBOUND_ID
from bot.database import DatabaseManager
from bot.api import XUIClient
from bot.handlers import common, manager, admin, extended
from bot.middlewares import BanCheckMiddleware, ThrottlingMiddleware, MaintenanceMiddleware
from bot.webapp.server import start_webapp_server, set_bot_instance
from bot.api.remote_xui import load_servers_config, get_client_link_from_active_server, get_all_clients_from_panel, reset_client_traffic_via_panel

# Путь к базе данных X-UI
XUI_DB_PATH = Path("/etc/x-ui/x-ui.db")

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)


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

        # Удаляем старые бэкапы (оставляем только 7 последних)
        backups = sorted(backup_dir.glob("x-ui_backup_*.db"), key=lambda x: x.stat().st_mtime, reverse=True)
        for old_backup in backups[7:]:
            old_backup.unlink()
            logger.info(f"Удалён старый бэкап: {old_backup}")

    except Exception as e:
        logger.error(f"Ошибка отправки бэкапа: {e}")
        try:
            await bot.send_message(ADMIN_ID, f"❌ Ошибка бэкапа X-UI: {e}")
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
