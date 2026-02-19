"""
Клавиатуры для бота
"""
from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo
)
from bot.price_config import get_subscription_periods
from bot.config import WEBAPP_URL, ADMIN_PANEL_URL


class Keyboards:
    @staticmethod
    def main_menu(is_admin: bool = False):
        """Главное меню"""
        buttons = [
            [KeyboardButton(text="Создать ключ"), KeyboardButton(text="🔄 Замена ключа")],
            [KeyboardButton(text="🔧 Исправить ключ"), KeyboardButton(text="💰 Прайс")],
            [KeyboardButton(text="Моя статистика"), KeyboardButton(text="📖 Инструкции", web_app=WebAppInfo(url=WEBAPP_URL))]
        ]

        if is_admin:
            buttons.append([KeyboardButton(text="Панель администратора")])

        return ReplyKeyboardMarkup(
            keyboard=buttons,
            resize_keyboard=True
        )

    @staticmethod
    def admin_menu():
        """Меню администратора"""
        buttons = [
            [KeyboardButton(text="🔑 Создать ключ (выбор inbound)")],
            [KeyboardButton(text="Добавить менеджера")],
            [KeyboardButton(text="Список менеджеров")],
            [KeyboardButton(text="Общая статистика")],
            [KeyboardButton(text="Детальная статистика")],
            [KeyboardButton(text="💰 Изменить цены")],
            [KeyboardButton(text="🔍 Поиск ключа")],
            [KeyboardButton(text="🗑️ Удалить ключ")],
            [KeyboardButton(text="📢 Отправить уведомление")],
            [KeyboardButton(text="🌐 Управление SNI")],
            [KeyboardButton(text="💳 Реквизиты"), KeyboardButton(text="📋 Веб-заказы")],
            [KeyboardButton(text="📡 Добавить сервер")],
            [KeyboardButton(text="🖥 Статус серверов"), KeyboardButton(text="🔧 Панели X-UI")],
            [KeyboardButton(text="🌐 Админ-панель сайта")],
            [KeyboardButton(text="Назад")]
        ]
        return ReplyKeyboardMarkup(
            keyboard=buttons,
            resize_keyboard=True
        )

    @staticmethod
    def cancel_button():
        """Клавиатура с кнопкой отмены"""
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Отмена")]],
            resize_keyboard=True
        )

    @staticmethod
    def subscription_periods():
        """Инлайн клавиатура с периодами подписки"""
        periods = get_subscription_periods()  # Загружаем актуальные цены
        buttons = []
        for key, value in periods.items():
            buttons.append([
                InlineKeyboardButton(
                    text=f"{value['name']} - {value['price']} ₽",
                    callback_data=f"period_{key}"
                )
            ])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def replacement_periods(show_original: bool = False, remaining_days: int = 0):
        """Инлайн клавиатура с периодами для замены ключа (без цены)"""
        periods = get_subscription_periods()
        buttons = []

        # Если есть оригинальный срок - показываем его первым
        if show_original and remaining_days > 0:
            buttons.append([
                InlineKeyboardButton(
                    text=f"✅ Оставить оригинальный ({remaining_days} дн.)",
                    callback_data="replace_period_original"
                )
            ])

        for key, value in periods.items():
            buttons.append([
                InlineKeyboardButton(
                    text=f"{value['name']} ({value['days']} дней)",
                    callback_data=f"replace_period_{key}"
                )
            ])
        buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_replacement")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def confirm_key_replacement(phone: str, period: str):
        """Подтверждение замены ключа"""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Заменить", callback_data=f"replace_{phone}_{period}"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_replacement")
                ]
            ]
        )

    @staticmethod
    def cancel():
        """Кнопка отмены"""
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Отмена")]],
            resize_keyboard=True
        )

    @staticmethod
    def phone_input():
        """Клавиатура для ввода номера телефона или ID"""
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="Сгенерировать ID")],
                [KeyboardButton(text="Отмена")]
            ],
            resize_keyboard=True
        )

    @staticmethod
    def confirm_key_creation(phone: str, period: str):
        """Подтверждение создания ключа"""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Создать", callback_data=f"create_{phone}_{period}"),
                    InlineKeyboardButton(text="Отмена", callback_data="cancel_creation")
                ]
            ]
        )

    @staticmethod
    def admin_price_selection(standard_price: int):
        """Выбор цены для администратора"""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"💰 Стандартная цена ({standard_price} ₽)",
                        callback_data=f"price_standard_{standard_price}"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🎁 Бесплатно (0 ₽)",
                        callback_data="price_custom_0"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="✏️ Указать свою цену",
                        callback_data="price_custom_input"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="❌ Отмена",
                        callback_data="cancel_creation"
                    )
                ]
            ]
        )

    @staticmethod
    def detailed_stats_menu():
        """Меню детальной статистики"""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📅 По дням", callback_data="stats_by_days")],
                [InlineKeyboardButton(text="📆 По месяцам", callback_data="stats_by_months")],
                [InlineKeyboardButton(text="👥 По менеджерам", callback_data="stats_by_managers")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="stats_back")]
            ]
        )

    @staticmethod
    def stats_period_menu():
        """Меню выбора периода для статистики"""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="7 дней", callback_data="stats_days_7")],
                [InlineKeyboardButton(text="30 дней", callback_data="stats_days_30")],
                [InlineKeyboardButton(text="90 дней", callback_data="stats_days_90")],
                [InlineKeyboardButton(text="1 год", callback_data="stats_days_365")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="stats_menu")]
            ]
        )

    @staticmethod
    def stats_months_menu():
        """Меню выбора периода для статистики по месяцам"""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="3 месяца", callback_data="months_3")],
                [InlineKeyboardButton(text="6 месяцев", callback_data="months_6")],
                [InlineKeyboardButton(text="12 месяцев", callback_data="months_12")],
                [InlineKeyboardButton(text="Все время", callback_data="months_all")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="stats_menu")]
            ]
        )

    @staticmethod
    def managers_list_for_stats(managers: list):
        """Список менеджеров для выбора детальной статистики"""
        buttons = []
        for manager in managers:
            # Используем display_name если есть, иначе формируем с учетом custom_name
            display_name = manager.get('display_name')
            if not display_name:
                custom_name = manager.get('custom_name', '') or ''
                full_name = manager.get('full_name', '') or ''
                username = manager.get('username', '') or ''
                if custom_name:
                    display_name = custom_name
                elif full_name:
                    display_name = full_name
                elif username:
                    display_name = f"@{username}"
                else:
                    display_name = f"ID: {manager['user_id']}"

            buttons.append([
                InlineKeyboardButton(
                    text=f"{display_name} ({manager['total_keys']} ключей)",
                    callback_data=f"manager_stats_{manager['user_id']}"
                )
            ])
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="stats_menu")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def manager_stats_period_menu(manager_id: int):
        """Меню выбора периода для статистики менеджера"""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="7 дней", callback_data=f"mgr_period_{manager_id}_7")],
                [InlineKeyboardButton(text="30 дней", callback_data=f"mgr_period_{manager_id}_30")],
                [InlineKeyboardButton(text="90 дней", callback_data=f"mgr_period_{manager_id}_90")],
                [InlineKeyboardButton(text="Все время", callback_data=f"mgr_period_{manager_id}_all")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data="stats_by_managers")]
            ]
        )

    @staticmethod
    def price_edit_menu(periods: dict):
        """Меню выбора тарифа для редактирования цены"""
        buttons = []
        for key, value in periods.items():
            buttons.append([
                InlineKeyboardButton(
                    text=f"{value['name']} - {value['price']} ₽",
                    callback_data=f"edit_price_{key}"
                )
            ])
        buttons.append([InlineKeyboardButton(text="◀️ Отмена", callback_data="cancel_price_edit")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def server_selection(servers: list, prefix: str = "server_"):
        """Клавиатура для выбора сервера (только для админа)

        Args:
            servers: Список серверов
            prefix: Префикс для callback_data (по умолчанию "server_")
        """
        buttons = []
        for i, server in enumerate(servers):
            name = server.get('name', f'Server {i}')
            domain = server.get('domain', server.get('ip', ''))
            enabled = server.get('enabled', True)
            active_for_new = server.get('active_for_new', True)

            # Иконка статуса
            if not enabled:
                status = "🔴"
            elif active_for_new:
                status = "🟢"
            else:
                status = "🟡"

            button_text = f"{status} {name} ({domain})"

            buttons.append([
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"{prefix}{i}"
                )
            ])

        buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_creation")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def inbound_selection(inbounds: list):
        """Клавиатура для выбора inbound (только для админа)"""
        buttons = []
        for inbound in inbounds:
            inbound_id = inbound.get('id')
            remark = inbound.get('remark', f'Inbound {inbound_id}')
            protocol = inbound.get('protocol', 'unknown')
            port = inbound.get('port', '?')

            # Формируем красивое название с маппингом портов
            if port != 443:
                # Показываем маппинг: внутренний порт → внешний порт 443
                button_text = f"🔌 {remark} ({protocol}:{port}→443)"
            else:
                button_text = f"🔌 {remark} ({protocol}:{port})"

            buttons.append([
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"inbound_{inbound_id}"
                )
            ])

        # Добавляем кнопку отмены
        buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_creation")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def inbound_selection_from_config(inbounds: dict, server_name: str):
        """Клавиатура для выбора inbound из конфига сервера"""
        buttons = []
        for key, inbound in inbounds.items():
            inbound_id = inbound.get('id', 1)
            name_prefix = inbound.get('name_prefix', key)
            sni = inbound.get('sni', '')

            button_text = f"🔌 {name_prefix} (SNI: {sni[:20]}...)" if len(sni) > 20 else f"🔌 {name_prefix} ({sni})"

            buttons.append([
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"srv_inbound_{key}"
                )
            ])

        buttons.append([InlineKeyboardButton(text="◀️ Назад к серверам", callback_data="back_to_servers")])
        buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_creation")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    @staticmethod
    def sni_inbound_list(inbounds: list):
        """Список Reality inbound-ов для управления SNI"""
        buttons = []
        for inbound in inbounds:
            inbound_id = inbound.get('id')
            remark = inbound.get('remark', f'Inbound {inbound_id}')
            port = inbound.get('port', '?')

            button_text = f"🌐 {remark} (Port {port}→443)"

            buttons.append([
                InlineKeyboardButton(
                    text=button_text,
                    callback_data=f"sni_inbound_{inbound_id}"
                )
            ])

        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data="sni_cancel")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)
