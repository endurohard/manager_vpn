# Логика генерации VLESS ключей

## Как работает создание ключей

### 1. Создание клиента в X-UI Panel

Когда менеджер создает ключ, бот:
- Создает клиента в inbound 12
- Использует номер телефона как `email` и `tgId`
- Устанавливает срок действия, лимит IP (2) и безлимитный трафик

### 2. Получение VLESS ссылки

Бот получает VLESS ключ **с реальным IP адресом сервера**:

```python
# Получаем ссылку с IP сервера (оригинал)
vless_link_original = await xui_client.get_client_link(
    inbound_id=INBOUND_ID,
    client_email=phone,
    use_domain=None  # None = использовать IP из inbound
)
```

**Пример оригинальной ссылки:**
```
vless://550e8400-e29b-41d4-a716-446655440000@localhost:1020?type=tcp&security=tls#client
```

### 3. Замена IP на домен

Для выдачи пользователю IP заменяется на домен:

```python
# Создаем версию с доменом для пользователя
vless_link_for_user = XUIClient.replace_ip_with_domain(
    vless_link_original,
    DOMAIN
)
```

**Пример ссылки для пользователя:**
```
vless://550e8400-e29b-41d4-a716-446655440000@raphaeilvpn.ru:1020?type=tcp&security=tls#client
```

### 4. Что выдается пользователю

Пользователь получает:
- **QR код** с доменом (raphaeilvpn.ru)
- **Текстовую VLESS ссылку** с доменом

### 5. Что сохраняется в базе данных

В таблице `keys_history` сохраняются:
- `manager_id` - ID менеджера
- `client_email` - номер телефона клиента
- `phone_number` - номер телефона
- `period` - срок действия (Месяц, 3 месяца, и т.д.)
- `expire_days` - количество дней
- `client_id` - UUID клиента
- `price` - цена ключа
- `created_at` - дата создания

⚠️ **Важно:** Сама VLESS ссылка НЕ сохраняется в БД (ни с IP, ни с доменом).

## Функции API клиента

### `get_client_link(inbound_id, client_email, use_domain=None)`

Получает VLESS ссылку для клиента.

**Параметры:**
- `inbound_id` - ID inbound (по умолчанию 12)
- `client_email` - email клиента (используется номер телефона)
- `use_domain` - домен для замены (если `None`, использует IP из inbound)

**Логика определения адреса:**
```python
if use_domain:
    host = use_domain
else:
    # Берем listen из inbound
    host = inbound.get('listen', '')
    # Если listen пустой или 0.0.0.0
    if not host or host == '0.0.0.0':
        # Извлекаем адрес из XUI_HOST
        host = извлечь_из_URL(self.host)
```

### `replace_ip_with_domain(vless_link, domain)`

Статический метод для замены IP на домен в уже готовой VLESS ссылке.

**Параметры:**
- `vless_link` - оригинальная ссылка с IP
- `domain` - домен для замены

**Пример:**
```python
original = "vless://uuid@192.168.1.1:443?params#name"
replaced = XUIClient.replace_ip_with_domain(original, "example.com")
# Результат: "vless://uuid@example.com:443?params#name"
```

## Схема работы

```
1. Менеджер создает ключ
        ↓
2. Бот создает клиента в X-UI (inbound 12)
        ↓
3. Бот получает VLESS ключ с IP сервера
   vless://uuid@localhost:1020...
        ↓
4. Бот заменяет IP на домен
   vless://uuid@raphaeilvpn.ru:1020...
        ↓
5. Генерирует QR код с доменом
        ↓
6. Отправляет пользователю:
   - QR код (с доменом)
   - Текст ключа (с доменом)
        ↓
7. Сохраняет метаданные в БД
   (без самой ссылки)
```

## Конфигурация

В `.env` файле:
```env
XUI_HOST=https://localhost:1020/Raphael
INBOUND_ID=12
DOMAIN=raphaeilvpn.ru
```

- `XUI_HOST` - адрес X-UI панели (используется для получения IP)
- `INBOUND_ID` - ID inbound для создания ключей
- `DOMAIN` - домен для замены в ключах

## Преимущества такого подхода

1. ✅ Ключи создаются с реальным адресом сервера
2. ✅ Пользователи получают ключи с красивым доменом
3. ✅ Можно легко изменить домен, не пересоздавая ключи
4. ✅ Гибкость в настройке
5. ✅ БД остается чистой - хранятся только метаданные

## Тестирование

Запустите тест замены IP на домен:
```bash
python3 test_ip_domain_replace.py
```

Тест проверяет:
- Замену различных IP адресов (192.168.x.x, 10.0.0.x, localhost, 127.0.0.1)
- Корректность замены в разных типах VLESS ссылок
- Сохранение всех параметров подключения
