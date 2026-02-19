"""
Маршруты для управления веб-заказами
"""
import os
import json
import logging
import aiosqlite
from datetime import datetime
from fastapi import APIRouter, Request, Query, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger(__name__)

orders_router = APIRouter()
templates: Jinja2Templates = None

ORDERS_DB = '/root/manager_vpn/web_orders.db'


def setup_orders_router(tpl: Jinja2Templates):
    global templates
    templates = tpl


@orders_router.get('/orders', response_class=HTMLResponse)
async def orders_list(
    request: Request,
    page: int = Query(1, ge=1),
    status: str = Query('')
):
    """Список веб-заказов"""
    if not os.path.exists(ORDERS_DB):
        return templates.TemplateResponse('orders.html', {
            'request': request,
            'orders': [],
            'page': 1,
            'total_pages': 0,
            'total': 0,
            'status_filter': status,
            'stats': {'pending': 0, 'paid': 0, 'completed': 0, 'cancelled': 0},
            'active': 'orders'
        })

    limit = 25
    offset = (page - 1) * limit

    async with aiosqlite.connect(ORDERS_DB) as db:
        db.row_factory = aiosqlite.Row

        # Статистика по статусам
        cursor = await db.execute('''
            SELECT status, COUNT(*) as cnt FROM web_orders GROUP BY status
        ''')
        stats_rows = await cursor.fetchall()
        stats = {'pending': 0, 'paid': 0, 'completed': 0, 'cancelled': 0}
        for row in stats_rows:
            stats[row['status']] = row['cnt']

        # Запрос с фильтром
        if status:
            cursor = await db.execute(
                'SELECT * FROM web_orders WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?',
                (status, limit, offset)
            )
            count_cursor = await db.execute(
                'SELECT COUNT(*) FROM web_orders WHERE status = ?', (status,)
            )
        else:
            cursor = await db.execute(
                'SELECT * FROM web_orders ORDER BY created_at DESC LIMIT ? OFFSET ?',
                (limit, offset)
            )
            count_cursor = await db.execute('SELECT COUNT(*) FROM web_orders')

        orders = [dict(row) for row in await cursor.fetchall()]
        total = (await count_cursor.fetchone())[0]

    total_pages = (total + limit - 1) // limit

    return templates.TemplateResponse('orders.html', {
        'request': request,
        'orders': orders,
        'page': page,
        'total_pages': total_pages,
        'total': total,
        'status_filter': status,
        'stats': stats,
        'active': 'orders'
    })


@orders_router.get('/orders/{order_id}')
async def order_detail(request: Request, order_id: str):
    """Детали заказа"""
    if not os.path.exists(ORDERS_DB):
        return JSONResponse({'error': 'Orders DB not found'}, status_code=404)

    async with aiosqlite.connect(ORDERS_DB) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM web_orders WHERE id = ?', (order_id,))
        order = await cursor.fetchone()

        if not order:
            return JSONResponse({'error': 'Order not found'}, status_code=404)

        return JSONResponse({'order': dict(order)})


@orders_router.post('/orders/{order_id}/approve')
async def approve_order(request: Request, order_id: str):
    """Подтвердить заказ и создать ключ"""
    if not os.path.exists(ORDERS_DB):
        return JSONResponse({'success': False, 'error': 'Orders DB not found'})

    async with aiosqlite.connect(ORDERS_DB) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM web_orders WHERE id = ?', (order_id,))
        order = await cursor.fetchone()

        if not order:
            return JSONResponse({'success': False, 'error': 'Заказ не найден'})

        if order['status'] == 'completed':
            return JSONResponse({'success': False, 'error': 'Заказ уже выполнен'})

        order_dict = dict(order)

    # Создаём ключ используя существующую логику
    try:
        import sys
        sys.path.insert(0, '/root/manager_vpn')

        from bot.api.remote_xui import load_servers_config, create_client_via_panel

        config = load_servers_config()
        client_name = f"web_{order_id}_{order_dict['contact'].replace('@', '').replace('+', '')[:15]}"

        import uuid
        client_uuid = str(uuid.uuid4())
        expire_days = order_dict.get('days', 30)

        # Создаём на активных серверах
        created_on = []
        for server in config.get('servers', []):
            if not server.get('enabled', True):
                continue
            if not server.get('active_for_new', True):
                continue

            panel = server.get('panel', {})
            if not panel:
                continue

            result = await create_client_via_panel(
                server, client_uuid, client_name, expire_days, ip_limit=2
            )
            if result.get('success'):
                created_on.append(server.get('name'))

        if not created_on:
            return JSONResponse({'success': False, 'error': 'Не удалось создать ключ ни на одном сервере'})

        # Генерируем VLESS ключ
        vless_key = None
        for server in config.get('servers', []):
            if server.get('name') in created_on:
                inbounds = server.get('inbounds', {})
                main_inbound = inbounds.get('main', {})
                if main_inbound:
                    domain = server.get('domain', server.get('ip', ''))
                    port = server.get('port', 443)
                    pbk = main_inbound.get('pbk', '')
                    sni = main_inbound.get('sni', '')
                    sid = main_inbound.get('sid', '')

                    vless_key = (
                        f"vless://{client_uuid}@{domain}:{port}?"
                        f"type=tcp&security=reality&pbk={pbk}&fp=chrome&sni={sni}&sid={sid}&spx=%2F"
                        f"#{server.get('name', 'VPN')}"
                    )
                    break

        # Обновляем заказ
        async with aiosqlite.connect(ORDERS_DB) as db:
            await db.execute('''
                UPDATE web_orders
                SET status = 'completed', vless_key = ?, confirmed_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (vless_key, order_id))
            await db.commit()

        return JSONResponse({
            'success': True,
            'message': f'Заказ {order_id} выполнен',
            'vless_key': vless_key,
            'created_on': created_on
        })

    except Exception as e:
        logger.error(f"Error approving order {order_id}: {e}")
        return JSONResponse({'success': False, 'error': str(e)})


@orders_router.post('/orders/{order_id}/reject')
async def reject_order(request: Request, order_id: str, reason: str = Form('')):
    """Отклонить заказ"""
    if not os.path.exists(ORDERS_DB):
        return JSONResponse({'success': False, 'error': 'Orders DB not found'})

    async with aiosqlite.connect(ORDERS_DB) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('SELECT * FROM web_orders WHERE id = ?', (order_id,))
        order = await cursor.fetchone()

        if not order:
            return JSONResponse({'success': False, 'error': 'Заказ не найден'})

        await db.execute('''
            UPDATE web_orders
            SET status = 'cancelled', admin_comment = ?
            WHERE id = ?
        ''', (reason, order_id))
        await db.commit()

    return JSONResponse({
        'success': True,
        'message': f'Заказ {order_id} отклонён'
    })


@orders_router.get('/api/orders/stats')
async def orders_stats():
    """Статистика заказов"""
    if not os.path.exists(ORDERS_DB):
        return JSONResponse({'stats': {'pending': 0, 'paid': 0, 'completed': 0, 'cancelled': 0}})

    async with aiosqlite.connect(ORDERS_DB) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute('''
            SELECT status, COUNT(*) as cnt FROM web_orders GROUP BY status
        ''')
        rows = await cursor.fetchall()

        stats = {'pending': 0, 'paid': 0, 'completed': 0, 'cancelled': 0}
        for row in rows:
            stats[row['status']] = row['cnt']

    return JSONResponse({'stats': stats})
