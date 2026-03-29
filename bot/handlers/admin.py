"""
Обработчики для администратора
"""
import logging
import asyncio
import aiosqlite
from functools import wraps
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.config import ADMIN_ID, INBOUND_ID, DOMAIN, DATABASE_PATH
from bot.database import DatabaseManager
from bot.api.xui_client import XUIClient
from bot.utils import Keyboards, generate_user_id, generate_qr_code, notify_admin_xui_error
from bot.price_config import PriceManager, get_subscription_periods

logger = logging.getLogger(__name__)

router = Router()


def get_manager_display_name(manager: dict) -> str:
    """
    Получить отображаемое имя менеджера с приоритетом:
    1. custom_name (установленное админом)
    2. full_name (из Telegram)
    3. username (из Telegram)
    4. ID пользователя
    """
    custom_name = manager.get('custom_name', '') or ''
    full_name = manager.get('full_name', '') or ''
    username = manager.get('username', '') or ''

    if custom_name:
        return custom_name
    elif full_name:
        return full_name
    elif username:
        return f"@{username}"
    else:
        return f"ID: {manager['user_id']}"


class AddManagerStates(StatesGroup):
    """Состояния для добавления менеджера"""
    waiting_for_user_id = State()
    waiting_for_server_permissions = State()


class EditManagerServersStates(StatesGroup):
    """Состояния для редактирования серверов менеджера"""
    waiting_for_server_selection = State()


class EditPriceStates(StatesGroup):
    """Состояния для редактирования цен"""
    waiting_for_period = State()
    waiting_for_new_price = State()


class EditManagerNameStates(StatesGroup):
    """Состояния для редактирования имени менеджера"""
    waiting_for_manager_id = State()
    waiting_for_new_name = State()


class SendNotificationStates(StatesGroup):
    """Состояния для отправки уведомлений"""
    waiting_for_message = State()


class ManageSNIStates(StatesGroup):
    """Состояния для управления настройками сервера (SNI, Target, Transport)"""
    waiting_for_sni_domains = State()
    waiting_for_dest = State()
    waiting_for_action = State()


class EditServerStates(StatesGroup):
    """Состояния для редактирования сервера"""
    waiting_for_field_value = State()


class ServerPaymentStates(StatesGroup):
    """Состояния для управления оплатой серверов"""
    waiting_for_date = State()
    waiting_for_cost = State()


class SearchKeyStates(StatesGroup):
    """Состояния для поиска ключей"""
    waiting_for_search_query = State()


class WebOrderRejectStates(StatesGroup):
    """Состояния для отказа веб-заказа"""
    waiting_for_reject_reason = State()


class AdminCreateKeyStates(StatesGroup):
    """Состояния для создания ключа с выбором inbound (только для админа)"""
    waiting_for_phone = State()
    waiting_for_server = State()  # Выбор сервера
    waiting_for_inbound = State()
    waiting_for_period = State()
    waiting_for_traffic = State()  # Выбор трафика (если сервер имеет лимит)
    confirming = State()


class ExtendSubscriptionStates(StatesGroup):
    """Состояния для продления подписки"""
    waiting_for_search = State()


class AddToSubscriptionStates(StatesGroup):
    """Состояния для добавления сервера в подписку"""
    waiting_for_search = State()
    waiting_for_server_select = State()
    waiting_for_traffic_choice = State()  # Выбор трафика для серверов с лимитом
    confirming = State()


class BulkAddServerStates(StatesGroup):
    """Состояния для массового добавления сервера ко всем активным подпискам"""
    waiting_for_target = State()  # Выбор целевого сервера
    waiting_for_traffic = State()
    confirming = State()
    processing = State()


class ManageSubscriptionStates(StatesGroup):
    """Состояния для управления подпиской (исключение серверов)"""
    waiting_for_search = State()
    waiting_for_action = State()  # Выбор действия (исключить сервер и т.д.)
    confirming_exclude = State()  # Подтверждение исключения


def admin_only(func):
    """Декоратор для проверки прав администратора"""
    @wraps(func)
    async def wrapper(message: Message, *args, **kwargs):
        if message.from_user.id != ADMIN_ID:
            await message.answer("У вас нет доступа к этой функции.")
            return
        return await func(message, *args, **kwargs)
    return wrapper


@router.message(F.text == "Панель администратора")
@admin_only
async def show_admin_panel(message: Message, **kwargs):
    """Показать панель администратора"""
    await message.answer(
        "Панель администратора:\n\n"
        "Управление менеджерами и просмотр статистики.",
        reply_markup=Keyboards.admin_menu()
    )


# ============ СОЗДАНИЕ КЛЮЧА С ВЫБОРОМ INBOUND ============

@router.message(F.text == "🔑 Создать ключ (выбор inbound)")
@admin_only
async def admin_start_create_key(message: Message, state: FSMContext, **kwargs):
    """Начало создания ключа с выбором inbound (только для админа)"""
    await state.set_state(AdminCreateKeyStates.waiting_for_phone)
    await message.answer(
        "🔑 <b>Создание ключа с выбором inbound</b>\n\n"
        "Введите идентификатор клиента или нажмите кнопку для генерации:",
        reply_markup=Keyboards.phone_input(),
        parse_mode="HTML"
    )


@router.message(AdminCreateKeyStates.waiting_for_phone, F.text == "Отмена")
async def admin_cancel_create_key(message: Message, state: FSMContext):
    """Отмена создания ключа"""
    await state.clear()
    await message.answer(
        "Создание ключа отменено.",
        reply_markup=Keyboards.admin_menu()
    )


@router.message(AdminCreateKeyStates.waiting_for_phone, F.text == "Сгенерировать ID")
async def admin_generate_id(message: Message, state: FSMContext, xui_client: XUIClient):
    """Генерация ID и показ выбора сервера"""
    from bot.api.remote_xui import load_servers_config

    user_id_value = generate_user_id()
    await state.update_data(phone=user_id_value)

    # Получаем список серверов (только включённые)
    servers_config = load_servers_config()
    servers = [s for s in servers_config.get('servers', []) if s.get('enabled', False)]

    if not servers:
        await message.answer(
            "❌ Нет доступных серверов.",
            reply_markup=Keyboards.admin_menu()
        )
        await state.clear()
        return

    all_indices = list(range(len(servers)))
    await state.update_data(servers=servers, selected_server_indices=all_indices)
    await state.set_state(AdminCreateKeyStates.waiting_for_server)
    await message.answer(
        f"🆔 Сгенерирован ID: <code>{user_id_value}</code>\n\n"
        f"🖥 <b>Выберите серверы</b> (можно несколько):\n"
        f"Нажмите на сервер чтобы вкл/выкл, затем ✅ Продолжить",
        reply_markup=Keyboards.server_multi_selection(servers, all_indices),
        parse_mode="HTML"
    )


@router.message(AdminCreateKeyStates.waiting_for_phone)
async def admin_process_phone(message: Message, state: FSMContext, xui_client: XUIClient):
    """Обработка введенного ID и показ выбора сервера"""
    from bot.api.remote_xui import load_servers_config

    user_input = message.text.strip()

    if len(user_input) < 3:
        await message.answer("Идентификатор слишком короткий. Минимум 3 символа.")
        return

    await state.update_data(phone=user_input)

    # Получаем список серверов (только включённые)
    servers_config = load_servers_config()
    servers = [s for s in servers_config.get('servers', []) if s.get('enabled', False)]

    if not servers:
        await message.answer(
            "❌ Нет доступных серверов.",
            reply_markup=Keyboards.admin_menu()
        )
        await state.clear()
        return

    all_indices = list(range(len(servers)))
    await state.update_data(servers=servers, selected_server_indices=all_indices)
    await state.set_state(AdminCreateKeyStates.waiting_for_server)
    await message.answer(
        f"🆔 ID клиента: <code>{user_input}</code>\n\n"
        f"🖥 <b>Выберите серверы</b> (можно несколько):\n"
        f"Нажмите на сервер чтобы вкл/выкл, затем ✅ Продолжить",
        reply_markup=Keyboards.server_multi_selection(servers, all_indices),
        parse_mode="HTML"
    )


@router.callback_query(AdminCreateKeyStates.waiting_for_server, F.data.startswith("mserver_"))
async def admin_process_multi_server(callback: CallbackQuery, state: FSMContext):
    """Мульти-выбор серверов для админа"""
    data = await state.get_data()
    servers = data.get('servers', [])
    selected = data.get('selected_server_indices', [])
    action = callback.data.replace("mserver_", "")

    if action == "all":
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
        if not selected:
            await callback.answer("Выберите хотя бы один сервер!", show_alert=True)
            return

        phone = data.get('phone', '')
        selected_servers = [servers[i] for i in selected if i < len(servers)]

        if len(selected_servers) == 1:
            srv = selected_servers[0]
            # Если один сервер — показываем выбор inbound
            inbounds = srv.get('inbounds', {})
            if not inbounds:
                await callback.answer("У сервера нет inbound'ов", show_alert=True)
                return

            await state.update_data(
                selected_server=srv,
                multi_servers=None,
                server_idx=selected[0]
            )
            await state.set_state(AdminCreateKeyStates.waiting_for_inbound)
            await callback.message.edit_text(
                f"🖥 Сервер: <b>{srv.get('name', '?')}</b>\n\n"
                f"🔌 <b>Выберите inbound:</b>",
                reply_markup=Keyboards.inbound_selection_from_config(inbounds, srv.get('name', '')),
                parse_mode="HTML"
            )
        else:
            # Мульти-сервер — используем main inbound каждого
            server_text = ", ".join(s.get('name', '?') for s in selected_servers)
            await state.update_data(
                selected_server=selected_servers[0],
                selected_inbound=selected_servers[0].get('inbounds', {}).get('main', {}),
                inbound_id=selected_servers[0].get('inbounds', {}).get('main', {}).get('id', 1),
                multi_servers=selected_servers
            )
            await state.set_state(AdminCreateKeyStates.waiting_for_period)
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


@router.callback_query(AdminCreateKeyStates.waiting_for_server, F.data == "back_to_servers")
@router.callback_query(AdminCreateKeyStates.waiting_for_inbound, F.data == "back_to_servers")
async def admin_back_to_servers(callback: CallbackQuery, state: FSMContext):
    """Возврат к выбору сервера"""
    data = await state.get_data()
    servers = data.get('servers', [])
    phone = data.get('phone', '')

    selected = data.get('selected_server_indices', list(range(len(servers))))
    await state.set_state(AdminCreateKeyStates.waiting_for_server)
    await callback.message.edit_text(
        f"🆔 ID клиента: <code>{phone}</code>\n\n"
        f"🖥 <b>Выберите серверы</b> (можно несколько):",
        reply_markup=Keyboards.server_multi_selection(servers, selected),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(AdminCreateKeyStates.waiting_for_inbound, F.data.startswith("srv_inbound_"))
async def admin_process_inbound_from_config(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора inbound из конфига сервера"""
    inbound_key = callback.data.replace("srv_inbound_", "")
    data = await state.get_data()
    selected_server = data.get('selected_server', {})
    inbounds = selected_server.get('inbounds', {})

    if inbound_key not in inbounds:
        await callback.answer("Inbound не найден", show_alert=True)
        return

    selected_inbound = inbounds[inbound_key]
    inbound_id = selected_inbound.get('id', 1)

    await state.update_data(
        inbound_key=inbound_key,
        inbound_id=inbound_id,
        selected_inbound=selected_inbound
    )

    server_name = selected_server.get('name', 'Unknown')
    inbound_name = selected_inbound.get('name_prefix', inbound_key)

    await state.set_state(AdminCreateKeyStates.waiting_for_period)
    await callback.message.edit_text(
        f"🖥 Сервер: <b>{server_name}</b>\n"
        f"🔌 Inbound: <b>{inbound_name}</b>\n\n"
        "Выберите срок действия ключа:",
        reply_markup=Keyboards.subscription_periods(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(AdminCreateKeyStates.waiting_for_inbound, F.data.startswith("inbound_"))
async def admin_process_inbound(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора inbound (старый метод для совместимости)"""
    inbound_id = int(callback.data.split("_", 1)[1])
    await state.update_data(inbound_id=inbound_id)

    await state.set_state(AdminCreateKeyStates.waiting_for_period)
    await callback.message.edit_text(
        f"✅ Выбран inbound: <b>{inbound_id}</b>\n\n"
        "Выберите срок действия ключа:",
        reply_markup=Keyboards.subscription_periods(),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(AdminCreateKeyStates.waiting_for_period, F.data.startswith("period_"))
async def admin_process_period(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора периода"""
    period_key = callback.data.split("_", 1)[1]
    periods = get_subscription_periods()

    if period_key not in periods:
        await callback.answer("Неверный период", show_alert=True)
        return

    period_data = periods[period_key]
    await state.update_data(
        period_key=period_key,
        period_name=period_data['name'],
        period_days=period_data['days'],
        period_price=period_data['price']
    )

    data = await state.get_data()
    selected_server = data.get('selected_server', {})
    traffic_limit = selected_server.get('traffic_limit_gb', 0)

    if traffic_limit > 0:
        # Сервер имеет лимит трафика — показываем выбор
        await state.set_state(AdminCreateKeyStates.waiting_for_traffic)
        await callback.message.edit_text(
            f"📋 <b>Выбор трафика:</b>\n\n"
            f"🆔 ID: <code>{data['phone']}</code>\n"
            f"🖥 Сервер: <b>{selected_server.get('name', 'Unknown')}</b>\n"
            f"⏰ Период: {period_data['name']}\n\n"
            f"Сервер имеет ограничение трафика <b>{traffic_limit} ГБ</b>.\n"
            f"Выберите лимит трафика:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"📊 {traffic_limit} ГБ (рекомендуется)", callback_data=f"admkey_traffic_{traffic_limit}")],
                [InlineKeyboardButton(text="♾ Без ограничений", callback_data="admkey_traffic_0")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_key")]
            ]),
            parse_mode="HTML"
        )
    else:
        # Без лимита — сразу к подтверждению
        await state.set_state(AdminCreateKeyStates.confirming)
        await callback.message.edit_text(
            f"📋 <b>Подтверждение создания ключа:</b>\n\n"
            f"🆔 ID: <code>{data['phone']}</code>\n"
            f"🔌 Inbound: <b>{data['inbound_id']}</b>\n"
            f"⏰ Период: {period_data['name']}\n"
            f"💰 Цена: {period_data['price']} ₽\n\n"
            f"Создать ключ?",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Создать", callback_data="admin_confirm_key")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_key")]
            ]),
            parse_mode="HTML"
        )
    await callback.answer()


@router.callback_query(AdminCreateKeyStates.waiting_for_traffic, F.data.startswith("admkey_traffic_"))
async def admin_process_traffic_choice(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора трафика для ключа"""
    total_gb = int(callback.data.split("_")[-1])
    await state.update_data(admin_total_gb=total_gb)

    data = await state.get_data()
    traffic_text = f"{total_gb} ГБ" if total_gb > 0 else "безлимит"

    await state.set_state(AdminCreateKeyStates.confirming)
    await callback.message.edit_text(
        f"📋 <b>Подтверждение создания ключа:</b>\n\n"
        f"🆔 ID: <code>{data['phone']}</code>\n"
        f"🔌 Inbound: <b>{data['inbound_id']}</b>\n"
        f"⏰ Период: {data['period_name']}\n"
        f"💰 Цена: {data['period_price']} ₽\n"
        f"📊 Трафик: {traffic_text}\n\n"
        f"Создать ключ?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать", callback_data="admin_confirm_key")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel_key")]
        ]),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "admin_cancel_key")
async def admin_cancel_key_callback(callback: CallbackQuery, state: FSMContext):
    """Отмена создания ключа"""
    await state.clear()
    await callback.message.edit_text("Создание ключа отменено.")
    await callback.message.answer(
        "Панель администратора:",
        reply_markup=Keyboards.admin_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "admin_confirm_key")
async def admin_confirm_key(callback: CallbackQuery, state: FSMContext, db: DatabaseManager,
                           xui_client: XUIClient, bot, **kwargs):
    """Создание ключа на выбранном сервере"""
    import urllib.parse
    import uuid
    from datetime import datetime, timedelta
    from bot.api.remote_xui import load_servers_config, create_client_via_panel, create_client_on_remote_server

    data = await state.get_data()
    phone = data.get("phone")
    inbound_id = data.get("inbound_id")
    inbound_key = data.get("inbound_key", "main")
    period_name = data.get("period_name")
    period_days = data.get("period_days")
    period_price = data.get("period_price", 0)
    selected_server = data.get("selected_server")
    selected_inbound = data.get("selected_inbound")
    admin_total_gb = data.get("admin_total_gb", 0)

    # Логируем для отладки
    logger.info(f"Admin create key: server={selected_server.get('name') if selected_server else 'None'}, "
                f"inbound_id={inbound_id}, inbound_key={inbound_key}, total_gb={admin_total_gb}")

    await callback.message.edit_text("⏳ Создание ключа...")

    try:
        # Генерируем UUID для клиента
        client_uuid = str(uuid.uuid4())
        server_name = selected_server.get('name', 'Unknown') if selected_server else 'Local'

        # Создаём клиента на выбранном сервере
        success = False

        if selected_server:
            # Создаём на выбранном сервере
            if selected_server.get('local'):
                # Локальный сервер - используем xui_client
                client_data = await xui_client.add_client(
                    inbound_id=inbound_id,
                    email=phone,
                    phone=phone,
                    expire_days=period_days,
                    ip_limit=2
                )
                if client_data and not client_data.get('error'):
                    success = True
                    client_uuid = client_data.get('client_id', client_uuid)
                elif client_data and client_data.get('is_duplicate'):
                    await callback.message.edit_text(
                        f"⚠️ Клиент с ID <code>{phone}</code> уже существует!",
                        parse_mode="HTML"
                    )
                    await state.clear()
                    await callback.message.answer("Панель администратора:", reply_markup=Keyboards.admin_menu())
                    return
            else:
                # Удалённый сервер
                success = await create_client_on_remote_server(
                    server_config=selected_server,
                    client_uuid=client_uuid,
                    email=phone,
                    expire_days=period_days,
                    ip_limit=2,
                    inbound_id=inbound_id,
                    total_gb=admin_total_gb
                )

                # Авто-добавление на серверы с лимитом трафика (LTE Билайн)
                if success:
                    selected_name = selected_server.get('name', '')
                    all_servers = load_servers_config().get('servers', [])
                    for srv in all_servers:
                        if (srv.get('traffic_limit_gb', 0) > 0
                                and srv.get('enabled', True)
                                and not srv.get('local', False)
                                and srv.get('name') != selected_name):
                            try:
                                await create_client_on_remote_server(
                                    server_config=srv,
                                    client_uuid=client_uuid,
                                    email=phone,
                                    expire_days=period_days,
                                    ip_limit=2,
                                    total_gb=srv.get('traffic_limit_gb', 0)
                                )
                                logger.info(f"Авто-добавлен на {srv.get('name')} с лимитом {srv.get('traffic_limit_gb')} ГБ")
                            except Exception as e:
                                logger.error(f"Ошибка авто-добавления на {srv.get('name')}: {e}")
        else:
            # Старый режим - на локальном сервере
            client_data = await xui_client.add_client(
                inbound_id=inbound_id,
                email=phone,
                phone=phone,
                expire_days=period_days,
                ip_limit=2
            )
            if client_data and not client_data.get('error'):
                success = True
                client_uuid = client_data.get('client_id', client_uuid)

        if not success:
            await callback.message.edit_text("❌ Ошибка при создании ключа.")
            await state.clear()
            await callback.message.answer("Панель администратора:", reply_markup=Keyboards.admin_menu())
            return

        # Формируем VLESS ссылку из конфига выбранного сервера
        vless_link_for_user = None

        if selected_server and selected_inbound:
            from bot.api.remote_xui import get_inbound_settings_from_panel

            # Получаем актуальные настройки inbound с панели сервера
            inbound_id_for_settings = selected_inbound.get('id', 1)
            panel_settings = await get_inbound_settings_from_panel(selected_server, inbound_id_for_settings)

            # Если получили настройки с панели - используем их
            if panel_settings:
                selected_inbound = {**selected_inbound, **panel_settings}
                logger.info(f"Используем актуальные настройки с панели: sni={panel_settings.get('sni')}")

            domain = selected_server.get('domain', selected_server.get('ip', ''))
            port = selected_server.get('port', 443)
            network = selected_inbound.get('network', 'tcp')

            params = [f"type={network}", "encryption=none"]

            # Добавляем gRPC параметры если нужно
            if network == 'grpc':
                params.append(f"serviceName={selected_inbound.get('serviceName', '')}")
                params.append(f"authority={selected_inbound.get('authority', '')}")

            params.append(f"security={selected_inbound.get('security', 'reality')}")

            if selected_inbound.get('security') == 'reality':
                if selected_inbound.get('pbk'):
                    params.append(f"pbk={selected_inbound['pbk']}")
                params.append(f"fp={selected_inbound.get('fp', 'chrome')}")
                if selected_inbound.get('sni'):
                    params.append(f"sni={selected_inbound['sni']}")
                if selected_inbound.get('sid'):
                    params.append(f"sid={selected_inbound['sid']}")
                if selected_inbound.get('flow'):
                    params.append(f"flow={selected_inbound['flow']}")
                params.append("spx=%2F")

            query = '&'.join(params)
            name_prefix = selected_inbound.get('name_prefix', server_name)
            # Формируем имя: PREFIX пробел EMAIL (как в get_client_link_from_active_server)
            full_name = f"{name_prefix} {phone}" if phone else name_prefix

            vless_link_for_user = f"vless://{client_uuid}@{domain}:{port}?{query}#{full_name}"
        else:
            # Старый режим - из локального сервера
            vless_link_original = await xui_client.get_client_link(
                inbound_id=inbound_id,
                client_email=phone,
                use_domain=None
            )
            if vless_link_original:
                vless_link_for_user = XUIClient.replace_ip_with_domain(vless_link_original, DOMAIN)

        if not vless_link_for_user:
            await callback.message.edit_text("Ключ создан, но не удалось сформировать VLESS ссылку.")
            await state.clear()
            return

        # Сохраняем в БД
        await db.add_key_to_history(
            manager_id=callback.from_user.id,
            client_email=phone,
            phone_number=phone,
            period=period_name,
            expire_days=period_days,
            client_id=client_data['client_id'],
            price=period_price
        )

        # Ссылка подписки
        subscription_url = f"https://{_get_sub_domain(kwargs)}/sub/{client_uuid}"

        # QR код для ссылки подписки
        try:
            qr_code = generate_qr_code(subscription_url)
            await callback.message.answer_photo(
                BufferedInputFile(qr_code.read(), filename="qrcode.png"),
                caption=(
                    f"✅ Ключ создан!\n\n"
                    f"🆔 ID: {phone}\n"
                    f"🔌 Inbound: {inbound_id}\n"
                    f"⏰ Срок: {period_name}\n"
                    f"💰 Цена: {period_price} ₽"
                )
            )
        except Exception as e:
            logger.error(f"QR generation error: {e}")

        # Текстовый ключ и подписка
        await callback.message.answer(
            f"📋 VLESS ключ:\n\n`{vless_link_for_user}`\n\n"
            f"🔄 Ссылка подписки (мульти-сервер):\n`{subscription_url}`\n\n"
            f"💡 Подписка включает все серверы и автоматически обновляется.",
            parse_mode="Markdown"
        )

        await callback.message.delete()

    except Exception as e:
        logger.error(f"Error creating key: {e}")
        await callback.message.edit_text(f"❌ Ошибка: {str(e)}")

    finally:
        await state.clear()
        await callback.message.answer("Панель администратора:", reply_markup=Keyboards.admin_menu())

    await callback.answer()


# ============ КОНЕЦ СОЗДАНИЯ КЛЮЧА С ВЫБОРОМ INBOUND ============


@router.message(F.text == "Добавить менеджера")
@admin_only
async def start_add_manager(message: Message, state: FSMContext, **kwargs):
    """Начало добавления менеджера"""
    await state.set_state(AddManagerStates.waiting_for_user_id)
    await message.answer(
        "Отправьте ID пользователя Telegram, которого хотите добавить в менеджеры.\n\n"
        "Пользователь может узнать свой ID через @userinfobot\n\n"
        "Или нажмите 'Отмена' для возврата.",
        reply_markup=Keyboards.cancel()
    )


@router.message(AddManagerStates.waiting_for_user_id, F.text == "Отмена")
async def cancel_add_manager(message: Message, state: FSMContext):
    """Отмена добавления менеджера"""
    await state.clear()
    await message.answer(
        "Добавление менеджера отменено.",
        reply_markup=Keyboards.admin_menu()
    )


@router.message(AddManagerStates.waiting_for_user_id)
async def process_add_manager(message: Message, state: FSMContext, db: DatabaseManager):
    """Обработка добавления менеджера"""
    try:
        user_id = int(message.text.strip())

        # Проверяем, не является ли уже менеджером
        if await db.is_manager(user_id):
            await message.answer(
                "Этот пользователь уже является менеджером.",
                reply_markup=Keyboards.admin_menu()
            )
            await state.clear()
            return

        # Добавляем менеджера
        success = await db.add_manager(
            user_id=user_id,
            username="",  # Username будет заполнен при первом использовании бота
            full_name="",
            added_by=ADMIN_ID
        )

        if success:
            # Показываем выбор серверов для менеджера
            from bot.api.remote_xui import load_servers_config
            servers_config = load_servers_config()
            all_servers = [s for s in servers_config.get('servers', []) if s.get('enabled', True) and not s.get('local', False)]

            if all_servers:
                await state.update_data(new_manager_id=user_id, selected_servers=[s['name'] for s in all_servers])
                await state.set_state(AddManagerStates.waiting_for_server_permissions)
                keyboard = Keyboards.manager_server_permissions(all_servers, [s['name'] for s in all_servers])
                await message.answer(
                    f"✅ Менеджер с ID {user_id} успешно добавлен!\n\n"
                    f"🖥 <b>Выберите серверы, доступные менеджеру:</b>\n"
                    f"Нажмите на сервер чтобы включить/выключить доступ.\n"
                    f"По умолчанию доступны все серверы.",
                    reply_markup=keyboard,
                    parse_mode="HTML"
                )
                return
            else:
                await message.answer(
                    f"✅ Менеджер с ID {user_id} успешно добавлен!\n\n"
                    f"Пользователь теперь может использовать бота.",
                    reply_markup=Keyboards.admin_menu()
                )
        else:
            await message.answer(
                "Произошла ошибка при добавлении менеджера.",
                reply_markup=Keyboards.admin_menu()
            )

    except ValueError:
        await message.answer(
            "Некорректный ID. Введите числовое значение.\n"
            "Например: 123456789"
        )
        return

    await state.clear()


@router.callback_query(AddManagerStates.waiting_for_server_permissions, F.data.startswith("mgr_srv_toggle_"))
async def toggle_server_permission_new_manager(callback: CallbackQuery, state: FSMContext):
    """Переключение доступа к серверу при добавлении менеджера"""
    server_name = callback.data.replace("mgr_srv_toggle_", "")
    data = await state.get_data()
    selected = data.get('selected_servers', [])

    if server_name in selected:
        selected.remove(server_name)
    else:
        selected.append(server_name)

    await state.update_data(selected_servers=selected)

    from bot.api.remote_xui import load_servers_config
    servers_config = load_servers_config()
    all_servers = [s for s in servers_config.get('servers', []) if s.get('enabled', True) and not s.get('local', False)]

    keyboard = Keyboards.manager_server_permissions(all_servers, selected)
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@router.callback_query(AddManagerStates.waiting_for_server_permissions, F.data == "mgr_srv_save")
async def save_server_permission_new_manager(callback: CallbackQuery, state: FSMContext, db: DatabaseManager):
    """Сохранение серверов при добавлении менеджера"""
    data = await state.get_data()
    manager_id = data.get('new_manager_id')
    selected = data.get('selected_servers', [])

    from bot.api.remote_xui import load_servers_config
    servers_config = load_servers_config()
    all_servers = [s for s in servers_config.get('servers', []) if s.get('enabled', True) and not s.get('local', False)]
    all_names = [s['name'] for s in all_servers]

    # Если выбраны все серверы - сохраняем NULL (все доступны)
    if set(selected) >= set(all_names):
        await db.set_manager_allowed_servers(manager_id, None)
        servers_text = "все серверы"
    else:
        await db.set_manager_allowed_servers(manager_id, selected)
        servers_text = ", ".join(selected) if selected else "нет серверов"

    await state.clear()
    await callback.message.edit_text(
        f"✅ Менеджер с ID {manager_id} успешно добавлен!\n\n"
        f"🖥 Доступные серверы: <b>{servers_text}</b>\n\n"
        f"Пользователь теперь может использовать бота.",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("edit_mgr_servers_"))
async def start_edit_manager_servers(callback: CallbackQuery, state: FSMContext, db: DatabaseManager):
    """Начать редактирование серверов менеджера"""
    manager_id = int(callback.data.replace("edit_mgr_servers_", ""))

    from bot.api.remote_xui import load_servers_config
    servers_config = load_servers_config()
    all_servers = [s for s in servers_config.get('servers', []) if s.get('enabled', True) and not s.get('local', False)]

    if not all_servers:
        await callback.answer("Нет доступных серверов", show_alert=True)
        return

    allowed = await db.get_manager_allowed_servers(manager_id)
    # Если None - значит все серверы разрешены
    if allowed is None:
        selected = [s['name'] for s in all_servers]
    else:
        selected = allowed

    await state.set_state(EditManagerServersStates.waiting_for_server_selection)
    await state.update_data(edit_manager_id=manager_id, selected_servers=selected)

    keyboard = Keyboards.manager_server_permissions(all_servers, selected, edit_mode=True)
    await callback.message.edit_text(
        f"🖥 <b>СЕРВЕРЫ МЕНЕДЖЕРА</b> (ID: <code>{manager_id}</code>)\n\n"
        f"Нажмите на сервер чтобы включить/выключить доступ.\n"
        f"✅ = доступен, ❌ = недоступен",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(EditManagerServersStates.waiting_for_server_selection, F.data.startswith("mgr_srv_edit_toggle_"))
async def toggle_server_permission_edit(callback: CallbackQuery, state: FSMContext):
    """Переключение доступа к серверу при редактировании"""
    server_name = callback.data.replace("mgr_srv_edit_toggle_", "")
    data = await state.get_data()
    selected = data.get('selected_servers', [])

    if server_name in selected:
        selected.remove(server_name)
    else:
        selected.append(server_name)

    await state.update_data(selected_servers=selected)

    from bot.api.remote_xui import load_servers_config
    servers_config = load_servers_config()
    all_servers = [s for s in servers_config.get('servers', []) if s.get('enabled', True) and not s.get('local', False)]

    keyboard = Keyboards.manager_server_permissions(all_servers, selected, edit_mode=True)
    await callback.message.edit_reply_markup(reply_markup=keyboard)
    await callback.answer()


@router.callback_query(EditManagerServersStates.waiting_for_server_selection, F.data == "mgr_srv_edit_save")
async def save_server_permission_edit(callback: CallbackQuery, state: FSMContext, db: DatabaseManager):
    """Сохранение серверов при редактировании"""
    data = await state.get_data()
    manager_id = data.get('edit_manager_id')
    selected = data.get('selected_servers', [])

    from bot.api.remote_xui import load_servers_config
    servers_config = load_servers_config()
    all_servers = [s for s in servers_config.get('servers', []) if s.get('enabled', True) and not s.get('local', False)]
    all_names = [s['name'] for s in all_servers]

    if set(selected) >= set(all_names):
        await db.set_manager_allowed_servers(manager_id, None)
        servers_text = "все серверы"
    else:
        await db.set_manager_allowed_servers(manager_id, selected)
        servers_text = ", ".join(selected) if selected else "нет серверов"

    await state.clear()
    await callback.message.edit_text(
        f"✅ Серверы менеджера обновлены!\n\n"
        f"🖥 Доступные серверы: <b>{servers_text}</b>",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(EditManagerServersStates.waiting_for_server_selection, F.data == "mgr_srv_edit_cancel")
async def cancel_edit_manager_servers(callback: CallbackQuery, state: FSMContext):
    """Отмена редактирования серверов"""
    await state.clear()
    await callback.message.edit_text("❌ Редактирование серверов отменено.")
    await callback.answer()


@router.message(F.text == "Список менеджеров")
@admin_only
async def show_managers_list(message: Message, db: DatabaseManager, **kwargs):
    """Показать список всех менеджеров с возможностью редактирования"""
    managers = await db.get_all_managers()

    if not managers:
        await message.answer("Список менеджеров пуст.")
        return

    import json as _json
    text = "👥 <b>СПИСОК МЕНЕДЖЕРОВ</b>\n\n"
    text += "✏️ - изменить имя | 🖥 - настроить серверы\n\n"

    buttons = []

    for idx, manager in enumerate(managers, 1):
        custom_name = manager.get('custom_name', '') or ''
        username = manager.get('username', '') or ''
        full_name = manager.get('full_name', '') or ''
        added_at = manager['added_at'][:10]  # Только дата

        display_name = get_manager_display_name(manager)

        text += f"{idx}. <b>{display_name}</b>\n"

        # Дополнительная информация
        if custom_name:
            # Если установлено кастомное имя, показываем оригинальную информацию
            text += f"   📝 Пользовательское имя\n"
            if full_name:
                text += f"   👤 Реальное имя: {full_name}\n"
            if username:
                text += f"   📱 Username: @{username}\n"
        else:
            if full_name and username:
                text += f"   Username: @{username}\n"
            elif full_name:
                text += f"   Username: не установлен\n"

        text += f"   ID: <code>{manager['user_id']}</code>\n"
        text += f"   Добавлен: {added_at}\n"

        # Показываем доступные серверы
        allowed_raw = manager.get('allowed_servers')
        if allowed_raw:
            try:
                allowed_list = _json.loads(allowed_raw) if isinstance(allowed_raw, str) else allowed_raw
                text += f"   🖥 Серверы: {', '.join(allowed_list)}\n"
            except Exception:
                text += f"   🖥 Серверы: все\n"
        else:
            text += f"   🖥 Серверы: все\n"

        # Кнопки редактирования
        buttons.append([
            InlineKeyboardButton(
                text=f"✏️ {display_name[:20]}",
                callback_data=f"edit_mgr_name_{manager['user_id']}"
            ),
            InlineKeyboardButton(
                text=f"🖥 Серверы",
                callback_data=f"edit_mgr_servers_{manager['user_id']}"
            )
        ])
        text += "\n"

    text += f"━━━━━━━━━━━━━━━━\n"
    text += f"Всего менеджеров: {len(managers)}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.message(F.text == "Общая статистика")
@admin_only
async def show_general_stats(message: Message, db: DatabaseManager, **kwargs):
    """Показать общую статистику по всем менеджерам"""
    stats = await db.get_managers_detailed_stats()
    revenue_stats = await db.get_revenue_stats()
    admin_stats = await db.get_admin_revenue_stats(ADMIN_ID)
    managers_revenue = await db.get_managers_only_revenue_stats(exclude_admin_id=ADMIN_ID)

    text = "📊 <b>ОБЩАЯ СТАТИСТИКА</b>\n\n"

    # Статистика админа
    text += "━━━━━━━━━━━━━━━━\n"
    text += "👑 <b>ДОХОДЫ АДМИНА:</b>\n"
    text += f"💵 Всего: <b>{admin_stats['total']:,} ₽</b> ({admin_stats['total_keys']} ключей)\n"
    text += f"📅 Сегодня: <b>{admin_stats['today']:,} ₽</b> ({admin_stats['today_keys']} ключей)\n"
    text += f"📆 За месяц: <b>{admin_stats['month']:,} ₽</b> ({admin_stats['month_keys']} ключей)\n\n"

    # Статистика менеджеров
    text += "━━━━━━━━━━━━━━━━\n"
    text += "👥 <b>ДОХОДЫ МЕНЕДЖЕРОВ:</b>\n"
    text += f"💵 Всего: <b>{managers_revenue['total']:,} ₽</b>\n"
    text += f"📅 Сегодня: <b>{managers_revenue['today']:,} ₽</b>\n"
    text += f"📆 За месяц: <b>{managers_revenue['month']:,} ₽</b>\n\n"

    # Итого
    total_all_revenue = admin_stats['total'] + managers_revenue['total']
    total_today_revenue = admin_stats['today'] + managers_revenue['today']
    total_month_revenue = admin_stats['month'] + managers_revenue['month']

    text += "━━━━━━━━━━━━━━━━\n"
    text += "💰 <b>ИТОГО ДОХОДЫ:</b>\n"
    text += f"💵 Всего заработано: <b>{total_all_revenue:,} ₽</b>\n"
    text += f"📅 За сегодня: <b>{total_today_revenue:,} ₽</b>\n"
    text += f"📆 За месяц: <b>{total_month_revenue:,} ₽</b>\n\n"

    text += "━━━━━━━━━━━━━━━━\n\n"
    text += "👥 <b>ДЕТАЛИЗАЦИЯ ПО МЕНЕДЖЕРАМ:</b>\n\n"

    if not stats:
        text += "<i>Нет активных менеджеров</i>\n"
    else:
        total_all_keys = 0
        for idx, stat in enumerate(stats, 1):
            total_keys = stat['total_keys'] or 0
            today_keys = stat['today_keys'] or 0
            month_keys = stat['month_keys'] or 0

            total_revenue = stat['total_revenue'] or 0
            today_revenue = stat['today_revenue'] or 0
            month_revenue = stat['month_revenue'] or 0

            total_all_keys += total_keys

            # Используем общую функцию для получения имени
            display_name = get_manager_display_name(stat)

            text += (
                f"{idx}. <b>{display_name}</b>\n"
                f"   🔑 Ключей: {total_keys} (сегодня: {today_keys}, месяц: {month_keys})\n"
                f"   💰 Доход: {total_revenue:,} ₽ (сегодня: {today_revenue:,} ₽, месяц: {month_revenue:,} ₽)\n\n"
            )

        text += f"━━━━━━━━━━━━━━━━\n"
        text += f"🔑 <b>Всего ключей менеджеров: {total_all_keys}</b>\n"

    await message.answer(text, parse_mode="HTML")


@router.message(F.text == "Детальная статистика")
@admin_only
async def show_detailed_stats_menu(message: Message, **kwargs):
    """Показать меню детальной статистики"""
    await message.answer(
        "📊 Детальная статистика:\n\n"
        "Выберите тип отчета:",
        reply_markup=Keyboards.detailed_stats_menu()
    )


@router.callback_query(F.data == "stats_menu")
async def back_to_stats_menu(callback: CallbackQuery):
    """Вернуться в меню статистики"""
    await callback.message.edit_text(
        "📊 Детальная статистика:\n\n"
        "Выберите тип отчета:",
        reply_markup=Keyboards.detailed_stats_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "stats_back")
async def stats_back_to_admin(callback: CallbackQuery):
    """Закрыть статистику"""
    await callback.message.delete()
    await callback.answer("Возвращайтесь в панель администратора для новых отчетов")


@router.callback_query(F.data == "stats_by_days")
async def show_stats_by_days_menu(callback: CallbackQuery):
    """Показать меню выбора периода для статистики по дням"""
    await callback.message.edit_text(
        "📅 Статистика по дням\n\n"
        "Выберите период:",
        reply_markup=Keyboards.stats_period_menu()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("stats_days_"))
async def show_stats_by_days(callback: CallbackQuery, db: DatabaseManager):
    """Показать статистику по дням"""
    days = int(callback.data.split("_")[2])

    stats = await db.get_detailed_stats_by_day(days)

    if not stats:
        await callback.message.edit_text(
            f"📅 За последние {days} дней ключей не создавалось.",
            reply_markup=Keyboards.stats_period_menu()
        )
        await callback.answer()
        return

    text = f"📅 Статистика по дням (последние {days} дней):\n\n"

    total_keys = 0
    for stat in stats:
        date = stat['date']
        keys = stat['total_keys']
        managers = stat['active_managers']
        total_keys += keys

        text += f"📆 {date}\n"
        text += f"   🔑 Ключей: {keys}\n"
        text += f"   👥 Менеджеров: {managers}\n\n"

    text += f"━━━━━━━━━━━━━━━━\n"
    text += f"🔑 Всего за период: {total_keys} ключей\n"
    text += f"📊 Среднее в день: {total_keys // len(stats)} ключей\n"

    # Telegram имеет лимит на длину сообщения, разделим если нужно
    if len(text) > 4000:
        text = text[:4000] + "\n\n... (список сокращен)"

    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.stats_period_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "stats_by_months")
async def show_stats_by_months_menu(callback: CallbackQuery):
    """Показать меню выбора периода для статистики по месяцам"""
    await callback.message.edit_text(
        "📆 Статистика по месяцам\n\n"
        "Выберите период:",
        reply_markup=Keyboards.stats_months_menu()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("months_"))
async def show_stats_by_months(callback: CallbackQuery, db: DatabaseManager):
    """Показать статистику по месяцам"""
    period = callback.data.split("_")[1]

    if period == "all":
        months = 1200  # 100 лет, практически все данные
        period_text = "все время"
    else:
        months = int(period)
        period_text = f"последние {months} месяцев"

    stats = await db.get_detailed_stats_by_month(months)

    if not stats:
        await callback.message.edit_text(
            f"📆 За {period_text} ключей не создавалось.",
            reply_markup=Keyboards.stats_months_menu()
        )
        await callback.answer()
        return

    text = f"📆 Статистика по месяцам ({period_text}):\n\n"

    total_keys = 0
    for stat in stats:
        month = stat['month']
        keys = stat['total_keys']
        managers = stat['active_managers']
        total_keys += keys

        # Форматируем месяц
        year, month_num = month.split('-')
        month_names = {
            '01': 'Январь', '02': 'Февраль', '03': 'Март', '04': 'Апрель',
            '05': 'Май', '06': 'Июнь', '07': 'Июль', '08': 'Август',
            '09': 'Сентябрь', '10': 'Октябрь', '11': 'Ноябрь', '12': 'Декабрь'
        }
        month_name = month_names.get(month_num, month_num)

        text += f"📅 {month_name} {year}\n"
        text += f"   🔑 Ключей: {keys}\n"
        text += f"   👥 Менеджеров: {managers}\n\n"

    text += f"━━━━━━━━━━━━━━━━\n"
    text += f"🔑 Всего за период: {total_keys} ключей\n"
    if len(stats) > 0:
        text += f"📊 Среднее в месяц: {total_keys // len(stats)} ключей\n"

    if len(text) > 4000:
        text = text[:4000] + "\n\n... (список сокращен)"

    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.stats_months_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "stats_by_managers")
async def show_managers_for_stats(callback: CallbackQuery, db: DatabaseManager):
    """Показать список менеджеров для детальной статистики"""
    managers = await db.get_managers_detailed_stats()

    if not managers:
        await callback.message.edit_text(
            "👥 Нет активных менеджеров.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ Назад", callback_data="stats_menu")]
            ])
        )
        await callback.answer()
        return

    # Добавляем отображаемые имена для клавиатуры
    for manager in managers:
        manager['display_name'] = get_manager_display_name(manager)

    await callback.message.edit_text(
        "👥 Выберите менеджера для детальной статистики:\n\n"
        "(Показано общее количество созданных ключей)",
        reply_markup=Keyboards.managers_list_for_stats(managers)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("manager_stats_"))
