"""
Обработчики для расширенных функций:
- Промокоды
- Аналитика
- Группы клиентов
- Рефералы
"""
import logging
from datetime import datetime, timedelta
from functools import wraps
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from bot.config import ADMIN_ID
from bot.database import (
    ClientManager,
    PromoManager,
    ReferralManager,
    AnalyticsManager,
    AuditManager,
    AuditAction
)

logger = logging.getLogger(__name__)

router = Router()


# ==================== FSM States ====================

class PromoStates(StatesGroup):
    """Состояния для управления промокодами"""
    waiting_for_code = State()
    waiting_for_discount_type = State()
    waiting_for_discount_value = State()
    waiting_for_max_uses = State()
    waiting_for_valid_days = State()
    confirming = State()


class AnalyticsStates(StatesGroup):
    """Состояния для аналитики"""
    waiting_for_period = State()


class ClientSearchStates(StatesGroup):
    """Состояния для расширенного поиска"""
    waiting_for_query = State()


# ==================== Decorators ====================

def admin_only(func):
    """Декоратор для проверки прав администратора"""
    @wraps(func)
    async def wrapper(update, *args, **kwargs):
        user_id = None
        if isinstance(update, Message):
            user_id = update.from_user.id
        elif isinstance(update, CallbackQuery):
            user_id = update.from_user.id

        if user_id != ADMIN_ID:
            if isinstance(update, Message):
                await update.answer("У вас нет доступа к этой функции.")
            elif isinstance(update, CallbackQuery):
                await update.answer("Нет доступа", show_alert=True)
            return
        return await func(update, *args, **kwargs)
    return wrapper


# ==================== Промокоды ====================

@router.message(F.text == "🎟 Промокоды")
@admin_only
async def show_promo_menu(message: Message, db, **kwargs):
    """Показать меню промокодов"""
    promo_manager = PromoManager(db.db_path)
    promos = await promo_manager.get_active_promos()

    text = "🎟 <b>Управление промокодами</b>\n\n"

    if promos:
        text += "Активные промокоды:\n"
        for p in promos[:10]:
            uses = f"{p['current_uses']}/{p['max_uses']}" if p['max_uses'] > 0 else f"{p['current_uses']}/∞"
            text += f"• <code>{p['code']}</code> - {p['discount_value']}{'%' if p['discount_type'] == 'percent' else '₽'} ({uses})\n"
    else:
        text += "Нет активных промокодов.\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать промокод", callback_data="promo_create")],
        [InlineKeyboardButton(text="📊 Статистика промокодов", callback_data="promo_stats")],
        [InlineKeyboardButton(text="🗑 Деактивировать", callback_data="promo_deactivate")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "promo_create")
