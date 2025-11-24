#!/usr/bin/env python3
"""
Тест замены IP на домен в VLESS ссылке с сохранением всех параметров
"""
import re

def replace_ip_with_domain(vless_link: str, domain: str) -> str:
    """
    Заменить IP адрес на домен в VLESS ссылке, сохраняя все параметры
    """
    # Паттерн для поиска vless://uuid@IP:PORT (с сохранением всего что после)
    pattern = r'(vless://[^@]+@)([^:]+)(:.+)'
    replacement = r'\1' + domain + r'\3'
    return re.sub(pattern, replacement, vless_link)


# Тестовые данные из реальной ситуации пользователя
server_link = "vless://405f62e5-990f-4b08-8d63-77028c1ef128@185.128.104.219:443/?type=tcp&security=reality&pbk=Nf4RWEpUDg5CA3KoCyMK3YGOXClt16zNjs3HN6QBAhQ&fp=random&sni=mirror.yandex.ru&sid=0e38db&spx=%2F#VPNPULSE-%2B79298666675"
domain = "raphaelvpn.ru"
expected = "vless://405f62e5-990f-4b08-8d63-77028c1ef128@raphaelvpn.ru:443/?type=tcp&security=reality&pbk=Nf4RWEpUDg5CA3KoCyMK3YGOXClt16zNjs3HN6QBAhQ&fp=random&sni=mirror.yandex.ru&sid=0e38db&spx=%2F#VPNPULSE-%2B79298666675"

print("=" * 80)
print("ТЕСТ ЗАМЕНЫ IP НА ДОМЕН В VLESS ССЫЛКЕ")
print("=" * 80)
print()

print("Исходная ссылка с сервера:")
print(server_link)
print()

print("Домен для замены:")
print(domain)
print()

result = replace_ip_with_domain(server_link, domain)

print("Результат замены:")
print(result)
print()

print("Ожидаемый результат:")
print(expected)
print()

if result == expected:
    print("✅ ТЕСТ ПРОЙДЕН! Все параметры сохранены, IP заменен на домен.")
else:
    print("❌ ТЕСТ НЕ ПРОЙДЕН!")
    print()
    print("Различия:")
    if result != expected:
        print(f"  Результат:  {result}")
        print(f"  Ожидалось:  {expected}")

print()
print("=" * 80)
print("ПРОВЕРКА КОМПОНЕНТОВ:")
print("=" * 80)

# Разбираем ссылку для детального анализа
import urllib.parse

def analyze_vless_link(link, label):
    """Анализ компонентов VLESS ссылки"""
    print(f"\n{label}:")
    # Извлекаем части вручную
    if '@' in link:
        uuid_part = link.split('@')[0].replace('vless://', '')
        rest = link.split('@')[1]

        if '?' in rest or '#' in rest:
            # Есть параметры
            host_port = rest.split('?')[0].split('#')[0]
            host = host_port.split(':')[0]
            port = host_port.split(':')[1] if ':' in host_port else 'N/A'

            print(f"  UUID: {uuid_part}")
            print(f"  Host: {host}")
            print(f"  Port: {port}")

            # Параметры
            if '?' in rest:
                params_part = rest.split('?')[1].split('#')[0]
                params = params_part.split('&')
                print(f"  Параметры:")
                for param in params:
                    print(f"    - {param}")

            # Fragment (после #)
            if '#' in rest:
                fragment = rest.split('#')[1]
                print(f"  Fragment: #{fragment}")

analyze_vless_link(server_link, "Исходная ссылка")
analyze_vless_link(result, "Результат замены")
print()
