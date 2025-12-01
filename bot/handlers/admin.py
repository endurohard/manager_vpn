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
    waiting_for_inbound = State()
    waiting_for_period = State()
    confirming = State()


class ExternalKeyStates(StatesGroup):
    """Состояния для создания ключа на внешнем сервере"""
    waiting_for_inbound = State()
    waiting_for_period = State()
    waiting_for_custom_days = State()
    waiting_for_custom_price = State()
    waiting_for_manual_period = State()  # Ручной ввод даты или дней
    waiting_for_phone = State()
    confirming = State()


class AddExternalServerStates(StatesGroup):
    """Состояния для добавления внешнего сервера"""
    waiting_for_name = State()
    waiting_for_url = State()
    waiting_for_credentials = State()
    waiting_for_inbound_id = State()
    waiting_for_domain = State()


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
    """Генерация ID и показ выбора inbound"""
    user_id_value = generate_user_id()
    await state.update_data(phone=user_id_value)

    # Получаем список inbound'ов
    inbounds = await xui_client.list_inbounds()

    if not inbounds:
        await message.answer(
            "❌ Не удалось получить список inbound'ов.",
            reply_markup=Keyboards.admin_menu()
        )
        await state.clear()
        return

    await state.set_state(AdminCreateKeyStates.waiting_for_inbound)
    await message.answer(
        f"🆔 Сгенерирован ID: <code>{user_id_value}</code>\n\n"
        f"🔌 <b>Выберите inbound для создания ключа:</b>",
        reply_markup=Keyboards.inbound_selection(inbounds),
        parse_mode="HTML"
    )


@router.message(AdminCreateKeyStates.waiting_for_phone)
async def admin_process_phone(message: Message, state: FSMContext, xui_client: XUIClient):
    """Обработка введенного ID и показ выбора inbound"""
    user_input = message.text.strip()

    if len(user_input) < 3:
        await message.answer("Идентификатор слишком короткий. Минимум 3 символа.")
        return

    await state.update_data(phone=user_input)

    # Получаем список inbound'ов
    inbounds = await xui_client.list_inbounds()

    if not inbounds:
        await message.answer(
            "❌ Не удалось получить список inbound'ов.",
            reply_markup=Keyboards.admin_menu()
        )
        await state.clear()
        return

    await state.set_state(AdminCreateKeyStates.waiting_for_inbound)
    await message.answer(
        f"🆔 ID клиента: <code>{user_input}</code>\n\n"
        f"🔌 <b>Выберите inbound для создания ключа:</b>",
        reply_markup=Keyboards.inbound_selection(inbounds),
        parse_mode="HTML"
    )


@router.callback_query(AdminCreateKeyStates.waiting_for_inbound, F.data.startswith("inbound_"))
async def admin_process_inbound(callback: CallbackQuery, state: FSMContext):
    """Обработка выбора inbound"""
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
    """Создание ключа с выбранным inbound"""
    data = await state.get_data()
    phone = data.get("phone")
    inbound_id = data.get("inbound_id")
    period_name = data.get("period_name")
    period_days = data.get("period_days")
    period_price = data.get("period_price", 0)

    await callback.message.edit_text("⏳ Создание ключа...")

    try:
        # Создаем клиента
        client_data = await xui_client.add_client(
            inbound_id=inbound_id,
            email=phone,
            phone=phone,
            expire_days=period_days,
            ip_limit=2
        )

        if not client_data:
            await callback.message.edit_text("❌ Ошибка при создании ключа в X-UI панели.")
            await state.clear()
            return

        if client_data.get('error'):
            error_message = client_data.get('message', 'Неизвестная ошибка')
            if client_data.get('is_duplicate'):
                await callback.message.edit_text(
                    f"⚠️ Клиент с ID <code>{phone}</code> уже существует!",
                    parse_mode="HTML"
                )
            else:
                await callback.message.edit_text(f"❌ Ошибка: {error_message}")
            await state.clear()
            await callback.message.answer("Панель администратора:", reply_markup=Keyboards.admin_menu())
            return

        # Получаем VLESS ссылку
        vless_link_original = await xui_client.get_client_link(
            inbound_id=inbound_id,
            client_email=phone,
            use_domain=None
        )

        if not vless_link_original:
            await callback.message.edit_text("Ключ создан, но не удалось сформировать VLESS ссылку.")
            await state.clear()
            return

        # Заменяем IP на домен
        vless_link_for_user = XUIClient.replace_ip_with_domain(vless_link_original, DOMAIN)

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
        client_uuid = client_data['client_id']
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


