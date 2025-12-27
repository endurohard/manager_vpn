"""
Обработчики для менеджеров (создание ключей, статистика)
"""
import logging
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from datetime import datetime, timedelta

from bot.config import ADMIN_ID, INBOUND_ID, DOMAIN

logger = logging.getLogger(__name__)
from bot.database import DatabaseManager
from bot.api.xui_client import XUIClient
from bot.utils import Keyboards, validate_phone, format_phone, generate_user_id, generate_qr_code, notify_admin_xui_error
from bot.handlers.common import is_authorized
from bot.price_config import get_subscription_periods

router = Router()


class CreateKeyStates(StatesGroup):
    """Состояния для создания ключа"""
    waiting_for_phone = State()
    waiting_for_server = State()  # Выбор сервера
    waiting_for_inbound = State()  # Для админа - выбор inbound
    waiting_for_period = State()
    waiting_for_custom_price = State()  # Для админа - ввод кастомной цены
    confirm = State()


class EditRealityStates(StatesGroup):
    """Состояния для редактирования REALITY параметров"""
    waiting_for_inbound_selection = State()
    waiting_for_dest = State()
    waiting_for_sni = State()
    confirm = State()


class ReplaceKeyStates(StatesGroup):
    """Состояния для замены ключа"""
    waiting_for_phone = State()
    waiting_for_period = State()
    confirm = State()


class FixKeyStates(StatesGroup):
    """Состояния для исправления ключа"""
    waiting_for_key = State()


@router.message(F.text == "Создать ключ")
async def start_create_key(message: Message, state: FSMContext, db: DatabaseManager):
    """Начало процесса создания ключа"""
    user_id = message.from_user.id

    # Проверка авторизации
    if not await is_authorized(user_id, db):
        await message.answer("У вас нет доступа к этой функции.")
        return

    await state.set_state(CreateKeyStates.waiting_for_phone)
    await message.answer(
        "Введите идентификатор клиента (номер телефона или любой текст) или нажмите 'Сгенерировать ID':\n\n"
        "Примеры:\n"
        "• +79001234567\n"
        "• client_name_123\n"
        "• user_12345\n"
        "• Или нажмите 'Сгенерировать ID' для автоматической генерации",
        reply_markup=Keyboards.phone_input()
    )


@router.message(CreateKeyStates.waiting_for_phone, F.text == "Сгенерировать ID")
async def generate_user_identifier(message: Message, state: FSMContext, xui_client: XUIClient):
    """Генерация случайного ID пользователя"""
    from bot.api.remote_xui import load_servers_config

    user_id_value = generate_user_id()
    await state.update_data(phone=user_id_value)

    # Загружаем список серверов
    servers_config = load_servers_config()
    servers = [s for s in servers_config.get('servers', []) if s.get('enabled', True) and not s.get('local', False)]

    if not servers:
        # Если нет удалённых серверов, используем локальный
        await state.update_data(inbound_id=INBOUND_ID)
        await state.set_state(CreateKeyStates.waiting_for_period)
        await message.answer(
            f"Сгенерирован ID: {user_id_value}\n\n"
            "Выберите срок действия ключа:",
            reply_markup=Keyboards.subscription_periods()
        )
        return

    await state.update_data(servers=servers)
    await state.set_state(CreateKeyStates.waiting_for_server)
    await message.answer(
        f"🆔 Сгенерирован ID: <code>{user_id_value}</code>\n\n"
        f"🖥 <b>Выберите сервер:</b>\n"
        f"🟢 - активен для новых\n"
        f"🟡 - отключен для новых",
        reply_markup=Keyboards.server_selection(servers),
        parse_mode="HTML"
    )


@router.message(CreateKeyStates.waiting_for_phone, F.text == "Отмена")
async def cancel_key_creation(message: Message, state: FSMContext):
    """Отмена создания ключа"""
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID

    await state.clear()
    await message.answer(
        "Создание ключа отменено.",
        reply_markup=Keyboards.main_menu(is_admin)
    )


@router.message(CreateKeyStates.waiting_for_phone)
async def process_phone_input(message: Message, state: FSMContext, xui_client: XUIClient):
    """Обработка введенного ID/номера телефона"""
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID

    user_input = message.text.strip()
    original_input = user_input  # Сохраняем оригинал для сравнения

    # Проверяем, не ввел ли пользователь вручную текст кнопки "Сгенерировать"
    if 'генерир' in user_input.lower() or 'generate' in user_input.lower():
        # Автоматически генерируем ID и показываем выбор сервера
        from bot.api.remote_xui import load_servers_config

        generated_id = generate_user_id()
        await state.update_data(phone=generated_id, inbound_id=INBOUND_ID)

        servers_config = load_servers_config()
        servers = [s for s in servers_config.get('servers', []) if s.get('enabled', True) and not s.get('local', False)]

        if not servers:
            await state.set_state(CreateKeyStates.waiting_for_period)
            await message.answer(
                f"⚠️ Обнаружен текст кнопки. Автоматически сгенерирован новый ID:\n"
                f"🆔 <code>{generated_id}</code>\n\n"
                "Выберите срок действия ключа:",
                reply_markup=Keyboards.subscription_periods(),
                parse_mode="HTML"
            )
        else:
            await state.update_data(servers=servers)
            await state.set_state(CreateKeyStates.waiting_for_server)
            await message.answer(
                f"🆔 Сгенерирован ID: <code>{generated_id}</code>\n\n"
                f"🖥 <b>Выберите сервер:</b>\n"
                f"🟢 - активен для новых\n"
                f"🟡 - отключен для новых",
                reply_markup=Keyboards.server_selection(servers),
                parse_mode="HTML"
            )
        return

    # Проверяем минимальную длину
    if len(user_input) < 3:
        await message.answer(
            "Идентификатор слишком короткий. Минимум 3 символа.\n"
            "Попробуйте еще раз или нажмите 'Сгенерировать ID'"
        )
        return

    # Если это похоже на номер телефона, форматируем его
    if validate_phone(user_input):
        user_input = format_phone(user_input)

        # Если номер был изменен, показываем пользователю отформатированную версию
        if user_input != original_input:
            format_message = (
                f"✅ Номер телефона распознан и отформатирован:\n"
                f"📱 <code>{user_input}</code>\n\n"
            )
        else:
            format_message = (
                f"Идентификатор клиента: <code>{user_input}</code>\n\n"
            )
    else:
        format_message = (
            f"Идентификатор клиента: <code>{user_input}</code>\n\n"
        )

    await state.update_data(phone=user_input, inbound_id=INBOUND_ID)

    # Загружаем список серверов
    from bot.api.remote_xui import load_servers_config
    servers_config = load_servers_config()
    servers = [s for s in servers_config.get('servers', []) if s.get('enabled', True) and not s.get('local', False)]

    if not servers:
        # Если нет удалённых серверов, используем локальный
        await state.set_state(CreateKeyStates.waiting_for_period)
        await message.answer(
            format_message + "Выберите срок действия ключа:",
            reply_markup=Keyboards.subscription_periods(),
            parse_mode="HTML"
        )
        return

    await state.update_data(servers=servers)
    await state.set_state(CreateKeyStates.waiting_for_server)
    await message.answer(
        format_message +
        "🖥 <b>Выберите сервер:</b>\n"
        "🟢 - активен для новых\n"
        "🟡 - отключен для новых",
        reply_markup=Keyboards.server_selection(servers),
        parse_mode="HTML"
    )


