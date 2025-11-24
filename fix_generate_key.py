"""
Исправление ключа с текстом "Сгенерировать"
"""
import asyncio
import aiosqlite
from bot.config import DATABASE_PATH
from bot.utils import generate_user_id


async def fix_generate_key():
    """Исправить ключ с текстом Сгенерировать"""
    print("=" * 60)
    print("ИСПРАВЛЕНИЕ КЛЮЧА С ТЕКСТОМ 'Сгенерировать'")
    print("=" * 60)

    async with aiosqlite.connect(DATABASE_PATH) as db:
        # Находим ключи с текстом "Сгенерировать"
        cursor = await db.execute(
            "SELECT id, manager_id, client_email, phone_number FROM keys_history WHERE phone_number LIKE '%генерир%' OR client_email LIKE '%генерир%'"
        )
        keys = await cursor.fetchall()

        if not keys:
            print("\n✅ Ключей с текстом 'Сгенерировать' не найдено.")
            return

        print(f"\n⚠️ Найдено {len(keys)} ключей с проблемой:\n")

        for key in keys:
            key_id, manager_id, client_email, phone_number = key
            print(f"ID: {key_id}")
            print(f"  Manager: {manager_id}")
            print(f"  client_email: '{client_email}'")
            print(f"  phone_number: '{phone_number}'")

            # Генерируем новый ID
            new_id = generate_user_id()
            print(f"  → Новый ID: '{new_id}'")

            # Обновляем запись
            await db.execute(
                "UPDATE keys_history SET client_email = ?, phone_number = ? WHERE id = ?",
                (new_id, new_id, key_id)
            )
            print(f"  ✅ Обновлено\n")

        await db.commit()
        print("=" * 60)
        print(f"✅ Обновлено {len(keys)} записей")
        print("=" * 60)


if __name__ == "__main__":
    asyncio.run(fix_generate_key())