async def show_manager_stats_period(callback: CallbackQuery, db: DatabaseManager):
    """Показать меню периода для статистики менеджера"""
    manager_id = int(callback.data.split("_")[2])

    # Получаем информацию о менеджере
    managers = await db.get_all_managers()
    manager = next((m for m in managers if m['user_id'] == manager_id), None)

    if not manager:
        await callback.answer("Менеджер не найден")
        return

    display_name = get_manager_display_name(manager)

    await callback.message.edit_text(
        f"👤 Статистика менеджера: <b>{display_name}</b>\n\n"
        "Выберите период:",
        reply_markup=Keyboards.manager_stats_period_menu(manager_id),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("mgr_period_"))
async def show_manager_detailed_stats(callback: CallbackQuery, db: DatabaseManager):
    """Показать детальную статистику менеджера"""
    parts = callback.data.split("_")
    manager_id = int(parts[2])
    period = parts[3]

    # Получаем информацию о менеджере
    managers = await db.get_all_managers()
    manager = next((m for m in managers if m['user_id'] == manager_id), None)

    if not manager:
        await callback.answer("Менеджер не найден")
        return

    display_name = get_manager_display_name(manager)

    # Определяем количество дней
    if period == "all":
        days = 10000  # Все данные
        period_text = "все время"
        stats_by_day = await db.get_stats_by_day_for_manager(manager_id, days)
        keys = await db.get_keys_by_manager_and_period(manager_id, days)
    else:
        days = int(period)
        period_text = f"последние {days} дней"
        stats_by_day = await db.get_stats_by_day_for_manager(manager_id, days)
        keys = await db.get_keys_by_manager_and_period(manager_id, days)

    if not keys:
        await callback.message.edit_text(
            f"👤 <b>Менеджер:</b> {display_name}\n"
            f"📅 <b>Период:</b> {period_text}\n\n"
            f"За выбранный период ключей не создавалось.",
            reply_markup=Keyboards.manager_stats_period_menu(manager_id),
            parse_mode="HTML"
        )
        await callback.answer()
        return

    text = f"👤 <b>Менеджер:</b> {display_name}\n"
    text += f"📅 <b>Период:</b> {period_text}\n\n"
    text += f"━━━━━━━━━━━━━━━━\n\n"

    # Статистика по дням
    if stats_by_day:
        text += "📊 Статистика по дням:\n\n"
        for stat in stats_by_day[:10]:  # Показываем последние 10 дней
            text += f"📆 {stat['date']}: {stat['total_keys']} ключей\n"

        if len(stats_by_day) > 10:
            text += f"\n... и еще {len(stats_by_day) - 10} дней\n"

        text += f"\n━━━━━━━━━━━━━━━━\n\n"

    # Общая информация
    text += f"🔑 Всего ключей за период: {len(keys)}\n"

    if stats_by_day:
        text += f"📊 Среднее в день: {len(keys) // len(stats_by_day)}\n"

    text += f"\n━━━━━━━━━━━━━━━━\n\n"
    text += "📋 Последние 10 ключей:\n\n"

    # Показываем последние ключи
    for idx, key in enumerate(keys[:10], 1):
        created = key['created_at'][:16].replace('T', ' ')  # Дата и время
        text += f"{idx}. {key['phone_number']}\n"
        text += f"   Срок: {key['period']}\n"
        text += f"   Создан: {created}\n\n"

    if len(text) > 4000:
        text = text[:4000] + "\n\n... (список сокращен)"

    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.manager_stats_period_menu(manager_id),
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(F.text == "💰 Изменить цены")
@admin_only
async def show_price_edit_menu(message: Message, **kwargs):
    """Показать меню редактирования цен"""
    periods = get_subscription_periods()

    text = "💰 <b>РЕДАКТИРОВАНИЕ ЦЕН</b>\n\n"
    text += "Текущие цены:\n\n"

    for key, info in periods.items():
        text += f"📅 <b>{info['name']}</b> ({info['days']} дней)\n"
        text += f"   💵 {info['price']} ₽\n\n"

    text += "Выберите тариф для изменения цены:"

    await message.answer(
        text,
        reply_markup=Keyboards.price_edit_menu(periods),
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("edit_price_"))
async def start_price_edit(callback: CallbackQuery, state: FSMContext):
    """Начать редактирование цены"""
    period_key = callback.data.replace("edit_price_", "")
    periods = get_subscription_periods()

    if period_key not in periods:
        await callback.answer("Ошибка: тариф не найден")
        return

    period_info = periods[period_key]

    await state.set_state(EditPriceStates.waiting_for_new_price)
    await state.update_data(period_key=period_key)

    await callback.message.edit_text(
        f"💰 <b>Изменение цены</b>\n\n"
        f"📅 Тариф: <b>{period_info['name']}</b>\n"
        f"💵 Текущая цена: <b>{period_info['price']} ₽</b>\n\n"
        f"Введите новую цену (только число):",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_price_edit")
async def cancel_price_edit(callback: CallbackQuery, state: FSMContext):
    """Отмена редактирования цены"""
    await state.clear()
    await callback.message.delete()
    await callback.answer("Редактирование отменено")


@router.message(EditPriceStates.waiting_for_new_price)
async def process_new_price(message: Message, state: FSMContext):
    """Обработка новой цены"""
    try:
        new_price = int(message.text.strip())

        if new_price < 0:
            await message.answer("❌ Цена не может быть отрицательной. Попробуйте еще раз:")
            return

        if new_price > 1000000:
            await message.answer("❌ Цена слишком большая. Попробуйте еще раз:")
            return

        # Получаем данные из состояния
        data = await state.get_data()
        period_key = data.get('period_key')

        periods = get_subscription_periods()
        if period_key not in periods:
            await message.answer("❌ Ошибка: тариф не найден")
            await state.clear()
            return

        period_info = periods[period_key]
        old_price = period_info['price']

        # Обновляем цену
        success = PriceManager.update_price(period_key, new_price)

        if success:
            # Обновляем глобальную переменную (для обратной совместимости)
            from bot import config
            config.SUBSCRIPTION_PERIODS = get_subscription_periods()

            await message.answer(
                f"✅ <b>Цена успешно обновлена!</b>\n\n"
                f"📅 Тариф: <b>{period_info['name']}</b>\n"
                f"💵 Старая цена: {old_price} ₽\n"
                f"💵 Новая цена: <b>{new_price} ₽</b>\n\n"
                f"Изменения вступили в силу немедленно.",
                parse_mode="HTML",
                reply_markup=Keyboards.admin_menu()
            )
        else:
            await message.answer(
                "❌ Произошла ошибка при сохранении цены. Попробуйте еще раз.",
                reply_markup=Keyboards.admin_menu()
            )

        await state.clear()

    except ValueError:
        await message.answer(
            "❌ Некорректная цена. Введите целое число.\n"
            "Например: 500"
        )


@router.callback_query(F.data.startswith("edit_mgr_name_"))
async def start_edit_manager_name(callback: CallbackQuery, state: FSMContext, db: DatabaseManager):
    """Начать редактирование имени менеджера"""
    manager_id = int(callback.data.replace("edit_mgr_name_", ""))

    # Получаем информацию о менеджере
    managers = await db.get_all_managers()
    manager = next((m for m in managers if m['user_id'] == manager_id), None)

    if not manager:
        await callback.answer("Менеджер не найден")
        return

    display_name = get_manager_display_name(manager)
    custom_name = manager.get('custom_name', '') or ''
    full_name = manager.get('full_name', '') or ''
    username = manager.get('username', '') or ''

    text = f"✏️ <b>РЕДАКТИРОВАНИЕ ИМЕНИ МЕНЕДЖЕРА</b>\n\n"
    text += f"📋 <b>ID менеджера:</b> <code>{manager_id}</code>\n\n"

    if custom_name:
        text += f"📝 Текущее имя: <b>{custom_name}</b> (пользовательское)\n"
    else:
        text += f"📝 Текущее имя: <b>{display_name}</b>\n"

    if full_name:
        text += f"👤 Реальное имя из Telegram: {full_name}\n"
    if username:
        text += f"📱 Username из Telegram: @{username}\n"

    text += f"\n━━━━━━━━━━━━━━━━\n\n"
    text += f"Введите новое имя для менеджера:\n\n"
    text += f"<i>• Введите имя, которое будет отображаться в списках\n"
    text += f"• Введите \"/clear\" чтобы удалить пользовательское имя\n"
    text += f"• Введите \"/cancel\" для отмены</i>"

    await state.set_state(EditManagerNameStates.waiting_for_new_name)
    await state.update_data(manager_id=manager_id)

    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@router.message(EditManagerNameStates.waiting_for_new_name)
async def process_new_manager_name(message: Message, state: FSMContext, db: DatabaseManager):
    """Обработка нового имени менеджера"""
    data = await state.get_data()
    manager_id = data.get('manager_id')

    if not manager_id:
        await message.answer("❌ Ошибка: менеджер не найден")
        await state.clear()
        return

    new_name = message.text.strip()

    # Проверка на команды
    if new_name == "/cancel":
        await message.answer(
            "❌ Редактирование отменено.",
            reply_markup=Keyboards.admin_menu()
        )
        await state.clear()
        return

    # Очистка пользовательского имени
    if new_name == "/clear":
        success = await db.set_manager_custom_name(manager_id, "")
        if success:
            await message.answer(
                f"✅ Пользовательское имя удалено!\n\n"
                f"Теперь будет отображаться автоматическое имя из Telegram.",
                reply_markup=Keyboards.admin_menu()
            )
        else:
            await message.answer(
                "❌ Произошла ошибка при удалении имени.",
                reply_markup=Keyboards.admin_menu()
            )
        await state.clear()
        return

    # Проверка длины имени
    if len(new_name) < 2:
        await message.answer("❌ Имя слишком короткое. Минимум 2 символа.")
        return

    if len(new_name) > 100:
        await message.answer("❌ Имя слишком длинное. Максимум 100 символов.")
        return

    # Получаем старую информацию
    managers = await db.get_all_managers()
    manager = next((m for m in managers if m['user_id'] == manager_id), None)

    if not manager:
        await message.answer("❌ Менеджер не найден")
        await state.clear()
        return

    old_display_name = get_manager_display_name(manager)

    # Обновляем имя
    success = await db.set_manager_custom_name(manager_id, new_name)

    if success:
        await message.answer(
            f"✅ <b>Имя успешно обновлено!</b>\n\n"
            f"📋 ID менеджера: <code>{manager_id}</code>\n"
            f"📝 Старое имя: {old_display_name}\n"
            f"📝 Новое имя: <b>{new_name}</b>\n\n"
            f"Изменения сразу отобразятся во всех списках и статистике.",
            parse_mode="HTML",
            reply_markup=Keyboards.admin_menu()
        )
    else:
        await message.answer(
            "❌ Произошла ошибка при сохранении имени.",
            reply_markup=Keyboards.admin_menu()
        )

    await state.clear()


@router.message(F.text == "🗑️ Удалить ключ")
@admin_only
async def show_keys_for_deletion(message: Message, db: DatabaseManager, **kwargs):
    """Показать список последних ключей для удаления"""
    # Получаем последние 20 ключей
    keys = await db.get_recent_keys(limit=20)

    if not keys:
        await message.answer(
            "📋 Список ключей пуст.\n\n"
            "Нет созданных ключей для удаления."
        )
        return

    text = "🗑️ <b>УДАЛЕНИЕ КЛЮЧЕЙ</b>\n\n"
    text += "Последние 20 созданных ключей:\n\n"
    text += "<i>⚠️ Удаление записи уберет ключ ТОЛЬКО из аналитики бота.\n"
    text += "Ключ останется активным в X-UI панели!</i>\n\n"
    text += "━━━━━━━━━━━━━━━━\n\n"

    buttons = []

    for idx, key in enumerate(keys[:20], 1):
        # Получаем имя менеджера
        custom_name = key.get('custom_name', '') or ''
        full_name = key.get('full_name', '') or ''
        username = key.get('username', '') or ''

        if custom_name:
            manager_name = custom_name
        elif full_name:
            manager_name = full_name
        elif username:
            manager_name = f"@{username}"
        else:
            manager_name = f"ID: {key['manager_id']}"

        # Форматируем дату
        created_at = key['created_at'][:16].replace('T', ' ')

        text += f"{idx}. <b>{key['phone_number']}</b>\n"
        text += f"   👤 Менеджер: {manager_name}\n"
        text += f"   📅 Срок: {key['period']}\n"
        text += f"   💰 Цена: {key['price']} ₽\n"
        text += f"   🕒 Создан: {created_at}\n\n"

        # Кнопка удаления
        buttons.append([
            InlineKeyboardButton(
                text=f"🗑️ {key['phone_number'][:15]}",
                callback_data=f"del_key_{key['id']}"
            )
        ])

        # Ограничиваем длину сообщения
        if len(text) > 3500:
            text += "\n<i>... список сокращен</i>"
            break

    # Добавляем кнопку "Назад"
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="cancel_key_delete")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "cancel_key_delete")
async def cancel_key_deletion(callback: CallbackQuery, **kwargs):
    """Отмена операции — возврат в админ-меню"""
    await callback.message.delete()
    await callback.message.answer("Панель администратора", reply_markup=Keyboards.admin_menu())
    await callback.answer("Отменено")


@router.callback_query(F.data.startswith("del_key_"))
async def confirm_key_deletion(callback: CallbackQuery, db: DatabaseManager):
    """Подтверждение удаления ключа"""
    key_id = int(callback.data.replace("del_key_", ""))

    # Получаем информацию о ключе
    key = await db.get_key_by_id(key_id)

    if not key:
        await callback.message.edit_text("❌ Ключ не найден в базе данных.")
        await callback.answer()
        return

    # Получаем имя менеджера
    custom_name = key.get('custom_name', '') or ''
    full_name = key.get('full_name', '') or ''
    username = key.get('username', '') or ''

    if custom_name:
        manager_name = custom_name
    elif full_name:
        manager_name = full_name
    elif username:
        manager_name = f"@{username}"
    else:
        manager_name = f"ID: {key['manager_id']}"

    created_at = key['created_at'][:16].replace('T', ' ')

    text = "⚠️ <b>ПОДТВЕРЖДЕНИЕ УДАЛЕНИЯ</b>\n\n"
    text += "Вы уверены, что хотите удалить эту запись?\n\n"
    text += f"📋 ID записи: <code>{key['id']}</code>\n"
    text += f"📱 Номер/ID: <b>{key['phone_number']}</b>\n"
    text += f"👤 Менеджер: {manager_name}\n"
    text += f"📅 Срок: {key['period']}\n"
    text += f"💰 Цена: {key['price']} ₽\n"
    text += f"🕒 Создан: {created_at}\n\n"
    text += "━━━━━━━━━━━━━━━━\n\n"
    text += "⚠️ <b>ВАЖНО:</b>\n"
    text += "• Запись будет удалена из аналитики бота\n"
    text += "• Ключ останется активным в X-UI панели\n"
    text += "• Это действие нельзя отменить!"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"confirm_del_{key_id}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_key_delete")
        ]
    ])

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("confirm_del_"))
async def delete_key_record(callback: CallbackQuery, db: DatabaseManager):
    """Фактическое удаление записи о ключе из БД и X-UI панели"""
    from bot.api.xui_client import XUIClient
    from bot.config import XUI_HOST, XUI_USERNAME, XUI_PASSWORD

    key_id = int(callback.data.replace("confirm_del_", ""))

    # Получаем информацию перед удалением
    key = await db.get_key_by_id(key_id)

    if not key:
        await callback.message.edit_text("❌ Ключ уже был удален или не найден.")
        await callback.answer()
        return

    # Показываем процесс удаления
    await callback.message.edit_text(
        f"⏳ <b>Удаление ключа...</b>\n\n"
        f"📱 Номер/ID: <code>{key['phone_number']}</code>\n\n"
        f"Удаление из X-UI панели...",
        parse_mode="HTML"
    )

    xui_deleted = False
    remote_deleted = {}
    client_email = key.get('client_email', '')

    # Удаляем клиента из X-UI если есть email
    if client_email:
        # Удаляем с локального сервера
        try:
            async with XUIClient(XUI_HOST, XUI_USERNAME, XUI_PASSWORD) as xui:
                xui_deleted = await xui.find_and_delete_client(client_email)
                if xui_deleted:
                    logger.info(f"Клиент {client_email} удален из X-UI панели (локально)")
                else:
                    logger.warning(f"Клиент {client_email} не найден в X-UI панели (возможно уже удален)")
        except Exception as e:
            logger.error(f"Ошибка при удалении клиента из X-UI: {e}")
            xui_deleted = False

        # Удаляем с удалённых серверов
        try:
            from bot.api.remote_xui import delete_client_by_email_on_all_remote_servers
            remote_deleted = await delete_client_by_email_on_all_remote_servers(client_email)
            if remote_deleted:
                for server_name, success in remote_deleted.items():
                    if success:
                        logger.info(f"Клиент {client_email} удален с сервера {server_name}")
                    else:
                        logger.warning(f"Клиент {client_email} не удален с сервера {server_name}")
        except Exception as e:
            logger.error(f"Ошибка при удалении клиента с удалённых серверов: {e}")

    # Удаляем запись из базы данных
    db_success = await db.delete_key_record(key_id)

    if db_success:
        # Формируем строку статуса удалённых серверов
        remote_status_lines = []
        all_remote_success = True
        for server_name, success in remote_deleted.items():
            if success:
                remote_status_lines.append(f"✅ {server_name}")
            else:
                remote_status_lines.append(f"⚠️ {server_name} (не найден)")
                all_remote_success = False
        remote_status = "\n".join(remote_status_lines) if remote_status_lines else ""

        if xui_deleted and all_remote_success:
            result_text = (
                f"✅ <b>Ключ полностью удален!</b>\n\n"
                f"📱 Номер/ID: <code>{key['phone_number']}</code>\n"
                f"📅 Срок: {key['period']}\n"
                f"💰 Цена: {key['price']} ₽\n\n"
                f"✅ Удален из X-UI панели (локально)\n"
            )
            if remote_status:
                result_text += f"\n<b>Удалённые серверы:</b>\n{remote_status}\n"
            result_text += f"\n✅ Удален из аналитики бота"
        else:
            result_text = (
                f"⚠️ <b>Запись удалена частично</b>\n\n"
                f"📱 Номер/ID: <code>{key['phone_number']}</code>\n"
                f"📅 Срок: {key['period']}\n"
                f"💰 Цена: {key['price']} ₽\n\n"
            )
            if xui_deleted:
                result_text += f"✅ Удален из X-UI (локально)\n"
            else:
                result_text += f"⚠️ Не найден в X-UI (локально)\n"

            if remote_status:
                result_text += f"\n<b>Удалённые серверы:</b>\n{remote_status}\n"

            result_text += f"\n✅ Удален из аналитики бота\n\n"
            result_text += f"<i>Возможно ключ уже был удален ранее</i>"
        await callback.message.edit_text(result_text, parse_mode="HTML")
    else:
        await callback.message.edit_text(
            "❌ <b>Ошибка при удалении!</b>\n\n"
            "Не удалось удалить запись из базы данных.\n"
            "Обратитесь к администратору.",
            parse_mode="HTML"
        )

    await callback.answer("Готово" if db_success else "Ошибка")


# ===== СИСТЕМА УВЕДОМЛЕНИЙ ДЛЯ МЕНЕДЖЕРОВ =====

@router.message(F.text == "📢 Отправить уведомление")
@admin_only
async def start_send_notification(message: Message, state: FSMContext, **kwargs):
    """Начало отправки уведомления всем менеджерам"""
    await state.set_state(SendNotificationStates.waiting_for_message)
    await message.answer(
        "📢 <b>Отправка уведомления менеджерам</b>\n\n"
        "Введите текст уведомления, которое будет отправлено всем менеджерам.\n\n"
        "Вы можете использовать HTML-форматирование:\n"
        "• <code>&lt;b&gt;жирный текст&lt;/b&gt;</code>\n"
        "• <code>&lt;i&gt;курсив&lt;/i&gt;</code>\n"
        "• <code>&lt;code&gt;моноширинный&lt;/code&gt;</code>\n"
        "• <code>&lt;a href=\"url\"&gt;ссылка&lt;/a&gt;</code>\n\n"
        "Или нажмите 'Отмена' для возврата.",
        parse_mode="HTML",
        reply_markup=Keyboards.cancel()
    )


@router.message(SendNotificationStates.waiting_for_message, F.text == "Отмена")
async def cancel_send_notification(message: Message, state: FSMContext):
    """Отмена отправки уведомления"""
    await state.clear()
    await message.answer(
        "Отправка уведомления отменена.",
        reply_markup=Keyboards.admin_menu()
    )


@router.message(SendNotificationStates.waiting_for_message)
async def process_notification_message(message: Message, state: FSMContext, db: DatabaseManager, bot):
    """Обработка и отправка уведомления всем менеджерам"""
    notification_text = message.text

    # Получаем список всех менеджеров
    managers = await db.get_all_managers()

    if not managers:
        await message.answer(
            "❌ В системе нет зарегистрированных менеджеров.",
            reply_markup=Keyboards.admin_menu()
        )
        await state.clear()
        return

    # Отправляем уведомление
    await message.answer(
        f"📤 Отправка уведомления {len(managers)} менеджерам...\n"
        "Пожалуйста, подождите...",
        reply_markup=Keyboards.admin_menu()
    )

    success_count = 0
    failed_count = 0
    failed_managers = []

    # Формируем итоговое сообщение с заголовком
    final_notification = (
        "📢 <b>УВЕДОМЛЕНИЕ ОТ АДМИНИСТРАТОРА</b>\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{notification_text}\n\n"
        "━━━━━━━━━━━━━━━━━━━━"
    )

    for manager in managers:
        try:
            await bot.send_message(
                chat_id=manager['user_id'],
                text=final_notification,
                parse_mode="HTML"
            )
            success_count += 1
        except Exception as e:
            failed_count += 1
            manager_name = get_manager_display_name(manager)
            failed_managers.append(f"{manager_name} (ID: {manager['user_id']})")
            logger.error(f"Не удалось отправить уведомление менеджеру {manager['user_id']}: {e}")

    # Отправляем отчет администратору
    report = (
        f"✅ <b>Уведомление отправлено!</b>\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• Успешно: {success_count}\n"
        f"• Ошибок: {failed_count}\n"
        f"• Всего менеджеров: {len(managers)}\n"
    )

    if failed_managers:
        report += f"\n❌ <b>Не удалось отправить:</b>\n"
        for manager in failed_managers[:10]:  # Показываем первые 10
            report += f"• {manager}\n"
        if len(failed_managers) > 10:
            report += f"• ... и еще {len(failed_managers) - 10}\n"

    await message.answer(report, parse_mode="HTML")
    await state.clear()


# ===== УПРАВЛЕНИЕ НАСТРОЙКАМИ СЕРВЕРОВ (SNI, Target, Transport) =====

@router.message(F.text == "🌐 Управление SNI")
@admin_only
async def show_server_management(message: Message, **kwargs):
    """Показать список серверов для управления настройками"""
    from bot.api.remote_xui import load_servers_config
    import json

    servers_config = load_servers_config()
    servers = servers_config.get('servers', [])

    # Фильтруем только включенные серверы
    enabled_servers = [s for s in servers if s.get('enabled', True)]

    if not enabled_servers:
        await message.answer(
            "❌ Нет активных серверов в конфигурации.",
            reply_markup=Keyboards.admin_menu()
        )
        return

    text = "🖥 <b>УПРАВЛЕНИЕ НАСТРОЙКАМИ СЕРВЕРОВ</b>\n\n"
    text += "Выберите сервер для изменения настроек:\n\n"

    buttons = []
    for srv in enabled_servers:
        name = srv.get('name', 'Unknown')
        domain = srv.get('domain', srv.get('ip', ''))
        is_local = srv.get('local', False)
        active = "🟢" if srv.get('active_for_new') else "🟡"

        text += f"{active} <b>{name}</b>\n"
        text += f"   🌐 {domain}\n"
        text += f"   📍 {'Локальный' if is_local else 'Удалённый'}\n\n"

        buttons.append([
            InlineKeyboardButton(
                text=f"{active} {name}",
                callback_data=f"srv_manage_{name}"
            )
        ])

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="sni_cancel")])

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data.startswith("srv_manage_"))
async def select_server_for_management(callback: CallbackQuery, state: FSMContext):
    """Выбор сервера для управления настройками"""
    from bot.api.remote_xui import load_servers_config, _get_panel_opener, _panel_login
    import json

    server_name = callback.data.replace("srv_manage_", "")
    servers_config = load_servers_config()

    # Находим сервер
    server = None
    for srv in servers_config.get('servers', []):
        if srv.get('name') == server_name:
            server = srv
            break

    if not server:
        await callback.answer("❌ Сервер не найден", show_alert=True)
        return

    await callback.answer("⏳ Загружаю настройки...")

    is_local = server.get('local', False)

    try:
        if is_local:
            # Локальный сервер - читаем из SQLite
            import sqlite3
            conn = sqlite3.connect('/etc/x-ui/x-ui.db')
            cursor = conn.cursor()
            cursor.execute("SELECT id, remark, port, streamSettings FROM inbounds WHERE enable=1")
            rows = cursor.fetchall()
            conn.close()

            inbounds_info = []
            for inbound_id, remark, port, stream_str in rows:
                stream = json.loads(stream_str) if stream_str else {}
                if stream.get('security') == 'reality':
                    reality = stream.get('realitySettings', {})
                    inbounds_info.append({
                        'id': inbound_id,
                        'remark': remark,
                        'port': port,
                        'network': stream.get('network', 'tcp'),
                        'dest': reality.get('dest', 'не указан'),
                        'sni': reality.get('serverNames', [])
                    })
        else:
            # Удалённый сервер - через API панели
            panel = server.get('panel', {})
            if not panel:
                await callback.message.edit_text("❌ У сервера нет настроек панели")
                return

            session = await _get_panel_opener(server_name)
            if not session.get('logged_in'):
                if not await _panel_login(server):
                    await callback.message.edit_text("❌ Не удалось авторизоваться в панели")
                    return

            import urllib.request
            base_url = session.get('base_url', '')
            opener = session.get('opener')

            list_url = f"{base_url}/panel/api/inbounds/list"
            list_req = urllib.request.Request(list_url)

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, opener.open, list_req)
            data = json.loads(response.read().decode())

            if not data.get('success'):
                await callback.message.edit_text("❌ Не удалось получить список inbounds")
                return

            inbounds_info = []
            for inb in data.get('obj', []):
                stream = json.loads(inb.get('streamSettings', '{}'))
                if stream.get('security') == 'reality':
                    reality = stream.get('realitySettings', {})
                    inbounds_info.append({
                        'id': inb.get('id'),
                        'remark': inb.get('remark', ''),
                        'port': inb.get('port'),
                        'network': stream.get('network', 'tcp'),
                        'dest': reality.get('dest', 'не указан'),
                        'sni': reality.get('serverNames', [])
                    })

        if not inbounds_info:
            await callback.message.edit_text(
                f"📋 Reality inbound-ы не найдены на {server_name}."
            )
            return

        # Сохраняем данные сервера
        await state.update_data(
            manage_server_name=server_name,
            manage_server_local=is_local,
            manage_server_config=server
        )

        # Показываем inbounds сервера
        text = f"🖥 <b>{server_name}</b>\n\n"
        text += "Reality inbound-ы:\n\n"

        buttons = []
        for inb in inbounds_info:
            text += f"📍 <b>{inb['remark']}</b> (ID: {inb['id']})\n"
            text += f"   📡 Transport: <code>{inb['network']}</code>\n"
            text += f"   🎯 Target: <code>{inb['dest']}</code>\n"
            text += f"   🌐 SNI: <code>{', '.join(inb['sni'][:2]) if inb['sni'] else 'нет'}</code>\n\n"

            buttons.append([
                InlineKeyboardButton(
                    text=f"⚙️ {inb['remark']}",
                    callback_data=f"inb_manage_{inb['id']}"
                )
            ])

        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="back_to_srv_list")])
        buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="sni_cancel")])

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )

    except Exception as e:
        logger.error(f"Ошибка при загрузке настроек сервера {server_name}: {e}")
        await callback.message.edit_text(f"❌ Ошибка: {str(e)[:100]}")


@router.callback_query(F.data == "back_to_srv_list")
async def back_to_server_list(callback: CallbackQuery, state: FSMContext):
    """Вернуться к списку серверов"""
    await state.clear()
    await callback.message.delete()
    await callback.answer()
    # Вызываем показ списка серверов заново
    from bot.api.remote_xui import load_servers_config

    servers_config = load_servers_config()
    servers = servers_config.get('servers', [])
    enabled_servers = [s for s in servers if s.get('enabled', True)]

    text = "🖥 <b>УПРАВЛЕНИЕ НАСТРОЙКАМИ СЕРВЕРОВ</b>\n\n"
    buttons = []
    for srv in enabled_servers:
        name = srv.get('name', 'Unknown')
        active = "🟢" if srv.get('active_for_new') else "🟡"
        buttons.append([
            InlineKeyboardButton(text=f"{active} {name}", callback_data=f"srv_manage_{name}")
        ])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="sni_cancel")])

    await callback.message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data.startswith("inb_manage_"))
async def select_inbound_action(callback: CallbackQuery, state: FSMContext):
    """Выбор действия для inbound"""
    from bot.api.remote_xui import _get_panel_opener
    import json

    inbound_id = int(callback.data.replace("inb_manage_", ""))
    data = await state.get_data()

    server_name = data.get('manage_server_name')
    is_local = data.get('manage_server_local', False)
    server_config = data.get('manage_server_config', {})

    # Получаем актуальные данные inbound
    try:
        if is_local:
            import sqlite3
            conn = sqlite3.connect('/etc/x-ui/x-ui.db')
            cursor = conn.cursor()
            cursor.execute("SELECT remark, port, streamSettings FROM inbounds WHERE id=?", (inbound_id,))
            row = cursor.fetchone()
            conn.close()

            if not row:
                await callback.answer("❌ Inbound не найден", show_alert=True)
                return

            remark, port, stream_str = row
            stream = json.loads(stream_str) if stream_str else {}
        else:
            session = await _get_panel_opener(server_name)
            base_url = session.get('base_url', '')
            opener = session.get('opener')

            import urllib.request
            get_url = f"{base_url}/panel/api/inbounds/get/{inbound_id}"
            get_req = urllib.request.Request(get_url)

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, opener.open, get_req)
            result = json.loads(response.read().decode())

            if not result.get('success'):
                await callback.answer("❌ Не удалось получить inbound", show_alert=True)
                return

            inb = result.get('obj', {})
            remark = inb.get('remark', '')
            port = inb.get('port')
            stream = json.loads(inb.get('streamSettings', '{}'))

        reality = stream.get('realitySettings', {})
        network = stream.get('network', 'tcp')
        dest = reality.get('dest', '')
        sni_list = reality.get('serverNames', [])

        # Сохраняем в state
        await state.update_data(
            manage_inbound_id=inbound_id,
            manage_inbound_remark=remark,
            manage_current_network=network,
            manage_current_dest=dest,
            manage_current_sni=sni_list
        )

        text = f"⚙️ <b>НАСТРОЙКИ INBOUND</b>\n\n"
        text += f"🖥 Сервер: <b>{server_name}</b>\n"
        text += f"📍 Inbound: <b>{remark}</b> (ID: {inbound_id})\n\n"
        text += f"━━━━━━━━━━━━━━━━\n\n"
        text += f"📡 <b>Transport:</b> <code>{network}</code>\n"
        text += f"🎯 <b>Target (Dest):</b> <code>{dest or 'не указан'}</code>\n"
        text += f"🌐 <b>SNI:</b>\n"
        if sni_list:
            for sni in sni_list[:5]:
                text += f"   • <code>{sni}</code>\n"
            if len(sni_list) > 5:
                text += f"   <i>...и ещё {len(sni_list) - 5}</i>\n"
        else:
            text += f"   <i>не указаны</i>\n"

        text += f"\n━━━━━━━━━━━━━━━━\n\n"
        text += f"Выберите что изменить:"

        buttons = [
            [InlineKeyboardButton(text="🎯 Изменить Target", callback_data="change_dest")],
            [InlineKeyboardButton(text="🌐 Изменить SNI", callback_data="change_sni")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=f"srv_manage_{server_name}")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="sni_cancel")]
        ]

        await callback.message.edit_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
        )
        await callback.answer()

    except Exception as e:
        logger.error(f"Ошибка при получении настроек inbound: {e}")
        await callback.answer(f"❌ Ошибка: {str(e)[:50]}", show_alert=True)