# ===== УПРАВЛЕНИЕ ВНЕШНИМИ СЕРВЕРАМИ (кнопка меню) =====

@router.message(F.text == "🖥 Внешние серверы")
@admin_only
async def show_external_servers_menu(message: Message, db: DatabaseManager, state: FSMContext, **kwargs):
    """Показать меню внешних серверов из кнопки меню"""
    await state.clear()
    servers = await db.get_external_servers(active_only=False)

    text = "🌐 <b>Внешние серверы</b>\n\n"
    if servers:
        text += f"Найдено серверов: {len(servers)}\n"
        text += "Выберите сервер для управления:"
    else:
        text += "Серверов пока нет.\nНажмите «➕ Добавить сервер» для добавления."

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.external_servers_list(servers)
    )


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



# ===== СОЗДАНИЕ КЛЮЧЕЙ НА ВНЕШНЕМ СЕРВЕРЕ =====

@router.message(F.text == "🌍 Создать ключ (внешний сервер)")
@admin_only
async def start_external_key_creation(message: Message, state: FSMContext, **kwargs):
    """Начало создания ключа на внешнем сервере"""
    from bot.api.external_xui import get_external_xui_client, EXTERNAL_SERVER_CONFIG

    await message.answer("⏳ Подключение к внешнему серверу...")

    try:
        client = get_external_xui_client()
        async with client:
            inbounds = await client.list_inbounds()

            if not inbounds:
                await message.answer(
                    "❌ Не удалось получить список inbound-ов с внешнего сервера.\n"
                    "Проверьте настройки подключения.",
                    reply_markup=Keyboards.admin_menu()
                )
                return

            # Сохраняем список inbound-ов для последующего использования
            await state.update_data(external_inbounds=inbounds)
            await state.set_state(ExternalKeyStates.waiting_for_inbound)

            text = f"🌍 <b>ВНЕШНИЙ СЕРВЕР</b>\n\n"
            text += f"📡 Сервер: <code>{EXTERNAL_SERVER_CONFIG['server_address']}</code>\n"
            text += f"📋 Найдено inbound-ов: {len(inbounds)}\n\n"
            text += "Выберите inbound для создания ключа:"

            await message.answer(
                text,
                parse_mode="HTML",
                reply_markup=Keyboards.external_inbound_list(inbounds)
            )

    except Exception as e:
        logger.error(f"Ошибка подключения к внешнему серверу: {e}")
        await message.answer(
            f"❌ Ошибка подключения к внешнему серверу:\n{str(e)}",
            reply_markup=Keyboards.admin_menu()
        )


@router.callback_query(F.data == "ext_cancel")
async def cancel_external_key(callback: CallbackQuery, state: FSMContext):
    """Отмена создания ключа на внешнем сервере"""
    await state.clear()
    await callback.message.delete()
    await callback.answer("Отменено")


