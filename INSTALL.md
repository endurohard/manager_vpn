# Быстрая установка и настройка

## Шаг 1: Создание Telegram бота

1. Найдите @BotFather в Telegram
2. Отправьте команду `/newbot`
3. Следуйте инструкциям и получите токен бота
4. Сохраните токен - он понадобится для настройки

## Шаг 2: Получение вашего Telegram ID

1. Найдите @userinfobot в Telegram
2. Нажмите "Старт"
3. Скопируйте ваш ID (числовое значение)

## Шаг 3: Настройка X-UI панели

Убедитесь, что у вас установлена и запущена X-UI панель.

Проверьте:
- URL панели (обычно http://localhost:54321 или http://IP:54321)
- Имя пользователя и пароль для входа
- ID inbound для VLESS (обычно можно посмотреть в панели X-UI)

## Шаг 4: Установка бота

```bash
# 1. Перейдите в директорию проекта
cd /root/manager_vpn

# 2. Создайте виртуальное окружение
python3 -m venv venv

# 3. Активируйте виртуальное окружение
source venv/bin/activate

# 4. Установите зависимости
pip install -r requirements.txt

# 5. Создайте .env файл из примера
cp .env.example .env

# 6. Отредактируйте .env файл
nano .env
```

## Шаг 5: Настройка .env файла

Откройте файл `.env` и заполните следующие параметры:

```env
# Токен бота от @BotFather
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz

# Ваш Telegram ID от @userinfobot
ADMIN_ID=123456789

# URL вашей X-UI панели
XUI_HOST=http://localhost:54321

# Логин и пароль X-UI панели
XUI_USERNAME=admin
XUI_PASSWORD=admin

# ID inbound для создания ключей (проверьте в X-UI панели)
INBOUND_ID=12

# Ваш домен для VPN
DOMAIN=raphaeilvpn.ru
```

Сохраните файл (Ctrl+O, Enter, Ctrl+X)

## Шаг 6: Запуск бота

### Вариант 1: Запуск в текущей сессии (для тестирования)
```bash
python3 main.py
```

### Вариант 2: Запуск через screen (фоновый режим)
```bash
screen -S vpn_bot
python3 main.py
# Нажмите Ctrl+A, затем D для отключения от screen
# Для возврата: screen -r vpn_bot
```

### Вариант 3: Запуск как системный сервис (рекомендуется)

Создайте файл сервиса:
```bash
sudo nano /etc/systemd/system/vpn-bot.service
```

Вставьте следующее содержимое:
```ini
[Unit]
Description=VPN Manager Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/manager_vpn
Environment="PATH=/root/manager_vpn/venv/bin"
ExecStart=/root/manager_vpn/venv/bin/python3 /root/manager_vpn/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Запустите сервис:
```bash
sudo systemctl daemon-reload
sudo systemctl enable vpn-bot
sudo systemctl start vpn-bot
sudo systemctl status vpn-bot
```

## Шаг 7: Проверка работы

1. Найдите вашего бота в Telegram (по имени, которое вы дали в @BotFather)
2. Нажмите "Старт" или отправьте `/start`
3. Вы должны увидеть приветственное сообщение с главным меню
4. Попробуйте создать тестовый ключ

## Управление сервисом (если используете systemd)

```bash
# Просмотр логов
sudo journalctl -u vpn-bot -f

# Перезапуск бота
sudo systemctl restart vpn-bot

# Остановка бота
sudo systemctl stop vpn-bot

# Проверка статуса
sudo systemctl status vpn-bot
```

## Устранение проблем

### Бот не отвечает
1. Проверьте, запущен ли бот: `systemctl status vpn-bot`
2. Проверьте логи: `tail -f bot.log` или `journalctl -u vpn-bot -f`
3. Убедитесь, что токен бота корректный
4. Проверьте интернет-соединение

### Ошибка подключения к X-UI
1. Проверьте, запущена ли X-UI панель
2. Убедитесь, что URL, логин и пароль корректны
3. Попробуйте открыть панель в браузере по указанному URL
4. Проверьте файрволл и доступ к порту

### Не создается ключ
1. Проверьте, существует ли inbound с указанным ID в X-UI панели
2. Убедитесь, что inbound настроен на VLESS протокол
3. Проверьте логи бота на наличие ошибок

### Ошибка "У вас нет доступа"
1. Убедитесь, что ваш Telegram ID корректно указан в .env как ADMIN_ID
2. Перезапустите бота после изменения .env файла

## Добавление менеджеров

После успешного запуска:
1. Откройте "Панель администратора"
2. Нажмите "Добавить менеджера"
3. Отправьте Telegram ID нового менеджера
4. Менеджер может использовать бота командой `/start`
