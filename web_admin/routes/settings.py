"""
Настройки админ-панели
"""
import hashlib
import json
import os
import aiosqlite
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

settings_router = APIRouter()
templates: Jinja2Templates = None

PRICES_FILE = '/root/manager_vpn/prices.json'
REQUISITES_FILE = '/root/manager_vpn/requisites.json'

DEFAULT_PRICES = {
    "1_month": {"name": "Месяц", "days": 30, "price": 300},
    "3_months": {"name": "3 месяца", "days": 90, "price": 800},
    "6_months": {"name": "6 месяцев", "days": 180, "price": 1500},
    "1_year": {"name": "Год", "days": 365, "price": 2500}
}


def setup_settings_router(tpl: Jinja2Templates):
    global templates
    templates = tpl


def load_prices():
    """Загрузить цены"""
    try:
        if os.path.exists(PRICES_FILE):
            with open(PRICES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except:
        pass
    return DEFAULT_PRICES.copy()


def save_prices(prices: dict):
    """Сохранить цены"""
    with open(PRICES_FILE, 'w', encoding='utf-8') as f:
        json.dump(prices, f, ensure_ascii=False, indent=2)


def load_requisites():
    """Загрузить реквизиты"""
    try:
        if os.path.exists(REQUISITES_FILE):
            with open(REQUISITES_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except:
        pass
    return {
        'card_number': '',
        'card_holder': '',
        'bank_name': '',
        'phone': '',
        'sbp_banks': ''
    }


def save_requisites(requisites: dict):
    """Сохранить реквизиты"""
    with open(REQUISITES_FILE, 'w', encoding='utf-8') as f:
        json.dump(requisites, f, ensure_ascii=False, indent=2)


def hash_password(password: str) -> str:
    """Хеширование пароля"""
    return hashlib.sha256(password.encode()).hexdigest()


async def get_admin_credentials(db_path: str) -> tuple:
    """Получить логин и пароль из БД"""
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute(
            "SELECT value FROM admin_settings WHERE key = 'admin_login'"
        )
        row = await cursor.fetchone()
        login = row[0] if row else 'admin'

        cursor = await db.execute(
            "SELECT value FROM admin_settings WHERE key = 'admin_password_hash'"
        )
        row = await cursor.fetchone()
        password_hash = row[0] if row else None

    return login, password_hash


async def verify_password(db_path: str, password: str, default_password: str) -> bool:
    """Проверить пароль"""
    _, stored_hash = await get_admin_credentials(db_path)

    if stored_hash:
        return hash_password(password) == stored_hash
    else:
        # Если хеш не установлен, используем пароль из переменной окружения
        return password == default_password


async def set_admin_credentials(db_path: str, login: str, password: str):
    """Установить логин и пароль"""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO admin_settings (key, value) VALUES ('admin_login', ?)",
            (login,)
        )
        await db.execute(
            "INSERT OR REPLACE INTO admin_settings (key, value) VALUES ('admin_password_hash', ?)",
            (hash_password(password),)
        )
        await db.commit()


@settings_router.get('/settings', response_class=HTMLResponse)
async def settings_page(request: Request):
    """Страница настроек"""
    db_path = request.app.state.db_path
    login, _ = await get_admin_credentials(db_path)
    prices = load_prices()
    requisites = load_requisites()

    return templates.TemplateResponse('settings.html', {
        'request': request,
        'login': login,
        'prices': prices,
        'requisites': requisites,
        'active': 'settings',
        'success': None,
        'error': None
    })


@settings_router.post('/settings/credentials')
async def update_credentials(
    request: Request,
    login: str = Form(...),
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...)
):
    """Обновление логина и пароля"""
    db_path = request.app.state.db_path
    default_password = request.app.state.admin_password

    # Проверяем текущий пароль
    if not await verify_password(db_path, current_password, default_password):
        return templates.TemplateResponse('settings.html', {
            'request': request,
            'login': login,
            'active': 'settings',
            'success': None,
            'error': 'Неверный текущий пароль'
        })

    # Проверяем совпадение паролей
    if new_password != confirm_password:
        return templates.TemplateResponse('settings.html', {
            'request': request,
            'login': login,
            'active': 'settings',
            'success': None,
            'error': 'Новые пароли не совпадают'
        })

    # Проверяем длину пароля
    if len(new_password) < 4:
        return templates.TemplateResponse('settings.html', {
            'request': request,
            'login': login,
            'active': 'settings',
            'success': None,
            'error': 'Пароль должен быть не менее 4 символов'
        })

    # Сохраняем новые данные
    await set_admin_credentials(db_path, login, new_password)

    current_login, _ = await get_admin_credentials(db_path)

    return templates.TemplateResponse('settings.html', {
        'request': request,
        'login': current_login,
        'prices': load_prices(),
        'requisites': load_requisites(),
        'active': 'settings',
        'success': 'Учётные данные успешно обновлены',
        'error': None
    })


# ============ Управление ценами ============

@settings_router.post('/settings/prices')
async def update_prices(request: Request):
    """Обновление цен"""
    form = await request.form()
    prices = load_prices()

    for period_key in prices.keys():
        price_field = f'price_{period_key}'
        if price_field in form:
            try:
                new_price = int(form[price_field])
                if new_price >= 0:
                    prices[period_key]['price'] = new_price
            except:
                pass

    save_prices(prices)

    return JSONResponse({'success': True, 'message': 'Цены обновлены'})


@settings_router.get('/api/prices')
async def get_prices():
    """Получить текущие цены"""
    return JSONResponse({'prices': load_prices()})


# ============ Управление реквизитами ============

@settings_router.post('/settings/requisites')
async def update_requisites(request: Request):
    """Обновление реквизитов"""
    form = await request.form()

    requisites = {
        'card_number': form.get('card_number', ''),
        'card_holder': form.get('card_holder', ''),
        'bank_name': form.get('bank_name', ''),
        'phone': form.get('phone', ''),
        'sbp_banks': form.get('sbp_banks', '')
    }

    save_requisites(requisites)

    return JSONResponse({'success': True, 'message': 'Реквизиты обновлены'})


@settings_router.get('/api/requisites')
async def get_requisites():
    """Получить текущие реквизиты"""
    return JSONResponse({'requisites': load_requisites()})