@router.callback_query(F.data.startswith("ext_inbound_"), ExternalKeyStates.waiting_for_inbound)
async def select_external_inbound(callback: CallbackQuery, state: FSMContext):
    """Выбор inbound на внешнем сервере"""
    inbound_id = int(callback.data.replace("ext_inbound_", ""))

    # Получаем данные о выбранном inbound
    data = await state.get_data()
    inbounds = data.get('external_inbounds', [])
    selected_inbound = next((i for i in inbounds if i.get('id') == inbound_id), None)

    if not selected_inbound:
        await callback.answer("Inbound не найден")
        return

    await state.update_data(
        selected_inbound_id=inbound_id,
        selected_inbound_name=selected_inbound.get('remark', f'Inbound {inbound_id}')
    )
    await state.set_state(ExternalKeyStates.waiting_for_period)

    await callback.message.edit_text(
        f"🌍 <b>ВНЕШНИЙ СЕРВЕР</b>\n\n"
        f"📍 Выбран inbound: <b>{selected_inbound.get('remark', f'Inbound {inbound_id}')}</b>\n\n"
        f"Выберите срок подписки:",
        parse_mode="HTML",
        reply_markup=Keyboards.external_subscription_periods()
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ext_period_"), ExternalKeyStates.waiting_for_period)
async def select_external_period(callback: CallbackQuery, state: FSMContext):
    """Выбор периода подписки для внешнего сервера"""
    period_key = callback.data.replace("ext_period_", "")

    # Обработка бесплатного ключа
    if period_key == "free":
        await state.set_state(ExternalKeyStates.waiting_for_custom_days)
        await callback.message.edit_text(
            "🆓 <b>Бесплатный ключ</b>\n\n"
            "Введите количество дней действия ключа:",
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # Обработка своей цены
    if period_key == "custom":
        await state.set_state(ExternalKeyStates.waiting_for_custom_days)
        await state.update_data(custom_price_mode=True)
        await callback.message.edit_text(
            "💵 <b>Своя цена</b>\n\n"
            "Введите количество дней действия ключа:",
            parse_mode="HTML"
        )
        await callback.answer()
        return

    # Обработка ручного ввода даты/дней
    if period_key == "manual":
        await state.set_state(ExternalKeyStates.waiting_for_manual_period)
        await callback.message.edit_text(
            "📅 <b>Ручной ввод срока</b>\n\n"
            "Введите срок одним из способов:\n"
            "• Количество дней (например: <code>90</code>)\n"
            "• Дату окончания (например: <code>31.12.2025</code>)\n\n"
            "Формат даты: ДД.ММ.ГГГГ",
            parse_mode="HTML"
        )
        await callback.answer()
        return

    periods = get_subscription_periods()

    if period_key not in periods:
        await callback.answer("Неверный период")
        return

    period_info = periods[period_key]

    await state.update_data(
        selected_period=period_key,
        selected_period_name=period_info['name'],
        selected_period_days=period_info['days'],
        selected_period_price=period_info['price']
    )
    await state.set_state(ExternalKeyStates.waiting_for_phone)

    data = await state.get_data()
    server_name = data.get('ext_server_name', 'Внешний сервер')

    await callback.message.edit_text(
        f"🌍 <b>{server_name}</b>\n\n"
        f"📅 Период: <b>{period_info['name']}</b> ({period_info['days']} дней)\n"
        f"💰 Цена: <b>{period_info['price']} ₽</b>\n\n"
        f"Теперь введите номер телефона или ID клиента:",
        parse_mode="HTML"
    )
    await callback.answer()


@router.message(ExternalKeyStates.waiting_for_custom_days)
async def process_external_custom_days(message: Message, state: FSMContext):
    """Обработка количества дней для бесплатного/своего ключа"""
    try:
        days = int(message.text.strip())
        if days <= 0:
            await message.answer("❌ Количество дней должно быть больше 0")
            return
    except ValueError:
        await message.answer("❌ Введите число (количество дней)")
        return

    data = await state.get_data()

    await state.update_data(selected_period_days=days)

    # Если это режим своей цены - запрашиваем цену
    if data.get('custom_price_mode'):
        await state.set_state(ExternalKeyStates.waiting_for_custom_price)
        await message.answer(
            f"Количество дней: <b>{days}</b>\n\n"
            f"Теперь введите цену в рублях (или 0 для бесплатно):",
            parse_mode="HTML"
        )
    else:
        # Бесплатный ключ
        await state.update_data(
            selected_period_name=f"Бесплатно ({days} дн.)",
            selected_period_price=0
        )
        await state.set_state(ExternalKeyStates.waiting_for_phone)
        await message.answer(
            f"🆓 <b>Бесплатный ключ</b>\n\n"
            f"📅 Срок: <b>{days} дней</b>\n"
            f"💰 Цена: <b>0 ₽</b>\n\n"
            f"Теперь введите номер телефона или ID клиента:",
            parse_mode="HTML"
        )


@router.message(ExternalKeyStates.waiting_for_custom_price)
async def process_external_custom_price(message: Message, state: FSMContext):
    """Обработка своей цены"""
    try:
        price = int(message.text.strip())
        if price < 0:
            await message.answer("❌ Цена не может быть отрицательной")
            return
    except ValueError:
        await message.answer("❌ Введите число (цена в рублях)")
        return

    data = await state.get_data()
    days = data.get('selected_period_days')

    await state.update_data(
        selected_period_name=f"Своя цена ({days} дн.)",
        selected_period_price=price
    )
    await state.set_state(ExternalKeyStates.waiting_for_phone)

    server_name = data.get('ext_server_name', 'Внешний сервер')

    await message.answer(
        f"💵 <b>{server_name}</b>\n\n"
        f"📅 Срок: <b>{days} дней</b>\n"
        f"💰 Цена: <b>{price} ₽</b>\n\n"
        f"Теперь введите номер телефона или ID клиента:",
        parse_mode="HTML"
    )


@router.message(ExternalKeyStates.waiting_for_manual_period)
async def process_external_manual_period(message: Message, state: FSMContext):
    """Обработка ручного ввода срока (дней или даты)"""
    from datetime import datetime, date

    text = message.text.strip()
    days = None
    period_name = None

    # Пробуем распарсить как число (дни)
    try:
        days = int(text)
        if days <= 0:
            await message.answer("❌ Количество дней должно быть больше 0")
            return
        period_name = f"{days} дн."
    except ValueError:
        # Пробуем распарсить как дату
        try:
            # Формат ДД.ММ.ГГГГ
            end_date = datetime.strptime(text, "%d.%m.%Y").date()
            today = date.today()

            if end_date <= today:
                await message.answer("❌ Дата должна быть в будущем")
                return

            days = (end_date - today).days
            period_name = f"до {text}"
        except ValueError:
            await message.answer(
                "❌ Неверный формат.\n\n"
                "Введите:\n"
                "• Число дней (например: <code>90</code>)\n"
                "• Или дату в формате ДД.ММ.ГГГГ (например: <code>31.12.2025</code>)",
                parse_mode="HTML"
            )
            return

    await state.update_data(
        selected_period_days=days,
        selected_period_name=period_name,
        selected_period_price=0  # По умолчанию бесплатно для ручного ввода
    )
    await state.set_state(ExternalKeyStates.waiting_for_phone)

    data = await state.get_data()
    server_name = data.get('ext_server_name', 'Внешний сервер')

    await message.answer(
        f"📅 <b>{server_name}</b>\n\n"
        f"📅 Срок: <b>{period_name}</b> ({days} дней)\n"
        f"💰 Цена: <b>0 ₽</b> (бесплатно)\n\n"
        f"Теперь введите номер телефона или ID клиента:",
        parse_mode="HTML"
    )


@router.message(ExternalKeyStates.waiting_for_phone)
async def process_external_phone(message: Message, state: FSMContext):
    """Обработка номера телефона для внешнего сервера"""
    phone = message.text.strip()

    if phone == "Отмена":
        await state.clear()
        await message.answer("Создание ключа отменено.", reply_markup=Keyboards.admin_menu())
        return

    # Генерация ID если нужно
    if phone == "Сгенерировать ID":
        phone = generate_user_id()

    # Простая валидация
    if len(phone) < 3:
        await message.answer("❌ Слишком короткий ID. Введите минимум 3 символа.")
        return

    await state.update_data(client_phone=phone)
    await state.set_state(ExternalKeyStates.confirming)

    data = await state.get_data()

    # Генерируем email для клиента (только телефон, без суффиксов)
    import re
    clean_phone = re.sub(r'[^\w\d]', '', phone)
    client_email = clean_phone
    await state.update_data(client_email=client_email)

    text = f"🌍 <b>ПОДТВЕРЖДЕНИЕ СОЗДАНИЯ КЛЮЧА</b>\n\n"
    text += f"📡 Сервер: <b>Внешний</b>\n"
    text += f"📍 Inbound: <b>{data.get('selected_inbound_name')}</b>\n"
    text += f"📅 Период: <b>{data.get('selected_period_name')}</b> ({data.get('selected_period_days')} дней)\n"
    text += f"💰 Цена: <b>{data.get('selected_period_price')} ₽</b>\n"
    text += f"📱 Клиент: <b>{phone}</b>\n"
    text += f"📧 Email: <code>{client_email}</code>\n\n"
    text += "Подтвердите создание ключа:"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Создать", callback_data="ext_confirm_create"),
            InlineKeyboardButton(text="❌ Отмена", callback_data="ext_cancel")
        ]
    ])

    await message.answer(text, parse_mode="HTML", reply_markup=keyboard)


