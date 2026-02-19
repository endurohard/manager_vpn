"""
Модуль базы данных
"""
from .db_manager import DatabaseManager
from .models import (
    ClientStatus,
    SubscriptionAction,
    NotificationType,
    PromoDiscountType,
    init_extended_tables,
    migrate_existing_data
)
from .client_manager import ClientManager
from .promo_manager import PromoManager, ReferralManager
from .audit_manager import AuditManager, AuditAction, EntityType, AuditContext
from .analytics_manager import AnalyticsManager
from .pool import (
    PoolConfig,
    DatabasePool,
    get_pool,
    close_pool,
    Repository,
)

__all__ = [
    # Основной менеджер
    'DatabaseManager',

    # Модели и enum
    'ClientStatus',
    'SubscriptionAction',
    'NotificationType',
    'PromoDiscountType',

    # Функции инициализации
    'init_extended_tables',
    'migrate_existing_data',

    # Менеджеры
    'ClientManager',
    'PromoManager',
    'ReferralManager',
    'AuditManager',
    'AnalyticsManager',

    # Аудит
    'AuditAction',
    'EntityType',
    'AuditContext',

    # Connection pool
    'PoolConfig',
    'DatabasePool',
    'get_pool',
    'close_pool',
    'Repository',
]
