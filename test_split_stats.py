"""
Тестирование раздельной статистики админа и менеджеров
"""
import asyncio
from bot.database import DatabaseManager
from bot.config import DATABASE_PATH, ADMIN_ID


async def test_split_stats():
    """Тестирование раздельной статистики"""
    db = DatabaseManager(DATABASE_PATH)
    await db.init_db()

    print("=" * 60)
    print("ТЕСТИРОВАНИЕ РАЗДЕЛЬНОЙ СТАТИСТИКИ")
    print("=" * 60)
    print(f"\nAdmin ID: {ADMIN_ID}\n")

    # Статистика админа
    admin_stats = await db.get_admin_revenue_stats(ADMIN_ID)
    print("👑 ДОХОДЫ АДМИНА:")
    print(f"   💵 Всего: {admin_stats['total']:,} ₽ ({admin_stats['total_keys']} ключей)")
    print(f"   📅 Сегодня: {admin_stats['today']:,} ₽ ({admin_stats['today_keys']} ключей)")
    print(f"   📆 За месяц: {admin_stats['month']:,} ₽ ({admin_stats['month_keys']} ключей)")
    print()

    # Статистика менеджеров (без админа)
    managers_revenue = await db.get_managers_only_revenue_stats(exclude_admin_id=ADMIN_ID)
    print("👥 ДОХОДЫ МЕНЕДЖЕРОВ (без админа):")
    print(f"   💵 Всего: {managers_revenue['total']:,} ₽")
    print(f"   📅 Сегодня: {managers_revenue['today']:,} ₽")
    print(f"   📆 За месяц: {managers_revenue['month']:,} ₽")
    print()

    # Итого
    total_all = admin_stats['total'] + managers_revenue['total']
    total_today = admin_stats['today'] + managers_revenue['today']
    total_month = admin_stats['month'] + managers_revenue['month']

    print("=" * 60)
    print("💰 ИТОГО ДОХОДЫ:")
    print(f"   💵 Всего заработано: {total_all:,} ₽")
    print(f"   📅 За сегодня: {total_today:,} ₽")
    print(f"   📆 За месяц: {total_month:,} ₽")
    print("=" * 60)

    # Детализация по менеджерам
    stats = await db.get_managers_detailed_stats()
    if stats:
        print("\n👥 ДЕТАЛИЗАЦИЯ ПО МЕНЕДЖЕРАМ:\n")
        for idx, stat in enumerate(stats, 1):
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
            print(f"   🔑 Ключей: {stat['total_keys']} (сегодня: {stat['today_keys']}, месяц: {stat['month_keys']})")
            print(f"   💰 Доход: {stat['total_revenue']:,} ₽ (сегодня: {stat['today_revenue']:,} ₽, месяц: {stat['month_revenue']:,} ₽)")
            print()

    # Проверка
    general_stats = await db.get_revenue_stats()
    print("=" * 60)
    print("✅ ПРОВЕРКА (старый метод get_revenue_stats):")
    print(f"   💵 Всего: {general_stats['total']:,} ₽")
    print(f"   📅 Сегодня: {general_stats['today']:,} ₽")
    print(f"   📆 За месяц: {general_stats['month']:,} ₽")
    print()

    if general_stats['total'] == total_all:
        print("✅ Суммы совпадают! Статистика корректна.")
    else:
        print("❌ ОШИБКА! Суммы не совпадают!")
        print(f"   Новый расчет: {total_all:,} ₽")
        print(f"   Старый метод: {general_stats['total']:,} ₽")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_split_stats())
