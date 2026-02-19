# План улучшения VPN Manager Bot

## Текущее состояние
- 4 таблицы БД (managers, keys_history, key_replacements, pending_keys)
- 3 сервера (1 локальный, 2 удалённых)
- ~3000 строк Python кода
- Базовый функционал создания/удаления ключей

---

## КРИТИЧЕСКИЕ УЛУЧШЕНИЯ (Приоритет 1)

### 1.1 Таблица клиентов (clients)

**Проблема:** Сейчас клиенты хранятся только в X-UI, нет единой базы.

**Решение:** Создать таблицу `clients` для централизованного учёта.

```sql
CREATE TABLE clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid TEXT UNIQUE NOT NULL,              -- UUID клиента в X-UI
    email TEXT UNIQUE NOT NULL,             -- Email/ID клиента
    phone TEXT,                             -- Телефон
    name TEXT,                              -- Имя клиента
    telegram_id INTEGER,                    -- Telegram ID (если есть)

    -- Статус подписки
    status TEXT DEFAULT 'active',           -- active, expired, suspended, deleted
    expire_time INTEGER,                    -- Unix timestamp истечения (ms)

    -- Связи
    created_by INTEGER,                     -- ID менеджера, создавшего
    current_server TEXT,                    -- Текущий основной сервер

    -- Метаданные
    total_traffic INTEGER DEFAULT 0,        -- Использованный трафик (bytes)
    last_connect_at TIMESTAMP,              -- Последнее подключение
    ip_limit INTEGER DEFAULT 2,             -- Лимит IP

    -- Даты
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (created_by) REFERENCES managers(user_id)
);

CREATE INDEX idx_clients_uuid ON clients(uuid);
CREATE INDEX idx_clients_email ON clients(email);
CREATE INDEX idx_clients_status ON clients(status);
CREATE INDEX idx_clients_expire ON clients(expire_time);
CREATE INDEX idx_clients_telegram ON clients(telegram_id);
```

### 1.2 Таблица серверов клиента

**Проблема:** Нет отслеживания на каких серверах есть клиент.

```sql
CREATE TABLE client_servers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    server_name TEXT NOT NULL,              -- Имя сервера из config
    inbound_id INTEGER,                     -- ID inbound на сервере
    status TEXT DEFAULT 'active',           -- active, deleted, error
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (client_id) REFERENCES clients(id),
    UNIQUE(client_id, server_name)
);

CREATE INDEX idx_client_servers_client ON client_servers(client_id);
CREATE INDEX idx_client_servers_server ON client_servers(server_name);
```

### 1.3 История подписок

**Проблема:** keys_history не отслеживает продления и изменения.

```sql
CREATE TABLE subscription_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    action TEXT NOT NULL,                   -- created, extended, suspended, reactivated, deleted
    period TEXT,                            -- Период подписки
    days INTEGER,                           -- Количество дней
    price INTEGER DEFAULT 0,                -- Цена операции
    old_expire INTEGER,                     -- Старая дата истечения
    new_expire INTEGER,                     -- Новая дата истечения
    manager_id INTEGER,                     -- Кто выполнил
    note TEXT,                              -- Комментарий
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (client_id) REFERENCES clients(id),
    FOREIGN KEY (manager_id) REFERENCES managers(user_id)
);

CREATE INDEX idx_sub_history_client ON subscription_history(client_id);
CREATE INDEX idx_sub_history_action ON subscription_history(action);
CREATE INDEX idx_sub_history_date ON subscription_history(created_at);
```

---

## ФУНКЦИОНАЛЬНЫЕ УЛУЧШЕНИЯ (Приоритет 2)

### 2.1 Автоматические уведомления

```python
# Таблица уведомлений
CREATE TABLE notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER,
    type TEXT NOT NULL,                     -- expiry_warning, expired, traffic_limit
    days_before INTEGER,                    -- За сколько дней до истечения
    sent_at TIMESTAMP,
    status TEXT DEFAULT 'pending',          -- pending, sent, failed

    FOREIGN KEY (client_id) REFERENCES clients(id)
);

# Настройки уведомлений
notification_settings = {
    "expiry_warnings": [7, 3, 1],           -- Дней до истечения
    "send_to_telegram": True,
    "send_to_manager": True
}
```

**Функционал:**
- Уведомление за 7, 3, 1 день до истечения
- Уведомление при истечении
- Уведомление при превышении лимита трафика
- Отправка менеджеру и клиенту (если есть Telegram ID)

### 2.2 Промокоды и скидки

```sql
CREATE TABLE promo_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    discount_type TEXT NOT NULL,            -- percent, fixed, days
    discount_value INTEGER NOT NULL,        -- Значение скидки
    max_uses INTEGER DEFAULT 0,             -- 0 = безлимит
    current_uses INTEGER DEFAULT 0,
    valid_from TIMESTAMP,
    valid_until TIMESTAMP,
    min_period TEXT,                        -- Минимальный период
    created_by INTEGER,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE promo_uses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    promo_id INTEGER NOT NULL,
    client_id INTEGER NOT NULL,
    order_id INTEGER,
    discount_amount INTEGER,
    used_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (promo_id) REFERENCES promo_codes(id),
    FOREIGN KEY (client_id) REFERENCES clients(id)
);
```

