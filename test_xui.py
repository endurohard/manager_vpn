"""Тест подключения к X-UI"""
import asyncio
import aiohttp
import ssl

async def test_connection():
    # Настройки
    host = "https://localhost:1020/Raphael"
    username = "itadmin"
    password = "20TQNF_Srld"

    print(f"Тестирование подключения к: {host}")
    print(f"Пользователь: {username}")

    # Создаем SSL context который игнорирует ошибки сертификата
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    connector = aiohttp.TCPConnector(ssl=ssl_context)

    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            # Тест 1: Проверка доступности
            print("\n1. Проверка доступности панели...")
            url = f"{host}/login"
            try:
                async with session.get(url) as response:
                    print(f"   GET {url}")
                    print(f"   Status: {response.status}")
                    if response.status == 200:
                        print("   ✅ Панель доступна")
                    else:
                        print(f"   ❌ Ошибка: {response.status}")
            except Exception as e:
                print(f"   ❌ Ошибка подключения: {e}")

            # Тест 2: Авторизация
            print("\n2. Попытка авторизации...")
            payload = {
                "username": username,
                "password": password
            }

            try:
                async with session.post(url, json=payload) as response:
                    print(f"   POST {url}")
                    print(f"   Status: {response.status}")
                    data = await response.json()
                    print(f"   Response: {data}")

                    if response.status == 200 and data.get('success'):
                        print("   ✅ Авторизация успешна")

                        # Тест 3: Получение inbound
                        print("\n3. Получение информации об inbound 12...")
                        inbound_url = f"{host}/panel/api/inbounds/get/12"
                        async with session.get(inbound_url) as resp:
                            print(f"   GET {inbound_url}")
                            print(f"   Status: {resp.status}")
                            if resp.status == 200:
                                inbound_data = await resp.json()
                                if inbound_data.get('success'):
                                    print("   ✅ Inbound найден")
                                    obj = inbound_data.get('obj', {})
                                    print(f"   Port: {obj.get('port')}")
                                    print(f"   Protocol: {obj.get('protocol')}")
                                else:
                                    print(f"   ❌ {inbound_data}")
                            else:
                                print(f"   ❌ Ошибка: {resp.status}")
                    else:
                        print(f"   ❌ Ошибка авторизации")

            except Exception as e:
                print(f"   ❌ Ошибка: {e}")

    except Exception as e:
        print(f"❌ Общая ошибка: {e}")

if __name__ == "__main__":
    asyncio.run(test_connection())
