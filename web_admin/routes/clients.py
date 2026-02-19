"""
Маршруты для поиска клиентов в истории ключей
"""
import aiosqlite
from datetime import datetime
from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

clients_router = APIRouter()
templates: Jinja2Templates = None


def setup_clients_router(tpl: Jinja2Templates):
    global templates
    templates = tpl


@clients_router.get('/clients', response_class=HTMLResponse)
async def clients_list(
    request: Request,
    page: int = Query(1, ge=1),
    search: str = Query(''),
    manager_id: int = Query(None)
):
    """Список клиентов из истории ключей с поиском и пагинацией"""
    db_path = request.app.state.db_path
    limit = 20
    offset = (page - 1) * limit

    # Запрос с группировкой по email/телефону
    base_query = '''
        SELECT
            kh.client_email,
            kh.phone_number,
            COUNT(*) as keys_count,
            MAX(kh.created_at) as last_key_date,
            COALESCE(SUM(kh.price), 0) as total_spent,
            GROUP_CONCAT(DISTINCT kh.period) as periods,
            COALESCE(m.custom_name, m.full_name, m.username) as last_manager
        FROM keys_history kh
        LEFT JOIN managers m ON kh.manager_id = m.user_id
    '''
    count_query = '''
        SELECT COUNT(DISTINCT COALESCE(kh.client_email, '') || COALESCE(kh.phone_number, ''))
        FROM keys_history kh
    '''

    conditions = []
    params = []

    # Фильтр по менеджеру
    if manager_id:
        conditions.append('kh.manager_id = ?')
        params.append(manager_id)

    # Поиск
    if search:
        search_pattern = f'%{search}%'
        conditions.append('(kh.client_email LIKE ? OR kh.phone_number LIKE ? OR kh.client_id LIKE ?)')
        params.extend([search_pattern] * 3)

    if conditions:
        where_clause = ' WHERE ' + ' AND '.join(conditions)
        base_query += where_clause
        count_query += where_clause.replace('kh.', 'kh.')

    base_query += ' GROUP BY COALESCE(kh.client_email, kh.phone_number)'
    base_query += ' ORDER BY last_key_date DESC LIMIT ? OFFSET ?'

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Получаем клиентов
        cursor = await db.execute(base_query, params + [limit, offset])
        clients_rows = await cursor.fetchall()

        # Получаем общее количество
        cursor = await db.execute(count_query, params)
        total = (await cursor.fetchone())[0]

        # Получаем менеджеров для фильтра
        cursor = await db.execute('''
            SELECT user_id, COALESCE(custom_name, full_name, username) as name
            FROM managers WHERE is_active = 1
            ORDER BY name
        ''')
        managers = [dict(row) for row in await cursor.fetchall()]

    # Преобразуем в список словарей
    clients = [dict(row) for row in clients_rows]

    total_pages = (total + limit - 1) // limit

    # Пагинация
    def build_pages(current, total_p, neighbors=2):
        pages = []
        for p in range(1, total_p + 1):
            if p == 1 or p == total_p or abs(p - current) <= neighbors:
                if pages and pages[-1] != '...' and p - (pages[-1] if isinstance(pages[-1], int) else 0) > 1:
                    pages.append('...')
                pages.append(p)
        return pages

    pages = build_pages(page, total_pages) if total_pages > 0 else []

    return templates.TemplateResponse('clients.html', {
        'request': request,
        'clients': clients,
        'page': page,
        'total_pages': total_pages,
        'total': total,
        'search': search,
        'manager_id': manager_id,
        'managers': managers,
        'pages': pages,
        'active': 'clients'
    })


@clients_router.get('/clients/search/api')
async def clients_search_api(
    request: Request,
    q: str = Query('', min_length=1)
):
    """API для быстрого поиска клиентов"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        search_pattern = f'%{q}%'
        cursor = await db.execute('''
            SELECT
                client_email,
                phone_number,
                COUNT(*) as keys_count,
                MAX(created_at) as last_key_date
            FROM keys_history
            WHERE client_email LIKE ? OR phone_number LIKE ? OR client_id LIKE ?
            GROUP BY COALESCE(client_email, phone_number)
            LIMIT 10
        ''', (search_pattern, search_pattern, search_pattern))

        results = [dict(row) for row in await cursor.fetchall()]

    return JSONResponse({'results': results})


@clients_router.get('/clients/{client_email:path}', response_class=HTMLResponse)
async def client_detail(request: Request, client_email: str):
    """Детальная страница клиента - все ключи по email/телефону"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        # Получаем все ключи клиента
        cursor = await db.execute('''
            SELECT kh.*,
                   COALESCE(m.custom_name, m.full_name, m.username) as manager_name
            FROM keys_history kh
            LEFT JOIN managers m ON kh.manager_id = m.user_id
            WHERE kh.client_email = ? OR kh.phone_number = ?
            ORDER BY kh.created_at DESC
        ''', (client_email, client_email))
        keys = [dict(row) for row in await cursor.fetchall()]

        if not keys:
            return HTMLResponse("Клиент не найден", status_code=404)

        # Агрегированные данные
        client = {
            'email': keys[0].get('client_email', ''),
            'phone': keys[0].get('phone_number', ''),
            'keys_count': len(keys),
            'total_spent': sum(k.get('price', 0) or 0 for k in keys),
            'first_key': keys[-1].get('created_at') if keys else None,
            'last_key': keys[0].get('created_at') if keys else None,
        }

    return templates.TemplateResponse('client_detail.html', {
        'request': request,
        'client': client,
        'keys': keys,
        'active': 'clients'
    })