@router.callback_query(F.data == "ext_confirm_create", ExternalKeyStates.confirming)
async def confirm_external_key_creation(callback: CallbackQuery, state: FSMContext, db: DatabaseManager):
    """Подтверждение и создание ключа на внешнем сервере"""
    from bot.api.external_xui import ExternalXUIClient, get_external_xui_client, EXTERNAL_SERVER_CONFIG

    data = await state.get_data()
    period_days = data.get('selected_period_days')
    client_email = data.get('client_email')
    client_phone = data.get('client_phone')
    period_name = data.get('selected_period_name')
    price = data.get('selected_period_price')

    # Проверяем, используется ли внешний сервер из БД или старый конфиг
    ext_server_id = data.get('ext_server_id')
    if ext_server_id:
        # Используем данные из БД
        inbound_id = data.get('ext_inbound_id')
        server_name = data.get('ext_server_name')
        server_address = data.get('ext_domain') or data.get('ext_host')
        server_port = data.get('ext_server_port', 443)
        client = ExternalXUIClient(
            host=f"https://{data.get('ext_host')}:{data.get('ext_port')}",
            username=data.get('ext_username'),
            password=data.get('ext_password'),
            base_path=data.get('ext_base_path')
        )
    else:
        # Старый режим - из EXTERNAL_SERVER_CONFIG
        inbound_id = data.get('selected_inbound_id')
        server_name = f"Внешний ({EXTERNAL_SERVER_CONFIG.get('server_address')})"
        server_address = EXTERNAL_SERVER_CONFIG.get('server_address')
        server_port = EXTERNAL_SERVER_CONFIG.get('server_port', 443)
        client = get_external_xui_client()

    await callback.message.edit_text("⏳ Создание ключа на внешнем сервере...")

    try:
        async with client:
            # Создаём клиента
            result = await client.add_client(
                inbound_id=inbound_id,
                email=client_email,
                phone=client_phone,
                expire_days=period_days,
                ip_limit=2
            )

            if not result:
                await callback.message.delete()
                await callback.message.answer(
                    "❌ Не удалось создать ключ на внешнем сервере.\n"
                    "Проверьте логи для подробностей.",
                    reply_markup=Keyboards.admin_menu()
                )
                await state.clear()
                return

            if result.get('error'):
                error_msg = result.get('message', 'Неизвестная ошибка')
                await callback.message.delete()
                if result.get('is_duplicate'):
                    await callback.message.answer(
                        f"❌ Клиент с таким email уже существует.\n\n"
                        f"Email: <code>{client_email}</code>\n"
                        f"Ошибка: {error_msg}",
                        parse_mode="HTML",
                        reply_markup=Keyboards.admin_menu()
                    )
                else:
                    await callback.message.answer(
                        f"❌ Ошибка создания ключа:\n{error_msg}",
                        reply_markup=Keyboards.admin_menu()
                    )
                await state.clear()
                return

            # Получаем VLESS ссылку
            vless_link = await client.get_client_link(
                inbound_id=inbound_id,
                client_email=client_email,
                server_address=server_address,
                server_port=server_port
            )

            # Сохраняем в базу данных (для статистики)
            await db.add_key_to_history(
                manager_id=callback.from_user.id,
                client_email=client_email,
                phone_number=client_phone,
                period=period_name,
                expire_days=period_days,
                client_id=result.get('client_id', ''),
                price=price
            )

            # Генерируем QR код
            qr_image = None
            if vless_link:
                qr_image = generate_qr_code(vless_link)

            # Формируем сообщение
            text = f"✅ <b>КЛЮЧ УСПЕШНО СОЗДАН</b>\n\n"
            text += f"📡 Сервер: <b>{server_name}</b>\n"
            text += f"📍 Inbound ID: <b>{inbound_id}</b>\n"
            text += f"📱 Клиент: <b>{client_phone}</b>\n"
            text += f"📅 Срок: <b>{period_name}</b>\n"
            text += f"💰 Цена: <b>{price} ₽</b>\n\n"

            if vless_link:
                text += f"🔗 <b>VLESS ссылка:</b>\n<code>{vless_link}</code>\n\n"
            else:
                text += "⚠️ Не удалось получить VLESS ссылку\n\n"

            text += "━━━━━━━━━━━━━━━━"

            # Отправляем сообщение с QR кодом если есть
            if qr_image:
                await callback.message.delete()
                await callback.message.answer_photo(
                    photo=BufferedInputFile(qr_image.read(), filename="qr.png"),
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=Keyboards.admin_menu()
                )
            else:
                await callback.message.delete()
                await callback.message.answer(
                    text,
                    parse_mode="HTML",
                    reply_markup=Keyboards.admin_menu()
                )

            logger.info(f"Ключ создан на внешнем сервере {server_name}: {client_email} для {client_phone}")

    except Exception as e:
        logger.error(f"Ошибка при создании ключа на внешнем сервере: {e}")
        import traceback
        traceback.print_exc()
        await callback.message.delete()
        await callback.message.answer(
            f"❌ Произошла ошибка:\n{str(e)}",
            reply_markup=Keyboards.admin_menu()
        )

    await state.clear()
    await callback.answer()


