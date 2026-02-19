"""
Авторизация в админ-панели
"""
import hashlib
import secrets
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

auth_router = APIRouter()
templates: Jinja2Templates = None

# Простое хранилище сессий
sessions = {}


def setup_auth_router(tpl: Jinja2Templates):
    global templates
    templates = tpl


async def verify_admin_password(db_path: str, password: str, default_password: str) -> bool:
    """Проверить пароль админа"""
    from web_admin.routes.settings import verify_password
    return await verify_password(db_path, password, default_password)


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware для проверки авторизации"""

    async def dispatch(self, request: Request, call_next):
        # Получаем root_path из приложения
        root_path = request.app.root_path or ''

        path = request.url.path

        # Убираем root_path из пути для проверки
        if root_path and path.startswith(root_path):
            check_path = path[len(root_path):]
        else:
            check_path = path

        # Пути, не требующие авторизации
        public_paths = ['/login', '/static', '/health']

        # Проверяем публичные пути
        for public in public_paths:
            if check_path.startswith(public):
                return await call_next(request)

        # Проверяем сессию
        session_id = request.cookies.get('session_id')
        if not session_id or session_id not in sessions:
            return RedirectResponse(url=f'{root_path}/login', status_code=302)

        return await call_next(request)


@auth_router.get('/login', response_class=HTMLResponse)
async def login_page(request: Request):
    """Страница входа"""
    return templates.TemplateResponse('login.html', {
        'request': request,
        'error': None
    })


@auth_router.post('/login')
async def login(request: Request, password: str = Form(...)):
    """Обработка входа"""
    db_path = request.app.state.db_path
    default_password = request.app.state.admin_password
    root_path = request.app.root_path or ''

    # Проверяем пароль через БД или дефолтный
    if await verify_admin_password(db_path, password, default_password):
        # Создаём сессию
        session_id = secrets.token_hex(32)
        sessions[session_id] = True

        response = RedirectResponse(url=f'{root_path}/stats', status_code=302)
        response.set_cookie(
            key='session_id',
            value=session_id,
            httponly=True,
            max_age=86400 * 7  # 7 дней
        )
        return response

    return templates.TemplateResponse('login.html', {
        'request': request,
        'error': 'Неверный пароль'
    })


@auth_router.get('/logout')
async def logout(request: Request):
    """Выход из системы"""
    root_path = request.app.root_path or ''
    session_id = request.cookies.get('session_id')
    if session_id and session_id in sessions:
        del sessions[session_id]

    response = RedirectResponse(url=f'{root_path}/login', status_code=302)
    response.delete_cookie('session_id')
    return response
