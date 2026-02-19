"""
VPN Manager Bot - бот для управления X-UI VPN панелью
"""

__version__ = "1.0.0"

# Errors
from .errors import (
    VPNManagerError,
    APIError,
    AuthenticationError,
    ConnectionError,
    TimeoutError,
    PanelAPIError,
    DatabaseError,
    RecordNotFoundError,
    ClientError,
    ClientNotFoundError,
    KeyCreationError,
    ServerError,
    ServerUnavailableError,
    NoAvailableServersError,
    ValidationError,
    ErrorTracker,
    track_error,
    get_error_stats,
)

__all__ = [
    '__version__',
    # Errors
    'VPNManagerError',
    'APIError',
    'AuthenticationError',
    'ConnectionError',
    'TimeoutError',
    'PanelAPIError',
    'DatabaseError',
    'RecordNotFoundError',
    'ClientError',
    'ClientNotFoundError',
    'KeyCreationError',
    'ServerError',
    'ServerUnavailableError',
    'NoAvailableServersError',
    'ValidationError',
    'ErrorTracker',
    'track_error',
    'get_error_stats',
]