@router.callback_query(F.data == "change_dest")
async def start_change_dest(callback: CallbackQuery, state: FSMContext):
    """Начать изменение Target (Dest)"""
    data = await state.get_data()
    current_dest = data.get('manage_current_dest', '')
    remark = data.get('manage_inbound_remark', '')

    text = f"🎯 <b>ИЗМЕНЕНИЕ TARGET</b>\n\n"
    text += f"📍 Inbound: <b>{remark}</b>\n\n"
    text += f"Текущий Target: <code>{current_dest or 'не указан'}</code>\n\n"
    text += f"━━━━━━━━━━━━━━━━\n\n"
    text += f"📝 <b>Введите новый Target</b>\n\n"
    text += f"Формат: <code>домен:порт</code>\n\n"
    text += f"<b>Примеры:</b>\n"
    text += f"• <code>www.google.com:443</code>\n"
    text += f"• <code>ozon.ru:443</code>\n"
    text += f"• <code>m.vk.com:443</code>\n\n"
    text += f"<i>Или отправьте /cancel для отмены</i>"

    await state.set_state(ManageSNIStates.waiting_for_dest)
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


@router.message(ManageSNIStates.waiting_for_dest, F.text == "/cancel")
async def cancel_dest_edit(message: Message, state: FSMContext):
    """Отмена изменения Target"""
    await state.clear()
    await message.answer("❌ Изменение отменено.", reply_markup=Keyboards.admin_menu())


@router.message(ManageSNIStates.waiting_for_dest)
async def process_new_dest(message: Message, state: FSMContext):
    """Обработка нового Target"""
    import re

    new_dest = message.text.strip()

    # Валидация формата домен:порт
    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9.-]+:\d+$', new_dest):
        await message.answer(
            "❌ Неверный формат!\n\n"
            "Используйте формат: <code>домен:порт</code>\n"
            "Например: <code>ozon.ru:443</code>",
            parse_mode="HTML"
        )
        return

    data = await state.get_data()
    server_name = data.get('manage_server_name')
    is_local = data.get('manage_server_local', False)
    inbound_id = data.get('manage_inbound_id')
    current_sni = data.get('manage_current_sni', [])

    # Извлекаем домен из dest для SNI если SNI пустой
    domain = new_dest.split(':')[0]
    if not current_sni:
        current_sni = [domain]

    msg = await message.answer(f"⏳ Обновляю Target на {server_name}...")

    try:
        success = await update_inbound_reality_settings(
            server_name=server_name,
            is_local=is_local,
            inbound_id=inbound_id,
            new_dest=new_dest,
            new_sni=current_sni,
            server_config=data.get('manage_server_config', {})
        )

        if success:
            await msg.edit_text(
                f"✅ <b>Target успешно обновлён!</b>\n\n"
                f"🖥 Сервер: {server_name}\n"
                f"🎯 Новый Target: <code>{new_dest}</code>",
                parse_mode="HTML"
            )
        else:
            await msg.edit_text("❌ Не удалось обновить Target")

    except Exception as e:
        logger.error(f"Ошибка при обновлении Target: {e}")
        await msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")

    await state.clear()
    await message.answer("Панель администратора:", reply_markup=Keyboards.admin_menu())


@router.callback_query(F.data == "change_sni")
async def start_change_sni(callback: CallbackQuery, state: FSMContext):
    """Начать изменение SNI"""
    data = await state.get_data()
    current_sni = data.get('manage_current_sni', [])
    remark = data.get('manage_inbound_remark', '')

    text = f"🌐 <b>ИЗМЕНЕНИЕ SNI</b>\n\n"
    text += f"📍 Inbound: <b>{remark}</b>\n\n"
    text += f"<b>Текущие SNI:</b>\n"
    if current_sni:
        for sni in current_sni:
            text += f"   • <code>{sni}</code>\n"
    else:
        text += f"   <i>не указаны</i>\n"

    text += f"\n━━━━━━━━━━━━━━━━\n\n"
    text += f"📝 <b>Введите новые SNI домены</b>\n\n"
    text += f"Формат: домены через запятую или пробел\n\n"
    text += f"<b>Примеры:</b>\n"
    text += f"• <code>ozon.ru, www.ozon.ru</code>\n"
    text += f"• <code>m.vk.com vk.com</code>\n\n"
    text += f"<i>Или отправьте /cancel для отмены</i>"

    await state.set_state(ManageSNIStates.waiting_for_sni_domains)
    await callback.message.edit_text(text, parse_mode="HTML")
    await callback.answer()


async def update_inbound_reality_settings(
    server_name: str,
    is_local: bool,
    inbound_id: int,
    new_dest: str,
    new_sni: list,
    server_config: dict
) -> bool:
    """Обновить настройки Reality на сервере"""
    import json
    import subprocess

    try:
        if is_local:
            # Локальный сервер - обновляем через SQLite и API
            from bot.api.xui_client import XUIClient
            from bot.config import XUI_HOST, XUI_USERNAME, XUI_PASSWORD

            async with XUIClient(XUI_HOST, XUI_USERNAME, XUI_PASSWORD) as xui:
                success = await xui.update_reality_settings(
                    inbound_id=inbound_id,
                    dest=new_dest,
                    server_names=new_sni
                )

                if success:
                    # Перезапускаем x-ui
                    subprocess.run(['systemctl', 'restart', 'x-ui'], timeout=30, check=False)
                    await asyncio.sleep(2)

                return success
        else:
            # Удалённый сервер - через API панели
            from bot.api.remote_xui import _get_panel_opener
            import urllib.request
            import urllib.parse

            session = await _get_panel_opener(server_name)
            base_url = session.get('base_url', '')
            opener = session.get('opener')

            # Получаем текущий inbound
            get_url = f"{base_url}/panel/api/inbounds/get/{inbound_id}"
            get_req = urllib.request.Request(get_url)

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(None, opener.open, get_req)
            result = json.loads(response.read().decode())

            if not result.get('success'):
                return False

            inbound = result.get('obj', {})

            # Обновляем streamSettings
            stream = json.loads(inbound.get('streamSettings', '{}'))
            reality = stream.get('realitySettings', {})
            reality['dest'] = new_dest
            reality['serverNames'] = new_sni
            stream['realitySettings'] = reality
            inbound['streamSettings'] = json.dumps(stream)

            # Отправляем обновление
            update_url = f"{base_url}/panel/api/inbounds/update/{inbound_id}"
            update_data = {
                "id": inbound_id,
                "up": inbound.get('up', 0),
                "down": inbound.get('down', 0),
                "total": inbound.get('total', 0),
                "remark": inbound.get('remark', ''),
                "enable": inbound.get('enable', True),
                "expiryTime": inbound.get('expiryTime', 0),
                "listen": inbound.get('listen', ''),
                "port": inbound.get('port'),
                "protocol": inbound.get('protocol'),
                "settings": inbound.get('settings'),
                "streamSettings": inbound['streamSettings'],
                "sniffing": inbound.get('sniffing', '{}')
            }

            payload = json.dumps(update_data).encode()
            update_req = urllib.request.Request(
                update_url,
                data=payload,
                method='POST',
                headers={'Content-Type': 'application/json'}
            )

            resp = await loop.run_in_executor(None, opener.open, update_req)
            update_result = json.loads(resp.read().decode())

            if update_result.get('success'):
                logger.info(f"Reality настройки обновлены на {server_name}: dest={new_dest}, sni={new_sni}")
                return True
            else:
                logger.error(f"Ошибка обновления на {server_name}: {update_result.get('msg')}")
                return False

    except Exception as e:
        logger.error(f"Ошибка при обновлении настроек на {server_name}: {e}")
        return False


@router.callback_query(F.data.startswith("sni_inbound_"))
async def select_inbound_for_sni(callback: CallbackQuery, state: FSMContext):
    """Выбор inbound-а для изменения SNI"""
    from bot.api.xui_client import XUIClient
    from bot.config import XUI_HOST, XUI_USERNAME, XUI_PASSWORD
    import json

    inbound_id = int(callback.data.replace("sni_inbound_", ""))

    try:
        # Получаем данные inbound-а
        async with XUIClient(XUI_HOST, XUI_USERNAME, XUI_PASSWORD) as xui:
            inbound = await xui.get_inbound(inbound_id)

            if not inbound:
                await callback.message.edit_text("❌ Inbound не найден")
                await callback.answer()
                return

            # Парсим настройки
            stream_settings = json.loads(inbound.get('streamSettings', '{}'))
            reality_settings = stream_settings.get('realitySettings', {})
            server_names = reality_settings.get('serverNames', [])
            dest = reality_settings.get('dest', 'не указан')

            remark = inbound.get('remark', f'Inbound {inbound_id}')
            port = inbound.get('port', '?')

            # Сохраняем данные в состояние
            await state.update_data(
                inbound_id=inbound_id,
                inbound_remark=remark,
                current_dest=dest,
                current_sni=server_names
            )
            await state.set_state(ManageSNIStates.waiting_for_sni_domains)

            text = f"🌐 <b>ИЗМЕНЕНИЕ SNI АДРЕСОВ</b>\n\n"
            text += f"📍 <b>Inbound:</b> {remark} (ID: {inbound_id}, Port: {port}→443)\n"
            text += f"🎯 <b>Dest:</b> <code>{dest}</code>\n\n"
            text += f"━━━━━━━━━━━━━━━━\n\n"
            text += f"<b>Текущие SNI домены:</b>\n"

            if server_names:
                for idx, sni in enumerate(server_names, 1):
                    text += f"  {idx}. <code>{sni}</code>\n"
            else:
                text += "  <i>Не указаны</i>\n"

            text += f"\n━━━━━━━━━━━━━━━━\n\n"
            text += f"📝 <b>Введите новые SNI домены</b>\n\n"
            text += f"Формат: домены через запятую или пробел\n\n"
            text += f"<b>Примеры:</b>\n"
            text += f"• <code>vk.com, www.vk.com, m.vk.com</code>\n"
            text += f"• <code>mirror.yandex.ru www.mirror.yandex.ru ftp.yandex.ru</code>\n\n"
            text += f"<i>Или отправьте /cancel для отмены</i>"

            await callback.message.edit_text(text, parse_mode="HTML")
            await callback.answer()

    except Exception as e:
        logger.error(f"Ошибка при получении данных inbound: {e}")
        await callback.message.edit_text(f"❌ Ошибка: {str(e)}")
        await callback.answer()


@router.message(ManageSNIStates.waiting_for_sni_domains, F.text == "/cancel")
async def cancel_sni_edit(message: Message, state: FSMContext):
    """Отмена изменения SNI"""
    await state.clear()
    await message.answer(
        "❌ Изменение SNI адресов отменено.",
        reply_markup=Keyboards.admin_menu()
    )


@router.message(ManageSNIStates.waiting_for_sni_domains)
async def process_new_sni_domains(message: Message, state: FSMContext, **kwargs):
    """Обработка новых SNI доменов (универсальный для всех серверов)"""
    import re

    # Получаем данные из состояния
    data = await state.get_data()

    # Проверяем откуда пришли - от нового интерфейса или старого
    inbound_id = data.get('manage_inbound_id') or data.get('inbound_id')
    inbound_remark = data.get('manage_inbound_remark') or data.get('inbound_remark', '')
    current_dest = data.get('manage_current_dest') or data.get('current_dest', '')
    current_sni = data.get('manage_current_sni') or data.get('current_sni', [])
    server_name = data.get('manage_server_name', 'Local')
    is_local = data.get('manage_server_local', True)
    server_config = data.get('manage_server_config', {})

    if not inbound_id:
        await message.answer("❌ Ошибка: данные inbound не найдены")
        await state.clear()
        return

    # Парсим введенные домены
    input_text = message.text.strip()

    # Разделяем по запятым или пробелам
    domains = re.split(r'[,\s]+', input_text)
    # Убираем пустые строки и дубликаты
    domains = list(dict.fromkeys([d.strip() for d in domains if d.strip()]))

    if not domains:
        await message.answer("❌ Не указаны домены. Попробуйте еще раз или отправьте /cancel")
        return

    # Валидация доменов
    domain_pattern = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9]?(\.[a-zA-Z0-9][a-zA-Z0-9-]{0,61}[a-zA-Z0-9]?)*$')
    invalid_domains = [d for d in domains if not domain_pattern.match(d)]

    if invalid_domains:
        await message.answer(
            f"❌ Некорректные домены:\n" +
            "\n".join(f"  • {d}" for d in invalid_domains) +
            "\n\nПопробуйте еще раз или отправьте /cancel"
        )
        return

    msg = await message.answer(f"⏳ Обновляю SNI на {server_name}...")

    try:
        success = await update_inbound_reality_settings(
            server_name=server_name,
            is_local=is_local,
            inbound_id=inbound_id,
            new_dest=current_dest,
            new_sni=domains,
            server_config=server_config
        )

        if success:
            await msg.edit_text(
                f"✅ <b>SNI успешно обновлены!</b>\n\n"
                f"🖥 Сервер: {server_name}\n"
                f"📍 Inbound: {inbound_remark}\n"
                f"🌐 Новые SNI:\n" +
                "\n".join(f"   • <code>{d}</code>" for d in domains),
                parse_mode="HTML"
            )
        else:
            await msg.edit_text("❌ Не удалось обновить SNI")

    except Exception as e:
        logger.error(f"Ошибка при обновлении SNI: {e}")
        await msg.edit_text(f"❌ Ошибка: {str(e)[:100]}")

    await state.clear()
    await message.answer("Панель администратора:", reply_markup=Keyboards.admin_menu())


@router.callback_query(F.data == "sni_cancel")
async def cancel_sni_management(callback: CallbackQuery, state: FSMContext):
    """Отмена управления настройками"""
    await state.clear()
    await callback.message.delete()
    await callback.answer("Отменено")
    await callback.message.answer("Панель администратора:", reply_markup=Keyboards.admin_menu())


# ===== ПОИСК КЛЮЧЕЙ =====

async def search_clients_on_servers(query: str) -> list:
    """Поиск клиентов по email/имени на всех X-UI серверах"""
    import json
    import subprocess
    from pathlib import Path
    from datetime import datetime

    results = []
    query_lower = query.lower()

    # Загружаем конфиг серверов
    servers_file = Path(__file__).parent.parent.parent / 'servers_config.json'
    if not servers_file.exists():
        return results

    with open(servers_file, 'r') as f:
        config = json.load(f)

    # Определяем имя локального сервера из конфига
    local_server_name = 'Local'
    for server in config.get('servers', []):
        if server.get('local', False):
            local_server_name = server.get('name', 'Local')
            break

    # Поиск на локальном сервере
    try:
        import sqlite3
        conn = sqlite3.connect('/etc/x-ui/x-ui.db')
        cursor = conn.cursor()
        cursor.execute("SELECT id, settings FROM inbounds WHERE enable=1")
        rows = cursor.fetchall()
        conn.close()

        for inbound_id, settings_str in rows:
            try:
                settings = json.loads(settings_str)
                for client in settings.get('clients', []):
                    email = client.get('email', '')
                    if query_lower in email.lower():
                        expiry_time = client.get('expiryTime', 0)
                        if expiry_time > 0:
                            expiry_dt = datetime.fromtimestamp(expiry_time / 1000)
                            expiry_str = expiry_dt.strftime("%d.%m.%Y")
                        else:
                            expiry_str = "Безлимит"

                        results.append({
                            'email': email,
                            'uuid': client.get('id', ''),
                            'server': local_server_name,
                            'inbound_id': inbound_id,
                            'expiry_time': expiry_time,
                            'expiry_str': expiry_str,
                            'limit_ip': client.get('limitIp', 2)
                        })
            except:
                continue
    except Exception as e:
        logger.error(f"Ошибка поиска на локальном сервере: {e}")

    # Поиск на удалённых серверах
    for server in config.get('servers', []):
        if server.get('local') or not server.get('enabled', True):
            continue

        server_name = server.get('name', server.get('ip', 'Unknown'))

        # Попробуем через API панели (если есть)
        panel_config = server.get('panel', {})
        if panel_config:
            try:
                from bot.api.remote_xui import _get_panel_opener, _panel_login
                import asyncio
                import ssl
                import urllib.request
                import http.cookiejar

                ip = server.get('ip', '')
                port = panel_config.get('port', 1020)
                path = panel_config.get('path', '')
                username = panel_config.get('username', '')
                password = panel_config.get('password', '')

                if ip and username and password:
                    # Создаём opener для HTTPS без проверки сертификата
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE

                    cookie_jar = http.cookiejar.CookieJar()
                    opener = urllib.request.build_opener(
                        urllib.request.HTTPCookieProcessor(cookie_jar),
                        urllib.request.HTTPSHandler(context=ctx)
                    )

                    base_url = f"https://{ip}:{port}{path}"

                    # Логин
                    import urllib.parse
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

                    resp = opener.open(login_req, timeout=10)
                    login_result = json.loads(resp.read())

                    if login_result.get('success'):
                        # Получаем список inbounds
                        list_req = urllib.request.Request(f"{base_url}/panel/api/inbounds/list")
                        resp = opener.open(list_req, timeout=10)
                        data = json.loads(resp.read())

                        if data.get('success'):
                            for inbound in data.get('obj', []):
                                settings_str = inbound.get('settings', '{}')
                                try:
                                    settings = json.loads(settings_str)
                                    for client in settings.get('clients', []):
                                        email = client.get('email', '')
                                        if query_lower in email.lower():
                                            expiry_time = client.get('expiryTime', 0)
                                            if expiry_time > 0:
                                                expiry_dt = datetime.fromtimestamp(expiry_time / 1000)
                                                expiry_str = expiry_dt.strftime("%d.%m.%Y")
                                            else:
                                                expiry_str = "Безлимит"

                                            results.append({
                                                'email': email,
                                                'uuid': client.get('id', ''),
                                                'server': server_name,
                                                'inbound_id': inbound.get('id'),
                                                'expiry_time': expiry_time,
                                                'expiry_str': expiry_str,
                                                'limit_ip': client.get('limitIp', 2)
                                            })
                                except:
                                    continue
                        continue  # Переходим к следующему серверу
            except Exception as e:
                logger.error(f"Ошибка поиска через API панели {server_name}: {e}")

        # Если нет панели или ошибка - пробуем через SSH
        ssh_config = server.get('ssh', {})
        if not ssh_config.get('password') or not server.get('ip'):
            continue

        try:
            cmd = f"sshpass -p '{ssh_config['password']}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 {ssh_config.get('user', 'root')}@{server['ip']} \"sqlite3 /etc/x-ui/x-ui.db 'SELECT settings FROM inbounds WHERE enable=1'\""
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)

            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().split('\n'):
                    try:
                        settings = json.loads(line)
                        for client in settings.get('clients', []):
                            email = client.get('email', '')
                            if query_lower in email.lower():
                                expiry_time = client.get('expiryTime', 0)
                                if expiry_time > 0:
                                    expiry_dt = datetime.fromtimestamp(expiry_time / 1000)
                                    expiry_str = expiry_dt.strftime("%d.%m.%Y")
                                else:
                                    expiry_str = "Безлимит"

                                results.append({
                                    'email': email,
                                    'uuid': client.get('id', ''),
                                    'server': server_name,
                                    'expiry_time': expiry_time,
                                    'expiry_str': expiry_str,
                                    'limit_ip': client.get('limitIp', 2)
                                })
                    except:
                        continue
        except Exception as e:
            logger.error(f"Ошибка поиска на сервере {server_name}: {e}")

    return results


@router.message(F.text == "🔍 Поиск ключа")
@admin_only
async def start_search_key(message: Message, state: FSMContext, **kwargs):
    """Начало поиска ключа"""
    await state.set_state(SearchKeyStates.waiting_for_search_query)
    await message.answer(
        "🔍 <b>ПОИСК КЛЮЧА</b>\n\n"
        "Введите номер телефона или имя клиента для поиска.\n\n"
        "Примеры:\n"
        "• <code>+79001234567</code>\n"
        "• <code>9001234567</code>\n"
        "• <code>Иван</code>\n\n"
        "Или нажмите 'Отмена' для возврата.",
        parse_mode="HTML",
        reply_markup=Keyboards.cancel()
    )


@router.message(SearchKeyStates.waiting_for_search_query, F.text == "Отмена")
async def cancel_search_key(message: Message, state: FSMContext):
    """Отмена поиска"""
    await state.clear()
    await message.answer(
        "Поиск отменен.",
        reply_markup=Keyboards.admin_menu()
    )


@router.message(SearchKeyStates.waiting_for_search_query)
async def process_search_query(message: Message, state: FSMContext, db: DatabaseManager, **kwargs):
    """Обработка поискового запроса - ищет в базе и на X-UI серверах"""
    query = message.text.strip()

    # Если пользователь нажал кнопку меню - выходим из режима поиска
    admin_menu_buttons = {
        "📡 Добавить сервер", "📡 Сервер → всем", "🔑 Создать ключ (выбор inbound)",
        "Добавить менеджера", "Список менеджеров", "Общая статистика",
        "Детальная статистика", "💰 Изменить цены", "🔍 Поиск ключа",
        "📅 Продлить подписку",
        "🗑️ Удалить ключ", "📢 Отправить уведомление", "🌐 Управление SNI",
        "💳 Реквизиты", "📋 Веб-заказы", "🖥 Статус серверов", "🔧 Панели X-UI", "💳 Оплата серверов",
        "🌐 Админ-панель сайта",
        "Назад", "Панель администратора", "Создать ключ", "🔄 Замена ключа",
        "🔧 Исправить ключ", "💰 Прайс", "Моя статистика",
    }
    if query in admin_menu_buttons:
        await state.clear()
        await message.answer(
            "🔍 Поиск отменен.",
            reply_markup=Keyboards.admin_menu()
        )
        return

    if len(query) < 2:
        await message.answer("❌ Введите минимум 2 символа для поиска.")
        return

    status_msg = await message.answer("🔍 Поиск...")

    # Ищем ключи в локальной базе
    keys = await db.search_keys(query)

    # Также ищем на X-UI серверах
    xui_clients = await search_clients_on_servers(query)

    if not keys and not xui_clients:
        await status_msg.edit_text(
            f"🔍 По запросу «<b>{query}</b>» ничего не найдено.\n\n"
            "Попробуйте другой запрос или нажмите 'Отмена' для выхода.",
            parse_mode="HTML"
        )
        return

    await state.clear()

    text = f"🔍 <b>РЕЗУЛЬТАТЫ ПОИСКА</b>\n"
    text += f"Запрос: «{query}»\n\n"

    buttons = []
    idx = 0

    # Показываем клиентов с X-UI серверов
    if xui_clients:
        text += f"<b>📡 На серверах X-UI:</b> {len(xui_clients)}\n"
        text += "━━━━━━━━━━━━━━━━\n\n"

        for client in xui_clients[:15]:
            idx += 1
            email = client.get('email', 'N/A')
            server = client.get('server', 'Unknown')
            expiry = client.get('expiry_str', 'N/A')
            uuid_short = client.get('uuid', '')[:8] + '...' if client.get('uuid') else 'N/A'

            sub_url = f"https://{_get_sub_domain(kwargs)}/sub/{client.get('uuid', '')}" if client.get('uuid') else ''

            text += f"{idx}. <b>{email}</b>\n"
            text += f"   🖥 Сервер: {server}\n"
            text += f"   ⏰ Истекает: {expiry}\n"
            text += f"   🔑 UUID: <code>{uuid_short}</code>\n"
            if sub_url:
                text += f"   📱 Подписка: <code>{sub_url}</code>\n"
            text += "\n"

            # Кнопки для клиента: ссылка и продление
            # Формат: exts_{server}_{uuid} - продление на конкретном сервере
            # exts_ = 5, server = ~10, _ = 1, uuid = 36 = ~52 символов (лимит 64)
            if client.get('uuid'):
                server_short = server[:10]  # Ограничиваем имя сервера
                buttons.append([
                    InlineKeyboardButton(
                        text=f"🔗 {email[:15]}",
                        callback_data=f"get_link_{client['uuid']}"
                    ),
                    InlineKeyboardButton(
                        text=f"📅 {server_short}",
                        callback_data=f"exts_{server_short}_{client['uuid']}"
                    )
                ])

            if len(text) > 2500:
                text += "\n<i>... показаны первые результаты</i>\n"
                break

    # Показываем из локальной базы
    if keys:
        text += f"\n<b>📋 В базе бота:</b> {len(keys)}\n"
        text += "━━━━━━━━━━━━━━━━\n\n"

        for key in keys[:10]:
            idx += 1
            custom_name = key.get('custom_name', '') or ''
            full_name = key.get('full_name', '') or ''
            username = key.get('username', '') or ''

            if custom_name:
                manager_name = custom_name
            elif full_name:
                manager_name = full_name
            elif username:
                manager_name = f"@{username}"
            else:
                manager_name = f"ID: {key['manager_id']}"

            created_at = key['created_at'][:16].replace('T', ' ')
            price = key.get('price', 0) or 0
            price_status = f"💰 {price} ₽" if price > 0 else "🎁 Бесплатно"

            text += f"{idx}. <b>{key['phone_number']}</b>\n"
            text += f"   👤 Менеджер: {manager_name}\n"
            text += f"   📅 Срок: {key['period']}\n"
            text += f"   {price_status}\n"
            text += f"   🕒 Создан: {created_at}\n\n"

            buttons.append([
                InlineKeyboardButton(
                    text=f"🗑️ {key['phone_number'][:15]}",
                    callback_data=f"del_key_{key['id']}"
                )
            ])

            if len(text) > 3800:
                text += "\n<i>... показаны первые результаты</i>"
                break

    # Добавляем кнопки
    buttons.append([InlineKeyboardButton(text="🔍 Новый поиск", callback_data="new_search")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="cancel_key_delete")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await status_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "new_search")
async def new_search(callback: CallbackQuery, state: FSMContext):
    """Начать новый поиск"""
    await state.set_state(SearchKeyStates.waiting_for_search_query)
    await callback.message.edit_text(
        "🔍 <b>ПОИСК КЛЮЧА</b>\n\n"
        "Введите номер телефона или имя клиента для поиска.\n\n"
        "Примеры:\n"
        "• <code>+79001234567</code>\n"
        "• <code>9001234567</code>\n"
        "• <code>Иван</code>",
        parse_mode="HTML"
    )
    await callback.answer()


# ==================== ПРОДЛЕНИЕ ПОДПИСКИ ====================

@router.message(F.text == "📅 Продлить подписку")
@admin_only
async def start_extend_subscription(message: Message, state: FSMContext, **kwargs):
    """Начало продления подписки клиента"""
    await state.clear()
    await state.set_state(ExtendSubscriptionStates.waiting_for_search)
    await message.answer(
        "📅 <b>ПРОДЛЕНИЕ ПОДПИСКИ</b>\n\n"
        "Введите email, UUID или телефон клиента для поиска.\n\n"
        "Подписка будет продлена на <b>всех серверах</b> клиента.\n\n"
        "Или нажмите 'Отмена' для возврата.",
        parse_mode="HTML",
        reply_markup=Keyboards.cancel()
    )


@router.message(ExtendSubscriptionStates.waiting_for_search, F.text == "Отмена")
async def cancel_extend_subscription(message: Message, state: FSMContext):
    """Отмена продления"""
    await state.clear()
    await message.answer("Операция отменена.", reply_markup=Keyboards.admin_menu())


@router.message(ExtendSubscriptionStates.waiting_for_search)
async def process_extend_search(message: Message, state: FSMContext):
    """Поиск клиента для продления подписки"""
    from datetime import datetime

    query = message.text.strip()

    # Если пользователь нажал кнопку меню - выходим
    admin_menu_buttons = {
        "📡 Добавить сервер", "🔑 Создать ключ (выбор inbound)",
        "Добавить менеджера", "Список менеджеров", "Общая статистика",
        "Детальная статистика", "💰 Изменить цены", "🔍 Поиск ключа",
        "🗑️ Удалить ключ", "📢 Отправить уведомление", "🌐 Управление SNI",
        "💳 Реквизиты", "📋 Веб-заказы", "🖥 Статус серверов", "🔧 Панели X-UI",
        "💳 Оплата серверов", "🌐 Админ-панель сайта", "📅 Продлить подписку",
        "Назад", "Панель администратора", "Создать ключ", "🔄 Замена ключа",
        "🔧 Исправить ключ", "💰 Прайс", "Моя статистика",
    }
    if query in admin_menu_buttons:
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=Keyboards.admin_menu())
        return

    if len(query) < 2:
        await message.answer("❌ Введите минимум 2 символа для поиска.")
        return

    status_msg = await message.answer("🔍 Поиск клиента на серверах...")

    xui_clients = await search_clients_on_servers(query)

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
        clients_by_uuid[uuid]['servers'].append(client.get('server', 'Unknown'))

    unique_clients = list(clients_by_uuid.values())

    if not unique_clients:
        await status_msg.edit_text(
            f"🔍 По запросу «<b>{query}</b>» ничего не найдено.",
            parse_mode="HTML"
        )
        return

    await state.clear()

    text = f"📅 <b>ПРОДЛЕНИЕ ПОДПИСКИ</b>\n"
    text += f"Запрос: «{query}»\n\n"

    buttons = []

    for idx, client in enumerate(unique_clients[:10]):
        email = client['email']
        uuid_val = client['uuid']
        servers_str = ', '.join(client['servers'])
        expiry_time = client.get('expiry_time', 0)

        if expiry_time > 0:
            expiry_dt = datetime.fromtimestamp(expiry_time / 1000)
            expiry_str = expiry_dt.strftime("%d.%m.%Y")
            now_ms = int(datetime.now().timestamp() * 1000)
            if expiry_time < now_ms:
                expiry_str += " ❌ истекла"
        else:
            expiry_str = "Безлимит"

        text += f"{idx + 1}. <b>{email}</b>\n"
        text += f"   🖥 Серверы: {servers_str}\n"
        text += f"   ⏰ До: {expiry_str}\n\n"

        # Кнопка продления на ВСЕХ серверах
        buttons.append([InlineKeyboardButton(
            text=f"📅 Продлить: {email[:30]}",
            callback_data=f"extall_{uuid_val}"
        )])

        if len(text) > 3000:
            text += "<i>... показаны первые результаты</i>\n"
            break

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_key_delete")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await status_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("extall_"))
async def extend_all_servers_select_period(callback: CallbackQuery):
    """Выбор периода продления на ВСЕХ серверах"""
    from bot.api.remote_xui import find_client_presence_on_all_servers
    from datetime import datetime

    client_uuid = callback.data.replace("extall_", "")
    uuid_short = client_uuid[:8] + "..."

    await callback.answer("⏳ Проверяю серверы...")
    await callback.message.edit_text("🔍 Проверяю серверы клиента...")

    # Находим клиента на всех серверах
    presence = await find_client_presence_on_all_servers(client_uuid)
    found_on = presence.get('found_on', [])

    if not found_on:
        await callback.message.edit_text(
            f"❌ Клиент <code>{uuid_short}</code> не найден ни на одном сервере.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="new_search")]
            ])
        )
        return

    # Показываем информацию о клиенте и его серверах
    email = found_on[0].get('email', uuid_short)
    text = f"📅 <b>ПРОДЛЕНИЕ ПОДПИСКИ</b>\n\n"
    text += f"👤 Клиент: <code>{email}</code>\n"
    text += f"🔑 UUID: <code>{uuid_short}</code>\n\n"
    text += f"<b>Серверы ({len(found_on)}):</b>\n"

    for srv in found_on:
        exp = srv.get('expiry_time', 0)
        if exp > 0:
            exp_str = datetime.fromtimestamp(exp / 1000).strftime("%d.%m.%Y")
        else:
            exp_str = "Безлимит"
        prefix = srv.get('name_prefix', '')
        label = srv['server_name']
        if prefix and prefix != label:
            label = f"{label} [{prefix}]"
        text += f"  • {label} — до {exp_str}\n"

    text += f"\n📌 Подписка будет продлена на <b>всех серверах</b>.\n"
    text += "Выберите период:"

    # Кнопки выбора периода
    buttons = [
        [
            InlineKeyboardButton(text="1 мес", callback_data=f"doextall_{client_uuid}_30"),
            InlineKeyboardButton(text="3 мес", callback_data=f"doextall_{client_uuid}_90"),
        ],
        [
            InlineKeyboardButton(text="6 мес", callback_data=f"doextall_{client_uuid}_180"),
            InlineKeyboardButton(text="1 год", callback_data=f"doextall_{client_uuid}_365"),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="cancel_key_delete")]
    ]

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("doextall_"))
async def do_extend_all_servers(callback: CallbackQuery):
    """Выполнить продление подписки на всех серверах"""
    from bot.api.remote_xui import extend_client_on_all_servers
    from bot.integration import get_services
    from datetime import datetime

    # Формат: doextall_{uuid}_{days}
    parts = callback.data.replace("doextall_", "").rsplit("_", 1)
    if len(parts) != 2:
        await callback.answer("❌ Ошибка формата данных", show_alert=True)
        return

    client_uuid = parts[0]
    try:
        extend_days = int(parts[1])
    except ValueError:
        await callback.answer("❌ Ошибка: неверное количество дней", show_alert=True)
        return

    await callback.answer("⏳ Продлеваю подписку на всех серверах...")
    await callback.message.edit_text("⏳ Продление подписки на всех серверах...")

    # Продлеваем на всех серверах через API панелей
    result = await extend_client_on_all_servers(client_uuid, extend_days)

    if result.get('success'):
        new_expiry_ms = result.get('new_expiry', 0)
        if new_expiry_ms:
            new_expiry_date = datetime.fromtimestamp(new_expiry_ms / 1000).strftime('%d.%m.%Y %H:%M')
        else:
            new_expiry_date = "неизвестно"

        # Обновляем локальную БД (clients и client_servers)
        try:
            services = get_services()
            if services and services.client_manager:
                # Ищем клиента в локальной БД по UUID
                client_data = await services.client_manager.get_client(uuid=client_uuid)
                if client_data:
                    await services.client_manager.extend_subscription(
                        client_id=client_data['id'],
                        days=extend_days,
                        manager_id=callback.from_user.id
                    )
                    logger.info(f"Локальная БД обновлена: клиент {client_uuid[:8]}... продлён на {extend_days} дней")
        except Exception as e:
            logger.warning(f"Не удалось обновить локальную БД: {e}")

        # Формируем отчёт по серверам
        results_text = ""
        success_count = 0
        fail_count = 0
        for server_name, success in result.get('results', {}).items():
            if success:
                results_text += f"  ✅ {server_name}\n"
                success_count += 1
            else:
                results_text += f"  ❌ {server_name}\n"
                fail_count += 1

        period_text = {30: "1 месяц", 90: "3 месяца", 180: "6 месяцев", 365: "1 год"}.get(extend_days, f"{extend_days} дней")

        await callback.message.edit_text(
            f"✅ <b>ПОДПИСКА ПРОДЛЕНА</b>\n\n"
            f"🔑 UUID: <code>{client_uuid[:8]}...</code>\n"
            f"📅 Период: +{period_text}\n"
            f"⏰ Новый срок: <b>{new_expiry_date}</b>\n\n"
            f"<b>Серверы:</b> ✅ {success_count}"
            + (f" / ❌ {fail_count}" if fail_count else "") + f"\n{results_text}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📅 Продлить ещё", callback_data=f"extall_{client_uuid}")],
                [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="new_search")],
                [InlineKeyboardButton(text="◀️ В меню", callback_data="cancel_key_delete")]
            ])
        )
    else:
        await callback.message.edit_text(
            f"❌ <b>Ошибка продления</b>\n\n"
            f"Не удалось продлить подписку на серверах.\n"
            f"UUID: <code>{client_uuid[:8]}...</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data=f"extall_{client_uuid}")],
                [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="new_search")]
            ])
        )


