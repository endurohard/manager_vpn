"""
Модели данных и расширенные таблицы БД
"""
import aiosqlite
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Any
from enum import Enum

logger = logging.getLogger(__name__)


class ClientStatus(Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class SubscriptionAction(Enum):
    CREATED = "created"
    EXTENDED = "extended"
    SUSPENDED = "suspended"
    REACTIVATED = "reactivated"
    DELETED = "deleted"
    REPLACED = "replaced"


class NotificationType(Enum):
    EXPIRY_WARNING = "expiry_warning"
    EXPIRED = "expired"
    TRAFFIC_LIMIT = "traffic_limit"
    WELCOME = "welcome"
    PROMO = "promo"


class PromoDiscountType(Enum):
    PERCENT = "percent"
    FIXED = "fixed"
    DAYS = "days"


# SQL для создания новых таблиц
EXTENDED_TABLES_SQL = """
-- ==================== КЛИЕНТЫ ====================
CREATE TABLE IF NOT EXISTS clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    phone TEXT,
    name TEXT,
    telegram_id INTEGER,

    status TEXT DEFAULT 'active',
    expire_time INTEGER,

    created_by INTEGER,
    current_server TEXT,

    total_traffic INTEGER DEFAULT 0,
    last_connect_at TIMESTAMP,
    ip_limit INTEGER DEFAULT 2,

    group_id INTEGER,
    referrer_id INTEGER,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (created_by) REFERENCES managers(user_id),
    FOREIGN KEY (group_id) REFERENCES client_groups(id)
);

CREATE INDEX IF NOT EXISTS idx_clients_uuid ON clients(uuid);
CREATE INDEX IF NOT EXISTS idx_clients_email ON clients(email);
CREATE INDEX IF NOT EXISTS idx_clients_status ON clients(status);
CREATE INDEX IF NOT EXISTS idx_clients_expire ON clients(expire_time);
CREATE INDEX IF NOT EXISTS idx_clients_telegram ON clients(telegram_id);
CREATE INDEX IF NOT EXISTS idx_clients_created_by ON clients(created_by);

-- ==================== СЕРВЕРЫ КЛИЕНТА ====================
CREATE TABLE IF NOT EXISTS client_servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    server_name TEXT NOT NULL,
    inbound_id INTEGER,
    status TEXT DEFAULT 'active',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    UNIQUE(client_id, server_name)
);

CREATE INDEX IF NOT EXISTS idx_client_servers_client ON client_servers(client_id);
CREATE INDEX IF NOT EXISTS idx_client_servers_server ON client_servers(server_name);

-- ==================== ИСТОРИЯ ПОДПИСОК ====================
CREATE TABLE IF NOT EXISTS subscription_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    period TEXT,
    days INTEGER,
    price INTEGER DEFAULT 0,
    old_expire INTEGER,
    new_expire INTEGER,
    manager_id INTEGER,
    promo_code TEXT,
    discount_amount INTEGER DEFAULT 0,
    note TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
    FOREIGN KEY (manager_id) REFERENCES managers(user_id)
);

CREATE INDEX IF NOT EXISTS idx_sub_history_client ON subscription_history(client_id);
CREATE INDEX IF NOT EXISTS idx_sub_history_action ON subscription_history(action);
CREATE INDEX IF NOT EXISTS idx_sub_history_date ON subscription_history(created_at);
CREATE INDEX IF NOT EXISTS idx_sub_history_manager ON subscription_history(manager_id);

-- ==================== УВЕДОМЛЕНИЯ ====================
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER,
    type TEXT NOT NULL,
    title TEXT,
    message TEXT,
    days_before INTEGER,
    scheduled_at TIMESTAMP,
    sent_at TIMESTAMP,
    status TEXT DEFAULT 'pending',
    error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_notifications_client ON notifications(client_id);
CREATE INDEX IF NOT EXISTS idx_notifications_status ON notifications(status);
CREATE INDEX IF NOT EXISTS idx_notifications_scheduled ON notifications(scheduled_at);

-- ==================== НАСТРОЙКИ УВЕДОМЛЕНИЙ ====================
CREATE TABLE IF NOT EXISTS notification_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    setting_key TEXT UNIQUE NOT NULL,
    setting_value TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==================== ПРОМОКОДЫ ====================
CREATE TABLE IF NOT EXISTS promo_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    description TEXT,
    discount_type TEXT NOT NULL,
    discount_value INTEGER NOT NULL,
    max_uses INTEGER DEFAULT 0,
    current_uses INTEGER DEFAULT 0,
    valid_from TIMESTAMP,
    valid_until TIMESTAMP,
    min_period TEXT,
    min_amount INTEGER DEFAULT 0,
    applicable_periods TEXT,
    created_by INTEGER,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (created_by) REFERENCES managers(user_id)
);

CREATE INDEX IF NOT EXISTS idx_promo_code ON promo_codes(code);
CREATE INDEX IF NOT EXISTS idx_promo_active ON promo_codes(is_active);

-- ==================== ИСПОЛЬЗОВАНИЕ ПРОМОКОДОВ ====================
CREATE TABLE IF NOT EXISTS promo_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    promo_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    subscription_id INTEGER,
    original_price INTEGER,
    discount_amount INTEGER,
    final_price INTEGER,
    used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (promo_id) REFERENCES promo_codes(id),
    FOREIGN KEY (client_id) REFERENCES clients(id)
);

CREATE INDEX IF NOT EXISTS idx_promo_uses_promo ON promo_uses(promo_id);
CREATE INDEX IF NOT EXISTS idx_promo_uses_client ON promo_uses(client_id);

-- ==================== РЕФЕРАЛЫ ====================
CREATE TABLE IF NOT EXISTS referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,
    referred_id INTEGER NOT NULL,
    referral_code TEXT,
    bonus_days INTEGER DEFAULT 7,
    bonus_applied INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (referrer_id) REFERENCES clients(id),
    FOREIGN KEY (referred_id) REFERENCES clients(id),
    UNIQUE(referred_id)
);

CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
CREATE INDEX IF NOT EXISTS idx_referrals_code ON referrals(referral_code);

-- ==================== ГРУППЫ КЛИЕНТОВ ====================
CREATE TABLE IF NOT EXISTS client_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    discount_percent INTEGER DEFAULT 0,
    bonus_days INTEGER DEFAULT 0,
    priority INTEGER DEFAULT 0,
    color TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ==================== АУДИТ ЛОГ ====================
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    user_type TEXT DEFAULT 'manager',
    action TEXT NOT NULL,
    entity_type TEXT,
    entity_id INTEGER,
    old_value TEXT,
    new_value TEXT,
    ip_address TEXT,
    details TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_date ON audit_log(created_at);

-- ==================== ЕЖЕДНЕВНАЯ СТАТИСТИКА ====================
CREATE TABLE IF NOT EXISTS daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,

    keys_created INTEGER DEFAULT 0,
    keys_extended INTEGER DEFAULT 0,
    keys_expired INTEGER DEFAULT 0,
    keys_deleted INTEGER DEFAULT 0,

    revenue INTEGER DEFAULT 0,
    avg_order_value INTEGER DEFAULT 0,

    new_clients INTEGER DEFAULT 0,
    active_clients INTEGER DEFAULT 0,
    churned_clients INTEGER DEFAULT 0,

    period_1m INTEGER DEFAULT 0,
    period_3m INTEGER DEFAULT 0,
    period_6m INTEGER DEFAULT 0,
    period_1y INTEGER DEFAULT 0,

    promo_uses INTEGER DEFAULT 0,
    promo_discount_total INTEGER DEFAULT 0,

    referrals_count INTEGER DEFAULT 0,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_daily_stats_date ON daily_stats(date);

-- ==================== ВОРОНКА ПРОДАЖ ====================
CREATE TABLE IF NOT EXISTS sales_funnel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE,
    telegram_id INTEGER,
    manager_id INTEGER,

    step_phone_at TIMESTAMP,
    step_period_at TIMESTAMP,
    step_confirm_at TIMESTAMP,
    step_complete_at TIMESTAMP,

    selected_period TEXT,
    selected_price INTEGER,
    promo_code TEXT,
    final_price INTEGER,

    status TEXT DEFAULT 'started',
    abandon_step TEXT,
    error_message TEXT,

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_funnel_session ON sales_funnel(session_id);
CREATE INDEX IF NOT EXISTS idx_funnel_status ON sales_funnel(status);
CREATE INDEX IF NOT EXISTS idx_funnel_date ON sales_funnel(created_at);

-- ==================== ЗАДАЧИ SCHEDULER ====================
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type TEXT NOT NULL,
    task_data TEXT,
    scheduled_at TIMESTAMP NOT NULL,
    executed_at TIMESTAMP,
    status TEXT DEFAULT 'pending',
    result TEXT,
    error TEXT,
    retry_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON scheduled_tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_scheduled ON scheduled_tasks(scheduled_at);

-- ==================== КЭШ ====================
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    value TEXT,
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_cache_expires ON cache(expires_at);

-- ==================== МИГРАЦИИ ====================
CREATE TABLE IF NOT EXISTS migrations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version TEXT UNIQUE NOT NULL,
    name TEXT,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# Дефолтные настройки уведомлений
DEFAULT_NOTIFICATION_SETTINGS = {
    "expiry_warning_days": "7,3,1",
    "send_to_client": "true",
    "send_to_manager": "true",
    "welcome_message": "true",
    "expired_message": "true"
}

# Дефолтные группы клиентов
DEFAULT_CLIENT_GROUPS = [
    {"name": "Standard", "description": "Обычные клиенты", "discount_percent": 0, "priority": 0},
    {"name": "VIP", "description": "VIP клиенты", "discount_percent": 15, "priority": 10, "color": "#FFD700"},
    {"name": "Corporate", "description": "Корпоративные клиенты", "discount_percent": 20, "priority": 20, "color": "#4169E1"},
    {"name": "Trial", "description": "Пробный период", "discount_percent": 0, "priority": -10, "color": "#808080"}
]


async def init_extended_tables(db_path: str):
    """Инициализация расширенных таблиц"""
    async with aiosqlite.connect(db_path) as db:
        # Создаём таблицы
        await db.executescript(EXTENDED_TABLES_SQL)

        # Добавляем дефолтные настройки уведомлений
        for key, value in DEFAULT_NOTIFICATION_SETTINGS.items():
            await db.execute(
                """INSERT OR IGNORE INTO notification_settings (setting_key, setting_value)
                   VALUES (?, ?)""",
                (key, value)
            )

        # Добавляем дефолтные группы
        for group in DEFAULT_CLIENT_GROUPS:
            await db.execute(
                """INSERT OR IGNORE INTO client_groups (name, description, discount_percent, priority, color)
                   VALUES (?, ?, ?, ?, ?)""",
                (group["name"], group["description"], group["discount_percent"],
                 group["priority"], group.get("color"))
            )

        # Записываем миграцию
        await db.execute(
            """INSERT OR IGNORE INTO migrations (version, name) VALUES (?, ?)""",
            ("001", "extended_tables")
        )

        await db.commit()
        logger.info("Расширенные таблицы БД инициализированы")


async def migrate_existing_data(db_path: str):
    """Миграция существующих данных в новую структуру"""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Проверяем, была ли уже миграция
        cursor = await db.execute(
            "SELECT 1 FROM migrations WHERE version = ?", ("002_data_migration",)
        )
        if await cursor.fetchone():
            logger.info("Миграция данных уже выполнена")
            return

        # Получаем все записи из keys_history
        cursor = await db.execute(
            """SELECT DISTINCT client_email, phone_number, client_id, manager_id,
                      MAX(created_at) as last_created, MAX(expire_days) as expire_days
               FROM keys_history
               GROUP BY client_email"""
        )
        rows = await cursor.fetchall()

        migrated_count = 0
        for row in rows:
            if not row['client_email'] or not row['client_id']:
                continue

            # Вычисляем expire_time
            expire_time = None
            if row['expire_days'] and row['last_created']:
                try:
                    created = datetime.fromisoformat(row['last_created'].replace('Z', '+00:00'))
                    expire_dt = created + timedelta(days=row['expire_days'])
                    expire_time = int(expire_dt.timestamp() * 1000)
                except:
                    pass

            # Определяем статус
            status = 'active'
            if expire_time and expire_time < int(datetime.now().timestamp() * 1000):
                status = 'expired'

            try:
                await db.execute(
                    """INSERT OR IGNORE INTO clients
                       (uuid, email, phone, created_by, status, expire_time, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (row['client_id'], row['client_email'], row['phone_number'],
                     row['manager_id'], status, expire_time, row['last_created'])
                )
                migrated_count += 1
            except Exception as e:
                logger.warning(f"Ошибка миграции клиента {row['client_email']}: {e}")

        # Записываем миграцию
        await db.execute(
            """INSERT INTO migrations (version, name) VALUES (?, ?)""",
            ("002_data_migration", f"migrated {migrated_count} clients")
        )

        await db.commit()
        logger.info(f"Мигрировано {migrated_count} клиентов в новую структуру")
