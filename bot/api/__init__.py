"""
API клиенты для работы с X-UI панелями
"""
from .xui_client import XUIClient
from .base_client import (
    ServerConfig,
    ClientSettings,
    SessionManager,
    XUIClient as BaseXUIClient,
    XUIClientFactory,
    get_client_factory,
)

__all__ = [
    # Legacy client
    'XUIClient',

    # New base client
    'ServerConfig',
    'ClientSettings',
    'SessionManager',
    'BaseXUIClient',
    'XUIClientFactory',
    'get_client_factory',
]
