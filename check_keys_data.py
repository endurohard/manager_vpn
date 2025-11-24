"""
Проверка данных ключей в базе
"""
import asyncio
from bot.database import DatabaseManager
from bot.config import DATABASE_PATH, ADMIN_ID


async def check_keys_data():
    """Проверить данные ключей"""
    db = DatabaseManager(DATABASE_PATH)
    await db.init_db()

    print("=" * 80)
    print("ПРОВЕРКА ДАННЫХ КЛЮЧЕЙ В БАЗЕ")
    print("=" * 80)

    # Получаем все ключи
    keys = await db.get_recent_keys(limit=50)

    # Группируем по менеджерам
    admin_keys = [k for k in keys if k['manager_id'] == ADMIN_ID]
    manager_keys = [k for k in keys if k['manager_id'] != ADMIN_ID]

    print(f"\n👑 КЛЮЧИ АДМИНА (всего: {len(admin_keys)}):\n")
    for idx, key in enumerate(admin_keys[:10], 1):
        print(f"{idx}. ID: {key['id']}")
        print(f"   client_email: '{key['client_email']}'")
        print(f"   phone_number: '{key['phone_number']}'")
        print(f"   period: {key['period']}")
        print(f"   created_at: {key['created_at'][:16]}")
        print()

    print("=" * 80)
    print(f"\n👥 КЛЮЧИ МЕНЕДЖЕРОВ (всего: {len(manager_keys)}):\n")
    for idx, key in enumerate(manager_keys[:10], 1):
        custom_name = key.get('custom_name', '') or ''
        full_name = key.get('full_name', '') or ''
        username = key.get('username', '') or ''

        if custom_name:
            manager_name = custom_name
        elif full_name:
            manager_name = full_name
        elif username:
            manager_name = f"@{username}"
        else:
            manager_name = f"ID: {key['manager_id']}"

        print(f"{idx}. ID: {key['id']} | Менеджер: {manager_name}")
        print(f"   client_email: '{key['client_email']}'")
        print(f"   phone_number: '{key['phone_number']}'")
        print(f"   period: {key['period']}")
        print(f"   created_at: {key['created_at'][:16]}")
        print()

    # Проверяем, есть ли ключи с текстом "Сгенерировать"
    generate_keys = [k for k in keys if 'генер' in k['phone_number'].lower() or 'генер' in k['client_email'].lower()]
    if generate_keys:
        print("=" * 80)
        print(f"\n⚠️ КЛЮЧИ С ТЕКСТОМ 'Сгенерировать' (всего: {len(generate_keys)}):\n")
        for key in generate_keys:
            print(f"ID: {key['id']} | Manager: {key['manager_id']}")
            print(f"   client_email: '{key['client_email']}'")
            print(f"   phone_number: '{key['phone_number']}'")
            print(f"   created_at: {key['created_at'][:16]}")
            print()


if __name__ == "__main__":
    asyncio.run(check_keys_data())