@admin_only
async def start_create_promo(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Начало создания промокода"""
    await callback.answer()
    await state.set_state(PromoStates.waiting_for_code)
    await callback.message.edit_text(
        "🎟 <b>Создание промокода</b>\n\n"
        "Введите код промокода (буквы и цифры, без пробелов):",
        parse_mode="HTML"
    )


@router.message(PromoStates.waiting_for_code)
async def process_promo_code(message: Message, state: FSMContext, **kwargs):
    """Обработка кода промокода"""
    code = message.text.strip().upper()

    if not code.isalnum() or len(code) < 3 or len(code) > 20:
        await message.answer(
            "Код должен содержать только буквы и цифры (3-20 символов).\n"
            "Попробуйте снова:"
        )
        return

    await state.update_data(code=code)
    await state.set_state(PromoStates.waiting_for_discount_type)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="% Процент скидки", callback_data="dtype_percent")],
        [InlineKeyboardButton(text="₽ Фиксированная сумма", callback_data="dtype_fixed")],
        [InlineKeyboardButton(text="📅 Бонусные дни", callback_data="dtype_days")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="promo_cancel")]
    ])

    await message.answer(
        f"Код: <code>{code}</code>\n\n"
        "Выберите тип скидки:",
        reply_markup=keyboard,
        parse_mode="HTML"
    )


@router.callback_query(F.data.startswith("dtype_"))
async def process_discount_type(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Обработка типа скидки"""
    dtype = callback.data.split("_")[1]
    await callback.answer()
    await state.update_data(discount_type=dtype)
    await state.set_state(PromoStates.waiting_for_discount_value)

    type_names = {
        "percent": "процент скидки (1-100)",
        "fixed": "сумму скидки в рублях",
        "days": "количество бонусных дней"
    }

    await callback.message.edit_text(
        f"Тип скидки: {dtype}\n\n"
        f"Введите {type_names.get(dtype, 'значение')}:"
    )


@router.message(PromoStates.waiting_for_discount_value)
async def process_discount_value(message: Message, state: FSMContext, **kwargs):
    """Обработка значения скидки"""
    try:
        value = int(message.text.strip())
        if value <= 0:
            raise ValueError("Значение должно быть положительным")

        data = await state.get_data()
        if data.get('discount_type') == 'percent' and value > 100:
            await message.answer("Процент скидки не может быть больше 100. Попробуйте снова:")
            return

        await state.update_data(discount_value=value)
        await state.set_state(PromoStates.waiting_for_max_uses)

        await message.answer(
            f"Значение: {value}\n\n"
            "Введите максимальное количество использований (0 = без ограничений):"
        )

    except ValueError:
        await message.answer("Введите целое положительное число:")


@router.message(PromoStates.waiting_for_max_uses)
async def process_max_uses(message: Message, state: FSMContext, **kwargs):
    """Обработка макс. использований"""
    try:
        max_uses = int(message.text.strip())
        if max_uses < 0:
            raise ValueError("Значение не может быть отрицательным")

        await state.update_data(max_uses=max_uses)
        await state.set_state(PromoStates.waiting_for_valid_days)

        await message.answer(
            f"Макс. использований: {max_uses if max_uses > 0 else 'без ограничений'}\n\n"
            "Введите срок действия в днях (0 = бессрочно):"
        )

    except ValueError:
        await message.answer("Введите целое неотрицательное число:")


@router.message(PromoStates.waiting_for_valid_days)
async def process_valid_days(message: Message, state: FSMContext, db, **kwargs):
    """Обработка срока действия и создание промокода"""
    try:
        valid_days = int(message.text.strip())
        if valid_days < 0:
            raise ValueError("Значение не может быть отрицательным")

        data = await state.get_data()
        await state.clear()

        # Создаём промокод
        promo_manager = PromoManager(db.db_path)

        valid_until = None
        if valid_days > 0:
            valid_until = datetime.now() + timedelta(days=valid_days)

        promo_result = await promo_manager.create_promo(
            code=data['code'],
            discount_type=data['discount_type'],
            discount_value=data['discount_value'],
            max_uses=data['max_uses'],
            valid_until=valid_until,
            created_by=message.from_user.id
        )

        promo_id = promo_result['id'] if promo_result else None

        # Логируем
        audit_manager = AuditManager(db.db_path)
        await audit_manager.log_promo_action(
            manager_id=message.from_user.id,
            action=AuditAction.PROMO_CREATED,
            promo_id=promo_id,
            promo_code=data['code'],
            details=f"Скидка: {data['discount_value']}, Тип: {data['discount_type']}"
        )

        type_symbols = {"percent": "%", "fixed": "₽", "days": " дн."}
        symbol = type_symbols.get(data['discount_type'], '')

        await message.answer(
            f"✅ Промокод создан!\n\n"
            f"Код: <code>{data['code']}</code>\n"
            f"Скидка: {data['discount_value']}{symbol}\n"
            f"Макс. использований: {data['max_uses'] if data['max_uses'] > 0 else 'без ограничений'}\n"
            f"Действует: {valid_days if valid_days > 0 else 'бессрочно'} дн.",
            parse_mode="HTML"
        )

    except ValueError:
        await message.answer("Введите целое неотрицательное число:")


@router.callback_query(F.data == "promo_stats")
@admin_only
async def show_promo_stats(callback: CallbackQuery, db, **kwargs):
    """Показать статистику промокодов"""
    await callback.answer()

    promo_manager = PromoManager(db.db_path)
    promos = await promo_manager.get_all_promos()

    total_uses = 0
    total_discount = 0

    text = "📊 <b>Статистика промокодов</b>\n\n"

    for p in promos[:15]:
        stats = await promo_manager.get_promo_stats(p['id'])
        total_uses += stats.get('total_uses', 0)
        total_discount += stats.get('total_discount', 0)

        status = "✅" if p['is_active'] else "❌"
        text += f"{status} <code>{p['code']}</code>: {stats.get('total_uses', 0)} исп., {stats.get('total_discount', 0)}₽ скидок\n"

    text += f"\n<b>Всего:</b> {total_uses} использований, {total_discount}₽ скидок"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="promo_back")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "promo_cancel")
