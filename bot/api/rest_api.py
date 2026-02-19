"""
REST API для внешних интеграций
"""
import os
import json
import logging
import hmac
import hashlib
from datetime import datetime
from typing import Dict, Any, Optional
from functools import wraps
from aiohttp import web

logger = logging.getLogger(__name__)


class RestAPI:
    """REST API сервер для внешних интеграций"""

    def __init__(
        self,
        client_manager=None,
        promo_manager=None,
        analytics_manager=None,
        audit_manager=None,
        api_key: Optional[str] = None
    ):
        self.client_manager = client_manager
        self.promo_manager = promo_manager
        self.analytics_manager = analytics_manager
        self.audit_manager = audit_manager
        self.api_key = api_key or os.getenv('API_KEY', 'change-me-in-production')
        self.app = web.Application(middlewares=[self._error_middleware])
        self._setup_routes()

    def _setup_routes(self):
        """Настройка маршрутов"""
        # Здоровье
        self.app.router.add_get('/health', self.health_check)
        self.app.router.add_get('/api/v1/health', self.health_check)

        # Клиенты
        self.app.router.add_get('/api/v1/clients', self.list_clients)
        self.app.router.add_get('/api/v1/clients/{uuid}', self.get_client)
        self.app.router.add_post('/api/v1/clients', self.create_client)
        self.app.router.add_put('/api/v1/clients/{uuid}', self.update_client)
        self.app.router.add_delete('/api/v1/clients/{uuid}', self.delete_client)
        self.app.router.add_post('/api/v1/clients/{uuid}/extend', self.extend_subscription)
        self.app.router.add_get('/api/v1/clients/expiring', self.get_expiring_clients)

        # Промокоды
        self.app.router.add_get('/api/v1/promo', self.list_promos)
        self.app.router.add_post('/api/v1/promo', self.create_promo)
        self.app.router.add_get('/api/v1/promo/{code}', self.get_promo)
        self.app.router.add_post('/api/v1/promo/{code}/validate', self.validate_promo)
        self.app.router.add_delete('/api/v1/promo/{code}', self.delete_promo)

        # Аналитика
        self.app.router.add_get('/api/v1/stats/dashboard', self.get_dashboard)
        self.app.router.add_get('/api/v1/stats/revenue', self.get_revenue_stats)
        self.app.router.add_get('/api/v1/stats/managers', self.get_manager_stats)
        self.app.router.add_get('/api/v1/stats/report', self.generate_report)

        # Аудит
        self.app.router.add_get('/api/v1/audit', self.get_audit_logs)

    @web.middleware
    async def _error_middleware(self, request, handler):
        """Middleware для обработки ошибок"""
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except Exception as e:
            logger.error(f"API Error: {e}")
            return web.json_response(
                {'error': 'Internal server error', 'message': str(e)},
                status=500
            )

    def _require_auth(self, handler):
        """Декоратор для проверки авторизации"""
        @wraps(handler)
        async def wrapper(request):
            auth_header = request.headers.get('Authorization', '')

            if not auth_header.startswith('Bearer '):
                return web.json_response(
                    {'error': 'Missing or invalid Authorization header'},
                    status=401
                )

            token = auth_header[7:]
            if not hmac.compare_digest(token, self.api_key):
                return web.json_response(
                    {'error': 'Invalid API key'},
                    status=401
                )

            return await handler(request)
        return wrapper

    def _json_response(self, data: Any, status: int = 200):
        """Создание JSON ответа"""
        return web.json_response(
            data,
            status=status,
            dumps=lambda x: json.dumps(x, default=str, ensure_ascii=False)
        )

    # ==================== HEALTH ====================

    async def health_check(self, request):
        """Проверка здоровья"""
        return self._json_response({
            'status': 'ok',
            'timestamp': datetime.now().isoformat()
        })

    # ==================== CLIENTS ====================

    async def list_clients(self, request):
        """Список клиентов"""
        if not self.client_manager:
            return self._json_response({'error': 'Client manager not configured'}, 500)

        status = request.query.get('status')
        limit = int(request.query.get('limit', 100))
        offset = int(request.query.get('offset', 0))

        clients = await self.client_manager.search_clients(
            status=status,
            limit=limit,
            offset=offset
        )

        return self._json_response({
            'clients': clients,
            'count': len(clients),
            'limit': limit,
            'offset': offset
        })

    async def get_client(self, request):
        """Получение клиента по UUID"""
        if not self.client_manager:
            return self._json_response({'error': 'Client manager not configured'}, 500)

        uuid = request.match_info['uuid']
        client = await self.client_manager.get_client(uuid=uuid)

        if not client:
            return self._json_response({'error': 'Client not found'}, 404)

        # Получаем серверы клиента
        servers = await self.client_manager.get_client_servers(client['id'])
        client['servers'] = servers

        return self._json_response({'client': client})

    async def create_client(self, request):
        """Создание клиента"""
        if not self.client_manager:
            return self._json_response({'error': 'Client manager not configured'}, 500)

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return self._json_response({'error': 'Invalid JSON'}, 400)

        required = ['uuid', 'email']
        for field in required:
            if field not in data:
                return self._json_response({'error': f'Missing required field: {field}'}, 400)

        try:
            client_id = await self.client_manager.create_client(
                uuid=data['uuid'],
                email=data['email'],
                phone=data.get('phone'),
                name=data.get('name'),
                telegram_id=data.get('telegram_id'),
                expire_time=data.get('expire_time'),
                created_by=data.get('created_by'),
                current_server=data.get('current_server')
            )

            client = await self.client_manager.get_client(client_id=client_id)
            return self._json_response({'client': client}, 201)

        except Exception as e:
            return self._json_response({'error': str(e)}, 400)

    async def update_client(self, request):
        """Обновление клиента"""
        if not self.client_manager:
            return self._json_response({'error': 'Client manager not configured'}, 500)

        uuid = request.match_info['uuid']
        client = await self.client_manager.get_client(uuid=uuid)

        if not client:
            return self._json_response({'error': 'Client not found'}, 404)

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return self._json_response({'error': 'Invalid JSON'}, 400)

        # Обновляем только разрешённые поля
        allowed_fields = ['phone', 'name', 'telegram_id', 'status', 'expire_time', 'ip_limit', 'group_id']
        update_data = {k: v for k, v in data.items() if k in allowed_fields}

        if update_data:
            await self.client_manager.update_client(client['id'], **update_data)

        updated_client = await self.client_manager.get_client(uuid=uuid)
        return self._json_response({'client': updated_client})

    async def delete_client(self, request):
        """Удаление клиента"""
        if not self.client_manager:
            return self._json_response({'error': 'Client manager not configured'}, 500)

        uuid = request.match_info['uuid']
        client = await self.client_manager.get_client(uuid=uuid)

        if not client:
            return self._json_response({'error': 'Client not found'}, 404)

        await self.client_manager.delete_client(client['id'])
        return self._json_response({'message': 'Client deleted'})

    async def extend_subscription(self, request):
        """Продление подписки"""
        if not self.client_manager:
            return self._json_response({'error': 'Client manager not configured'}, 500)

        uuid = request.match_info['uuid']
        client = await self.client_manager.get_client(uuid=uuid)

        if not client:
            return self._json_response({'error': 'Client not found'}, 404)

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return self._json_response({'error': 'Invalid JSON'}, 400)

        days = data.get('days', 30)
        period = data.get('period', 'custom')
        price = data.get('price', 0)
        manager_id = data.get('manager_id')

        await self.client_manager.extend_subscription(
            client_id=client['id'],
            days=days,
            period=period,
            price=price,
            manager_id=manager_id
        )

        updated_client = await self.client_manager.get_client(uuid=uuid)
        return self._json_response({'client': updated_client})

    async def get_expiring_clients(self, request):
        """Клиенты с истекающей подпиской"""
        if not self.client_manager:
            return self._json_response({'error': 'Client manager not configured'}, 500)

        days = int(request.query.get('days', 7))
        clients = await self.client_manager.get_expiring_clients(days)

        return self._json_response({
            'clients': clients,
            'count': len(clients),
            'days_threshold': days
        })

    # ==================== PROMO ====================

    async def list_promos(self, request):
        """Список промокодов"""
        if not self.promo_manager:
            return self._json_response({'error': 'Promo manager not configured'}, 500)

        active_only = request.query.get('active', 'true').lower() == 'true'
        promos = await self.promo_manager.get_active_promos() if active_only else await self.promo_manager.get_all_promos()

        return self._json_response({'promos': promos})

    async def create_promo(self, request):
        """Создание промокода"""
        if not self.promo_manager:
            return self._json_response({'error': 'Promo manager not configured'}, 500)

        try:
            data = await request.json()
        except json.JSONDecodeError:
            return self._json_response({'error': 'Invalid JSON'}, 400)

        required = ['code', 'discount_type', 'discount_value']
        for field in required:
            if field not in data:
                return self._json_response({'error': f'Missing required field: {field}'}, 400)

        try:
            promo_id = await self.promo_manager.create_promo(
                code=data['code'],
                discount_type=data['discount_type'],
                discount_value=data['discount_value'],
                description=data.get('description'),
                max_uses=data.get('max_uses', 0),
                valid_from=datetime.fromisoformat(data['valid_from']) if data.get('valid_from') else None,
                valid_until=datetime.fromisoformat(data['valid_until']) if data.get('valid_until') else None,
                min_period=data.get('min_period'),
                min_amount=data.get('min_amount', 0),
                applicable_periods=data.get('applicable_periods'),
                created_by=data.get('created_by')
            )

            promo = await self.promo_manager.get_promo(data['code'])
            return self._json_response({'promo': promo}, 201)

        except Exception as e:
            return self._json_response({'error': str(e)}, 400)

    async def get_promo(self, request):
        """Получение промокода"""
        if not self.promo_manager:
            return self._json_response({'error': 'Promo manager not configured'}, 500)

        code = request.match_info['code']
        promo = await self.promo_manager.get_promo(code)

        if not promo:
            return self._json_response({'error': 'Promo code not found'}, 404)

        # Добавляем статистику
        stats = await self.promo_manager.get_promo_stats(promo['id'])
        promo['stats'] = stats

        return self._json_response({'promo': promo})

    async def validate_promo(self, request):
        """Валидация промокода"""
        if not self.promo_manager:
            return self._json_response({'error': 'Promo manager not configured'}, 500)

        code = request.match_info['code']

        try:
            data = await request.json()
        except json.JSONDecodeError:
            data = {}

        period = data.get('period')
        amount = data.get('amount', 0)

        is_valid, error = await self.promo_manager.validate_promo(code, period, amount)

        if not is_valid:
            return self._json_response({
                'valid': False,
                'error': error
            })

        promo = await self.promo_manager.get_promo(code)
        discount = await self.promo_manager.calculate_discount(code, amount, period)

        return self._json_response({
            'valid': True,
            'promo': promo,
            'discount': discount
        })

    async def delete_promo(self, request):
        """Удаление промокода"""
        if not self.promo_manager:
            return self._json_response({'error': 'Promo manager not configured'}, 500)

        code = request.match_info['code']
        promo = await self.promo_manager.get_promo(code)

        if not promo:
            return self._json_response({'error': 'Promo code not found'}, 404)

        await self.promo_manager.deactivate_promo(promo['id'])
        return self._json_response({'message': 'Promo code deactivated'})

    # ==================== ANALYTICS ====================

    async def get_dashboard(self, request):
        """Данные дашборда"""
        if not self.analytics_manager:
            return self._json_response({'error': 'Analytics not configured'}, 500)

        stats = await self.analytics_manager.get_dashboard_stats()
        return self._json_response(stats)

    async def get_revenue_stats(self, request):
        """Статистика выручки"""
        if not self.analytics_manager:
            return self._json_response({'error': 'Analytics not configured'}, 500)

        from_date = request.query.get('from')
        to_date = request.query.get('to')
        group_by = request.query.get('group_by', 'day')

        if from_date:
            from_date = datetime.fromisoformat(from_date)
        else:
            from_date = datetime.now() - timedelta(days=30)

        if to_date:
            to_date = datetime.fromisoformat(to_date)
        else:
            to_date = datetime.now()

        stats = await self.analytics_manager.get_revenue_report(
            from_date, to_date, group_by
        )
        return self._json_response({'revenue': stats})

    async def get_manager_stats(self, request):
        """Статистика менеджеров"""
        if not self.analytics_manager:
            return self._json_response({'error': 'Analytics not configured'}, 500)

        manager_id = request.query.get('manager_id')
        if manager_id:
            manager_id = int(manager_id)

        stats = await self.analytics_manager.get_manager_stats(manager_id)
        return self._json_response({'managers': stats})

    async def generate_report(self, request):
        """Генерация отчёта"""
        if not self.analytics_manager:
            return self._json_response({'error': 'Analytics not configured'}, 500)

        report_type = request.query.get('type', 'full')
        from_date = request.query.get('from')
        to_date = request.query.get('to')

        if from_date:
            from_date = datetime.fromisoformat(from_date)
        else:
            from_date = datetime.now() - timedelta(days=30)

        if to_date:
            to_date = datetime.fromisoformat(to_date)
        else:
            to_date = datetime.now()

        report = await self.analytics_manager.generate_report(
            report_type, from_date, to_date
        )
        return self._json_response(report)

    # ==================== AUDIT ====================

    async def get_audit_logs(self, request):
        """Получение аудит логов"""
        if not self.audit_manager:
            return self._json_response({'error': 'Audit not configured'}, 500)

        user_id = request.query.get('user_id')
        action = request.query.get('action')
        entity_type = request.query.get('entity_type')
        limit = int(request.query.get('limit', 100))
        offset = int(request.query.get('offset', 0))

        logs = await self.audit_manager.get_logs(
            user_id=int(user_id) if user_id else None,
            action=action,
            entity_type=entity_type,
            limit=limit,
            offset=offset
        )

        return self._json_response({
            'logs': logs,
            'count': len(logs),
            'limit': limit,
            'offset': offset
        })

    # ==================== RUN ====================

    def run(self, host: str = '0.0.0.0', port: int = 8081):
        """Запуск API сервера"""
        web.run_app(self.app, host=host, port=port)

    async def start_background(self, host: str = '0.0.0.0', port: int = 8081):
        """Запуск API сервера в фоне"""
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()
        logger.info(f"REST API запущен на http://{host}:{port}")
        return runner


# Хелпер для создания роутов с авторизацией
def create_api_app(
    client_manager=None,
    promo_manager=None,
    analytics_manager=None,
    audit_manager=None,
    api_key: Optional[str] = None,
    require_auth: bool = True
) -> web.Application:
    """Создание приложения API с конфигурацией"""
    api = RestAPI(
        client_manager=client_manager,
        promo_manager=promo_manager,
        analytics_manager=analytics_manager,
        audit_manager=audit_manager,
        api_key=api_key
    )

    if require_auth:
        # Добавляем авторизацию ко всем роутам кроме health
        for route in api.app.router.routes():
            if '/health' not in str(route.resource):
                original_handler = route.handler
                route._handler = api._require_auth(original_handler)

    return api.app