# ========== Управление внешними серверами ==========

@router.callback_query(F.data == "ext_servers")
async def show_external_servers(callback: CallbackQuery, db: DatabaseManager, state: FSMContext):
    """Показать список внешних серверов"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return

    await state.clear()
    servers = await db.get_external_servers(active_only=False)

    text = "🌐 <b>Внешние серверы</b>\n\n"
    if servers:
        text += f"Найдено серверов: {len(servers)}\n"
        text += "Выберите сервер для управления:"
    else:
        text += "Серверов пока нет.\nНажмите «➕ Добавить сервер» для добавления."

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.external_servers_list(servers)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ext_srv_") & ~F.data.startswith("ext_srv_add") & ~F.data.startswith("ext_srv_key_") & ~F.data.startswith("ext_srv_test_") & ~F.data.startswith("ext_srv_toggle_") & ~F.data.startswith("ext_srv_edit_") & ~F.data.startswith("ext_srv_del_"))
async def show_external_server_details(callback: CallbackQuery, db: DatabaseManager):
    """Показать детали внешнего сервера"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return

    server_id = int(callback.data.split("_")[-1])
    server = await db.get_external_server(server_id)

    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return

    status = "✅ Активен" if server['is_active'] else "❌ Отключен"
    domain_info = server['domain'] if server['domain'] else "Не указан (используется IP)"

    text = f"🌐 <b>Сервер: {server['name']}</b>\n\n"
    text += f"📍 Статус: {status}\n"
    text += f"🖥 Хост: <code>{server['host']}:{server['port']}</code>\n"
    text += f"📁 Путь: <code>{server['base_path']}</code>\n"
    text += f"👤 Логин: <code>{server['username']}</code>\n"
    text += f"🔗 Inbound ID: <code>{server['inbound_id']}</code>\n"
    text += f"🌍 Домен: <code>{domain_info}</code>\n"
    text += f"🔌 Порт клиента: <code>{server['server_port']}</code>\n"

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=Keyboards.external_server_actions(server_id, server['is_active'])
    )
    await callback.answer()


