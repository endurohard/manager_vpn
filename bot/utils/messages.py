"""
Система шаблонов сообщений для Telegram бота

Централизованное хранение и форматирование всех текстовых сообщений.
Поддержка:
- Шаблоны с переменными
- Локализация (подготовка)
- Экранирование HTML/Markdown
- Генерация клавиатур
"""
import re
import json
import logging
from typing import Dict, Any, Optional, List, Union
from dataclasses import dataclass
from datetime import datetime, timedelta
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

logger = logging.getLogger(__name__)


def escape_html(text: str) -> str:
    """Экранировать HTML символы"""
    return (
        text
        .replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )


def escape_markdown(text: str) -> str:
    """Экранировать Markdown символы"""
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text


def format_bytes(size: int) -> str:
    """Форматировать размер в байтах"""
    if size < 1024:
        return f"{size} B"
    elif size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    elif size < 1024 ** 3:
        return f"{size / 1024**2:.1f} MB"
    else:
        return f"{size / 1024**3:.2f} GB"


def format_duration(days: int) -> str:
    """Форматировать длительность в днях"""
    if days == 1:
        return "1 день"
    elif days < 5:
        return f"{days} дня"
    elif days < 21:
        return f"{days} дней"
    elif days % 10 == 1:
        return f"{days} день"
    elif days % 10 < 5:
        return f"{days} дня"
    else:
        return f"{days} дней"


def format_datetime(dt: datetime) -> str:
    """Форматировать дату и время"""
    return dt.strftime("%d.%m.%Y %H:%M")


def format_date(dt: datetime) -> str:
    """Форматировать только дату"""
    return dt.strftime("%d.%m.%Y")


def format_price(amount: int) -> str:
    """Форматировать цену"""
    return f"{amount} руб."


@dataclass
class MessageTemplate:
    """Шаблон сообщения"""
    text: str
    parse_mode: str = "HTML"
    keyboard: Optional[InlineKeyboardMarkup] = None

    def format(self, **kwargs) -> str:
        """Форматировать шаблон с переменными"""
        try:
            return self.text.format(**kwargs)
        except KeyError as e:
            logger.warning(f"Missing template variable: {e}")
            return self.text


# ============================================================================
# Шаблоны сообщений
# ============================================================================

