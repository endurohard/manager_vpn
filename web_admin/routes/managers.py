"""
Маршруты для управления менеджерами
"""
import aiosqlite
from datetime import datetime
from fastapi import APIRouter, Request, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from web_admin.routes.settings import load_prices

managers_router = APIRouter()
templates: Jinja2Templates = None


def setup_managers_router(tpl: Jinja2Templates):
    global templates
    templates = tpl


def _build_global_cost_map() -> dict:
    """Построить маппинг expire_days → global price из prices.json"""
    prices = load_prices()
    cost_map = {}
    for period_key, info in prices.items():
        cost_map[info['days']] = info['price']
    return cost_map


@managers_router.get('/managers', response_class=HTMLResponse)
async def managers_list(request: Request):
    """Список всех менеджеров"""
    db_path = request.app.state.db_path
    root_path = request.app.root_path

    global_cost_map = _build_global_cost_map()

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

        for manager in managers:
            uid = manager['user_id']

            # Статистика за сегодня
            cursor = await db.execute('''
                SELECT COUNT(*) FROM keys_history
                WHERE manager_id = ? AND DATE(created_at) = DATE('now')
            ''', (uid,))
            manager['keys_today'] = (await cursor.fetchone())[0]

            # Себестоимости менеджера
            cursor = await db.execute(
                'SELECT expire_days, cost_price FROM manager_prices WHERE manager_id = ?',
                (uid,)
            )
            mp_map = {row[0]: row[1] for row in await cursor.fetchall()}

            # Долг: SUM(cost_price * count) по каждому expire_days
            cursor = await db.execute('''
                SELECT expire_days, COUNT(*) as cnt
                FROM keys_history
                WHERE manager_id = ?
                GROUP BY expire_days
            ''', (uid,))
            gross_debt = 0
            for row in await cursor.fetchall():
                ed = row[0] or 0
                cnt = row[1]
                cp = mp_map.get(ed, global_cost_map.get(ed, 0))
                gross_debt += cp * cnt

            # Сумма оплат
            cursor = await db.execute(
                'SELECT COALESCE(SUM(amount), 0) FROM manager_payments WHERE manager_id = ?',
                (uid,)
            )
            total_paid = (await cursor.fetchone())[0]

            manager['total_debt'] = gross_debt - total_paid
            manager['total_paid'] = total_paid
            manager['manager_profit'] = manager['total_revenue'] - gross_debt

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


@managers_router.post('/managers/{user_id}/prices')
async def update_manager_prices(request: Request, user_id: int):
    """Upsert себестоимостей менеджера"""
    db_path = request.app.state.db_path
    data = await request.json()
    prices_list = data.get('prices', [])

    async with aiosqlite.connect(db_path) as db:
        for item in prices_list:
            expire_days = int(item['expire_days'])
            cost_price = int(item['cost_price'])
            await db.execute('''
                INSERT INTO manager_prices (manager_id, expire_days, cost_price, updated_at)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(manager_id, expire_days)
                DO UPDATE SET cost_price = excluded.cost_price, updated_at = CURRENT_TIMESTAMP
            ''', (user_id, expire_days, cost_price))
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

        # Статистика по периодам подписки (с expire_days для JOIN)
        cursor = await db.execute('''
            SELECT period, expire_days, COUNT(*) as count, COALESCE(SUM(price), 0) as revenue
            FROM keys_history
            WHERE manager_id = ?
            GROUP BY period, expire_days
            ORDER BY count DESC
        ''', (user_id,))
        periods_stats = [dict(row) for row in await cursor.fetchall()]

        # Загружаем себестоимости менеджера
        cursor = await db.execute(
            'SELECT expire_days, cost_price FROM manager_prices WHERE manager_id = ?',
            (user_id,)
        )
        manager_prices_rows = await cursor.fetchall()
        manager_prices_map = {row[0]: row[1] for row in manager_prices_rows}

        # Оплаты менеджера
        cursor = await db.execute('''
            SELECT id, amount, paid_at, note
            FROM manager_payments
            WHERE manager_id = ?
            ORDER BY paid_at DESC
        ''', (user_id,))
        payments = [dict(row) for row in await cursor.fetchall()]

        total_paid = sum(p['amount'] for p in payments)

    # Глобальные цены как fallback
    global_cost_map = _build_global_cost_map()

    # Обогащаем periods_stats колонками cost_price / debt
    gross_debt = 0
    for p in periods_stats:
        ed = p.get('expire_days') or 0
        cp = manager_prices_map.get(ed, global_cost_map.get(ed, 0))
        p['cost_price'] = cp
        p['debt'] = cp * p['count']
        gross_debt += p['debt']

    current_debt = gross_debt - total_paid
    manager_profit = stats['total_revenue'] - gross_debt

    # Формируем данные для отображения цен в форме
    global_prices = load_prices()
    manager_prices_display = []
    for period_key, info in global_prices.items():
        days = info['days']
        manager_prices_display.append({
            'period_key': period_key,
            'name': info['name'],
            'days': days,
            'global_price': info['price'],
            'cost_price': manager_prices_map.get(days, ''),
        })

    return templates.TemplateResponse('manager_detail.html', {
        'request': request,
        'manager': manager,
        'stats': stats,
        'recent_keys': recent_keys,
        'periods_stats': periods_stats,
        'manager_prices_display': manager_prices_display,
        'debt_total': current_debt,
        'gross_debt': gross_debt,
        'total_paid': total_paid,
        'payments': payments,
        'manager_profit': manager_profit,
        'active': 'managers'
    })


