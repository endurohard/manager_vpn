"""
Обработчики для менеджеров (создание ключей, статистика)
"""
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.config import ADMIN_ID, INBOUND_ID, DOMAIN
from bot.database import DatabaseManager
from bot.api.xui_client import XUIClient
from bot.utils import Keyboards, validate_phone, format_phone, generate_user_id, generate_qr_code, notify_admin_xui_error
from bot.handlers.common import is_authorized
from bot.price_config import get_subscription_periods

router = Router()


class CreateKeyStates(StatesGroup):
    """Состояния для создания ключа"""
    waiting_for_phone = State()
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
    user_id_value = generate_user_id()
    await state.update_data(phone=user_id_value)

    # Используем дефолтный inbound для всех
    await state.update_data(inbound_id=INBOUND_ID)
    await state.set_state(CreateKeyStates.waiting_for_period)

    await message.answer(
        f"Сгенерирован ID: {user_id_value}\n\n"
        "Выберите срок действия ключа:",
        reply_markup=Keyboards.subscription_periods()
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
        # Автоматически генерируем ID
        generated_id = generate_user_id()
        await state.update_data(phone=generated_id, inbound_id=INBOUND_ID)
        await state.set_state(CreateKeyStates.waiting_for_period)

        await message.answer(
            f"⚠️ Обнаружен текст кнопки. Автоматически сгенерирован новый ID:\n"
            f"🆔 <code>{generated_id}</code>\n\n"
            "Выберите срок действия ключа:",
            reply_markup=Keyboards.subscription_periods(),
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
    await state.set_state(CreateKeyStates.waiting_for_period)

    await message.answer(
        format_message + "Выберите срок действия ключа:",
        reply_markup=Keyboards.subscription_periods(),
        parse_mode="HTML"
    )


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

    await callback.message.edit_text("Создание ключа...")

    try:
        # Создаем клиента в X-UI
        client_data = await xui_client.add_client(
            inbound_id=inbound_id,
            email=phone,
            phone=phone,
            expire_days=period_days,
            ip_limit=2
        )

        if not client_data:
            await callback.message.edit_text(
                "❌ Ошибка при создании ключа в X-UI панели.\n"
                "Проверьте подключение к панели."
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
                error_details=f"Не удалось создать клиента для ID: {phone}, период: {period_name} ({period_days} дней)"
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

        # Получаем VLESS ссылку с реальным IP сервера
        vless_link_original = await xui_client.get_client_link(
            inbound_id=inbound_id,  # Используем выбранный inbound
            client_email=phone,
            use_domain=None  # Получаем с IP сервера
        )

        if not vless_link_original:
            await callback.message.edit_text(
                "Ключ создан, но не удалось сформировать VLESS ссылку."
            )
            return

        # Создаем версию с доменом для выдачи пользователю
        # Заменяем IP на домен и порт на 443 (так как используется парсинг портов)
        vless_link_for_user = XUIClient.replace_ip_with_domain(vless_link_original, DOMAIN)

        # Получаем цену из данных
        period_price = data.get("period_price", 0)

        # Сохраняем в базу данных (сохраняем оригинальную ссылку с IP для внутренних нужд)
        await db.add_key_to_history(
            manager_id=user_id,
            client_email=phone,
            phone_number=phone,
            period=period_name,
            expire_days=period_days,
            client_id=client_data['client_id'],
            price=period_price
        )

        # Формируем ссылку подписки
        client_uuid = client_data['client_id']
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

    stats_text = (
        f"📊 <b>ВАША СТАТИСТИКА</b>\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"💰 <b>ДОХОДЫ:</b>\n"
        f"💵 Всего заработано: <b>{revenue_stats['total']:,} ₽</b>\n"
        f"📅 За сегодня: <b>{revenue_stats['today']:,} ₽</b>\n"
        f"📆 За месяц: <b>{revenue_stats['month']:,} ₽</b>\n\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"🔑 <b>КЛЮЧИ:</b>\n"
        f"Всего создано: <b>{stats['total']}</b>\n"
        f"Создано сегодня: <b>{stats['today']}</b>\n"
        f"Создано за месяц: <b>{stats['month']}</b>\n"
    )

    # Получаем последние 5 ключей
    history = await db.get_manager_history(user_id, limit=5)

    if history:
        stats_text += "\n━━━━━━━━━━━━━━━━\n"
        stats_text += "📋 <b>Последние 5 ключей:</b>\n\n"
        for item in history:
            stats_text += f"• {item['phone_number']} - {item['period']} ({item['created_at'][:10]})\n"

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