### 2.3 Реферальная система

```sql
CREATE TABLE referrals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    referrer_id INTEGER NOT NULL,           -- Кто пригласил (client_id)
    referred_id INTEGER NOT NULL,           -- Кого пригласил (client_id)
    bonus_days INTEGER DEFAULT 0,           -- Бонусные дни рефереру
    bonus_applied INTEGER DEFAULT 0,        -- Применён ли бонус
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (referrer_id) REFERENCES clients(id),
    FOREIGN KEY (referred_id) REFERENCES clients(id),
    UNIQUE(referred_id)                     -- Можно быть приглашённым только раз
);
```

**Функционал:**
- Генерация реферальных ссылок
- +7 дней рефереру за каждого приглашённого
- Статистика рефералов

### 2.4 Группы клиентов

```sql
CREATE TABLE client_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT,
    discount_percent INTEGER DEFAULT 0,     -- Скидка для группы
    priority INTEGER DEFAULT 0,             -- Приоритет обслуживания
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Связь клиент-группа
ALTER TABLE clients ADD COLUMN group_id INTEGER REFERENCES client_groups(id);
```

**Примеры групп:**
- VIP (скидка 20%)
- Корпоративные (приоритет)
- Тестовые (лимитированные)

---

## АНАЛИТИЧЕСКИЕ УЛУЧШЕНИЯ (Приоритет 3)

### 3.1 Расширенная статистика

```sql
-- Ежедневная агрегированная статистика
CREATE TABLE daily_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,                     -- YYYY-MM-DD

    -- Ключи
    keys_created INTEGER DEFAULT 0,
    keys_extended INTEGER DEFAULT 0,
    keys_expired INTEGER DEFAULT 0,
    keys_deleted INTEGER DEFAULT 0,

    -- Финансы
    revenue INTEGER DEFAULT 0,
    avg_order_value INTEGER DEFAULT 0,

    -- Клиенты
    new_clients INTEGER DEFAULT 0,
    active_clients INTEGER DEFAULT 0,
    churned_clients INTEGER DEFAULT 0,

    -- По периодам
    period_1m INTEGER DEFAULT 0,
    period_3m INTEGER DEFAULT 0,
    period_6m INTEGER DEFAULT 0,
    period_1y INTEGER DEFAULT 0,

    UNIQUE(date)
);

CREATE INDEX idx_daily_stats_date ON daily_stats(date);
```

### 3.2 Воронка продаж

```sql
CREATE TABLE sales_funnel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,                        -- Уникальный ID сессии
    telegram_id INTEGER,

    -- Этапы воронки
    started_at TIMESTAMP,                   -- Начал создание ключа
    selected_period_at TIMESTAMP,           -- Выбрал период
    confirmed_at TIMESTAMP,                 -- Подтвердил
    completed_at TIMESTAMP,                 -- Ключ создан

    -- Результат
    period_key TEXT,
    price INTEGER,
    status TEXT,                            -- completed, abandoned, error
    abandon_step TEXT,                      -- На каком шаге бросил

    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 3.3 Отчёты

**Новые команды:**
```
/report daily    - Отчёт за день
/report weekly   - Отчёт за неделю
/report monthly  - Отчёт за месяц
/report churn    - Отчёт по оттоку
/report revenue  - Финансовый отчёт
/export csv      - Экспорт данных
```

---

## ТЕХНИЧЕСКИЕ УЛУЧШЕНИЯ (Приоритет 4)

### 4.1 Кэширование

```python
# Redis или in-memory кэш
cache_config = {
    "servers_config": {"ttl": 300},         # 5 минут
    "client_info": {"ttl": 60},             # 1 минута
    "stats": {"ttl": 300},                  # 5 минут
    "vless_links": {"ttl": 3600},           # 1 час
}

class CacheManager:
    async def get_or_set(self, key, factory, ttl):
        """Получить из кэша или вычислить"""
        ...
```

### 4.2 Background задачи

```python
# Scheduler для фоновых задач
tasks = {
    "sync_clients": {
        "interval": "1h",
        "func": sync_clients_with_xui
    },
    "send_notifications": {
        "interval": "6h",
        "func": check_and_send_notifications
    },
    "cleanup_expired": {
        "interval": "1d",
        "func": cleanup_expired_clients
    },
    "aggregate_stats": {
        "interval": "1d",
        "time": "00:05",
        "func": aggregate_daily_stats
    },
    "health_check": {
        "interval": "5m",
        "func": check_servers_health
    }
}
```

### 4.3 API для внешних интеграций

```python
# REST API для WebApp и внешних систем
from fastapi import FastAPI

app = FastAPI()

@app.get("/api/v1/clients/{client_id}")
async def get_client(client_id: int):
    ...

@app.post("/api/v1/clients")
async def create_client(data: ClientCreate):
    ...

@app.get("/api/v1/stats/dashboard")
async def get_dashboard():
    ...