@router.callback_query(F.data.startswith("exts_"))
async def extend_on_server_callback(callback: CallbackQuery):
    """Показать меню выбора периода продления на конкретном сервере"""
    # Формат: exts_{server}_{uuid}
    data = callback.data.replace("exts_", "")
    # Ищем первый _ после имени сервера (UUID содержит дефисы, не подчёркивания)
    parts = data.split("_", 1)
    if len(parts) != 2:
        await callback.answer("❌ Ошибка формата данных", show_alert=True)
        return

    server_name = parts[0]
    client_uuid = parts[1]
    uuid_short = client_uuid[:8] + "..."

    # Кнопки выбора периода - формат: dexts_{server}_{uuid}_{days}
    buttons = [
        [
            InlineKeyboardButton(text="1 мес", callback_data=f"dexts_{server_name}_{client_uuid}_30"),
            InlineKeyboardButton(text="3 мес", callback_data=f"dexts_{server_name}_{client_uuid}_90"),
        ],
        [
            InlineKeyboardButton(text="6 мес", callback_data=f"dexts_{server_name}_{client_uuid}_180"),
            InlineKeyboardButton(text="1 год", callback_data=f"dexts_{server_name}_{client_uuid}_365"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="new_search")
        ]
    ]

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(
        f"📅 <b>ПРОДЛЕНИЕ КЛЮЧА</b>\n\n"
        f"🖥 Сервер: <b>{server_name}</b>\n"
        f"🔑 UUID: <code>{uuid_short}</code>\n\n"
        f"Выберите период продления:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback.answer()


@router.callback_query(F.data.startswith("dexts_"))
async def do_extend_on_server_callback(callback: CallbackQuery):
    """Выполнить продление ключа на конкретном сервере"""
    from bot.api.remote_xui import extend_client_on_server, load_servers_config
    from datetime import datetime

    # Формат: dexts_{server}_{uuid}_{days}
    data = callback.data.replace("dexts_", "")
    # Парсим: сервер_uuid_дни (дни в конце после последнего _)
    parts = data.rsplit("_", 1)
    if len(parts) != 2:
        await callback.answer("❌ Ошибка формата данных", show_alert=True)
        return

    try:
        extend_days = int(parts[1])
    except ValueError:
        await callback.answer("❌ Ошибка: неверное количество дней", show_alert=True)
        return

    # Парсим сервер и UUID
    server_uuid = parts[0]
    server_parts = server_uuid.split("_", 1)
    if len(server_parts) != 2:
        await callback.answer("❌ Ошибка формата данных", show_alert=True)
        return

    server_name = server_parts[0]
    client_uuid = server_parts[1]

    await callback.answer(f"⏳ Продлеваю ключ на {server_name}...")

    # Продлеваем на указанном сервере
    result = await extend_client_on_server(server_name, client_uuid, extend_days)

    if result.get('success'):
        new_expiry_ms = result.get('new_expiry', 0)
        if new_expiry_ms:
            new_expiry_date = datetime.fromtimestamp(new_expiry_ms / 1000).strftime('%d.%m.%Y %H:%M')
        else:
            new_expiry_date = "неизвестно"

        # Обновляем локальную БД (clients и client_servers)
        try:
            from bot.integration import get_services
            services = get_services()
            if services and services.client_manager:
                client_data = await services.client_manager.get_client(uuid=client_uuid)
                if client_data:
                    await services.client_manager.extend_subscription(
                        client_id=client_data['id'],
                        days=extend_days,
                        manager_id=callback.from_user.id
                    )
                    logger.info(f"Локальная БД обновлена: клиент {client_uuid[:8]}... продлён на {extend_days} дней на {server_name}")
        except Exception as e:
            logger.warning(f"Не удалось обновить локальную БД: {e}")

        period_text = {30: "1 месяц", 90: "3 месяца", 180: "6 месяцев", 365: "1 год"}.get(extend_days, f"{extend_days} дней")

        await callback.message.edit_text(
            f"✅ <b>Ключ успешно продлён!</b>\n\n"
            f"🖥 Сервер: <b>{server_name}</b>\n"
            f"🔑 UUID: <code>{client_uuid[:8]}...</code>\n"
            f"📅 Период: +{period_text}\n"
            f"⏰ Новый срок: <b>{new_expiry_date}</b>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="new_search")],
                [InlineKeyboardButton(text="◀️ В меню", callback_data="cancel_key_delete")]
            ])
        )
    else:
        error_msg = result.get('error', 'Неизвестная ошибка')
        await callback.message.edit_text(
            f"❌ <b>Ошибка продления</b>\n\n"
            f"🖥 Сервер: {server_name}\n"
            f"🔑 UUID: <code>{client_uuid[:8]}...</code>\n\n"
            f"Причина: {error_msg}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data=f"exts_{server_name}_{client_uuid}")],
                [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="new_search")]
            ])
        )


@router.callback_query(F.data.startswith("extend_"))
async def extend_client_callback(callback: CallbackQuery):
    """Показать меню выбора периода продления (старый формат - на всех серверах)"""
    uuid_prefix = callback.data.replace("extend_", "")

    # Кнопки выбора периода
    buttons = [
        [
            InlineKeyboardButton(text="1 мес", callback_data=f"do_extend_{uuid_prefix}_30"),
            InlineKeyboardButton(text="3 мес", callback_data=f"do_extend_{uuid_prefix}_90"),
        ],
        [
            InlineKeyboardButton(text="6 мес", callback_data=f"do_extend_{uuid_prefix}_180"),
            InlineKeyboardButton(text="1 год", callback_data=f"do_extend_{uuid_prefix}_365"),
        ],
        [
            InlineKeyboardButton(text="◀️ Назад", callback_data="new_search")
        ]
    ]

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(
        f"📅 <b>ПРОДЛЕНИЕ КЛЮЧА</b>\n\n"
        f"🔑 UUID: <code>{uuid_prefix}...</code>\n\n"
        f"Выберите период продления:",
        parse_mode="HTML",
        reply_markup=keyboard
    )
    await callback.answer()


@router.callback_query(F.data.startswith("do_extend_"))
async def do_extend_client_callback(callback: CallbackQuery):
    """Выполнить продление ключа"""
    from bot.api.remote_xui import extend_client_on_all_servers, load_servers_config
    import json
    from datetime import datetime

    # Парсим данные: do_extend_{uuid}_{days}
    parts = callback.data.replace("do_extend_", "").rsplit("_", 1)
    if len(parts) != 2:
        await callback.answer("❌ Ошибка: неверный формат данных", show_alert=True)
        return

    uuid_prefix = parts[0]
    try:
        extend_days = int(parts[1])
    except ValueError:
        await callback.answer("❌ Ошибка: неверное количество дней", show_alert=True)
        return

    await callback.answer("⏳ Продлеваю ключ на всех серверах...")

    # Ищем полный UUID по префиксу
    config = load_servers_config()
    full_uuid = None
    client_email = None

    for server in config.get('servers', []):
        if not server.get('enabled', True):
            continue

        if server.get('local', False):
            # Ищем в локальной базе
            import sqlite3
            try:
                conn = sqlite3.connect('/etc/x-ui/x-ui.db')
                cursor = conn.cursor()
                cursor.execute("SELECT settings FROM inbounds")
                for (settings_str,) in cursor.fetchall():
                    try:
                        settings = json.loads(settings_str)
                        for client in settings.get('clients', []):
                            if client.get('id', '').startswith(uuid_prefix):
                                full_uuid = client.get('id')
                                client_email = client.get('email', '')
                                break
                    except:
                        continue
                    if full_uuid:
                        break
                conn.close()
            except:
                pass
        else:
            # Ищем через API панели
            from bot.api.remote_xui import _get_panel_opener, _panel_login
            import urllib.request

            panel = server.get('panel', {})
            if not panel:
                continue

            server_name = server.get('name', 'Unknown')
            session = await _get_panel_opener(server_name)

            if not session.get('logged_in'):
                import asyncio
                loop = asyncio.get_event_loop()
                logged_in = await _panel_login(server)
                if not logged_in:
                    continue

            base_url = session.get('base_url', '')
            opener = session.get('opener')

            try:
                list_url = f"{base_url}/panel/api/inbounds/list"
                list_req = urllib.request.Request(list_url)

                import asyncio
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(None, opener.open, list_req)
                data = json.loads(response.read().decode())

                if data.get('success'):
                    for inbound in data.get('obj', []):
                        settings_str = inbound.get('settings', '{}')
                        try:
                            settings = json.loads(settings_str)
                            for client in settings.get('clients', []):
                                if client.get('id', '').startswith(uuid_prefix):
                                    full_uuid = client.get('id')
                                    client_email = client.get('email', '')
                                    break
                        except:
                            continue
                        if full_uuid:
                            break
            except:
                pass

        if full_uuid:
            break

    if not full_uuid:
        await callback.message.edit_text(
            "❌ <b>Ошибка</b>\n\n"
            f"Клиент с UUID <code>{uuid_prefix}...</code> не найден на серверах.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="new_search")]
            ])
        )
        return

    # Продлеваем на всех серверах
    result = await extend_client_on_all_servers(full_uuid, extend_days)

    if result.get('success'):
        # Форматируем новую дату истечения
        new_expiry_ms = result.get('new_expiry', 0)
        if new_expiry_ms:
            new_expiry_date = datetime.fromtimestamp(new_expiry_ms / 1000).strftime('%d.%m.%Y %H:%M')
        else:
            new_expiry_date = "неизвестно"

        # Формируем отчёт по серверам
        results_text = ""
        for server_name, success in result.get('results', {}).items():
            status = "✅" if success else "❌"
            results_text += f"  {status} {server_name}\n"

        period_text = {30: "1 месяц", 90: "3 месяца", 180: "6 месяцев", 365: "1 год"}.get(extend_days, f"{extend_days} дней")

        await callback.message.edit_text(
            f"✅ <b>Ключ успешно продлён!</b>\n\n"
            f"👤 Клиент: <code>{client_email or uuid_prefix}</code>\n"
            f"📅 Период: +{period_text}\n"
            f"⏰ Новый срок: <b>{new_expiry_date}</b>\n\n"
            f"<b>Результаты по серверам:</b>\n{results_text}",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="new_search")],
                [InlineKeyboardButton(text="◀️ В меню", callback_data="cancel_key_delete")]
            ])
        )
    else:
        await callback.message.edit_text(
            f"❌ <b>Ошибка продления</b>\n\n"
            f"Не удалось продлить ключ на серверах.\n"
            f"UUID: <code>{uuid_prefix}...</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data=f"extend_{uuid_prefix}")],
                [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="new_search")]
            ])
        )


@router.callback_query(F.data.startswith("get_link_"))
async def get_client_link_callback(callback: CallbackQuery, **kwargs):
    """Получить VLESS ссылку для клиента по UUID"""
    uuid_prefix = callback.data.replace("get_link_", "")

    await callback.answer("Генерирую ссылку...")

    # Ищем полный UUID клиента на серверах
    import json
    import subprocess
    import sqlite3
    from pathlib import Path

    servers_file = Path(__file__).parent.parent.parent / 'servers_config.json'
    with open(servers_file, 'r') as f:
        config = json.load(f)

    client_info = None
    full_uuid = None
    target_server = None

    # Ищем на локальном сервере
    try:
        conn = sqlite3.connect('/etc/x-ui/x-ui.db')
        cursor = conn.cursor()
        cursor.execute("SELECT settings FROM inbounds WHERE enable=1")
        rows = cursor.fetchall()
        conn.close()

        for (settings_str,) in rows:
            try:
                settings = json.loads(settings_str)
                for client in settings.get('clients', []):
                    if client.get('id', '').startswith(uuid_prefix):
                        full_uuid = client.get('id')
                        client_info = client
                        # Найти активный сервер для генерации ссылки
                        for srv in config.get('servers', []):
                            if srv.get('active_for_new'):
                                target_server = srv
                                break
                        break
            except:
                continue
            if client_info:
                break
    except Exception as e:
        logger.error(f"Ошибка поиска UUID на локальном сервере: {e}")

    # Если не нашли локально, ищем на удалённых
    if not client_info:
        for server in config.get('servers', []):
            if server.get('local') or not server.get('enabled', True):
                continue

            # Попробуем через API панели (если есть)
            panel_config = server.get('panel', {})
            if panel_config:
                try:
                    import ssl
                    import urllib.request
                    import urllib.parse
                    import http.cookiejar

                    ip = server.get('ip', '')
                    port = panel_config.get('port', 1020)
                    path = panel_config.get('path', '')
                    username = panel_config.get('username', '')
                    password = panel_config.get('password', '')

                    if ip and username and password:
                        ctx = ssl.create_default_context()
                        ctx.check_hostname = False
                        ctx.verify_mode = ssl.CERT_NONE

                        cookie_jar = http.cookiejar.CookieJar()
                        opener = urllib.request.build_opener(
                            urllib.request.HTTPCookieProcessor(cookie_jar),
                            urllib.request.HTTPSHandler(context=ctx)
                        )

                        base_url = f"https://{ip}:{port}{path}"

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

                        resp = opener.open(login_req, timeout=10)
                        login_result = json.loads(resp.read())

                        if login_result.get('success'):
                            list_req = urllib.request.Request(f"{base_url}/panel/api/inbounds/list")
                            resp = opener.open(list_req, timeout=10)
                            data = json.loads(resp.read())

                            if data.get('success'):
                                for inbound in data.get('obj', []):
                                    settings_str = inbound.get('settings', '{}')
                                    try:
                                        settings = json.loads(settings_str)
                                        for client in settings.get('clients', []):
                                            if client.get('id', '').startswith(uuid_prefix):
                                                full_uuid = client.get('id')
                                                client_info = client
                                                target_server = server
                                                break
                                    except:
                                        continue
                                    if client_info:
                                        break
                except Exception as e:
                    logger.error(f"Ошибка поиска через API панели: {e}")

            if client_info:
                break

            # Если нет панели - пробуем через SSH
            ssh_config = server.get('ssh', {})
            if not ssh_config.get('password') or not server.get('ip'):
                continue

            try:
                cmd = f"sshpass -p '{ssh_config['password']}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=5 {ssh_config.get('user', 'root')}@{server['ip']} \"sqlite3 /etc/x-ui/x-ui.db 'SELECT settings FROM inbounds WHERE enable=1'\""
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)

                if result.returncode == 0 and result.stdout.strip():
                    for line in result.stdout.strip().split('\n'):
                        try:
                            settings = json.loads(line)
                            for client in settings.get('clients', []):
                                if client.get('id', '').startswith(uuid_prefix):
                                    full_uuid = client.get('id')
                                    client_info = client
                                    target_server = server
                                    break
                        except:
                            continue
                        if client_info:
                            break
            except:
                continue
            if client_info:
                break

    if not client_info or not full_uuid:
        await callback.message.answer("❌ Клиент не найден")
        return

    # Генерируем VLESS ссылку с параметрами ТОГО сервера, где найден клиент
    email = client_info.get('email', 'client')
    vless_link = None

    if target_server:
        # Используем параметры сервера, где реально найден клиент
        domain = target_server.get('domain', target_server.get('ip', ''))
        port = target_server.get('port', 443)
        inbounds = target_server.get('inbounds', {})
        main_inbound = inbounds.get('main', {})

        sni = main_inbound.get('sni', '')
        pbk = main_inbound.get('pbk', '')
        sid = main_inbound.get('sid', '')
        fp = main_inbound.get('fp', 'chrome')
        security = main_inbound.get('security', 'reality')
        flow = main_inbound.get('flow', '')
        name_prefix = main_inbound.get('name_prefix', '')
        network = main_inbound.get('network', 'tcp')

        params = [f"type={network}", "encryption=none"]
        if network == 'grpc':
            params.append(f"serviceName={main_inbound.get('serviceName', '')}")
            params.append(f"authority={main_inbound.get('authority', '')}")
        params.append(f"security={security}")
        if security == 'reality':
            if pbk:
                params.append(f"pbk={pbk}")
            params.append(f"fp={fp or 'chrome'}")
            if sni:
                params.append(f"sni={sni}")
            if sid:
                params.append(f"sid={sid}")
            if flow:
                params.append(f"flow={flow}")
            params.append("spx=%2F")

        link_name = f"{name_prefix} {email}" if name_prefix else email
        vless_link = f"vless://{full_uuid}@{domain}:{port}?" + "&".join(params) + f"#{link_name}"
    else:
        # Fallback на старую функцию
        from bot.api.remote_xui import get_client_link_from_active_server
        vless_link = await get_client_link_from_active_server(full_uuid, email)

    if vless_link:
        sub_url = f"https://{_get_sub_domain(kwargs)}/sub/{full_uuid}"

        text = (
            f"🔑 <b>Ключ клиента</b>\n\n"
            f"👤 Email: <code>{email}</code>\n"
            f"🔑 UUID: <code>{full_uuid[:8]}...</code>\n\n"
            f"<b>VLESS ключ:</b>\n<code>{vless_link}</code>\n\n"
            f"<b>Подписка:</b>\n<code>{sub_url}</code>"
        )

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="new_search")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data="cancel_key_delete")]
        ])

        # Генерируем QR код подписки
        try:
            qr_code = generate_qr_code(sub_url)
            await callback.message.answer_photo(
                BufferedInputFile(qr_code.read(), filename="qrcode.png"),
                caption=text,
                parse_mode="HTML",
                reply_markup=keyboard
            )
        except Exception as e:
            logger.error(f"QR generation error in admin search: {e}")
            await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await callback.message.answer("❌ Не удалось сгенерировать ссылку")


# ==================== УПРАВЛЕНИЕ ВЕБ-ЗАКАЗАМИ И РЕКВИЗИТАМИ ====================

import json
import aiosqlite
from pathlib import Path


# Глобальный домен подписки (обновляется при инициализации)
_sub_domain = 'zov-gor.ru'

def set_sub_domain(domain: str):
    """Установить домен подписки"""
    global _sub_domain
    _sub_domain = domain

def _get_sub_domain(kwargs_dict=None):
    """Получить домен подписки — из brand context если есть, иначе глобальный"""
    if isinstance(kwargs_dict, dict):
        brand = kwargs_dict.get('brand')
        if brand and hasattr(brand, 'domain'):
            return brand.domain
    return _sub_domain


PAYMENT_FILE = Path(__file__).parent.parent.parent / 'payment_details.json'
ORDERS_DB = Path(__file__).parent.parent.parent / 'web_orders.db'


class AddServerStates(StatesGroup):
    """Состояния для добавления нового сервера (через панель, без SSH)"""
    waiting_name = State()
    waiting_ip = State()
    waiting_domain = State()
    waiting_panel_port = State()
    waiting_panel_path = State()
    waiting_panel_credentials = State()
    waiting_name_prefix = State()
    confirm = State()


class PaymentSettingsStates(StatesGroup):
    """Состояния для настройки реквизитов"""
    waiting_for_card = State()
    waiting_for_sbp = State()
    waiting_for_holder = State()


def load_payment_details():
    """Загрузить реквизиты"""
    if PAYMENT_FILE.exists():
        with open(PAYMENT_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"active": False}


def save_payment_details(data):
    """Сохранить реквизиты"""
    with open(PAYMENT_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


@router.message(F.text == "💳 Реквизиты")
async def show_payment_settings(message: Message):
    """Показать настройки реквизитов"""
    if message.from_user.id != ADMIN_ID:
        return

    details = load_payment_details()
    
    status = "✅ Активно" if details.get("active") else "❌ Неактивно"
    card = details.get("card", {})
    sbp = details.get("sbp", {})
    
    text = (
        f"💳 <b>РЕКВИЗИТЫ ОПЛАТЫ</b>\n\n"
        f"Статус: {status}\n\n"
        f"<b>Карта:</b>\n"
        f"• Номер: <code>{card.get('number', 'не указан')}</code>\n"
        f"• Банк: {card.get('bank', 'не указан')}\n"
        f"• Получатель: {card.get('holder', 'не указан')}\n\n"
        f"<b>СБП:</b>\n"
        f"• Телефон: <code>{sbp.get('phone', 'не указан')}</code>\n"
        f"• Банк: {sbp.get('bank', 'не указан')}\n\n"
        f"<b>Команды:</b>\n"
        f"/set_card &lt;номер&gt; - Установить номер карты\n"
        f"/set_sbp &lt;телефон&gt; - Установить телефон СБП\n"
        f"/set_holder &lt;имя&gt; - Установить получателя\n"
        f"/set_bank &lt;банк&gt; - Установить банк\n"
        f"/payment_on - Включить оплату\n"
        f"/payment_off - Выключить оплату"
    )
    
    await message.answer(text, parse_mode="HTML")


@router.message(F.text.startswith("/set_card"))
async def set_card_number(message: Message):
    """Установить номер карты"""
    if message.from_user.id != ADMIN_ID:
        return
    
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /set_card 2200 0000 0000 0000")
        return
    
    card_number = parts[1].strip()
    details = load_payment_details()
    if "card" not in details:
        details["card"] = {}
    details["card"]["number"] = card_number
    save_payment_details(details)
    
    await message.answer(f"✅ Номер карты установлен: <code>{card_number}</code>", parse_mode="HTML")


@router.message(F.text.startswith("/set_sbp"))
async def set_sbp_phone(message: Message):
    """Установить телефон СБП"""
    if message.from_user.id != ADMIN_ID:
        return
    
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /set_sbp +7 900 000 00 00")
        return
    
    phone = parts[1].strip()
    details = load_payment_details()
    if "sbp" not in details:
        details["sbp"] = {}
    details["sbp"]["phone"] = phone
    save_payment_details(details)
    
    await message.answer(f"✅ Телефон СБП установлен: <code>{phone}</code>", parse_mode="HTML")


@router.message(F.text.startswith("/set_holder"))
async def set_card_holder(message: Message):
    """Установить получателя"""
    if message.from_user.id != ADMIN_ID:
        return
    
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /set_holder IVAN IVANOV")
        return
    
    holder = parts[1].strip().upper()
    details = load_payment_details()
    if "card" not in details:
        details["card"] = {}
    details["card"]["holder"] = holder
    save_payment_details(details)
    
    await message.answer(f"✅ Получатель установлен: {holder}")


@router.message(F.text.startswith("/set_bank"))
async def set_bank(message: Message):
    """Установить банк"""
    if message.from_user.id != ADMIN_ID:
        return
    
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Использование: /set_bank Сбербанк")
        return
    
    bank = parts[1].strip()
    details = load_payment_details()
    if "card" not in details:
        details["card"] = {}
    if "sbp" not in details:
        details["sbp"] = {}
    details["card"]["bank"] = bank
    details["sbp"]["bank"] = bank
    save_payment_details(details)
    
    await message.answer(f"✅ Банк установлен: {bank}")


@router.message(F.text == "/payment_on")
async def payment_on(message: Message):
    """Включить оплату"""
    if message.from_user.id != ADMIN_ID:
        return
    
    details = load_payment_details()
    details["active"] = True
    save_payment_details(details)
    
    await message.answer("✅ Оплата на сайте включена!")


@router.message(F.text == "/payment_off")
async def payment_off(message: Message):
    """Выключить оплату"""
    if message.from_user.id != ADMIN_ID:
        return
    
    details = load_payment_details()
    details["active"] = False
    save_payment_details(details)
    
    await message.answer("❌ Оплата на сайте выключена!")


@router.message(F.text.startswith("/web_approve"))
async def approve_web_order(message: Message, db: DatabaseManager, xui_client, **kwargs):
    """Подтвердить веб-заказ и выдать ключ"""
    if message.from_user.id != ADMIN_ID:
        return
    
    parts = message.text.split()
    if len(parts) < 2:
        await message.answer("Использование: /web_approve ORDER_ID")
        return
    
    order_id = parts[1].strip().upper()
    
    async with aiosqlite.connect(ORDERS_DB) as db_orders:
        db_orders.row_factory = aiosqlite.Row
        cursor = await db_orders.execute('SELECT * FROM web_orders WHERE id = ?', (order_id,))
        order = await cursor.fetchone()
        
        if not order:
            await message.answer(f"❌ Заказ {order_id} не найден")
            return
        
        if order["status"] == "completed":
            await message.answer(f"⚠️ Заказ {order_id} уже выполнен")
            return
        
        order_dict = dict(order)
    
    # Генерируем ключ через X-UI на активном сервере
    try:
        from bot.api.remote_xui import get_client_link_from_active_server
        from bot.config import INBOUND_ID

        status_msg = await message.answer("⏳ Генерирую ключ...")

        # Используем контакт как email/имя клиента
        client_name = f"web_{order_id}_{order_dict['contact'].replace('@', '').replace('+', '')[:15]}"

        # Создаем клиента в X-UI (на активных серверах)
        client_data = await xui_client.add_client(
            inbound_id=INBOUND_ID,  # Используем inbound из конфига
            email=client_name,
            phone=client_name,
            expire_days=order_dict["days"],
            ip_limit=2
        )

        if client_data and not client_data.get('error'):
            # Получаем UUID клиента
            client_uuid = client_data.get('client_id', '')

            # Получаем VLESS ссылку с активного сервера
            vless_key = await get_client_link_from_active_server(
                client_uuid=client_uuid,
                client_email=client_name
            )

            if vless_key:
                # Формируем ссылку подписки
                subscription_url = f"https://{_get_sub_domain(kwargs)}/sub/{client_uuid}" if client_uuid else ""

                # Сохраняем ключ в заказ
                async with aiosqlite.connect(ORDERS_DB) as db_orders:
                    await db_orders.execute('''
                        UPDATE web_orders
                        SET status = 'completed', vless_key = ?, confirmed_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    ''', (vless_key, order_id))
                    await db_orders.commit()

                sub_text = f"\n🔄 Подписка:\n<code>{subscription_url}</code>\n" if subscription_url else ""
                await status_msg.edit_text(
                    f"✅ <b>Заказ {order_id} выполнен!</b>\n\n"
                    f"📦 Тариф: {order_dict['tariff_name']}\n"
                    f"📱 Контакт: {order_dict['contact']}\n"
                    f"📅 Дней: {order_dict['days']}\n\n"
                    f"🔑 Ключ:\n<code>{vless_key}</code>{sub_text}\n"
                    f"Клиент может проверить статус заказа на сайте.",
                    parse_mode="HTML"
                )
            else:
                await status_msg.edit_text("❌ Ошибка: не удалось получить ссылку на ключ")
        else:
            error_msg = client_data.get('message', 'Неизвестная ошибка') if client_data else 'Не удалось создать клиента'
            await status_msg.edit_text(f"❌ Ошибка создания клиента: {error_msg}")
            
    except Exception as e:
        logger.error(f"Error generating key for web order: {e}")
        await message.answer(f"❌ Ошибка: {e}")


@router.message(F.text == "/web_orders")
async def list_web_orders(message: Message):
    """Показать список веб-заказов"""
    if message.from_user.id != ADMIN_ID:
        return
    
    if not ORDERS_DB.exists():
        await message.answer("📋 Веб-заказов пока нет")
        return
    
    async with aiosqlite.connect(ORDERS_DB) as db_orders:
        db_orders.row_factory = aiosqlite.Row
        cursor = await db_orders.execute(
            'SELECT * FROM web_orders ORDER BY created_at DESC LIMIT 20'
        )
        orders = await cursor.fetchall()
    
    if not orders:
        await message.answer("📋 Веб-заказов пока нет")
        return
    
    text = "📋 <b>ПОСЛЕДНИЕ ВЕБ-ЗАКАЗЫ:</b>\n\n"
    
    status_emoji = {
        "pending": "⏳",
        "paid": "💰", 
        "completed": "✅",
        "cancelled": "❌"
    }
    
    for order in orders:
        emoji = status_emoji.get(order["status"], "❓")
        text += (
            f"{emoji} <b>{order['id']}</b> - {order['tariff_name']} ({order['price']}₽)\n"
            f"   📱 {order['contact']} | {order['created_at'][:10]}\n"
        )
        if order["status"] == "paid":
            text += f"   ➡️ /web_approve {order['id']}\n"
        text += "\n"
    
    await message.answer(text, parse_mode="HTML")


@router.message(F.text == "📋 Веб-заказы")
async def show_web_orders_button(message: Message):
    """Показать веб-заказы через кнопку"""
    if message.from_user.id != ADMIN_ID:
        return
    
    # Переиспользуем логику list_web_orders
    if not ORDERS_DB.exists():
        await message.answer("📋 Веб-заказов пока нет")
        return
    
    async with aiosqlite.connect(ORDERS_DB) as db_orders:
        db_orders.row_factory = aiosqlite.Row
        cursor = await db_orders.execute(
            'SELECT * FROM web_orders ORDER BY created_at DESC LIMIT 20'
        )
        orders = await cursor.fetchall()
    
    if not orders:
        await message.answer("📋 Веб-заказов пока нет")
        return
    
    text = "📋 <b>ПОСЛЕДНИЕ ВЕБ-ЗАКАЗЫ:</b>\n\n"
    
    status_emoji = {
        "pending": "⏳",
        "paid": "💰", 
        "completed": "✅",
        "cancelled": "❌"
    }
    
    for order in orders:
        emoji = status_emoji.get(order["status"], "❓")
        text += (
            f"{emoji} <b>{order['id']}</b> - {order['tariff_name']} ({order['price']}₽)\n"
            f"   📱 {order['contact']} | {order['created_at'][:10]}\n"
        )
        if order["status"] == "paid":
            text += f"   ➡️ /web_approve {order['id']}\n"
        text += "\n"
    
    await message.answer(text, parse_mode="HTML")


# ============== CALLBACK HANDLERS FOR WEB ORDERS ==============

@router.callback_query(F.data.startswith("web_approve_"))
async def callback_approve_web_order(callback: CallbackQuery, db: DatabaseManager, xui_client, **kwargs):
    """Подтвердить веб-заказ через кнопку"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа")
        return

    order_id = callback.data.replace("web_approve_", "")

    async with aiosqlite.connect(ORDERS_DB) as db_orders:
        db_orders.row_factory = aiosqlite.Row
        cursor = await db_orders.execute('SELECT * FROM web_orders WHERE id = ?', (order_id,))
        order = await cursor.fetchone()

        if not order:
            await callback.answer("Заказ не найден")
            return

        if order["status"] == "completed":
            await callback.answer("Заказ уже выполнен")
            return

        order_dict = dict(order)

    await callback.answer("Генерирую ключ...")

    # Редактируем сообщение
    try:
        if callback.message.photo or callback.message.document:
            await callback.message.edit_caption(
                caption=callback.message.caption + "\n\n⏳ <b>Генерация ключа...</b>",
                parse_mode="HTML"
            )
        else:
            await callback.message.edit_text(
                text=callback.message.text + "\n\n⏳ <b>Генерация ключа...</b>",
                parse_mode="HTML"
            )
    except:
        pass

    # Генерируем ключ через X-UI на активном сервере
    try:
        from bot.api.remote_xui import get_client_link_from_active_server
        from bot.config import INBOUND_ID

        client_name = f"web_{order_id}_{order_dict['contact'].replace('@', '').replace('+', '')[:15]}"

        # Создаем клиента в X-UI (на активных серверах)
        client_data = await xui_client.add_client(
            inbound_id=INBOUND_ID,  # Используем inbound из конфига
            email=client_name,
            phone=client_name,
            expire_days=order_dict["days"],
            ip_limit=2
        )

        if client_data and not client_data.get('error'):
            # Получаем UUID клиента
            client_uuid = client_data.get('client_id', '')

            # Получаем VLESS ссылку с активного сервера
            vless_key = await get_client_link_from_active_server(
                client_uuid=client_uuid,
                client_email=client_name
            )

            if vless_key:
                # Формируем ссылку подписки
                subscription_url = f"https://{_get_sub_domain(kwargs)}/sub/{client_uuid}" if client_uuid else ""

                # Сохраняем ключ в заказ
                async with aiosqlite.connect(ORDERS_DB) as db_orders:
                    await db_orders.execute('''
                        UPDATE web_orders
                        SET status = 'completed', vless_key = ?, confirmed_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    ''', (vless_key, order_id))
                    await db_orders.commit()

                sub_text = f"\n🔄 Подписка:\n<code>{subscription_url}</code>\n" if subscription_url else ""
                success_text = (
                    f"✅ <b>Заказ {order_id} выполнен!</b>\n\n"
                    f"📦 Тариф: {order_dict['tariff_name']}\n"
                    f"📱 Контакт: {order_dict['contact']}\n"
                    f"📅 Дней: {order_dict['days']}\n\n"
                    f"🔑 Ключ:\n<code>{vless_key}</code>{sub_text}\n"
                    f"Клиент может проверить статус заказа на сайте."
                )

                try:
                    if callback.message.photo or callback.message.document:
                        await callback.message.edit_caption(caption=success_text, parse_mode="HTML")
                    else:
                        await callback.message.edit_text(text=success_text, parse_mode="HTML")
                except:
                    await callback.message.answer(success_text, parse_mode="HTML")
            else:
                await callback.message.answer("❌ Ошибка: не удалось получить ссылку на ключ")
        else:
            error_msg = client_data.get('message', 'Неизвестная ошибка') if client_data else 'Не удалось создать клиента'
            await callback.message.answer(f"❌ Ошибка создания клиента: {error_msg}")

    except Exception as e:
        logger.error(f"Error generating key for web order: {e}")
        await callback.message.answer(f"❌ Ошибка: {e}")


@router.callback_query(F.data.startswith("web_reject_"))
async def callback_reject_web_order(callback: CallbackQuery, state: FSMContext):
    """Начать отказ веб-заказа через кнопку"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа")
        return

    order_id = callback.data.replace("web_reject_", "")

    # Сохраняем ID заказа и сообщения для последующего редактирования
    await state.update_data(
        reject_order_id=order_id,
        reject_message_id=callback.message.message_id,
        reject_chat_id=callback.message.chat.id
    )
    await state.set_state(WebOrderRejectStates.waiting_for_reject_reason)

    await callback.answer()
    await callback.message.answer(
        f"❌ <b>Отказ заказа {order_id}</b>\n\n"
        f"Напишите причину отказа (она будет видна клиенту):\n\n"
        f"Или отправьте /cancel для отмены",
        parse_mode="HTML"
    )


@router.message(WebOrderRejectStates.waiting_for_reject_reason, F.text == "/cancel")
async def cancel_reject_order(message: Message, state: FSMContext):
    """Отмена отказа заказа"""
    await state.clear()
    await message.answer("Отказ заказа отменён.", reply_markup=Keyboards.admin_menu())


@router.message(WebOrderRejectStates.waiting_for_reject_reason)
async def process_reject_reason(message: Message, state: FSMContext):
    """Обработка причины отказа"""
    if message.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    order_id = data.get("reject_order_id")

    if not order_id:
        await state.clear()
        await message.answer("Ошибка: заказ не найден")
        return

    reject_reason = message.text.strip()

    # Обновляем статус заказа
    async with aiosqlite.connect(ORDERS_DB) as db_orders:
        db_orders.row_factory = aiosqlite.Row
        cursor = await db_orders.execute('SELECT * FROM web_orders WHERE id = ?', (order_id,))
        order = await cursor.fetchone()

        if not order:
            await state.clear()
            await message.answer("Заказ не найден")
            return

        order_dict = dict(order)

        await db_orders.execute('''
            UPDATE web_orders
            SET status = 'cancelled', admin_comment = ?
            WHERE id = ?
        ''', (reject_reason, order_id))
        await db_orders.commit()

    await state.clear()

    await message.answer(
        f"❌ <b>Заказ {order_id} отклонён</b>\n\n"
        f"📦 Тариф: {order_dict['tariff_name']}\n"
        f"📱 Контакт: {order_dict['contact']}\n"
        f"💬 Причина: {reject_reason}",
        parse_mode="HTML",
        reply_markup=Keyboards.admin_menu()
    )

    # Пытаемся отредактировать оригинальное сообщение
    try:
        bot = message.bot
        original_msg_id = data.get("reject_message_id")
        chat_id = data.get("reject_chat_id")
        if original_msg_id and chat_id:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=original_msg_id, reply_markup=None)
    except:
        pass


# ===== СТАТУС СЕРВЕРОВ =====

def load_servers_config():
    """Загрузить конфигурацию серверов"""
    import json
    from pathlib import Path
    config_path = Path('/root/manager_vpn/servers_config.json')
    if config_path.exists():
        with open(config_path, 'r') as f:
            return json.load(f)
    return {"servers": []}


def save_servers_config(config: dict):
    """Сохранить конфигурацию серверов"""
    import json
    from pathlib import Path
    config_path = Path('/root/manager_vpn/servers_config.json')
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(config, f, ensure_ascii=False, indent=2)


