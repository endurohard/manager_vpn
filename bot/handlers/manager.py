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


async def _get_allowed_servers(user_id: int, db: DatabaseManager, all_servers: list) -> list:
    """Фильтрация серверов по разрешениям менеджера. Админ видит все."""
    if user_id == ADMIN_ID:
        return all_servers
    allowed = await db.get_manager_allowed_servers(user_id)
    if allowed is None:
        return all_servers
    return [s for s in all_servers if s.get('name') in allowed]


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


class SearchClientStates(StatesGroup):
    """Состояния для поиска клиента менеджером"""
    waiting_for_query = State()


class FixKeyStates(StatesGroup):
    """Состояния для исправления ключа"""
    waiting_for_key = State()
    waiting_for_server_selection = State()  # Для админа - выбор сервера


class MgrAddServerStates(StatesGroup):
    """Состояния для добавления сервера менеджером"""
    waiting_for_search = State()
    waiting_for_server_select = State()
    confirming = State()
    waiting_for_traffic_choice = State()


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
async def generate_user_identifier(message: Message, state: FSMContext, xui_client: XUIClient, db: DatabaseManager):
    """Генерация случайного ID пользователя"""
    from bot.api.remote_xui import load_servers_config

    user_id_value = generate_user_id()
    await state.update_data(phone=user_id_value)

    # Загружаем список серверов
    servers_config = load_servers_config()
    servers = [s for s in servers_config.get('servers', []) if s.get('enabled', True) and not s.get('local', False)]
    servers = await _get_allowed_servers(message.from_user.id, db, servers)

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

    all_indices = list(range(len(servers)))
    await state.update_data(servers=servers, selected_server_indices=all_indices)
    await state.set_state(CreateKeyStates.waiting_for_server)
    await message.answer(
        f"🆔 Сгенерирован ID: <code>{user_id_value}</code>\n\n"
        f"🖥 <b>Выберите серверы</b> (можно несколько):\n"
        f"Нажмите на сервер чтобы вкл/выкл, затем ✅ Продолжить",
        reply_markup=Keyboards.server_multi_selection(servers, all_indices),
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
async def process_phone_input(message: Message, state: FSMContext, xui_client: XUIClient, db: DatabaseManager):
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
        servers = await _get_allowed_servers(user_id, db, servers)

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
            all_indices = list(range(len(servers)))
            await state.update_data(servers=servers, selected_server_indices=all_indices)
            await state.set_state(CreateKeyStates.waiting_for_server)
            await message.answer(
                f"🆔 Сгенерирован ID: <code>{generated_id}</code>\n\n"
                f"🖥 <b>Выберите серверы</b> (можно несколько):\n"
                f"Нажмите на сервер чтобы вкл/выкл, затем ✅ Продолжить",
                reply_markup=Keyboards.server_multi_selection(servers, all_indices),
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
    servers = await _get_allowed_servers(user_id, db, servers)

    if not servers:
        # Если нет удалённых серверов, используем локальный
        await state.set_state(CreateKeyStates.waiting_for_period)
        await message.answer(
            format_message + "Выберите срок действия ключа:",
            reply_markup=Keyboards.subscription_periods(),
            parse_mode="HTML"
        )
        return

    all_indices = list(range(len(servers)))
    await state.update_data(servers=servers, selected_server_indices=all_indices)
    await state.set_state(CreateKeyStates.waiting_for_server)
    await message.answer(
        format_message +
        "🖥 <b>Выберите серверы</b> (можно несколько):\n"
        "Нажмите на сервер чтобы вкл/выкл, затем ✅ Продолжить",
        reply_markup=Keyboards.server_multi_selection(servers, all_indices),
        parse_mode="HTML"
    )


@router.callback_query(CreateKeyStates.waiting_for_server, F.data.startswith("mserver_"))
async def process_multi_server_toggle(callback: CallbackQuery, state: FSMContext):
    """Переключение сервера в мульти-выборе"""
    data = await state.get_data()
    servers = data.get('servers', [])
    selected = data.get('selected_server_indices', [])
    action = callback.data.replace("mserver_", "")

    if action == "all":
        # Переключить все: если все выбраны — снять все, иначе выбрать все
        if set(selected) == set(range(len(servers))):
            selected = []
        else:
            selected = list(range(len(servers)))
        await state.update_data(selected_server_indices=selected)
        await callback.message.edit_reply_markup(
            reply_markup=Keyboards.server_multi_selection(servers, selected)
        )
        await callback.answer()
        return

    if action == "done":
        # Продолжить — переход к выбору периода
        if not selected:
            await callback.answer("Выберите хотя бы один сервер!", show_alert=True)
            return

        phone = data.get('phone', '')
        selected_servers = [servers[i] for i in selected if i < len(servers)]

        # Если один сервер — сохраняем как раньше
        if len(selected_servers) == 1:
            srv = selected_servers[0]
            main_inbound = srv.get('inbounds', {}).get('main', {})
            await state.update_data(
                selected_server=srv,
                selected_inbound=main_inbound,
                inbound_id=main_inbound.get('id', 1),
                multi_servers=None
            )
            server_text = srv.get('name', 'Unknown')
        else:
            # Мульти-сервер: сохраняем список
            await state.update_data(
                selected_server=selected_servers[0],
                selected_inbound=selected_servers[0].get('inbounds', {}).get('main', {}),
                inbound_id=selected_servers[0].get('inbounds', {}).get('main', {}).get('id', 1),
                multi_servers=selected_servers
            )
            server_text = ", ".join(s.get('name', '?') for s in selected_servers)

        await state.set_state(CreateKeyStates.waiting_for_period)
        await callback.message.edit_text(
            f"🆔 ID: <code>{phone}</code>\n"
            f"🖥 Серверы: <b>{server_text}</b>\n\n"
            "Выберите срок действия ключа:",
            reply_markup=Keyboards.subscription_periods(),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # Toggle конкретного сервера
    idx = int(action)
    if idx in selected:
        selected.remove(idx)
    else:
        selected.append(idx)

    await state.update_data(selected_server_indices=selected)
    await callback.message.edit_reply_markup(
        reply_markup=Keyboards.server_multi_selection(servers, selected)
    )
    await callback.answer()


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
    multi_servers = data.get("multi_servers")  # Список серверов для мульти-выбора

    await callback.message.edit_text("Создание ключа...")

    try:
        # Если выбраны серверы (один или несколько)
        if selected_server and not selected_server.get('local', False):
            import uuid as uuid_module
            from bot.api.remote_xui import create_client_on_remote_server, load_servers_config

            client_uuid = str(uuid_module.uuid4())

            # Определяем список серверов для создания
            servers_to_create = multi_servers if multi_servers else [selected_server]

            # Вычисляем expire_timestamp (мс)
            from datetime import timedelta
            _expire_ts = int((datetime.now() + timedelta(days=period_days)).timestamp() * 1000)

            created_servers = []
            failed_servers = []

            for srv in servers_to_create:
                srv_name = srv.get('name', 'Unknown')
                srv_inbound_id = srv.get('inbounds', {}).get('main', {}).get('id', 1)
                srv_total_gb = srv.get('traffic_limit_gb', 0)
                try:
                    success = await create_client_on_remote_server(
                        server_config=srv,
                        client_uuid=client_uuid,
                        email=phone,
                        expire_days=period_days,
                        ip_limit=2,
                        inbound_id=srv_inbound_id,
                        total_gb=srv_total_gb
                    )
                    if success:
                        created_servers.append(srv_name)
                        await db.add_client_server(
                            client_uuid=client_uuid, client_email=phone,
                            server_name=srv_name,
                            inbound_id=srv_inbound_id, expire_days=period_days,
                            expire_timestamp=_expire_ts,
                            total_gb=srv_total_gb, ip_limit=2
                        )
                    else:
                        failed_servers.append(srv_name)
                except Exception as e:
                    logger.error(f"Ошибка создания на {srv_name}: {e}")
                    failed_servers.append(srv_name)

            # Авто-добавление на серверы с лимитом трафика (LTE) если не были выбраны явно
            created_names = set(created_servers)
            all_servers = load_servers_config().get('servers', [])
            for srv in all_servers:
                if (srv.get('traffic_limit_gb', 0) > 0
                        and srv.get('enabled', True)
                        and not srv.get('local', False)
                        and srv.get('name') not in created_names):
                    try:
                        await create_client_on_remote_server(
                            server_config=srv,
                            client_uuid=client_uuid,
                            email=phone,
                            expire_days=period_days,
                            ip_limit=2,
                            total_gb=srv.get('traffic_limit_gb', 0)
                        )
                        await db.add_client_server(
                            client_uuid=client_uuid, client_email=phone,
                            server_name=srv.get('name', ''),
                            inbound_id=srv.get('inbounds', {}).get('main', {}).get('id', 1),
                            expire_days=period_days,
                            expire_timestamp=_expire_ts,
                            total_gb=srv.get('traffic_limit_gb', 0), ip_limit=2
                        )
                        logger.info(f"Авто-добавлен на {srv.get('name')} с лимитом {srv.get('traffic_limit_gb')} ГБ")
                    except Exception as e:
                        logger.error(f"Ошибка авто-добавления на {srv.get('name')}: {e}")

            if created_servers:
                client_data = {
                    'client_id': client_uuid,
                    'local_created': False,
                    'created_servers': created_servers,
                    'failed_servers': failed_servers
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

            # Фиксируем серверы для старой логики
            if client_data and not client_data.get('error'):
                _expire_ts_old = client_data.get('expire_time', 0)
                from bot.api.remote_xui import load_servers_config as _lsc
                _cfg = _lsc()
                for srv in _cfg.get('servers', []):
                    if not srv.get('enabled', True) or not srv.get('active_for_new', True):
                        continue
                    try:
                        await db.add_client_server(
                            client_uuid=client_data['client_id'], client_email=phone,
                            server_name=srv.get('name', ''),
                            inbound_id=srv.get('inbounds', {}).get('main', {}).get('id', 1),
                            expire_days=period_days,
                            expire_timestamp=_expire_ts_old,
                            total_gb=srv.get('traffic_limit_gb', 0), ip_limit=2
                        )
                    except Exception as e:
                        logger.error(f"Ошибка фиксации сервера {srv.get('name')}: {e}")

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
            from bot.api.remote_xui import load_servers_config, get_inbound_settings_from_panel

            # Если есть выбранный сервер - используем его, иначе ищем первый активный
            target_server = selected_server
            target_inbound = selected_inbound

            if not target_server:
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
                # Получаем актуальные настройки inbound с панели сервера
                inbound_id = target_inbound.get('id', 1)
                panel_settings = await get_inbound_settings_from_panel(target_server, inbound_id)

                # Если получили настройки с панели - используем их, иначе статический конфиг
                if panel_settings:
                    target_inbound = {**target_inbound, **panel_settings}
                    logger.info(f"Используем актуальные настройки с панели: sni={panel_settings.get('sni')}")

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

        # Формируем имя серверов для записи
        created_srv_list = client_data.get('created_servers', [])
        failed_srv_list = client_data.get('failed_servers', [])
        server_name_for_db = ", ".join(created_srv_list) if created_srv_list else (selected_server.get('name') if selected_server else None)

        # Сохраняем в базу данных
        await db.add_key_to_history(
            manager_id=user_id,
            client_email=phone,
            phone_number=phone,
            period=period_name,
            expire_days=period_days,
            client_id=client_uuid,
            price=period_price,
            server_name=server_name_for_db
        )

        # Формируем ссылку подписки
        subscription_url = f"https://zov-gor.ru/sub/{client_uuid}"

        # Информация о серверах
        servers_info = ""
        if created_srv_list:
            servers_info = f"🖥 Серверы: {', '.join(created_srv_list)}\n"
        if failed_srv_list:
            servers_info += f"⚠️ Не удалось: {', '.join(failed_srv_list)}\n"

        # Генерируем QR код для ссылки подписки (автообновление)
        try:
            qr_code = generate_qr_code(subscription_url)

            # Отправляем QR код
            await callback.message.answer_photo(
                BufferedInputFile(qr_code.read(), filename="qrcode.png"),
                caption=(
                    f"✅ Ключ успешно создан!\n\n"
                    f"🆔 ID клиента: {phone}\n"
                    f"⏰ Срок действия: {period_name}\n"
                    f"💰 Стоимость: {period_price} ₽\n"
                    f"{servers_info}"
                    f"🌐 Лимит IP: 2\n"
                    f"📊 Трафик: безлимит\n\n"
                    f"📱 Отсканируйте QR код подписки в приложении VPN"
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

        # Генерируем QR код для ссылки подписки
        try:
            qr_code = generate_qr_code(subscription_url)

            await callback.message.answer_photo(
                BufferedInputFile(qr_code.read(), filename="qrcode.png"),
                caption=(
                    f"🔄 Ключ успешно заменен!\n\n"
                    f"🆔 ID клиента: {phone}\n"
                    f"⏰ Срок действия: {period_name}\n"
                    f"🌐 Лимит IP: 2\n"
                    f"📊 Трафик: безлимит\n\n"
                    f"📱 Отсканируйте QR код подписки в приложении VPN"
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
    """Обработка VLESS ключа для исправления"""
    import urllib.parse
    from bot.api.remote_xui import load_servers_config

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

        # Сохраняем данные в state
        await state.update_data(
            uuid_part=uuid_part,
            original_fragment=original_fragment,
            vless_link=vless_link
        )

        # Загружаем конфиг серверов
        servers_config = load_servers_config()

        # Получаем список серверов с панелью
        servers = [s for s in servers_config.get('servers', [])
                  if s.get('enabled', True) and not s.get('local', False) and s.get('panel', {})]

        if not servers:
            await message.answer("❌ Нет доступных серверов с панелью")
            await state.clear()
            return

        if is_admin:
            # Для админа - показываем выбор сервера для ПЕРЕСОЗДАНИЯ
            await state.update_data(fix_servers=servers)
            await state.set_state(FixKeyStates.waiting_for_server_selection)

            await message.answer(
                f"🔧 <b>Исправление ключа (Админ)</b>\n\n"
                f"🔑 UUID: <code>{uuid_part[:8]}...</code>\n\n"
                f"Выберите сервер для пересоздания клиента:",
                reply_markup=Keyboards.server_selection(servers, prefix="fixserver_"),
                parse_mode="HTML"
            )
        else:
            # Для менеджера - ищем клиента на ВСЕХ серверах, используем параметры того где нашли
            await _execute_fix_key_search_all(message, state, servers, uuid_part, original_fragment, is_admin)

    except Exception as e:
        logger.error(f"Error parsing key: {e}")
        await message.answer(f"❌ Ошибка при обработке ключа: {str(e)[:100]}")
        await state.clear()
        await message.answer("Главное меню:", reply_markup=Keyboards.main_menu(is_admin))


@router.callback_query(FixKeyStates.waiting_for_server_selection, F.data.startswith("fixserver_"))
async def process_fix_server_selection(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора сервера админом для исправления ключа"""
    server_idx = int(callback.data.split("_", 1)[1])
    data = await state.get_data()
    servers = data.get('fix_servers', [])
    uuid_part = data.get('uuid_part', '')
    original_fragment = data.get('original_fragment', '')

    if server_idx >= len(servers):
        await callback.answer("Ошибка: сервер не найден", show_alert=True)
        return

    target_server = servers[server_idx]
    await callback.message.edit_text(f"🔧 Исправляю ключ на {target_server.get('name')}...")

    # Выполняем исправление
    await _execute_fix_key(callback.message, state, target_server, uuid_part, original_fragment, is_admin=True)
    await callback.answer()


async def _execute_fix_key_search_all(message: Message, state: FSMContext, servers: list, uuid_part: str, original_fragment: str, is_admin: bool):
    """Поиск клиента на всех серверах и исправление ключа (для менеджера)"""
    import urllib.parse
    from datetime import datetime, timedelta
    from bot.api.remote_xui import (
        find_client_on_server, find_client_on_local_server, create_client_via_panel
    )

    await message.answer("🔍 Ищу клиента на серверах...")

    client_info = None
    target_server = None
    found_on_server_name = None

    # Ищем клиента на ВСЕХ удалённых серверах
    for srv in servers:
        srv_name = srv.get('name', 'Unknown')
        client_info = await find_client_on_server(srv, uuid_part)

        if client_info:
            target_server = srv
            found_on_server_name = srv_name
            logger.info(f"Клиент найден на сервере {found_on_server_name}")
            await message.answer(f"✅ Найден на {found_on_server_name}")
            break

    if not client_info:
        # Не нашли на удалённых - ищем на локальном
        await message.answer("🔍 Не найден на удалённых серверах, ищу на локальном...")
        local_client = await find_client_on_local_server(uuid_part)

        if local_client:
            # Нашли на локальном - создаём на первом active_for_new сервере
            for srv in servers:
                if srv.get('active_for_new'):
                    target_server = srv
                    break

            if not target_server and servers:
                target_server = servers[0]

            if not target_server:
                await message.answer("❌ Нет доступного сервера")
                await state.clear()
                await message.answer("Главное меню:", reply_markup=Keyboards.main_menu(is_admin))
                return

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
                    expire_days = 30
            else:
                expire_days = 365

            await message.answer(f"📤 Создаю клиента на {target_server.get('name')}...")

            create_result = await create_client_via_panel(
                server_config=target_server,
                client_uuid=uuid_part,
                email=client_email,
                expire_days=expire_days,
                ip_limit=limit_ip
            )

            if create_result.get('success'):
                found_on_server_name = target_server.get('name')
                actual_uuid = create_result.get('uuid', uuid_part)
                await message.answer(f"✅ Клиент создан на {target_server.get('name')}!")

                client_info = await find_client_on_server(target_server, actual_uuid)
                if not client_info:
                    client_info = {
                        'email': client_email,
                        'inbound_name': 'main',
                        'inbound_remark': target_server.get('inbounds', {}).get('main', {}).get('name_prefix', 'VPN'),
                        'expiry_time': expiry_time,
                        'limit_ip': limit_ip
                    }
            else:
                await message.answer(f"⚠️ Не удалось создать: {create_result.get('error', 'Ошибка')}")
        else:
            # Не нашли нигде - используем первый active сервер
            for srv in servers:
                if srv.get('active_for_new'):
                    target_server = srv
                    break
            if not target_server and servers:
                target_server = servers[0]

    if not target_server:
        await message.answer("❌ Сервер не определён")
        await state.clear()
        await message.answer("Главное меню:", reply_markup=Keyboards.main_menu(is_admin))
        return

    # Формируем ссылку с параметрами найденного сервера
    await _generate_fixed_link(message, state, target_server, client_info, uuid_part, original_fragment, is_admin)


async def _execute_fix_key(message: Message, state: FSMContext, target_server: dict, uuid_part: str, original_fragment: str, is_admin: bool):
    """Выполнение исправления ключа на ВЫБРАННОМ сервере (для админа)"""
    import urllib.parse
    from datetime import datetime, timedelta
    from bot.api.remote_xui import (
        load_servers_config, find_client_on_server,
        find_client_on_local_server, create_client_via_panel
    )

    await message.answer(f"🔍 Ищу клиента на {target_server.get('name')}...")

    client_info = None
    found_on_server_name = None
    created_on_server = False

    # Сначала ищем на выбранном сервере
    client_info = await find_client_on_server(target_server, uuid_part)

    if client_info:
        found_on_server_name = target_server.get('name')
        logger.info(f"Клиент найден на сервере {found_on_server_name}")
        await message.answer(f"✅ Найден на {found_on_server_name}")
    else:
        # Не нашли на выбранном сервере - ищем на локальном
        await message.answer(f"🔍 Не найден на {target_server.get('name')}, ищу на локальном...")
        local_client = await find_client_on_local_server(uuid_part)

        if local_client:
            # Нашли на локальном - создаём на выбранном сервере
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

            await message.answer(f"📤 Создаю клиента {client_email} на {target_server.get('name')}...")

            # Создаём на выбранном сервере через API панели
            create_result = await create_client_via_panel(
                server_config=target_server,
                client_uuid=uuid_part,
                email=client_email,
                expire_days=expire_days,
                ip_limit=limit_ip
            )

            if create_result.get('success'):
                created_on_server = True
                found_on_server_name = target_server.get('name')
                actual_uuid = create_result.get('uuid', uuid_part)
                if create_result.get('existing'):
                    await message.answer(f"✅ Клиент уже есть на {target_server.get('name')}!")
                else:
                    await message.answer(f"✅ Клиент создан на {target_server.get('name')}!")

                # Ищем клиента заново для получения реальных параметров inbound
                client_info = await find_client_on_server(target_server, actual_uuid)
                if not client_info:
                    # Fallback если поиск не удался
                    client_info = {
                        'email': client_email,
                        'inbound_name': 'main',
                        'inbound_remark': target_server.get('inbounds', {}).get('main', {}).get('name_prefix', 'VPN'),
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

        # Используем РЕАЛЬНЫЕ параметры inbound с сервера
        real_inbound = client_info.get('inbound_settings', {})
        if real_inbound:
            inbound_config = real_inbound
        else:
            # Fallback на статический конфиг сервера
            inbound_config = target_server.get('inbounds', {}).get(client_inbound, {})
            if not inbound_config:
                inbound_config = target_server.get('inbounds', {}).get('main', {})

        # Формируем имя для ключа
        link_name = f"{inbound_remark} {client_email}"
        found_on_server = True
    else:
        # Не нашли - используем оригинальный fragment
        link_name = urllib.parse.unquote(original_fragment) if original_fragment else "Unknown"
        inbound_config = target_server.get('inbounds', {}).get('main', {})
        client_email = link_name
        inbound_remark = "Unknown"
        found_on_server = False

    # Формируем исправленный ключ с настройками сервера
    target_domain = target_server.get('domain', target_server.get('ip'))
    target_port = target_server.get('port', 443)

    security = inbound_config.get('security', 'reality')
    network = inbound_config.get('network', 'tcp')
    client_flow = client_info.get('flow', '') if client_info else ''

    # Также берём flow из конфига inbound если нет у клиента
    if not client_flow:
        client_flow = inbound_config.get('flow', '')

    params = [
        f"type={network}",
        "encryption=none"
    ]

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

    # Генерируем QR код для ссылки подписки (не для VLESS ключа)
    subscription_url = f"https://zov-gor.ru/sub/{uuid_part}"
    qr_code = generate_qr_code(subscription_url)

    # Формируем информацию об изменениях
    changes = []
    changes.append(f"• Хост: {target_domain}")
    changes.append(f"• SNI: {inbound_config.get('sni', 'N/A')}")
    if client_flow:
        changes.append(f"• Flow: {client_flow}")

    changes_text = "\n".join(changes)

    if created_on_server:
        status_text = f"✅ Создан на {target_server.get('name', 'Unknown')} (из локальной базы)"
    elif found_on_server:
        status_text = f"✅ Найден на {target_server.get('name', 'Unknown')}"
    else:
        status_text = f"⚠️ Не найден, использованы параметры {target_server.get('name', 'Unknown')}"

    # Форматируем дату окончания
    expiry_time = client_info.get('expiry_time', 0) if client_info else 0
    if expiry_time and expiry_time > 0:
        expiry_date = datetime.fromtimestamp(expiry_time / 1000)
        expiry_str = expiry_date.strftime('%d.%m.%Y %H:%M')
        if expiry_date < datetime.now():
            expiry_str += " ⚠️ (истёк)"
    else:
        expiry_str = "Безлимит"

    await message.answer_photo(
        BufferedInputFile(qr_code.read(), filename="qrcode.png"),
        caption=(
            f"✅ <b>Ключ исправлен!</b>\n\n"
            f"🖥 Сервер: {target_server.get('name', 'Unknown')}\n"
            f"📍 Inbound: {inbound_remark}\n"
            f"👤 Клиент: {client_email}\n"
            f"📅 Действует до: {expiry_str}\n"
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

    await state.clear()
    await message.answer(
        "Главное меню:",
        reply_markup=Keyboards.main_menu(is_admin)
    )


async def _generate_fixed_link(message: Message, state: FSMContext, target_server: dict, client_info: dict, uuid_part: str, original_fragment: str, is_admin: bool):
    """Генерация исправленной VLESS ссылки"""
    import urllib.parse
    from datetime import datetime

    if client_info:
        # Нашли клиента - берём данные
        client_email = client_info.get('email', '')
        client_inbound = client_info.get('inbound_name', 'main')
        inbound_remark = client_info.get('inbound_remark', client_inbound)

        # Используем РЕАЛЬНЫЕ параметры inbound с сервера
        real_inbound = client_info.get('inbound_settings', {})
        if real_inbound:
            inbound_config = real_inbound
        else:
            inbound_config = target_server.get('inbounds', {}).get(client_inbound, {})
            if not inbound_config:
                inbound_config = target_server.get('inbounds', {}).get('main', {})

        link_name = f"{inbound_remark} {client_email}"
        found_on_server = True
    else:
        link_name = urllib.parse.unquote(original_fragment) if original_fragment else "Unknown"
        inbound_config = target_server.get('inbounds', {}).get('main', {})
        client_email = link_name
        inbound_remark = "Unknown"
        found_on_server = False

    # Формируем исправленный ключ
    target_domain = target_server.get('domain', target_server.get('ip'))
    target_port = target_server.get('port', 443)

    security = inbound_config.get('security', 'reality')
    network = inbound_config.get('network', 'tcp')
    client_flow = client_info.get('flow', '') if client_info else ''

    if not client_flow:
        client_flow = inbound_config.get('flow', '')

    params = [f"type={network}", "encryption=none"]

    if network == 'grpc':
        params.append(f"serviceName={inbound_config.get('serviceName', '')}")
        params.append(f"authority={inbound_config.get('authority', '')}")

    params.append(f"security={security}")

    if security == 'reality':
        if inbound_config.get('pbk'):
            params.append(f"pbk={inbound_config['pbk']}")
        params.append(f"fp={inbound_config.get('fp') or 'chrome'}")
        if inbound_config.get('sni'):
            params.append(f"sni={inbound_config['sni']}")
        if inbound_config.get('sid'):
            params.append(f"sid={inbound_config['sid']}")
        if client_flow:
            params.append(f"flow={client_flow}")
        params.append("spx=%2F")

    new_query = '&'.join(params)
    fixed_link = f"vless://{uuid_part}@{target_domain}:{target_port}?{new_query}#{link_name}"

    # Генерируем QR код для ссылки подписки (не для VLESS ключа)
    subscription_url = f"https://zov-gor.ru/sub/{uuid_part}"
    qr_code = generate_qr_code(subscription_url)

    # Статус
    if found_on_server:
        status_text = f"✅ Найден на {target_server.get('name', 'Unknown')}"
    else:
        status_text = f"⚠️ Не найден, использованы параметры {target_server.get('name', 'Unknown')}"

    # Дата окончания
    expiry_time = client_info.get('expiry_time', 0) if client_info else 0
    if expiry_time and expiry_time > 0:
        expiry_date = datetime.fromtimestamp(expiry_time / 1000)
        expiry_str = expiry_date.strftime('%d.%m.%Y %H:%M')
        if expiry_date < datetime.now():
            expiry_str += " ⚠️ (истёк)"
    else:
        expiry_str = "Безлимит"

    await message.answer_photo(
        BufferedInputFile(qr_code.read(), filename="qrcode.png"),
        caption=(
            f"✅ <b>Ключ исправлен!</b>\n\n"
            f"🖥 Сервер: {target_server.get('name', 'Unknown')}\n"
            f"📍 Inbound: {inbound_remark}\n"
            f"👤 Клиент: {client_email}\n"
            f"📅 Действует до: {expiry_str}\n"
            f"🔍 Статус: {status_text}\n"
            f"🌐 Хост: {target_domain}:{target_port}\n"
            f"🔒 SNI: {inbound_config.get('sni', 'N/A')}\n"
            f"📡 Flow: {client_flow or 'пусто'}"
        ),
        parse_mode="HTML"
    )

    await message.answer(
        f"📋 <b>Исправленный VLESS ключ:</b>\n\n"
        f"<code>{fixed_link}</code>\n\n"
        f"💡 Скопируйте и отправьте клиенту.",
        parse_mode="HTML"
    )

    await state.clear()
    await message.answer("Главное меню:", reply_markup=Keyboards.main_menu(is_admin))


# ==================== ПОИСК КЛИЕНТА ====================

@router.message(F.text == "🔍 Найти клиента")
async def start_search_client(message: Message, state: FSMContext, db: DatabaseManager):
    """Начало поиска клиента менеджером"""
    user_id = message.from_user.id
    if not await is_authorized(user_id, db):
        await message.answer("У вас нет доступа к этой функции.")
        return

    await state.set_state(SearchClientStates.waiting_for_query)
    await message.answer(
        "🔍 <b>ПОИСК КЛИЕНТА</b>\n\n"
        "Введите номер телефона или имя клиента для поиска.\n\n"
        "Примеры:\n"
        "• <code>+79001234567</code>\n"
        "• <code>client_name</code>\n\n"
        "Или нажмите 'Отмена' для возврата.",
        parse_mode="HTML",
        reply_markup=Keyboards.cancel()
    )


@router.message(SearchClientStates.waiting_for_query, F.text == "Отмена")
async def cancel_search_client(message: Message, state: FSMContext):
    """Отмена поиска"""
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID
    await state.clear()
    await message.answer(
        "Поиск отменен.",
        reply_markup=Keyboards.main_menu(is_admin)
    )


@router.message(SearchClientStates.waiting_for_query)
async def process_search_client(message: Message, state: FSMContext, db: DatabaseManager):
    """Обработка поиска клиента менеджером"""
    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID
    query = message.text.strip()

    # Если нажата кнопка меню — выходим
    menu_buttons = {
        "Создать ключ", "🔄 Замена ключа", "🔧 Исправить ключ",
        "💰 Прайс", "Моя статистика", "🔍 Найти клиента",
        "📡 Управление подпиской",
        "Панель администратора", "Назад",
    }
    if query in menu_buttons:
        await state.clear()
        await message.answer("🔍 Поиск отменен.", reply_markup=Keyboards.main_menu(is_admin))
        return

    if len(query) < 2:
        await message.answer("❌ Введите минимум 2 символа для поиска.")
        return

    status_msg = await message.answer("🔍 Поиск...")

    # Для админа ищем по всем менеджерам, для менеджера — только по своим
    if is_admin:
        keys = await db.search_keys(query)
    else:
        keys = await db.search_keys_by_manager(user_id, query)

    if not keys:
        await status_msg.edit_text(
            f"🔍 По запросу «<b>{query}</b>» ничего не найдено.\n\n"
            "Попробуйте другой запрос или нажмите 'Отмена' для выхода.",
            parse_mode="HTML"
        )
        return

    await state.clear()

    # Если один результат — сразу показываем подписку с QR
    if len(keys) == 1:
        key = keys[0]
        client_uuid = key.get('client_id', '')
        phone = key.get('phone_number', key.get('client_email', 'N/A'))
        period = key.get('period', 'N/A')
        price = key.get('price', 0) or 0
        server_name = key.get('server_name', '')
        created_at = key['created_at'][:16].replace('T', ' ')

        if client_uuid:
            subscription_url = f"https://zov-gor.ru/sub/{client_uuid}"
            try:
                qr_code = generate_qr_code(subscription_url)
                await status_msg.delete()
                await message.answer_photo(
                    BufferedInputFile(qr_code.read(), filename="qrcode.png"),
                    caption=(
                        f"🔍 <b>Найден клиент:</b>\n\n"
                        f"📱 Клиент: <b>{phone}</b>\n"
                        f"📅 Срок: {period}\n"
                        f"💰 Цена: {price} ₽\n"
                        f"🖥 Сервер: {server_name or 'N/A'}\n"
                        f"📆 Создан: {created_at}\n\n"
                        f"📱 QR код подписки для сканирования в VPN приложении"
                    ),
                    parse_mode="HTML"
                )
                await message.answer(
                    f"🔄 Ссылка подписки:\n<code>{subscription_url}</code>\n\n"
                    f"💡 Скопируйте и отправьте клиенту.",
                    parse_mode="HTML",
                    reply_markup=Keyboards.main_menu(is_admin)
                )
                return
            except Exception as e:
                logger.error(f"QR generation error in search: {e}")

        # Если нет UUID или ошибка QR
        await status_msg.edit_text(
            f"🔍 <b>Найден клиент:</b>\n\n"
            f"📱 Клиент: <b>{phone}</b>\n"
            f"📅 Срок: {period}\n"
            f"💰 Цена: {price} ₽\n"
            f"🖥 Сервер: {server_name or 'N/A'}\n"
            f"📆 Создан: {created_at}\n"
            f"🔑 UUID: <code>{client_uuid or 'N/A'}</code>",
            parse_mode="HTML"
        )
        await message.answer("Главное меню:", reply_markup=Keyboards.main_menu(is_admin))
        return

    # Несколько результатов — показываем список с кнопками
    text = f"🔍 <b>РЕЗУЛЬТАТЫ ПОИСКА</b>\n"
    text += f"Запрос: «{query}» — найдено: {len(keys)}\n\n"

    buttons = []
    for idx, key in enumerate(keys[:10], 1):
        phone = key.get('phone_number', key.get('client_email', 'N/A'))
        period = key.get('period', 'N/A')
        price = key.get('price', 0) or 0
        server_name = key.get('server_name', '')
        created_at = key['created_at'][:10]
        client_uuid = key.get('client_id', '')

        text += f"{idx}. <b>{phone}</b>\n"
        text += f"   📅 {period} | 💰 {price} ₽ | 🖥 {server_name or 'N/A'}\n"
        text += f"   📆 {created_at}\n\n"

        if client_uuid:
            buttons.append([
                InlineKeyboardButton(
                    text=f"📱 {phone[:25]}",
                    callback_data=f"mgr_sub_{client_uuid[:36]}"
                )
            ])

        if len(text) > 3000:
            text += "<i>... показаны первые результаты</i>\n"
            break

    if buttons:
        text += "👆 Нажмите на клиента чтобы получить подписку и QR код"

    from aiogram.types import InlineKeyboardMarkup
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    await status_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await message.answer("Главное меню:", reply_markup=Keyboards.main_menu(is_admin))


@router.callback_query(F.data.startswith("mgr_sub_"))
async def show_client_subscription(callback: CallbackQuery, db: DatabaseManager):
    """Показать подписку и QR код клиента"""
    user_id = callback.from_user.id
    if not await is_authorized(user_id, db):
        await callback.answer("Нет доступа", show_alert=True)
        return

    client_uuid = callback.data.replace("mgr_sub_", "")
    subscription_url = f"https://zov-gor.ru/sub/{client_uuid}"

    try:
        qr_code = generate_qr_code(subscription_url)
        await callback.message.answer_photo(
            BufferedInputFile(qr_code.read(), filename="qrcode.png"),
            caption=(
                f"📱 QR код подписки\n\n"
                f"🔄 Подписка:\n<code>{subscription_url}</code>\n\n"
                f"💡 Отсканируйте QR или скопируйте ссылку"
            ),
            parse_mode="HTML"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"QR generation error: {e}")
        await callback.message.answer(
            f"🔄 Ссылка подписки:\n<code>{subscription_url}</code>",
            parse_mode="HTML"
        )
        await callback.answer()


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


# ============ УПРАВЛЕНИЕ ПОДПИСКОЙ (добавление/перенос сервера) ============

@router.message(F.text == "📡 Управление подпиской")
async def mgr_start_manage_sub(message: Message, state: FSMContext, db: DatabaseManager):
    """Менеджер: начало управления подпиской клиента"""
    user_id = message.from_user.id
    if not await is_authorized(user_id, db):
        await message.answer("У вас нет доступа к этой функции.")
        return

    await state.clear()
    await state.set_state(MgrAddServerStates.waiting_for_search)
    await message.answer(
        "📡 <b>УПРАВЛЕНИЕ ПОДПИСКОЙ</b>\n\n"
        "Здесь вы можете добавить сервер к подписке клиента "
        "или перенести его на другой сервер.\n\n"
        "Введите номер телефона, имя или UUID клиента:\n\n"
        "Примеры:\n"
        "• <code>79001234567</code>\n"
        "• <code>Иван</code>",
        parse_mode="HTML",
        reply_markup=Keyboards.cancel()
    )


@router.message(MgrAddServerStates.waiting_for_search, F.text == "Отмена")
async def mgr_cancel_manage_sub(message: Message, state: FSMContext):
    """Отмена управления подпиской"""
    is_admin = message.from_user.id == ADMIN_ID
    await state.clear()
    await message.answer("Операция отменена.", reply_markup=Keyboards.main_menu(is_admin))


@router.message(MgrAddServerStates.waiting_for_search)
async def mgr_process_sub_search(message: Message, state: FSMContext, db: DatabaseManager):
    """Поиск клиента для управления подпиской"""
    from bot.handlers.admin import search_clients_on_servers
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    user_id = message.from_user.id
    is_admin = user_id == ADMIN_ID
    query = message.text.strip()

    menu_buttons = {
        "Создать ключ", "🔄 Замена ключа", "🔧 Исправить ключ",
        "💰 Прайс", "Моя статистика", "🔍 Найти клиента",
        "📡 Управление подпиской",
        "Панель администратора", "Назад",
    }
    if query in menu_buttons:
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=Keyboards.main_menu(is_admin))
        return

    if len(query) < 2:
        await message.answer("❌ Введите минимум 2 символа для поиска.")
        return

    status_msg = await message.answer("🔍 Поиск клиента на серверах...")

    # Ищем на всех серверах (как у админа)
    xui_clients = await search_clients_on_servers(query)

    # Дополнительно ищем по keys_history менеджера
    if not xui_clients:
        if is_admin:
            keys = await db.search_keys(query)
        else:
            keys = await db.search_keys_by_manager(user_id, query)

        if keys:
            # Ищем UUID найденных ключей на серверах
            for key in keys[:5]:
                uuid = key.get('client_id', '')
                if uuid:
                    from bot.api.remote_xui import find_client_presence_on_all_servers
                    presence = await find_client_presence_on_all_servers(uuid)
                    for srv in presence.get('found_on', []):
                        xui_clients.append({
                            'email': key.get('client_email', key.get('phone_number', '')),
                            'uuid': uuid,
                            'server': srv.get('server_name', ''),
                            'expiry_time': srv.get('expiry_time', 0),
                            'limit_ip': srv.get('ip_limit', 2)
                        })

    if not xui_clients:
        await status_msg.edit_text(
            f"🔍 По запросу «<b>{query}</b>» ничего не найдено.\n\n"
            "Попробуйте другой запрос.",
            parse_mode="HTML"
        )
        return

    # Группируем по UUID
    clients_by_uuid = {}
    for client in xui_clients:
        uuid = client.get('uuid', '')
        if not uuid:
            continue
        if uuid not in clients_by_uuid:
            clients_by_uuid[uuid] = {
                'email': client.get('email', ''),
                'uuid': uuid,
                'servers': [],
                'expiry_time': client.get('expiry_time', 0),
                'ip_limit': client.get('limit_ip', 2)
            }
        srv_name = client.get('server', 'Unknown')
        if srv_name not in clients_by_uuid[uuid]['servers']:
            clients_by_uuid[uuid]['servers'].append(srv_name)

    unique_clients = list(clients_by_uuid.values())

    if not unique_clients:
        await status_msg.edit_text(
            f"🔍 По запросу «<b>{query}</b>» ничего не найдено.",
            parse_mode="HTML"
        )
        return

    await state.update_data(mgr_search_results=unique_clients, mgr_user_id=user_id)

    text = f"🔍 <b>Найдено клиентов:</b> {len(unique_clients)}\n\n"
    buttons = []

    for idx, client in enumerate(unique_clients[:10]):
        email = client['email']
        uuid_short = client['uuid'][:8] + '...'
        servers_str = ', '.join(client['servers'])
        expiry_time = client.get('expiry_time', 0)

        if expiry_time > 0:
            expiry_dt = datetime.fromtimestamp(expiry_time / 1000)
            expiry_str = expiry_dt.strftime("%d.%m.%Y")
        else:
            expiry_str = "Безлимит"

        sub_url = f"https://zov-gor.ru/sub/{client['uuid']}"

        text += f"{idx + 1}. <b>{email}</b>\n"
        text += f"   🖥 Серверы: {servers_str}\n"
        text += f"   ⏰ До: {expiry_str}\n"
        text += f"   📱 <code>{sub_url}</code>\n\n"

        buttons.append([InlineKeyboardButton(
            text=f"📡 {email[:30]}",
            callback_data=f"mgrsub_sel_{idx}"
        )])

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="mgrsub_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await status_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("mgrsub_sel_"))
async def mgr_select_client_for_sub(callback: CallbackQuery, state: FSMContext, db: DatabaseManager):
    """Менеджер: выбор клиента — показ серверов и действий"""
    from bot.api.remote_xui import find_client_presence_on_all_servers, load_servers_config
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    user_id = callback.from_user.id
    if not await is_authorized(user_id, db):
        await callback.answer("Нет доступа", show_alert=True)
        return

    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    search_results = data.get('mgr_search_results', [])

    if idx >= len(search_results):
        await callback.answer("Ошибка: клиент не найден")
        return

    client = search_results[idx]
    client_uuid = client['uuid']
    email = client['email']

    await callback.message.edit_text("🔍 Проверяю серверы...")

    presence = await find_client_presence_on_all_servers(client_uuid)
    found_on = presence.get('found_on', [])
    not_found_on = presence.get('not_found_on', [])

    # Фильтруем not_found_on по разрешённым серверам менеджера
    servers_config = load_servers_config()
    all_servers = servers_config.get('servers', [])
    allowed = await _get_allowed_servers(user_id, db, all_servers)
    allowed_names = {s.get('name') for s in allowed}

    not_found_filtered = [
        srv for srv in not_found_on
        if srv['server_name'] in allowed_names
    ]

    # Берём expiry и ip_limit
    expiry_time_ms = 0
    ip_limit = 2
    if found_on:
        expiry_time_ms = found_on[0].get('expiry_time', 0)
        ip_limit = found_on[0].get('ip_limit', 2)

    available_servers = []
    for srv in not_found_filtered:
        available_servers.append({
            'server_name': srv['server_name'],
            'name_prefix': srv.get('name_prefix', srv['server_name']),
            'server_config': srv['server_config']
        })

    await state.update_data(
        mgr_client_uuid=client_uuid,
        mgr_client_email=email,
        mgr_expiry_time_ms=expiry_time_ms,
        mgr_ip_limit=ip_limit,
        mgr_available_servers=available_servers,
        mgr_selected_indices=[],
    )

    # Формируем текст
    sub_url = f"https://zov-gor.ru/sub/{client_uuid}"
    text = f"📡 <b>Клиент:</b> <code>{email}</code>\n"
    text += f"📱 Подписка: <code>{sub_url}</code>\n\n"

    if found_on:
        text += "<b>✅ Уже на серверах:</b>\n"
        for srv in found_on:
            exp = srv.get('expiry_time', 0)
            if exp > 0:
                exp_str = datetime.fromtimestamp(exp / 1000).strftime("%d.%m.%Y")
            else:
                exp_str = "Безлимит"
            prefix = srv.get('name_prefix', '')
            label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
            text += f"  ✅ {label} — до {exp_str}\n"
        text += "\n"

    buttons = []

    # QR код подписки
    buttons.append([InlineKeyboardButton(
        text="📱 QR код подписки",
        callback_data=f"mgrsub_qr_{client_uuid[:36]}"
    )])

    if not not_found_filtered:
        text += "🎉 <b>Клиент уже на всех доступных серверах!</b>"
        buttons.append([InlineKeyboardButton(text="🔍 Новый поиск", callback_data="mgrsub_newsearch")])
        buttons.append([InlineKeyboardButton(text="◀️ В меню", callback_data="mgrsub_cancel")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
        return

    await state.set_state(MgrAddServerStates.waiting_for_server_select)

    text += "<b>➕ Добавить на серверы:</b>\n"
    for srv in not_found_filtered:
        prefix = srv.get('name_prefix', '')
        label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        text += f"  ➕ {label}\n"
    text += "\nВыберите серверы для добавления:"

    for idx_s, srv in enumerate(not_found_filtered):
        prefix = srv.get('name_prefix', '')
        btn_label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        buttons.append([InlineKeyboardButton(
            text=f"➕ {btn_label}",
            callback_data=f"mgrsub_srv_{idx_s}"
        )])

    if len(not_found_filtered) > 1:
        buttons.append([InlineKeyboardButton(
            text="📡 Добавить на ВСЕ",
            callback_data="mgrsub_all"
        )])

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="mgrsub_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("mgrsub_qr_"))
async def mgr_show_sub_qr(callback: CallbackQuery, db: DatabaseManager):
    """Показать QR код подписки"""
    user_id = callback.from_user.id
    if not await is_authorized(user_id, db):
        await callback.answer("Нет доступа", show_alert=True)
        return

    client_uuid = callback.data[len("mgrsub_qr_"):]
    subscription_url = f"https://zov-gor.ru/sub/{client_uuid}"

    try:
        qr_code = generate_qr_code(subscription_url)
        await callback.message.answer_photo(
            BufferedInputFile(qr_code.read(), filename="qrcode.png"),
            caption=(
                f"📱 <b>QR код подписки</b>\n\n"
                f"🔄 Ссылка:\n<code>{subscription_url}</code>\n\n"
                f"💡 Отсканируйте QR или скопируйте ссылку и отправьте клиенту"
            ),
            parse_mode="HTML"
        )
        await callback.answer()
    except Exception as e:
        logger.error(f"QR generation error: {e}")
        await callback.message.answer(
            f"🔄 Ссылка подписки:\n<code>{subscription_url}</code>",
            parse_mode="HTML"
        )
        await callback.answer()


@router.callback_query(MgrAddServerStates.waiting_for_server_select, F.data.startswith("mgrsub_srv_"))
async def mgr_toggle_server(callback: CallbackQuery, state: FSMContext):
    """Менеджер: переключение выбора сервера"""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    selected = data.get('mgr_selected_indices', [])
    available = data.get('mgr_available_servers', [])
    email = data.get('mgr_client_email', '')
    client_uuid = data.get('mgr_client_uuid', '')
    expiry_time_ms = data.get('mgr_expiry_time_ms', 0)

    if idx >= len(available):
        await callback.answer("Ошибка")
        return

    if idx in selected:
        selected.remove(idx)
    else:
        selected.append(idx)

    await state.update_data(mgr_selected_indices=selected)

    buttons = []
    for i, srv in enumerate(available):
        mark = "✅" if i in selected else "➕"
        prefix = srv.get('name_prefix', '')
        btn_label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {btn_label}",
            callback_data=f"mgrsub_srv_{i}"
        )])

    if len(available) > 1:
        buttons.append([InlineKeyboardButton(text="📡 Добавить на ВСЕ", callback_data="mgrsub_all")])

    if selected:
        buttons.append([InlineKeyboardButton(
            text=f"✅ Подтвердить ({len(selected)})",
            callback_data="mgrsub_go"
        )])

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="mgrsub_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = f"📡 <b>Клиент:</b> <code>{email}</code>\n\n"
    text += "<b>Серверы:</b>\n"
    for i, srv in enumerate(available):
        mark = "✅" if i in selected else "➕"
        prefix = srv.get('name_prefix', '')
        label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        text += f"  {mark} {label}\n"

    if expiry_time_ms > 0:
        exp_str = datetime.fromtimestamp(expiry_time_ms / 1000).strftime("%d.%m.%Y")
        text += f"\n⏰ Срок: до {exp_str}"

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(MgrAddServerStates.waiting_for_server_select, F.data == "mgrsub_all")
async def mgr_select_all_servers(callback: CallbackQuery, state: FSMContext):
    """Менеджер: выбрать все серверы"""
    data = await state.get_data()
    available = data.get('mgr_available_servers', [])
    selected = list(range(len(available)))
    await state.update_data(mgr_selected_indices=selected)
    await state.set_state(MgrAddServerStates.confirming)

    await _mgr_show_confirm(callback, state)


@router.callback_query(MgrAddServerStates.waiting_for_server_select, F.data == "mgrsub_go")
async def mgr_go_confirm(callback: CallbackQuery, state: FSMContext):
    """Менеджер: перейти к подтверждению"""
    data = await state.get_data()
    selected = data.get('mgr_selected_indices', [])
    if not selected:
        await callback.answer("Выберите хотя бы один сервер")
        return

    await state.set_state(MgrAddServerStates.confirming)
    await _mgr_show_confirm(callback, state)


async def _mgr_show_confirm(callback: CallbackQuery, state: FSMContext):
    """Показать экран подтверждения"""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    data = await state.get_data()
    selected = data.get('mgr_selected_indices', [])
    available = data.get('mgr_available_servers', [])
    email = data.get('mgr_client_email', '')
    client_uuid = data.get('mgr_client_uuid', '')
    expiry_time_ms = data.get('mgr_expiry_time_ms', 0)

    selected_servers = [available[i] for i in selected if i < len(available)]

    text = f"📡 <b>Подтверждение</b>\n\n"
    text += f"Клиент: <code>{email}</code>\n\n"
    text += "<b>Добавить на серверы:</b>\n"
    for srv in selected_servers:
        prefix = srv.get('name_prefix', '')
        label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        text += f"  • {label}\n"

    if expiry_time_ms > 0:
        exp_str = datetime.fromtimestamp(expiry_time_ms / 1000).strftime("%d.%m.%Y")
        text += f"\n⏰ Срок: до {exp_str}"

        now_ms = int(datetime.now().timestamp() * 1000)
        if expiry_time_ms < now_ms:
            text += "\n⚠️ <i>Внимание: ключ просрочен!</i>"

    buttons = [
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="mgrsub_confirm")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="mgrsub_back")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="mgrsub_cancel")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(MgrAddServerStates.confirming, F.data == "mgrsub_back")
async def mgr_back_to_select(callback: CallbackQuery, state: FSMContext):
    """Назад к выбору серверов"""
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    await state.set_state(MgrAddServerStates.waiting_for_server_select)

    data = await state.get_data()
    selected = data.get('mgr_selected_indices', [])
    available = data.get('mgr_available_servers', [])
    email = data.get('mgr_client_email', '')
    client_uuid = data.get('mgr_client_uuid', '')
    expiry_time_ms = data.get('mgr_expiry_time_ms', 0)

    buttons = []
    for i, srv in enumerate(available):
        mark = "✅" if i in selected else "➕"
        prefix = srv.get('name_prefix', '')
        btn_label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {btn_label}",
            callback_data=f"mgrsub_srv_{i}"
        )])

    if len(available) > 1:
        buttons.append([InlineKeyboardButton(text="📡 Добавить на ВСЕ", callback_data="mgrsub_all")])

    if selected:
        buttons.append([InlineKeyboardButton(
            text=f"✅ Подтвердить ({len(selected)})",
            callback_data="mgrsub_go"
        )])

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="mgrsub_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = f"📡 <b>Клиент:</b> <code>{email}</code>\n\n"
    text += "<b>Серверы:</b>\n"
    for i, srv in enumerate(available):
        mark = "✅" if i in selected else "➕"
        prefix = srv.get('name_prefix', '')
        label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        text += f"  {mark} {label}\n"

    if expiry_time_ms > 0:
        exp_str = datetime.fromtimestamp(expiry_time_ms / 1000).strftime("%d.%m.%Y")
        text += f"\n⏰ Срок: до {exp_str}"

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(MgrAddServerStates.confirming, F.data == "mgrsub_confirm")
async def mgr_confirm_add_servers(callback: CallbackQuery, state: FSMContext):
    """Менеджер: подтверждение — создаём клиента на выбранных серверах"""
    from bot.api.remote_xui import create_client_via_panel
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    data = await state.get_data()
    client_uuid = data.get('mgr_client_uuid', '')
    email = data.get('mgr_client_email', '')
    expiry_time_ms = data.get('mgr_expiry_time_ms', 0)
    ip_limit = data.get('mgr_ip_limit', 2)
    selected = data.get('mgr_selected_indices', [])
    available = data.get('mgr_available_servers', [])

    selected_servers = [available[i] for i in selected if i < len(available)]

    if not selected_servers:
        await callback.answer("Нет выбранных серверов")
        return

    # Проверяем серверы с лимитом трафика
    traffic_servers = [
        srv for srv in selected_servers
        if srv['server_config'].get('traffic_limit_gb', 0) > 0
    ]
    admin_total_gb = data.get('mgr_total_gb')
    if traffic_servers and admin_total_gb is None:
        traffic_limit = traffic_servers[0]['server_config']['traffic_limit_gb']
        await state.set_state(MgrAddServerStates.waiting_for_traffic_choice)
        await callback.message.edit_text(
            f"📊 <b>Выбор трафика</b>\n\n"
            f"Некоторые серверы имеют ограничение трафика.\n"
            f"Выберите лимит:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"📊 {traffic_limit} ГБ (рекомендуется)", callback_data=f"mgrsub_traffic_{traffic_limit}")],
                [InlineKeyboardButton(text="♾ Без ограничений", callback_data="mgrsub_traffic_0")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="mgrsub_cancel")]
            ]),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    await _mgr_execute_add(callback, state, data, selected_servers)


@router.callback_query(MgrAddServerStates.waiting_for_traffic_choice, F.data.startswith("mgrsub_traffic_"))
async def mgr_traffic_choice(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора трафика"""
    total_gb = int(callback.data.split("_")[-1])
    await state.update_data(mgr_total_gb=total_gb)

    data = await state.get_data()
    selected = data.get('mgr_selected_indices', [])
    available = data.get('mgr_available_servers', [])
    selected_servers = [available[i] for i in selected if i < len(available)]

    await _mgr_execute_add(callback, state, data, selected_servers)


async def _mgr_execute_add(callback: CallbackQuery, state: FSMContext, data: dict, selected_servers: list):
    """Выполнить добавление клиента на серверы"""
    from bot.api.remote_xui import create_client_via_panel
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

    client_uuid = data.get('mgr_client_uuid', '')
    email = data.get('mgr_client_email', '')
    expiry_time_ms = data.get('mgr_expiry_time_ms', 0)
    ip_limit = data.get('mgr_ip_limit', 2)
    total_gb = data.get('mgr_total_gb', 0) or 0

    await callback.message.edit_text("⏳ Добавление клиента на серверы...")

    results = []
    for srv in selected_servers:
        server_config = srv['server_config']
        server_name = srv['server_name']

        server_traffic_limit = server_config.get('traffic_limit_gb', 0)
        srv_total_gb = total_gb if server_traffic_limit > 0 else 0

        try:
            result = await create_client_via_panel(
                server_config=server_config,
                client_uuid=client_uuid,
                email=email,
                expire_days=30,
                ip_limit=ip_limit,
                expire_time_ms=expiry_time_ms,
                total_gb=srv_total_gb
            )
            success = result.get('success', False)
            existing = result.get('existing', False)
            results.append({'server': server_name, 'success': success, 'existing': existing})
        except Exception as e:
            logger.error(f"Ошибка добавления на {server_name}: {e}")
            results.append({'server': server_name, 'success': False})

    await state.clear()

    sub_url = f"https://zov-gor.ru/sub/{client_uuid}"
    text = "📡 <b>Результат:</b>\n\n"
    for r in results:
        if r.get('success'):
            if r.get('existing'):
                text += f"✅ {r['server']} — клиент уже существовал\n"
            else:
                text += f"✅ {r['server']} — клиент добавлен\n"
        else:
            text += f"❌ {r['server']} — ошибка\n"

    text += f"\n📱 Подписка: <code>{sub_url}</code>\n"
    text += "Подписка обновлена автоматически."

    buttons = [
        [InlineKeyboardButton(text="📱 QR код", callback_data=f"mgrsub_qr_{client_uuid[:36]}")],
        [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="mgrsub_newsearch")],
        [InlineKeyboardButton(text="◀️ В меню", callback_data="mgrsub_cancel")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "mgrsub_newsearch")
async def mgr_new_search(callback: CallbackQuery, state: FSMContext):
    """Новый поиск"""
    await state.clear()
    await state.set_state(MgrAddServerStates.waiting_for_search)
    await callback.message.edit_text(
        "📡 <b>УПРАВЛЕНИЕ ПОДПИСКОЙ</b>\n\n"
        "Введите номер телефона, имя или UUID клиента для поиска.",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "mgrsub_cancel")
async def mgr_cancel_sub_callback(callback: CallbackQuery, state: FSMContext):
    """Отмена (inline)"""
    is_admin = callback.from_user.id == ADMIN_ID
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(
        "Главное меню:",
        reply_markup=Keyboards.main_menu(is_admin)
    )
    await callback.answer()
