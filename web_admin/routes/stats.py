"""
Маршруты для статистики
"""
import aiosqlite
from datetime import datetime, timedelta
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

stats_router = APIRouter()
templates: Jinja2Templates = None


def setup_stats_router(tpl: Jinja2Templates):
    global templates
    templates = tpl


@stats_router.get('/stats', response_class=HTMLResponse)
async def stats_dashboard(request: Request):
    """Дашборд со статистикой"""
    db_path = request.app.state.db_path

    now = datetime.now()
    today = now.strftime('%Y-%m-%d')
    week_ago = (now - timedelta(days=7)).strftime('%Y-%m-%d')
    month_ago = (now - timedelta(days=30)).strftime('%Y-%m-%d')

    stats = {
        'keys': {},
        'revenue': {},
        'managers': [],
        'chart': []
    }

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # --- Ключи ---
        cursor = await db.execute('SELECT COUNT(*) FROM keys_history')
        stats['keys']['total'] = (await cursor.fetchone())[0]

        # Ключи за сегодня
        cursor = await db.execute('''
            SELECT COUNT(*) FROM keys_history WHERE DATE(created_at) = ?
        ''', (today,))
        stats['keys']['today'] = (await cursor.fetchone())[0]

        # Ключи за неделю
        cursor = await db.execute('''
            SELECT COUNT(*) FROM keys_history WHERE DATE(created_at) >= ?
        ''', (week_ago,))
        stats['keys']['week'] = (await cursor.fetchone())[0]

        # Ключи за месяц
        cursor = await db.execute('''
            SELECT COUNT(*) FROM keys_history WHERE DATE(created_at) >= ?
        ''', (month_ago,))
        stats['keys']['month'] = (await cursor.fetchone())[0]

        # --- Выручка ---
        cursor = await db.execute('''
            SELECT COALESCE(SUM(price), 0) FROM keys_history
            WHERE DATE(created_at) = ?
        ''', (today,))
        stats['revenue']['today'] = (await cursor.fetchone())[0]

        cursor = await db.execute('''
            SELECT COALESCE(SUM(price), 0) FROM keys_history
            WHERE DATE(created_at) >= ?
        ''', (week_ago,))
        stats['revenue']['week'] = (await cursor.fetchone())[0]

        cursor = await db.execute('''
            SELECT COALESCE(SUM(price), 0) FROM keys_history
            WHERE DATE(created_at) >= ?
        ''', (month_ago,))
        stats['revenue']['month'] = (await cursor.fetchone())[0]

        # --- Топ менеджеров за месяц ---
        cursor = await db.execute('''
            SELECT
                kh.manager_id,
                COALESCE(m.custom_name, m.full_name, m.username) as name,
                COUNT(*) as keys_count,
                COALESCE(SUM(kh.price), 0) as revenue
            FROM keys_history kh
            LEFT JOIN managers m ON kh.manager_id = m.user_id
            WHERE DATE(kh.created_at) >= ?
            GROUP BY kh.manager_id
            ORDER BY revenue DESC
            LIMIT 5
        ''', (month_ago,))
        managers = await cursor.fetchall()
        stats['managers'] = [dict(m) for m in managers]

        # --- График за 7 дней ---
        cursor = await db.execute('''
            SELECT DATE(created_at) as date, COUNT(*) as count, COALESCE(SUM(price), 0) as revenue
            FROM keys_history
            WHERE DATE(created_at) >= ?
            GROUP BY DATE(created_at)
            ORDER BY date
        ''', (week_ago,))
        chart_data = [dict(row) for row in await cursor.fetchall()]
        stats['chart'] = chart_data

        # --- Менеджеры ---
        cursor = await db.execute("SELECT COUNT(*) FROM managers WHERE is_active = 1")
        stats['managers_active'] = (await cursor.fetchone())[0]

    return templates.TemplateResponse('stats.html', {
        'request': request,
        'stats': stats,
        'active': 'stats'
    })