@router.message(F.text == "🖥 Статус серверов")
@admin_only
async def check_servers_status(message: Message, **kwargs):
    """Проверка доступности всех VPN серверов"""
    import json
    import asyncio
    from pathlib import Path

    await message.answer("⏳ Проверяю доступность серверов...")

    # Загружаем конфигурацию серверов
    config_path = Path('/root/manager_vpn/servers_config.json')
    if not config_path.exists():
        await message.answer(
            "❌ Файл конфигурации серверов не найден.",
            reply_markup=Keyboards.admin_menu()
        )
        return

    with open(config_path, 'r') as f:
        config = json.load(f)

    servers = [s for s in config.get('servers', []) if s.get('enabled', False)]
    if not servers:
        await message.answer(
            "❌ Серверы не настроены.",
            reply_markup=Keyboards.admin_menu()
        )
        return

    results = []

    for server in servers:
        server_name = server.get('name', 'Unknown')
        server_ip = server.get('ip', '')
        server_domain = server.get('domain', '')
        is_local = server.get('local', False)
        is_enabled = server.get('enabled', True)

        if not is_enabled:
            results.append({
                'name': server_name,
                'status': 'disabled',
                'details': 'Сервер отключен в конфиге'
            })
            continue

        server_result = {
            'name': server_name,
            'ip': server_ip,
            'domain': server_domain,
            'local': is_local,
            'checks': {}
        }

        if is_local:
            # Проверка локального сервера
            try:
                # Проверяем X-UI
                proc = await asyncio.create_subprocess_shell(
                    "systemctl is-active x-ui",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                xui_status = stdout.decode().strip() == 'active'
                server_result['checks']['x-ui'] = xui_status

                # Проверяем xray процесс
                proc = await asyncio.create_subprocess_shell(
                    "pgrep -f 'xray' > /dev/null && echo 'ok'",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                xray_status = 'ok' in stdout.decode()
                server_result['checks']['xray'] = xray_status

                # Проверяем порт 443
                proc = await asyncio.create_subprocess_shell(
                    "ss -tlnp | grep ':443 ' > /dev/null && echo 'ok'",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                port_status = 'ok' in stdout.decode()
                server_result['checks']['port_443'] = port_status

                # Считаем клиентов
                proc = await asyncio.create_subprocess_shell(
                    "sqlite3 /etc/x-ui/x-ui.db \"SELECT COUNT(*) FROM client_traffics WHERE enable=1 AND expiry_time > strftime('%s','now')*1000;\"",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
                try:
                    clients_count = int(stdout.decode().strip())
                except:
                    clients_count = 0
                server_result['clients'] = clients_count

                server_result['status'] = 'ok' if all(server_result['checks'].values()) else 'warning'

            except asyncio.TimeoutError:
                server_result['status'] = 'error'
                server_result['details'] = 'Таймаут при проверке'
            except Exception as e:
                server_result['status'] = 'error'
                server_result['details'] = str(e)

        else:
            # Проверка удалённого сервера
            ssh_config = server.get('ssh', {})
            panel_config = server.get('panel', {})
            ssh_password = ssh_config.get('password', '')

            # Если есть SSH - используем SSH
            if ssh_password:
                ssh_user = ssh_config.get('user', 'root')
                try:
                    cmd = f"sshpass -p '{ssh_password}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {ssh_user}@{server_ip} 'systemctl is-active x-ui && pgrep -c xray && ss -tlnp | grep -c \":443 \"'"

                    proc = await asyncio.create_subprocess_shell(
                        cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)

                    output_lines = stdout.decode().strip().split('\n')

                    if len(output_lines) >= 1:
                        xui_status = output_lines[0] == 'active'
                        server_result['checks']['x-ui'] = xui_status

                        if len(output_lines) >= 2:
                            try:
                                xray_count = int(output_lines[1])
                                server_result['checks']['xray'] = xray_count > 0
                            except:
                                server_result['checks']['xray'] = False

                        if len(output_lines) >= 3:
                            try:
                                port_count = int(output_lines[2])
                                server_result['checks']['port_443'] = port_count > 0
                            except:
                                server_result['checks']['port_443'] = False

                        server_result['status'] = 'ok' if all(server_result['checks'].values()) else 'warning'
                    else:
                        server_result['status'] = 'error'
                        server_result['details'] = 'Некорректный ответ сервера'

                    # Получаем количество клиентов
                    cmd_clients = f"sshpass -p '{ssh_password}' ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 {ssh_user}@{server_ip} \"sqlite3 /etc/x-ui/x-ui.db \\\"SELECT COUNT(*) FROM client_traffics WHERE enable=1 AND expiry_time > strftime('%s','now')*1000;\\\"\""

                    proc = await asyncio.create_subprocess_shell(
                        cmd_clients,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
                    try:
                        server_result['clients'] = int(stdout.decode().strip())
                    except:
                        server_result['clients'] = 0

                except asyncio.TimeoutError:
                    server_result['status'] = 'error'
                    server_result['details'] = 'Таймаут подключения SSH'
                except Exception as e:
                    server_result['status'] = 'error'
                    server_result['details'] = str(e)

            # Если нет SSH, но есть панель - используем API панели
            elif panel_config.get('url'):
                try:
                    import aiohttp
                    import ssl
                    ssl_context = ssl.create_default_context()
                    ssl_context.check_hostname = False
                    ssl_context.verify_mode = ssl.CERT_NONE

                    panel_url = panel_config.get('url')
                    panel_user = panel_config.get('username')
                    panel_pass = panel_config.get('password')

                    connector = aiohttp.TCPConnector(ssl=ssl_context)
                    jar = aiohttp.CookieJar(unsafe=True)
                    async with aiohttp.ClientSession(connector=connector, cookie_jar=jar) as session:
                        # Авторизация
                        login_url = f"{panel_url}/login"
                        async with session.post(login_url, json={"username": panel_user, "password": panel_pass}, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                            if resp.status == 200:
                                login_data = await resp.json()
                                if login_data.get('success'):
                                    server_result['checks']['panel_auth'] = True

                        # Проверяем inbound'ы
                        if server_result['checks'].get('panel_auth'):
                            inbounds_url = f"{panel_url}/panel/api/inbounds/list"
                            async with session.get(inbounds_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                if resp.status == 200:
                                    inb_data = await resp.json()
                                    if inb_data.get('success'):
                                        server_result['checks']['inbounds'] = True
                                        # Считаем активных клиентов
                                        total_clients = 0
                                        import time
                                        now_ms = int(time.time() * 1000)
                                        for inb in inb_data.get('obj', []):
                                            settings = json.loads(inb.get('settings', '{}'))
                                            for client in settings.get('clients', []):
                                                exp = client.get('expiryTime', 0)
                                                if client.get('enable', True) and (exp == 0 or exp > now_ms):
                                                    total_clients += 1
                                        server_result['clients'] = total_clients

                    server_result['status'] = 'ok' if all(server_result['checks'].values()) else 'warning'

                except asyncio.TimeoutError:
                    server_result['status'] = 'error'
                    server_result['details'] = 'Таймаут подключения к панели'
                except Exception as e:
                    server_result['status'] = 'error'
                    server_result['details'] = f'Ошибка панели: {str(e)[:50]}'
            else:
                server_result['status'] = 'error'
                server_result['details'] = 'Нет SSH или панели в конфиге'

        results.append(server_result)

    # Формируем ответ
    text = "🖥 <b>СТАТУС VPN СЕРВЕРОВ</b>\n\n"

    for r in results:
        if r.get('status') == 'disabled':
            text += f"⚫ <b>{r['name']}</b>\n"
            text += f"   └ {r.get('details', 'Отключен')}\n\n"
            continue

        status_emoji = {
            'ok': '🟢',
            'warning': '🟡',
            'error': '🔴'
        }.get(r.get('status'), '⚪')

        text += f"{status_emoji} <b>{r['name']}</b>"
        if r.get('local'):
            text += " (локальный)"
        text += "\n"

        if r.get('ip'):
            text += f"   📍 IP: <code>{r['ip']}</code>\n"
        if r.get('domain'):
            text += f"   🌐 Домен: <code>{r['domain']}</code>\n"

        checks = r.get('checks', {})
        if checks:
            text += "   📊 Службы:\n"
            for check_name, check_status in checks.items():
                check_emoji = '✅' if check_status else '❌'
                check_display = {
                    'x-ui': 'X-UI панель',
                    'xray': 'Xray процесс',
                    'port_443': 'Порт 443',
                    'panel_auth': 'Панель (авторизация)',
                    'inbounds': 'Inbound\'ы'
                }.get(check_name, check_name)
                text += f"      {check_emoji} {check_display}\n"

        if 'clients' in r:
            text += f"   👥 Активных клиентов: {r['clients']}\n"

        if r.get('details'):
            text += f"   ⚠️ {r['details']}\n"

        text += "\n"

    # Добавляем оплату серверов
    from datetime import datetime, date
    from bot.database.db_manager import DatabaseManager
    from bot.config import DATABASE_PATH
    db_mgr = DatabaseManager(DATABASE_PATH)
    payments = await db_mgr.get_all_server_payments()
    if payments:
        payments_map = {p['server_name']: p for p in payments}
        today = date.today()
        text += "💳 <b>Оплата серверов:</b>\n"
        for srv in servers:
            srv_name = srv.get('name', 'Unknown')
            payment = payments_map.get(srv_name)
            if payment:
                paid_until = datetime.strptime(payment['paid_until'], '%Y-%m-%d').date()
                days_left = (paid_until - today).days
                if days_left < 0:
                    pay_icon = "🔴"
                    pay_text = f"просрочено {abs(days_left)} дн."
                elif days_left <= 3:
                    pay_icon = "🟠"
                    pay_text = f"{days_left} дн."
                elif days_left <= 7:
                    pay_icon = "🟡"
                    pay_text = f"{days_left} дн."
                else:
                    pay_icon = "🟢"
                    pay_text = f"{days_left} дн."
                text += f"   {pay_icon} {srv_name}: до {payment['paid_until']} ({pay_text})\n"
            else:
                text += f"   ⚪ {srv_name}: не указана\n"
        text += "\n"

    text += f"━━━━━━━━━━━━━━━━\n"
    text += f"🕐 Проверено: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}\n\n"

    # Добавляем статус активности для новых подписок
    text += "📋 <b>Активность для новых подписок:</b>\n"
    servers_cfg = load_servers_config()
    for srv in servers_cfg.get('servers', []):
        srv_name = srv.get('name', 'Unknown')
        is_active = srv.get('active_for_new', True)
        status_icon = "✅" if is_active else "❌"
        text += f"   {status_icon} {srv_name}: {'Включен' if is_active else 'Выключен'}\n"

    # Кнопки для управления серверами
    buttons = []
    for srv in servers_cfg.get('servers', []):
        srv_name = srv.get('name', 'Unknown')
        is_active = srv.get('active_for_new', True)
        action = "disable" if is_active else "enable"
        btn_text = f"{'🔴 Выкл' if is_active else '🟢 Вкл'} {srv_name}"
        buttons.append([
            InlineKeyboardButton(text=btn_text, callback_data=f"server_{action}_{srv_name}"),
            InlineKeyboardButton(text=f"✏️", callback_data=f"srv_edit_{srv_name}")
        ])

    # Кнопка добавления нового сервера
    buttons.append([InlineKeyboardButton(text="➕ Добавить сервер", callback_data="add_new_server")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=keyboard
    )


# ============ ОПЛАТА СЕРВЕРОВ ============

@router.message(F.text == "💳 Оплата серверов")
@admin_only
async def server_payments_menu(message: Message, **kwargs):
    """Показать статус оплаты серверов"""
    from bot.api.remote_xui import load_servers_config
    from bot.database.db_manager import DatabaseManager
    from bot.config import DATABASE_PATH
    from datetime import datetime, date

    db = DatabaseManager(DATABASE_PATH)
    payments = await db.get_all_server_payments()
    servers_config = load_servers_config()
    servers = servers_config.get('servers', [])

    # Маппинг оплат по имени сервера
    payments_map = {p['server_name']: p for p in payments}

    today = date.today()
    text = "💳 <b>ОПЛАТА СЕРВЕРОВ</b>\n\n"

    buttons = []

    for server in servers:
        name = server.get('name', 'Unknown')
        enabled = server.get('enabled', False)

        payment = payments_map.get(name)

        if payment:
            paid_until = datetime.strptime(payment['paid_until'], '%Y-%m-%d').date()
            days_left = (paid_until - today).days
            cost = payment.get('monthly_cost', 0)
            currency = payment.get('currency', 'RUB')

            if days_left < 0:
                emoji = "🔴"
                status_text = f"просрочена {abs(days_left)} дн."
            elif days_left == 0:
                emoji = "🔴"
                status_text = "истекает сегодня!"
            elif days_left <= 3:
                emoji = "🟠"
                status_text = f"осталось {days_left} дн."
            elif days_left <= 7:
                emoji = "🟡"
                status_text = f"осталось {days_left} дн."
            else:
                emoji = "🟢"
                status_text = f"осталось {days_left} дн."

            text += f"{emoji} <b>{name}</b>"
            if not enabled:
                text += " (выкл)"
            text += f"\n   📅 До: <b>{payment['paid_until']}</b> ({status_text})\n"
            if cost > 0:
                text += f"   💰 {cost:.0f} {currency}/мес\n"
            if payment.get('notes'):
                text += f"   📝 {payment['notes']}\n"
            text += "\n"
        else:
            text += f"⚪ <b>{name}</b>"
            if not enabled:
                text += " (выкл)"
            text += "\n   📅 Дата оплаты не указана\n\n"

        buttons.append([
            InlineKeyboardButton(
                text=f"📅 {name}",
                callback_data=f"srvpay_{name}"
            )
        ])

    text += "━━━━━━━━━━━━━━━━\n"
    text += "Нажмите на сервер, чтобы установить дату оплаты."

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("srvpay_"))
async def server_payment_select(callback: CallbackQuery, state: FSMContext):
    """Выбор сервера для установки даты оплаты"""
    server_name = callback.data[len("srvpay_"):]

    from bot.database.db_manager import DatabaseManager
    from bot.config import DATABASE_PATH

    db = DatabaseManager(DATABASE_PATH)
    payment = await db.get_server_payment(server_name)

    text = f"💳 <b>Оплата: {server_name}</b>\n\n"
    if payment:
        text += f"📅 Текущая дата: <b>{payment['paid_until']}</b>\n"
        if payment.get('monthly_cost', 0) > 0:
            text += f"💰 Стоимость: {payment['monthly_cost']:.0f} {payment.get('currency', 'RUB')}/мес\n"
        if payment.get('notes'):
            text += f"📝 Заметка: {payment['notes']}\n"
        text += "\n"

    text += "Введите дату оплаты (до какого числа оплачено):\n"
    text += "Формат: <code>ДД.ММ.ГГГГ</code>\n"
    text += "Например: <code>15.04.2026</code>"

    await state.set_state(ServerPaymentStates.waiting_for_date)
    await state.update_data(payment_server_name=server_name)

    buttons = []
    if payment:
        buttons.append([InlineKeyboardButton(text="🗑 Удалить запись", callback_data=f"srvpay_del_{server_name}")])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="srvpay_cancel")])

    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("srvpay_del_"))
async def server_payment_delete(callback: CallbackQuery, state: FSMContext):
    """Удалить запись оплаты"""
    server_name = callback.data[len("srvpay_del_"):]

    from bot.database.db_manager import DatabaseManager
    from bot.config import DATABASE_PATH

    db = DatabaseManager(DATABASE_PATH)
    await db.delete_server_payment(server_name)
    await state.clear()

    await callback.message.edit_text(
        f"✅ Запись оплаты для <b>{server_name}</b> удалена.",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "srvpay_cancel")
async def server_payment_cancel(callback: CallbackQuery, state: FSMContext):
    """Отмена установки оплаты"""
    await state.clear()
    await callback.message.edit_text("Отменено.")
    await callback.answer()


@router.message(ServerPaymentStates.waiting_for_date)
async def server_payment_process_date(message: Message, state: FSMContext):
    """Обработка введённой даты оплаты"""
    from datetime import datetime

    text = message.text.strip()

    if text == "Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=Keyboards.admin_menu())
        return

    # Парсим дату
    date_obj = None
    for fmt in ('%d.%m.%Y', '%Y-%m-%d', '%d/%m/%Y', '%d-%m-%Y'):
        try:
            date_obj = datetime.strptime(text, fmt).date()
            break
        except ValueError:
            continue

    if not date_obj:
        await message.answer(
            "❌ Неверный формат даты.\n"
            "Введите в формате <code>ДД.ММ.ГГГГ</code>, например: <code>15.04.2026</code>",
            parse_mode="HTML"
        )
        return

    await state.update_data(payment_date=date_obj.isoformat())
    await state.set_state(ServerPaymentStates.waiting_for_cost)

    data = await state.get_data()
    server_name = data.get('payment_server_name', '?')

    await message.answer(
        f"📅 Дата: <b>{date_obj.strftime('%d.%m.%Y')}</b>\n\n"
        f"Введите стоимость сервера в месяц (число).\n"
        f"Или отправьте <code>0</code> чтобы не указывать.\n\n"
        f"Можно добавить заметку через пробел:\n"
        f"<code>500 Оплата через Payeer</code>",
        parse_mode="HTML"
    )


@router.message(ServerPaymentStates.waiting_for_cost)
async def server_payment_process_cost(message: Message, state: FSMContext):
    """Обработка стоимости и сохранение"""
    from bot.database.db_manager import DatabaseManager
    from bot.config import DATABASE_PATH
    from datetime import datetime

    text = message.text.strip()

    if text == "Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=Keyboards.admin_menu())
        return

    # Парсим стоимость и заметку
    parts = text.split(None, 1)
    try:
        cost = float(parts[0].replace(',', '.'))
    except (ValueError, IndexError):
        await message.answer("❌ Введите число (стоимость). Например: <code>500</code>", parse_mode="HTML")
        return

    notes = parts[1] if len(parts) > 1 else ''

    data = await state.get_data()
    server_name = data.get('payment_server_name', '')
    paid_until = data.get('payment_date', '')

    db = DatabaseManager(DATABASE_PATH)
    success = await db.set_server_payment(
        server_name=server_name,
        paid_until=paid_until,
        monthly_cost=cost,
        notes=notes
    )

    await state.clear()

    if success:
        date_display = datetime.strptime(paid_until, '%Y-%m-%d').strftime('%d.%m.%Y')
        result_text = (
            f"✅ <b>Оплата сервера обновлена</b>\n\n"
            f"🖥 Сервер: <b>{server_name}</b>\n"
            f"📅 Оплачен до: <b>{date_display}</b>\n"
        )
        if cost > 0:
            result_text += f"💰 Стоимость: <b>{cost:.0f} RUB/мес</b>\n"
        if notes:
            result_text += f"📝 Заметка: {notes}\n"

        result_text += "\nУведомления придут за 7, 3 и 1 день до истечения."

        await message.answer(result_text, parse_mode="HTML", reply_markup=Keyboards.admin_menu())
    else:
        await message.answer("❌ Ошибка сохранения.", reply_markup=Keyboards.admin_menu())


# ============ ПАНЕЛИ УПРАВЛЕНИЯ X-UI ============

@router.message(F.text == "🔧 Панели X-UI")
@admin_only
async def show_xui_panels(message: Message, **kwargs):
    """Показать ссылки на панели управления X-UI серверов"""
    import json
    from pathlib import Path

    config_path = Path('/root/manager_vpn/servers_config.json')
    if not config_path.exists():
        await message.answer(
            "❌ Файл конфигурации серверов не найден.",
            reply_markup=Keyboards.admin_menu()
        )
        return

    with open(config_path, 'r') as f:
        config = json.load(f)

    servers = config.get('servers', [])
    if not servers:
        await message.answer(
            "❌ Серверы не настроены.",
            reply_markup=Keyboards.admin_menu()
        )
        return

    text = "🔧 <b>ПАНЕЛИ УПРАВЛЕНИЯ X-UI</b>\n\n"

    buttons = []
    for server in servers:
        name = server.get('name', 'Unknown')
        is_enabled = server.get('enabled', True)
        is_local = server.get('local', False)
        panel = server.get('panel', {})

        status_emoji = "🟢" if is_enabled else "⚫"
        text += f"{status_emoji} <b>{name}</b>"
        if is_local:
            text += " (локальный)"
        text += "\n"

        if is_local:
            # Локальный сервер - панель на localhost
            text += f"   🔗 Локальная панель X-UI\n"
            text += f"   📍 IP: {server.get('ip', 'N/A')}\n\n"
        elif panel.get('url'):
            panel_url = panel.get('url')
            panel_user = panel.get('username', 'N/A')
            panel_pass = panel.get('password', 'N/A')

            text += f"   🔗 <code>{panel_url}</code>\n"
            text += f"   👤 Логин: <code>{panel_user}</code>\n"
            text += f"   🔑 Пароль: <code>{panel_pass}</code>\n\n"

            # Кнопка для быстрого перехода
            buttons.append([InlineKeyboardButton(
                text=f"🌐 {name}",
                url=panel_url
            )])
        else:
            text += f"   ⚠️ Панель не настроена\n"
            text += f"   📍 IP: {server.get('ip', 'N/A')}\n\n"

    text += "━━━━━━━━━━━━━━━━\n"
    text += "💡 <i>Нажмите на кнопку для перехода в панель</i>"

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=keyboard
    )


# ============ ССЫЛКА НА ВЕБ АДМИН-ПАНЕЛЬ ============

@router.message(F.text == "🌐 Админ-панель сайта")
@admin_only
async def show_admin_panel_link(message: Message, **kwargs):
    """Показать ссылку на веб админ-панель"""
    from bot.config import ADMIN_PANEL_URL

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🌐 Открыть админ-панель", url=ADMIN_PANEL_URL)]
    ])

    await message.answer(
        "🌐 <b>Веб админ-панель</b>\n\n"
        f"🔗 <code>{ADMIN_PANEL_URL}</code>\n\n"
        "Нажмите кнопку ниже для перехода:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


# ============ УПРАВЛЕНИЕ СЕРВЕРАМИ ДЛЯ НОВЫХ ПОДПИСОК ============

@router.message(F.text == "/servers")
@admin_only
async def show_servers_management(message: Message, **kwargs):
    """Показать управление серверами для новых подписок"""
    config = load_servers_config()
    servers = config.get('servers', [])

    text = "🖥 <b>УПРАВЛЕНИЕ СЕРВЕРАМИ</b>\n\n"
    text += "Выберите серверы для новых подписок:\n\n"

    buttons = []
    for server in servers:
        name = server.get('name', 'Unknown')
        is_active = server.get('active_for_new', True)
        is_local = server.get('local', False)
        domain = server.get('domain', server.get('ip', ''))

        status_emoji = "✅" if is_active else "❌"
        local_tag = " (локальный)" if is_local else ""

        text += f"{status_emoji} <b>{name}</b>{local_tag}\n"
        text += f"   🌐 {domain}\n"
        text += f"   📊 Статус: {'Включен' if is_active else 'Выключен'}\n\n"

        # Кнопка для переключения
        action = "disable" if is_active else "enable"
        action_text = f"{'🔴 Выкл' if is_active else '🟢 Вкл'} {name}"
        buttons.append([InlineKeyboardButton(
            text=action_text,
            callback_data=f"server_{action}_{name}"
        )])

    text += "━━━━━━━━━━━━━━━━\n"
    text += "💡 <i>Включенные серверы используются\nдля создания новых подписок</i>"

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("server_enable_") | F.data.startswith("server_disable_"))
async def toggle_server_for_new(callback: CallbackQuery):
    """Переключить сервер для новых подписок"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts = callback.data.split("_", 2)
    action = parts[1]  # enable или disable
    server_name = parts[2]

    config = load_servers_config()

    # Находим сервер и переключаем
    server_found = False
    for server in config.get('servers', []):
        if server.get('name') == server_name:
            server['active_for_new'] = (action == "enable")
            server_found = True
            break

    if not server_found:
        await callback.answer(f"Сервер {server_name} не найден", show_alert=True)
        return

    # Сохраняем конфиг
    save_servers_config(config)

    # Обновляем сообщение
    servers = config.get('servers', [])

    text = "🖥 <b>УПРАВЛЕНИЕ СЕРВЕРАМИ</b>\n\n"
    text += "Выберите серверы для новых подписок:\n\n"

    buttons = []
    for server in servers:
        name = server.get('name', 'Unknown')
        is_active = server.get('active_for_new', True)
        is_local = server.get('local', False)
        domain = server.get('domain', server.get('ip', ''))

        status_emoji = "✅" if is_active else "❌"
        local_tag = " (локальный)" if is_local else ""

        text += f"{status_emoji} <b>{name}</b>{local_tag}\n"
        text += f"   🌐 {domain}\n"
        text += f"   📊 Статус: {'Включен' if is_active else 'Выключен'}\n\n"

        # Кнопка для переключения
        btn_action = "disable" if is_active else "enable"
        action_text = f"{'🔴 Выкл' if is_active else '🟢 Вкл'} {name}"
        buttons.append([InlineKeyboardButton(
            text=action_text,
            callback_data=f"server_{btn_action}_{name}"
        )])

    text += "━━━━━━━━━━━━━━━━\n"
    text += "💡 <i>Включенные серверы используются\nдля создания новых подписок</i>"

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    status_text = "включен" if action == "enable" else "выключен"
    await callback.answer(f"Сервер {server_name} {status_text}", show_alert=False)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


# ============ РЕДАКТИРОВАНИЕ СЕРВЕРА ============

@router.callback_query(F.data.startswith("srv_edit_"))
async def show_edit_server_menu(callback: CallbackQuery, state: FSMContext):
    """Показать меню редактирования сервера"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    server_name = callback.data.replace("srv_edit_", "")
    config = load_servers_config()

    server = None
    for s in config.get('servers', []):
        if s.get('name') == server_name:
            server = s
            break

    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return

    domain = server.get('domain', 'N/A')
    ip = server.get('ip', 'N/A')
    port = server.get('port', 443)
    enabled = server.get('enabled', True)
    active_for_new = server.get('active_for_new', True)
    description = server.get('description', '')

    text = f"✏️ <b>РЕДАКТИРОВАНИЕ СЕРВЕРА</b>\n\n"
    text += f"📛 Имя: <b>{server_name}</b>\n"
    text += f"🌐 Домен: <code>{domain}</code>\n"
    text += f"📍 IP: <code>{ip}</code>\n"
    text += f"🔌 Порт: <code>{port}</code>\n"
    text += f"📋 Описание: {description or 'нет'}\n"
    text += f"✅ Включен: {'Да' if enabled else 'Нет'}\n"
    text += f"🆕 Для новых: {'Да' if active_for_new else 'Нет'}\n\n"

    # Показываем inbounds
    inbounds = server.get('inbounds', {})
    if inbounds:
        text += "<b>Inbounds:</b>\n"
        for inb_name, inb_data in inbounds.items():
            sni = inb_data.get('sni', 'N/A')
            prefix = inb_data.get('name_prefix', '')
            text += f"  • <b>{inb_name}</b>: SNI=<code>{sni}</code> | {prefix}\n"
    text += "\n👇 Выберите что изменить:"

    buttons = [
        [
            InlineKeyboardButton(text="📛 Имя", callback_data=f"srvedit_name_{server_name}"),
            InlineKeyboardButton(text="🌐 Домен", callback_data=f"srvedit_domain_{server_name}"),
        ],
        [
            InlineKeyboardButton(text="📍 IP", callback_data=f"srvedit_ip_{server_name}"),
            InlineKeyboardButton(text="🔌 Порт", callback_data=f"srvedit_port_{server_name}"),
        ],
        [
            InlineKeyboardButton(text="📋 Описание", callback_data=f"srvedit_desc_{server_name}"),
        ],
        [
            InlineKeyboardButton(
                text="🔄 Загрузить inbounds из панели",
                callback_data=f"srvedit_fetch_inbounds_{server_name}"
            ),
        ],
        [
            InlineKeyboardButton(
                text=f"{'❌ Выключить' if enabled else '✅ Включить'} сервер",
                callback_data=f"srvedit_toggle_enabled_{server_name}"
            ),
        ],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="srvedit_back")],
    ]

    await callback.message.edit_text(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("srvedit_fetch_inbounds_"))
async def fetch_inbounds_from_panel(callback: CallbackQuery):
    """Загрузить inbounds с панели X-UI и обновить конфиг"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    server_name = callback.data.replace("srvedit_fetch_inbounds_", "")
    config = load_servers_config()

    server = None
    server_idx = None
    for i, s in enumerate(config.get('servers', [])):
        if s.get('name') == server_name:
            server = s
            server_idx = i
            break

    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return

    panel = server.get('panel', {})
    if not panel.get('url'):
        await callback.answer("У сервера нет настроек панели", show_alert=True)
        return

    await callback.message.edit_text(f"⏳ Загружаю inbounds с панели <b>{server_name}</b>...", parse_mode="HTML")

    import json as _json
    panel_url = panel['url']
    panel_username = panel.get('username', '')
    panel_password = panel.get('password', '')

    try:
        import aiohttp
        import ssl
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        jar = aiohttp.CookieJar(unsafe=True)
        inbounds_data = {}

        async with aiohttp.ClientSession(connector=connector, cookie_jar=jar) as session:
            # Авторизация
            logged_in = False
            for auth_method in ['json', 'data']:
                kwargs = {'json' if auth_method == 'json' else 'data': {"username": panel_username, "password": panel_password}}
                async with session.post(f"{panel_url}/login", timeout=aiohttp.ClientTimeout(total=15), **kwargs) as resp:
                    if resp.status == 200:
                        result = await resp.json()
                        if result.get('success'):
                            logged_in = True
                            break

            if not logged_in:
                await callback.message.edit_text(f"❌ Не удалось авторизоваться в панели <b>{server_name}</b>", parse_mode="HTML")
                await callback.answer()
                return

            # Получаем inbounds
            async with session.get(f"{panel_url}/panel/api/inbounds/list", timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    await callback.message.edit_text("❌ Ошибка получения inbounds")
                    await callback.answer()
                    return

                response_data = await resp.json()
                if not response_data.get('success'):
                    await callback.message.edit_text("❌ API вернул ошибку")
                    await callback.answer()
                    return

                # Сохраняем существующие name_prefix
                old_inbounds = server.get('inbounds', {})

                for inbound in response_data.get('obj', []):
                    if not inbound.get('enable'):
                        continue

                    inbound_id = inbound.get('id')
                    remark = inbound.get('remark', f'inbound_{inbound_id}')
                    protocol = inbound.get('protocol', '')

                    try:
                        stream = _json.loads(inbound.get('streamSettings', '{}'))
                        settings = _json.loads(inbound.get('settings', '{}'))
                        security = stream.get('security', 'none')
                        network = stream.get('network', 'tcp')

                        flow = ''
                        for c in settings.get('clients', []):
                            if c.get('flow'):
                                flow = c['flow']
                                break

                        # Берём name_prefix из старого конфига если есть
                        old_prefix = ''
                        for old_key, old_val in old_inbounds.items():
                            if old_val.get('id') == inbound_id:
                                old_prefix = old_val.get('name_prefix', '')
                                break

                        inbound_config = {
                            "id": int(inbound_id),
                            "security": security,
                            "flow": flow,
                            "fp": "chrome",
                            "name_prefix": old_prefix or f"📶 {server_name}"
                        }

                        if security == 'reality':
                            reality = stream.get('realitySettings', {})
                            sni_list = reality.get('serverNames', [])
                            short_ids = reality.get('shortIds', [])
                            inbound_config.update({
                                "sni": sni_list[0] if sni_list else '',
                                "pbk": reality.get('settings', {}).get('publicKey', ''),
                                "sid": short_ids[0] if short_ids else '',
                                "fp": reality.get('settings', {}).get('fingerprint', 'chrome'),
                            })
                        elif security == 'tls':
                            tls = stream.get('tlsSettings', {})
                            inbound_config["sni"] = tls.get('serverName', '')

                        if network and network != 'tcp':
                            inbound_config["network"] = network

                        inbounds_data[remark] = inbound_config
                    except Exception as e:
                        logger.error(f"Ошибка парсинга inbound {inbound_id}: {e}")

        if not inbounds_data:
            await callback.message.edit_text(f"⚠️ Не найдено активных inbounds на <b>{server_name}</b>", parse_mode="HTML")
            await callback.answer()
            return

        # Первый inbound = main
        if 'main' not in inbounds_data:
            first_key = next(iter(inbounds_data))
            inbounds_data['main'] = inbounds_data.pop(first_key)

        # Сохраняем
        config['servers'][server_idx]['inbounds'] = inbounds_data
        save_servers_config(config)

        text = f"✅ <b>Inbounds обновлены для {server_name}</b>\n\n"
        for key, val in inbounds_data.items():
            sni = val.get('sni', 'N/A')
            pbk = val.get('pbk', '')
            pbk_short = f"{pbk[:15]}..." if pbk else 'N/A'
            text += f"• <b>{key}</b> (ID: {val.get('id')})\n"
            text += f"  SNI: <code>{sni}</code>\n"
            text += f"  PBK: <code>{pbk_short}</code>\n"
            text += f"  Flow: {val.get('flow') or 'нет'}\n"
            text += f"  FP: {val.get('fp', 'chrome')}\n\n"

        await callback.message.edit_text(text, parse_mode="HTML")
        await callback.answer("Inbounds обновлены!")

    except Exception as e:
        logger.error(f"Ошибка загрузки inbounds для {server_name}: {e}")
        await callback.message.edit_text(f"❌ Ошибка: {str(e)[:200]}")
        await callback.answer()


@router.callback_query(F.data.startswith("srvedit_toggle_enabled_"))
async def toggle_server_enabled(callback: CallbackQuery):
    """Переключить enabled/disabled сервер"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    server_name = callback.data.replace("srvedit_toggle_enabled_", "")
    config = load_servers_config()

    for s in config.get('servers', []):
        if s.get('name') == server_name:
            s['enabled'] = not s.get('enabled', True)
            new_status = s['enabled']
            save_servers_config(config)
            await callback.answer(f"Сервер {'включен' if new_status else 'выключен'}")
            # Перерисовываем меню
            callback.data = f"srv_edit_{server_name}"
            await show_edit_server_menu(callback, None)
            return

    await callback.answer("Сервер не найден", show_alert=True)


@router.callback_query(F.data.startswith("srvedit_name_") | F.data.startswith("srvedit_domain_") |
                        F.data.startswith("srvedit_ip_") | F.data.startswith("srvedit_port_") |
                        F.data.startswith("srvedit_desc_"))
async def start_edit_server_field(callback: CallbackQuery, state: FSMContext):
    """Начать редактирование поля сервера"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    # Парсим: srvedit_{field}_{server_name}
    parts = callback.data.split("_", 2)
    field = parts[1]
    server_name = callback.data.split(f"srvedit_{field}_", 1)[1]

    field_labels = {
        "name": "название",
        "domain": "домен",
        "ip": "IP адрес",
        "port": "порт",
        "desc": "описание",
    }

    config = load_servers_config()
    current_value = ""
    for s in config.get('servers', []):
        if s.get('name') == server_name:
            if field == "desc":
                current_value = s.get('description', '')
            elif field == "name":
                current_value = s.get('name', '')
            else:
                current_value = str(s.get(field, ''))
            break

    await state.set_state(EditServerStates.waiting_for_field_value)
    await state.update_data(edit_server_name=server_name, edit_field=field)

    await callback.message.edit_text(
        f"✏️ <b>Редактирование {field_labels.get(field, field)}</b>\n\n"
        f"Сервер: <b>{server_name}</b>\n"
        f"Текущее значение: <code>{current_value or 'не задано'}</code>\n\n"
        f"Введите новое значение или /cancel для отмены:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(EditServerStates.waiting_for_field_value)
async def process_edit_server_field(message: Message, state: FSMContext):
    """Обработка нового значения поля сервера"""
    if message.from_user.id != ADMIN_ID:
        return

    text = message.text.strip()

    if text == "/cancel":
        await state.clear()
        await message.answer("❌ Редактирование отменено.", reply_markup=Keyboards.admin_menu())
        return

    data = await state.get_data()
    server_name = data.get('edit_server_name')
    field = data.get('edit_field')

    config = load_servers_config()
    server_found = False
    old_name = server_name

    for s in config.get('servers', []):
        if s.get('name') == server_name:
            if field == "name":
                s['name'] = text
            elif field == "domain":
                s['domain'] = text
            elif field == "ip":
                s['ip'] = text
            elif field == "port":
                try:
                    s['port'] = int(text)
                except ValueError:
                    await message.answer("❌ Порт должен быть числом. Попробуйте ещё раз:")
                    return
            elif field == "desc":
                s['description'] = text
            server_found = True
            break

    if not server_found:
        await state.clear()
        await message.answer("❌ Сервер не найден.", reply_markup=Keyboards.admin_menu())
        return

    save_servers_config(config)
    await state.clear()

    field_labels = {
        "name": "Название",
        "domain": "Домен",
        "ip": "IP",
        "port": "Порт",
        "desc": "Описание",
    }

    await message.answer(
        f"✅ {field_labels.get(field, field)} сервера <b>{old_name}</b> изменён на:\n"
        f"<code>{text}</code>",
        parse_mode="HTML",
        reply_markup=Keyboards.admin_menu()
    )


@router.callback_query(F.data == "srvedit_back")
async def edit_server_back(callback: CallbackQuery, state: FSMContext):
    """Назад из редактирования сервера"""
    await state.clear()
    await callback.message.delete()
    await callback.answer()


# ============ ДОБАВЛЕНИЕ НОВОГО СЕРВЕРА ============

@router.callback_query(F.data == "add_new_server")
async def start_add_server(callback: CallbackQuery, state: FSMContext):
    """Начать процесс добавления нового сервера"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Нет доступа", show_alert=True)
        return

    await callback.message.edit_text(
        "➕ <b>ДОБАВЛЕНИЕ НОВОГО СЕРВЕРА</b>\n\n"
        "Шаг 1/5: Введите <b>название</b> сервера\n"
        "(например: Germany-1, NL-Premium)\n\n"
        "Для отмены нажмите /cancel",
        parse_mode="HTML"
    )
    await state.set_state(AddServerStates.waiting_name)
    await callback.answer()


@router.message(AddServerStates.waiting_name)
async def process_server_name(message: Message, state: FSMContext):
    """Обработка названия сервера"""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление сервера отменено", reply_markup=Keyboards.admin_menu())
        return

    name = message.text.strip()

    # Проверяем уникальность имени
    config = load_servers_config()
    existing_names = [s.get('name', '').lower() for s in config.get('servers', [])]
    if name.lower() in existing_names:
        await message.answer(
            f"❌ Сервер с именем <b>{name}</b> уже существует.\n"
            "Введите другое название:",
            parse_mode="HTML"
        )
        return

    await state.update_data(name=name)
    await message.answer(
        f"✅ Название: <b>{name}</b>\n\n"
        "Шаг 2/5: Введите <b>IP адрес</b> сервера\n"
        "(например: 80.76.43.74)",
        parse_mode="HTML"
    )
    await state.set_state(AddServerStates.waiting_ip)


@router.message(AddServerStates.waiting_ip)
async def process_server_ip(message: Message, state: FSMContext):
    """Обработка IP адреса"""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление сервера отменено", reply_markup=Keyboards.admin_menu())
        return

    ip = message.text.strip()

    # Простая валидация IP
    import re
    ip_pattern = r'^(\d{1,3}\.){3}\d{1,3}$'
    if not re.match(ip_pattern, ip):
        await message.answer(
            "❌ Некорректный IP адрес.\n"
            "Введите в формате: xxx.xxx.xxx.xxx"
        )
        return

    await state.update_data(ip=ip)
    await message.answer(
        f"✅ IP: <b>{ip}</b>\n\n"
        "Шаг 3/5: Введите <b>домен</b> сервера\n"
        "(например: vpn.example.com)\n\n"
        "Или отправьте <b>-</b> если домена нет",
        parse_mode="HTML"
    )
    await state.set_state(AddServerStates.waiting_domain)


@router.message(AddServerStates.waiting_domain)
async def process_server_domain(message: Message, state: FSMContext):
    """Обработка домена"""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление сервера отменено", reply_markup=Keyboards.admin_menu())
        return

    domain = message.text.strip()
    data = await state.get_data()

    # Если домен пустой, "-" или совпадает с IP - используем IP как домен
    if not domain or domain == "-" or domain == data.get('ip', ''):
        domain = data.get('ip', '')

    await state.update_data(domain=domain)
    await message.answer(
        f"✅ Домен: <b>{domain}</b>\n\n"
        "Шаг 4/5: Введите <b>URL панели X-UI</b>\n"
        "(например: https://80.76.43.74:1020/AMYmhoyf5gRI0qS)\n\n"
        "Полный URL до /panel/inbounds",
        parse_mode="HTML"
    )
    await state.set_state(AddServerStates.waiting_panel_path)


