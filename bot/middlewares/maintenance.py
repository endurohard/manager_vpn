"""
Middleware для режима технического обслуживания
"""
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, User as TelegramUser

logger = logging.getLogger(__name__)


class MaintenanceMiddleware(BaseMiddleware):
    """
    Middleware для режима технического обслуживания.

    Когда режим активен, только администраторы могут использовать бота.

    Использование:
        MaintenanceMiddleware.set_mode(True)   # Включить
        MaintenanceMiddleware.set_mode(False)  # Выключить
    """

    active: bool = False

    def __init__(self, admin_ids: list[int] = None) -> None:
        """
        Args:
            admin_ids: Список ID администраторов, которые могут
                      использовать бота во время обслуживания
        """
        self.admin_ids = admin_ids or []
        logger.debug("MaintenanceMiddleware инициализирован")
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Update):
            user: TelegramUser | None = None

            # Получаем пользователя из разных типов событий
            if event.message:
                user = event.message.from_user
            elif event.callback_query:
                user = event.callback_query.from_user
            elif event.inline_query:
                user = event.inline_query.from_user

            if user is not None:
                is_admin = user.id in self.admin_ids

                if MaintenanceMiddleware.active and not is_admin:
                    logger.info(
                        f"Пользователь {user.id} попытался использовать бота "
                        "во время обслуживания"
                    )

                    message = None
                    if event.message:
                        message = event.message
                    elif event.callback_query and event.callback_query.message:
                        message = event.callback_query.message

                    if message:
                        await message.answer(
                            "Бот находится на техническом обслуживании. "
                            "Попробуйте позже."
                        )

                    return None

        return await handler(event, data)

    @classmethod
    def set_mode(cls, active: bool) -> None:
        """Включить/выключить режим обслуживания"""
        cls.active = active
        status = "включён" if active else "выключен"
        logger.info(f"Режим обслуживания: {status}")

    @classmethod
    def is_active(cls) -> bool:
        """Проверить, активен ли режим обслуживания"""
        return cls.active
