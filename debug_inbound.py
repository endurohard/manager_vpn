#!/usr/bin/env python3
"""
Скрипт для отладки структуры inbound в X-UI
"""
import asyncio
import json
from bot.api.xui_client import XUIClient
from bot.config import XUI_HOST, XUI_USERNAME, XUI_PASSWORD, INBOUND_ID

async def main():
    xui = XUIClient(XUI_HOST, XUI_USERNAME, XUI_PASSWORD)

    # Логинимся
    await xui.login()
    print("✅ Успешно подключились к X-UI панели\n")

    # Получаем inbound
    inbound = await xui.get_inbound(INBOUND_ID)

    if not inbound:
        print(f"❌ Inbound {INBOUND_ID} не найден")
        return

    print(f"📋 Информация об Inbound {INBOUND_ID}:\n")
    print(f"Port: {inbound.get('port')}")
    print(f"Listen: {inbound.get('listen')}")
    print(f"Protocol: {inbound.get('protocol')}\n")

    # Парсим streamSettings
    stream_settings_raw = inbound.get('streamSettings', '{}')
    print(f"Raw streamSettings:\n{stream_settings_raw}\n")

    stream_settings = json.loads(stream_settings_raw)
    print(f"Parsed streamSettings:")
    print(json.dumps(stream_settings, indent=2, ensure_ascii=False))
    print()

    # Показываем security
    security = stream_settings.get('security', 'none')
    print(f"Security: {security}")

    if security == 'reality':
        print("\n🔐 REALITY Settings:")
        reality = stream_settings.get('realitySettings', {})
        print(json.dumps(reality, indent=2, ensure_ascii=False))

    # Показываем clients (только первых 3)
    settings = json.loads(inbound.get('settings', '{}'))
    clients = settings.get('clients', [])
    print(f"\n👥 Клиентов найдено: {len(clients)}")

    if clients:
        print("\nПервый клиент:")
        print(json.dumps(clients[0], indent=2, ensure_ascii=False))

    await xui.close()

if __name__ == "__main__":
    asyncio.run(main())