@router.message(AddServerStates.waiting_panel_path)
async def process_panel_path(message: Message, state: FSMContext):
    """Обработка URL панели"""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление сервера отменено", reply_markup=Keyboards.admin_menu())
        return

    panel_url = message.text.strip()

    # Парсим URL панели
    from urllib.parse import urlparse
    parsed = urlparse(panel_url)

    if not parsed.scheme or not parsed.netloc:
        await message.answer(
            "❌ Некорректный URL.\n"
            "Введите полный URL, например:\n"
            "<code>https://80.76.43.74:1020/AMYmhoyf5gRI0qS</code>",
            parse_mode="HTML"
        )
        return

    # Извлекаем порт (по умолчанию 443 для https, 80 для http)
    panel_port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    panel_path = parsed.path.rstrip('/') or '/'

    # Убираем лишние части URL (/panel/inbounds, /panel, etc.)
    for suffix in ['/panel/inbounds', '/panel/api', '/panel', '/inbounds']:
        if panel_path.endswith(suffix):
            panel_path = panel_path[:-len(suffix)]
            break

    # Формируем чистый URL
    panel_url = f"{parsed.scheme}://{parsed.hostname}:{panel_port}{panel_path}"

    await state.update_data(panel_url=panel_url, panel_port=panel_port, panel_path=panel_path)
    await message.answer(
        f"✅ URL панели: <code>{panel_url}</code>\n"
        f"   Порт: {panel_port}\n"
        f"   Путь: {panel_path}\n\n"
        "Шаг 5/5: Введите <b>логин и пароль</b> от панели X-UI\n"
        "в формате: логин пароль\n"
        "(например: admin MyPassword123)",
        parse_mode="HTML"
    )
    await state.set_state(AddServerStates.waiting_panel_credentials)


@router.message(AddServerStates.waiting_panel_credentials)
async def process_panel_credentials(message: Message, state: FSMContext):
    """Обработка учётных данных панели"""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление сервера отменено", reply_markup=Keyboards.admin_menu())
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "❌ Введите логин и пароль через пробел\n"
            "Например: admin MyPassword123"
        )
        return

    panel_username, panel_password = parts

    # Удаляем сообщение с паролем
    try:
        await message.delete()
    except:
        pass

    await state.update_data(panel_username=panel_username, panel_password=panel_password)

    # Запрашиваем name_prefix для подписки
    data = await state.get_data()
    default_prefix = f"📶 {data['name']}"

    await message.answer(
        "📝 <b>Имя сервера в подписке</b>\n\n"
        "Это имя будет отображаться в VPN-приложении клиента.\n\n"
        f"По умолчанию: <code>{default_prefix}</code>\n\n"
        "Введите имя или отправьте <b>+</b> чтобы использовать по умолчанию:",
        parse_mode="HTML"
    )
    await state.set_state(AddServerStates.waiting_name_prefix)


@router.message(AddServerStates.waiting_name_prefix)
async def process_name_prefix(message: Message, state: FSMContext):
    """Обработка имени сервера для подписки"""
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Добавление сервера отменено", reply_markup=Keyboards.admin_menu())
        return

    data = await state.get_data()

    if message.text.strip() == "+":
        name_prefix = f"📶 {data['name']}"
    else:
        name_prefix = message.text.strip()

    await state.update_data(name_prefix=name_prefix)

    text = (
        "📋 <b>ПРОВЕРЬТЕ ДАННЫЕ СЕРВЕРА</b>\n\n"
        f"📛 Название: <b>{data['name']}</b>\n"
        f"🌐 IP: <code>{data['ip']}</code>\n"
        f"🔗 Домен: <code>{data['domain']}</code>\n"
        f"🖥 Панель: <code>{data.get('panel_url', '')}</code>\n"
        f"👤 Логин: {data['panel_username']}\n"
        f"📝 В подписке: <b>{name_prefix}</b>\n\n"
        "Всё верно?"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_add_server"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add_server")
        ],
        [InlineKeyboardButton(text="🔄 Проверить подключение", callback_data="test_server_connection")]
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await state.set_state(AddServerStates.confirm)


@router.callback_query(F.data == "test_server_connection", AddServerStates.confirm)
async def test_server_connection(callback: CallbackQuery, state: FSMContext):
    """Тестирование подключения к панели"""
    data = await state.get_data()
    panel_url = data.get('panel_url', '')
    panel_username = data.get('panel_username')
    panel_password = data.get('panel_password')

    await callback.message.edit_text("⏳ Проверяю подключение к панели...")

    results = {"panel_auth": False, "inbounds": False, "inbounds_count": 0}

    try:
        import aiohttp
        import ssl
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(connector=connector, cookie_jar=jar) as session:
            # Тест авторизации (form-data, не JSON!)
            login_url = f"{panel_url}/login"
            async with session.post(login_url, data={"username": panel_username, "password": panel_password}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    response_data = await resp.json()
                    results['panel_auth'] = response_data.get('success', False)

            # Если авторизация успешна, проверяем inbound'ы
            if results['panel_auth']:
                inbounds_url = f"{panel_url}/panel/api/inbounds/list"
                async with session.get(inbounds_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        inbounds_data = await resp.json()
                        if inbounds_data.get('success'):
                            results['inbounds'] = True
                            results['inbounds_count'] = len(inbounds_data.get('obj', []))
    except Exception as e:
        logger.error(f"Ошибка проверки подключения: {e}")

    # Формируем результат
    text = (
        "🔍 <b>РЕЗУЛЬТАТЫ ПРОВЕРКИ</b>\n\n"
        f"{'✅' if results['panel_auth'] else '❌'} Авторизация в панели\n"
        f"{'✅' if results['inbounds'] else '❌'} Доступ к inbound'ам"
    )

    if results['inbounds']:
        text += f" ({results['inbounds_count']} шт.)"

    text += "\n\n"

    if results['panel_auth'] and results['inbounds']:
        text += "✅ <b>Панель доступна!</b>"
    else:
        text += "⚠️ <b>Есть проблемы с подключением</b>"

    # Данные сервера
    text += (
        f"\n\n━━━━━━━━━━━━━━━━\n"
        f"📛 Название: <b>{data['name']}</b>\n"
        f"🌐 IP: <code>{data['ip']}</code>\n"
        f"🔗 Домен: <code>{data['domain']}</code>\n"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_add_server"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add_server")
        ],
        [InlineKeyboardButton(text="🔄 Повторить проверку", callback_data="test_server_connection")]
    ])

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "confirm_add_server", AddServerStates.confirm)
async def confirm_add_server(callback: CallbackQuery, state: FSMContext):
    """Подтверждение и сохранение сервера"""
    data = await state.get_data()

    await callback.message.edit_text("⏳ Получаю данные inbound'ов с панели...")

    panel_url = data.get('panel_url', '')
    panel_username = data.get('panel_username')
    panel_password = data.get('panel_password')

    inbounds_data = {}

    try:
        import aiohttp
        import ssl
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        connector = aiohttp.TCPConnector(ssl=ssl_context)
        jar = aiohttp.CookieJar(unsafe=True)
        async with aiohttp.ClientSession(connector=connector, cookie_jar=jar) as session:
            # Авторизация (JSON, как в xui_client)
            login_url = f"{panel_url}/login"
            logged_in = False
            async with session.post(login_url, json={"username": panel_username, "password": panel_password}, timeout=aiohttp.ClientTimeout(total=15)) as login_resp:
                if login_resp.status == 200:
                    login_data = await login_resp.json()
                    if login_data.get('success'):
                        logged_in = True
                        logger.info(f"Авторизация в панели успешна (JSON)")
                    else:
                        logger.error(f"Авторизация в панели не удалась (JSON): {login_data.get('msg')}")
                else:
                    logger.error(f"Ошибка авторизации в панели (JSON): статус {login_resp.status}")

            # Если JSON не сработал, пробуем form-data
            if not logged_in:
                logger.info("Пробуем авторизацию через form-data...")
                async with session.post(login_url, data={"username": panel_username, "password": panel_password}, timeout=aiohttp.ClientTimeout(total=15)) as login_resp:
                    if login_resp.status == 200:
                        login_data = await login_resp.json()
                        if login_data.get('success'):
                            logged_in = True
                            logger.info(f"Авторизация в панели успешна (form-data)")
                        else:
                            logger.error(f"Авторизация в панели не удалась (form-data): {login_data.get('msg')}")
                    else:
                        logger.error(f"Ошибка авторизации в панели (form-data): статус {login_resp.status}")

            if not logged_in:
                logger.error("Не удалось авторизоваться в панели ни одним способом")
            else:
                # Получаем inbound'ы через API только если авторизация успешна
                inbounds_url = f"{panel_url}/panel/api/inbounds/list"
                async with session.get(inbounds_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status == 200:
                        response_data = await resp.json()
                        if response_data.get('success'):
                            for inbound in response_data.get('obj', []):
                                if not inbound.get('enable'):
                                    continue

                                inbound_id = inbound.get('id')
                                remark = inbound.get('remark', f'inbound_{inbound_id}')
                                port = inbound.get('port')
                                protocol = inbound.get('protocol')

                                try:
                                    stream_settings = json.loads(inbound.get('streamSettings', '{}'))
                                    security = stream_settings.get('security', 'none')
                                    network = stream_settings.get('network', 'tcp')
                                    settings = json.loads(inbound.get('settings', '{}'))

                                    # Извлекаем flow из существующих клиентов
                                    flow = ''
                                    for c in settings.get('clients', []):
                                        if c.get('flow'):
                                            flow = c.get('flow')
                                            break

                                    # Используем name_prefix из FSM (введённый пользователем)
                                    user_prefix = data.get('name_prefix', f"📶 {data['name']}")
                                    inbound_config = {
                                        "id": int(inbound_id),
                                        "security": security,
                                        "flow": flow,
                                        "fp": "chrome",
                                        "name_prefix": user_prefix
                                    }

                                    if security == 'reality':
                                        reality = stream_settings.get('realitySettings', {})
                                        sni_list = reality.get('serverNames', [])
                                        short_ids = reality.get('shortIds', [])
                                        inbound_config.update({
                                            "sni": sni_list[0] if sni_list else '',
                                            "pbk": reality.get('settings', {}).get('publicKey', ''),
                                            "sid": short_ids[0] if short_ids else '',
                                            "fp": reality.get('settings', {}).get('fingerprint', 'chrome'),
                                        })
                                    elif security == 'tls':
                                        tls = stream_settings.get('tlsSettings', {})
                                        inbound_config["sni"] = tls.get('serverName', '')

                                    # Добавляем network если не tcp
                                    if network and network != 'tcp':
                                        inbound_config["network"] = network

                                    inbounds_data[remark] = inbound_config
                                except Exception as parse_err:
                                    logger.error(f"Ошибка парсинга inbound {inbound_id} ({remark}): {parse_err}")
                        else:
                            logger.error(f"API вернул ошибку: {response_data.get('msg')}")
                    else:
                        logger.error(f"Ошибка получения inbound'ов: статус {resp.status}")
    except Exception as e:
        logger.error(f"Ошибка получения inbound'ов через API: {e}")

    # Первый inbound (или единственный) всегда должен быть "main"
    if inbounds_data and 'main' not in inbounds_data:
        first_key = next(iter(inbounds_data))
        inbounds_data['main'] = inbounds_data.pop(first_key)

    # Добавляем network: tcp для всех inbound'ов где network не указан
    for key, val in inbounds_data.items():
        if 'network' not in val:
            val['network'] = 'tcp'

    # Если inbound'ы не определены — НЕ сохраняем, предлагаем повторить
    if not inbounds_data:
        await callback.message.edit_text(
            f"⚠️ <b>НЕ УДАЛОСЬ ОПРЕДЕЛИТЬ INBOUND'Ы</b>\n\n"
            f"📛 Сервер: <b>{data['name']}</b>\n"
            f"🌐 IP: <code>{data['ip']}</code>\n"
            f"🖥 Панель: <code>{panel_url}</code>\n\n"
            "Возможные причины:\n"
            "• Панель недоступна\n"
            "• Неверные логин/пароль\n"
            "• Нет активных inbound'ов на панели\n\n"
            "Сервер <b>НЕ сохранён</b>. Выберите действие:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Повторить попытку", callback_data="confirm_add_server")],
                [InlineKeyboardButton(text="💾 Сохранить без inbound'ов", callback_data="force_save_server")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_add_server")]
            ])
        )
        await callback.answer()
        return

    # Создаём конфигурацию сервера (без SSH)
    new_server = {
        "name": data['name'],
        "domain": data['domain'],
        "ip": data['ip'],
        "port": 443,
        "enabled": True,
        "active_for_new": True,
        "local": False,
        "description": f"Сервер {data['name']}",
        "panel": {
            "url": panel_url,
            "port": data.get('panel_port', 1020),
            "path": data.get('panel_path', '/'),
            "username": panel_username,
            "password": panel_password
        },
        "inbounds": inbounds_data
    }

    # Сохраняем в конфиг
    config = load_servers_config()
    config['servers'].append(new_server)
    save_servers_config(config)

    await state.clear()

    inbounds_info = f"\n\n📋 Найдено inbound'ов: {len(inbounds_data)}\n"
    for key, val in inbounds_data.items():
        sni = val.get('sni', 'N/A')
        pbk_short = val.get('pbk', '')[:12] + '...' if val.get('pbk') else 'N/A'
        flow = val.get('flow', '') or '-'
        fp = val.get('fp', 'chrome')
        inbounds_info += f"   • <b>{key}</b>: SNI={sni}, fp={fp}\n"
        inbounds_info += f"     pbk={pbk_short}, flow={flow}\n"

    await callback.message.edit_text(
        f"✅ <b>СЕРВЕР ДОБАВЛЕН</b>\n\n"
        f"📛 Название: <b>{data['name']}</b>\n"
        f"🌐 IP: <code>{data['ip']}</code>\n"
        f"🔗 Домен: <code>{data['domain']}</code>\n"
        f"🖥 Панель: <code>{panel_url}</code>\n"
        f"{inbounds_info}",
        parse_mode="HTML"
    )

    await callback.message.answer(
        "Сервер добавлен в конфигурацию.\n"
        "Используйте 🖥 Статус серверов для управления.",
        reply_markup=Keyboards.admin_menu()
    )
    await callback.answer("Сервер успешно добавлен!")


@router.callback_query(F.data == "force_save_server", AddServerStates.confirm)
async def force_save_server(callback: CallbackQuery, state: FSMContext):
    """Принудительное сохранение сервера без inbound'ов"""
    data = await state.get_data()
    panel_url = data.get('panel_url', '')

    new_server = {
        "name": data['name'],
        "domain": data['domain'],
        "ip": data['ip'],
        "port": 443,
        "enabled": True,
        "active_for_new": False,  # Не активен для новых — inbound'ы не настроены
        "local": False,
        "description": f"Сервер {data['name']}",
        "panel": {
            "url": panel_url,
            "port": data.get('panel_port', 1020),
            "path": data.get('panel_path', '/'),
            "username": data.get('panel_username'),
            "password": data.get('panel_password')
        },
        "inbounds": {}
    }

    config = load_servers_config()
    config['servers'].append(new_server)
    save_servers_config(config)

    await state.clear()

    await callback.message.edit_text(
        f"⚠️ <b>СЕРВЕР СОХРАНЁН БЕЗ INBOUND'ОВ</b>\n\n"
        f"📛 Название: <b>{data['name']}</b>\n"
        f"🌐 IP: <code>{data['ip']}</code>\n"
        f"🖥 Панель: <code>{panel_url}</code>\n\n"
        f"⚠️ <code>active_for_new: false</code> — сервер не будет использоваться для новых ключей.\n"
        f"Настройте inbound'ы вручную или через 🖥 Статус серверов.",
        parse_mode="HTML"
    )
    await callback.message.answer(
        "Сервер добавлен (без inbound'ов).",
        reply_markup=Keyboards.admin_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "cancel_add_server")
async def cancel_add_server(callback: CallbackQuery, state: FSMContext):
    """Отмена добавления сервера"""
    await state.clear()
    await callback.message.edit_text("❌ Добавление сервера отменено")
    await callback.message.answer("Главное меню:", reply_markup=Keyboards.admin_menu())
    await callback.answer()


@router.message(F.text == "/pending")
@admin_only
async def show_pending_keys(message: Message, db: DatabaseManager, **kwargs):
    """Показать отложенные ключи в очереди на создание"""
    stats = await db.get_pending_keys_count()
    pending_keys = await db.get_pending_keys(limit=10)

    text = "⏳ <b>ОЧЕРЕДЬ ОТЛОЖЕННЫХ КЛЮЧЕЙ</b>\n\n"
    text += f"📊 <b>Статистика:</b>\n"
    text += f"   • В ожидании: {stats['pending']}\n"
    text += f"   • Создано: {stats['completed']}\n"
    text += f"   • Не удалось: {stats['failed']}\n\n"

    if pending_keys:
        text += "📋 <b>Ключи в очереди:</b>\n"
        for pk in pending_keys:
            text += f"\n🔑 #{pk['id']} | <code>{pk['phone']}</code>\n"
            text += f"   👤 User: {pk['telegram_id']} (@{pk['username'] or 'N/A'})\n"
            text += f"   📦 Тариф: {pk['period_name']}\n"
            text += f"   🔄 Попыток: {pk['retry_count']}/{pk['max_retries']}\n"
            if pk['last_error']:
                text += f"   ❌ Ошибка: {pk['last_error'][:50]}...\n"
    else:
        text += "✅ <i>Очередь пуста</i>"

    text += "\n\n💡 <i>Retry каждые 2 минуты автоматически</i>"

    await message.answer(text, parse_mode="HTML")


# ============ ДОБАВЛЕНИЕ СЕРВЕРА В ПОДПИСКУ ============

@router.message(F.text == "📡 Добавить сервер")
@admin_only
async def start_add_server_to_sub(message: Message, state: FSMContext, **kwargs):
    """Начало добавления сервера в подписку клиента"""
    await state.clear()
    await state.set_state(AddToSubscriptionStates.waiting_for_search)
    await message.answer(
        "📡 <b>ДОБАВИТЬ СЕРВЕР В ПОДПИСКУ</b>\n\n"
        "Введите номер телефона, email или UUID клиента для поиска.\n\n"
        "Примеры:\n"
        "• <code>79001234567</code>\n"
        "• <code>Иван</code>\n\n"
        "Или нажмите 'Отмена' для возврата.",
        parse_mode="HTML",
        reply_markup=Keyboards.cancel()
    )


@router.message(AddToSubscriptionStates.waiting_for_search, F.text == "Отмена")
async def cancel_add_server_to_sub(message: Message, state: FSMContext):
    """Отмена добавления сервера"""
    await state.clear()
    await message.answer(
        "Операция отменена.",
        reply_markup=Keyboards.admin_menu()
    )


@router.message(AddToSubscriptionStates.waiting_for_search)
async def process_add_sub_search(message: Message, state: FSMContext, **kwargs):
    """Обработка поискового запроса для добавления сервера"""
    query = message.text.strip()

    # Если пользователь нажал кнопку меню - выходим из режима поиска
    admin_menu_buttons = {
        "📡 Добавить сервер", "📡 Сервер → всем", "🔑 Создать ключ (выбор inbound)",
        "Добавить менеджера", "Список менеджеров", "Общая статистика",
        "Детальная статистика", "💰 Изменить цены", "🔍 Поиск ключа",
        "📅 Продлить подписку",
        "🗑️ Удалить ключ", "📢 Отправить уведомление", "🌐 Управление SNI",
        "💳 Реквизиты", "📋 Веб-заказы", "🖥 Статус серверов", "🔧 Панели X-UI", "💳 Оплата серверов",
        "🌐 Админ-панель сайта",
        "Назад", "Панель администратора", "Создать ключ", "🔄 Замена ключа",
        "🔧 Исправить ключ", "💰 Прайс", "Моя статистика",
    }
    if query in admin_menu_buttons:
        await state.clear()
        await message.answer(
            "Операция отменена.",
            reply_markup=Keyboards.admin_menu()
        )
        return

    if len(query) < 2:
        await message.answer("❌ Введите минимум 2 символа для поиска.")
        return

    status_msg = await message.answer("🔍 Поиск клиента на серверах...")

    xui_clients = await search_clients_on_servers(query)

    if not xui_clients:
        await status_msg.edit_text(
            f"🔍 По запросу «<b>{query}</b>» ничего не найдено на серверах.\n\n"
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
        clients_by_uuid[uuid]['servers'].append(client.get('server', 'Unknown'))

    unique_clients = list(clients_by_uuid.values())

    if not unique_clients:
        await status_msg.edit_text(
            f"🔍 По запросу «<b>{query}</b>» ничего не найдено.",
            parse_mode="HTML"
        )
        return

    # Сохраняем в FSM для следующего шага
    await state.update_data(search_results=unique_clients)

    text = f"🔍 <b>Найдено клиентов:</b> {len(unique_clients)}\n\n"
    buttons = []

    for idx, client in enumerate(unique_clients[:10]):
        email = client['email']
        uuid_short = client['uuid'][:8] + '...'
        servers_str = ', '.join(client['servers'])
        expiry_time = client.get('expiry_time', 0)

        if expiry_time > 0:
            from datetime import datetime
            expiry_dt = datetime.fromtimestamp(expiry_time / 1000)
            expiry_str = expiry_dt.strftime("%d.%m.%Y")
        else:
            expiry_str = "Безлимит"

        sub_url = f"https://{_get_sub_domain(kwargs)}/sub/{client['uuid']}"

        text += f"{idx + 1}. <b>{email}</b>\n"
        text += f"   🔑 UUID: <code>{uuid_short}</code>\n"
        text += f"   🖥 Серверы: {servers_str}\n"
        text += f"   ⏰ Истекает: {expiry_str}\n"
        text += f"   📱 Подписка: <code>{sub_url}</code>\n\n"

        buttons.append([InlineKeyboardButton(
            text=f"📡 {email[:30]}",
            callback_data=f"addsub_sel_{idx}"
        )])

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="addsub_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await status_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data.startswith("addsub_sel_"))
async def select_client_for_add(callback: CallbackQuery, state: FSMContext):
    """Выбор клиента — показ серверов где он есть и где нет"""
    from bot.api.remote_xui import find_client_presence_on_all_servers
    from datetime import datetime

    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    search_results = data.get('search_results', [])

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

    # Берём expiry и ip_limit из первого найденного сервера
    expiry_time_ms = 0
    ip_limit = 2
    if found_on:
        expiry_time_ms = found_on[0].get('expiry_time', 0)
        ip_limit = found_on[0].get('ip_limit', 2)

    # Сохраняем в FSM
    available_servers = []
    for srv in not_found_on:
        available_servers.append({
            'server_name': srv['server_name'],
            'name_prefix': srv.get('name_prefix', srv['server_name']),
            'server_config': srv['server_config']
        })

    await state.update_data(
        client_uuid=client_uuid,
        client_email=email,
        expiry_time_ms=expiry_time_ms,
        ip_limit=ip_limit,
        available_servers=available_servers,
        selected_server_indices=[],
        admin_total_gb=None
    )
    await state.set_state(AddToSubscriptionStates.waiting_for_server_select)

    # Формируем текст
    text = f"📡 <b>Клиент:</b> <code>{email}</code>\n"
    text += f"🔑 UUID: <code>{client_uuid[:8]}...</code>\n\n"

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

    if not not_found_on:
        text += "🎉 <b>Клиент уже на всех серверах!</b>"
        buttons = [
            [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="addsub_newsearch")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data="addsub_cancel")]
        ]
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
        return

    text += "<b>➕ Доступные серверы:</b>\n"
    for srv in not_found_on:
        prefix = srv.get('name_prefix', '')
        label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        text += f"  ➕ {label}\n"
    text += "\nВыберите серверы для добавления:"

    # Кнопки серверов
    buttons = []
    for idx, srv in enumerate(not_found_on):
        prefix = srv.get('name_prefix', '')
        btn_label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        buttons.append([InlineKeyboardButton(
            text=f"➕ {btn_label}",
            callback_data=f"addsub_srv_{idx}"
        )])

    if len(not_found_on) > 1:
        buttons.append([InlineKeyboardButton(
            text="📡 Добавить на ВСЕ",
            callback_data="addsub_all"
        )])

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="addsub_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(AddToSubscriptionStates.waiting_for_server_select, F.data.startswith("addsub_srv_"))
async def pick_server_toggle(callback: CallbackQuery, state: FSMContext):
    """Переключение выбора конкретного сервера"""
    from datetime import datetime

    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    selected = data.get('selected_server_indices', [])
    available = data.get('available_servers', [])
    found_expiry = data.get('expiry_time_ms', 0)
    email = data.get('client_email', '')
    client_uuid = data.get('client_uuid', '')

    if idx >= len(available):
        await callback.answer("Ошибка")
        return

    # Toggle
    if idx in selected:
        selected.remove(idx)
    else:
        selected.append(idx)

    await state.update_data(selected_server_indices=selected)

    # Обновляем клавиатуру
    buttons = []
    for i, srv in enumerate(available):
        mark = "✅" if i in selected else "➕"
        prefix = srv.get('name_prefix', '')
        btn_label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {btn_label}",
            callback_data=f"addsub_srv_{i}"
        )])

    if len(available) > 1:
        buttons.append([InlineKeyboardButton(
            text="📡 Добавить на ВСЕ",
            callback_data="addsub_all"
        )])

    if selected:
        buttons.append([InlineKeyboardButton(
            text=f"✅ Подтвердить ({len(selected)})",
            callback_data="addsub_go"
        )])

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="addsub_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    # Обновляем текст
    text = f"📡 <b>Клиент:</b> <code>{email}</code>\n"
    text += f"🔑 UUID: <code>{client_uuid[:8]}...</code>\n\n"
    text += "<b>Выбранные серверы:</b>\n"
    for i, srv in enumerate(available):
        mark = "✅" if i in selected else "➕"
        prefix = srv.get('name_prefix', '')
        label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        text += f"  {mark} {label}\n"

    if found_expiry > 0:
        exp_str = datetime.fromtimestamp(found_expiry / 1000).strftime("%d.%m.%Y")
        text += f"\n⏰ Срок: до {exp_str}"
    else:
        text += "\n⏰ Срок: Безлимит"

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(AddToSubscriptionStates.waiting_for_server_select, F.data == "addsub_all")
async def pick_all_servers(callback: CallbackQuery, state: FSMContext):
    """Выбрать все доступные серверы и перейти к подтверждению"""
    from datetime import datetime

    data = await state.get_data()
    available = data.get('available_servers', [])
    email = data.get('client_email', '')
    client_uuid = data.get('client_uuid', '')
    expiry_time_ms = data.get('expiry_time_ms', 0)

    selected = list(range(len(available)))
    await state.update_data(selected_server_indices=selected)
    await state.set_state(AddToSubscriptionStates.confirming)

    # Формируем подтверждение
    text = f"📡 <b>Подтверждение</b>\n\n"
    text += f"Клиент: <code>{email}</code>\n"
    text += f"UUID: <code>{client_uuid[:8]}...</code>\n\n"
    text += "<b>Добавить на серверы:</b>\n"
    for srv in available:
        prefix = srv.get('name_prefix', '')
        label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        text += f"  • {label}\n"

    if expiry_time_ms > 0:
        exp_str = datetime.fromtimestamp(expiry_time_ms / 1000).strftime("%d.%m.%Y")
        text += f"\n⏰ Срок: до {exp_str}"
    else:
        text += "\n⏰ Срок: Безлимит"

    now_ms = int(datetime.now().timestamp() * 1000)
    if expiry_time_ms > 0 and expiry_time_ms < now_ms:
        text += "\n⚠️ <i>Внимание: ключ просрочен!</i>"

    buttons = [
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="addsub_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="addsub_cancel")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(AddToSubscriptionStates.waiting_for_server_select, F.data == "addsub_go")
async def go_to_confirm(callback: CallbackQuery, state: FSMContext):
    """Перейти к подтверждению выбранных серверов"""
    from datetime import datetime

    data = await state.get_data()
    selected = data.get('selected_server_indices', [])
    available = data.get('available_servers', [])
    email = data.get('client_email', '')
    client_uuid = data.get('client_uuid', '')
    expiry_time_ms = data.get('expiry_time_ms', 0)

    if not selected:
        await callback.answer("Выберите хотя бы один сервер")
        return

    await state.set_state(AddToSubscriptionStates.confirming)

    selected_servers = [available[i] for i in selected if i < len(available)]

    text = f"📡 <b>Подтверждение</b>\n\n"
    text += f"Клиент: <code>{email}</code>\n"
    text += f"UUID: <code>{client_uuid[:8]}...</code>\n\n"
    text += "<b>Добавить на серверы:</b>\n"
    for srv in selected_servers:
        prefix = srv.get('name_prefix', '')
        label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        text += f"  • {label}\n"

    if expiry_time_ms > 0:
        exp_str = datetime.fromtimestamp(expiry_time_ms / 1000).strftime("%d.%m.%Y")
        text += f"\n⏰ Срок: до {exp_str}"
    else:
        text += "\n⏰ Срок: Безлимит"

    now_ms = int(datetime.now().timestamp() * 1000)
    if expiry_time_ms > 0 and expiry_time_ms < now_ms:
        text += "\n⚠️ <i>Внимание: ключ просрочен!</i>"

    buttons = [
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="addsub_confirm")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="addsub_back")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="addsub_cancel")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(AddToSubscriptionStates.confirming, F.data == "addsub_back")
async def back_to_server_select(callback: CallbackQuery, state: FSMContext):
    """Назад к выбору серверов"""
    await state.set_state(AddToSubscriptionStates.waiting_for_server_select)
    # Симулируем нажатие на toggle чтобы перерисовать экран
    data = await state.get_data()
    selected = data.get('selected_server_indices', [])
    available = data.get('available_servers', [])
    email = data.get('client_email', '')
    client_uuid = data.get('client_uuid', '')
    expiry_time_ms = data.get('expiry_time_ms', 0)

    from datetime import datetime

    buttons = []
    for i, srv in enumerate(available):
        mark = "✅" if i in selected else "➕"
        prefix = srv.get('name_prefix', '')
        btn_label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        buttons.append([InlineKeyboardButton(
            text=f"{mark} {btn_label}",
            callback_data=f"addsub_srv_{i}"
        )])

    if len(available) > 1:
        buttons.append([InlineKeyboardButton(
            text="📡 Добавить на ВСЕ",
            callback_data="addsub_all"
        )])

    if selected:
        buttons.append([InlineKeyboardButton(
            text=f"✅ Подтвердить ({len(selected)})",
            callback_data="addsub_go"
        )])

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="addsub_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = f"📡 <b>Клиент:</b> <code>{email}</code>\n"
    text += f"🔑 UUID: <code>{client_uuid[:8]}...</code>\n\n"
    text += "<b>Выбранные серверы:</b>\n"
    for i, srv in enumerate(available):
        mark = "✅" if i in selected else "➕"
        prefix = srv.get('name_prefix', '')
        label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        text += f"  {mark} {label}\n"

    if expiry_time_ms > 0:
        exp_str = datetime.fromtimestamp(expiry_time_ms / 1000).strftime("%d.%m.%Y")
        text += f"\n⏰ Срок: до {exp_str}"
    else:
        text += "\n⏰ Срок: Безлимит"

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(AddToSubscriptionStates.confirming, F.data == "addsub_confirm")
async def confirm_add_to_sub(callback: CallbackQuery, state: FSMContext):
    """Подтверждение — создаём клиента на выбранных серверах"""
    from bot.api.remote_xui import create_client_via_panel, create_client_on_remote_server
    from datetime import datetime

    data = await state.get_data()
    client_uuid = data.get('client_uuid', '')
    email = data.get('client_email', '')
    expiry_time_ms = data.get('expiry_time_ms', 0)
    ip_limit = data.get('ip_limit', 2)
    selected = data.get('selected_server_indices', [])
    available = data.get('available_servers', [])
    admin_total_gb = data.get('admin_total_gb')

    selected_servers = [available[i] for i in selected if i < len(available)]

    if not selected_servers:
        await callback.answer("Нет выбранных серверов")
        return

    # Проверяем, есть ли среди выбранных серверов серверы с лимитом трафика
    if admin_total_gb is None:
        traffic_servers = [
            srv for srv in selected_servers
            if srv['server_config'].get('traffic_limit_gb', 0) > 0
        ]
        if traffic_servers:
            # Берём значение лимита из первого сервера с лимитом
            traffic_limit = traffic_servers[0]['server_config']['traffic_limit_gb']
            server_names = ", ".join(s['server_name'] for s in traffic_servers)

            await state.set_state(AddToSubscriptionStates.waiting_for_traffic_choice)
            await callback.message.edit_text(
                f"📊 <b>Выбор трафика</b>\n\n"
                f"Серверы с ограничением трафика:\n"
                f"  {server_names}\n\n"
                f"Выберите лимит трафика для этих серверов:",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text=f"📊 {traffic_limit} ГБ (рекомендуется)", callback_data=f"addsub_traffic_{traffic_limit}")],
                    [InlineKeyboardButton(text="♾ Без ограничений", callback_data="addsub_traffic_0")],
                    [InlineKeyboardButton(text="❌ Отмена", callback_data="addsub_cancel")]
                ]),
                parse_mode="HTML"
            )
            await callback.answer()
            return

    await _execute_add_to_sub(callback, state, data, selected_servers)


