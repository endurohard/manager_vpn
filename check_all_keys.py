"""
Проверка всех ключей в базе данных
"""
import asyncio
from bot.database import DatabaseManager
from bot.config import DATABASE_PATH


async def check_all_keys():
    """Проверить все ключи и их создателей"""
    db = DatabaseManager(DATABASE_PATH)
    await db.init_db()

    print("=" * 60)
    print("ПРОВЕРКА ВСЕХ КЛЮЧЕЙ В БАЗЕ")
    print("=" * 60)

    # Получаем все ключи
    keys = await db.get_recent_keys(limit=100)

    if not keys:
        print("\n⚠️ Нет ключей в базе")
        return

    # Получаем активных менеджеров
    managers = await db.get_all_managers()
    active_manager_ids = {m['user_id'] for m in managers}

    print(f"\n📊 Всего ключей в базе: {len(keys)}")
    print(f"👥 Активных менеджеров: {len(active_manager_ids)}")
    print(f"🆔 ID активных менеджеров: {active_manager_ids}")
    print()

    # Группируем по manager_id
    by_manager = {}
    for key in keys:
        manager_id = key['manager_id']
        if manager_id not in by_manager:
            by_manager[manager_id] = []
        by_manager[manager_id].append(key)

    # Выводим статистику
    total_revenue = 0
    for manager_id, manager_keys in by_manager.items():
        is_active = manager_id in active_manager_ids
        revenue = sum(k['price'] for k in manager_keys)
        total_revenue += revenue

        status = "✅ АКТИВНЫЙ" if is_active else "❌ НЕ АКТИВНЫЙ/УДАЛЕН"

        print(f"Manager ID: {manager_id} {status}")
        print(f"   Ключей: {len(manager_keys)}")
        print(f"   Доход: {revenue:,} ₽")

        # Показываем ключи
        for idx, key in enumerate(manager_keys[:3], 1):  # Первые 3
            print(f"   {idx}. {key['phone_number']} - {key['price']} ₽ ({key['created_at'][:10]})")
        if len(manager_keys) > 3:
            print(f"   ... и еще {len(manager_keys) - 3} ключей")
        print()

    print("=" * 60)
    print(f"💰 ИТОГО ДОХОД: {total_revenue:,} ₽")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(check_all_keys())
