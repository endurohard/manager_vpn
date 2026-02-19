"""
Обработчики для администратора
"""
import logging
import asyncio
from functools import wraps
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.config import ADMIN_ID, INBOUND_ID, DOMAIN
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


class AddToSubscriptionStates(StatesGroup):
    """Состояния для добавления сервера в подписку"""
    waiting_for_search = State()
    waiting_for_server_select = State()
    waiting_for_traffic_choice = State()  # Выбор трафика для серверов с лимитом
    confirming = State()


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

    # Получаем список серверов
    servers_config = load_servers_config()
    servers = servers_config.get('servers', [])

    if not servers:
        await message.answer(
            "❌ Нет доступных серверов.",
            reply_markup=Keyboards.admin_menu()
        )
        await state.clear()
        return

    await state.update_data(servers=servers)
    await state.set_state(AdminCreateKeyStates.waiting_for_server)
    await message.answer(
        f"🆔 Сгенерирован ID: <code>{user_id_value}</code>\n\n"
        f"🖥 <b>Выберите сервер:</b>\n"
        f"🟢 - активен для новых\n"
        f"🟡 - отключен для новых\n"
        f"🔴 - выключен",
        reply_markup=Keyboards.server_selection(servers),
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

    # Получаем список серверов
    servers_config = load_servers_config()
    servers = servers_config.get('servers', [])

    if not servers:
        await message.answer(
            "❌ Нет доступных серверов.",
            reply_markup=Keyboards.admin_menu()
        )
        await state.clear()
        return

    await state.update_data(servers=servers)
    await state.set_state(AdminCreateKeyStates.waiting_for_server)
    await message.answer(
        f"🆔 ID клиента: <code>{user_input}</code>\n\n"
        f"🖥 <b>Выберите сервер:</b>\n"
        f"🟢 - активен для новых\n"
        f"🟡 - отключен для новых\n"
        f"🔴 - выключен",
        reply_markup=Keyboards.server_selection(servers),
        parse_mode="HTML"
    )


@router.callback_query(AdminCreateKeyStates.waiting_for_server, F.data.startswith("server_"))
async def admin_process_server(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора сервера"""
    server_idx = int(callback.data.split("_", 1)[1])
    data = await state.get_data()
    servers = data.get('servers', [])

    if server_idx >= len(servers):
        await callback.answer("Ошибка: сервер не найден", show_alert=True)
        return

    selected_server = servers[server_idx]
    await state.update_data(selected_server=selected_server, server_idx=server_idx)

    # Показываем inbound'ы этого сервера из конфига
    inbounds = selected_server.get('inbounds', {})

    if not inbounds:
        await callback.answer("У сервера нет inbound'ов", show_alert=True)
        return

    server_name = selected_server.get('name', 'Unknown')
    await state.set_state(AdminCreateKeyStates.waiting_for_inbound)
    await callback.message.edit_text(
        f"🖥 Сервер: <b>{server_name}</b>\n\n"
        f"🔌 <b>Выберите inbound:</b>",
        reply_markup=Keyboards.inbound_selection_from_config(inbounds, server_name),
        parse_mode="HTML"
    )
    await callback.answer()


@router.callback_query(AdminCreateKeyStates.waiting_for_server, F.data == "back_to_servers")
@router.callback_query(AdminCreateKeyStates.waiting_for_inbound, F.data == "back_to_servers")
async def admin_back_to_servers(callback: CallbackQuery, state: FSMContext):
    """Возврат к выбору сервера"""
    data = await state.get_data()
    servers = data.get('servers', [])
    phone = data.get('phone', '')

    await state.set_state(AdminCreateKeyStates.waiting_for_server)
    await callback.message.edit_text(
        f"🆔 ID клиента: <code>{phone}</code>\n\n"
        f"🖥 <b>Выберите сервер:</b>",
        reply_markup=Keyboards.server_selection(servers),
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
                           xui_client: XUIClient, bot):
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
        subscription_url = f"https://zov-gor.ru/sub/{client_uuid}"

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
            await message.answer(
                f"Менеджер с ID {user_id} успешно добавлен!\n\n"
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


@router.message(F.text == "Список менеджеров")
@admin_only
async def show_managers_list(message: Message, db: DatabaseManager, **kwargs):
    """Показать список всех менеджеров с возможностью редактирования"""
    managers = await db.get_all_managers()

    if not managers:
        await message.answer("Список менеджеров пуст.")
        return

    text = "👥 <b>СПИСОК МЕНЕДЖЕРОВ</b>\n\n"
    text += "Нажмите кнопку \"✏️\" чтобы изменить имя менеджера\n\n"

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

        # Кнопка редактирования
        buttons.append([
            InlineKeyboardButton(
                text=f"✏️ {display_name[:20]}...",
                callback_data=f"edit_mgr_name_{manager['user_id']}"
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
async def cancel_key_deletion(callback: CallbackQuery):
    """Отмена удаления ключа"""
    await callback.message.delete()
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
async def process_search_query(message: Message, state: FSMContext, db: DatabaseManager):
    """Обработка поискового запроса - ищет в базе и на X-UI серверах"""
    query = message.text.strip()

    # Если пользователь нажал кнопку меню - выходим из режима поиска
    admin_menu_buttons = {
        "📡 Добавить сервер", "🔑 Создать ключ (выбор inbound)",
        "Добавить менеджера", "Список менеджеров", "Общая статистика",
        "Детальная статистика", "💰 Изменить цены", "🔍 Поиск ключа",
        "🗑️ Удалить ключ", "📢 Отправить уведомление", "🌐 Управление SNI",
        "💳 Реквизиты", "📋 Веб-заказы", "🖥 Статус серверов", "🔧 Панели X-UI",
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

            sub_url = f"https://zov-gor.ru/sub/{client.get('uuid', '')}" if client.get('uuid') else ''

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
async def get_client_link_callback(callback: CallbackQuery):
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
        sub_url = f"https://zov-gor.ru/sub/{full_uuid}"

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

        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    else:
        await callback.message.answer("❌ Не удалось сгенерировать ссылку")


# ==================== УПРАВЛЕНИЕ ВЕБ-ЗАКАЗАМИ И РЕКВИЗИТАМИ ====================

import json
import aiosqlite
from pathlib import Path

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
async def approve_web_order(message: Message, db: DatabaseManager, xui_client):
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
                subscription_url = f"https://zov-gor.ru/sub/{client_uuid}" if client_uuid else ""

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
async def callback_approve_web_order(callback: CallbackQuery, db: DatabaseManager, xui_client):
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
                subscription_url = f"https://zov-gor.ru/sub/{client_uuid}" if client_uuid else ""

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

    servers = config.get('servers', [])
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
                    async with aiohttp.ClientSession(connector=connector) as session:
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

    # Добавляем время проверки
    from datetime import datetime
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
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"server_{action}_{srv_name}")])

    # Кнопка добавления нового сервера
    buttons.append([InlineKeyboardButton(text="➕ Добавить сервер", callback_data="add_new_server")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=keyboard
    )


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

    # Показываем подтверждение
    data = await state.get_data()

    text = (
        "📋 <b>ПРОВЕРЬТЕ ДАННЫЕ СЕРВЕРА</b>\n\n"
        f"📛 Название: <b>{data['name']}</b>\n"
        f"🌐 IP: <code>{data['ip']}</code>\n"
        f"🔗 Домен: <code>{data['domain']}</code>\n"
        f"🖥 Панель: <code>{data.get('panel_url', '')}</code>\n"
        f"👤 Логин: {panel_username}\n\n"
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
        async with aiohttp.ClientSession(connector=connector) as session:
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
        async with aiohttp.ClientSession(connector=connector) as session:
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

                                    inbound_config = {
                                        "id": int(inbound_id),
                                        "security": security,
                                        "flow": flow,
                                        "fp": "chrome",
                                        "name_prefix": f"🌐 {remark}"
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
        "inbounds": inbounds_data if inbounds_data else {
            "main": {
                "id": 1,
                "security": "reality",
                "sni": "example.com",
                "pbk": "",
                "sid": "",
                "flow": "",
                "fp": "chrome",
                "name_prefix": "🌐 Main"
            }
        }
    }

    # Сохраняем в конфиг
    config = load_servers_config()
    config['servers'].append(new_server)
    save_servers_config(config)

    await state.clear()

    inbounds_info = ""
    if inbounds_data:
        inbounds_info = f"\n\n📋 Найдено inbound'ов: {len(inbounds_data)}\n"
        for key, val in inbounds_data.items():
            inbounds_info += f"   • {key}: {val.get('sni', 'N/A')}\n"
    else:
        inbounds_info = "\n\n⚠️ Inbound'ы не найдены автоматически.\nНастройте вручную в servers_config.json"

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
async def process_add_sub_search(message: Message, state: FSMContext):
    """Обработка поискового запроса для добавления сервера"""
    query = message.text.strip()

    # Если пользователь нажал кнопку меню - выходим из режима поиска
    admin_menu_buttons = {
        "📡 Добавить сервер", "🔑 Создать ключ (выбор inbound)",
        "Добавить менеджера", "Список менеджеров", "Общая статистика",
        "Детальная статистика", "💰 Изменить цены", "🔍 Поиск ключа",
        "🗑️ Удалить ключ", "📢 Отправить уведомление", "🌐 Управление SNI",
        "💳 Реквизиты", "📋 Веб-заказы", "🖥 Статус серверов", "🔧 Панели X-UI",
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

        sub_url = f"https://zov-gor.ru/sub/{client['uuid']}"

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
