#!/usr/bin/env python3
"""
Тест замены IP на домен в VLESS ссылках
"""
from bot.api.xui_client import XUIClient

# Тестовые VLESS ссылки с различными IP адресами
test_links = [
    "vless://550e8400-e29b-41d4-a716-446655440000@192.168.1.100:443?type=tcp&security=tls&encryption=none&sni=example.com#test_client",
    "vless://550e8400-e29b-41d4-a716-446655440000@10.0.0.50:8443?type=ws&security=tls&encryption=none&path=/websocket&host=example.com&sni=example.com#test2",
    "vless://550e8400-e29b-41d4-a716-446655440000@localhost:1020?type=tcp&security=none&encryption=none#+79991234567",
    "vless://550e8400-e29b-41d4-a716-446655440000@127.0.0.1:443?type=tcp&security=tls&encryption=none#client123",
]

domain = "raphaeilvpn.ru"

print("=" * 80)
print("ТЕСТ ЗАМЕНЫ IP НА ДОМЕН В VLESS ССЫЛКАХ")
print("=" * 80)
print(f"\nДомен для замены: {domain}\n")

for i, original_link in enumerate(test_links, 1):
    print(f"\n{i}. Оригинальная ссылка:")
    print(f"   {original_link}")

    replaced_link = XUIClient.replace_ip_with_domain(original_link, domain)

    print(f"\n   После замены:")
    print(f"   {replaced_link}")

    # Проверяем что домен действительно заменился
    if f"@{domain}:" in replaced_link:
        print(f"   ✅ Домен успешно заменен")
    else:
        print(f"   ❌ ОШИБКА: Домен не заменен!")

    print("-" * 80)

print("\n" + "=" * 80)
print("ТЕСТ ЗАВЕРШЕН")
print("=" * 80)