@router.callback_query(CreateKeyStates.waiting_for_server, F.data.startswith("server_"))
async def process_server_selection(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора сервера для создания ключа"""
    server_idx = int(callback.data.split("_", 1)[1])
    data = await state.get_data()
    servers = data.get('servers', [])
    phone = data.get('phone', '')

    if server_idx >= len(servers):
        await callback.answer("Ошибка: сервер не найден", show_alert=True)
        return

    selected_server = servers[server_idx]
    main_inbound = selected_server.get('inbounds', {}).get('main', {})
    inbound_id = main_inbound.get('id', 1)

    await state.update_data(
        selected_server=selected_server,
        selected_inbound=main_inbound,
        inbound_id=inbound_id
    )

    server_name = selected_server.get('name', 'Unknown')

    await state.set_state(CreateKeyStates.waiting_for_period)
    await callback.message.edit_text(
        f"🆔 ID: <code>{phone}</code>\n"
        f"🖥 Сервер: <b>{server_name}</b>\n\n"
        "Выберите срок действия ключа:",
        reply_markup=Keyboards.subscription_periods(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("inbound_"))
async def process_inbound_selection(callback: CallbackQuery, state: FSMContext, xui_client: XUIClient):
    """Обработка выбора inbound (для создания ключа или редактирования REALITY)"""
    user_id = callback.from_user.id
    is_admin = user_id == ADMIN_ID

    # Проверяем, что пользователь - админ
    if not is_admin:
        await callback.answer("У вас нет доступа к этой функции", show_alert=True)
        return

    # Получаем ID выбранного inbound
    inbound_id = int(callback.data.split("_", 1)[1])

    # Проверяем текущее состояние FSM
    current_state = await state.get_state()

    # Если это редактирование REALITY
    if current_state == EditRealityStates.waiting_for_inbound_selection:
        # Получаем текущие настройки inbound
        inbound = await xui_client.get_inbound(inbound_id)
        if not inbound:
            await callback.message.edit_text("❌ Не удалось получить данные inbound")
            await state.clear()
            return

        import json
        stream_settings = json.loads(inbound.get('streamSettings', '{}'))
        reality_settings = stream_settings.get('realitySettings', {})

        current_dest = reality_settings.get('dest', 'Не указан')
        current_sni = ', '.join(reality_settings.get('serverNames', []))

        # Сохраняем ID inbound
        await state.update_data(
            inbound_id=inbound_id,
            current_dest=current_dest,
            current_sni=current_sni
        )
        await state.set_state(EditRealityStates.waiting_for_dest)

        await callback.message.edit_text(
            f"🔐 <b>Редактирование REALITY параметров</b>\n\n"
            f"Inbound ID: <code>{inbound_id}</code>\n\n"
            f"📍 <b>Текущий Dest:</b> <code>{current_dest}</code>\n"
            f"🌐 <b>Текущий SNI:</b> <code>{current_sni}</code>\n\n"
            f"━━━━━━━━━━━━━━━━\n\n"
            f"Введите новый <b>Dest (Target)</b>:\n"
            f"Формат: <code>domain.com:443</code>\n\n"
            f"Пример: <code>vk.com:443</code> или <code>mail.ru:443</code>",
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # Если это создание ключа
    # Сохраняем выбранный inbound
    await state.update_data(inbound_id=inbound_id)
    await state.set_state(CreateKeyStates.waiting_for_period)

    # Получаем данные для отображения
    data = await state.get_data()
    phone = data.get("phone")

    await callback.message.edit_text(
        f"🆔 ID клиента: <code>{phone}</code>\n"
        f"🔌 Inbound ID: <b>{inbound_id}</b>\n\n"
        f"Выберите срок действия ключа:",
        reply_markup=Keyboards.subscription_periods(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("period_"))
async def process_period_selection(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора периода подписки"""
    user_id = callback.from_user.id
    is_admin = user_id == ADMIN_ID

    period_key = callback.data.split("_", 1)[1]

    # Загружаем актуальные цены
    periods = get_subscription_periods()

    if period_key not in periods:
        await callback.answer("Ошибка выбора периода")
        return

    period_info = periods[period_key]
    await state.update_data(
        period_key=period_key,
        period_name=period_info["name"],
        period_days=period_info["days"],
        period_price=period_info["price"]  # Стандартная цена
    )

    # Получаем данные
    data = await state.get_data()
    phone = data.get("phone")

    # Для администратора - показываем выбор цены
    if is_admin:
        await callback.message.edit_text(
            f"👑 <b>РЕЖИМ АДМИНИСТРАТОРА</b>\n\n"
            f"📋 Параметры ключа:\n"
            f"🆔 ID клиента: <code>{phone}</code>\n"
            f"📅 Срок действия: <b>{period_info['name']}</b> ({period_info['days']} дней)\n"
            f"🌐 Лимит IP: 2\n"
            f"📊 Трафик: безлимит\n\n"
            f"💰 <b>Выберите цену для клиента:</b>",
            reply_markup=Keyboards.admin_price_selection(period_info['price']),
            parse_mode="HTML"
        )
    else:
        # Для обычного менеджера - сразу подтверждение
        await callback.message.edit_text(
            f"📋 <b>Подтверждение создания ключа:</b>\n\n"
            f"🆔 ID клиента: <code>{phone}</code>\n"
            f"📅 Срок действия: <b>{period_info['name']}</b> ({period_info['days']} дней)\n"
            f"💰 Стоимость: <b>{period_info['price']} ₽</b>\n"
            f"🌐 Лимит IP: 2\n"
            f"📊 Трафик: безлимит\n\n"
            f"❓ Создать ключ?",
            reply_markup=Keyboards.confirm_key_creation(phone, period_key),
            parse_mode="HTML"
        )

    await callback.answer()


@router.callback_query(F.data.startswith("price_standard_"))
async def process_standard_price(callback: CallbackQuery, state: FSMContext):
    """Использовать стандартную цену"""
    # Цена уже сохранена в state.update_data выше, ничего не меняем
    data = await state.get_data()
    phone = data.get("phone")
    period_key = data.get("period_key")
    period_name = data.get("period_name")
    period_days = data.get("period_days")
    period_price = data.get("period_price")

    await callback.message.edit_text(
        f"📋 <b>Подтверждение создания ключа:</b>\n\n"
        f"🆔 ID клиента: <code>{phone}</code>\n"
        f"📅 Срок действия: <b>{period_name}</b> ({period_days} дней)\n"
        f"💰 Стоимость: <b>{period_price} ₽</b>\n"
        f"🌐 Лимит IP: 2\n"
        f"📊 Трафик: безлимит\n\n"
        f"❓ Создать ключ?",
        reply_markup=Keyboards.confirm_key_creation(phone, period_key),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("price_custom_"))
async def process_custom_price(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора кастомной цены"""
    price_data = callback.data.split("_", 2)[2]

    data = await state.get_data()
    phone = data.get("phone")
    period_key = data.get("period_key")
    period_name = data.get("period_name")
    period_days = data.get("period_days")

    if price_data == "input":
        # Запрашиваем ввод цены
        await state.set_state(CreateKeyStates.waiting_for_custom_price)
        await callback.message.edit_text(
            f"✏️ <b>Ввод цены</b>\n\n"
            f"🆔 ID клиента: <code>{phone}</code>\n"
            f"📅 Срок: {period_name}\n\n"
            f"Введите цену в рублях (целое число):\n"
            f"• 0 - бесплатный ключ\n"
            f"• 500 - пятьсот рублей\n"
            f"• 1000 - тысяча рублей\n\n"
            f"Или нажмите /cancel для отмены",
            parse_mode="HTML"
        )
    else:
        # Цена указана напрямую (например, 0 для бесплатного)
        custom_price = int(price_data)
        await state.update_data(period_price=custom_price)

        await callback.message.edit_text(
            f"📋 <b>Подтверждение создания ключа:</b>\n\n"
            f"🆔 ID клиента: <code>{phone}</code>\n"
            f"📅 Срок действия: <b>{period_name}</b> ({period_days} дней)\n"
            f"💰 Стоимость: <b>{custom_price} ₽</b> {'🎁' if custom_price == 0 else ''}\n"
            f"🌐 Лимит IP: 2\n"
            f"📊 Трафик: безлимит\n\n"
            f"❓ Создать ключ?",
            reply_markup=Keyboards.confirm_key_creation(phone, period_key),
            parse_mode="HTML"
        )

    await callback.answer()


@router.message(CreateKeyStates.waiting_for_custom_price, F.text == "/cancel")
async def cancel_custom_price_input(message: Message, state: FSMContext):
    """Отмена ввода кастомной цены"""
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID

    await state.clear()
    await message.answer(
        "Создание ключа отменено.",
        reply_markup=Keyboards.main_menu(is_admin)
    )


@router.message(CreateKeyStates.waiting_for_custom_price)
async def process_custom_price_input(message: Message, state: FSMContext):
    """Обработка введенной кастомной цены"""
    try:
        custom_price = int(message.text.strip())

        if custom_price < 0:
            await message.answer("❌ Цена не может быть отрицательной. Попробуйте еще раз:")
            return

        if custom_price > 1000000:
            await message.answer("❌ Цена слишком большая. Попробуйте еще раз:")
            return

        # Сохраняем кастомную цену
        await state.update_data(period_price=custom_price)

        data = await state.get_data()
        phone = data.get("phone")
        period_key = data.get("period_key")
        period_name = data.get("period_name")
        period_days = data.get("period_days")

        await message.answer(
            f"📋 <b>Подтверждение создания ключа:</b>\n\n"
            f"🆔 ID клиента: <code>{phone}</code>\n"
            f"📅 Срок действия: <b>{period_name}</b> ({period_days} дней)\n"
            f"💰 Стоимость: <b>{custom_price} ₽</b> {'🎁' if custom_price == 0 else ''}\n"
            f"🌐 Лимит IP: 2\n"
            f"📊 Трафик: безлимит\n\n"
            f"❓ Создать ключ?",
            reply_markup=Keyboards.confirm_key_creation(phone, period_key),
            parse_mode="HTML"
        )

    except ValueError:
        await message.answer(
            "❌ Некорректная цена. Введите целое число.\n"
            "Например: 500 или 0\n\n"
            "Или нажмите /cancel для отмены"
        )


@router.callback_query(F.data.startswith("create_"))
async def confirm_create_key(callback: CallbackQuery, state: FSMContext, db: DatabaseManager,
                             xui_client: XUIClient, bot):
    """Подтверждение и создание ключа"""
    user_id = callback.from_user.id

    # Получаем данные из состояния
    data = await state.get_data()
    phone = data.get("phone")
    period_key = data.get("period_key")
    period_name = data.get("period_name")
    period_days = data.get("period_days")
    inbound_id = data.get("inbound_id", INBOUND_ID)  # Используем выбранный или дефолтный
    selected_server = data.get("selected_server")  # Выбранный сервер (если есть)
    selected_inbound = data.get("selected_inbound")  # Выбранный inbound

    await callback.message.edit_text("Создание ключа...")

    try:
        # Если выбран конкретный сервер - создаём только на нём
        if selected_server and not selected_server.get('local', False):
            import uuid as uuid_module
            from bot.api.remote_xui import create_client_on_remote_server

            client_uuid = str(uuid_module.uuid4())
            success = await create_client_on_remote_server(
                server_config=selected_server,
                client_uuid=client_uuid,
                email=phone,
                expire_days=period_days,
                ip_limit=2,
                inbound_id=inbound_id
            )

            if success:
                client_data = {
                    'client_id': client_uuid,
                    'local_created': False
                }
            else:
                client_data = None
        else:
            # Старая логика - создаём на локальном и всех удалённых
            client_data = await xui_client.add_client(
                inbound_id=inbound_id,
                email=phone,
                phone=phone,
                expire_days=period_days,
                ip_limit=2
            )

        if not client_data:
            # Сохраняем в очередь на повторное создание
            error_msg = f"Не удалось создать клиента для ID: {phone}, период: {period_name} ({period_days} дней)"
            pending_id = await db.add_pending_key(
                telegram_id=user_id,
                username=callback.from_user.username or "",
                phone=phone,
                period_key=period_key,
                period_name=period_name,
                period_days=period_days,
                period_price=data.get("period_price", 0),
                inbound_id=inbound_id,
                error=error_msg
            )

            if pending_id:
                await callback.message.edit_text(
                    "⏳ <b>Временная ошибка сервера</b>\n\n"
                    f"🆔 ID/Номер: <code>{phone}</code>\n"
                    f"📦 Тариф: {period_name}\n\n"
                    "⚙️ Ваш ключ добавлен в очередь и будет создан автоматически "
                    "в течение нескольких минут.\n\n"
                    "📬 Вы получите уведомление с ключом, как только он будет готов.",
                    parse_mode="HTML"
                )
            else:
                await callback.message.edit_text(
                    "❌ Ошибка при создании ключа в X-UI панели.\n"
                    "Попробуйте позже или обратитесь к администратору."
                )

            # Отправляем уведомление админу об ошибке
            await notify_admin_xui_error(
                bot=bot,
                operation="Создание ключа",
                user_info={
                    'user_id': user_id,
                    'username': callback.from_user.username,
                    'phone': phone
                },
                error_details=f"{error_msg}\n📋 Добавлен в очередь: #{pending_id}" if pending_id else error_msg
            )

            return

        # Проверяем наличие ошибки в ответе
        if client_data.get('error'):
            error_message = client_data.get('message', 'Неизвестная ошибка')

            # Обработка дубликата
            if client_data.get('is_duplicate'):
                # Возвращаем пользователя в главное меню
                is_admin = user_id == ADMIN_ID
                await callback.message.edit_text(
                    f"⚠️ <b>Такой клиент уже существует!</b>\n\n"
                    f"🆔 ID/Номер: <code>{phone}</code>\n\n"
                    f"Клиент с таким идентификатором уже создан в системе.\n"
                    f"Каждый ID/номер должен быть уникальным.\n\n"
                    f"💡 <b>Что делать:</b>\n"
                    f"1️⃣ Используйте другой номер телефона\n"
                    f"2️⃣ Сгенерируйте автоматический ID (нажмите \"Создать ключ\" → \"Сгенерировать ID\")\n"
                    f"3️⃣ Или удалите старый ключ в X-UI панели\n\n"
                    f"Нажмите \"Создать ключ\" снова, чтобы попробовать с другим ID.",
                    parse_mode="HTML"
                )
                # Отправляем главное меню
                await callback.message.answer(
                    "Выберите действие:",
                    reply_markup=Keyboards.main_menu(is_admin)
                )
                # Очищаем состояние
                await state.clear()
            else:
                # Другие ошибки
                is_admin = user_id == ADMIN_ID
                await callback.message.edit_text(
                    f"❌ <b>Ошибка создания ключа</b>\n\n"
                    f"Детали: {error_message}\n\n"
                    f"Попробуйте еще раз или обратитесь к администратору.",
                    parse_mode="HTML"
                )

                # Отправляем уведомление админу об ошибке
                await notify_admin_xui_error(
                    bot=bot,
                    operation="Создание ключа (ошибка X-UI)",
                    user_info={
                        'user_id': user_id,
                        'username': callback.from_user.username,
                        'phone': phone
                    },
                    error_details=f"Ошибка X-UI: {error_message}\nID клиента: {phone}\nПериод: {period_name} ({period_days} дней)"
                )

                # Отправляем главное меню
                await callback.message.answer(
                    "Выберите действие:",
                    reply_markup=Keyboards.main_menu(is_admin)
                )
                # Очищаем состояние
                await state.clear()
            return

        # Проверяем, создан ли клиент локально
        local_created = client_data.get('local_created', True)
        client_uuid = client_data['client_id']

        # Получаем VLESS ссылку
        vless_link_for_user = None

        if local_created:
            # Если создан локально - получаем с локального сервера
            vless_link_original = await xui_client.get_client_link(
                inbound_id=inbound_id,
                client_email=phone,
                use_domain=None
            )
            if vless_link_original:
                vless_link_for_user = XUIClient.replace_ip_with_domain(vless_link_original, DOMAIN)

        # Если локально не создан или не получилось - генерируем из конфига сервера
        if not vless_link_for_user:
            import urllib.parse

            # Если есть выбранный сервер - используем его, иначе ищем первый активный
            target_server = selected_server
            target_inbound = selected_inbound

            if not target_server:
                from bot.api.remote_xui import load_servers_config
                servers_config = load_servers_config()
                for server in servers_config.get('servers', []):
                    if not server.get('enabled', True):
                        continue
                    if not server.get('active_for_new', True):
                        continue
                    target_server = server
                    target_inbound = server.get('inbounds', {}).get('main', {})
                    break

            if target_server and target_inbound:
                domain = target_server.get('domain', target_server.get('ip', ''))
                port = target_server.get('port', 443)
                network = target_inbound.get('network', 'tcp')

                params = [f"type={network}", "encryption=none"]

                # Добавляем gRPC параметры если нужно
                if network == 'grpc':
                    params.append(f"serviceName={target_inbound.get('serviceName', '')}")
                    params.append(f"authority={target_inbound.get('authority', '')}")

                params.append(f"security={target_inbound.get('security', 'reality')}")

                if target_inbound.get('security') == 'reality':
                    if target_inbound.get('pbk'):
                        params.append(f"pbk={target_inbound['pbk']}")
                    params.append(f"fp={target_inbound.get('fp', 'chrome')}")
                    if target_inbound.get('sni'):
                        params.append(f"sni={target_inbound['sni']}")
                    if target_inbound.get('sid'):
                        params.append(f"sid={target_inbound['sid']}")
                    if target_inbound.get('flow'):
                        params.append(f"flow={target_inbound['flow']}")
                    params.append("spx=%2F")

                query = '&'.join(params)
                name_prefix = target_inbound.get('name_prefix', target_server.get('name', 'VPN'))
                # Формируем имя: PREFIX пробел EMAIL (как в get_client_link_from_active_server)
                full_name = f"{name_prefix} {phone}" if phone else name_prefix

                vless_link_for_user = f"vless://{client_uuid}@{domain}:{port}?{query}#{full_name}"

        if not vless_link_for_user:
            await callback.message.edit_text(
                "Ключ создан, но не удалось сформировать VLESS ссылку."
            )
            return

        # Получаем цену из данных
        period_price = data.get("period_price", 0)

        # Сохраняем в базу данных
        await db.add_key_to_history(
            manager_id=user_id,
            client_email=phone,
            phone_number=phone,
            period=period_name,
            expire_days=period_days,
            client_id=client_uuid,
            price=period_price
        )

        # Формируем ссылку подписки
        subscription_url = f"https://zov-gor.ru/sub/{client_uuid}"

        # Генерируем QR код для ссылки с ДОМЕНОМ (для пользователя)
        try:
            qr_code = generate_qr_code(vless_link_for_user)

            # Отправляем QR код
            await callback.message.answer_photo(
                BufferedInputFile(qr_code.read(), filename="qrcode.png"),
                caption=(
                    f"✅ Ключ успешно создан!\n\n"
                    f"🆔 ID клиента: {phone}\n"
                    f"⏰ Срок действия: {period_name}\n"
                    f"💰 Стоимость: {period_price} ₽\n"
                    f"🌐 Лимит IP: 2\n"
                    f"📊 Трафик: безлимит\n\n"
                    f"📱 Отсканируйте QR код в приложении VPN"
                )
            )

            # Отправляем текстовый ключ с ДОМЕНОМ
            await callback.message.answer(
                f"📋 VLESS ключ:\n\n`{vless_link_for_user}`\n\n"
                f"🔄 Ссылка подписки (автообновление):\n`{subscription_url}`\n\n"
                f"💡 Подписка автоматически обновит ключ при изменениях на сервере.\n"
                f"Скопируйте и отправьте клиенту.",
                parse_mode="Markdown"
            )

            # Удаляем сообщение "Создание ключа..."
            await callback.message.delete()

        except Exception as e:
            print(f"QR generation error: {e}")
            # Если QR не создался, отправляем хотя бы текст
            await callback.message.edit_text(
                f"✅ Ключ успешно создан!\n\n"
                f"🆔 ID клиента: {phone}\n"
                f"⏰ Срок действия: {period_name}\n"
                f"💰 Стоимость: {period_price} ₽\n"
                f"🌐 Лимит IP: 2\n\n"
                f"📋 VLESS ключ:\n`{vless_link_for_user}`\n\n"
                f"🔄 Ссылка подписки:\n`{subscription_url}`\n\n"
                f"Скопируйте и отправьте клиенту.",
                parse_mode="Markdown"
            )

        # Возвращаем в главное меню
        is_admin = user_id == ADMIN_ID
        await callback.message.answer(
            "✅ Готово!",
            reply_markup=Keyboards.main_menu(is_admin)
        )

    except Exception as e:
        await callback.message.edit_text(
            f"Произошла ошибка при создании ключа: {str(e)}"
        )

    finally:
        await state.clear()

    await callback.answer()


@router.callback_query(F.data == "cancel_creation")
async def cancel_creation_callback(callback: CallbackQuery, state: FSMContext):
    """Отмена создания ключа (callback)"""
    user_id = callback.from_user.id
    is_admin = user_id == ADMIN_ID

    await state.clear()
    await callback.message.edit_text("Создание ключа отменено.")
    await callback.message.answer(
        "Главное меню:",
        reply_markup=Keyboards.main_menu(is_admin)
    )
    await callback.answer()


# ==================== ЗАМЕНА КЛЮЧА ====================

@router.message(F.text == "🔄 Замена ключа")
async def start_replace_key(message: Message, state: FSMContext, db: DatabaseManager):
    """Начало процесса замены ключа"""
    user_id = message.from_user.id

    # Проверка авторизации
    if not await is_authorized(user_id, db):
        await message.answer("У вас нет доступа к этой функции.")
        return

    await state.set_state(ReplaceKeyStates.waiting_for_phone)
    await message.answer(
        "🔄 <b>Замена ключа</b>\n\n"
        "Введите:\n"
        "• ID клиента (номер телефона или текст)\n"
        "• Или <b>VLESS ключ</b> целиком\n\n"
        "Примеры:\n"
        "• +79001234567\n"
        "• client_name_123\n"
        "• vless://uuid@server...\n\n"
        "Или нажмите 'Сгенерировать ID'",
        reply_markup=Keyboards.phone_input(),
        parse_mode="HTML"
    )


@router.message(ReplaceKeyStates.waiting_for_phone, F.text == "Сгенерировать ID")
async def generate_replacement_id(message: Message, state: FSMContext):
    """Генерация случайного ID для замены"""
    user_id_value = generate_user_id()
    await state.update_data(phone=user_id_value, inbound_id=INBOUND_ID)
    await state.set_state(ReplaceKeyStates.waiting_for_period)

    await message.answer(
        f"🆔 Сгенерирован ID: <code>{user_id_value}</code>\n\n"
        "Выберите срок действия ключа:",
        reply_markup=Keyboards.replacement_periods(),
        parse_mode="HTML"
    )


@router.message(ReplaceKeyStates.waiting_for_phone, F.text == "Отмена")
async def cancel_replacement(message: Message, state: FSMContext):
    """Отмена замены ключа"""
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID

    await state.clear()
    await message.answer(
        "Замена ключа отменена.",
        reply_markup=Keyboards.main_menu(is_admin)
    )


@router.message(ReplaceKeyStates.waiting_for_phone)
async def process_replacement_phone(message: Message, state: FSMContext, xui_client: XUIClient):
    """Обработка введенного ID/номера/VLESS ключа для замены"""
    user_input = message.text.strip()

    # Проверяем, не VLESS ли это ключ
    if user_input.startswith('vless://'):
        # Парсим VLESS ключ
        try:
            from urllib.parse import urlparse, unquote
            parsed = urlparse(user_input)
            client_uuid = parsed.username  # UUID из ключа

            # Получаем email из фрагмента (имя после #)
            fragment = unquote(parsed.fragment) if parsed.fragment else ''

            # Ищем клиента по UUID в локальной базе
            client_info = await xui_client.find_client_by_uuid(client_uuid)

            if client_info:
                client_email = client_info.get('email', fragment or client_uuid[:8])
                ip_limit = client_info.get('limitIp', 2)
                expiry_time = client_info.get('expiryTime', 0)

                # Вычисляем оставшиеся дни
                if expiry_time > 0:
                    import time
                    remaining_ms = expiry_time - int(time.time() * 1000)
                    remaining_days = max(0, remaining_ms // (1000 * 60 * 60 * 24))
                else:
                    remaining_days = 0

                await state.update_data(
                    phone=client_email,
                    original_uuid=client_uuid,
                    original_ip_limit=ip_limit,
                    original_expiry=expiry_time,
                    remaining_days=remaining_days,
                    inbound_id=INBOUND_ID,
                    from_vless_key=True
                )
                await state.set_state(ReplaceKeyStates.waiting_for_period)

                await message.answer(
                    f"🔑 <b>Найден клиент из VLESS ключа:</b>\n\n"
                    f"🆔 Email: <code>{client_email}</code>\n"
                    f"🔐 UUID: <code>{client_uuid[:8]}...</code>\n"
                    f"🌐 Лимит IP: {ip_limit}\n"
                    f"⏰ Осталось дней: {remaining_days}\n\n"
                    f"Выберите срок действия ключа:",
                    reply_markup=Keyboards.replacement_periods(show_original=True, remaining_days=remaining_days),
                    parse_mode="HTML"
                )
                return
            else:
                # Клиент не найден - используем имя из ключа
                client_email = fragment if fragment else client_uuid[:8]
                await state.update_data(
                    phone=client_email,
                    original_uuid=client_uuid,
                    inbound_id=INBOUND_ID,
                    from_vless_key=True
                )
                await state.set_state(ReplaceKeyStates.waiting_for_period)

                await message.answer(
                    f"⚠️ <b>Клиент не найден в локальной базе</b>\n\n"
                    f"🆔 Используем имя: <code>{client_email}</code>\n"
                    f"🔐 UUID из ключа: <code>{client_uuid[:8]}...</code>\n\n"
                    f"Выберите срок действия <b>нового</b> ключа:",
                    reply_markup=Keyboards.replacement_periods(),
                    parse_mode="HTML"
                )
                return
        except Exception as e:
            await message.answer(
                f"❌ Ошибка парсинга VLESS ключа: {str(e)[:50]}\n"
                "Попробуйте ввести ID клиента вручную."
            )
            return

    # Проверяем минимальную длину
    if len(user_input) < 3:
        await message.answer(
            "Идентификатор слишком короткий. Минимум 3 символа.\n"
            "Попробуйте еще раз или нажмите 'Сгенерировать ID'"
        )
        return

    # Если это похоже на номер телефона, форматируем его
    if validate_phone(user_input):
        user_input = format_phone(user_input)

    await state.update_data(phone=user_input, inbound_id=INBOUND_ID)
    await state.set_state(ReplaceKeyStates.waiting_for_period)

    await message.answer(
        f"🆔 ID клиента: <code>{user_input}</code>\n\n"
        "Выберите срок действия ключа:",
        reply_markup=Keyboards.replacement_periods(),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("replace_period_"))
async def process_replacement_period(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора периода для замены"""
    period_key = callback.data.replace("replace_period_", "")

    data = await state.get_data()
    phone = data.get("phone")
    original_ip_limit = data.get("original_ip_limit", 2)
    remaining_days = data.get("remaining_days", 0)

    # Обработка выбора "оставить оригинальный"
    if period_key == "original":
        period_name = f"Оригинальный ({remaining_days} дн.)"
        period_days = remaining_days
        await state.update_data(
            period_key="original",
            period_name=period_name,
            period_days=period_days,
            use_original_expiry=True
        )
    else:
        periods = get_subscription_periods()
        if period_key not in periods:
            await callback.answer("Ошибка выбора периода")
            return

        period_info = periods[period_key]
        period_name = period_info["name"]
        period_days = period_info["days"]
        await state.update_data(
            period_key=period_key,
            period_name=period_name,
            period_days=period_days,
            use_original_expiry=False
        )

    await callback.message.edit_text(
        f"🔄 <b>Подтверждение замены ключа:</b>\n\n"
        f"🆔 ID клиента: <code>{phone}</code>\n"
        f"📅 Срок действия: <b>{period_name}</b>\n"
        f"🌐 Лимит IP: {original_ip_limit}\n"
        f"📊 Трафик: безлимит\n\n"
        f"❓ Создать ключ на новом сервере?",
        reply_markup=Keyboards.confirm_key_replacement(phone, period_key),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_replacement")
async def cancel_replacement_callback(callback: CallbackQuery, state: FSMContext):
    """Отмена замены ключа (callback)"""
    user_id = callback.from_user.id
    is_admin = user_id == ADMIN_ID

    await state.clear()
    await callback.message.edit_text("Замена ключа отменена.")
    await callback.message.answer(
        "Главное меню:",
        reply_markup=Keyboards.main_menu(is_admin)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("replace_") & ~F.data.startswith("replace_period_"))
async def confirm_replace_key(callback: CallbackQuery, state: FSMContext, db: DatabaseManager,
                               xui_client: XUIClient, bot):
    """Подтверждение и замена ключа - поиск в локальной базе, создание на удалённом сервере"""
    user_id = callback.from_user.id
    is_admin = user_id == ADMIN_ID

    # Получаем данные из состояния
    data = await state.get_data()
    phone = data.get("phone")
    period_name = data.get("period_name")
    period_days = data.get("period_days")
    original_ip_limit = data.get("original_ip_limit", 2)
    original_expiry = data.get("original_expiry", 0)
    use_original_expiry = data.get("use_original_expiry", False)

    await callback.message.edit_text("🔄 Создание ключа на новом сервере...")

    try:
        from bot.api.remote_xui import load_servers_config
        import urllib.parse
        import aiohttp
        import ssl
        import uuid
        import time

        servers_config = load_servers_config()

        # Находим активный удалённый сервер с панелью для создания ключа
        active_server = None
        for server in servers_config.get('servers', []):
            if not server.get('enabled', True):
                continue
            if not server.get('active_for_new', True):
                continue
            if server.get('panel', {}).get('url'):
                active_server = server
                break

        if not active_server:
            await callback.message.edit_text(
                "❌ Нет активного сервера для создания ключей.\n"
                "Включите сервер в настройках."
            )
            await state.clear()
            return

        panel_config = active_server.get('panel', {})
        panel_url = panel_config.get('url')
        panel_user = panel_config.get('username')
        panel_pass = panel_config.get('password')
        main_inbound = active_server.get('inbounds', {}).get('main', {})
        inbound_id = main_inbound.get('id', 1)
        server_domain = active_server.get('domain', active_server.get('ip', ''))
        server_port = active_server.get('port', 443)

        # Сначала проверяем клиента в ЛОКАЛЬНОЙ базе (xui_client читает напрямую из SQLite)
        local_client = await xui_client.find_client_by_email(phone)
        if local_client:
            logger.info(f"Найден клиент {phone} в локальной базе: UUID={local_client.get('id')}, expiry={local_client.get('expiryTime')}")
            # Используем данные из локальной базы если не переданы из состояния
            if original_ip_limit == 2 and local_client.get('limitIp'):
                original_ip_limit = local_client.get('limitIp')
            if original_expiry == 0 and local_client.get('expiryTime'):
                original_expiry = local_client.get('expiryTime')

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            # Авторизация на удалённом сервере
            login_url = f"{panel_url}/login"
            login_data = {"username": panel_user, "password": panel_pass}
            async with session.post(login_url, data=login_data, timeout=aiohttp.ClientTimeout(total=15)) as login_resp:
                login_result = await login_resp.json()
                if not login_result.get('success'):
                    await callback.message.edit_text("❌ Ошибка авторизации в панели сервера")
                    await state.clear()
                    return

            # Проверяем, существует ли клиент с таким email на УДАЛЁННОМ сервере
            inbounds_url = f"{panel_url}/panel/api/inbounds/get/{inbound_id}"
            logger.info(f"Запрос к удалённому серверу: {inbounds_url}")
            async with session.get(inbounds_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                logger.info(f"Ответ от удалённого сервера: status={resp.status}")
                if resp.status != 200:
                    resp_text = await resp.text()
                    logger.error(f"Ошибка получения inbound: status={resp.status}, body={resp_text[:200]}")
                    await callback.message.edit_text(f"❌ Ошибка получения данных inbound (статус {resp.status})")
                    await state.clear()
                    return

                inb_data = await resp.json()
                if not inb_data.get('success'):
                    await callback.message.edit_text("❌ Inbound не найден на сервере")
                    await state.clear()
                    return

                inbound_obj = inb_data.get('obj', {})
                settings = json.loads(inbound_obj.get('settings', '{}'))
                existing_clients = settings.get('clients', [])

                # Ищем клиента по email на удалённом сервере
                existing_client = None
                for client in existing_clients:
                    if client.get('email') == phone:
                        existing_client = client
                        break

            if existing_client:
                # Клиент уже существует на удалённом сервере - возвращаем его ключ
                client_uuid = existing_client.get('id')
                logger.info(f"Клиент {phone} уже существует на сервере {active_server.get('name')}, UUID: {client_uuid}")
            else:
                # Создаём нового клиента на удалённом сервере
                client_uuid = str(uuid.uuid4())

                # Вычисляем время истечения
                if use_original_expiry and original_expiry > 0:
                    # Используем оригинальную дату истечения
                    expire_time = original_expiry
                else:
                    # Новая дата на основе period_days
                    expire_time = int((time.time() + period_days * 24 * 60 * 60) * 1000)

                new_client = {
                    "id": client_uuid,
                    "flow": main_inbound.get('flow', ''),
                    "email": phone,
                    "limitIp": original_ip_limit,
                    "totalGB": 0,
                    "expiryTime": expire_time,
                    "enable": True,
                    "tgId": "",
                    "subId": "",
                    "reset": 0
                }

                add_client_data = {
                    "id": inbound_id,
                    "settings": json.dumps({"clients": [new_client]})
                }

                add_url = f"{panel_url}/panel/api/inbounds/addClient"
                async with session.post(add_url, json=add_client_data, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    result = await resp.json()
                    if not result.get('success'):
                        error_msg = result.get('msg', 'Неизвестная ошибка')
                        await callback.message.edit_text(
                            f"❌ Ошибка создания клиента: {error_msg}"
                        )
                        await state.clear()
                        return

                logger.info(f"Создан клиент {phone} на сервере {active_server.get('name')}, UUID: {client_uuid}")

        # Формируем VLESS ссылку
        network = main_inbound.get('network', 'tcp')
        params = [f"type={network}", "encryption=none"]

        # Добавляем gRPC параметры если нужно
        if network == 'grpc':
            params.append(f"serviceName={main_inbound.get('serviceName', '')}")
            params.append(f"authority={main_inbound.get('authority', '')}")

        params.append(f"security={main_inbound.get('security', 'reality')}")

        if main_inbound.get('security') == 'reality':
            if main_inbound.get('pbk'):
                params.append(f"pbk={main_inbound['pbk']}")
            params.append(f"fp={main_inbound.get('fp', 'chrome')}")
            if main_inbound.get('sni'):
                params.append(f"sni={main_inbound['sni']}")
            if main_inbound.get('sid'):
                params.append(f"sid={main_inbound['sid']}")
            params.append("spx=%2F")

        query = '&'.join(params)
        name_prefix = main_inbound.get('name_prefix', active_server.get('name', 'VPN'))
        # Формируем имя как в get_client_link_from_active_server: PREFIX пробел EMAIL
        display_name = f"{name_prefix} {phone}" if name_prefix else phone

        vless_link_for_user = f"vless://{client_uuid}@{server_domain}:{server_port}?{query}#{display_name}"

        # Сохраняем в базу данных ЗАМЕН
        await db.add_key_replacement(
            manager_id=user_id,
            client_email=phone,
            phone_number=phone,
            period=period_name,
            expire_days=period_days,
            client_id=client_uuid
        )

        # Формируем ссылку подписки
        subscription_url = f"https://zov-gor.ru/sub/{client_uuid}"

        # Генерируем QR код
        try:
            qr_code = generate_qr_code(vless_link_for_user)

            await callback.message.answer_photo(
                BufferedInputFile(qr_code.read(), filename="qrcode.png"),
                caption=(
                    f"🔄 Ключ успешно заменен!\n\n"
                    f"🆔 ID клиента: {phone}\n"
                    f"⏰ Срок действия: {period_name}\n"
                    f"🌐 Лимит IP: 2\n"
                    f"📊 Трафик: безлимит\n\n"
                    f"📱 Отсканируйте QR код в приложении VPN"
                )
            )

            await callback.message.answer(
                f"📋 VLESS ключ:\n\n`{vless_link_for_user}`\n\n"
                f"🔄 Ссылка подписки (автообновление):\n`{subscription_url}`\n\n"
                f"💡 Скопируйте и отправьте клиенту.",
                parse_mode="Markdown"
            )

            await callback.message.delete()

        except Exception as e:
            print(f"QR generation error: {e}")
            await callback.message.edit_text(
                f"🔄 Ключ успешно заменен!\n\n"
                f"🆔 ID клиента: {phone}\n"
                f"⏰ Срок действия: {period_name}\n"
                f"🌐 Лимит IP: 2\n\n"
                f"📋 VLESS ключ:\n`{vless_link_for_user}`\n\n"
                f"🔄 Ссылка подписки:\n`{subscription_url}`",
                parse_mode="Markdown"
            )

        # Возвращаем в главное меню
        is_admin = user_id == ADMIN_ID
        await callback.message.answer(
            "✅ Готово!",
            reply_markup=Keyboards.main_menu(is_admin)
        )

    except Exception as e:
        await callback.message.edit_text(
            f"Произошла ошибка при замене ключа: {str(e)}"
        )

    finally:
        await state.clear()

    await callback.answer()


# ============ ИСПРАВЛЕНИЕ КЛЮЧА ============

@router.message(F.text == "🔧 Исправить ключ")
async def start_fix_key(message: Message, state: FSMContext, db: DatabaseManager):
    """Начало исправления ключа"""
    user_id = message.from_user.id

    # Проверка авторизации
    if not await is_authorized(user_id, db):
        await message.answer("У вас нет доступа к этой функции.")
        return

    await state.set_state(FixKeyStates.waiting_for_key)
    await message.answer(
        "🔧 <b>Исправление ключа</b>\n\n"
        "Вставьте VLESS ключ, который нужно исправить.\n\n"
        "Функция исправит параметры ключа (SNI, pbk, sid, flow) "
        "по текущему конфигу активного сервера.\n\n"
        "Пример:\n<code>vless://uuid@server:443?...</code>",
        parse_mode="HTML",
        reply_markup=Keyboards.cancel_button()
    )


@router.message(FixKeyStates.waiting_for_key, F.text == "Отмена")
async def cancel_fix_key(message: Message, state: FSMContext):
    """Отмена исправления ключа"""
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID

    await state.clear()
    await message.answer(
        "Исправление ключа отменено.",
        reply_markup=Keyboards.main_menu(is_admin)
    )


@router.message(FixKeyStates.waiting_for_key)
async def process_fix_key(message: Message, state: FSMContext):
    """Обработка VLESS ключа для исправления - ищет клиента на сервере по UUID"""
    import urllib.parse
    from datetime import datetime, timedelta
    from bot.api.remote_xui import (
        load_servers_config, find_client_on_server,
        find_client_on_local_server, create_client_via_panel
    )

    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID
    vless_link = message.text.strip()

    if not vless_link.startswith('vless://'):
        await message.answer(
            "❌ Неверный формат. Ключ должен начинаться с <code>vless://</code>",
            parse_mode="HTML"
        )
        return

    try:
        # Парсим ссылку
        link_without_proto = vless_link[8:]

        if '#' in link_without_proto:
            main_part, original_fragment = link_without_proto.rsplit('#', 1)
        else:
            main_part, original_fragment = link_without_proto, ""

        if '?' in main_part:
            address_part, query_string = main_part.split('?', 1)
        else:
            address_part, query_string = main_part, ""

        if '@' not in address_part:
            await message.answer("❌ Неверный формат: отсутствует UUID")
            return

        uuid_part, host_port = address_part.rsplit('@', 1)

        # Загружаем конфиг серверов
        servers_config = load_servers_config()

        # Находим активный сервер (Germany)
        target_server = None
        for srv in servers_config.get('servers', []):
            if srv.get('active_for_new'):
                target_server = srv
                break

        if not target_server:
            await message.answer("❌ Активный сервер не найден в конфиге")
            await state.clear()
            return

        await message.answer("🔍 Ищу клиента на серверах...")

        # Сначала ищем на Germany (активный сервер)
        client_info = await find_client_on_server(target_server, uuid_part)
        found_on_germany = client_info is not None
        created_on_germany = False

        if not client_info:
            # Не нашли на Germany - ищем на локальном сервере
            await message.answer("🔍 Не найден на Germany, ищу на локальном...")
            local_client = await find_client_on_local_server(uuid_part)

            if local_client:
                # Нашли на локальном - берём данные и создаём на Germany
                client_email = local_client.get('email', '')
                expiry_time = local_client.get('expiry_time', 0)
                limit_ip = local_client.get('limit_ip', 2)

                # Вычисляем оставшиеся дни
                if expiry_time > 0:
                    expiry_date = datetime.fromtimestamp(expiry_time / 1000)
                    now = datetime.now()
                    if expiry_date > now:
                        expire_days = (expiry_date - now).days + 1
                    else:
                        expire_days = 30  # Истёк - даём 30 дней
                else:
                    expire_days = 365  # Безлимит

                await message.answer(f"📤 Создаю клиента {client_email} на Germany...")

                # Создаём на Germany через API панели
                create_result = await create_client_via_panel(
                    server_config=target_server,
                    client_uuid=uuid_part,
                    email=client_email,
                    expire_days=expire_days,
                    ip_limit=limit_ip
                )

                if create_result.get('success'):
                    created_on_germany = True
                    actual_uuid = create_result.get('uuid', uuid_part)
                    if create_result.get('existing'):
                        await message.answer(f"✅ Клиент уже есть на Germany!")
                    else:
                        await message.answer(f"✅ Клиент создан на Germany!")

                    # Ищем клиента заново для получения реальных параметров inbound
                    client_info = await find_client_on_server(target_server, actual_uuid)
                    if not client_info:
                        # Fallback если поиск не удался
                        client_info = {
                            'email': client_email,
                            'inbound_name': 'main',
                            'inbound_remark': 'ГОС',
                            'expiry_time': expiry_time,
                            'limit_ip': limit_ip
                        }
                else:
                    error_msg = create_result.get('error', 'Неизвестная ошибка')
                    await message.answer(f"⚠️ Не удалось создать: {error_msg}")

        if client_info:
            # Нашли клиента - берём данные
            client_email = client_info.get('email', '')
            client_inbound = client_info.get('inbound_name', 'main')
            inbound_remark = client_info.get('inbound_remark', client_inbound)

            # Используем РЕАЛЬНЫЕ параметры inbound с Germany
            real_inbound = client_info.get('inbound_settings', {})
            if real_inbound:
                inbound_config = real_inbound
            else:
                # Fallback на конфиг Germany
                inbound_config = target_server.get('inbounds', {}).get(client_inbound, {})
                if not inbound_config:
                    inbound_config = target_server.get('inbounds', {}).get('main', {})

            # Формируем имя для ключа: PREFIX пробел EMAIL (БЕЗ url-encode, как в get_client_link_from_active_server)
            link_name = f"{inbound_remark} {client_email}"
            found_on_server = True
        else:
            # Не нашли нигде - используем оригинальный fragment и main inbound Germany
            link_name = urllib.parse.unquote(original_fragment) if original_fragment else "Unknown"
            inbound_config = target_server.get('inbounds', {}).get('main', {})
            client_email = link_name
            inbound_remark = "Unknown"
            found_on_server = False

        # Формируем исправленный ключ с настройками Germany
        # Порядок параметров как в get_client_link_from_active_server: type, security, encryption, pbk, fp, sni, sid, flow, spx
        target_domain = target_server.get('domain', target_server.get('ip'))
        target_port = target_server.get('port', 443)

        security = inbound_config.get('security', 'reality')
        network = inbound_config.get('network', 'tcp')
        client_flow = client_info.get('flow', '') if client_info else ''

        params = [
            f"type={network}",
            "encryption=none"
        ]

        # Добавляем gRPC параметры если нужно
        if network == 'grpc':
            params.append(f"serviceName={inbound_config.get('serviceName', '')}")
            params.append(f"authority={inbound_config.get('authority', '')}")

        params.append(f"security={security}")

        if security == 'reality':
            if inbound_config.get('pbk'):
                params.append(f"pbk={inbound_config['pbk']}")
            params.append(f"fp={inbound_config.get('fp', 'chrome')}")
            if inbound_config.get('sni'):
                params.append(f"sni={inbound_config['sni']}")
            if inbound_config.get('sid'):
                params.append(f"sid={inbound_config['sid']}")
            if client_flow:
                params.append(f"flow={client_flow}")
            params.append("spx=%2F")

        new_query = '&'.join(params)

        fixed_link = f"vless://{uuid_part}@{target_domain}:{target_port}?{new_query}#{link_name}"

        # Генерируем QR код
        qr_code = generate_qr_code(fixed_link)

        # Формируем информацию об изменениях
        changes = []
        if target_domain not in vless_link:
            changes.append(f"• Хост: {target_domain}")
        if str(target_port) not in vless_link:
            changes.append(f"• Порт: {target_port}")
        if inbound_config.get('sni') and inbound_config['sni'] not in vless_link:
            changes.append(f"• SNI: {inbound_config['sni']}")
        if inbound_config.get('pbk') and inbound_config['pbk'] not in vless_link:
            changes.append(f"• Public Key: обновлён")
        if 'flow=' in vless_link and not client_flow:
            changes.append("• Flow: убран")
        elif client_flow and client_flow not in vless_link:
            changes.append(f"• Flow: {client_flow}")
        original_name = urllib.parse.unquote(original_fragment) if original_fragment else ""
        if found_on_server and original_name != link_name:
            changes.append(f"• Имя: из базы сервера")

        changes_text = "\n".join(changes) if changes else "Параметры актуальны"

        if created_on_germany:
            status_text = "✅ Создан на Germany (из локальной базы)"
        elif found_on_germany:
            status_text = "✅ Найден на Germany"
        elif found_on_server:
            status_text = "✅ Найден на Germany"
        else:
            status_text = "⚠️ Не найден, использованы параметры Germany"

        await message.answer_photo(
            BufferedInputFile(qr_code.read(), filename="qrcode.png"),
            caption=(
                f"✅ <b>Ключ исправлен!</b>\n\n"
                f"🖥 Сервер: {target_server.get('name', 'Unknown')}\n"
                f"📍 Inbound: {inbound_remark}\n"
                f"👤 Клиент: {client_email}\n"
                f"🔍 Статус: {status_text}\n"
                f"🌐 Хост: {target_domain}:{target_port}\n"
                f"🔒 SNI: {inbound_config.get('sni', 'N/A')}\n"
                f"📡 Flow: {client_flow or 'пусто'}\n\n"
                f"<b>Изменения:</b>\n{changes_text}"
            ),
            parse_mode="HTML"
        )

        await message.answer(
            f"📋 <b>Исправленный VLESS ключ:</b>\n\n"
            f"<code>{fixed_link}</code>\n\n"
            f"💡 Скопируйте и отправьте клиенту.",
            parse_mode="HTML"
        )

    except Exception as e:
        logger.error(f"Error fixing key: {e}")
        import traceback
        traceback.print_exc()
        await message.answer(f"❌ Ошибка при обработке ключа: {str(e)[:100]}")

    finally:
        await state.clear()
        await message.answer(
            "Главное меню:",
            reply_markup=Keyboards.main_menu(is_admin)
        )


@router.message(F.text == "Моя статистика")
async def show_my_stats(message: Message, db: DatabaseManager):
    """Показать статистику менеджера"""
    user_id = message.from_user.id

    # Проверка авторизации
    if not await is_authorized(user_id, db):
        await message.answer("У вас нет доступа к этой функции.")
        return

    # Получаем статистику
    stats = await db.get_manager_stats(user_id)
    revenue_stats = await db.get_manager_revenue_stats(user_id)
    replacement_stats = await db.get_replacement_stats(user_id)

    stats_text = (
        f"📊 <b>ВАША СТАТИСТИКА</b>\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 <b>ДОХОДЫ:</b>\n"
        f"💵 Всего заработано: <b>{revenue_stats['total']:,} ₽</b>\n"
        f"📅 За сегодня: <b>{revenue_stats['today']:,} ₽</b>\n"
        f"📆 За месяц: <b>{revenue_stats['month']:,} ₽</b>\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🔑 <b>СОЗДАННЫЕ КЛЮЧИ:</b>\n"
        f"Всего создано: <b>{stats['total']}</b>\n"
        f"Создано сегодня: <b>{stats['today']}</b>\n"
        f"Создано за месяц: <b>{stats['month']}</b>\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🔄 <b>ЗАМЕНЫ КЛЮЧЕЙ:</b>\n"
        f"Всего замен: <b>{replacement_stats['total']}</b>\n"
        f"Замен сегодня: <b>{replacement_stats['today']}</b>\n"
        f"Замен за месяц: <b>{replacement_stats['month']}</b>\n"
    )

    # Получаем последние 5 ключей
    history = await db.get_manager_history(user_id, limit=5)

    if history:
        stats_text += "\n━━━━━━━━━━━━━━━━\n"
        stats_text += "📋 <b>Последние 5 ключей:</b>\n\n"
        for item in history:
            # Вычисляем дату истечения
            expire_date_str = ""
            if item.get('expire_days') and item.get('created_at'):
                try:
                    created_at = datetime.strptime(item['created_at'][:19], '%Y-%m-%d %H:%M:%S')
                    expire_date = created_at + timedelta(days=item['expire_days'])
                    expire_date_str = f" → до {expire_date.strftime('%d.%m.%Y')}"
                except:
                    pass
            stats_text += f"• {item['phone_number']} - {item['period']}{expire_date_str}\n"

    await message.answer(stats_text, parse_mode="HTML")


@router.message(F.text == "/list_inbounds")
async def list_inbounds(message: Message, xui_client: XUIClient):
    """Показать список всех inbound'ов (только для админа)"""
    user_id = message.from_user.id

    # Проверка прав админа
    if user_id != ADMIN_ID:
        await message.answer("⛔️ У вас нет доступа к этой команде.")
        return

    # Получаем список inbound'ов
    inbounds = await xui_client.list_inbounds()

    if not inbounds:
        await message.answer("❌ Не удалось получить список inbound'ов.")
        return

    # Формируем сообщение
    text = "🔌 <b>Список доступных inbound'ов:</b>\n\n"

    for inbound in inbounds:
        inbound_id = inbound.get('id')
        remark = inbound.get('remark', f'Inbound {inbound_id}')
        protocol = inbound.get('protocol', 'unknown')
        port = inbound.get('port', '?')
        enable = inbound.get('enable', False)

        # Статус inbound
        status_emoji = "✅" if enable else "❌"

        # Информация о маппинге портов (внутренний порт → внешний порт 443)
        port_mapping = f"{port} → 443" if port != 443 else f"{port}"

        text += (
            f"{status_emoji} <b>{remark}</b>\n"
            f"   ID: <code>{inbound_id}</code>\n"
            f"   Протокол: {protocol}\n"
            f"   Порт: {port_mapping}\n\n"
        )

    text += "━━━━━━━━━━━━━━━━\n"
    text += "ℹ️ Все порты маппятся на внешний порт 443"

    await message.answer(text, parse_mode="HTML")


@router.message(F.text == "/edit_reality")
async def start_edit_reality(message: Message, state: FSMContext, xui_client: XUIClient):
    """Начать редактирование REALITY параметров (только для админа)"""
    user_id = message.from_user.id

    # Проверка прав админа
    if user_id != ADMIN_ID:
        await message.answer("⛔️ У вас нет доступа к этой команде.")
        return

    # Получаем список inbound'ов
    inbounds = await xui_client.list_inbounds()

    if not inbounds:
        await message.answer("❌ Не удалось получить список inbound'ов.")
        return

    # Фильтруем только inbound'ы с REALITY
    reality_inbounds = []
    for inbound in inbounds:
        stream_settings = inbound.get('streamSettings')
        if stream_settings:
            import json
            try:
                settings = json.loads(stream_settings) if isinstance(stream_settings, str) else stream_settings
                if settings.get('security') == 'reality':
                    reality_inbounds.append(inbound)
            except:
                continue

    if not reality_inbounds:
        await message.answer("❌ Не найдено inbound'ов с REALITY.")
        return

    await state.set_state(EditRealityStates.waiting_for_inbound_selection)

    await message.answer(
        "🔐 <b>Редактирование REALITY параметров</b>\n\n"
        "Выберите inbound для редактирования:",
        reply_markup=Keyboards.inbound_selection(reality_inbounds),
        parse_mode="HTML"
    )


@router.message(EditRealityStates.waiting_for_dest)
async def process_dest_input(message: Message, state: FSMContext):
    """Обработка ввода Dest (Target)"""
    dest = message.text.strip()

    # Валидация формата dest (должен быть domain:port)
    if ':' not in dest:
        await message.answer(
            "❌ Неверный формат!\n\n"
            "Dest должен быть в формате: <code>domain.com:443</code>\n"
            "Попробуйте еще раз:",
            parse_mode="HTML"
        )
        return

    parts = dest.split(':')
    if len(parts) != 2:
        await message.answer(
            "❌ Неверный формат!\n\n"
            "Dest должен быть в формате: <code>domain.com:443</code>\n"
            "Попробуйте еще раз:",
            parse_mode="HTML"
        )
        return

    domain, port = parts
    try:
        port_num = int(port)
        if port_num < 1 or port_num > 65535:
            raise ValueError()
    except ValueError:
        await message.answer(
            "❌ Неверный порт!\n\n"
            "Порт должен быть числом от 1 до 65535\n"
            "Попробуйте еще раз:",
            parse_mode="HTML"
        )
        return

    # Сохраняем новый dest
    await state.update_data(new_dest=dest)
    await state.set_state(EditRealityStates.waiting_for_sni)

    data = await state.get_data()
    current_sni = data.get('current_sni', '')

    await message.answer(
        f"✅ Dest установлен: <code>{dest}</code>\n\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"📍 <b>Текущий SNI:</b> <code>{current_sni}</code>\n\n"
        f"Введите новый <b>SNI (Server Names)</b>:\n"
        f"Формат: домены через запятую\n\n"
        f"Пример: <code>vk.com,www.vk.com</code>",
        parse_mode="HTML"
    )


@router.message(EditRealityStates.waiting_for_sni)
async def process_sni_input(message: Message, state: FSMContext):
    """Обработка ввода SNI"""
    sni_input = message.text.strip()

    # Разделяем по запятой и очищаем от пробелов
    sni_list = [s.strip() for s in sni_input.split(',') if s.strip()]

    if not sni_list:
        await message.answer(
            "❌ SNI не может быть пустым!\n\n"
            "Введите хотя бы один домен.\n"
            "Попробуйте еще раз:",
            parse_mode="HTML"
        )
        return

    # Сохраняем новый SNI
    await state.update_data(new_sni=sni_list)
    await state.set_state(EditRealityStates.confirm)

    data = await state.get_data()
    inbound_id = data.get('inbound_id')
    current_dest = data.get('current_dest')
    current_sni = data.get('current_sni')
    new_dest = data.get('new_dest')
    new_sni_str = ', '.join(sni_list)

    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    await message.answer(
        f"🔐 <b>Подтверждение изменений REALITY</b>\n\n"
        f"Inbound ID: <code>{inbound_id}</code>\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"<b>Было:</b>\n"
        f"📍 Dest: <code>{current_dest}</code>\n"
        f"🌐 SNI: <code>{current_sni}</code>\n\n"
        f"<b>Будет:</b>\n"
        f"📍 Dest: <code>{new_dest}</code>\n"
        f"🌐 SNI: <code>{new_sni_str}</code>\n\n"
        f"━━━━━━━━━━━━━━━━\n\n"
        f"❓ Применить изменения?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Применить", callback_data="reality_confirm_yes"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="reality_confirm_no")
            ]
        ]),
        parse_mode="HTML"
    )


@router.callback_query(F.data == "reality_confirm_yes")
async def confirm_reality_changes(callback: CallbackQuery, state: FSMContext, xui_client: XUIClient):
    """Применение изменений REALITY параметров"""
    data = await state.get_data()
    inbound_id = data.get('inbound_id')
    new_dest = data.get('new_dest')
    new_sni = data.get('new_sni')

    await callback.message.edit_text("⏳ Применение изменений...")

    try:
        # Обновляем inbound с новыми REALITY параметрами
        success = await xui_client.update_reality_settings(inbound_id, new_dest, new_sni)

        if success:
            new_sni_str = ', '.join(new_sni)
            await callback.message.edit_text(
                f"✅ <b>REALITY параметры успешно обновлены!</b>\n\n"
                f"Inbound ID: <code>{inbound_id}</code>\n"
                f"📍 Dest: <code>{new_dest}</code>\n"
                f"🌐 SNI: <code>{new_sni_str}</code>\n\n"
                f"━━━━━━━━━━━━━━━━\n"
                f"ℹ️ Изменения вступят в силу немедленно.\n"
                f"Новые клиенты будут использовать обновленные параметры.",
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                "❌ Не удалось обновить REALITY параметры.\n"
                "Проверьте подключение к X-UI панели."
            )
    except Exception as e:
        await callback.message.edit_text(
            f"❌ Ошибка при обновлении REALITY параметров:\n"
            f"<code>{str(e)}</code>",
            parse_mode="HTML"
        )

    await state.clear()
    await callback.answer()


@router.callback_query(F.data == "reality_confirm_no")
async def cancel_reality_changes(callback: CallbackQuery, state: FSMContext):
    """Отмена изменений REALITY параметров"""
    await callback.message.edit_text(
        "❌ Изменения отменены."
    )
    await state.clear()
    await callback.answer()
