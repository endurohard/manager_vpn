"""
Маршруты для управления менеджерами
"""
import aiosqlite
from datetime import datetime
from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

managers_router = APIRouter()
templates: Jinja2Templates = None


def setup_managers_router(tpl: Jinja2Templates):
    global templates
    templates = tpl


@managers_router.get('/managers', response_class=HTMLResponse)
async def managers_list(request: Request):
    """Список всех менеджеров"""
    db_path = request.app.state.db_path
    root_path = request.app.root_path

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Получаем менеджеров со статистикой
        cursor = await db.execute('''
            SELECT
                m.*,
                COUNT(kh.id) as keys_count,
                COALESCE(SUM(kh.price), 0) as total_revenue,
                MAX(kh.created_at) as last_key_date
            FROM managers m
            LEFT JOIN keys_history kh ON m.user_id = kh.manager_id
            GROUP BY m.user_id
            ORDER BY m.added_at DESC
        ''')
        managers = [dict(row) for row in await cursor.fetchall()]

        # Статистика за сегодня для каждого
        for manager in managers:
            cursor = await db.execute('''
                SELECT COUNT(*) FROM keys_history
                WHERE manager_id = ? AND DATE(created_at) = DATE('now')
            ''', (manager['user_id'],))
            manager['keys_today'] = (await cursor.fetchone())[0]

    return templates.TemplateResponse('managers.html', {
        'request': request,
        'managers': managers,
        'active': 'managers'
    })


@managers_router.post('/managers/add')
async def add_manager(
    request: Request,
    user_id: int = Form(...),
    custom_name: str = Form('')
):
    """Добавить нового менеджера"""
    db_path = request.app.state.db_path
    root_path = request.app.root_path

    async with aiosqlite.connect(db_path) as db:
        # Проверяем существование
        cursor = await db.execute(
            'SELECT user_id FROM managers WHERE user_id = ?',
            (user_id,)
        )
        if await cursor.fetchone():
            return JSONResponse({
                'success': False,
                'error': 'Менеджер с таким ID уже существует'
            })

        # Добавляем менеджера
        await db.execute('''
            INSERT INTO managers (user_id, custom_name, is_active, added_at)
            VALUES (?, ?, 1, datetime('now'))
        ''', (user_id, custom_name or None))
        await db.commit()

    return JSONResponse({'success': True})


@managers_router.post('/managers/{user_id}/toggle')
async def toggle_manager(request: Request, user_id: int):
    """Включить/выключить менеджера"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        # Получаем текущий статус
        cursor = await db.execute(
            'SELECT is_active FROM managers WHERE user_id = ?',
            (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return JSONResponse({'success': False, 'error': 'Менеджер не найден'})

        new_status = 0 if row[0] else 1
        await db.execute(
            'UPDATE managers SET is_active = ? WHERE user_id = ?',
            (new_status, user_id)
        )
        await db.commit()

    return JSONResponse({'success': True, 'is_active': bool(new_status)})


@managers_router.post('/managers/{user_id}/rename')
async def rename_manager(
    request: Request,
    user_id: int,
    custom_name: str = Form('')
):
    """Переименовать менеджера"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            'UPDATE managers SET custom_name = ? WHERE user_id = ?',
            (custom_name or None, user_id)
        )
        await db.commit()

    return JSONResponse({'success': True})


@managers_router.delete('/managers/{user_id}')
async def delete_manager(request: Request, user_id: int):
    """Удалить менеджера"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        await db.execute('DELETE FROM managers WHERE user_id = ?', (user_id,))
        await db.commit()

    return JSONResponse({'success': True})


@managers_router.get('/managers/{user_id}', response_class=HTMLResponse)
async def manager_detail(request: Request, user_id: int):
    """Детальная страница менеджера"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Информация о менеджере
        cursor = await db.execute(
            'SELECT * FROM managers WHERE user_id = ?',
            (user_id,)
        )
        manager = await cursor.fetchone()
        if not manager:
            return HTMLResponse("Менеджер не найден", status_code=404)
        manager = dict(manager)

        # Статистика
        cursor = await db.execute('''
            SELECT
                COUNT(*) as total_keys,
                COALESCE(SUM(price), 0) as total_revenue
            FROM keys_history WHERE manager_id = ?
        ''', (user_id,))
        stats = dict(await cursor.fetchone())

        # Статистика за периоды
        cursor = await db.execute('''
            SELECT COUNT(*) FROM keys_history
            WHERE manager_id = ? AND DATE(created_at) = DATE('now')
        ''', (user_id,))
        stats['today'] = (await cursor.fetchone())[0]

        cursor = await db.execute('''
            SELECT COUNT(*) FROM keys_history
            WHERE manager_id = ? AND DATE(created_at) >= DATE('now', '-7 days')
        ''', (user_id,))
        stats['week'] = (await cursor.fetchone())[0]

        cursor = await db.execute('''
            SELECT COUNT(*) FROM keys_history
            WHERE manager_id = ? AND DATE(created_at) >= DATE('now', '-30 days')
        ''', (user_id,))
        stats['month'] = (await cursor.fetchone())[0]

        # Последние ключи
        cursor = await db.execute('''
            SELECT * FROM keys_history
            WHERE manager_id = ?
            ORDER BY created_at DESC
            LIMIT 20
        ''', (user_id,))
        recent_keys = [dict(row) for row in await cursor.fetchall()]

        # Статистика по периодам подписки
        cursor = await db.execute('''
            SELECT period, COUNT(*) as count, COALESCE(SUM(price), 0) as revenue
            FROM keys_history
            WHERE manager_id = ?
            GROUP BY period
            ORDER BY count DESC
        ''', (user_id,))
        periods_stats = [dict(row) for row in await cursor.fetchall()]

    return templates.TemplateResponse('manager_detail.html', {
        'request': request,
        'manager': manager,
        'stats': stats,
        'recent_keys': recent_keys,
        'periods_stats': periods_stats,
        'active': 'managers'
    })
