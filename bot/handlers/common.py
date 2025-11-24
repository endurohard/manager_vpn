"""
Общие обработчики команд
"""
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext

from bot.config import ADMIN_ID
from bot.database import DatabaseManager
from bot.utils import Keyboards
from bot.price_config import get_subscription_periods

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