class Messages:
    """Централизованное хранилище сообщений"""

    # --- Приветствие и основное меню ---

    WELCOME = MessageTemplate(
        text="""Добро пожаловать в VPN Manager!

Здесь вы можете приобрести и управлять VPN ключами.

Выберите действие:"""
    )

    WELCOME_BACK = MessageTemplate(
        text="""С возвращением, {name}!

У вас {keys_count} активных ключей.

Выберите действие:"""
    )

    # --- Ключи ---

    KEY_INFO = MessageTemplate(
        text="""<b>Информация о ключе</b>

<b>Название:</b> {name}
<b>Сервер:</b> {server}
<b>Статус:</b> {status}
<b>Создан:</b> {created_at}
<b>Истекает:</b> {expires_at}

<b>Трафик:</b> {traffic_used} / {traffic_limit}
<b>Устройств:</b> {devices_used} / {devices_limit}"""
    )

    KEY_CREATED = MessageTemplate(
        text="""<b>Ключ успешно создан!</b>

<b>Название:</b> {name}
<b>Сервер:</b> {server}
<b>Действует до:</b> {expires_at}

<b>Ваш ключ:</b>
<code>{vless_key}</code>

<i>Нажмите на ключ, чтобы скопировать</i>"""
    )

    KEY_EXTENDED = MessageTemplate(
        text="""<b>Ключ продлён!</b>

<b>Название:</b> {name}
<b>Добавлено:</b> {days_added}
<b>Новая дата окончания:</b> {new_expires_at}"""
    )

    KEY_DELETED = MessageTemplate(
        text="""<b>Ключ удалён</b>

Ключ "{name}" был удалён со всех серверов."""
    )

    KEY_EXPIRED = MessageTemplate(
        text="""<b>Внимание!</b>

Срок действия вашего ключа <b>{name}</b> истекает через {days_left}!

Продлите ключ, чтобы не потерять доступ."""
    )

    KEY_TRAFFIC_WARNING = MessageTemplate(
        text="""<b>Внимание!</b>

Трафик по ключу <b>{name}</b> использован на {percent}%.

Осталось: {remaining}"""
    )

    NO_KEYS = MessageTemplate(
        text="""У вас пока нет активных ключей.

Нажмите "Получить ключ" чтобы создать новый."""
    )

    # --- Покупка ---

    SELECT_TARIFF = MessageTemplate(
        text="""<b>Выберите тариф</b>

Выберите период действия ключа:"""
    )

    SELECT_SERVER = MessageTemplate(
        text="""<b>Выберите сервер</b>

Доступные серверы:"""
    )

    PAYMENT_INFO = MessageTemplate(
        text="""<b>Оплата</b>

<b>Тариф:</b> {tariff_name}
<b>Период:</b> {period}
<b>Сумма:</b> <b>{price}</b>

<b>Реквизиты для оплаты:</b>
{requisites}

После оплаты нажмите кнопку "Я оплатил" и прикрепите чек."""
    )

    PAYMENT_RECEIVED = MessageTemplate(
        text="""<b>Оплата получена!</b>

Ваш заказ #{order_id} принят в обработку.
Ключ будет создан в ближайшее время."""
    )

    PAYMENT_CONFIRMED = MessageTemplate(
        text="""<b>Заказ #{order_id} подтверждён!</b>

Ваш ключ создан и готов к использованию.

<b>Ваш ключ:</b>
<code>{vless_key}</code>"""
    )

    PAYMENT_REJECTED = MessageTemplate(
        text="""<b>Заказ #{order_id} отклонён</b>

Причина: {reason}

Если вы уверены в оплате, свяжитесь с поддержкой."""
    )

    # --- Промокоды ---

    PROMO_APPLIED = MessageTemplate(
        text="""<b>Промокод применён!</b>

<b>Скидка:</b> {discount}%
<b>Новая цена:</b> <s>{old_price}</s> → <b>{new_price}</b>"""
    )

    PROMO_INVALID = MessageTemplate(
        text="""Промокод недействителен или уже использован."""
    )

    PROMO_EXPIRED = MessageTemplate(
        text="""Срок действия промокода истёк."""
    )

    # --- Ошибки ---

    ERROR_GENERIC = MessageTemplate(
        text="""Произошла ошибка. Попробуйте позже или обратитесь в поддержку."""
    )

    ERROR_SERVER_UNAVAILABLE = MessageTemplate(
        text="""Сервер временно недоступен. Попробуйте позже."""
    )

    ERROR_KEY_NOT_FOUND = MessageTemplate(
        text="""Ключ не найден."""
    )

    ERROR_PAYMENT_TIMEOUT = MessageTemplate(
        text="""Время ожидания оплаты истекло. Создайте новый заказ."""
    )

    ERROR_NO_SERVERS = MessageTemplate(
        text="""Нет доступных серверов для создания ключа. Попробуйте позже."""
    )

    # --- Админ сообщения ---

    ADMIN_NEW_ORDER = MessageTemplate(
        text="""<b>Новый заказ!</b>

<b>ID:</b> #{order_id}
<b>Пользователь:</b> {user_name} (ID: {user_id})
<b>Тариф:</b> {tariff}
<b>Сумма:</b> {price}
<b>Время:</b> {created_at}"""
    )

    ADMIN_PAYMENT_RECEIVED = MessageTemplate(
        text="""<b>Получена оплата!</b>

<b>Заказ:</b> #{order_id}
<b>Пользователь:</b> {user_name}
<b>Сумма:</b> {price}

Проверьте оплату и подтвердите заказ."""
    )

    ADMIN_STATS = MessageTemplate(
        text="""<b>Статистика</b>

<b>Пользователей:</b> {total_users}
<b>Активных ключей:</b> {active_keys}
<b>Заказов сегодня:</b> {orders_today}
<b>Доход сегодня:</b> {revenue_today}

<b>За месяц:</b>
<b>Заказов:</b> {orders_month}
<b>Доход:</b> {revenue_month}"""
    )

    ADMIN_SERVER_STATUS = MessageTemplate(
        text="""<b>Статус серверов</b>

{servers_status}"""
    )

    # --- Инструкции ---

    INSTRUCTION_IOS = MessageTemplate(
        text="""<b>Инструкция для iOS</b>

1. Скачайте приложение <b>Streisand</b> из App Store
2. Откройте приложение
3. Нажмите "+" и выберите "Импорт из буфера"
4. Вставьте ваш ключ
5. Включите VPN

<a href="https://apps.apple.com/app/streisand/id6450534064">Скачать Streisand</a>"""
    )

    INSTRUCTION_ANDROID = MessageTemplate(
        text="""<b>Инструкция для Android</b>

1. Скачайте приложение <b>v2rayNG</b>
2. Откройте приложение
3. Нажмите "+" и выберите "Импорт из буфера"
4. Вставьте ваш ключ
5. Нажмите кнопку подключения

<a href="https://play.google.com/store/apps/details?id=com.v2ray.ang">Скачать v2rayNG</a>"""
    )

    INSTRUCTION_WINDOWS = MessageTemplate(
        text="""<b>Инструкция для Windows</b>

1. Скачайте <b>Hiddify</b> с официального сайта
2. Установите и запустите
3. Нажмите "+" → "Импорт из буфера"
4. Вставьте ваш ключ
5. Нажмите "Подключить"

<a href="https://github.com/hiddify/hiddify-next/releases">Скачать Hiddify</a>"""
    )

    INSTRUCTION_MACOS = MessageTemplate(
        text="""<b>Инструкция для macOS</b>

1. Скачайте <b>Hiddify</b> или <b>V2Box</b>
2. Установите и запустите
3. Импортируйте ключ из буфера
4. Подключитесь

<a href="https://github.com/hiddify/hiddify-next/releases">Скачать Hiddify</a>"""
    )