async def cancel_promo_creation(callback: CallbackQuery, state: FSMContext, **kwargs):
    """Отмена создания промокода"""
    await callback.answer("Отменено")
    await state.clear()
    await callback.message.edit_text("Создание промокода отменено.")


# ==================== Аналитика ====================

@router.message(F.text == "📊 Аналитика")
@admin_only
async def show_analytics_menu(message: Message, db, **kwargs):
    """Показать меню аналитики"""
    analytics = AnalyticsManager(db.db_path)
    dashboard = await analytics.get_dashboard_stats()

    clients = dashboard.get('clients', {})
    revenue = dashboard.get('revenue', {})
    today = dashboard.get('today', {})

    text = (
        "📊 <b>Аналитика</b>\n\n"
        f"👥 <b>Клиенты:</b>\n"
        f"  • Всего: {clients.get('total', 0)}\n"
        f"  • Активных: {clients.get('active', 0)}\n"
        f"  • Истекающих (7 дн.): {clients.get('expiring_7d', 0)}\n"
        f"  • Истекших: {clients.get('expired', 0)}\n\n"
        f"💰 <b>Выручка:</b>\n"
        f"  • Сегодня: {revenue.get('today', 0)}₽\n"
        f"  • За месяц: {revenue.get('month', 0)}₽\n\n"
        f"📈 <b>Сегодня:</b>\n"
        f"  • Ключей создано: {today.get('keys_created', 0)}\n"
        f"  • Продлений: {today.get('extensions', 0)}\n"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📈 Отчёт по выручке", callback_data="analytics_revenue")],
        [InlineKeyboardButton(text="👔 Статистика менеджеров", callback_data="analytics_managers")],
        [InlineKeyboardButton(text="📅 Популярность периодов", callback_data="analytics_periods")],
        [InlineKeyboardButton(text="📉 Анализ оттока", callback_data="analytics_churn")],
        [InlineKeyboardButton(text="💎 LTV клиентов", callback_data="analytics_ltv")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "analytics_revenue")
@admin_only
async def show_revenue_report(callback: CallbackQuery, db, **kwargs):
    """Отчёт по выручке за 30 дней"""
    await callback.answer()

    analytics = AnalyticsManager(db.db_path)
    from_date = datetime.now() - timedelta(days=30)
    to_date = datetime.now()

    report = await analytics.get_revenue_report(from_date, to_date, 'week')

    text = "📈 <b>Выручка по неделям (30 дней)</b>\n\n"

    total_revenue = 0
    for r in report:
        total_revenue += r.get('revenue', 0) or 0
        text += f"• {r['period']}: {r.get('revenue', 0) or 0}₽ ({r.get('transactions', 0)} транз.)\n"

    text += f"\n<b>Итого:</b> {total_revenue}₽"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="analytics_back")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "analytics_managers")
@admin_only
async def show_manager_stats(callback: CallbackQuery, db, **kwargs):
    """Статистика менеджеров"""
    await callback.answer()

    analytics = AnalyticsManager(db.db_path)
    from_date = datetime.now() - timedelta(days=30)

    stats = await analytics.get_manager_stats(from_date=from_date)

    text = "👔 <b>Статистика менеджеров (30 дней)</b>\n\n"

    for s in stats[:10]:
        text += (
            f"• ID {s['manager_id']}: "
            f"{s.get('keys_created', 0)} созд., "
            f"{s.get('extensions', 0)} продл., "
            f"{s.get('total_revenue', 0) or 0}₽\n"
        )

    if not stats:
        text += "Нет данных за период."

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="analytics_back")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "analytics_periods")
@admin_only
async def show_period_popularity(callback: CallbackQuery, db, **kwargs):
    """Популярность периодов подписки"""
    await callback.answer()

    analytics = AnalyticsManager(db.db_path)
    data = await analytics.get_period_popularity()

    text = "📅 <b>Популярность периодов</b>\n\n"

    periods = data.get('periods', [])
    for p in periods:
        bar = "█" * int(p.get('percentage', 0) / 10) + "░" * (10 - int(p.get('percentage', 0) / 10))
        text += f"• {p['period'] or 'N/A'}: {p['count']} ({p.get('percentage', 0)}%) {bar}\n"

    text += f"\n<b>Всего заказов:</b> {data.get('total_orders', 0)}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="analytics_back")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "analytics_churn")
