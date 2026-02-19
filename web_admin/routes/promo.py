"""
Маршруты для промокодов
"""
import aiosqlite
from datetime import datetime, timedelta
from fastapi import APIRouter, Form, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

promo_router = APIRouter()
templates: Jinja2Templates = None


def setup_promo_router(tpl: Jinja2Templates):
    global templates
    templates = tpl


@promo_router.get('/promo', response_class=HTMLResponse)
async def promo_list(request: Request):
    """Список промокодов"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row

        cursor = await db.execute('''
            SELECT p.*, m.full_name as creator_name,
                   (SELECT COUNT(*) FROM promo_uses WHERE promo_id = p.id) as uses_count
            FROM promo_codes p
            LEFT JOIN managers m ON p.created_by = m.user_id
            ORDER BY p.created_at DESC
        ''')
        promos = [dict(row) for row in await cursor.fetchall()]

    return templates.TemplateResponse('promo.html', {
        'request': request,
        'promos': promos,
        'active': 'promo'
    })


@promo_router.post('/promo/new')
async def promo_create(
    request: Request,
    code: str = Form(...),
    discount_type: str = Form(...),
    discount_value: int = Form(...),
    max_uses: int = Form(0),
    valid_days: int = Form(0)
):
    """Создание промокода"""
    db_path = request.app.state.db_path

    valid_until = None
    if valid_days > 0:
        valid_until = (datetime.now() + timedelta(days=valid_days)).isoformat()

    async with aiosqlite.connect(db_path) as db:
        await db.execute('''
            INSERT INTO promo_codes
            (code, discount_type, discount_value, max_uses, valid_until, is_active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        ''', (code.upper(), discount_type, discount_value, max_uses, valid_until, datetime.now().isoformat()))
        await db.commit()

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JSONResponse({'success': True})

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url='/admin/promo', status_code=302)


@promo_router.post('/promo/{promo_id}/edit')
async def promo_edit(
    request: Request,
    promo_id: int,
    code: str = Form(...),
    discount_type: str = Form(...),
    discount_value: int = Form(...),
    max_uses: int = Form(0),
    is_active: bool = Form(False)
):
    """Редактирование промокода"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        await db.execute('''
            UPDATE promo_codes
            SET code = ?, discount_type = ?, discount_value = ?, max_uses = ?, is_active = ?
            WHERE id = ?
        ''', (code.upper(), discount_type, discount_value, max_uses, 1 if is_active else 0, promo_id))
        await db.commit()

    if request.headers.get('x-requested-with') == 'XMLHttpRequest':
        return JSONResponse({'success': True})

    from fastapi.responses import RedirectResponse
    return RedirectResponse(url='/admin/promo', status_code=302)


@promo_router.post('/promo/{promo_id}/delete')
async def promo_delete(request: Request, promo_id: int):
    """Удаление промокода"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        await db.execute('DELETE FROM promo_codes WHERE id = ?', (promo_id,))
        await db.commit()

    return JSONResponse({'success': True})


@promo_router.post('/promo/{promo_id}/toggle')
async def promo_toggle(request: Request, promo_id: int):
    """Переключение активности промокода"""
    db_path = request.app.state.db_path

    async with aiosqlite.connect(db_path) as db:
        await db.execute('''
            UPDATE promo_codes SET is_active = NOT is_active WHERE id = ?
        ''', (promo_id,))
        await db.commit()

    return JSONResponse({'success': True})
