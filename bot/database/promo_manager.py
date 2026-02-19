"""
Менеджер промокодов и реферальной системы
"""
import aiosqlite
import logging
import secrets
import string
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from .models import PromoDiscountType

logger = logging.getLogger(__name__)


class PromoManager:
    """Менеджер промокодов"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    # ==================== ПРОМОКОДЫ ====================

    async def create_promo(
        self,
        code: str = None,
        discount_type: str = "percent",
        discount_value: int = 10,
        description: str = None,
        max_uses: int = 0,
        valid_days: int = 30,
        valid_until: datetime = None,
        min_period: str = None,
        min_amount: int = 0,
        applicable_periods: List[str] = None,
        created_by: int = None
    ) -> Optional[Dict]:
        """
        Создать промокод

        :param code: Код (если None - генерируется автоматически)
        :param discount_type: percent, fixed, days
        :param discount_value: Значение скидки
        :param max_uses: Макс. использований (0 = безлимит)
        :param valid_days: Срок действия в днях (игнорируется если указан valid_until)
        :param valid_until: Дата окончания действия (приоритет над valid_days)
        :param min_period: Минимальный период подписки
        :param applicable_periods: Список периодов для применения (JSON)
        """
        try:
            if not code:
                code = self._generate_code()

            code = code.upper().strip()
            # valid_until имеет приоритет над valid_days
            if valid_until is None and valid_days:
                valid_until = datetime.now() + timedelta(days=valid_days)

            async with aiosqlite.connect(self.db_path) as db:
                cursor = await db.execute(
                    """INSERT INTO promo_codes
                       (code, description, discount_type, discount_value, max_uses,
                        valid_from, valid_until, min_period, min_amount, applicable_periods, created_by)
                       VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?, ?, ?)""",
                    (code, description, discount_type, discount_value, max_uses,
                     valid_until, min_period, min_amount,
                     ",".join(applicable_periods) if applicable_periods else None,
                     created_by)
                )
                promo_id = cursor.lastrowid
                await db.commit()

                logger.info(f"Создан промокод {code} (ID: {promo_id})")
                return {
                    "id": promo_id,
                    "code": code,
                    "discount_type": discount_type,
                    "discount_value": discount_value,
                    "valid_until": valid_until.isoformat() if valid_until else None
                }

        except Exception as e:
            logger.error(f"Ошибка создания промокода: {e}")
            return None

    def _generate_code(self, length: int = 8) -> str:
        """Генерация случайного кода"""
        chars = string.ascii_uppercase + string.digits
        return ''.join(secrets.choice(chars) for _ in range(length))

    async def validate_promo(
        self,
        code: str,
        period_key: str = None,
        amount: int = 0,
        client_id: int = None
    ) -> Tuple[bool, Optional[Dict], str]:
        """
        Проверить промокод

        :return: (is_valid, promo_data, error_message)
        """
        code = code.upper().strip()

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                "SELECT * FROM promo_codes WHERE code = ? AND is_active = 1",
                (code,)
            )
            promo = await cursor.fetchone()

            if not promo:
                return False, None, "Промокод не найден"

            promo = dict(promo)

            # Проверка срока действия
            if promo['valid_until']:
                valid_until = datetime.fromisoformat(promo['valid_until'])
                if datetime.now() > valid_until:
                    return False, None, "Промокод истёк"

            # Проверка лимита использований
            if promo['max_uses'] > 0 and promo['current_uses'] >= promo['max_uses']:
                return False, None, "Промокод исчерпан"

            # Проверка минимальной суммы
            if promo['min_amount'] > 0 and amount < promo['min_amount']:
                return False, None, f"Минимальная сумма заказа: {promo['min_amount']} ₽"

            # Проверка применимых периодов
            if promo['applicable_periods'] and period_key:
                applicable = promo['applicable_periods'].split(',')
                if period_key not in applicable:
                    return False, None, "Промокод не применим к этому периоду"

            # Проверка: использовал ли клиент этот промокод
            if client_id:
                cursor = await db.execute(
                    "SELECT 1 FROM promo_uses WHERE promo_id = ? AND client_id = ?",
                    (promo['id'], client_id)
                )
                if await cursor.fetchone():
                    return False, None, "Вы уже использовали этот промокод"

            return True, promo, ""

    def calculate_discount(
        self,
        promo: Dict,
        original_price: int,
        period_days: int = 30
    ) -> Tuple[int, int]:
        """
        Рассчитать скидку

        :return: (discount_amount, final_price) или (bonus_days, original_price) для типа days
        """
        discount_type = promo['discount_type']
        discount_value = promo['discount_value']

        if discount_type == 'percent':
            discount = int(original_price * discount_value / 100)
            return discount, original_price - discount

        elif discount_type == 'fixed':
            discount = min(discount_value, original_price)
            return discount, original_price - discount

        elif discount_type == 'days':
            # Бонусные дни - цена не меняется
            return discount_value, original_price

        return 0, original_price

    async def use_promo(
        self,
        promo_id: int,
        client_id: int,
        original_price: int,
        discount_amount: int,
        final_price: int,
        subscription_id: int = None
    ) -> bool:
        """Записать использование промокода"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Записываем использование
                await db.execute(
                    """INSERT INTO promo_uses
                       (promo_id, client_id, subscription_id, original_price, discount_amount, final_price)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (promo_id, client_id, subscription_id, original_price, discount_amount, final_price)
                )

                # Увеличиваем счётчик
                await db.execute(
                    "UPDATE promo_codes SET current_uses = current_uses + 1 WHERE id = ?",
                    (promo_id,)
                )

                await db.commit()
                return True

        except Exception as e:
            logger.error(f"Ошибка записи использования промокода: {e}")
            return False

    async def get_promo(self, code: str = None, promo_id: int = None) -> Optional[Dict]:
        """Получить промокод"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if promo_id:
                cursor = await db.execute(
                    "SELECT * FROM promo_codes WHERE id = ?", (promo_id,)
                )
            elif code:
                cursor = await db.execute(
                    "SELECT * FROM promo_codes WHERE code = ?", (code.upper(),)
                )
            else:
                return None

            row = await cursor.fetchone()
            return dict(row) if row else None

    async def list_promos(self, active_only: bool = True, limit: int = 50) -> List[Dict]:
        """Список промокодов"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            if active_only:
                cursor = await db.execute(
                    """SELECT * FROM promo_codes
                       WHERE is_active = 1
                       ORDER BY created_at DESC LIMIT ?""",
                    (limit,)
                )
            else:
                cursor = await db.execute(
                    "SELECT * FROM promo_codes ORDER BY created_at DESC LIMIT ?",
                    (limit,)
                )

            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    # Алиасы для совместимости с extended.py
    async def get_active_promos(self, limit: int = 50) -> List[Dict]:
        """Алиас для list_promos(active_only=True)"""
        return await self.list_promos(active_only=True, limit=limit)

    async def get_all_promos(self, limit: int = 50) -> List[Dict]:
        """Алиас для list_promos(active_only=False)"""
        return await self.list_promos(active_only=False, limit=limit)

    async def deactivate_promo(self, promo_id: int) -> bool:
        """Деактивировать промокод"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "UPDATE promo_codes SET is_active = 0 WHERE id = ?",
                    (promo_id,)
                )
                await db.commit()
                return True
        except Exception as e:
            logger.error(f"Ошибка деактивации промокода: {e}")
            return False

    async def get_promo_stats(self, promo_id: int) -> Dict:
        """Статистика промокода"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                """SELECT
                     COUNT(*) as total_uses,
                     SUM(discount_amount) as total_discount,
                     SUM(final_price) as total_revenue,
                     AVG(discount_amount) as avg_discount
                   FROM promo_uses WHERE promo_id = ?""",
                (promo_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else {}


class ReferralManager:
    """Менеджер реферальной системы"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.bonus_days = 7  # Бонус рефереру за приглашённого

    async def generate_referral_code(self, client_id: int) -> str:
        """Генерировать реферальный код для клиента (использует UUID)"""
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                "SELECT uuid FROM clients WHERE id = ?", (client_id,)
            )
            row = await cursor.fetchone()
            if row:
                return row[0][:8].upper()  # Первые 8 символов UUID
            return None

    async def get_referral_link(self, client_id: int, bot_username: str) -> Optional[str]:
        """Получить реферальную ссылку"""
        code = await self.generate_referral_code(client_id)
        if code:
            return f"https://t.me/{bot_username}?start=ref_{code}"
        return None

    async def process_referral(self, referrer_code: str, referred_client_id: int) -> Optional[int]:
        """
        Обработать реферал

        :return: ID реферера или None
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Находим реферера по коду (первые 8 символов UUID)
                cursor = await db.execute(
                    "SELECT id FROM clients WHERE uuid LIKE ?",
                    (f"{referrer_code}%",)
                )
                referrer = await cursor.fetchone()

                if not referrer:
                    return None

                referrer_id = referrer[0]

                # Проверяем, не является ли это самореферал
                if referrer_id == referred_client_id:
                    return None

                # Проверяем, не был ли уже записан этот реферал
                cursor = await db.execute(
                    "SELECT 1 FROM referrals WHERE referred_id = ?",
                    (referred_client_id,)
                )
                if await cursor.fetchone():
                    return referrer_id  # Уже записан, возвращаем ID

                # Записываем реферал
                await db.execute(
                    """INSERT INTO referrals (referrer_id, referred_id, referral_code, bonus_days)
                       VALUES (?, ?, ?, ?)""",
                    (referrer_id, referred_client_id, referrer_code, self.bonus_days)
                )

                await db.commit()
                logger.info(f"Записан реферал: {referrer_id} -> {referred_client_id}")
                return referrer_id

        except Exception as e:
            logger.error(f"Ошибка обработки реферала: {e}")
            return None

    async def apply_referral_bonus(self, referrer_id: int) -> Optional[int]:
        """
        Применить бонус рефереру

        :return: Новое время истечения или None
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                # Находим неприменённые бонусы
                cursor = await db.execute(
                    """SELECT id, bonus_days FROM referrals
                       WHERE referrer_id = ? AND bonus_applied = 0""",
                    (referrer_id,)
                )
                referrals = await cursor.fetchall()

                if not referrals:
                    return None

                total_bonus_days = sum(r[1] for r in referrals)
                referral_ids = [r[0] for r in referrals]

                # Получаем текущее время истечения реферера
                cursor = await db.execute(
                    "SELECT expire_time FROM clients WHERE id = ?",
                    (referrer_id,)
                )
                row = await cursor.fetchone()
                if not row:
                    return None

                current_expire = row[0] or int(datetime.now().timestamp() * 1000)
                new_expire = current_expire + (total_bonus_days * 24 * 60 * 60 * 1000)

                # Обновляем время истечения реферера
                await db.execute(
                    "UPDATE clients SET expire_time = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (new_expire, referrer_id)
                )

                # Отмечаем бонусы как применённые
                placeholders = ','.join('?' * len(referral_ids))
                await db.execute(
                    f"UPDATE referrals SET bonus_applied = 1 WHERE id IN ({placeholders})",
                    referral_ids
                )

                # Записываем в историю
                await db.execute(
                    """INSERT INTO subscription_history
                       (client_id, action, days, old_expire, new_expire, note)
                       VALUES (?, 'extended', ?, ?, ?, ?)""",
                    (referrer_id, total_bonus_days, current_expire, new_expire,
                     f"Реферальный бонус за {len(referral_ids)} приглашённых")
                )

                await db.commit()
                logger.info(f"Применён реферальный бонус +{total_bonus_days} дней для клиента {referrer_id}")
                return new_expire

        except Exception as e:
            logger.error(f"Ошибка применения бонуса: {e}")
            return None

    async def get_referral_stats(self, client_id: int) -> Dict:
        """Получить статистику рефералов клиента"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Количество приглашённых
            cursor = await db.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = ?",
                (client_id,)
            )
            total_referred = (await cursor.fetchone())[0]

            # Применённые бонусы
            cursor = await db.execute(
                "SELECT SUM(bonus_days) FROM referrals WHERE referrer_id = ? AND bonus_applied = 1",
                (client_id,)
            )
            applied_bonus = (await cursor.fetchone())[0] or 0

            # Ожидающие бонусы
            cursor = await db.execute(
                "SELECT SUM(bonus_days) FROM referrals WHERE referrer_id = ? AND bonus_applied = 0",
                (client_id,)
            )
            pending_bonus = (await cursor.fetchone())[0] or 0

            # Список приглашённых
            cursor = await db.execute(
                """SELECT c.email, c.created_at, r.bonus_applied
                   FROM referrals r
                   JOIN clients c ON r.referred_id = c.id
                   WHERE r.referrer_id = ?
                   ORDER BY r.created_at DESC
                   LIMIT 10""",
                (client_id,)
            )
            referred_list = [dict(row) for row in await cursor.fetchall()]

            # Кто пригласил этого клиента
            cursor = await db.execute(
                """SELECT c.email, r.created_at
                   FROM referrals r
                   JOIN clients c ON r.referrer_id = c.id
                   WHERE r.referred_id = ?""",
                (client_id,)
            )
            referred_by = await cursor.fetchone()

            return {
                "total_referred": total_referred,
                "total_referrals": total_referred,  # Алиас для совместимости
                "applied_bonus_days": applied_bonus,
                "pending_bonus_days": pending_bonus,
                "referred_list": referred_list,
                "referred_by": dict(referred_by) if referred_by else None
            }

    async def get_top_referrers(self, limit: int = 10) -> List[Dict]:
        """Топ рефереров"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            cursor = await db.execute(
                """SELECT c.id as referrer_id, c.email, c.name,
                          COUNT(r.id) as referred_count,
                          SUM(r.bonus_days) as total_bonus
                   FROM clients c
                   JOIN referrals r ON c.id = r.referrer_id
                   GROUP BY c.id
                   ORDER BY referred_count DESC
                   LIMIT ?""",
                (limit,)
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]