@router.callback_query(F.data == "ext_srv_add")
async def start_add_external_server(callback: CallbackQuery, state: FSMContext):
    """Начать добавление внешнего сервера"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return

    await state.set_state(AddExternalServerStates.waiting_for_name)

    text = "➕ <b>Добавление внешнего сервера</b>\n\n"
    text += "Шаг 1/5: Введите название сервера\n"
    text += "Например: <code>EU Server</code> или <code>LTE Germany</code>"

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ext_servers")]
        ])
    )
    await callback.answer()


@router.message(AddExternalServerStates.waiting_for_name)
async def process_server_name(message: Message, state: FSMContext):
    """Обработка имени сервера"""
    if message.from_user.id != ADMIN_ID:
        return

    server_name = message.text.strip()
    if not server_name or len(server_name) < 2:
        await message.answer("❌ Имя слишком короткое. Введите минимум 2 символа.")
        return

    await state.update_data(server_name=server_name)
    await state.set_state(AddExternalServerStates.waiting_for_url)

    text = "➕ <b>Добавление внешнего сервера</b>\n\n"
    text += f"Название: <b>{server_name}</b>\n\n"
    text += "Шаг 2/5: Введите URL панели в формате:\n"
    text += "<code>IP:PORT/BASE_PATH</code>\n\n"
    text += "Пример: <code>38.180.205.196:27450/J6CkyRIalbUZdPd</code>\n"
    text += "(без https:// и /panel/inbounds)"

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ext_servers")]
        ])
    )


@router.message(AddExternalServerStates.waiting_for_url)
async def process_server_url(message: Message, state: FSMContext):
    """Обработка URL сервера"""
    if message.from_user.id != ADMIN_ID:
        return

    import re
    url_text = message.text.strip()

    # Парсим URL вида IP:PORT/PATH
    match = re.match(r'^([^:/]+):(\d+)(/.*)?$', url_text)
    if not match:
        await message.answer(
            "❌ Неверный формат URL\n\n"
            "Введите в формате: <code>IP:PORT/BASE_PATH</code>\n"
            "Пример: <code>38.180.205.196:27450/J6CkyRIalbUZdPd</code>",
            parse_mode="HTML"
        )
        return

    host = match.group(1)
    port = int(match.group(2))
    base_path = match.group(3) or ""

    data = await state.get_data()
    await state.update_data(host=host, port=port, base_path=base_path)
    await state.set_state(AddExternalServerStates.waiting_for_credentials)

    text = "➕ <b>Добавление внешнего сервера</b>\n\n"
    text += f"Название: <b>{data['server_name']}</b>\n"
    text += f"Хост: <code>{host}:{port}{base_path}</code>\n\n"
    text += "Шаг 3/5: Введите логин и пароль через пробел:\n"
    text += "<code>username password</code>"

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ext_servers")]
        ])
    )


@router.message(AddExternalServerStates.waiting_for_credentials)
async def process_server_credentials(message: Message, state: FSMContext):
    """Обработка логина и пароля"""
    if message.from_user.id != ADMIN_ID:
        return

    parts = message.text.strip().split(maxsplit=1)
    if len(parts) != 2:
        await message.answer(
            "❌ Введите логин и пароль через пробел:\n"
            "<code>username password</code>",
            parse_mode="HTML"
        )
        return

    username, password = parts

    data = await state.get_data()
    await state.update_data(username=username, password=password)
    await state.set_state(AddExternalServerStates.waiting_for_inbound_id)

    text = "➕ <b>Добавление внешнего сервера</b>\n\n"
    text += f"Название: <b>{data['server_name']}</b>\n"
    text += f"Хост: <code>{data['host']}:{data['port']}{data['base_path']}</code>\n"
    text += f"Логин: <code>{username}</code>\n\n"
    text += "Шаг 4/5: Введите ID inbound для создания ключей:\n"
    text += "Например: <code>1</code>"

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ext_servers")]
        ])
    )


@router.message(AddExternalServerStates.waiting_for_inbound_id)
async def process_server_inbound_id(message: Message, state: FSMContext):
    """Обработка ID inbound"""
    if message.from_user.id != ADMIN_ID:
        return

    try:
        inbound_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ Введите число (ID inbound)")
        return

    data = await state.get_data()
    await state.update_data(inbound_id=inbound_id)
    await state.set_state(AddExternalServerStates.waiting_for_domain)

    text = "➕ <b>Добавление внешнего сервера</b>\n\n"
    text += f"Название: <b>{data['server_name']}</b>\n"
    text += f"Хост: <code>{data['host']}:{data['port']}{data['base_path']}</code>\n"
    text += f"Логин: <code>{data['username']}</code>\n"
    text += f"Inbound ID: <code>{inbound_id}</code>\n\n"
    text += "Шаг 5/5: Введите домен для ключей и порт через пробел\n"
    text += "или <code>-</code> для использования IP:\n\n"
    text += "Пример: <code>vpn.example.com 443</code>\n"
    text += "или просто: <code>-</code>"

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ext_servers")]
        ])
    )


@router.message(AddExternalServerStates.waiting_for_domain)
async def process_server_domain(message: Message, state: FSMContext, db: DatabaseManager):
    """Обработка домена и завершение добавления"""
    if message.from_user.id != ADMIN_ID:
        return

    text = message.text.strip()

    domain = None
    server_port = 443

    if text != "-":
        parts = text.split()
        domain = parts[0]
        if len(parts) > 1:
            try:
                server_port = int(parts[1])
            except ValueError:
                await message.answer("❌ Неверный формат порта. Введите число.")
                return

    data = await state.get_data()

    # Сохраняем сервер в БД
    server_id = await db.add_external_server(
        name=data['server_name'],
        host=data['host'],
        port=data['port'],
        base_path=data['base_path'],
        username=data['username'],
        password=data['password'],
        inbound_id=data['inbound_id'],
        domain=domain,
        server_port=server_port
    )

    if server_id:
        await message.answer(
            f"✅ Сервер <b>{data['server_name']}</b> успешно добавлен!\n\n"
            f"ID: {server_id}",
            parse_mode="HTML",
            reply_markup=Keyboards.external_servers_list(await db.get_external_servers(active_only=False))
        )
    else:
        await message.answer(
            "❌ Ошибка при добавлении сервера",
            reply_markup=Keyboards.admin_menu()
        )

    await state.clear()


@router.callback_query(F.data.startswith("ext_srv_test_"))
async def test_external_server(callback: CallbackQuery, db: DatabaseManager):
    """Тест подключения к внешнему серверу"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return

    server_id = int(callback.data.split("_")[-1])
    server = await db.get_external_server(server_id)

    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return

    await callback.answer("🔄 Тестирую подключение...", show_alert=False)

    from bot.api.external_xui import ExternalXUIClient

    try:
        client = ExternalXUIClient(
            host=f"https://{server['host']}:{server['port']}",
            username=server['username'],
            password=server['password'],
            base_path=server['base_path']
        )

        async with client:
            inbounds = await client.list_inbounds()

            if inbounds is not None:
                # Ищем нужный inbound
                target_inbound = None
                for inb in inbounds:
                    if inb.get('id') == server['inbound_id']:
                        target_inbound = inb
                        break

                text = f"✅ <b>Подключение успешно!</b>\n\n"
                text += f"Сервер: {server['name']}\n"
                text += f"Найдено inbounds: {len(inbounds)}\n\n"

                if target_inbound:
                    text += f"📌 Целевой inbound (ID {server['inbound_id']}):\n"
                    text += f"  Название: {target_inbound.get('remark', 'N/A')}\n"
                    text += f"  Порт: {target_inbound.get('port', 'N/A')}\n"
                    text += f"  Протокол: {target_inbound.get('protocol', 'N/A')}\n"
                else:
                    text += f"⚠️ Inbound с ID {server['inbound_id']} не найден!\n"
                    text += "Доступные inbounds:\n"
                    for inb in inbounds[:5]:
                        text += f"  • ID {inb.get('id')}: {inb.get('remark')} (порт {inb.get('port')})\n"

                await callback.message.edit_text(
                    text,
                    parse_mode="HTML",
                    reply_markup=Keyboards.external_server_actions(server_id, server['is_active'])
                )
            else:
                await callback.message.edit_text(
                    f"❌ Не удалось получить список inbounds\n"
                    f"Проверьте учетные данные.",
                    reply_markup=Keyboards.external_server_actions(server_id, server['is_active'])
                )

    except Exception as e:
        logger.error(f"Ошибка тестирования сервера {server_id}: {e}")
        await callback.message.edit_text(
            f"❌ <b>Ошибка подключения</b>\n\n"
            f"Сервер: {server['name']}\n"
            f"Ошибка: {str(e)}",
            parse_mode="HTML",
            reply_markup=Keyboards.external_server_actions(server_id, server['is_active'])
        )


