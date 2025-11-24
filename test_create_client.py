"""Тест создания клиента в X-UI"""
import asyncio
import sys
import os

# Добавляем путь к модулям бота
sys.path.insert(0, '/root/manager_vpn')

from bot.api import XUIClient
from bot.config import XUI_HOST, XUI_USERNAME, XUI_PASSWORD, INBOUND_ID, DOMAIN


async def test_create_client():
    print(f"Подключение к X-UI: {XUI_HOST}")
    print(f"Пользователь: {XUI_USERNAME}")
    print(f"Inbound ID: {INBOUND_ID}")
    print(f"Домен: {DOMAIN}")
    print()

    async with XUIClient(XUI_HOST, XUI_USERNAME, XUI_PASSWORD) as xui:
        # Тест 1: Авторизация
        print("1. Попытка авторизации...")
        login_result = await xui.login()
        print(f"   Результат: {login_result}")

        if not login_result:
            print("   ❌ Ошибка авторизации")
            return

        print("   ✅ Авторизация успешна")

        # Тест 2: Получение inbound
        print("\n2. Получение информации об inbound...")
        inbound = await xui.get_inbound(INBOUND_ID)

        if not inbound:
            print(f"   ❌ Inbound {INBOUND_ID} не найден")
            return

        print(f"   ✅ Inbound найден")
        print(f"   Port: {inbound.get('port')}")
        print(f"   Protocol: {inbound.get('protocol')}")

        # Тест 3: Создание тестового клиента
        print("\n3. Создание тестового клиента...")
        test_phone = "test_user_12345"

        try:
            client_data = await xui.add_client(
                inbound_id=INBOUND_ID,
                email=test_phone,
                phone=test_phone,
                expire_days=30,
                ip_limit=2
            )

            if client_data:
                print("   ✅ Клиент создан успешно")
                print(f"   Client ID: {client_data.get('client_id')}")
                print(f"   Email: {client_data.get('email')}")
                print(f"   Phone: {client_data.get('phone')}")
                print(f"   IP Limit: {client_data.get('ip_limit')}")

                # Тест 4: Получение VLESS ссылки
                print("\n4. Получение VLESS ссылки...")
                vless_link = await xui.get_client_link(
                    inbound_id=INBOUND_ID,
                    client_email=test_phone,
                    domain=DOMAIN
                )

                if vless_link:
                    print("   ✅ VLESS ссылка сформирована")
                    print(f"   {vless_link[:100]}...")
                else:
                    print("   ❌ Не удалось получить VLESS ссылку")
            else:
                print("   ❌ Не удалось создать клиента")
                print("   client_data вернул None")

        except Exception as e:
            print(f"   ❌ Ошибка: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_create_client())
