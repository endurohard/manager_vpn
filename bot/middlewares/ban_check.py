"""
Middleware для проверки бана пользователей
"""
import logging
import aiosqlite
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

logger = logging.getLogger(__name__)


class BanCheckMiddleware(BaseMiddleware):
    """
    Middleware для проверки бана пользователей.
    Блокирует доступ заблокированным пользователям ко всем функциям бота.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        super().__init__()

    async def _check_user_banned(self, telegram_id: int) -> bool:
        """
        Проверяет, заблокирован ли пользователь.
        Возвращает True если заблокирован, False если нет.
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Проверяем наличие колонки is_banned
                cursor = await db.execute("PRAGMA table_info(managers)")
                columns = [row[1] for row in await cursor.fetchall()]

                if 'is_banned' not in columns:
                    return False

                cursor = await db.execute(
                    "SELECT is_banned FROM managers WHERE user_id = ?",
                    (telegram_id,)
                )
                row = await cursor.fetchone()

                if row:
                    return bool(row[0])
                return False
        except Exception as e:
            logger.error(f"Ошибка при проверке бана пользователя {telegram_id}: {e}")
            return False

    async def __call__(
        self,
        handler: Callable[[Message, dict[str, Any]], Awaitable[Any]],
        event: Message | CallbackQuery,
        data: dict[str, Any]
    ) -> Any:
        if isinstance(event, (Message, CallbackQuery)):
            telegram_id = event.from_user.id

            if await self._check_user_banned(telegram_id):
                logger.warning(f"Заблокированный пользователь {telegram_id} попытался использовать бота")

                ban_message = "Ваш доступ к боту заблокирован. Обратитесь к администратору."

                if isinstance(event, Message):
                    await event.answer(ban_message)
                elif isinstance(event, CallbackQuery):
                    await event.answer(ban_message, show_alert=True)

                return None

        return await handler(event, data)
