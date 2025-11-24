"""
Утилиты для отправки уведомлений администратору
"""
import logging
from datetime import datetime
from aiogram import Bot
from bot.config import ADMIN_ID

logger = logging.getLogger(__name__)


async def notify_admin_error(bot: Bot, error_type: str, details: str = "", user_info: dict = None):
    """
    Отправить уведомление админу об ошибке

    :param bot: Экземпляр бота
    :param error_type: Тип ошибки
    :param details: Дополнительные детали
    :param user_info: Информация о пользователе (dict с ключами: user_id, username, phone)
    """
    if not ADMIN_ID or ADMIN_ID == 0:
        logger.warning("ADMIN_ID не настроен, уведомление не отправлено")
        return

    try:
        # Формируем сообщение для админа
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        message = f"🚨 <b>ОШИБКА В БОТЕ</b>\n\n"
        message += f"⏰ <b>Время:</b> {timestamp}\n"
        message += f"❌ <b>Тип:</b> {error_type}\n"

        if user_info:
            message += f"\n👤 <b>Пользователь:</b>\n"
            if user_info.get('user_id'):
                message += f"• ID: <code>{user_info['user_id']}</code>\n"
            if user_info.get('username'):
                message += f"• Username: @{user_info['username']}\n"
            if user_info.get('phone'):
                message += f"• Телефон: <code>{user_info['phone']}</code>\n"

        if details:
            message += f"\n📝 <b>Детали:</b>\n{details}\n"

        message += f"\n💡 <b>Рекомендация:</b> Проверьте подключение к X-UI панели и логи бота."

        await bot.send_message(
            chat_id=ADMIN_ID,
            text=message,
            parse_mode="HTML"
        )

        logger.info(f"Уведомление админу об ошибке '{error_type}' отправлено")

    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления админу: {e}")


async def notify_admin_xui_error(bot: Bot, operation: str, user_info: dict = None, error_details: str = ""):
    """
    Отправить уведомление админу об ошибке X-UI

    :param bot: Экземпляр бота
    :param operation: Операция (например, "Создание ключа", "Получение inbound")
    :param user_info: Информация о пользователе
    :param error_details: Детали ошибки
    """
    error_type = f"Ошибка X-UI: {operation}"
    details = f"Операция: {operation}\n"

    if error_details:
        details += f"Ошибка: {error_details}\n"

    details += "\nВозможные причины:\n"
    details += "• Панель X-UI недоступна\n"
    details += "• Сессия истекла\n"
    details += "• Неверные учетные данные\n"
    details += "• Проблемы с сетью\n"

    await notify_admin_error(
        bot=bot,
        error_type=error_type,
        details=details,
        user_info=user_info
    )
