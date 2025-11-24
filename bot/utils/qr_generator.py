"""
Генератор QR кодов для VLESS ключей
"""
import qrcode
from io import BytesIO


def generate_qr_code(vless_link: str) -> BytesIO:
    """
    Генерация QR кода для VLESS ключа

    :param vless_link: VLESS ссылка
    :return: BytesIO объект с изображением QR кода
    """
    # Создаем QR код
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )

    qr.add_data(vless_link)
    qr.make(fit=True)

    # Создаем изображение
    img = qr.make_image(fill_color="black", back_color="white")

    # Сохраняем в BytesIO
    bio = BytesIO()
    bio.name = 'qrcode.png'
    img.save(bio, 'PNG')
    bio.seek(0)

    return bio