@router.callback_query(AddToSubscriptionStates.waiting_for_traffic_choice, F.data.startswith("addsub_traffic_"))
async def addsub_traffic_choice(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора трафика при добавлении сервера в подписку"""
    total_gb = int(callback.data.split("_")[-1])
    await state.update_data(admin_total_gb=total_gb)

    data = await state.get_data()
    selected = data.get('selected_server_indices', [])
    available = data.get('available_servers', [])
    selected_servers = [available[i] for i in selected if i < len(available)]

    await _execute_add_to_sub(callback, state, data, selected_servers)


async def _execute_add_to_sub(callback: CallbackQuery, state: FSMContext, data: dict, selected_servers: list):
    """Выполнить добавление клиента на выбранные серверы"""
    from bot.api.remote_xui import create_client_via_panel, _create_client_local_with_uuid

    client_uuid = data.get('client_uuid', '')
    email = data.get('client_email', '')
    expiry_time_ms = data.get('expiry_time_ms', 0)
    ip_limit = data.get('ip_limit', 2)
    admin_total_gb = data.get('admin_total_gb', 0) or 0

    await callback.message.edit_text("⏳ Добавление клиента на серверы...")

    results = []
    for srv in selected_servers:
        server_config = srv['server_config']
        server_name = srv['server_name']

        # Определяем лимит трафика для сервера
        server_traffic_limit = server_config.get('traffic_limit_gb', 0)
        total_gb = admin_total_gb if server_traffic_limit > 0 else 0

        try:
            if server_config.get('local', False):
                # Локальный сервер
                success = await _create_client_local_with_uuid(
                    client_uuid=client_uuid,
                    email=email,
                    expire_time_ms=expiry_time_ms,
                    ip_limit=ip_limit,
                    total_gb=total_gb
                )
                results.append({'server': server_name, 'success': success})
            else:
                # Удалённый сервер — через API панели
                result = await create_client_via_panel(
                    server_config=server_config,
                    client_uuid=client_uuid,
                    email=email,
                    expire_days=30,  # fallback, не используется если expire_time_ms задан
                    ip_limit=ip_limit,
                    expire_time_ms=expiry_time_ms,
                    total_gb=total_gb
                )
                success = result.get('success', False)
                existing = result.get('existing', False)
                results.append({
                    'server': server_name,
                    'success': success,
                    'existing': existing
                })
        except Exception as e:
            logger.error(f"Ошибка добавления на {server_name}: {e}")
            results.append({'server': server_name, 'success': False})

    await state.clear()

    # Формируем результат
    text = "📡 <b>Результат:</b>\n\n"
    for r in results:
        if r.get('success'):
            if r.get('existing'):
                text += f"✅ {r['server']} — клиент уже существовал\n"
            else:
                text += f"✅ {r['server']} — клиент добавлен\n"
        else:
            text += f"❌ {r['server']} — ошибка\n"

    text += "\n📱 Подписка обновлена автоматически."

    buttons = [
        [InlineKeyboardButton(text="🔍 Новый поиск", callback_data="addsub_newsearch")],
        [InlineKeyboardButton(text="◀️ В меню", callback_data="addsub_cancel")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "addsub_newsearch")
async def addsub_new_search(callback: CallbackQuery, state: FSMContext):
    """Новый поиск для добавления сервера"""
    await state.clear()
    await state.set_state(AddToSubscriptionStates.waiting_for_search)
    await callback.message.edit_text(
        "📡 <b>ДОБАВИТЬ СЕРВЕР В ПОДПИСКУ</b>\n\n"
        "Введите номер телефона, email или UUID клиента для поиска.",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "addsub_cancel")
async def cancel_add_sub_callback(callback: CallbackQuery, state: FSMContext):
    """Отмена добавления сервера (inline кнопка)"""
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(
        "Панель администратора:",
        reply_markup=Keyboards.admin_menu()
    )
    await callback.answer()


# ============ МАССОВОЕ ДОБАВЛЕНИЕ СЕРВЕРА КО ВСЕМ ПОДПИСКАМ ============

@router.message(F.text == "📡 Сервер → всем")
@admin_only
async def start_bulk_add_server(message: Message, state: FSMContext, **kwargs):
    """Начало массового добавления сервера ко всем активным подпискам из локальной БД"""
    from bot.api.remote_xui import load_servers_config
    from datetime import datetime

    await state.clear()

    config = load_servers_config()
    servers = [s for s in config.get('servers', []) if s.get('enabled', False)]

    if not servers:
        await message.answer("❌ Нет включённых серверов.", reply_markup=Keyboards.admin_menu())
        return

    # Проверяем сколько активных подписок в БД
    import aiosqlite
    from bot.config import DATABASE_PATH
    now_ms = int(datetime.now().timestamp() * 1000)
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM clients WHERE status = 'active' AND expire_time > ?",
            (now_ms,)
        )
        active_count = (await cursor.fetchone())[0]

    if active_count == 0:
        await message.answer(
            "❌ В базе нет активных подписок.\nСначала выполните /sync для синхронизации.",
            reply_markup=Keyboards.admin_menu()
        )
        return

    buttons = []
    for idx, srv in enumerate(servers):
        name = srv.get('name', 'Unknown')
        prefix = srv.get('inbounds', {}).get('main', {}).get('name_prefix', name)
        label = f"{name} [{prefix}]" if prefix and prefix != name else name
        buttons.append([InlineKeyboardButton(
            text=f"🎯 {label}",
            callback_data=f"bulktgt_{idx}"
        )])

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="bulkadd_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await state.set_state(BulkAddServerStates.waiting_for_target)
    await state.update_data(servers_list=[{
        'name': s.get('name', 'Unknown'),
        'config': s
    } for s in servers])

    await message.answer(
        f"📡 <b>МАССОВОЕ ДОБАВЛЕНИЕ СЕРВЕРА</b>\n\n"
        f"В базе: <b>{active_count}</b> активных подписок.\n\n"
        f"Выберите <b>целевой сервер</b> — куда добавить всех активных клиентов:",
        parse_mode="HTML",
        reply_markup=keyboard
    )


@router.callback_query(BulkAddServerStates.waiting_for_target, F.data.startswith("bulktgt_"))
async def bulk_add_select_target(callback: CallbackQuery, state: FSMContext):
    """Выбор целевого сервера — берём клиентов из локальной БД и фильтруем"""
    from bot.api.remote_xui import get_all_clients_from_panel
    from datetime import datetime
    import aiosqlite
    import sqlite3
    from bot.config import DATABASE_PATH

    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    servers_list = data.get('servers_list', [])

    if idx >= len(servers_list):
        await callback.answer("Ошибка")
        return

    target = servers_list[idx]
    target_name = target['name']
    target_config = target['config']

    await callback.message.edit_text(
        f"⏳ Загружаю активные подписки из базы данных...",
        parse_mode="HTML"
    )

    # 1. Берём всех активных клиентов из локальной БД clients
    now_ms = int(datetime.now().timestamp() * 1000)
    db_clients = {}  # uuid -> {email, expiry_time, ip_limit}

    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT uuid, email, expire_time, ip_limit FROM clients "
                "WHERE status = 'active' AND expire_time > ? AND uuid IS NOT NULL AND uuid != ''",
                (now_ms,)
            )
            for row in await cursor.fetchall():
                uuid = row['uuid']
                if uuid:
                    db_clients[uuid] = {
                        'email': row['email'] or '',
                        'uuid': uuid,
                        'expiry_time': row['expire_time'] or 0,
                        'ip_limit': row['ip_limit'] or 2
                    }
    except Exception as e:
        logger.error(f"Ошибка чтения подписок из БД: {e}")
        await callback.message.edit_text(
            f"❌ Ошибка чтения подписок из БД: {e}",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ В меню", callback_data="bulkadd_cancel")]
            ])
        )
        await callback.answer()
        return

    if not db_clients:
        await callback.message.edit_text(
            "❌ В базе нет активных подписок.\nСначала выполните /sync.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ В меню", callback_data="bulkadd_cancel")]
            ])
        )
        await callback.answer()
        return

    await callback.message.edit_text(
        f"⏳ В базе {len(db_clients)} активных подписок.\n"
        f"Проверяю, кого нет на <b>{target_name}</b>...",
        parse_mode="HTML"
    )

    # 2. Получаем кто уже есть на целевом сервере
    existing_uuids = set()
    try:
        if target_config.get('local', False):
            conn = sqlite3.connect('/etc/x-ui/x-ui.db')
            cursor = conn.cursor()
            cursor.execute("SELECT settings FROM inbounds WHERE enable=1")
            rows = cursor.fetchall()
            conn.close()
            for (settings_str,) in rows:
                try:
                    import json as _json
                    settings = _json.loads(settings_str)
                    for client in settings.get('clients', []):
                        uuid = client.get('id', '')
                        if uuid:
                            existing_uuids.add(uuid)
                except Exception:
                    continue
        else:
            target_clients = await get_all_clients_from_panel(target_config)
            for c in target_clients:
                uuid = c.get('uuid', '')
                if uuid:
                    existing_uuids.add(uuid)
    except Exception as e:
        logger.error(f"Ошибка получения клиентов целевого сервера {target_name}: {e}")

    # 3. Фильтруем — только те, кого нет на целевом сервере
    missing_clients = [
        c for uuid, c in db_clients.items()
        if uuid not in existing_uuids
    ]

    if not missing_clients:
        await callback.message.edit_text(
            f"🎉 Все активные клиенты уже есть на <b>{target_name}</b>!",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="◀️ В меню", callback_data="bulkadd_cancel")]
            ])
        )
        await callback.answer()
        return

    missing_clients.sort(key=lambda c: c['email'].lower())

    await state.update_data(
        target_server_name=target_name,
        target_server_config=target_config,
        missing_clients=missing_clients,
        total_source=len(db_clients)
    )

    # Проверяем, нужен ли выбор трафика
    traffic_limit = target_config.get('traffic_limit_gb', 0)
    if traffic_limit > 0:
        await state.set_state(BulkAddServerStates.waiting_for_traffic)
        await callback.message.edit_text(
            f"📊 <b>Выбор трафика</b>\n\n"
            f"Сервер <b>{target_name}</b> имеет ограничение трафика.\n"
            f"Выберите лимит трафика для добавляемых клиентов:",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"📊 {traffic_limit} ГБ (рекомендуется)", callback_data=f"bulkadd_traffic_{traffic_limit}")],
                [InlineKeyboardButton(text="♾ Без ограничений", callback_data="bulkadd_traffic_0")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="bulkadd_cancel")]
            ])
        )
        await callback.answer()
        return

    await state.update_data(bulk_total_gb=0)
    await _show_bulk_add_confirm(callback, state)


@router.callback_query(BulkAddServerStates.waiting_for_traffic, F.data.startswith("bulkadd_traffic_"))
async def bulk_add_traffic_choice(callback: CallbackQuery, state: FSMContext):
    """Выбор трафика для массового добавления"""
    total_gb = int(callback.data.split("_")[-1])
    await state.update_data(bulk_total_gb=total_gb)
    await _show_bulk_add_confirm(callback, state)


async def _show_bulk_add_confirm(callback: CallbackQuery, state: FSMContext):
    """Показать подтверждение массового добавления"""
    from datetime import datetime

    data = await state.get_data()
    target_name = data.get('target_server_name', '')
    missing_clients = data.get('missing_clients', [])
    total_source = data.get('total_source', 0)
    bulk_total_gb = data.get('bulk_total_gb', 0)

    await state.set_state(BulkAddServerStates.confirming)

    text = f"📡 <b>МАССОВОЕ ДОБАВЛЕНИЕ</b>\n\n"
    text += f"Подписок в БД: <b>{total_source}</b>\n"
    text += f"Целевой сервер: <b>{target_name}</b>\n"
    text += f"Клиентов для добавления: <b>{len(missing_clients)}</b>\n"
    if bulk_total_gb > 0:
        text += f"Лимит трафика: <b>{bulk_total_gb} ГБ</b>\n"
    else:
        text += f"Лимит трафика: <b>без ограничений</b>\n"

    text += f"\n<b>Список клиентов:</b>\n"
    # Показываем первых 20
    show_count = min(len(missing_clients), 20)
    for i, client in enumerate(missing_clients[:show_count]):
        email = client['email']
        expiry = client.get('expiry_time', 0)
        if expiry > 0:
            exp_str = datetime.fromtimestamp(expiry / 1000).strftime("%d.%m.%y")
        else:
            exp_str = "∞"
        text += f"  {i+1}. <code>{email}</code> — до {exp_str}\n"

    if len(missing_clients) > show_count:
        text += f"  ... и ещё {len(missing_clients) - show_count}\n"

    text += f"\n⚠️ <b>Подтвердите добавление {len(missing_clients)} клиентов на {target_name}.</b>"

    buttons = [
        [InlineKeyboardButton(text=f"✅ Добавить {len(missing_clients)} клиентов", callback_data="bulkadd_confirm")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="bulkadd_cancel")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(BulkAddServerStates.confirming, F.data == "bulkadd_confirm")
async def bulk_add_execute(callback: CallbackQuery, state: FSMContext):
    """Выполнить массовое добавление клиентов на сервер"""
    from bot.api.remote_xui import create_client_via_panel, _create_client_local_with_uuid

    data = await state.get_data()
    target_name = data.get('target_server_name', '')
    target_config = data.get('target_server_config', {})
    missing_clients = data.get('missing_clients', [])
    bulk_total_gb = data.get('bulk_total_gb', 0)

    await state.set_state(BulkAddServerStates.processing)

    total = len(missing_clients)
    await callback.message.edit_text(
        f"⏳ Добавление {total} клиентов на <b>{target_name}</b>...\n"
        f"Это может занять некоторое время.",
        parse_mode="HTML"
    )

    success_count = 0
    error_count = 0
    existing_count = 0
    errors = []

    for i, client in enumerate(missing_clients):
        client_uuid = client['uuid']
        email = client['email']
        expiry_time_ms = client.get('expiry_time', 0)
        ip_limit = client.get('ip_limit', 2)

        try:
            if target_config.get('local', False):
                success = await _create_client_local_with_uuid(
                    client_uuid=client_uuid,
                    email=email,
                    expire_time_ms=expiry_time_ms,
                    ip_limit=ip_limit,
                    total_gb=bulk_total_gb
                )
                if success:
                    success_count += 1
                else:
                    error_count += 1
                    errors.append(email)
            else:
                result = await create_client_via_panel(
                    server_config=target_config,
                    client_uuid=client_uuid,
                    email=email,
                    expire_days=30,
                    ip_limit=ip_limit,
                    expire_time_ms=expiry_time_ms,
                    total_gb=bulk_total_gb
                )
                if result.get('success'):
                    if result.get('existing'):
                        existing_count += 1
                    else:
                        success_count += 1
                else:
                    error_count += 1
                    errors.append(email)
        except Exception as e:
            logger.error(f"Ошибка добавления {email} на {target_name}: {e}")
            error_count += 1
            errors.append(email)

        # Обновляем прогресс каждые 10 клиентов
        if (i + 1) % 10 == 0:
            try:
                await callback.message.edit_text(
                    f"⏳ Добавление клиентов на <b>{target_name}</b>...\n"
                    f"Прогресс: {i + 1}/{total}\n"
                    f"✅ Добавлено: {success_count}\n"
                    f"❌ Ошибок: {error_count}",
                    parse_mode="HTML"
                )
            except Exception:
                pass  # Telegram rate limit

    await state.clear()

    # Формируем результат
    text = f"📡 <b>РЕЗУЛЬТАТ МАССОВОГО ДОБАВЛЕНИЯ</b>\n\n"
    text += f"Сервер: <b>{target_name}</b>\n"
    text += f"Всего клиентов: {total}\n\n"
    text += f"✅ Добавлено: {success_count}\n"
    if existing_count > 0:
        text += f"ℹ️ Уже существовали: {existing_count}\n"
    if error_count > 0:
        text += f"❌ Ошибок: {error_count}\n"
        if errors[:5]:
            text += f"\nОшибки:\n"
            for err_email in errors[:5]:
                text += f"  • {err_email}\n"
            if len(errors) > 5:
                text += f"  ... и ещё {len(errors) - 5}\n"

    text += "\n📱 Подписки обновлены автоматически."

    buttons = [
        [InlineKeyboardButton(text="◀️ В меню", callback_data="bulkadd_cancel")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "bulkadd_cancel")
async def cancel_bulk_add(callback: CallbackQuery, state: FSMContext):
    """Отмена массового добавления"""
    await state.clear()
    await callback.message.delete()
    await callback.message.answer(
        "Панель администратора:",
        reply_markup=Keyboards.admin_menu()
    )
    await callback.answer()


# ===== Синхронизация подписок в локальную БД =====

@router.message(F.text == "/sync")
@admin_only
async def sync_subscriptions_command(message: Message, **kwargs):
    """Синхронизировать всех клиентов со всех серверов в локальную БД"""
    from bot.database.client_manager import ClientManager
    from bot.config import DATABASE_PATH

    await message.answer("⏳ Синхронизация подписок со всех серверов...")

    cm = ClientManager(DATABASE_PATH)
    result = await cm.sync_all_from_panels()

    text = f"✅ <b>Синхронизация завершена</b>\n\n"
    text += f"Серверов обработано: {result['servers']}\n"
    text += f"Записей синхронизировано: {result['synced']}\n"

    if result['errors']:
        text += f"\n❌ Ошибки:\n"
        for err in result['errors'][:5]:
            text += f"  • {err}\n"

    # Подсчитаем итого в БД
    import aiosqlite
    async with aiosqlite.connect(DATABASE_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM clients")
        total = (await cursor.fetchone())[0]
        cursor = await db.execute("SELECT COUNT(*) FROM clients WHERE status = 'active'")
        active = (await cursor.fetchone())[0]

    text += f"\n📊 Итого в БД: {total} клиентов ({active} активных)"

    await message.answer(text, parse_mode="HTML", reply_markup=Keyboards.admin_menu())


# ===== Восстановление клиентов из бэкапа =====

class RestoreStates(StatesGroup):
    waiting_for_file = State()
    waiting_for_server = State()


@router.message(F.text == "/restore_backup")
async def cmd_restore_backup(message: Message, state: FSMContext, db: DatabaseManager):
    """Команда восстановления клиентов из JSON-бэкапа"""
    if message.from_user.id != ADMIN_ID:
        return

    from bot.api.remote_xui import load_servers_config
    config = load_servers_config()
    active_servers = [
        s for s in config.get('servers', [])
        if s.get('enabled', True) and s.get('panel')
    ]

    if not active_servers:
        await message.answer("❌ Нет доступных серверов с панелями")
        return

    await state.set_state(RestoreStates.waiting_for_file)
    await message.answer(
        "📥 <b>Восстановление из бэкапа</b>\n\n"
        "Отправьте JSON-файл бэкапа клиентов (clients_backup_*.json).\n\n"
        "Или /cancel для отмены.",
        parse_mode="HTML"
    )


@router.message(RestoreStates.waiting_for_file, F.document)
async def restore_receive_file(message: Message, state: FSMContext, db: DatabaseManager, bot):
    """Получение файла бэкапа"""
    if message.from_user.id != ADMIN_ID:
        return

    doc = message.document
    if not doc.file_name.endswith('.json'):
        await message.answer("⚠️ Нужен JSON-файл (clients_backup_*.json)")
        return

    # Скачиваем файл
    import os
    backup_dir = '/root/manager_vpn/backups'
    os.makedirs(backup_dir, exist_ok=True)
    file_path = f"{backup_dir}/restore_temp.json"

    file = await bot.get_file(doc.file_id)
    await bot.download_file(file.file_path, file_path)

    # Проверяем содержимое
    import json
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except Exception as e:
        await message.answer(f"❌ Ошибка чтения JSON: {e}")
        await state.clear()
        return

    client_servers = data.get('client_servers', [])
    clients = data.get('clients', [])
    backup_date = data.get('backup_date', '?')

    if not client_servers:
        await message.answer(
            "⚠️ В бэкапе нет данных client_servers.\n"
            "Восстановление невозможно — бэкап создан до добавления этой функции."
        )
        await state.clear()
        return

    # Показываем серверы для выбора
    from bot.api.remote_xui import load_servers_config
    config = load_servers_config()
    active_servers = [
        s for s in config.get('servers', [])
        if s.get('enabled', True) and s.get('panel')
    ]

    # Уникальные серверы из бэкапа
    backup_servers = set(r.get('server_name') for r in client_servers)

    buttons = []
    for server in active_servers:
        name = server.get('name', '')
        count = sum(1 for r in client_servers if r.get('server_name') == name)
        label = f"{name} ({count} клиентов)" if count else f"{name} (новый)"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"restore_srv_{name}")])

    buttons.append([InlineKeyboardButton(text="🔄 Все серверы (как в бэкапе)", callback_data="restore_srv_all")])
    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="restore_cancel")])

    await state.update_data(restore_file=file_path)
    await state.set_state(RestoreStates.waiting_for_server)
    await message.answer(
        f"📋 <b>Бэкап от {backup_date}</b>\n\n"
        f"🔑 Клиентов: {len(clients)}\n"
        f"📡 Записей серверов: {len(client_servers)}\n"
        f"🌐 Серверов в бэкапе: {', '.join(backup_servers)}\n\n"
        f"Выберите сервер для восстановления:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(RestoreStates.waiting_for_server, F.data.startswith("restore_srv_"))
async def restore_select_server(callback: CallbackQuery, state: FSMContext, db: DatabaseManager, bot):
    """Выбор сервера и запуск восстановления"""
    if callback.from_user.id != ADMIN_ID:
        return

    data = await state.get_data()
    file_path = data.get('restore_file')
    server_choice = callback.data.replace("restore_srv_", "")

    target_server = None if server_choice == "all" else server_choice

    await callback.message.edit_text("⏳ Восстановление клиентов... Это может занять несколько минут.")

    from main import restore_clients_from_backup
    result = await restore_clients_from_backup(file_path, target_server, bot)

    report = (
        f"✅ <b>Восстановление завершено</b>\n\n"
        f"📡 Сервер: {target_server or 'все (как в бэкапе)'}\n"
        f"✅ Восстановлено: {result['restored']}\n"
        f"⏭ Пропущено: {result['skipped']}\n"
        f"❌ Ошибок: {result['errors']}\n"
    )

    if result['details']:
        details_text = '\n'.join(result['details'][:20])
        report += f"\n<b>Детали:</b>\n{details_text}"
        if len(result['details']) > 20:
            report += f"\n... и ещё {len(result['details']) - 20}"

    await callback.message.edit_text(report, parse_mode="HTML")
    await state.clear()


@router.callback_query(RestoreStates.waiting_for_server, F.data == "restore_cancel")
async def restore_cancel(callback: CallbackQuery, state: FSMContext):
    """Отмена восстановления"""
    await state.clear()
    await callback.message.edit_text("❌ Восстановление отменено")
    await callback.answer()


@router.message(RestoreStates.waiting_for_file, F.text == "/cancel")
async def restore_cancel_text(message: Message, state: FSMContext):
    """Отмена восстановления текстом"""
    await state.clear()
    await message.answer("❌ Восстановление отменено")


# ===== Ручной бэкап =====

@router.message(F.text.in_({"/backup", "💾 Бэкап"}))
async def cmd_manual_backup(message: Message, bot, **kwargs):
    """Ручной бэкап всех баз — отправка в чат + Яндекс.Диск"""
    if message.from_user.id != ADMIN_ID:
        return

    await message.answer("⏳ Запускаю полный бэкап...")

    import shutil
    import json as _json
    from pathlib import Path
    from datetime import datetime
    from aiogram.types import FSInputFile
    from bot.config import DATABASE_PATH
    from main import upload_to_yandex_disk, backup_remote_panels, create_clients_backup

    backup_dir = Path('/root/manager_vpn/backups')
    backup_dir.mkdir(exist_ok=True)
    date_str = datetime.now().strftime('%Y-%m-%d_%H-%M')
    results = []

    # 1. Локальная X-UI база
    xui_db = Path('/etc/x-ui/x-ui.db')
    if xui_db.exists():
        try:
            dst = backup_dir / f'x-ui_backup_{date_str}.db'
            shutil.copy2(xui_db, dst)
            doc = FSInputFile(dst)
            await bot.send_document(ADMIN_ID, doc, caption=f"💾 X-UI local ({dst.stat().st_size/1024:.1f} KB)")
            await upload_to_yandex_disk(dst)
            results.append("✅ X-UI local")
        except Exception as e:
            results.append(f"❌ X-UI local: {e}")

    # 2. bot_database.db
    bot_db = Path(DATABASE_PATH)
    if bot_db.exists():
        try:
            dst = backup_dir / f'bot_db_backup_{date_str}.db'
            shutil.copy2(bot_db, dst)
            doc = FSInputFile(dst)
            await bot.send_document(ADMIN_ID, doc, caption=f"💾 bot_database ({dst.stat().st_size/1024:.1f} KB)")
            await upload_to_yandex_disk(dst)
            results.append("✅ bot_database")
        except Exception as e:
            results.append(f"❌ bot_database: {e}")

    # 3. Удалённые панели
    try:
        await backup_remote_panels(bot)
        results.append("✅ Удалённые панели")
    except Exception as e:
        results.append(f"❌ Удалённые панели: {e}")

    # 4. JSON бэкап клиентов
    try:
        await create_clients_backup(bot)
        results.append("✅ JSON клиентов")
    except Exception as e:
        results.append(f"❌ JSON клиентов: {e}")

    await message.answer(
        f"📋 <b>Бэкап завершён</b>\n\n" + "\n".join(results),
        parse_mode="HTML"
    )


# ==================== УПРАВЛЕНИЕ ПОДПИСКОЙ (исключение серверов) ====================

@router.message(F.text == "📋 Управление подпиской")
@admin_only
async def start_manage_subscription(message: Message, state: FSMContext, **kwargs):
    """Начало управления подпиской клиента"""
    await state.clear()
    await state.set_state(ManageSubscriptionStates.waiting_for_search)
    await message.answer(
        "📋 <b>УПРАВЛЕНИЕ ПОДПИСКОЙ</b>\n\n"
        "Здесь вы можете:\n"
        "• Посмотреть на каких серверах клиент\n"
        "• Исключить серверы из подписки\n\n"
        "Введите email, UUID или телефон клиента:\n\n"
        "Примеры:\n"
        "• <code>79001234567</code>\n"
        "• <code>Иван</code>",
        parse_mode="HTML",
        reply_markup=Keyboards.cancel()
    )


@router.message(ManageSubscriptionStates.waiting_for_search, F.text == "Отмена")
async def cancel_manage_sub(message: Message, state: FSMContext):
    """Отмена управления подпиской"""
    await state.clear()
    await message.answer("Операция отменена.", reply_markup=Keyboards.admin_menu())


@router.message(ManageSubscriptionStates.waiting_for_search)
async def process_manage_sub_search(message: Message, state: FSMContext, **kwargs):
    """Поиск клиента для управления подпиской"""
    query = message.text.strip()

    admin_menu_buttons = {
        "📡 Добавить сервер", "📡 Сервер → всем", "🔑 Создать ключ (выбор inbound)",
        "Добавить менеджера", "Список менеджеров", "Общая статистика",
        "Детальная статистика", "💰 Изменить цены", "🔍 Поиск ключа",
        "📅 Продлить подписку", "📋 Управление подпиской",
        "🗑️ Удалить ключ", "📢 Отправить уведомление", "🌐 Управление SNI",
        "💳 Реквизиты", "📋 Веб-заказы", "🖥 Статус серверов", "🔧 Панели X-UI",
        "💳 Оплата серверов", "🌐 Админ-панель сайта",
        "Назад", "Панель администратора", "Создать ключ", "🔄 Замена ключа",
        "🔧 Исправить ключ", "💰 Прайс", "Моя статистика", "💾 Бэкап",
    }
    if query in admin_menu_buttons:
        await state.clear()
        await message.answer("Операция отменена.", reply_markup=Keyboards.admin_menu())
        return

    if len(query) < 2:
        await message.answer("❌ Введите минимум 2 символа для поиска.")
        return

    status_msg = await message.answer("🔍 Поиск клиента на серверах...")

    xui_clients = await search_clients_on_servers(query)

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
        srv = client.get('server', 'Unknown')
        if srv not in clients_by_uuid[uuid]['servers']:
            clients_by_uuid[uuid]['servers'].append(srv)

    unique_clients = list(clients_by_uuid.values())

    if not unique_clients:
        await status_msg.edit_text(
            f"🔍 По запросу «<b>{query}</b>» ничего не найдено.",
            parse_mode="HTML"
        )
        return

    await state.update_data(msub_search_results=unique_clients)

    text = f"📋 <b>УПРАВЛЕНИЕ ПОДПИСКОЙ</b>\n"
    text += f"Запрос: «{query}»\n\n"

    buttons = []

    for idx, client in enumerate(unique_clients[:10]):
        email = client['email']
        servers_str = ', '.join(client['servers'])
        expiry_time = client.get('expiry_time', 0)

        if expiry_time > 0:
            expiry_dt = datetime.fromtimestamp(expiry_time / 1000)
            expiry_str = expiry_dt.strftime("%d.%m.%Y")
            now_ms = int(datetime.now().timestamp() * 1000)
            if expiry_time < now_ms:
                expiry_str += " ❌ истекла"
        else:
            expiry_str = "Безлимит"

        text += f"{idx + 1}. <b>{email}</b>\n"
        text += f"   🖥 Серверы ({len(client['servers'])}): {servers_str}\n"
        text += f"   ⏰ До: {expiry_str}\n\n"

        buttons.append([InlineKeyboardButton(
            text=f"📋 {email[:30]}",
            callback_data=f"msub_sel_{idx}"
        )])

        if len(text) > 3000:
            text += "...\n"
            break

    buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="msub_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await status_msg.edit_text(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "msub_cancel")
async def cancel_manage_sub_cb(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Отмена управления подпиской (callback)"""
    await state.clear()
    await callback.message.edit_text("Операция отменена.")
    await callback.answer()


@router.callback_query(F.data.startswith("msub_sel_"))
async def manage_sub_select_client(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Выбор клиента — показ серверов с возможностью исключения"""
    from bot.api.remote_xui import find_client_presence_on_all_servers

    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    search_results = data.get('msub_search_results', [])

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

    # Сохраняем found_on для последующего исключения
    found_servers = []
    for srv in found_on:
        found_servers.append({
            'server_name': srv['server_name'],
            'name_prefix': srv.get('name_prefix', srv['server_name']),
            'expiry_time': srv.get('expiry_time', 0),
            'server_config': srv.get('server_config', {})
        })

    await state.update_data(
        msub_client_uuid=client_uuid,
        msub_client_email=email,
        msub_found_servers=found_servers,
    )
    await state.set_state(ManageSubscriptionStates.waiting_for_action)

    # Формируем текст
    sub_url = f"https://{_get_sub_domain(kwargs)}/sub/{client_uuid}"
    text = f"📋 <b>Управление подпиской</b>\n\n"
    text += f"Клиент: <b>{email}</b>\n"
    text += f"📱 Подписка: <code>{sub_url}</code>\n\n"

    if found_on:
        text += f"<b>🖥 На серверах ({len(found_on)}):</b>\n"
        for i, srv in enumerate(found_on):
            exp = srv.get('expiry_time', 0)
            if exp > 0:
                exp_str = datetime.fromtimestamp(exp / 1000).strftime("%d.%m.%Y")
            else:
                exp_str = "Безлимит"
            prefix = srv.get('name_prefix', '')
            label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
            text += f"  {i + 1}. ✅ {label} — до {exp_str}\n"
        text += "\n"
    else:
        text += "⚠️ Клиент не найден ни на одном сервере.\n\n"

    if not_found_on:
        text += f"<b>➕ Нет на серверах ({len(not_found_on)}):</b>\n"
        for srv in not_found_on:
            prefix = srv.get('name_prefix', '')
            label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
            text += f"  ➖ {label}\n"
        text += "\n"

    buttons = []

    if len(found_on) > 0:
        text += "Выберите сервер для <b>исключения</b> из подписки:"

        for i, srv in enumerate(found_servers):
            prefix = srv.get('name_prefix', '')
            btn_label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
            buttons.append([InlineKeyboardButton(
                text=f"🗑 {btn_label}",
                callback_data=f"msub_excl_{i}"
            )])

    buttons.append([InlineKeyboardButton(text="🔍 Новый поиск", callback_data="msub_newsearch")])
    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="msub_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "msub_newsearch")
async def manage_sub_new_search(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Новый поиск"""
    await state.clear()
    await state.set_state(ManageSubscriptionStates.waiting_for_search)
    await callback.message.edit_text(
        "📋 <b>УПРАВЛЕНИЕ ПОДПИСКОЙ</b>\n\n"
        "Введите email, UUID или телефон клиента для поиска:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(ManageSubscriptionStates.waiting_for_action, F.data.startswith("msub_excl_"))
async def manage_sub_confirm_exclude(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Подтверждение исключения сервера"""
    idx = int(callback.data.split("_")[-1])
    data = await state.get_data()
    found_servers = data.get('msub_found_servers', [])
    email = data.get('msub_client_email', '')

    if idx >= len(found_servers):
        await callback.answer("Ошибка: сервер не найден")
        return

    srv = found_servers[idx]
    server_name = srv['server_name']
    prefix = srv.get('name_prefix', '')
    label = f"{server_name} [{prefix}]" if prefix and prefix != server_name else server_name

    await state.update_data(msub_exclude_idx=idx)
    await state.set_state(ManageSubscriptionStates.confirming_exclude)

    text = (
        f"⚠️ <b>Подтверждение исключения</b>\n\n"
        f"Клиент: <b>{email}</b>\n"
        f"Сервер: <b>{label}</b>\n\n"
        f"Клиент будет <b>удалён</b> с этого сервера.\n"
        f"Подписка на остальных серверах сохранится.\n\n"
        f"Вы уверены?"
    )

    buttons = [
        [InlineKeyboardButton(text="✅ Да, исключить", callback_data="msub_excl_confirm")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="msub_excl_back")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="msub_cancel")]
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(ManageSubscriptionStates.confirming_exclude, F.data == "msub_excl_back")
async def manage_sub_back_to_servers(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Назад к списку серверов"""
    await state.set_state(ManageSubscriptionStates.waiting_for_action)

    data = await state.get_data()
    client_uuid = data.get('msub_client_uuid', '')
    email = data.get('msub_client_email', '')
    found_servers = data.get('msub_found_servers', [])

    sub_url = f"https://{_get_sub_domain(kwargs)}/sub/{client_uuid}"
    text = f"📋 <b>Управление подпиской</b>\n\n"
    text += f"Клиент: <b>{email}</b>\n"
    text += f"📱 Подписка: <code>{sub_url}</code>\n\n"

    if found_servers:
        text += f"<b>🖥 На серверах ({len(found_servers)}):</b>\n"
        for i, srv in enumerate(found_servers):
            exp = srv.get('expiry_time', 0)
            if exp > 0:
                exp_str = datetime.fromtimestamp(exp / 1000).strftime("%d.%m.%Y")
            else:
                exp_str = "Безлимит"
            prefix = srv.get('name_prefix', '')
            label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
            text += f"  {i + 1}. ✅ {label} — до {exp_str}\n"
        text += "\nВыберите сервер для <b>исключения</b>:"

    buttons = []
    for i, srv in enumerate(found_servers):
        prefix = srv.get('name_prefix', '')
        btn_label = f"{srv['server_name']} [{prefix}]" if prefix and prefix != srv['server_name'] else srv['server_name']
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {btn_label}",
            callback_data=f"msub_excl_{i}"
        )])

    buttons.append([InlineKeyboardButton(text="🔍 Новый поиск", callback_data="msub_newsearch")])
    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="msub_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@router.callback_query(ManageSubscriptionStates.confirming_exclude, F.data == "msub_excl_confirm")
async def manage_sub_execute_exclude(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Выполнить исключение сервера из подписки"""
    from bot.api.remote_xui import delete_client_via_panel

    data = await state.get_data()
    client_uuid = data.get('msub_client_uuid', '')
    email = data.get('msub_client_email', '')
    found_servers = data.get('msub_found_servers', [])
    exclude_idx = data.get('msub_exclude_idx', 0)

    if exclude_idx >= len(found_servers):
        await callback.answer("Ошибка")
        return

    srv = found_servers[exclude_idx]
    server_name = srv['server_name']
    server_config = srv.get('server_config', {})
    prefix = srv.get('name_prefix', '')
    label = f"{server_name} [{prefix}]" if prefix and prefix != server_name else server_name

    await callback.message.edit_text(f"⏳ Удаление клиента с сервера {label}...")

    # Удаляем с панели
    success = False
    try:
        if server_config.get('local', False):
            # Локальный сервер — удаляем через XUIClient
            from bot.api.xui_client import XUIClient
            from bot.config import HOST, USERNAME, PASSWORD
            async with XUIClient(HOST, USERNAME, PASSWORD) as xui:
                if await xui.login():
                    # Находим inbound_id
                    inbounds = await xui.get_inbounds()
                    for inbound in inbounds:
                        inbound_id = inbound.get('id')
                        try:
                            del_result = await xui.delete_client(inbound_id, client_uuid)
                            if del_result:
                                success = True
                        except Exception:
                            pass
        else:
            success = await delete_client_via_panel(server_config, client_uuid)
    except Exception as e:
        logger.error(f"Ошибка удаления клиента {email} с {server_name}: {e}")

    # Удаляем из client_servers в БД
    try:
        from bot.config import DATABASE_PATH
        import aiosqlite
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "DELETE FROM client_servers WHERE client_uuid = ? AND server_name = ?",
                (client_uuid, server_name)
            )
            await db.commit()
    except Exception as e:
        logger.warning(f"Ошибка удаления из client_servers: {e}")

    # Обновляем список серверов в state
    found_servers.pop(exclude_idx)
    await state.update_data(msub_found_servers=found_servers)

    if success:
        result_text = (
            f"✅ <b>Сервер исключён</b>\n\n"
            f"Клиент: <b>{email}</b>\n"
            f"Удалён с: <b>{label}</b>\n\n"
        )
    else:
        result_text = (
            f"⚠️ <b>Возможно не удалось удалить</b>\n\n"
            f"Клиент: <b>{email}</b>\n"
            f"Сервер: <b>{label}</b>\n"
            f"Проверьте панель сервера вручную.\n\n"
        )

    if found_servers:
        result_text += f"<b>Остальные серверы ({len(found_servers)}):</b>\n"
        for i, s in enumerate(found_servers):
            p = s.get('name_prefix', '')
            lbl = f"{s['server_name']} [{p}]" if p and p != s['server_name'] else s['server_name']
            result_text += f"  ✅ {lbl}\n"
    else:
        result_text += "⚠️ Клиент больше не на серверах."

    await state.set_state(ManageSubscriptionStates.waiting_for_action)

    buttons = []
    for i, s in enumerate(found_servers):
        p = s.get('name_prefix', '')
        btn_label = f"{s['server_name']} [{p}]" if p and p != s['server_name'] else s['server_name']
        buttons.append([InlineKeyboardButton(
            text=f"🗑 {btn_label}",
            callback_data=f"msub_excl_{i}"
        )])
    buttons.append([InlineKeyboardButton(text="🔍 Новый поиск", callback_data="msub_newsearch")])
    buttons.append([InlineKeyboardButton(text="❌ Закрыть", callback_data="msub_cancel")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await callback.message.edit_text(result_text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


# ============ МОНИТОРИНГ ЛИМИТА УСТРОЙСТВ ============

class DeviceLimitStates(StatesGroup):
    waiting_for_search = State()
    waiting_for_new_limit = State()


@router.message(F.text == "📱 Лимит устройств")
@admin_only
async def show_device_limits_menu(message: Message, **kwargs):
    """Показать меню управления лимитом устройств"""
    from bot.services.device_monitor import get_blocked_clients, check_device_limits

    blocked = get_blocked_clients()

    text = "📱 <b>Мониторинг устройств</b>\n\n"
    text += f"🔄 Проверка каждые 2 минуты\n"
    text += f"🚫 Заблокировано сейчас: <b>{len(blocked)}</b>\n\n"

    if blocked:
        text += "<b>Заблокированные клиенты:</b>\n"
        # Получим email для UUID из БД
        try:
            async with aiosqlite.connect(DATABASE_PATH) as db:
                for uuid, ts in list(blocked.items())[:10]:
                    async with db.execute(
                        "SELECT email, ip_limit FROM clients WHERE uuid = ?", (uuid,)
                    ) as cursor:
                        row = await cursor.fetchone()
                        if row:
                            from datetime import datetime
                            blocked_time = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
                            text += f"• <code>{row[0]}</code> (лимит: {row[1]}) — с {blocked_time}\n"
        except Exception:
            pass

        if len(blocked) > 10:
            text += f"\n... и ещё {len(blocked) - 10}\n"

    buttons = [
        [InlineKeyboardButton(text="🔍 Проверить клиента", callback_data="devlim_check_client")],
        [InlineKeyboardButton(text="✏️ Изменить лимит клиента", callback_data="devlim_edit_limit")],
        [InlineKeyboardButton(text="🔄 Запустить проверку сейчас", callback_data="devlim_run_now")],
        [InlineKeyboardButton(text="🔓 Разблокировать всех", callback_data="devlim_unblock_all")],
    ]
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "devlim_run_now")
@admin_only
async def run_device_check_now(callback: CallbackQuery, **kwargs):
    """Запустить проверку лимита устройств прямо сейчас"""
    from bot.services.device_monitor import check_device_limits

    await callback.answer("🔄 Запускаю проверку...")
    await callback.message.edit_text("⏳ Проверяю подключения на всех серверах...")

    bot = callback.bot
    await check_device_limits(bot)

    from bot.services.device_monitor import get_blocked_clients
    blocked = get_blocked_clients()

    await callback.message.edit_text(
        f"✅ Проверка завершена.\n\n"
        f"🚫 Заблокировано: <b>{len(blocked)}</b>",
        parse_mode="HTML"
    )


@router.callback_query(F.data == "devlim_unblock_all")
@admin_only
async def unblock_all_devices(callback: CallbackQuery, **kwargs):
    """Разблокировать всех заблокированных клиентов"""
    from bot.services.device_monitor import get_blocked_clients, _blocked_clients
    from bot.api.remote_xui import load_servers_config, get_all_clients_from_panel

    blocked = get_blocked_clients()
    if not blocked:
        await callback.answer("Нет заблокированных клиентов", show_alert=True)
        return

    await callback.answer("🔓 Разблокирую...")
    await callback.message.edit_text(f"⏳ Разблокирую {len(blocked)} клиентов...")

    # Для каждого заблокированного — включить на всех серверах
    from bot.services.device_monitor import collect_client_presence_on_servers, toggle_client_on_panel

    servers_config = load_servers_config()
    servers = [s for s in servers_config.get('servers', [])
               if s.get('enabled', True) and s.get('panel')]

    uuid_to_servers = await collect_client_presence_on_servers(servers)
    restored = 0

    for uuid in list(blocked.keys()):
        entries = uuid_to_servers.get(uuid, [])
        for entry in entries:
            await toggle_client_on_panel(
                entry['server_config'],
                uuid,
                entry['inbound_id'],
                entry['client_data'],
                enable=True,
            )
        if uuid in _blocked_clients:
            del _blocked_clients[uuid]
        restored += 1

    await callback.message.edit_text(
        f"✅ Разблокировано: <b>{restored}</b> клиентов",
        parse_mode="HTML"
    )


@router.callback_query(F.data == "devlim_check_client")
@admin_only
async def ask_client_for_device_check(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Запросить email/UUID клиента для проверки устройств"""
    await state.set_state(DeviceLimitStates.waiting_for_search)
    await state.update_data(action="check")
    await callback.message.edit_text(
        "🔍 Введите <b>email</b> или <b>UUID</b> клиента для проверки подключённых устройств:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(F.data == "devlim_edit_limit")
@admin_only
async def ask_client_for_limit_edit(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Запросить email/UUID клиента для изменения лимита"""
    await state.set_state(DeviceLimitStates.waiting_for_search)
    await state.update_data(action="edit")
    await callback.message.edit_text(
        "✏️ Введите <b>email</b> или <b>UUID</b> клиента для изменения лимита устройств:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(DeviceLimitStates.waiting_for_search)
async def process_device_limit_search(message: Message, state: FSMContext, **kwargs):
    """Обработка ввода email/UUID для проверки или изменения лимита"""
    query = message.text.strip()
    data = await state.get_data()
    action = data.get("action", "check")

    # Ищем клиента в БД
    client = None
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT uuid, email, ip_limit, status, telegram_id FROM clients WHERE email = ? OR uuid = ?",
                (query, query)
            ) as cursor:
                row = await cursor.fetchone()
                if row:
                    client = dict(row)
    except Exception as e:
        await message.answer(f"❌ Ошибка поиска: {e}")
        await state.clear()
        return

    if not client:
        await message.answer(
            f"❌ Клиент не найден по запросу: <code>{query}</code>",
            parse_mode="HTML"
        )
        await state.clear()
        return

    if action == "edit":
        await state.set_state(DeviceLimitStates.waiting_for_new_limit)
        await state.update_data(client_uuid=client['uuid'], client_email=client['email'])
        await message.answer(
            f"📱 Клиент: <code>{client['email']}</code>\n"
            f"Текущий лимит: <b>{client['ip_limit']}</b> устройств\n\n"
            f"Введите новый лимит (число от 1 до 10, или 0 для безлимитного):",
            parse_mode="HTML"
        )
        return

    # action == "check" — показываем текущие подключения
    await state.clear()

    from bot.services.device_monitor import get_client_ips_from_panel
    from bot.api.remote_xui import load_servers_config

    servers_config = load_servers_config()
    servers = [s for s in servers_config.get('servers', [])
               if s.get('enabled', True) and s.get('panel')]

    text = f"📱 <b>Устройства клиента</b>\n\n"
    text += f"Email: <code>{client['email']}</code>\n"
    text += f"UUID: <code>{client['uuid']}</code>\n"
    text += f"Лимит: <b>{client['ip_limit']}</b> устройств\n"
    text += f"Статус: {client['status']}\n\n"

    all_ips = set()
    for server in servers:
        # Ищем email клиента на этом сервере (может отличаться)
        ips = await get_client_ips_from_panel(server, client['email'])
        server_name = server.get('name', '?')
        if ips:
            all_ips.update(ips)
            text += f"🖥 <b>{server_name}</b>: {', '.join(ips)}\n"
        else:
            text += f"🖥 {server_name}: нет подключений\n"

    text += f"\n<b>Всего уникальных IP: {len(all_ips)}</b>"
    if client['ip_limit'] > 0 and len(all_ips) > client['ip_limit']:
        text += f" ⚠️ <b>ПРЕВЫШЕНИЕ</b> (лимит: {client['ip_limit']})"

    from bot.services.device_monitor import get_blocked_clients
    if client['uuid'] in get_blocked_clients():
        text += "\n\n🚫 <b>Клиент сейчас заблокирован мониторингом</b>"

    await message.answer(text, parse_mode="HTML")


@router.message(DeviceLimitStates.waiting_for_new_limit)
async def process_new_device_limit(message: Message, state: FSMContext, **kwargs):
    """Обработка нового лимита устройств"""
    try:
        new_limit = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите число от 0 до 10")
        return

    if new_limit < 0 or new_limit > 10:
        await message.answer("❌ Лимит должен быть от 0 (безлимитный) до 10")
        return

    data = await state.get_data()
    client_uuid = data.get('client_uuid')
    client_email = data.get('client_email')

    # Обновляем в БД
    try:
        async with aiosqlite.connect(DATABASE_PATH) as db:
            await db.execute(
                "UPDATE clients SET ip_limit = ? WHERE uuid = ?",
                (new_limit, client_uuid)
            )
            # Также обновим в client_servers
            await db.execute(
                "UPDATE client_servers SET ip_limit = ? WHERE client_uuid = ?",
                (new_limit, client_uuid)
            )
            await db.commit()
    except Exception as e:
        await message.answer(f"❌ Ошибка обновления БД: {e}")
        await state.clear()
        return

    # Если клиент был заблокирован и новый лимит больше — разблокируем
    from bot.services.device_monitor import _blocked_clients
    if client_uuid in _blocked_clients:
        del _blocked_clients[client_uuid]

    limit_text = "безлимитный" if new_limit == 0 else f"{new_limit} устройств"
    await message.answer(
        f"✅ Лимит для <code>{client_email}</code> изменён на <b>{limit_text}</b>",
        parse_mode="HTML"
    )
    await state.clear()


# ==================== УПРАВЛЕНИЕ БРЕНДАМИ ====================

class AddBrandStates(StatesGroup):
    waiting_for_token = State()
    waiting_for_domain = State()
    waiting_for_name = State()
    waiting_for_theme = State()
    confirming = State()


class AssignBrandManagerStates(StatesGroup):
    waiting_for_manager_id = State()


@router.message(F.text == "🏷 Бренды")
@admin_only
async def brand_menu(message: Message, db: DatabaseManager, brand_mgr=None, **kwargs):
    """Меню управления брендами"""
    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    brands = await brand_mgr.list_brands()
    from bot.utils.keyboards import Keyboards
    await message.answer(
        f"🏷 <b>Управление брендами</b>\n\n"
        f"Всего брендов: {len(brands)}\n"
        f"Активных: {sum(1 for b in brands if b.is_active)}",
        reply_markup=Keyboards.brand_list_keyboard(brands)
    )


@router.callback_query(F.data == "brand_list")
async def brand_list_callback(callback: CallbackQuery, brand_mgr=None, **kwargs):
    """Список брендов (callback)"""
    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    brands = await brand_mgr.list_brands()
    from bot.utils.keyboards import Keyboards
    await callback.message.edit_text(
        f"🏷 <b>Управление брендами</b>\n\n"
        f"Всего брендов: {len(brands)}\n"
        f"Активных: {sum(1 for b in brands if b.is_active)}",
        reply_markup=Keyboards.brand_list_keyboard(brands)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("brand_view_"))
async def brand_view(callback: CallbackQuery, brand_mgr=None, **kwargs):
    """Просмотр бренда"""
    brand_id = int(callback.data.split("_")[-1])
    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    brand = await brand_mgr.get_brand(brand_id)
    if not brand:
        await callback.answer("Бренд не найден", show_alert=True)
        return

    managers = await brand_mgr.get_brand_managers(brand_id)
    status = "🟢 Активен" if brand.is_active else "🔴 Отключён"

    # Проверяем запущен ли бот
    import builtins
    bot_manager = getattr(builtins, '_bot_manager', None)
    bot_status = "▶️ Запущен" if (bot_manager and bot_manager.is_running(brand_id)) or brand_id == 1 else "⏹ Остановлен"

    text = (
        f"🏷 <b>{brand.name}</b>\n\n"
        f"📌 ID: {brand.id}\n"
        f"🌐 Домен: <code>{brand.domain}</code>\n"
        f"🎨 Цвет: {brand.theme_color}\n"
        f"📊 Статус: {status}\n"
        f"🤖 Бот: {bot_status}\n"
        f"👥 Менеджеров: {len(managers)}\n"
    )

    brand_servers = brand.get_allowed_servers()
    if brand_servers:
        text += f"🖥 Серверы: {', '.join(brand_servers)}\n"
    else:
        text += f"🖥 Серверы: все\n"

    if brand.logo_url:
        text += f"🖼 Лого: {brand.logo_url}\n"

    from bot.utils.keyboards import Keyboards
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.brand_actions_keyboard(brand_id, brand.is_active)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("brand_servers_"))
async def brand_servers_manage(callback: CallbackQuery, state: FSMContext, brand_mgr=None, **kwargs):
    """Управление серверами бренда"""
    brand_id = int(callback.data.split("_")[-1])
    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    brand = await brand_mgr.get_brand(brand_id)
    current_servers = brand.get_allowed_servers() or []

    # Загружаем все серверы
    from bot.api.remote_xui import load_servers_config
    config = load_servers_config()
    all_servers = [s.get('name', '') for s in config.get('servers', []) if s.get('name')]

    buttons = []
    for srv_name in all_servers:
        selected = srv_name in current_servers
        icon = "✅" if selected else "⬜"
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {srv_name}",
            callback_data=f"brand_srv_toggle_{brand_id}_{srv_name}"
        )])
    buttons.append([InlineKeyboardButton(text="☑️ Все серверы", callback_data=f"brand_srv_all_{brand_id}")])
    buttons.append([InlineKeyboardButton(text="💾 Сохранить", callback_data=f"brand_srv_save_{brand_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"brand_view_{brand_id}")])

    await state.update_data(brand_servers=current_servers)
    await callback.message.edit_text(
        f"🖥 <b>Серверы бренда «{brand.name}»</b>\n\n"
        f"Выберите серверы, доступные для этого бренда.\n"
        f"Если ни один не выбран — доступны все.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("brand_srv_toggle_"))
async def brand_srv_toggle(callback: CallbackQuery, state: FSMContext, brand_mgr=None, **kwargs):
    """Переключить сервер для бренда"""
    parts = callback.data.replace("brand_srv_toggle_", "").split("_", 1)
    brand_id = int(parts[0])
    srv_name = parts[1]

    data = await state.get_data()
    servers = data.get('brand_servers', [])

    if srv_name in servers:
        servers.remove(srv_name)
    else:
        servers.append(srv_name)
    await state.update_data(brand_servers=servers)

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    brand = await brand_mgr.get_brand(brand_id)

    from bot.api.remote_xui import load_servers_config
    config = load_servers_config()
    all_servers = [s.get('name', '') for s in config.get('servers', []) if s.get('name')]

    buttons = []
    for name in all_servers:
        selected = name in servers
        icon = "✅" if selected else "⬜"
        buttons.append([InlineKeyboardButton(
            text=f"{icon} {name}",
            callback_data=f"brand_srv_toggle_{brand_id}_{name}"
        )])
    buttons.append([InlineKeyboardButton(text="☑️ Все серверы", callback_data=f"brand_srv_all_{brand_id}")])
    buttons.append([InlineKeyboardButton(text="💾 Сохранить", callback_data=f"brand_srv_save_{brand_id}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"brand_view_{brand_id}")])

    await callback.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


@router.callback_query(F.data.startswith("brand_srv_all_"))
async def brand_srv_select_all(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Выбрать все серверы (= без ограничений)"""
    brand_id = int(callback.data.split("_")[-1])
    await state.update_data(brand_servers=[])
    await callback.answer("Все серверы выбраны (без ограничений)")
    # Trigger re-render
    callback.data = f"brand_servers_{brand_id}"
    await brand_servers_manage(callback, state, **kwargs)


@router.callback_query(F.data.startswith("brand_srv_save_"))
async def brand_srv_save(callback: CallbackQuery, state: FSMContext, brand_mgr=None, **kwargs):
    """Сохранить серверы бренда"""
    brand_id = int(callback.data.split("_")[-1])
    data = await state.get_data()
    servers = data.get('brand_servers', [])

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    # Пустой список = все серверы (NULL)
    await brand_mgr.set_brand_servers(brand_id, servers if servers else None)
    await state.clear()

    brand = await brand_mgr.get_brand(brand_id)
    if servers:
        await callback.answer(f"Сохранено: {', '.join(servers)}")
    else:
        await callback.answer("Сохранено: все серверы")

    # Вернуться к карточке бренда
    callback.data = f"brand_view_{brand_id}"
    await brand_view(callback, brand_mgr=brand_mgr, **kwargs)


@router.callback_query(F.data == "brand_add")
async def brand_add_start(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Начало добавления бренда"""
    await callback.message.edit_text(
        "🏷 <b>Добавление нового бренда</b>\n\n"
        "Шаг 1/4: Отправьте <b>токен Telegram бота</b>\n\n"
        "Получить токен можно у @BotFather"
    )
    await state.set_state(AddBrandStates.waiting_for_token)
    await callback.answer()


@router.message(AddBrandStates.waiting_for_token)
async def brand_add_token(message: Message, state: FSMContext, **kwargs):
    """Получение токена бота"""
    token = message.text.strip()

    # Проверка формата токена
    if ':' not in token or len(token) < 30:
        await message.answer("❌ Неверный формат токена. Отправьте токен от @BotFather:")
        return

    # Проверяем что токен рабочий
    from aiogram import Bot
    try:
        test_bot = Bot(token=token)
        bot_info = await test_bot.get_me()
        await test_bot.session.close()
    except Exception as e:
        await message.answer(f"❌ Токен не валидный: {e}\n\nОтправьте корректный токен:")
        return

    await state.update_data(bot_token=token, bot_username=bot_info.username, bot_name=bot_info.first_name)
    await message.answer(
        f"✅ Бот найден: @{bot_info.username} ({bot_info.first_name})\n\n"
        f"Шаг 2/4: Отправьте <b>домен</b> для подписок\n"
        f"Например: <code>kobra.peakvip.ru</code>\n\n"
        f"⚠️ DNS должен быть уже настроен (A-запись → IP этого сервера)"
    )
    await state.set_state(AddBrandStates.waiting_for_domain)


@router.message(AddBrandStates.waiting_for_domain)
async def brand_add_domain(message: Message, state: FSMContext, **kwargs):
    """Получение домена"""
    domain = message.text.strip().lower()

    # Валидация
    if ' ' in domain or '/' in domain or not '.' in domain:
        await message.answer("❌ Неверный формат домена. Пример: <code>kobra.peakvip.ru</code>")
        return

    # Проверяем что домен не занят
    from bot.database.brand_manager import BrandManager
    from bot.config import DATABASE_PATH
    brand_mgr = BrandManager(DATABASE_PATH)
    existing = await brand_mgr.get_brand_by_domain(domain)
    if existing:
        await message.answer(f"❌ Домен {domain} уже используется брендом '{existing.name}'")
        return

    await state.update_data(domain=domain)
    await message.answer(
        f"✅ Домен: <code>{domain}</code>\n\n"
        f"Шаг 3/4: Отправьте <b>название бренда</b>\n"
        f"Например: <code>KOBRA</code>"
    )
    await state.set_state(AddBrandStates.waiting_for_name)


@router.message(AddBrandStates.waiting_for_name)
async def brand_add_name(message: Message, state: FSMContext, **kwargs):
    """Получение названия бренда"""
    name = message.text.strip()
    if len(name) > 50:
        await message.answer("❌ Название слишком длинное (макс 50 символов)")
        return

    await state.update_data(name=name)

    from bot.utils.keyboards import Keyboards
    await message.answer(
        f"✅ Бренд: <b>{name}</b>\n\n"
        f"Шаг 4/4: Отправьте <b>цвет темы</b> (HEX)\n"
        f"Например: <code>#FF6600</code>\n\n"
        f"Или нажмите «Пропустить» для стандартного цвета",
        reply_markup=Keyboards.brand_skip_theme()
    )
    await state.set_state(AddBrandStates.waiting_for_theme)


@router.callback_query(F.data == "brand_skip_theme", AddBrandStates.waiting_for_theme)
async def brand_skip_theme(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Пропустить выбор темы"""
    await state.update_data(theme_color='#007bff')
    await _show_brand_confirm(callback.message, state, edit=True)
    await callback.answer()


@router.message(AddBrandStates.waiting_for_theme)
async def brand_add_theme(message: Message, state: FSMContext, **kwargs):
    """Получение цвета темы"""
    color = message.text.strip()
    if not color.startswith('#') or len(color) != 7:
        color = '#007bff'

    await state.update_data(theme_color=color)
    await _show_brand_confirm(message, state)


async def _show_brand_confirm(message, state, edit=False):
    """Показать подтверждение создания бренда"""
    data = await state.get_data()
    from bot.utils.keyboards import Keyboards

    text = (
        f"🏷 <b>Подтверждение создания бренда</b>\n\n"
        f"📛 Название: <b>{data['name']}</b>\n"
        f"🤖 Бот: @{data['bot_username']}\n"
        f"🌐 Домен: <code>{data['domain']}</code>\n"
        f"🎨 Цвет: {data['theme_color']}\n\n"
        f"⚠️ После подтверждения:\n"
        f"1. Будет получен SSL-сертификат\n"
        f"2. Настроен nginx\n"
        f"3. Бот будет запущен"
    )

    kb = Keyboards.brand_confirm_create(data)
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)
    await state.set_state(AddBrandStates.confirming)


@router.callback_query(F.data == "brand_confirm_create", AddBrandStates.confirming)
async def brand_confirm_create(callback: CallbackQuery, state: FSMContext, brand_mgr=None, **kwargs):
    """Подтверждение и создание бренда"""
    data = await state.get_data()

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    await callback.message.edit_text("⏳ Создаю бренд и настраиваю домен...")

    # 1. Создаём бренд в БД
    brand_id = await brand_mgr.create_brand(
        name=data['name'],
        bot_token=data['bot_token'],
        domain=data['domain'],
        theme_color=data.get('theme_color', '#007bff'),
        admin_id=callback.from_user.id
    )

    if not brand_id:
        await callback.message.edit_text("❌ Ошибка создания бренда (токен или домен уже используется)")
        await state.clear()
        await callback.answer()
        return

    # 2. Настраиваем SSL + nginx
    from bot.utils.ssl_manager import setup_brand_domain
    from bot.config import WEBAPP_PORT
    ssl_ok, ssl_msg = await setup_brand_domain(data['domain'], port=WEBAPP_PORT, db_path=DATABASE_PATH)

    # 3. Запускаем бота
    import builtins
    bot_manager = getattr(builtins, '_bot_manager', None)
    bot_started = False
    if bot_manager:
        brand = await brand_mgr.get_brand(brand_id)
        bot_started = await bot_manager.start_brand_bot(brand)

    status_parts = [f"✅ Бренд <b>{data['name']}</b> создан (ID: {brand_id})"]

    if ssl_ok:
        status_parts.append(f"✅ SSL + nginx настроены для {data['domain']}")
    else:
        status_parts.append(f"⚠️ SSL/nginx: {ssl_msg}")

    if bot_started:
        status_parts.append(f"✅ Бот @{data['bot_username']} запущен")
    else:
        status_parts.append(f"⚠️ Бот не запущен (запустится при перезагрузке)")

    await callback.message.edit_text("\n".join(status_parts))
    await state.clear()
    await callback.answer()


@router.callback_query(F.data == "brand_cancel_create")
async def brand_cancel_create(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Отмена создания бренда"""
    await state.clear()
    await callback.message.edit_text("❌ Создание бренда отменено")
    await callback.answer()




class EditBrandStates(StatesGroup):
    waiting_for_field_value = State()


@router.callback_query(F.data.startswith("brand_edit_"))
async def brand_edit_menu(callback: CallbackQuery, brand_mgr=None, **kwargs):
    """Меню редактирования бренда"""
    brand_id = int(callback.data.split("_")[-1])
    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    brand = await brand_mgr.get_brand(brand_id)
    if not brand:
        await callback.answer("Бренд не найден", show_alert=True)
        return

    buttons = [
        [InlineKeyboardButton(text=f"📛 Название: {brand.name}", callback_data=f"brand_set_name_{brand_id}")],
        [InlineKeyboardButton(text=f"🌐 Домен: {brand.domain}", callback_data=f"brand_set_domain_{brand_id}")],
        [InlineKeyboardButton(text=f"🎨 Цвет: {brand.theme_color}", callback_data=f"brand_set_color_{brand_id}")],
        [InlineKeyboardButton(text=f"🖼 Лого: {brand.logo_url or 'не задан'}", callback_data=f"brand_set_logo_{brand_id}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"brand_view_{brand_id}")]
    ]

    await callback.message.edit_text(
        f"✏️ <b>Редактирование бренда «{brand.name}»</b>\n\n"
        f"Выберите что изменить:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@router.callback_query(F.data.regexp(r"brand_set_(name|domain|color|logo)_(\d+)"))
async def brand_edit_field(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Начало редактирования поля бренда"""
    parts = callback.data.replace("brand_set_", "").rsplit("_", 1)
    field = parts[0]
    brand_id = int(parts[1])

    field_labels = {
        "name": ("📛 Название", "Отправьте новое название бренда:"),
        "domain": ("🌐 Домен", "Отправьте новый домен (например: kobra.peakvip.ru):"),
        "color": ("🎨 Цвет", "Отправьте HEX цвет (например: #FF6600):"),
        "logo": ("🖼 Лого", "Отправьте <b>изображение</b> (файл/фото) или URL логотипа:")
    }

    label, prompt = field_labels.get(field, ("", "Введите значение:"))

    await state.update_data(edit_brand_id=brand_id, edit_brand_field=field)
    await state.set_state(EditBrandStates.waiting_for_field_value)
    await callback.message.edit_text(f"{label}\n\n{prompt}")
    await callback.answer()


@router.message(EditBrandStates.waiting_for_field_value, F.photo)
async def brand_edit_save_photo(message: Message, state: FSMContext, bot, brand_mgr=None, **kwargs):
    """Сохранение лого бренда из загруженного фото"""
    data = await state.get_data()
    brand_id = data['edit_brand_id']
    field = data['edit_brand_field']

    if field != 'logo':
        await message.answer("❌ Для этого поля нужно отправить текст, не фото")
        return

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    # Скачиваем фото
    import uuid as _uuid
    photo = message.photo[-1]  # Наибольшее разрешение
    file = await bot.get_file(photo.file_id)
    ext = 'jpg'
    filename = f"brand_{brand_id}_logo_{_uuid.uuid4().hex[:8]}.{ext}"
    static_dir = Path(__file__).parent.parent / 'webapp' / 'static'
    save_path = static_dir / filename

    await bot.download_file(file.file_path, save_path)

    logo_url = f"/static/{filename}"
    ok = await brand_mgr.update_brand(brand_id, logo_url=logo_url)
    if ok:
        brand = await brand_mgr.get_brand(brand_id)
        await message.answer(
            f"✅ Лого бренда <b>{brand.name}</b> обновлено\n"
            f"Путь: <code>{logo_url}</code>",
            parse_mode="HTML"
        )
    else:
        await message.answer("❌ Ошибка сохранения")

    await state.clear()


@router.message(EditBrandStates.waiting_for_field_value, F.document)
async def brand_edit_save_document(message: Message, state: FSMContext, bot, brand_mgr=None, **kwargs):
    """Сохранение лого бренда из загруженного документа"""
    data = await state.get_data()
    brand_id = data['edit_brand_id']
    field = data['edit_brand_field']

    if field != 'logo':
        await message.answer("❌ Для этого поля нужно отправить текст, не файл")
        return

    doc = message.document
    if not doc.mime_type or not doc.mime_type.startswith('image/'):
        await message.answer("❌ Отправьте изображение (PNG, JPG, SVG)")
        return

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    import uuid as _uuid
    ext = doc.file_name.rsplit('.', 1)[-1] if '.' in doc.file_name else 'png'
    filename = f"brand_{brand_id}_logo_{_uuid.uuid4().hex[:8]}.{ext}"
    static_dir = Path(__file__).parent.parent / 'webapp' / 'static'
    save_path = static_dir / filename

    file = await bot.get_file(doc.file_id)
    await bot.download_file(file.file_path, save_path)

    logo_url = f"/static/{filename}"
    ok = await brand_mgr.update_brand(brand_id, logo_url=logo_url)
    if ok:
        brand = await brand_mgr.get_brand(brand_id)
        await message.answer(
            f"✅ Лого бренда <b>{brand.name}</b> обновлено\n"
            f"Путь: <code>{logo_url}</code>",
            parse_mode="HTML"
        )
    else:
        await message.answer("❌ Ошибка сохранения")

    await state.clear()


@router.message(EditBrandStates.waiting_for_field_value)
async def brand_edit_save(message: Message, state: FSMContext, brand_mgr=None, **kwargs):
    """Сохранение изменённого поля бренда (текст)"""
    data = await state.get_data()
    brand_id = data['edit_brand_id']
    field = data['edit_brand_field']
    value = message.text.strip() if message.text else ''

    if not value:
        await message.answer("❌ Отправьте текстовое значение")
        return

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    field_map = {"name": "name", "domain": "domain", "color": "theme_color", "logo": "logo_url"}
    db_field = field_map.get(field)

    if not db_field:
        await message.answer("❌ Неизвестное поле")
        await state.clear()
        return

    ok = await brand_mgr.update_brand(brand_id, **{db_field: value})
    if ok:
        await message.answer(f"✅ {field} обновлён на: <b>{value}</b>")
    else:
        await message.answer("❌ Ошибка обновления")

    await state.clear()


@router.callback_query(F.data.startswith("brand_toggle_"))
async def brand_toggle(callback: CallbackQuery, brand_mgr=None, **kwargs):
    """Включить/отключить бренд"""
    brand_id = int(callback.data.split("_")[-1])

    if brand_id == 1:
        await callback.answer("Нельзя отключить основной бренд", show_alert=True)
        return

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    new_status = await brand_mgr.toggle_brand(brand_id)
    if new_status is None:
        await callback.answer("Бренд не найден", show_alert=True)
        return

    import builtins
    bot_manager = getattr(builtins, '_bot_manager', None)

    if new_status:
        # Активирован — запускаем бота
        if bot_manager:
            brand = await brand_mgr.get_brand(brand_id)
            await bot_manager.start_brand_bot(brand)
        await callback.answer("✅ Бренд активирован и бот запущен")
    else:
        # Деактивирован — останавливаем бота
        if bot_manager:
            await bot_manager.stop_brand_bot(brand_id)
        await callback.answer("🔴 Бренд отключён и бот остановлен")

    # Обновляем карточку
    brand = await brand_mgr.get_brand(brand_id)
    managers = await brand_mgr.get_brand_managers(brand_id)
    status = "🟢 Активен" if brand.is_active else "🔴 Отключён"
    bot_status = "▶️ Запущен" if (bot_manager and bot_manager.is_running(brand_id)) else "⏹ Остановлен"

    from bot.utils.keyboards import Keyboards
    await callback.message.edit_text(
        f"🏷 <b>{brand.name}</b>\n\n"
        f"📌 ID: {brand.id}\n"
        f"🌐 Домен: <code>{brand.domain}</code>\n"
        f"📊 Статус: {status}\n"
        f"🤖 Бот: {bot_status}\n"
        f"👥 Менеджеров: {len(managers)}",
        reply_markup=Keyboards.brand_actions_keyboard(brand_id, brand.is_active)
    )


@router.callback_query(F.data.startswith("brand_delete_"))
async def brand_delete_ask(callback: CallbackQuery, **kwargs):
    """Подтверждение удаления бренда"""
    brand_id = int(callback.data.split("_")[-1])

    if brand_id == 1:
        await callback.answer("Нельзя удалить основной бренд", show_alert=True)
        return

    from bot.utils.keyboards import Keyboards
    await callback.message.edit_text(
        "⚠️ <b>Вы уверены?</b>\n\n"
        "Будет удалён бренд, все назначения менеджеров, "
        "бот будет остановлен.\n\n"
        "Клиенты и ключи останутся в базе.",
        reply_markup=Keyboards.brand_confirm_delete(brand_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("brand_confirm_del_"))
async def brand_confirm_delete(callback: CallbackQuery, brand_mgr=None, **kwargs):
    """Удаление бренда"""
    brand_id = int(callback.data.split("_")[-1])

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    brand = await brand_mgr.get_brand(brand_id)
    if not brand:
        await callback.answer("Бренд не найден", show_alert=True)
        return

    # Останавливаем бота
    import builtins
    bot_manager = getattr(builtins, '_bot_manager', None)
    if bot_manager:
        await bot_manager.stop_brand_bot(brand_id)

    # Удаляем nginx конфиг
    from bot.utils.ssl_manager import remove_brand_domain
    await remove_brand_domain(brand.domain, db_path=DATABASE_PATH)

    # Удаляем из БД
    await brand_mgr.delete_brand(brand_id)

    await callback.message.edit_text(f"✅ Бренд <b>{brand.name}</b> удалён")
    await callback.answer()


# ==================== МЕНЕДЖЕРЫ БРЕНДОВ ====================

@router.callback_query(F.data.startswith("brand_managers_"))
async def brand_managers_list(callback: CallbackQuery, brand_mgr=None, **kwargs):
    """Список менеджеров бренда"""
    brand_id = int(callback.data.split("_")[-1])

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    brand = await brand_mgr.get_brand(brand_id)
    managers = await brand_mgr.get_brand_managers(brand_id)

    # Получаем информацию о лимитах
    for mgr in managers:
        limit_info = await brand_mgr.get_manager_key_limit(mgr['manager_id'], brand_id)
        count = await brand_mgr.get_manager_keys_count(mgr['manager_id'], brand_id)
        mgr['key_limit'] = limit_info['limit']
        mgr['is_verified'] = limit_info['verified']
        mgr['keys_count'] = count

    from bot.utils.keyboards import Keyboards
    await callback.message.edit_text(
        f"👥 <b>Менеджеры бренда «{brand.name}»</b>\n\n"
        f"Назначено: {len(managers)}",
        reply_markup=Keyboards.brand_managers_keyboard(brand_id, managers)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("brand_add_mgr_"))
async def brand_add_manager_start(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Начало добавления менеджера в бренд"""
    brand_id = int(callback.data.split("_")[-1])
    await state.update_data(brand_id=brand_id)
    await callback.message.edit_text(
        "👤 <b>Добавление менеджера</b>\n\n"
        "Отправьте <b>Telegram ID</b> менеджера:"
    )
    await state.set_state(AssignBrandManagerStates.waiting_for_manager_id)
    await callback.answer()


@router.message(AssignBrandManagerStates.waiting_for_manager_id)
async def brand_add_manager_process(message: Message, state: FSMContext, db: DatabaseManager, brand_mgr=None, **kwargs):
    """Обработка добавления менеджера в бренд"""
    try:
        manager_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Отправьте числовой Telegram ID")
        return

    data = await state.get_data()
    brand_id = data['brand_id']

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    # Если менеджер не в системе — автоматически добавляем
    if not await db.is_manager(manager_id):
        await db.add_manager(
            user_id=manager_id,
            username=str(manager_id),
            full_name=f"Manager {manager_id}",
            added_by=message.from_user.id
        )
        logger.info(f"Менеджер {manager_id} автоматически добавлен в систему для бренда")

    # Назначаем
    ok = await brand_mgr.assign_manager(brand_id, manager_id)
    if ok:
        brand = await brand_mgr.get_brand(brand_id)
        mgr_info = await db.get_manager(manager_id) if hasattr(db, 'get_manager') else None
        mgr_name = str(manager_id)
        if mgr_info:
            mgr_name = mgr_info.get('custom_name') or mgr_info.get('full_name') or mgr_info.get('username', str(manager_id))

        await message.answer(
            f"✅ Менеджер <b>{mgr_name}</b> (ID: {manager_id}) "
            f"назначен на бренд <b>{brand.name}</b>"
        )
    else:
        await message.answer("❌ Ошибка назначения менеджера")

    await state.clear()


@router.callback_query(F.data.startswith("brand_rm_mgr_"))
async def brand_remove_manager(callback: CallbackQuery, brand_mgr=None, **kwargs):
    """Убрать менеджера из бренда"""
    parts = callback.data.split("_")
    brand_id = int(parts[3])
    manager_id = int(parts[4])

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    await brand_mgr.remove_manager(brand_id, manager_id)
    await callback.answer(f"✅ Менеджер {manager_id} убран из бренда")

    # Обновляем список
    brand = await brand_mgr.get_brand(brand_id)
    managers = await brand_mgr.get_brand_managers(brand_id)

    from bot.utils.keyboards import Keyboards
    await callback.message.edit_text(
        f"👥 <b>Менеджеры бренда «{brand.name}»</b>\n\n"
        f"Назначено: {len(managers)}",
        reply_markup=Keyboards.brand_managers_keyboard(brand_id, managers)
    )




class SetKeyLimitStates(StatesGroup):
    waiting_for_limit = State()


@router.callback_query(F.data.startswith("brand_mgr_info_"))
async def brand_mgr_info(callback: CallbackQuery, brand_mgr=None, **kwargs):
    """Информация о менеджере бренда с опциями лимита"""
    parts = callback.data.replace("brand_mgr_info_", "").split("_")
    brand_id = int(parts[0])
    manager_id = int(parts[1])

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    brand = await brand_mgr.get_brand(brand_id)
    limit_info = await brand_mgr.get_manager_key_limit(manager_id, brand_id)
    count = await brand_mgr.get_manager_keys_count(manager_id, brand_id)

    verified = "✅ Проверен" if limit_info['verified'] else "⏳ Не проверен"
    limit_str = "безлимит" if limit_info['limit'] == 0 or limit_info['verified'] else str(limit_info['limit'])

    buttons = []
    if not limit_info['verified']:
        buttons.append([InlineKeyboardButton(
            text="✅ Подтвердить (безлимит)",
            callback_data=f"brand_verify_mgr_{brand_id}_{manager_id}"
        )])
    else:
        buttons.append([InlineKeyboardButton(
            text="🔒 Снять подтверждение",
            callback_data=f"brand_unverify_mgr_{brand_id}_{manager_id}"
        )])
    buttons.append([InlineKeyboardButton(
        text=f"📝 Установить лимит ({limit_str})",
        callback_data=f"brand_set_limit_{brand_id}_{manager_id}"
    )])
    buttons.append([InlineKeyboardButton(
        text="🔙 Назад",
        callback_data=f"brand_managers_{brand_id}"
    )])

    await callback.message.edit_text(
        f"👤 <b>Менеджер {manager_id}</b>\n"
        f"🏷 Бренд: {brand.name}\n\n"
        f"📊 Статус: {verified}\n"
        f"🔑 Создано ключей: {count}\n"
        f"📏 Лимит: {limit_str}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("brand_verify_mgr_"))
async def brand_verify_manager(callback: CallbackQuery, brand_mgr=None, **kwargs):
    """Подтвердить менеджера — снять лимит"""
    parts = callback.data.replace("brand_verify_mgr_", "").split("_")
    brand_id = int(parts[0])
    manager_id = int(parts[1])

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    await brand_mgr.verify_manager(brand_id, manager_id, True)
    await callback.answer("✅ Менеджер подтверждён — лимит снят")
    # Обновить карточку
    callback.data = f"brand_mgr_info_{brand_id}_{manager_id}"
    await brand_mgr_info(callback, brand_mgr=brand_mgr, **kwargs)


@router.callback_query(F.data.startswith("brand_unverify_mgr_"))
async def brand_unverify_manager(callback: CallbackQuery, brand_mgr=None, **kwargs):
    """Снять подтверждение менеджера — вернуть лимит"""
    parts = callback.data.replace("brand_unverify_mgr_", "").split("_")
    brand_id = int(parts[0])
    manager_id = int(parts[1])

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    await brand_mgr.verify_manager(brand_id, manager_id, False)
    await callback.answer("🔒 Подтверждение снято — лимит 5 ключей")
    callback.data = f"brand_mgr_info_{brand_id}_{manager_id}"
    await brand_mgr_info(callback, brand_mgr=brand_mgr, **kwargs)


@router.callback_query(F.data.startswith("brand_set_limit_"))
async def brand_set_limit_start(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Начать установку лимита"""
    parts = callback.data.replace("brand_set_limit_", "").split("_")
    brand_id = int(parts[0])
    manager_id = int(parts[1])

    await state.update_data(limit_brand_id=brand_id, limit_manager_id=manager_id)
    await state.set_state(SetKeyLimitStates.waiting_for_limit)
    await callback.message.edit_text(
        "📏 <b>Установка лимита ключей</b>\n\n"
        "Отправьте число:\n"
        "• <b>0</b> = безлимит\n"
        "• <b>5</b>, <b>10</b>, <b>50</b> и т.д. = конкретный лимит"
    )
    await callback.answer()


@router.message(SetKeyLimitStates.waiting_for_limit)
async def brand_set_limit_save(message: Message, state: FSMContext, brand_mgr=None, **kwargs):
    """Сохранить лимит"""
    try:
        limit = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Отправьте число")
        return

    if limit < 0:
        await message.answer("❌ Лимит не может быть отрицательным")
        return

    data = await state.get_data()
    brand_id = data['limit_brand_id']
    manager_id = data['limit_manager_id']

    if not brand_mgr:
        from bot.database.brand_manager import BrandManager
        from bot.config import DATABASE_PATH
        brand_mgr = BrandManager(DATABASE_PATH)

    await brand_mgr.set_manager_key_limit(brand_id, manager_id, limit)
    limit_str = "безлимит" if limit == 0 else str(limit)
    await message.answer(f"✅ Лимит для менеджера {manager_id} установлен: <b>{limit_str}</b>")
    await state.clear()

@router.callback_query(F.data == "brand_back")
async def brand_back_to_admin(callback: CallbackQuery, **kwargs):
    """Вернуться в админ меню"""
    from bot.utils.keyboards import Keyboards
    await callback.message.delete()
    await callback.message.answer("Панель администратора", reply_markup=Keyboards.admin_menu())
    await callback.answer()