@admin_only
async def show_churn_analysis(callback: CallbackQuery, db, **kwargs):
    """Анализ оттока клиентов"""
    await callback.answer()

    analytics = AnalyticsManager(db.db_path)
    data = await analytics.get_churn_analysis(months=6)

    text = "📉 <b>Анализ оттока (6 месяцев)</b>\n\n"

    for m in data.get('months', []):
        text += (
            f"• {m['month']}: "
            f"+{m['new_clients']} новых, "
            f"-{m['churned']} ушло, "
            f"отток {m['churn_rate']}%\n"
        )

    text += f"\n<b>Средний отток:</b> {data.get('avg_churn_rate', 0)}%"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="analytics_back")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "analytics_ltv")
@admin_only
async def show_ltv_stats(callback: CallbackQuery, db, **kwargs):
    """LTV клиентов"""
    await callback.answer()

    analytics = AnalyticsManager(db.db_path)
    data = await analytics.get_client_lifetime_value()

    segments = data.get('ltv_segments', {})

    text = (
        "💎 <b>LTV (Lifetime Value) клиентов</b>\n\n"
        f"<b>Средний LTV:</b> {data.get('avg_ltv', 0)}₽\n"
        f"<b>Общая выручка:</b> {data.get('total_revenue', 0)}₽\n"
        f"<b>Всего клиентов:</b> {data.get('total_clients', 0)}\n"
        f"<b>Средн. транзакций:</b> {data.get('avg_transactions', 0)}\n\n"
        f"<b>Сегменты по LTV:</b>\n"
        f"• 0₽: {segments.get('0', 0)} клиентов\n"
        f"• 1-500₽: {segments.get('1-500', 0)} клиентов\n"
        f"• 500-1000₽: {segments.get('500-1000', 0)} клиентов\n"
        f"• 1000-5000₽: {segments.get('1000-5000', 0)} клиентов\n"
        f"• 5000+₽: {segments.get('5000+', 0)} клиентов\n"
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="analytics_back")]
    ])

    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")


@router.callback_query(F.data == "analytics_back")
async def analytics_back(callback: CallbackQuery, db, **kwargs):
    """Вернуться в меню аналитики"""
    await callback.answer()
    # Вызываем show_analytics_menu через сообщение-обёртку
    await callback.message.delete()


# ==================== Рефералы ====================

@router.message(F.text == "👥 Рефералы")
@admin_only
async def show_referrals_menu(message: Message, db, **kwargs):
    """Показать меню рефералов"""
    referral_manager = ReferralManager(db.db_path)
    top_referrers = await referral_manager.get_top_referrers(limit=5)

    text = "👥 <b>Реферальная система</b>\n\n"

    if top_referrers:
        text += "<b>Топ рефереров:</b>\n"
        for i, r in enumerate(top_referrers, 1):
            text += f"{i}. ID {r['referrer_id']}: {r['referred_count']} приглашений\n"
    else:
        text += "Пока нет рефералов.\n"

    # Общая статистика
    stats = await referral_manager.get_referral_stats(0)  # Все рефералы
    text += f"\n<b>Всего приглашений:</b> {stats.get('total_referrals', 0)}"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="referral_stats")],
        [InlineKeyboardButton(text="⚙️ Настройки бонусов", callback_data="referral_settings")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back")]
    ])

    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")


# ==================== Навигация ====================

@router.callback_query(F.data == "admin_back")
async def admin_back(callback: CallbackQuery, **kwargs):
    """Вернуться в админ меню"""
    await callback.answer()
    await callback.message.delete()


@router.callback_query(F.data == "promo_back")
async def promo_back(callback: CallbackQuery, **kwargs):
    """Вернуться в меню промокодов"""
    await callback.answer()
    await callback.message.delete()
