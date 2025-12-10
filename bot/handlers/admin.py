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
    """Состояния для управления SNI адресами"""
    waiting_for_sni_domains = State()


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
                    ip_limit=2
                )
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
            domain = selected_server.get('domain', selected_server.get('ip', ''))
            port = selected_server.get('port', 443)

            params = ["type=tcp", f"security={selected_inbound.get('security', 'reality')}"]

            if selected_inbound.get('security') == 'reality':
                if selected_inbound.get('sni'):
                    params.append(f"sni={selected_inbound['sni']}")
                if selected_inbound.get('pbk'):
                    params.append(f"pbk={selected_inbound['pbk']}")
                if selected_inbound.get('sid'):
                    params.append(f"sid={selected_inbound['sid']}")
                params.append(f"fp={selected_inbound.get('fp', 'chrome')}")
                if selected_inbound.get('flow'):
                    params.append(f"flow={selected_inbound['flow']}")

            query = '&'.join(params)
            name_prefix = selected_inbound.get('name_prefix', server_name)
            encoded_name = urllib.parse.quote(name_prefix)

            vless_link_for_user = f"vless://{client_uuid}@{domain}:{port}?{query}#{encoded_name}"
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

        # QR код
        try:
            qr_code = generate_qr_code(vless_link_for_user)
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
    client_email = key.get('client_email', '')

    # Удаляем клиента из X-UI если есть email
    if client_email:
        try:
            async with XUIClient(XUI_HOST, XUI_USERNAME, XUI_PASSWORD) as xui:
                xui_deleted = await xui.find_and_delete_client(client_email)
                if xui_deleted:
                    logger.info(f"Клиент {client_email} удален из X-UI панели")
                else:
                    logger.warning(f"Клиент {client_email} не найден в X-UI панели (возможно уже удален)")
        except Exception as e:
            logger.error(f"Ошибка при удалении клиента из X-UI: {e}")
            xui_deleted = False

    # Удаляем запись из базы данных
    db_success = await db.delete_key_record(key_id)

    if db_success:
        if xui_deleted:
            result_text = (
                f"✅ <b>Ключ полностью удален!</b>\n\n"
                f"📱 Номер/ID: <code>{key['phone_number']}</code>\n"
                f"📅 Срок: {key['period']}\n"
                f"💰 Цена: {key['price']} ₽\n\n"
                f"✅ Удален из X-UI панели\n"
                f"✅ Удален из аналитики бота"
            )
        else:
            result_text = (
                f"⚠️ <b>Запись удалена частично</b>\n\n"
                f"📱 Номер/ID: <code>{key['phone_number']}</code>\n"
                f"📅 Срок: {key['period']}\n"
                f"💰 Цена: {key['price']} ₽\n\n"
                f"❌ Не найден в X-UI панели\n"
                f"✅ Удален из аналитики бота\n\n"
                f"<i>Возможно ключ уже был удален из X-UI ранее</i>"
            )
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


# ===== УПРАВЛЕНИЕ SNI АДРЕСАМИ =====

