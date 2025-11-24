"""
Валидаторы данных
"""
import re
import random
import unicodedata


def clean_phone(phone: str) -> str:
    """
    Очистка номера телефона от всех лишних символов

    Удаляет:
    - Пробелы (обычные, неразрывные, табуляции)
    - Дефисы (обычные, длинные, неразрывные)
    - Скобки
    - Точки
    - Невидимые Unicode символы

    :param phone: Номер телефона в любом формате
    :return: Очищенный номер (только цифры и +)
    """
    # Сначала нормализуем Unicode (преобразуем специальные символы)
    phone = unicodedata.normalize('NFKD', phone)

    # Удаляем все невидимые управляющие символы
    phone = ''.join(char for char in phone if unicodedata.category(char)[0] != 'C')

    # Удаляем все символы кроме цифр и +
    # Это включает пробелы, дефисы, скобки, точки и любые другие символы
    phone = re.sub(r'[^\d+]', '', phone)

    # Если остался только + без цифр, возвращаем пустую строку
    if phone == '+':
        return ''

    return phone


def validate_phone(phone: str) -> bool:
    """
    Проверка корректности номера телефона

    :param phone: Номер телефона
    :return: True если корректный
    """
    # Очищаем номер от всех лишних символов
    phone = clean_phone(phone)

    if not phone:
        return False

    # Проверяем формат
    if phone.startswith('+'):
        return len(phone) >= 11 and len(phone) <= 15
    elif phone.startswith('8') or phone.startswith('7'):
        return len(phone) >= 10 and len(phone) <= 11
    else:
        return len(phone) >= 10 and len(phone) <= 15


def format_phone(phone: str) -> str:
    """
    Форматирование номера телефона

    Примеры входных данных:
    - "+7 929 866-66-75"
    - "‪+7 929 866‑66‑75‬"
    - "8 (929) 866 66 75"
    - "79298666675"

    Все преобразуются в формат: +79298666675

    :param phone: Номер телефона в любом формате
    :return: Отформатированный номер +XXXXXXXXXXX
    """
    # Очищаем номер от всех лишних символов
    phone = clean_phone(phone)

    if not phone:
        return phone

    # Если номер начинается с 8, заменяем на +7
    if phone.startswith('8') and len(phone) == 11:
        phone = '+7' + phone[1:]
    # Если номер начинается с 7 без +, добавляем +
    elif phone.startswith('7') and not phone.startswith('+') and len(phone) == 11:
        phone = '+' + phone
    # Если номер не начинается с +, добавляем +
    elif not phone.startswith('+'):
        # Проверяем, не является ли это международным номером
        if len(phone) >= 10:
            phone = '+' + phone

    return phone


def generate_phone() -> str:
    """
    Генерация случайного номера телефона

    :return: Сгенерированный номер в формате +7XXXXXXXXXX
    """
    # Генерируем российский номер
    prefix = '+7'
    # Коды операторов (первые 3 цифры после +7)
    operators = ['900', '901', '902', '903', '904', '905', '906', '908', '909',
                 '910', '911', '912', '913', '914', '915', '916', '917', '918',
                 '919', '920', '921', '922', '923', '924', '925', '926', '927',
                 '928', '929', '930', '931', '932', '933', '934', '936', '937',
                 '938', '939', '950', '951', '952', '953', '958', '960', '961',
                 '962', '963', '964', '965', '966', '967', '968', '969', '977',
                 '978', '980', '981', '982', '983', '984', '985', '986', '987',
                 '988', '989', '991', '992', '993', '994', '995', '996', '997',
                 '998', '999']

    operator = random.choice(operators)
    number = ''.join([str(random.randint(0, 9)) for _ in range(7)])

    return f"{prefix}{operator}{number}"


def generate_user_id() -> str:
    """
    Генерация случайного ID пользователя (без номера телефона)

    :return: Сгенерированный ID в формате user_XXXXXXXXXX
    """
    import string
    # Генерируем случайную строку из букв и цифр
    characters = string.ascii_lowercase + string.digits
    random_string = ''.join(random.choice(characters) for _ in range(10))
    return f"user_{random_string}"
