// Инициализация Telegram WebApp
(function initTelegramWebApp() {
    try {
        if (window.Telegram && window.Telegram.WebApp) {
            const tg = window.Telegram.WebApp;

            // Расширяем viewport на весь экран
            tg.expand();

            // Включаем закрытие при подтверждении
            tg.enableClosingConfirmation();

            // Устанавливаем цвет заголовка
            tg.setHeaderColor('secondary_bg_color');

            // Готовность приложения
            tg.ready();

            console.log('Telegram WebApp initialized', {
                platform: tg.platform,
                version: tg.version,
                colorScheme: tg.colorScheme
            });
        }
    } catch (e) {
        console.error('Telegram WebApp init error:', e);
    }
})();

document.addEventListener("DOMContentLoaded", function () {
    // Переключатель тем
    // Theme switcher (menu)
    const themeMenu = document.querySelector('.theme-menu');
    const themeBtn = document.querySelector('.theme-switcher-btn');
    const userId = document.body?.dataset?.userId || window.USER_ID || '';
    const lsKey = `webapp_theme_css_${userId}`;
    const savedTheme = localStorage.getItem(lsKey);

    const setActiveMark = (fileName) => {
        if (!themeMenu) return;
        themeMenu.querySelectorAll('.theme-item').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.theme === fileName);
        });
    };

    const setTheme = (fileName) => {
        const oldLink = document.getElementById('theme-link');
        if (!oldLink) return;
        const currentUrl = new URL(oldLink.href, window.location.origin);
        const versionParam = currentUrl.searchParams.get('v');
        const params = [];
        if (versionParam) params.push(`v=${versionParam}`);
        params.push(`ts=${Date.now()}`);
        let resolvedFile = fileName;
        if (fileName === '__system__') {
            const isDark = window.Telegram?.WebApp?.colorScheme === 'dark';
            resolvedFile = isDark ? 'style.css' : 'style4.css';
        }
        const newHref = `static/css/${resolvedFile}?${params.join('&')}`;

        const newLink = document.createElement('link');
        newLink.rel = 'stylesheet';
        newLink.id = 'theme-link-new';
        newLink.href = newHref;
        newLink.onload = () => {
            oldLink.remove();
            newLink.id = 'theme-link';
        };
        oldLink.parentNode.insertBefore(newLink, oldLink.nextSibling);
        localStorage.setItem(lsKey, fileName);
        if (themeMenu) themeMenu.classList.remove('open');
        if (themeBtn) themeBtn.setAttribute('aria-expanded', 'false');
        setActiveMark(fileName);
    };

    if (savedTheme) setTheme(savedTheme); else setTheme('stiyle_mini.css');
    if (themeBtn && themeMenu) {
        themeBtn.addEventListener('click', () => {
            const isOpen = themeMenu.classList.toggle('open');
            themeBtn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
            themeMenu.setAttribute('aria-hidden', isOpen ? 'false' : 'true');
        });
        themeMenu.addEventListener('click', (e) => {
            const item = e.target.closest('.theme-item');
            if (!item) return;
            const file = item.dataset.theme;
            setTheme(file);
        });
        themeBtn.addEventListener('click', () => {
            try { window.Telegram?.WebApp?.HapticFeedback?.impactOccurred('light'); } catch (_) {}
        });
        document.addEventListener('click', (e) => {
            if (!themeMenu.contains(e.target) && e.target !== themeBtn) {
                themeMenu.classList.remove('open');
                themeBtn.setAttribute('aria-expanded', 'false');
                themeMenu.setAttribute('aria-hidden', 'true');
            }
        });
    }

    try {
        window.Telegram?.WebApp?.onEvent?.('themeChanged', () => {
            const current = localStorage.getItem(lsKey) || '__system__';
            if (current === '__system__') setTheme('__system__');
        });
    } catch (_) {}

    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            document.querySelectorAll(".tab-btn").forEach(b => b.classList.remove("active"));
            btn.classList.add("active");
            const target = btn.dataset.target;
            document.querySelectorAll(".tab-content").forEach(tab => {
                tab.style.display = tab.id === target ? "block" : "none";
            });
        });
    });


    document.querySelectorAll(".accordion-header").forEach(header => {
        header.addEventListener("click", () => {
            const body = header.nextElementSibling;
            const isVisible = body.style.display === "block";
            document.querySelectorAll(".accordion-body").forEach(b => b.style.display = "none");
            if (!isVisible) body.style.display = "block";
        });
    });

    async function copyTextUniversal(text) {
        try {
            await navigator.clipboard.writeText(text);
            return true;
        } catch (_) {
            try {
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.left = '-9999px';
                ta.setAttribute('readonly', '');
                document.body.appendChild(ta);
                ta.select();
                const ok = document.execCommand('copy');
                document.body.removeChild(ta);
                return ok;
            } catch (_) {
                return false;
            }
        }
    }

    document.querySelectorAll(".copy-btn").forEach(button => {
        button.addEventListener("click", async () => {
            const targetId = button.dataset.copyTarget;
            const target = document.getElementById(targetId);
            const text = target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")
                ? target.value
                : (target ? target.textContent : '');

            const originalHTML = button.innerHTML;
            const ok = await copyTextUniversal(text || '');
            if (ok) {
                button.innerHTML = "<i class='fa fa-check' aria-hidden='true'></i>";
                setTimeout(() => { button.innerHTML = originalHTML; }, 1500);
            }
        });
    });
    const ua = navigator.userAgent.toLowerCase();
    let platform = "desktop";

    if (/(iphone|ipod)/i.test(ua)) {
        platform = "ios";
    } else if (/(ipad)/i.test(ua) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1)) {
        platform = "ios";
    } else if (/(android|linux; u; android)/i.test(ua)) {
        platform = "android";
    } else {
        platform = "desktop";
    }

    document.querySelectorAll(".platform-instruction").forEach(el => el.style.display = "none");
    document.querySelectorAll(`.platform-instruction.${platform}`).forEach(el => el.style.display = "block");

    try {
        const qrEl = document.getElementById('qr');
        const subEl = document.getElementById('sub-url') || document.getElementById('sub-url2');
        const data = subEl ? ((subEl.textContent || subEl.value || '').trim()) : '';
        if (qrEl && data && window.QRCode) {
            new QRCode(qrEl, { text: data, width: 200, height: 200 });
        }
    } catch (_) {}
});