@app.post("/api/v1/webhooks/payment")
async def payment_webhook(data: PaymentData):
    """Интеграция с платёжными системами"""
    ...
```

### 4.4 Миграции БД

```python
# Система миграций
migrations = [
    ("001", "create_clients_table", create_clients_table),
    ("002", "create_client_servers", create_client_servers),
    ("003", "add_notifications", add_notifications),
    ("004", "add_promo_codes", add_promo_codes),
]

async def run_migrations():
    """Автоматический запуск миграций при старте"""
    ...
```

---

## UI/UX УЛУЧШЕНИЯ (Приоритет 5)

### 5.1 Inline режим

```python
@router.inline_query()
async def inline_search(query: InlineQuery):
    """Поиск клиента прямо в чате"""
    results = await search_clients(query.query)
    # Показать карточки клиентов
```

### 5.2 Карточка клиента

```
📋 Клиент: user_12345

👤 Телефон: +79001234567
📅 Статус: Активен
⏰ Истекает: 15.02.2026 (через 43 дня)
📊 Трафик: 15.2 GB
🌐 Серверы: Germany, Niderland

[🔗 Ссылка] [📅 Продлить] [⚙️ Настройки]
[📊 История] [🗑 Удалить]
```

### 5.3 Dashboard админа

```
📊 DASHBOARD (сегодня)

💰 Доход: 15,600 ₽ (+12% к вчера)
🔑 Создано ключей: 52
📈 Активных клиентов: 487
⚠️ Истекает сегодня: 8

📅 За месяц:
├ Доход: 450,000 ₽
├ Новых клиентов: 89
├ Продлений: 156
└ Отток: 12 (2.4%)

[📈 Подробная статистика]
[👥 Менеджеры]
[⚙️ Настройки]
```

### 5.4 Wizard создания ключа

```
Шаг 1/4: Идентификатор
┌─────────────────────────┐
│ Введите телефон/ID      │
│ или нажмите кнопку      │
│                         │
│ [🎲 Сгенерировать]      │
│ [📱 Из контактов]       │
└─────────────────────────┘

Шаг 2/4: Период
┌─────────────────────────┐
│ Выберите срок действия  │
│                         │
│ [1 месяц - 300₽]        │
│ [3 месяца - 800₽] ⭐    │
│ [6 месяцев - 1500₽]     │
│ [1 год - 2500₽]         │
│                         │
│ 💡 3 месяца - выгоднее! │
└─────────────────────────┘
```

---

## БЕЗОПАСНОСТЬ (Приоритет 6)

### 6.1 Аудит действий

```sql
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    entity_type TEXT,                       -- client, key, manager, settings
    entity_id INTEGER,
    old_value TEXT,                         -- JSON старого значения
    new_value TEXT,                         -- JSON нового значения
    ip_address TEXT,
    user_agent TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_audit_user ON audit_log(user_id);
CREATE INDEX idx_audit_action ON audit_log(action);
CREATE INDEX idx_audit_date ON audit_log(created_at);
```

### 6.2 Rate limiting

```python
rate_limits = {
    "create_key": {"limit": 10, "window": 60},      # 10 в минуту
    "search": {"limit": 30, "window": 60},          # 30 в минуту
    "api_request": {"limit": 100, "window": 60},    # 100 в минуту
}
```

### 6.3 Шифрование чувствительных данных

```python
# Шифрование паролей и ключей в БД
from cryptography.fernet import Fernet

class SecureStorage:
    def encrypt(self, data: str) -> str:
        ...

    def decrypt(self, encrypted: str) -> str:
        ...
```

---

## ДОРОЖНАЯ КАРТА

### Фаза 1 (1-2 недели)
- [ ] Таблица clients
- [ ] Таблица client_servers
- [ ] Миграция существующих данных
- [ ] Синхронизация с X-UI

### Фаза 2 (2-3 недели)
- [ ] Subscription history
- [ ] Автоматические уведомления
- [ ] Background scheduler
- [ ] Улучшенная статистика

### Фаза 3 (3-4 недели)
- [ ] Промокоды
- [ ] Реферальная система
- [ ] REST API
- [ ] WebApp интеграция

### Фаза 4 (4-5 недель)
- [ ] Аудит и безопасность
- [ ] Кэширование
- [ ] Расширенная аналитика
- [ ] UI/UX улучшения

---

## ОЦЕНКА СЛОЖНОСТИ

| Улучшение | Сложность | Время | Влияние |
|-----------|-----------|-------|---------|
| Таблица clients | Средняя | 2-3 дня | Высокое |
| Уведомления | Средняя | 2-3 дня | Высокое |
| Промокоды | Низкая | 1-2 дня | Среднее |
| Рефералы | Низкая | 1-2 дня | Среднее |
| Статистика | Средняя | 3-4 дня | Среднее |
| Кэширование | Средняя | 2-3 дня | Высокое |
| REST API | Высокая | 4-5 дней | Высокое |
| Аудит | Низкая | 1 день | Среднее |

---

Дата создания: 2026-01-03
