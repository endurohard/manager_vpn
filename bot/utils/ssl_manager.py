"""
Автоматизация SSL-сертификатов и nginx конфигурации для брендов.
Все бренды управляются через один файл /etc/nginx/sites-enabled/brands.conf
"""
import asyncio
import logging
import os
import json
import aiosqlite
from pathlib import Path

logger = logging.getLogger(__name__)

NGINX_BRANDS_CONF = "/etc/nginx/sites-enabled/brands.conf"

# Шаблон server block для одного бренда
BRAND_SERVER_BLOCK = """
# === Brand: {name} ({domain}) ===
server {{
    listen 443 ssl http2;
    server_name {domain};

    ssl_certificate /etc/letsencrypt/live/{domain}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/{domain}/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options "nosniff" always;

    location /sub/ {{
        proxy_pass http://127.0.0.1:{port}/sub/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}

    location /static/ {{
        proxy_pass http://127.0.0.1:{port}/static/;
        proxy_set_header Host $host;
        expires 1d;
    }}

    location /api/ {{
        proxy_pass http://127.0.0.1:{port}/api/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }}

    location / {{
        proxy_pass http://127.0.0.1:{port}/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}

server {{
    listen 80;
    server_name {domain};

    location /.well-known/acme-challenge/ {{
        root /var/www/html;
    }}

    location / {{
        return 301 https://$host$request_uri;
    }}
}}
"""


async def regenerate_brands_conf(db_path: str, port: int = 9090):
    """
    Перегенерировать brands.conf из БД.
    Включает только бренды с id > 1 (основной zov-gor.ru управляется отдельно)
    и только те, для которых есть SSL-сертификат.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM brands WHERE id > 1 ORDER BY id")
        brands = await cursor.fetchall()

    blocks = ["# Auto-generated brands nginx config\n# Do not edit manually - managed by ssl_manager.py\n"]

    for brand in brands:
        domain = brand['domain']
        name = brand['name']
        cert_path = f"/etc/letsencrypt/live/{domain}/fullchain.pem"

        if os.path.exists(cert_path):
            blocks.append(BRAND_SERVER_BLOCK.format(
                name=name, domain=domain, port=port
            ))
            logger.info(f"Nginx block для {domain} ({name}) добавлен")
        else:
            # Только HTTP block для получения сертификата
            blocks.append(f"""
# === Brand: {name} ({domain}) - NO SSL YET ===
server {{
    listen 80;
    server_name {domain};

    location /.well-known/acme-challenge/ {{
        root /var/www/html;
    }}

    location / {{
        return 503 "SSL not configured yet";
    }}
}}
""")
            logger.warning(f"SSL для {domain} не найден, только HTTP block")

    content = "\n".join(blocks)

    with open(NGINX_BRANDS_CONF, 'w') as f:
        f.write(content)

    logger.info(f"brands.conf обновлён: {len(brands)} брендов")
    return len(brands)


async def obtain_ssl(domain: str, email: str = None) -> tuple:
    """Получить SSL-сертификат через certbot webroot."""
    email_arg = f"-m {email}" if email else "--register-unsafely-without-email"
    os.makedirs("/var/www/html/.well-known/acme-challenge", exist_ok=True)

    cmd = (
        f"certbot certonly --webroot -w /var/www/html -d {domain} "
        f"--non-interactive --agree-tos {email_arg}"
    )

    logger.info(f"Запуск certbot для {domain}")

    proc = await asyncio.create_subprocess_shell(
        cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode == 0:
        logger.info(f"SSL-сертификат для {domain} получен")
        return True, "SSL-сертификат получен"
    else:
        error = stderr.decode().strip()
        logger.error(f"Ошибка certbot для {domain}: {error}")
        return False, f"Ошибка certbot: {error[:300]}"


async def reload_nginx() -> tuple:
    """Перезагрузить nginx."""
    proc = await asyncio.create_subprocess_shell(
        "nginx -t && nginx -s reload",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode == 0:
        return True, "Nginx перезагружен"
    else:
        error = stderr.decode().strip()
        return False, f"Ошибка nginx: {error[:300]}"


async def setup_brand_domain(domain: str, port: int = 9090, email: str = None, db_path: str = None) -> tuple:
    """
    Полная настройка домена для бренда:
    1. Проверка DNS
    2. Генерация brands.conf (HTTP block для certbot)
    3. Reload nginx
    4. Certbot SSL
    5. Регенерация brands.conf (с SSL block)
    6. Final reload
    """
    steps = []

    if not db_path:
        from bot.config import DATABASE_PATH
        db_path = DATABASE_PATH

    # 1. Проверка DNS
    proc = await asyncio.create_subprocess_shell(
        f"dig +short {domain}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    ip = stdout.decode().strip()
    if not ip:
        return False, f"DNS для {domain} не настроен. Добавьте A-запись."
    steps.append(f"DNS: {domain} → {ip}")

    # 2. Генерация brands.conf (пока без SSL для нового домена)
    await regenerate_brands_conf(db_path, port)
    steps.append("brands.conf обновлён (HTTP)")

    # 3. Reload nginx
    ok, msg = await reload_nginx()
    if not ok:
        steps.append(msg)
        return False, "\n".join(steps)
    steps.append("Nginx перезагружен")

    # 4. SSL сертификат
    ok, msg = await obtain_ssl(domain, email)
    steps.append(msg)
    if not ok:
        return False, "\n".join(steps)

    # 5. Регенерация brands.conf (теперь с SSL)
    await regenerate_brands_conf(db_path, port)
    steps.append("brands.conf обновлён (SSL)")

    # 6. Final reload
    ok, msg = await reload_nginx()
    steps.append(msg)

    return ok, "\n".join(steps)


async def remove_brand_domain(domain: str, db_path: str = None) -> tuple:
    """Удалить домен — перегенерировать brands.conf без него."""
    if not db_path:
        from bot.config import DATABASE_PATH
        db_path = DATABASE_PATH

    # Удалить старый отдельный конфиг если есть
    old_conf = f"/etc/nginx/sites-enabled/{domain}.conf"
    if os.path.exists(old_conf):
        os.remove(old_conf)

    await regenerate_brands_conf(db_path)
    ok, msg = await reload_nginx()
    return ok, msg
