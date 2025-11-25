#!/bin/bash
# Watchdog для VPN бота и X-UI

LOG="/root/manager_vpn/watchdog.log"
BOT_SERVICE="raphaelvpn_bot"
XUI_SERVICE="x-ui"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG"
}

# Проверка бота
check_bot() {
    # Проверяем что сервис активен
    if ! systemctl is-active --quiet "$BOT_SERVICE"; then
        log "ERROR: Bot service is not running. Restarting..."
        systemctl restart "$BOT_SERVICE"
        sleep 5
        if systemctl is-active --quiet "$BOT_SERVICE"; then
            log "OK: Bot service restarted successfully"
        else
            log "CRITICAL: Bot service failed to start"
        fi
        return 1
    fi

    # Проверяем что webapp отвечает
    if ! curl -sf http://127.0.0.1:9090/api/tariffs > /dev/null 2>&1; then
        log "ERROR: Bot webapp not responding. Restarting..."
        systemctl restart "$BOT_SERVICE"
        sleep 5
        return 1
    fi

    return 0
}

# Проверка X-UI
check_xui() {
    if ! systemctl is-active --quiet "$XUI_SERVICE"; then
        log "ERROR: X-UI service is not running. Restarting..."
        systemctl restart "$XUI_SERVICE"
        sleep 5
        if systemctl is-active --quiet "$XUI_SERVICE"; then
            log "OK: X-UI service restarted successfully"
        else
            log "CRITICAL: X-UI service failed to start"
        fi
        return 1
    fi

    # Проверяем что xray слушает порт 8444
    if ! ss -tlnp | grep -q ":8444 "; then
        log "ERROR: Xray not listening on port 8444. Restarting X-UI..."
        systemctl restart "$XUI_SERVICE"
        sleep 10
        return 1
    fi

    return 0
}

# Проверка на дубликаты процессов бота
check_duplicates() {
    count=$(pgrep -fc "python3.*main.py" 2>/dev/null || echo "0")
    if [ "$count" -gt 1 ]; then
        log "WARNING: Found $count bot processes. Killing duplicates..."
        # Убиваем все и перезапускаем сервис
        pkill -9 -f "python3.*main.py" 2>/dev/null
        sleep 2
        systemctl start "$BOT_SERVICE"
        log "OK: Duplicates killed, service restarted"
        return 1
    fi
    return 0
}

# Основная проверка
check_duplicates
check_bot
check_xui

# Очистка старых логов (оставляем последние 1000 строк)
if [ -f "$LOG" ] && [ $(wc -l < "$LOG") -gt 1000 ]; then
    tail -500 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi
