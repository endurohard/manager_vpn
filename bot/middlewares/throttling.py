"""
Middleware для ограничения частоты запросов (троттлинг)
"""
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from typing import Any

from aiogram import BaseMiddleware
from aiogram.dispatcher.flags import get_flag
from aiogram.types import CallbackQuery, Message, TelegramObject, User as TelegramUser
from cachetools import TTLCache

logger = logging.getLogger(__name__)


class ThrottlingMiddleware(BaseMiddleware):
    """
    Middleware для ограничения частоты запросов от пользователей.

    Использование:
        dp.message.middleware(ThrottlingMiddleware(default_ttl=0.5))

    В хендлерах можно указать флаг:
        @router.message(flags={"throttling_key": "heavy_command"})
    """

    def __init__(
        self,
        *,
        default_key: str | None = "default",
        default_ttl: float = 0.3,
        **ttl_map: float,
    ) -> None:
        """
        Инициализация middleware.

        Args:
            default_key: Ключ по умолчанию для троттлинга
            default_ttl: Время в секундах между запросами по умолчанию
            **ttl_map: Дополнительные ключи с их TTL значениями
        """
        if default_key:
            ttl_map[default_key] = default_ttl

        self.default_key = default_key
        self.caches: dict[str, MutableMapping[int, None]] = {}

        for name, ttl in ttl_map.items():
            self.caches[name] = TTLCache(maxsize=10_000, ttl=ttl)

        logger.debug("ThrottlingMiddleware инициализирован")
        super().__init__()

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: TelegramUser | None = getattr(event, "from_user", None)

        if user:
            key = get_flag(handler=data, name="throttling_key", default=self.default_key)

            if key and key in self.caches:
                if user.id in self.caches[key]:
                    logger.debug(f"Пользователь {user.id} троттлен (ключ: {key})")

                    if isinstance(event, CallbackQuery):
                        await event.answer(
                            "Слишком много запросов! Подождите...",
                            show_alert=False,
                        )
                    elif isinstance(event, Message):
                        pass  # Не отвечаем на сообщения при троттлинге

                    return None

                self.caches[key][user.id] = None

        return await handler(event, data)
