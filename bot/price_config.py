"""
Управление конфигурацией цен
"""
import json
import os
from typing import Dict

# Путь к файлу с ценами
PRICES_FILE = 'prices.json'

# Цены по умолчанию
DEFAULT_PRICES = {
    "1_month": {"name": "Месяц", "days": 30, "price": 300},
    "3_months": {"name": "3 месяца", "days": 90, "price": 800},
    "6_months": {"name": "6 месяцев", "days": 180, "price": 1500},
    "1_year": {"name": "Год", "days": 365, "price": 2500}
}


class PriceManager:
    """Менеджер для управления ценами"""

    @staticmethod
    def load_prices() -> Dict:
        """Загрузить цены из файла"""
        if os.path.exists(PRICES_FILE):
            try:
                with open(PRICES_FILE, 'r', encoding='utf-8') as f:
                    prices = json.load(f)
                    # Проверяем, что все необходимые ключи присутствуют
                    for key in DEFAULT_PRICES:
                        if key not in prices:
                            prices[key] = DEFAULT_PRICES[key]
                    return prices
            except Exception as e:
                print(f"Ошибка загрузки цен: {e}")
                return DEFAULT_PRICES.copy()
        else:
            # Если файла нет, создаем его с ценами по умолчанию
            PriceManager.save_prices(DEFAULT_PRICES)
            return DEFAULT_PRICES.copy()

    @staticmethod
    def save_prices(prices: Dict) -> bool:
        """Сохранить цены в файл"""
        try:
            with open(PRICES_FILE, 'w', encoding='utf-8') as f:
                json.dump(prices, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            print(f"Ошибка сохранения цен: {e}")
            return False

    @staticmethod
    def update_price(period_key: str, new_price: int) -> bool:
        """Обновить цену для конкретного периода"""
        prices = PriceManager.load_prices()
        if period_key in prices:
            prices[period_key]['price'] = new_price
            return PriceManager.save_prices(prices)
        return False

    @staticmethod
    def get_price(period_key: str) -> int:
        """Получить цену для конкретного периода"""
        prices = PriceManager.load_prices()
        return prices.get(period_key, {}).get('price', 0)


# Глобальная функция для получения актуальных цен
def get_subscription_periods() -> Dict:
    """Получить актуальные цены на подписки"""
    return PriceManager.load_prices()
