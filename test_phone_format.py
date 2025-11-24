#!/usr/bin/env python3
"""
Тест форматирования номеров телефонов
"""
from bot.utils.validators import format_phone, clean_phone

# Тестовые номера в различных форматах
test_numbers = [
    "+7 929 866-66-75",
    "‪+7 929 866‑66‑75‬",  # С невидимыми Unicode символами
    "8 (929) 866 66 75",
    "79298666675",
    "89298666675",
    "+7-929-866-66-75",
    "8(929)8666675",
    "+7 (929) 866-66-75",
    "7 929 866 66 75",
    "+7  929  866  66  75",
]

print("=" * 60)
print("ТЕСТ ФОРМАТИРОВАНИЯ НОМЕРОВ ТЕЛЕФОНОВ")
print("=" * 60)

for i, number in enumerate(test_numbers, 1):
    print(f"\n{i}. Исходный номер: '{number}'")
    print(f"   Представление: {repr(number)}")

    cleaned = clean_phone(number)
    print(f"   После очистки: '{cleaned}'")

    formatted = format_phone(number)
    print(f"   После форматирования: '{formatted}'")

    # Проверяем, что результат содержит только цифры и +
    if formatted:
        is_valid = all(c.isdigit() or c == '+' for c in formatted)
        print(f"   Валидность: {'✅' if is_valid else '❌'}")

print("\n" + "=" * 60)
print("ТЕСТ ЗАВЕРШЕН")
print("=" * 60)
