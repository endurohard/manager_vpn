"""
FastAPI Web Admin Panel
"""
import os
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from web_admin.routes.auth import AuthMiddleware, auth_router, setup_auth_router
from web_admin.routes.clients import clients_router, setup_clients_router
from web_admin.routes.stats import stats_router, setup_stats_router
from web_admin.routes.keys import keys_router, setup_keys_router
from web_admin.routes.settings import settings_router, setup_settings_router
from web_admin.routes.servers import servers_router, setup_servers_router
from web_admin.routes.managers import managers_router, setup_managers_router
from web_admin.routes.orders import orders_router, setup_orders_router


def create_app(db_path: str = None, admin_password: str = None, root_path: str = None) -> FastAPI:
    """Создание FastAPI приложения"""
    # Default database path
    if db_path is None:
        db_path = os.getenv('DATABASE_PATH', '/root/manager_vpn/bot_database.db')

    # Default root path (for nginx proxy)
    # root_path is used for generating correct URLs in templates
    if root_path is None:
        root_path = os.getenv('ADMIN_ROOT_PATH', '/manager')

    app = FastAPI(
        title="VPN Manager Admin",
        root_path=root_path
    )

    # Сохраняем путь к БД в state
    app.state.db_path = db_path
    app.state.admin_password = admin_password or os.getenv('ADMIN_PASSWORD', 'admin')

    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    templates = Jinja2Templates(directory=os.path.join(BASE_DIR, 'templates'))

    # Статические файлы
    app.mount('/static', StaticFiles(directory=os.path.join(BASE_DIR, 'static')), name='static')

    # Middleware авторизации
    app.add_middleware(AuthMiddleware)

    # Настройка и подключение роутеров
    setup_auth_router(templates)
    app.include_router(auth_router)

    setup_clients_router(templates)
    app.include_router(clients_router)

    setup_stats_router(templates)
    app.include_router(stats_router)

    setup_keys_router(templates)
    app.include_router(keys_router)

    setup_settings_router(templates)
    app.include_router(settings_router)

    setup_servers_router(templates)
    app.include_router(servers_router)

    setup_managers_router(templates)
    app.include_router(managers_router)

    setup_orders_router(templates)
    app.include_router(orders_router)

    @app.get('/', response_class=HTMLResponse)
    async def index(request: Request):
        """Главная страница - редирект на статистику"""
        return RedirectResponse(url=f'{root_path}/stats', status_code=302)

    return app


# Создаём app для uvicorn
app = create_app()


# Для запуска напрямую
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=8082, reload=True)