@router.callback_query(F.data.startswith("ext_srv_toggle_"))
async def toggle_external_server(callback: CallbackQuery, db: DatabaseManager):
    """Переключить активность сервера"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return

    server_id = int(callback.data.split("_")[-1])

    if await db.toggle_external_server(server_id):
        server = await db.get_external_server(server_id)
        status = "включен" if server['is_active'] else "отключен"
        await callback.answer(f"Сервер {status}", show_alert=True)

        # Обновляем меню
        await show_external_server_details.__wrapped__(callback, db)
    else:
        await callback.answer("Ошибка переключения", show_alert=True)


@router.callback_query(F.data.startswith("ext_srv_del_"))
async def confirm_delete_external_server(callback: CallbackQuery, db: DatabaseManager):
    """Подтверждение удаления сервера"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return

    server_id = int(callback.data.split("_")[-1])
    server = await db.get_external_server(server_id)

    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return

    await callback.message.edit_text(
        f"⚠️ <b>Удалить сервер?</b>\n\n"
        f"Название: {server['name']}\n"
        f"Хост: {server['host']}:{server['port']}\n\n"
        f"Это действие нельзя отменить!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"ext_srv_del_confirm_{server_id}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data=f"ext_srv_{server_id}")
            ]
        ])
    )
    await callback.answer()