@router.message(F.text == "🌐 Управление SNI")
@admin_only
async def show_sni_management(message: Message, **kwargs):
    """Показать список Reality inbound-ов для управления SNI"""
    from bot.api.xui_client import XUIClient
    from bot.config import XUI_HOST, XUI_USERNAME, XUI_PASSWORD
    import json
    import subprocess

    await message.answer("⏳ Получаю список Reality inbound-ов...")

    try:
        # Подключаемся к X-UI API
        async with XUIClient(XUI_HOST, XUI_USERNAME, XUI_PASSWORD) as xui:
            inbounds = await xui.list_inbounds()

            if not inbounds:
                await message.answer(
                    "❌ Не удалось получить список inbound-ов.\n"
                    "Проверьте подключение к X-UI панели.",
                    reply_markup=Keyboards.admin_menu()
                )
                return

            # Фильтруем только Reality inbound-ы
            reality_inbounds = []
            for inbound in inbounds:
                try:
                    stream_settings = json.loads(inbound.get('streamSettings', '{}'))
                    if stream_settings.get('security') == 'reality':
                        reality_inbounds.append(inbound)
                except:
                    continue

            if not reality_inbounds:
                await message.answer(
                    "📋 Reality inbound-ы не найдены.\n\n"
                    "В системе нет inbound-ов с Reality протоколом.",
                    reply_markup=Keyboards.admin_menu()
                )
                return

            # Формируем список с текущими SNI
            text = "🌐 <b>УПРАВЛЕНИЕ SNI АДРЕСАМИ</b>\n\n"
            text += "Список Reality inbound-ов:\n\n"

            for inbound in reality_inbounds:
                inbound_id = inbound.get('id')
                remark = inbound.get('remark', f'Inbound {inbound_id}')
                port = inbound.get('port', '?')

                # Получаем текущие SNI
                stream_settings = json.loads(inbound.get('streamSettings', '{}'))
                reality_settings = stream_settings.get('realitySettings', {})
                server_names = reality_settings.get('serverNames', [])
                dest = reality_settings.get('dest', 'не указан')

                text += f"📍 <b>{remark}</b> (ID: {inbound_id}, Port: {port}→443)\n"
                text += f"   🎯 Dest: <code>{dest}</code>\n"
                text += f"   🌐 SNI: <code>{', '.join(server_names) if server_names else 'не указаны'}</code>\n\n"

            text += "━━━━━━━━━━━━━━━━\n\n"
            text += "Выберите inbound для изменения SNI адресов:"

            await message.answer(
                text,
                parse_mode="HTML",
                reply_markup=Keyboards.sni_inbound_list(reality_inbounds)
            )

    except Exception as e:
        logger.error(f"Ошибка при получении списка Reality inbound-ов: {e}")
        await message.answer(
            f"❌ Произошла ошибка при получении данных:\n{str(e)}",
            reply_markup=Keyboards.admin_menu()
        )


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
async def process_new_sni_domains(message: Message, state: FSMContext, xui_client):
    """Обработка новых SNI доменов"""
    from bot.api.xui_client import XUIClient
    from bot.config import XUI_HOST, XUI_USERNAME, XUI_PASSWORD
    import re
    import subprocess

    # Получаем данные из состояния
    data = await state.get_data()
    inbound_id = data.get('inbound_id')
    inbound_remark = data.get('inbound_remark')
    current_dest = data.get('current_dest')
    current_sni = data.get('current_sni', [])

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

    # Показываем подтверждение
    text = f"🌐 <b>ПОДТВЕРЖДЕНИЕ ИЗМЕНЕНИЙ</b>\n\n"
    text += f"📍 <b>Inbound:</b> {inbound_remark} (ID: {inbound_id})\n"
    text += f"🎯 <b>Dest:</b> <code>{current_dest}</code>\n\n"
    text += f"━━━━━━━━━━━━━━━━\n\n"

    text += f"<b>Текущие SNI:</b>\n"
    if current_sni:
        for sni in current_sni:
            text += f"  • <code>{sni}</code>\n"
    else:
        text += f"  <i>Не указаны</i>\n"

    text += f"\n<b>⬇️ Новые SNI:</b>\n"
    for sni in domains:
        text += f"  • <code>{sni}</code>\n"

    text += f"\n━━━━━━━━━━━━━━━━\n\n"
    text += f"⏳ Применяю изменения..."

    msg = await message.answer(text, parse_mode="HTML")

    try:
        # Обновляем SNI через API
        async with XUIClient(XUI_HOST, XUI_USERNAME, XUI_PASSWORD) as xui:
            success = await xui.update_reality_settings(
                inbound_id=inbound_id,
                dest=current_dest,
                server_names=domains
            )

            if not success:
                await msg.edit_text(
                    f"{text}\n\n❌ <b>Ошибка при обновлении SNI!</b>\n"
                    f"Не удалось применить изменения через X-UI API.",
                    parse_mode="HTML"
                )
                await state.clear()
                return

        # Перезапускаем x-ui
        await msg.edit_text(
            f"{text}\n\n✅ <b>SNI обновлены!</b>\n⏳ Перезапускаю x-ui...",
            parse_mode="HTML"
        )

        restart_result = subprocess.run(
            ["systemctl", "restart", "x-ui"],
            capture_output=True,
            text=True
        )

        if restart_result.returncode == 0:
            # Даём x-ui время на инициализацию и очистку базы
            await asyncio.sleep(5)

            # Сбрасываем сессию основного xui_client для переавторизации
            xui_client.session_cookie = None

            # Проверяем статус
            status_result = subprocess.run(
                ["systemctl", "is-active", "x-ui"],
                capture_output=True,
                text=True
            )

            if "active" in status_result.stdout:
                await msg.edit_text(
                    f"{text}\n\n"
                    f"✅ <b>УСПЕШНО ОБНОВЛЕНО!</b>\n\n"
                    f"🔄 x-ui перезапущен\n"
                    f"🌐 Новые SNI активны\n\n"
                    f"Изменения вступили в силу!",
                    parse_mode="HTML"
                )
            else:
                await msg.edit_text(
                    f"{text}\n\n"
                    f"⚠️ <b>SNI обновлены, но x-ui не запустился!</b>\n\n"
                    f"Проверьте статус сервиса вручную:\n"
                    f"<code>systemctl status x-ui</code>",
                    parse_mode="HTML"
                )
        else:
            await msg.edit_text(
                f"{text}\n\n"
                f"⚠️ <b>SNI обновлены, но не удалось перезапустить x-ui!</b>\n\n"
                f"Ошибка: <code>{restart_result.stderr}</code>\n\n"
                f"Перезапустите вручную:\n"
                f"<code>systemctl restart x-ui</code>",
                parse_mode="HTML"
            )

    except Exception as e:
        logger.error(f"Ошибка при обновлении SNI: {e}")
        await msg.edit_text(
            f"{text}\n\n"
            f"❌ <b>ОШИБКА!</b>\n\n"
            f"Не удалось обновить SNI:\n"
            f"<code>{str(e)}</code>",
            parse_mode="HTML"
        )

    await state.clear()