# ============================================================================
# Генераторы клавиатур
# ============================================================================

class Keyboards:
    """Генераторы клавиатур"""

    @staticmethod
    def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
        """Главное меню"""
        buttons = [
            [InlineKeyboardButton(text="Мои ключи", callback_data="my_keys")],
            [InlineKeyboardButton(text="Получить ключ", callback_data="get_key")],
            [InlineKeyboardButton(text="Инструкции", callback_data="instructions")],
            [InlineKeyboardButton(text="Поддержка", callback_data="support")],
        ]

        if is_admin:
            buttons.append([
                InlineKeyboardButton(text="Админ-панель", callback_data="admin_panel")
            ])

        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def keys_list(keys: List[Dict]) -> InlineKeyboardMarkup:
        """Список ключей пользователя"""
        buttons = []

        for key in keys:
            status_emoji = "" if key.get('is_active') else ""
            text = f"{status_emoji} {key['name']}"
            buttons.append([
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"key_info:{key['id']}"
                )
            ])

        buttons.append([
            InlineKeyboardButton(text="Назад", callback_data="main_menu")
        ])

        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def key_actions(key_id: int, can_extend: bool = True) -> InlineKeyboardMarkup:
        """Действия с ключом"""
        buttons = [
            [InlineKeyboardButton(text="Показать ключ", callback_data=f"show_key:{key_id}")],
            [InlineKeyboardButton(text="QR-код", callback_data=f"key_qr:{key_id}")],
        ]

        if can_extend:
            buttons.append([
                InlineKeyboardButton(text="Продлить", callback_data=f"extend_key:{key_id}")
            ])

        buttons.extend([
            [InlineKeyboardButton(text="Удалить", callback_data=f"delete_key:{key_id}")],
            [InlineKeyboardButton(text="Назад", callback_data="my_keys")],
        ])

        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def tariffs(tariffs: List[Dict]) -> InlineKeyboardMarkup:
        """Выбор тарифа"""
        buttons = []

        for tariff in tariffs:
            text = f"{tariff['name']} - {tariff['price']} руб."
            buttons.append([
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"tariff:{tariff['key']}"
                )
            ])

        buttons.append([
            InlineKeyboardButton(text="Назад", callback_data="main_menu")
        ])

        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def servers(servers: List[Dict]) -> InlineKeyboardMarkup:
        """Выбор сервера"""
        buttons = []

        for server in servers:
            emoji = "" if server.get('is_available') else ""
            text = f"{emoji} {server['name']}"
            buttons.append([
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"server:{server['name']}"
                )
            ])

        buttons.append([
            InlineKeyboardButton(text="Назад", callback_data="select_tariff")
        ])

        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def payment_actions(order_id: str) -> InlineKeyboardMarkup:
        """Действия при оплате"""
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Я оплатил", callback_data=f"paid:{order_id}")],
            [InlineKeyboardButton(text="Применить промокод", callback_data=f"promo:{order_id}")],
            [InlineKeyboardButton(text="Отмена", callback_data="cancel_order")],
        ])

    @staticmethod
    def confirm_delete(key_id: int) -> InlineKeyboardMarkup:
        """Подтверждение удаления"""
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Да, удалить", callback_data=f"confirm_delete:{key_id}"),
                InlineKeyboardButton(text="Отмена", callback_data=f"key_info:{key_id}"),
            ]
        ])

    @staticmethod
    def extend_periods(key_id: int, periods: List[Dict]) -> InlineKeyboardMarkup:
        """Выбор периода продления"""
        buttons = []

        for period in periods:
            text = f"{period['name']} - {period['price']} руб."
            buttons.append([
                InlineKeyboardButton(
                    text=text,
                    callback_data=f"extend:{key_id}:{period['key']}"
                )
            ])

        buttons.append([
            InlineKeyboardButton(text="Назад", callback_data=f"key_info:{key_id}")
        ])

        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def instructions() -> InlineKeyboardMarkup:
        """Выбор платформы для инструкции"""
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="iOS", callback_data="instruction:ios"),
                InlineKeyboardButton(text="Android", callback_data="instruction:android"),
            ],
            [
                InlineKeyboardButton(text="Windows", callback_data="instruction:windows"),
                InlineKeyboardButton(text="macOS", callback_data="instruction:macos"),
            ],
            [InlineKeyboardButton(text="Назад", callback_data="main_menu")],
        ])

    @staticmethod
    def admin_menu() -> InlineKeyboardMarkup:
        """Админ меню"""
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Статистика", callback_data="admin:stats")],
            [InlineKeyboardButton(text="Серверы", callback_data="admin:servers")],
            [InlineKeyboardButton(text="Пользователи", callback_data="admin:users")],
            [InlineKeyboardButton(text="Заказы", callback_data="admin:orders")],
            [InlineKeyboardButton(text="Рассылка", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="Назад", callback_data="main_menu")],
        ])

    @staticmethod
    def admin_order_actions(order_id: str) -> InlineKeyboardMarkup:
        """Действия с заказом для админа"""
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Подтвердить", callback_data=f"admin:confirm:{order_id}"),
                InlineKeyboardButton(text="Отклонить", callback_data=f"admin:reject:{order_id}"),
            ],
            [InlineKeyboardButton(text="Подробнее", callback_data=f"admin:order_detail:{order_id}")],
        ])

    @staticmethod
    def back_button(callback_data: str = "main_menu") -> InlineKeyboardMarkup:
        """Кнопка назад"""
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Назад", callback_data=callback_data)]
        ])