@router.callback_query(F.data.startswith("ext_srv_del_confirm_"))
async def delete_external_server(callback: CallbackQuery, db: DatabaseManager):
    """Удаление сервера"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return

    server_id = int(callback.data.split("_")[-1])

    if await db.delete_external_server(server_id):
        await callback.answer("Сервер удален", show_alert=True)
        servers = await db.get_external_servers(active_only=False)
        await callback.message.edit_text(
            "🌐 <b>Внешние серверы</b>\n\n"
            f"Найдено серверов: {len(servers)}\n"
            "Выберите сервер для управления:",
            parse_mode="HTML",
            reply_markup=Keyboards.external_servers_list(servers)
        )
    else:
        await callback.answer("Ошибка удаления", show_alert=True)


@router.callback_query(F.data.startswith("ext_srv_key_"))
async def start_create_key_on_external_server(callback: CallbackQuery, state: FSMContext, db: DatabaseManager):
    """Начать создание ключа на выбранном внешнем сервере"""
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return

    server_id = int(callback.data.split("_")[-1])
    server = await db.get_external_server(server_id)

    if not server:
        await callback.answer("Сервер не найден", show_alert=True)
        return

    if not server['is_active']:
        await callback.answer("Сервер отключен", show_alert=True)
        return

    # Сохраняем данные сервера в состоянии
    await state.update_data(
        ext_server_id=server_id,
        ext_server_name=server['name'],
        ext_host=server['host'],
        ext_port=server['port'],
        ext_base_path=server['base_path'],
        ext_username=server['username'],
        ext_password=server['password'],
        ext_inbound_id=server['inbound_id'],
        ext_domain=server['domain'],
        ext_server_port=server['server_port']
    )
    await state.set_state(ExternalKeyStates.waiting_for_period)

    await callback.message.edit_text(
        f"🔑 <b>Создание ключа на сервере: {server['name']}</b>\n\n"
        f"Inbound ID: {server['inbound_id']}\n\n"
        f"Выберите период подписки:",
        parse_mode="HTML",
        reply_markup=Keyboards.external_subscription_periods()
    )
    await callback.answer()
