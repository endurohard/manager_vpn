"""
Общие обработчики команд
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.config import ADMIN_ID
from bot.database import DatabaseManager
from bot.utils import Keyboards
from bot.price_config import get_subscription_periods


class FixKeyStates(StatesGroup):
    """Состояния для исправления/переноса ключа"""
    waiting_for_key = State()

router = Router()


async def is_authorized(user_id: int, db: DatabaseManager) -> bool:
    """Проверка авторизации пользователя"""
    if user_id == ADMIN_ID:
        return True
    return await db.is_manager(user_id)


@router.message(Command("start"))
async def cmd_start(message: Message, db: DatabaseManager):
    """Обработчик команды /start"""
    user_id = message.from_user.id

    # Проверяем авторизацию
    if not await is_authorized(user_id, db):
        await message.answer(
            "У вас нет доступа к этому боту.\n"
            "Для получения доступа обратитесь к администратору."
        )
        return

    # Обновляем информацию о менеджере (username и имя)
    if await db.is_manager(user_id):
        username = message.from_user.username or ""
        first_name = message.from_user.first_name or ""
        last_name = message.from_user.last_name or ""
        full_name = f"{first_name} {last_name}".strip()

        await db.update_manager_info(user_id, username, full_name)

    is_admin = user_id == ADMIN_ID

    welcome_text = "Добро пожаловать в бот управления VPN ключами!\n\n"

    if is_admin:
        welcome_text += "Вы вошли как администратор.\n\n"

    welcome_text += (
        "Доступные функции:\n"
        "• Создать ключ - создание нового VLESS ключа\n"
        "• Моя статистика - просмотр статистики\n"
    )

    if is_admin:
        welcome_text += "\n• Панель администратора - управление менеджерами и статистика\n"

    await message.answer(
        welcome_text,
        reply_markup=Keyboards.main_menu(is_admin)
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    """Обработчик команды /help"""
    help_text = (
        "Помощь по использованию бота:\n\n"
        "/start - Главное меню\n"
        "/help - Эта справка\n\n"
        "Создание ключа:\n"
        "1. Нажмите 'Создать ключ'\n"
        "2. Введите номер телефона или сгенерируйте автоматически\n"
        "3. Выберите срок действия ключа\n"
        "4. Получите готовый VLESS ключ\n\n"
        "Ограничения для каждого ключа:\n"
        "• Максимум 2 IP адреса\n"
        "• Безлимитный трафик\n"
    )

    await message.answer(help_text)


@router.message(F.text == "Назад")
async def back_to_main(message: Message, state: FSMContext):
    """Возврат в главное меню"""
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID

    await state.clear()
    await message.answer(
        "Главное меню:",
        reply_markup=Keyboards.main_menu(is_admin)
    )


@router.message(F.text == "💰 Прайс")
async def show_price_list(message: Message, db: DatabaseManager):
    """Показать прайс-лист"""
    user_id = message.from_user.id

    # Проверяем авторизацию
    if not await is_authorized(user_id, db):
        await message.answer(
            "У вас нет доступа к этому боту.\n"
            "Для получения доступа обратитесь к администратору."
        )
        return

    price_text = "💰 <b>ПРАЙС-ЛИСТ VPN КЛЮЧЕЙ</b>\n\n"
    price_text += "🔐 <b>Тарифы на подключение:</b>\n\n"

    # Получаем актуальные цены и сортируем по количеству дней
    periods = get_subscription_periods()
    sorted_periods = sorted(periods.items(), key=lambda x: x[1]['days'])

    for key, info in sorted_periods:
        price_text += f"📅 <b>{info['name']}</b> ({info['days']} дней)\n"
        price_text += f"   💵 Цена: <b>{info['price']} ₽</b>\n"

        # Рассчитываем цену за день
        price_per_day = info['price'] / info['days']
        price_text += f"   📊 ~{price_per_day:.1f} ₽/день\n\n"

    price_text += "━━━━━━━━━━━━━━━━\n\n"
    price_text += "✨ <b>Что включено:</b>\n"
    price_text += "• 🌐 Безлимитный трафик\n"
    price_text += "• 📱 До 2 устройств одновременно\n"
    price_text += "• 🚀 Высокая скорость\n"
    price_text += "• 🔒 Полная конфиденциальность\n"
    price_text += "• 💬 Техподдержка 24/7\n\n"
    price_text += "━━━━━━━━━━━━━━━━\n\n"
    price_text += "💡 <i>Чем дольше срок подписки, тем выгоднее цена!</i>\n\n"
    price_text += "Для заказа нажмите <b>\"Создать ключ\"</b>"

    await message.answer(price_text, parse_mode="HTML")


@router.message(F.text == "📖 Инструкции")
async def show_instructions(message: Message, db: DatabaseManager):
    """Отправить ссылку на инструкции"""
    user_id = message.from_user.id

    # Проверяем авторизацию
    if not await is_authorized(user_id, db):
        await message.answer(
            "У вас нет доступа к этому боту.\n"
            "Для получения доступа обратитесь к администратору."
        )
        return

    from bot.config import WEBAPP_URL

    instructions_text = (
        "📖 <b>Инструкции по настройке VPN</b>\n\n"
        "Здесь вы найдете подробные инструкции по настройке VPN для всех платформ:\n\n"
        "📱 iOS (iPhone/iPad)\n"
        "🤖 Android\n"
        "💻 Windows\n"
        "🍎 macOS\n"
        "🐧 Linux\n\n"
        f"👉 <a href='{WEBAPP_URL}'>Открыть инструкции</a>"
    )

    await message.answer(instructions_text, parse_mode="HTML", disable_web_page_preview=False)


@router.message(F.text == "🔧 Исправить ключ")
async def fix_key_start(message: Message, state: FSMContext, db: DatabaseManager):
    """Начало процесса исправления/переноса ключа"""
    user_id = message.from_user.id

    # Проверяем авторизацию
    if not await is_authorized(user_id, db):
        await message.answer(
            "У вас нет доступа к этому боту.\n"
            "Для получения доступа обратитесь к администратору."
        )
        return

    fix_text = (
        "🔧 <b>Исправление / Перенос ключа</b>\n\n"
        "Отправьте ваш VLESS ключ (начинается с <code>vless://</code>)\n\n"
        "<b>Что произойдёт:</b>\n"
        "1️⃣ Система найдёт клиента в базе данных\n"
        "2️⃣ Если ключ со старого сервера - создаст новый с сохранением срока\n"
        "3️⃣ Если ключ с текущего сервера - исправит настройки\n\n"
        "💡 <i>Просто вставьте ключ в чат и отправьте</i>"
    )

    await state.set_state(FixKeyStates.waiting_for_key)
    await message.answer(
        fix_text,
        parse_mode="HTML",
        reply_markup=Keyboards.cancel()
    )


@router.message(FixKeyStates.waiting_for_key)
async def fix_key_process(message: Message, state: FSMContext, db: DatabaseManager, xui_client):
    """Обработка введённого ключа"""
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID

    # Проверяем отмену
    if message.text == "Отмена":
        await state.clear()
        await message.answer(
            "Операция отменена.",
            reply_markup=Keyboards.main_menu(is_admin)
        )
        return

    key = message.text.strip()

    # Проверяем формат ключа
    if not key.startswith("vless://"):
        await message.answer(
            "❌ Неверный формат ключа.\n"
            "Ключ должен начинаться с <code>vless://</code>\n\n"
            "Попробуйте ещё раз или нажмите Отмена",
            parse_mode="HTML"
        )
        return

    # Извлекаем UUID из ключа
    try:
        link_without_proto = key[8:]
        if '@' in link_without_proto:
            uuid_part = link_without_proto.split('@')[0]
        else:
            await message.answer(
                "❌ Неверный формат ключа: отсутствует UUID\n\n"
                "Попробуйте ещё раз или нажмите Отмена"
            )
            return
    except Exception as e:
        await message.answer(
            f"❌ Ошибка парсинга ключа: {str(e)}\n\n"
            "Попробуйте ещё раз или нажмите Отмена"
        )
        return

    await message.answer("🔍 Ищу клиента в базе данных...")

    # Сначала ищем в старом бэкапе для миграции
    from bot.webapp.server import find_client_in_old_backup

    old_client = find_client_in_old_backup(uuid_part)

    if old_client:
        # Нашли в старой базе - выполняем миграцию
        await migrate_old_client(message, state, old_client, xui_client, is_admin)
    else:
        # Ищем в текущей базе для исправления
        await fix_current_client(message, state, uuid_part, xui_client, is_admin)


async def migrate_old_client(message: Message, state: FSMContext, old_client: dict, xui_client, is_admin: bool):
    """Миграция клиента со старого сервера"""
    from datetime import datetime, timedelta

    client_email = old_client.get('email', '')
    expiry_time_ms = old_client.get('expiryTime', 0)
    limit_ip = old_client.get('limitIp', 2)

    if limit_ip <= 0:
        limit_ip = 2

    # Вычисляем оставшееся время подписки
    if expiry_time_ms <= 0:
        days_left = 365
        expiry_date = datetime.now() + timedelta(days=365)
    else:
        expiry_timestamp = expiry_time_ms / 1000
        expiry_date = datetime.fromtimestamp(expiry_timestamp)
        now = datetime.now()

        if expiry_date > now:
            days_left = (expiry_date - now).days + 1
        else:
            days_left = 7
            expiry_date = now + timedelta(days=7)

    await message.answer(
        f"✅ <b>Найден клиент в старой базе!</b>\n\n"
        f"👤 Имя: <b>{client_email}</b>\n"
        f"📅 Срок до: <b>{expiry_date.strftime('%Y-%m-%d')}</b>\n"
        f"📱 Лимит устройств: <b>{limit_ip}</b>\n"
        f"⏳ Осталось дней: <b>{days_left}</b>\n\n"
        f"⏳ Создаю новый ключ...",
        parse_mode="HTML"
    )

    try:
        # Создаём нового клиента
        inbound_id = 1

        result = await xui_client.add_client(
            inbound_id=inbound_id,
            email=client_email,
            phone="",
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

            subscription_url = f"https://zov-gor.ru:8080/sub/{new_uuid}"

            success_text = (
                f"🎉 <b>Ключ успешно перенесён!</b>\n\n"
                f"👤 Клиент: <b>{client_email}</b>\n"
                f"📅 Действует до: <b>{expiry_date.strftime('%Y-%m-%d')}</b>\n"
                f"📱 Устройств: <b>{limit_ip}</b>\n\n"
                f"━━━━━━━━━━━━━━━━\n\n"
                f"🔑 <b>Новый VLESS ключ:</b>\n"
                f"<code>{new_vless_link}</code>\n\n"
                f"━━━━━━━━━━━━━━━━\n\n"
                f"📲 <b>Ссылка подписки:</b>\n"
                f"<code>{subscription_url}</code>\n\n"
                f"💡 <i>Скопируйте ключ и добавьте в приложение</i>"
            )

            await state.clear()
            await message.answer(
                success_text,
                parse_mode="HTML",
                reply_markup=Keyboards.main_menu(is_admin)
            )

        elif result and result.get('error'):
            error_msg = result.get('message', 'Неизвестная ошибка')
            if result.get('is_duplicate'):
                await state.clear()
                await message.answer(
                    f"⚠️ <b>Клиент уже существует</b>\n\n"
                    f"Клиент с именем <b>{client_email}</b> уже был перенесён ранее.\n\n"
                    f"Если вам нужен новый ключ, обратитесь к администратору.",
                    parse_mode="HTML",
                    reply_markup=Keyboards.main_menu(is_admin)
                )
            else:
                await message.answer(
                    f"❌ Ошибка создания клиента: {error_msg}\n\n"
                    "Попробуйте ещё раз или обратитесь к администратору."
                )
        else:
            await message.answer(
                "❌ Не удалось создать клиента. Попробуйте позже.",
                reply_markup=Keyboards.main_menu(is_admin)
            )
            await state.clear()

    except Exception as e:
        await state.clear()
        await message.answer(
            f"❌ Ошибка при миграции: {str(e)}\n\n"
            "Попробуйте ещё раз или обратитесь к администратору.",
            reply_markup=Keyboards.main_menu(is_admin)
        )


async def fix_current_client(message: Message, state: FSMContext, uuid_part: str, xui_client, is_admin: bool):
    """Исправление ключа для текущего сервера"""

    # Ищем клиента в текущей базе
    try:
        inbounds = await xui_client.list_inbounds()

        found_client = None
        found_inbound = None

        for inbound in inbounds:
            import json
            settings = json.loads(inbound.get('settings', '{}'))
            clients = settings.get('clients', [])

            for client in clients:
                if client.get('id') == uuid_part:
                    found_client = client
                    found_inbound = inbound
                    break

            if found_client:
                break

        if found_client:
            client_email = found_client.get('email', '')
            inbound_id = found_inbound.get('id')

            # Генерируем исправленную ссылку
            new_vless_link = await xui_client.get_client_link(inbound_id, client_email)

            if new_vless_link:
                new_vless_link = xui_client.replace_ip_with_domain(new_vless_link, 'raphaelvpn.ru', 443)

            subscription_url = f"https://zov-gor.ru:8080/sub/{uuid_part}"

            success_text = (
                f"✅ <b>Ключ найден и исправлен!</b>\n\n"
                f"👤 Клиент: <b>{client_email}</b>\n\n"
                f"━━━━━━━━━━━━━━━━\n\n"
                f"🔑 <b>Исправленный VLESS ключ:</b>\n"
                f"<code>{new_vless_link}</code>\n\n"
                f"━━━━━━━━━━━━━━━━\n\n"
                f"📲 <b>Ссылка подписки:</b>\n"
                f"<code>{subscription_url}</code>\n\n"
                f"💡 <i>Скопируйте ключ и добавьте в приложение</i>"
            )

            await state.clear()
            await message.answer(
                success_text,
                parse_mode="HTML",
                reply_markup=Keyboards.main_menu(is_admin)
            )
        else:
            await state.clear()
            await message.answer(
                "❌ <b>Ключ не найден</b>\n\n"
                "Этот ключ не найден ни в старой, ни в текущей базе данных.\n\n"
                "Возможные причины:\n"
                "• Ключ от другого сервера\n"
                "• Ключ был удалён\n"
                "• Неверный формат ключа\n\n"
                "Обратитесь к администратору для помощи.",
                parse_mode="HTML",
                reply_markup=Keyboards.main_menu(is_admin)
            )

    except Exception as e:
        await state.clear()
        await message.answer(
            f"❌ Ошибка при поиске: {str(e)}\n\n"
            "Попробуйте ещё раз или обратитесь к администратору.",
            reply_markup=Keyboards.main_menu(is_admin)
        )
