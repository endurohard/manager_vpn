"""
Скрипт для добавления менеджера в базу данных
"""
import asyncio
from bot.database import DatabaseManager
from bot.config import DATABASE_PATH

async def add_manager():
    """Добавить менеджера с ID 7125559428"""
    db = DatabaseManager(DATABASE_PATH)
    await db.init_db()

    manager_id = 7125559428
    admin_id = 398885257

    # Проверяем, не добавлен ли уже
    if await db.is_manager(manager_id):
        print(f"Менеджер с ID {manager_id} уже добавлен в базу данных.")
    else:
        # Добавляем менеджера
        success = await db.add_manager(
            user_id=manager_id,
            username="manager",
            full_name="Manager",
            added_by=admin_id
        )

        if success:
            print(f"✅ Менеджер с ID {manager_id} успешно добавлен!")
        else:
            print(f"❌ Ошибка при добавлении менеджера")

if __name__ == "__main__":
    asyncio.run(add_manager())
