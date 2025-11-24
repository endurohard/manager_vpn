#!/usr/bin/env python3
"""
Тест генерации VLESS ссылки с REALITY параметрами
"""
import asyncio
from bot.api.xui_client import XUIClient
from bot.config import XUI_HOST, XUI_USERNAME, XUI_PASSWORD, INBOUND_ID, DOMAIN

async def main():
    xui = XUIClient(XUI_HOST, XUI_USERNAME, XUI_PASSWORD)

    # Логинимся
    await xui.login()
    print("✅ Подключение к X-UI панели\n")

    # Получаем inbound
    inbound = await xui.get_inbound(INBOUND_ID)
    if not inbound:
        print(f"❌ Inbound {INBOUND_ID} не найден")
        return

    # Получаем клиентов
    import json
    settings = json.loads(inbound.get('settings', '{}'))
    clients = settings.get('clients', [])

    if not clients:
        print("❌ Нет клиентов в inbound")
        return

    # Берем первого клиента
    first_client = clients[0]
    client_email = first_client.get('email')

    print(f"📋 Тестирование с клиентом: {client_email}\n")

    # Получаем ссылку с IP
    print("=" * 80)
    print("1. ССЫЛКА С IP СЕРВЕРА (для внутренних нужд):")
    print("=" * 80)
    link_with_ip = await xui.get_client_link(
        inbound_id=INBOUND_ID,
        client_email=client_email,
        use_domain=None  # Используем IP
    )
    print(link_with_ip)
    print()

    # Получаем ссылку с доменом
    print("=" * 80)
    print("2. ССЫЛКА С ДОМЕНОМ (для выдачи пользователю):")
    print("=" * 80)
    link_with_domain = await xui.get_client_link(
        inbound_id=INBOUND_ID,
        client_email=client_email,
        use_domain=DOMAIN
    )
    print(link_with_domain)
    print()

    # Проверяем что заменили только IP на домен
    print("=" * 80)
    print("3. ПРОВЕРКА ЗАМЕНЫ IP НА ДОМЕН:")
    print("=" * 80)
    replaced_link = XUIClient.replace_ip_with_domain(link_with_ip, DOMAIN)
    print(f"Заменено через replace_ip_with_domain():")
    print(replaced_link)
    print()

    if link_with_domain == replaced_link:
        print("✅ Ссылки совпадают! Замена работает корректно.")
    else:
        print("⚠️ Ссылки НЕ совпадают!")
        print(f"Разница:")
        print(f"  С доменом:   {link_with_domain}")
        print(f"  Заменённая:  {replaced_link}")

    print()
    print("=" * 80)
    print("4. ПРОВЕРКА ПАРАМЕТРОВ REALITY:")
    print("=" * 80)

    # Проверяем наличие всех нужных параметров
    required_params = ['pbk=', 'fp=', 'sni=', 'sid=', 'spx=']
    missing_params = []

    for param in required_params:
        if param in link_with_ip:
            print(f"✅ {param} присутствует")
        else:
            print(f"❌ {param} ОТСУТСТВУЕТ!")
            missing_params.append(param)

    if not missing_params:
        print("\n✅ Все параметры REALITY присутствуют в ссылке!")
    else:
        print(f"\n❌ Отсутствуют параметры: {', '.join(missing_params)}")

if __name__ == "__main__":
    asyncio.run(main())