# ============================================================================
# Хелперы
# ============================================================================

class MessageBuilder:
    """Билдер для создания сложных сообщений"""

    def __init__(self):
        self._parts: List[str] = []
        self._keyboard: Optional[InlineKeyboardMarkup] = None

    def add_line(self, text: str) -> 'MessageBuilder':
        self._parts.append(text)
        return self

    def add_empty_line(self) -> 'MessageBuilder':
        self._parts.append("")
        return self

    def add_bold(self, text: str) -> 'MessageBuilder':
        self._parts.append(f"<b>{escape_html(text)}</b>")
        return self

    def add_italic(self, text: str) -> 'MessageBuilder':
        self._parts.append(f"<i>{escape_html(text)}</i>")
        return self

    def add_code(self, text: str) -> 'MessageBuilder':
        self._parts.append(f"<code>{escape_html(text)}</code>")
        return self

    def add_link(self, text: str, url: str) -> 'MessageBuilder':
        self._parts.append(f'<a href="{url}">{escape_html(text)}</a>')
        return self

    def add_field(self, label: str, value: str) -> 'MessageBuilder':
        self._parts.append(f"<b>{label}:</b> {escape_html(str(value))}")
        return self

    def set_keyboard(self, keyboard: InlineKeyboardMarkup) -> 'MessageBuilder':
        self._keyboard = keyboard
        return self

    def build(self) -> tuple:
        """Вернуть текст и клавиатуру"""
        return "\n".join(self._parts), self._keyboard

    def text(self) -> str:
        """Вернуть только текст"""
        return "\n".join(self._parts)


__all__ = [
    # Утилиты
    'escape_html',
    'escape_markdown',
    'format_bytes',
    'format_duration',
    'format_datetime',
    'format_date',
    'format_price',

    # Классы
    'MessageTemplate',
    'Messages',
    'Keyboards',
    'MessageBuilder',
]