@managers_router.post('/managers/{user_id}/payments')
async def add_payment(request: Request, user_id: int):
    """Записать оплату менеджера"""
    db_path = request.app.state.db_path
    data = await request.json()
    amount = int(data.get('amount', 0))
    note = data.get('note', '').strip()

    if amount <= 0:
        return JSONResponse({'success': False, 'error': 'Сумма должна быть больше 0'})

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            'INSERT INTO manager_payments (manager_id, amount, note) VALUES (?, ?, ?)',
            (user_id, amount, note or None)
        )
        await db.commit()

    return JSONResponse({'success': True})


@managers_router.post('/managers/{user_id}/payments/reset')
async def reset_debt(request: Request, user_id: int):
    """Погасить весь оставшийся долг одной записью"""
    db_path = request.app.state.db_path
    global_cost_map = _build_global_cost_map()

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Себестоимости менеджера
        cursor = await db.execute(
            'SELECT expire_days, cost_price FROM manager_prices WHERE manager_id = ?',
            (user_id,)
        )
        mp_map = {row[0]: row[1] for row in await cursor.fetchall()}

        # Валовый долг
        cursor = await db.execute('''
            SELECT expire_days, COUNT(*) as cnt
            FROM keys_history WHERE manager_id = ?
            GROUP BY expire_days
        ''', (user_id,))
        gross_debt = 0
        for row in await cursor.fetchall():
            ed = row[0] or 0
            cnt = row[1]
            cp = mp_map.get(ed, global_cost_map.get(ed, 0))
            gross_debt += cp * cnt

        # Уже оплачено
        cursor = await db.execute(
            'SELECT COALESCE(SUM(amount), 0) FROM manager_payments WHERE manager_id = ?',
            (user_id,)
        )
        total_paid = (await cursor.fetchone())[0]

        remaining = gross_debt - total_paid
        if remaining <= 0:
            return JSONResponse({'success': False, 'error': 'Долг уже погашен'})

        await db.execute(
            'INSERT INTO manager_payments (manager_id, amount, note) VALUES (?, ?, ?)',
            (user_id, remaining, 'Полное погашение долга')
        )
        await db.commit()

    return JSONResponse({'success': True, 'amount': remaining})


@managers_router.delete('/managers/{user_id}/payments/{payment_id}')
async def delete_payment(request: Request, user_id: int, payment_id: int):
    """Удалить запись об оплате"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            'SELECT id FROM manager_payments WHERE id = ? AND manager_id = ?',
            (payment_id, user_id)
        )
        if not await cursor.fetchone():
            return JSONResponse({'success': False, 'error': 'Оплата не найдена'})

        await db.execute('DELETE FROM manager_payments WHERE id = ?', (payment_id,))
        await db.commit()

    return JSONResponse({'success': True})


@managers_router.get('/managers/{user_id}/analytics')
async def manager_analytics(
    request: Request,
    user_id: int,
    group_by: str = Query('day', pattern='^(day|week|month)$'),
    date_from: str = Query('', alias='from'),
    date_to: str = Query('', alias='to')
):
    """Аналитика: ключи по дням/неделям/месяцам с фильтром дат"""
    db_path = request.app.state.db_path

    if group_by == 'day':
        date_expr = "DATE(created_at)"
    elif group_by == 'week':
        date_expr = "DATE(created_at, 'weekday 0', '-6 days')"
    else:
        date_expr = "strftime('%Y-%m', created_at)"

    conditions = ['manager_id = ?']
    params = [user_id]

    if date_from:
        conditions.append("DATE(created_at) >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("DATE(created_at) <= ?")
        params.append(date_to)

    where = ' AND '.join(conditions)

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        query = f'''
            SELECT {date_expr} as period,
                   COUNT(*) as keys_count,
                   COALESCE(SUM(price), 0) as revenue
            FROM keys_history
            WHERE {where}
            GROUP BY {date_expr}
            ORDER BY period DESC
        '''
        cursor = await db.execute(query, params)
        rows = [dict(row) for row in await cursor.fetchall()]

    total_keys = sum(r['keys_count'] for r in rows)
    total_revenue = sum(r['revenue'] for r in rows)

    return JSONResponse({
        'rows': rows,
        'total_keys': total_keys,
        'total_revenue': total_revenue
    })
