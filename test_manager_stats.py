"""
Тестирование новой функции статистики менеджеров
"""
import asyncio
from bot.database import DatabaseManager
from bot.config import DATABASE_PATH


async def test_manager_stats():
    """Тестирование статистики менеджеров"""
    db = DatabaseManager(DATABASE_PATH)
    await db.init_db()

    print("=" * 60)
    print("ТЕСТИРОВАНИЕ СТАТИСТИКИ МЕНЕДЖЕРОВ")
    print("=" * 60)

    # Получаем детальную статистику
    stats = await db.get_managers_detailed_stats()

    if not stats:
        print("\n⚠️ Нет данных для отображения")
        return

    print(f"\n📊 Найдено менеджеров: {len(stats)}\n")

    for idx, stat in enumerate(stats, 1):
        # Получаем имя для отображения
        custom_name = stat.get('custom_name', '') or ''
        full_name = stat.get('full_name', '') or ''
        username = stat.get('username', '') or ''

        if custom_name:
            display_name = custom_name
        elif full_name:
            display_name = full_name
        elif username:
            display_name = f"@{username}"
        else:
            display_name = f"ID: {stat['user_id']}"

        print(f"{idx}. {display_name}")
        print(f"   User ID: {stat['user_id']}")
        print(f"   🔑 Всего ключей: {stat['total_keys'] or 0}")
        print(f"   📅 Сегодня: {stat['today_keys'] or 0}")
        print(f"   📆 За месяц: {stat['month_keys'] or 0}")
        print(f"   💰 Всего доход: {stat['total_revenue'] or 0:,} ₽")
        print(f"   💵 Доход сегодня: {stat['today_revenue'] or 0:,} ₽")
        print(f"   💸 Доход за месяц: {stat['month_revenue'] or 0:,} ₽")
        print()

    # Общая статистика
    print("=" * 60)
    total_keys = sum(stat['total_keys'] or 0 for stat in stats)
    total_revenue = sum(stat['total_revenue'] or 0 for stat in stats)
    print(f"🔑 Всего ключей: {total_keys}")
    print(f"💰 Общий доход: {total_revenue:,} ₽")
    print("=" * 60)

    # Проверяем общую статистику доходов
    revenue_stats = await db.get_revenue_stats()
    print("\n📊 Общая статистика доходов:")
    print(f"   💵 Всего: {revenue_stats['total']:,} ₽")
    print(f"   📅 Сегодня: {revenue_stats['today']:,} ₽")
    print(f"   📆 За месяц: {revenue_stats['month']:,} ₽")


if __name__ == "__main__":
    asyncio.run(test_manager_stats())
