"""
Middleware для инъекции контекста бренда в каждый хендлер
"""
import logging
from dataclasses import dataclass, field
from typing import Optional, Any, Callable, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

logger = logging.getLogger(__name__)


@dataclass
class BrandContext:
    """Контекст бренда — передаётся в каждый хендлер"""
    brand_id: int
    name: str              # "KOBRA"
    domain: str            # "kobra.peakvip.ru"
    bot_token: str
    theme_color: str = '#007bff'
    logo_url: Optional[str] = None
    admin_id: Optional[int] = None
    allowed_servers: Optional[str] = None  # JSON list or None

    def get_allowed_servers(self) -> Optional[list]:
        """Список разрешённых серверов (None = все)"""
        if self.allowed_servers:
            import json
            return json.loads(self.allowed_servers) if isinstance(self.allowed_servers, str) else self.allowed_servers
        return None


class BrandMiddleware(BaseMiddleware):
    """
    Middleware, который инъектирует BrandContext в data хендлера.
    BrandContext берётся из dispatcher storage, куда он помещается при старте бота.
    """

    def __init__(self, brand_context: BrandContext):
        self.brand_context = brand_context

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any]
    ) -> Any:
        data['brand'] = self.brand_context
        return await handler(event, data)