// Функция для безопасного открытия приложений через deep links
function openApp(appType, subscriptionUrl) {
    let deepLink;
    
    if (appType === 'happ') {
        deepLink = 'happ://add/' + subscriptionUrl;
    } else if (appType === 'v2raytun') {
        deepLink = 'v2raytun://import/' + subscriptionUrl;
    } else if (appType === 'hiddify') {
        deepLink = 'hiddify://import/' + subscriptionUrl + '#VPN';
    } else {
        return;
    }
    
    // Попытка открыть приложение
    const startTime = Date.now();
    const iframe = document.createElement('iframe');
    iframe.style.display = 'none';
    iframe.src = deepLink;
    document.body.appendChild(iframe);
    
    // Альтернативный метод для некоторых браузеров
    setTimeout(() => {
        try {
            window.location.href = deepLink;
        } catch (e) {
            console.log('Deep link navigation failed', e);
        }
    }, 100);
    
    // Убираем iframe через секунду
    setTimeout(() => {
        document.body.removeChild(iframe);
    }, 1000);
    
    // Проверяем, открылось ли приложение
    setTimeout(() => {
        const endTime = Date.now();
        // Если прошло мало времени, возможно приложение не установлено
        if (endTime - startTime < 2000) {
            console.log('App may not be installed');
        }
    }, 2000);
}