@router.callback_query(F.data == "sni_cancel")
async def cancel_sni_management(callback: CallbackQuery):
    """Отмена управления SNI"""
    await callback.message.delete()
    await callback.answer("Отменено")


# ===== ПОИСК КЛЮЧЕЙ =====

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
    """Обработка поискового запроса"""
    query = message.text.strip()

    if len(query) < 2:
        await message.answer("❌ Введите минимум 2 символа для поиска.")
        return

    # Ищем ключи
    keys = await db.search_keys(query)

    if not keys:
        await message.answer(
            f"🔍 По запросу «<b>{query}</b>» ничего не найдено.\n\n"
            "Попробуйте другой запрос или нажмите 'Отмена' для выхода.",
            parse_mode="HTML"
        )
        return

    await state.clear()

    text = f"🔍 <b>РЕЗУЛЬТАТЫ ПОИСКА</b>\n"
    text += f"Запрос: «{query}»\n"
    text += f"Найдено: {len(keys)} ключей\n\n"
    text += "━━━━━━━━━━━━━━━━\n\n"

    buttons = []

    for idx, key in enumerate(keys[:20], 1):  # Ограничиваем 20 результатами
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
        price = key.get('price', 0) or 0

        # Отмечаем оплаченные/неоплаченные
        if price > 0:
            price_status = f"💰 {price} ₽"
        else:
            price_status = "❌ Не оплачен"

        text += f"{idx}. <b>{key['phone_number']}</b>\n"
        text += f"   👤 Менеджер: {manager_name}\n"
        text += f"   📅 Срок: {key['period']}\n"
        text += f"   {price_status}\n"
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
            text += "\n<i>... показаны первые результаты</i>"
            break

    if len(keys) > 20:
        text += f"\n<i>Показано 20 из {len(keys)} результатов</i>"

    # Добавляем кнопки
    buttons.append([InlineKeyboardButton(text="🔍 Новый поиск", callback_data="new_search")])
    buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="cancel_key_delete")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


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
    
    # Генерируем ключ через X-UI
    try:
        status_msg = await message.answer("⏳ Генерирую ключ...")

        # Используем контакт как email/имя клиента
        client_name = f"web_{order_id}_{order_dict['contact'].replace('@', '').replace('+', '')[:15]}"

        # Создаем клиента в X-UI
        client_data = await xui_client.add_client(
            inbound_id=12,  # Используем inbound 12 по умолчанию
            email=client_name,
            phone=client_name,
            expire_days=order_dict["days"],
            ip_limit=2
        )

        if client_data and not client_data.get('error'):
            # Получаем VLESS ссылку
            vless_key = await xui_client.get_client_link(
                inbound_id=12,
                client_email=client_name
            )

            if vless_key:
                # Формируем ссылку подписки
                client_uuid = client_data.get('client_id', '')
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

    # Генерируем ключ через X-UI (по умолчанию inbound 12)
    try:
        client_name = f"web_{order_id}_{order_dict['contact'].replace('@', '').replace('+', '')[:15]}"

        # Создаем клиента в X-UI
        client_data = await xui_client.add_client(
            inbound_id=12,  # Используем inbound 12 по умолчанию
            email=client_name,
            phone=client_name,
            expire_days=order_dict["days"],
            ip_limit=2
        )

        if client_data and not client_data.get('error'):
            # Получаем VLESS ссылку
            vless_key = await xui_client.get_client_link(
                inbound_id=12,
                client_email=client_name
            )

            if vless_key:
                # Формируем ссылку подписки
                client_uuid = client_data.get('client_id', '')
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
    if domain == "-":
        data = await state.get_data()
        domain = data.get('ip', '')  # Используем IP как домен

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
            # Тест авторизации
            login_url = f"{panel_url}/login"
            async with session.post(login_url, json={"username": panel_username, "password": panel_password}, timeout=aiohttp.ClientTimeout(total=15)) as resp:
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
            # Авторизация
            login_url = f"{panel_url}/login"
            await session.post(login_url, json={"username": panel_username, "password": panel_password}, timeout=aiohttp.ClientTimeout(total=15))

            # Получаем inbound'ы через API
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

                                if security == 'reality':
                                    reality = stream_settings.get('realitySettings', {})
                                    sni_list = reality.get('serverNames', [])
                                    sni = sni_list[0] if sni_list else ''
                                    pbk = reality.get('settings', {}).get('publicKey', '')
                                    short_ids = reality.get('shortIds', [])
                                    sid = short_ids[0] if short_ids else ''
                                    fp = reality.get('settings', {}).get('fingerprint', 'chrome')

                                    inbound_key = remark.lower().replace(' ', '_').replace('-', '_')
                                    inbounds_data[inbound_key] = {
                                        "id": int(inbound_id),
                                        "security": "reality",
                                        "sni": sni,
                                        "pbk": pbk,
                                        "sid": sid,
                                        "flow": "",
                                        "fp": fp,
                                        "name_prefix": f"🌐 {remark}"
                                    }
                            except:
                                pass
    except Exception as e:
        logger.error(f"Ошибка получения inbound'ов через API: {e}")

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
