// Инициализация Telegram Web App
let tg = window.Telegram.WebApp;

// Расширяем веб-приложение на весь экран
tg.expand();

// Применяем тему Telegram
document.documentElement.style.setProperty('--tg-theme-bg-color', tg.themeParams.bg_color || '#ffffff');
document.documentElement.style.setProperty('--tg-theme-text-color', tg.themeParams.text_color || '#000000');
document.documentElement.style.setProperty('--tg-theme-hint-color', tg.themeParams.hint_color || '#999999');
document.documentElement.style.setProperty('--tg-theme-link-color', tg.themeParams.link_color || '#3390ec');
document.documentElement.style.setProperty('--tg-theme-button-color', tg.themeParams.button_color || '#3390ec');
document.documentElement.style.setProperty('--tg-theme-button-text-color', tg.themeParams.button_text_color || '#ffffff');
document.documentElement.style.setProperty('--tg-theme-secondary-bg-color', tg.themeParams.secondary_bg_color || '#f4f4f5');

// Переключение вкладок
document.addEventListener('DOMContentLoaded', function() {
    const navTabs = document.querySelectorAll('.nav-tab');
    const tabContents = document.querySelectorAll('.tab-content');

    navTabs.forEach(tab => {
        tab.addEventListener('click', function() {
            const targetTab = this.getAttribute('data-tab');

            // Удаляем активный класс у всех вкладок
            navTabs.forEach(t => t.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));

            // Добавляем активный класс к выбранной вкладке
            this.classList.add('active');
            document.getElementById(targetTab).classList.add('active');

            // Вибрация при переключении
            if (tg.HapticFeedback) {
                tg.HapticFeedback.impactOccurred('light');
            }

            // Прокручиваем к началу контента
            window.scrollTo({ top: 0, behavior: 'smooth' });
        });
    });

    // Обработка кликов по ссылкам скачивания
    const downloadButtons = document.querySelectorAll('.download-btn');
    downloadButtons.forEach(btn => {
        btn.addEventListener('click', function(e) {
            // Вибрация при клике
            if (tg.HapticFeedback) {
                tg.HapticFeedback.impactOccurred('medium');
            }

            // Открываем ссылку в браузере
            const url = this.getAttribute('href');
            if (url && url !== '#') {
                e.preventDefault();
                tg.openLink(url);
            }
        });
    });

    // Отправляем событие о готовности приложения
    tg.ready();

    // Информируем бота о том, что пользователь открыл приложение
    sendEvent('app_opened');
});

// Функция для отправки событий боту
function sendEvent(event, data = {}) {
    const eventData = {
        event: event,
        timestamp: Date.now(),
        user_id: tg.initDataUnsafe?.user?.id,
        ...data
    };

    console.log('Event:', eventData);

    // Можно отправить событие боту через postEvent (опционально)
    // tg.sendData(JSON.stringify(eventData));
}

// Обработка изменения темы
tg.onEvent('themeChanged', function() {
    document.documentElement.style.setProperty('--tg-theme-bg-color', tg.themeParams.bg_color);
    document.documentElement.style.setProperty('--tg-theme-text-color', tg.themeParams.text_color);
    document.documentElement.style.setProperty('--tg-theme-hint-color', tg.themeParams.hint_color);
    document.documentElement.style.setProperty('--tg-theme-link-color', tg.themeParams.link_color);
    document.documentElement.style.setProperty('--tg-theme-button-color', tg.themeParams.button_color);
    document.documentElement.style.setProperty('--tg-theme-button-text-color', tg.themeParams.button_text_color);
    document.documentElement.style.setProperty('--tg-theme-secondary-bg-color', tg.themeParams.secondary_bg_color);
});

// Плавная прокрутка для всех внутренних ссылок
document.addEventListener('click', function(e) {
    if (e.target.tagName === 'A' && e.target.getAttribute('href')?.startsWith('#')) {
        e.preventDefault();
        const targetId = e.target.getAttribute('href').substring(1);
        const targetElement = document.getElementById(targetId);

        if (targetElement) {
            targetElement.scrollIntoView({ behavior: 'smooth' });

            if (tg.HapticFeedback) {
                tg.HapticFeedback.impactOccurred('light');
            }
        }
    }
});

// Анимация появления элементов при прокрутке
const observerOptions = {
    threshold: 0.1,
    rootMargin: '0px 0px -50px 0px'
};

const observer = new IntersectionObserver(function(entries) {
    entries.forEach(entry => {
        if (entry.isIntersecting) {
            entry.target.style.opacity = '1';
            entry.target.style.transform = 'translateY(0)';
        }
    });
}, observerOptions);

// Наблюдаем за карточками
document.querySelectorAll('.platform-card, .app-card, .tips-card, .info-card').forEach(card => {
    card.style.opacity = '0';
    card.style.transform = 'translateY(20px)';
    card.style.transition = 'opacity 0.4s ease, transform 0.4s ease';
    observer.observe(card);
});
