"""
Менеджер аналитики и статистики
"""
import aiosqlite
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)


class AnalyticsManager:
    """Менеджер аналитики и отчётов"""

    def __init__(self, db_path: str):
        self.db_path = db_path

    async def get_dashboard_stats(self) -> Dict[str, Any]:
        """Получение данных для дашборда"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Активные клиенты
            cursor = await db.execute(
                "SELECT COUNT(*) FROM clients WHERE status = 'active'"
            )
            active_clients = (await cursor.fetchone())[0]

            # Всего клиентов
            cursor = await db.execute("SELECT COUNT(*) FROM clients")
            total_clients = (await cursor.fetchone())[0]

            # Истекающие в ближайшие 7 дней
            week_later = int((datetime.now() + timedelta(days=7)).timestamp() * 1000)
            now = int(datetime.now().timestamp() * 1000)
            cursor = await db.execute(
                """SELECT COUNT(*) FROM clients
                   WHERE status = 'active' AND expire_time BETWEEN ? AND ?""",
                (now, week_later)
            )
            expiring_soon = (await cursor.fetchone())[0]

            # Истекшие
            cursor = await db.execute(
                "SELECT COUNT(*) FROM clients WHERE status = 'expired'"
            )
            expired = (await cursor.fetchone())[0]

            # Выручка за сегодня
            today = datetime.now().strftime('%Y-%m-%d')
            cursor = await db.execute(
                """SELECT COALESCE(SUM(price), 0) FROM subscription_history
                   WHERE DATE(created_at) = ?""",
                (today,)
            )
            today_revenue = (await cursor.fetchone())[0]

            # Выручка за месяц
            month_start = datetime.now().replace(day=1).strftime('%Y-%m-%d')
            cursor = await db.execute(
                """SELECT COALESCE(SUM(price), 0) FROM subscription_history
                   WHERE created_at >= ?""",
                (month_start,)
            )
            month_revenue = (await cursor.fetchone())[0]

            # Ключи созданы сегодня
            cursor = await db.execute(
                """SELECT COUNT(*) FROM subscription_history
                   WHERE action = 'created' AND DATE(created_at) = ?""",
                (today,)
            )
            keys_today = (await cursor.fetchone())[0]

            # Продления сегодня
            cursor = await db.execute(
                """SELECT COUNT(*) FROM subscription_history
                   WHERE action = 'extended' AND DATE(created_at) = ?""",
                (today,)
            )
            extensions_today = (await cursor.fetchone())[0]

            return {
                'clients': {
                    'total': total_clients,
                    'active': active_clients,
                    'expired': expired,
                    'expiring_7d': expiring_soon
                },
                'revenue': {
                    'today': today_revenue,
                    'month': month_revenue
                },
                'today': {
                    'keys_created': keys_today,
                    'extensions': extensions_today
                }
            }

    async def get_revenue_report(
        self,
        from_date: datetime,
        to_date: datetime,
        group_by: str = 'day'
    ) -> List[Dict[str, Any]]:
        """Отчёт по выручке"""
        if group_by == 'day':
            date_format = '%Y-%m-%d'
            group_expr = "DATE(created_at)"
        elif group_by == 'week':
            date_format = '%Y-W%W'
            group_expr = "strftime('%Y-W%W', created_at)"
        elif group_by == 'month':
            date_format = '%Y-%m'
            group_expr = "strftime('%Y-%m', created_at)"
        else:
            raise ValueError(f"Неподдерживаемый group_by: {group_by}")

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT
                      {group_expr} as period,
                      COUNT(*) as transactions,
                      SUM(price) as revenue,
                      SUM(CASE WHEN action = 'created' THEN price ELSE 0 END) as new_revenue,
                      SUM(CASE WHEN action = 'extended' THEN price ELSE 0 END) as extension_revenue,
                      SUM(discount_amount) as discounts
                    FROM subscription_history
                    WHERE created_at BETWEEN ? AND ?
                    GROUP BY {group_expr}
                    ORDER BY period""",
                (from_date.isoformat(), to_date.isoformat())
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_manager_stats(
        self,
        manager_id: Optional[int] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Статистика по менеджерам"""
        conditions = []
        params = []

        if manager_id:
            conditions.append("manager_id = ?")
            params.append(manager_id)

        if from_date:
            conditions.append("created_at >= ?")
            params.append(from_date.isoformat())

        if to_date:
            conditions.append("created_at <= ?")
            params.append(to_date.isoformat())

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT
                      manager_id,
                      COUNT(*) as total_transactions,
                      SUM(CASE WHEN action = 'created' THEN 1 ELSE 0 END) as keys_created,
                      SUM(CASE WHEN action = 'extended' THEN 1 ELSE 0 END) as extensions,
                      SUM(price) as total_revenue,
                      AVG(price) as avg_order,
                      SUM(discount_amount) as total_discounts
                    FROM subscription_history
                    WHERE {where_clause}
                    GROUP BY manager_id
                    ORDER BY total_revenue DESC""",
                params
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_period_popularity(
        self,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Популярность периодов подписки"""
        conditions = ["action = 'created'"]
        params = []

        if from_date:
            conditions.append("created_at >= ?")
            params.append(from_date.isoformat())

        if to_date:
            conditions.append("created_at <= ?")
            params.append(to_date.isoformat())

        where_clause = " AND ".join(conditions)

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT
                      period,
                      COUNT(*) as count,
                      SUM(price) as revenue
                    FROM subscription_history
                    WHERE {where_clause}
                    GROUP BY period
                    ORDER BY count DESC""",
                params
            )
            periods = [dict(row) for row in await cursor.fetchall()]

            # Вычисляем процентыtotal = sum(p['count'] for p in periods) or 1
            total = sum(p['count'] for p in periods) or 1
            for p in periods:
                p['percentage'] = round(p['count'] / total * 100, 1)

            return {
                'periods': periods,
                'total_orders': total
            }

    async def get_churn_analysis(self, months: int = 6) -> Dict[str, Any]:
        """Анализ оттока клиентов"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            results = []
            now = datetime.now()

            for i in range(months):
                # Начало и конец месяца
                month_date = now - timedelta(days=30 * i)
                month_start = month_date.replace(day=1)
                if month_start.month == 12:
                    month_end = month_start.replace(year=month_start.year + 1, month=1, day=1)
                else:
                    month_end = month_start.replace(month=month_start.month + 1, day=1)

                month_str = month_start.strftime('%Y-%m')

                # Активные в начале месяца
                start_ts = int(month_start.timestamp() * 1000)
                cursor = await db.execute(
                    """SELECT COUNT(*) FROM clients
                       WHERE created_at < ? AND (expire_time > ? OR status = 'active')""",
                    (month_start.isoformat(), start_ts)
                )
                active_start = (await cursor.fetchone())[0]

                # Истекли в этом месяце
                end_ts = int(month_end.timestamp() * 1000)
                cursor = await db.execute(
                    """SELECT COUNT(*) FROM clients
                       WHERE expire_time BETWEEN ? AND ? AND status = 'expired'""",
                    (start_ts, end_ts)
                )
                churned = (await cursor.fetchone())[0]

                # Новые клиенты
                cursor = await db.execute(
                    """SELECT COUNT(*) FROM clients
                       WHERE created_at BETWEEN ? AND ?""",
                    (month_start.isoformat(), month_end.isoformat())
                )
                new_clients = (await cursor.fetchone())[0]

                # Продления
                cursor = await db.execute(
                    """SELECT COUNT(*) FROM subscription_history
                       WHERE action = 'extended' AND created_at BETWEEN ? AND ?""",
                    (month_start.isoformat(), month_end.isoformat())
                )
                retained = (await cursor.fetchone())[0]

                churn_rate = (churned / active_start * 100) if active_start > 0 else 0

                results.append({
                    'month': month_str,
                    'active_start': active_start,
                    'new_clients': new_clients,
                    'retained': retained,
                    'churned': churned,
                    'churn_rate': round(churn_rate, 1)
                })

            return {
                'months': list(reversed(results)),
                'avg_churn_rate': round(
                    sum(r['churn_rate'] for r in results) / len(results), 1
                ) if results else 0
            }

    async def get_client_lifetime_value(self) -> Dict[str, Any]:
        """Расчёт LTV клиентов"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Средний доход на клиента
            cursor = await db.execute(
                """SELECT
                     c.id,
                     COALESCE(SUM(sh.price), 0) as total_paid,
                     COUNT(sh.id) as transactions,
                     c.created_at,
                     c.status
                   FROM clients c
                   LEFT JOIN subscription_history sh ON c.id = sh.client_id
                   GROUP BY c.id"""
            )
            clients = [dict(row) for row in await cursor.fetchall()]

            if not clients:
                return {'avg_ltv': 0, 'total_revenue': 0, 'avg_transactions': 0}

            total_revenue = sum(c['total_paid'] for c in clients)
            total_transactions = sum(c['transactions'] for c in clients)
            active_count = sum(1 for c in clients if c['status'] == 'active')

            # Средний LTV
            avg_ltv = total_revenue / len(clients) if clients else 0

            # LTV по сегментам
            ltv_segments = {'0': 0, '1-500': 0, '500-1000': 0, '1000-5000': 0, '5000+': 0}
            for c in clients:
                ltv = c['total_paid']
                if ltv == 0:
                    ltv_segments['0'] += 1
                elif ltv < 500:
                    ltv_segments['1-500'] += 1
                elif ltv < 1000:
                    ltv_segments['500-1000'] += 1
                elif ltv < 5000:
                    ltv_segments['1000-5000'] += 1
                else:
                    ltv_segments['5000+'] += 1

            return {
                'avg_ltv': round(avg_ltv, 2),
                'total_revenue': total_revenue,
                'total_clients': len(clients),
                'active_clients': active_count,
                'avg_transactions': round(total_transactions / len(clients), 1) if clients else 0,
                'ltv_segments': ltv_segments
            }

    async def get_daily_stats(
        self,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None
    ) -> List[Dict[str, Any]]:
        """Получение дневной статистики"""
        conditions = []
        params = []

        if from_date:
            conditions.append("date >= ?")
            params.append(from_date.strftime('%Y-%m-%d'))

        if to_date:
            conditions.append("date <= ?")
            params.append(to_date.strftime('%Y-%m-%d'))

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                f"""SELECT * FROM daily_stats
                    WHERE {where_clause}
                    ORDER BY date DESC""",
                params
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def get_sales_funnel_stats(
        self,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """Статистика воронки продаж"""
        conditions = []
        params = []

        if from_date:
            conditions.append("created_at >= ?")
            params.append(from_date.isoformat())

        if to_date:
            conditions.append("created_at <= ?")
            params.append(to_date.isoformat())

        where_clause = " AND ".join(conditions) if conditions else "1=1"

        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row

            # Общее количество
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM sales_funnel WHERE {where_clause}",
                params
            )
            total = (await cursor.fetchone())[0]

            # По статусам
            cursor = await db.execute(
                f"""SELECT status, COUNT(*) as cnt FROM sales_funnel
                    WHERE {where_clause} GROUP BY status""",
                params
            )
            by_status = {row['status']: row['cnt'] for row in await cursor.fetchall()}

            # По этапам отказа
            cursor = await db.execute(
                f"""SELECT abandon_step, COUNT(*) as cnt FROM sales_funnel
                    WHERE status = 'abandoned' AND {where_clause}
                    GROUP BY abandon_step""",
                params
            )
            abandon_steps = {row['abandon_step']: row['cnt'] for row in await cursor.fetchall()}

            # Конверсия
            started = by_status.get('started', 0) + by_status.get('completed', 0) + by_status.get('abandoned', 0)
            completed = by_status.get('completed', 0)
            conversion_rate = (completed / started * 100) if started > 0 else 0

            return {
                'total_sessions': total,
                'by_status': by_status,
                'abandon_steps': abandon_steps,
                'conversion_rate': round(conversion_rate, 1),
                'completed': completed,
                'abandoned': by_status.get('abandoned', 0)
            }

    async def get_server_stats(self) -> List[Dict[str, Any]]:
        """Статистика по серверам"""
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT
                     server_name,
                     COUNT(*) as clients_count,
                     SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) as active
                   FROM client_servers
                   GROUP BY server_name
                   ORDER BY clients_count DESC"""
            )
            return [dict(row) for row in await cursor.fetchall()]

    async def generate_report(
        self,
        report_type: str,
        from_date: datetime,
        to_date: datetime
    ) -> Dict[str, Any]:
        """Генерация сводного отчёта"""
        report = {
            'type': report_type,
            'period': {
                'from': from_date.isoformat(),
                'to': to_date.isoformat()
            },
            'generated_at': datetime.now().isoformat()
        }

        if report_type == 'full' or report_type == 'revenue':
            report['revenue'] = await self.get_revenue_report(from_date, to_date)

        if report_type == 'full' or report_type == 'managers':
            report['managers'] = await self.get_manager_stats(
                from_date=from_date,
                to_date=to_date
            )

        if report_type == 'full' or report_type == 'periods':
            report['periods'] = await self.get_period_popularity(from_date, to_date)

        if report_type == 'full':
            report['dashboard'] = await self.get_dashboard_stats()
            report['funnel'] = await self.get_sales_funnel_stats(from_date, to_date)
            report['servers'] = await self.get_server_stats()

        return report
