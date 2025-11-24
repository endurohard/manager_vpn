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

// Функция генерации QR кода
function generateQRCode(vlessKey) {
    const qrContainer = document.getElementById('qr-code');
    if (!qrContainer || !vlessKey) return;

    // Очищаем предыдущий QR код
    qrContainer.innerHTML = '';

    try {
        // Используем библиотеку qrcodejs
        if (typeof QRCode !== 'undefined') {
            new QRCode(qrContainer, {
                text: vlessKey,
                width: 180,
                height: 180,
                colorDark: '#000000',
                colorLight: '#ffffff',
                correctLevel: QRCode.CorrectLevel.M
            });
        }
    } catch (e) {
        console.error('QR generation error:', e);
        qrContainer.innerHTML = '<p style="color: #666;">QR код недоступен</p>';
    }
}

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

// ============== ORDERING SYSTEM ==============
(function initOrderSystem() {
    let selectedTariff = null;
    let currentOrderId = null;

    // Загрузка тарифов при переключении на вкладку заказа
    document.querySelectorAll(".tab-btn").forEach(btn => {
        btn.addEventListener("click", () => {
            if (btn.dataset.target === 'order-tab') {
                loadTariffs();
            }
        });
    });

    // Загрузка тарифов
    async function loadTariffs() {
        const container = document.getElementById('tariffs-list');
        if (!container) return;

        try {
            const response = await fetch('/api/tariffs');
            const data = await response.json();

            if (data.tariffs && data.tariffs.length > 0) {
                container.innerHTML = data.tariffs.map(tariff => `
                    <div class="tariff-item" data-id="${tariff.id}" data-name="${tariff.name}" data-price="${tariff.price}" data-days="${tariff.days}"
                         style="padding: 15px; background: var(--secondary-bg); border-radius: 8px; cursor: pointer; border: 2px solid transparent; transition: all 0.2s;">
                        <div style="display: flex; justify-content: space-between; align-items: center;">
                            <div>
                                <strong style="font-size: 16px;">${tariff.name}</strong>
                                <p style="margin: 5px 0 0 0; opacity: 0.7; font-size: 13px;">${tariff.days} дней</p>
                            </div>
                            <div style="font-size: 20px; font-weight: bold; color: var(--accent-primary);">
                                ${tariff.price} ₽
                            </div>
                        </div>
                    </div>
                `).join('');

                // Добавляем обработчики выбора тарифа
                container.querySelectorAll('.tariff-item').forEach(item => {
                    item.addEventListener('click', () => {
                        container.querySelectorAll('.tariff-item').forEach(i => {
                            i.style.borderColor = 'transparent';
                        });
                        item.style.borderColor = 'var(--accent-primary)';
                        selectedTariff = {
                            id: item.dataset.id,
                            name: item.dataset.name,
                            price: item.dataset.price,
                            days: item.dataset.days
                        };
                        updateCreateButton();
                    });
                });
            } else {
                container.innerHTML = '<p style="text-align: center; opacity: 0.7;">Тарифы недоступны</p>';
            }
        } catch (e) {
            container.innerHTML = '<p style="text-align: center; color: #ff6b6b;">Ошибка загрузки тарифов</p>';
            console.error('Load tariffs error:', e);
        }
    }

    // Обновление кнопки создания заказа
    function updateCreateButton() {
        const btn = document.getElementById('create-order-btn');
        const contact = document.getElementById('order-contact')?.value?.trim();
        if (btn) {
            btn.disabled = !selectedTariff || !contact;
        }
    }

    // Слушатель ввода контакта
    const contactInput = document.getElementById('order-contact');
    if (contactInput) {
        contactInput.addEventListener('input', updateCreateButton);
    }

    // Создание заказа
    const createOrderBtn = document.getElementById('create-order-btn');
    if (createOrderBtn) {
        createOrderBtn.addEventListener('click', async () => {
            const contact = document.getElementById('order-contact')?.value?.trim();
            if (!selectedTariff || !contact) return;

            createOrderBtn.disabled = true;
            createOrderBtn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Создание...';

            try {
                const response = await fetch('/api/order', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        tariff_id: selectedTariff.id,
                        contact: contact,
                        contact_type: contact.startsWith('@') ? 'telegram' : 'phone'
                    })
                });

                const data = await response.json();

                if (data.error) {
                    alert(data.error);
                    createOrderBtn.disabled = false;
                    createOrderBtn.innerHTML = '<i class="fa fa-arrow-right"></i> Продолжить';
                    return;
                }

                currentOrderId = data.order_id;
                document.getElementById('order-id').textContent = data.order_id;
                document.getElementById('order-tariff').textContent = data.tariff;
                document.getElementById('order-price').textContent = data.price + ' ₽';

                // Показать реквизиты
                displayPaymentDetails(data.payment);

                // Переход к шагу 2
                showStep(2);

                createOrderBtn.disabled = false;
                createOrderBtn.innerHTML = '<i class="fa fa-arrow-right"></i> Продолжить';

            } catch (e) {
                alert('Ошибка создания заказа');
                createOrderBtn.disabled = false;
                createOrderBtn.innerHTML = '<i class="fa fa-arrow-right"></i> Продолжить';
                console.error('Create order error:', e);
            }
        });
    }

    // Отображение реквизитов оплаты
    function displayPaymentDetails(payment) {
        const container = document.getElementById('payment-details');
        if (!container) return;

        let html = '';

        if (payment.card) {
            html += `
                <div style="background: var(--secondary-bg); padding: 15px; border-radius: 8px; margin-bottom: 10px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <strong><i class="fa fa-credit-card"></i> Карта</strong>
                        <span style="opacity: 0.7;">${payment.card.bank || ''}</span>
                    </div>
                    <div style="font-size: 18px; font-family: monospace; letter-spacing: 2px; margin-bottom: 5px;">
                        ${payment.card.number || ''}
                    </div>
                    <div style="opacity: 0.7; font-size: 13px;">
                        ${payment.card.holder || ''}
                    </div>
                </div>
            `;
        }

        if (payment.sbp) {
            html += `
                <div style="background: var(--secondary-bg); padding: 15px; border-radius: 8px;">
                    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
                        <strong><i class="fa fa-mobile-alt"></i> СБП</strong>
                        <span style="opacity: 0.7;">${payment.sbp.bank || ''}</span>
                    </div>
                    <div style="font-size: 18px; font-family: monospace;">
                        ${payment.sbp.phone || ''}
                    </div>
                </div>
            `;
        }

        if (!html) {
            html = '<p style="text-align: center; opacity: 0.7;">Реквизиты недоступны. Свяжитесь с поддержкой.</p>';
        }

        container.innerHTML = html;
    }

    // Загрузка файла оплаты
    let selectedFile = null;
    const uploadArea = document.getElementById('upload-area');
    const fileInput = document.getElementById('payment-proof');
    const uploadPlaceholder = document.getElementById('upload-placeholder');
    const uploadPreview = document.getElementById('upload-preview');
    const previewImage = document.getElementById('preview-image');
    const previewFilename = document.getElementById('preview-filename');
    const removeFileBtn = document.getElementById('remove-file-btn');

    if (uploadArea && fileInput) {
        // Клик по области загрузки
        uploadArea.addEventListener('click', (e) => {
            if (e.target !== removeFileBtn && !removeFileBtn.contains(e.target)) {
                fileInput.click();
            }
        });

        // Drag & Drop
        uploadArea.addEventListener('dragover', (e) => {
            e.preventDefault();
            uploadArea.style.borderColor = 'var(--accent-primary)';
            uploadArea.style.background = 'rgba(36, 129, 204, 0.05)';
        });

        uploadArea.addEventListener('dragleave', () => {
            uploadArea.style.borderColor = 'var(--secondary-bg)';
            uploadArea.style.background = '';
        });

        uploadArea.addEventListener('drop', (e) => {
            e.preventDefault();
            uploadArea.style.borderColor = 'var(--secondary-bg)';
            uploadArea.style.background = '';
            const files = e.dataTransfer.files;
            if (files.length > 0) {
                handleFile(files[0]);
            }
        });

        // Выбор файла
        fileInput.addEventListener('change', () => {
            if (fileInput.files.length > 0) {
                handleFile(fileInput.files[0]);
            }
        });

        // Удаление файла
        if (removeFileBtn) {
            removeFileBtn.addEventListener('click', (e) => {
                e.stopPropagation();
                selectedFile = null;
                fileInput.value = '';
                uploadPlaceholder.style.display = 'block';
                uploadPreview.style.display = 'none';
            });
        }
    }

    function handleFile(file) {
        // Проверка типа
        const allowedTypes = ['image/jpeg', 'image/png', 'image/gif', 'image/webp', 'application/pdf'];
        if (!allowedTypes.includes(file.type)) {
            alert('Поддерживаются только изображения (JPG, PNG, GIF, WebP) и PDF');
            return;
        }

        // Проверка размера (10MB)
        if (file.size > 10 * 1024 * 1024) {
            alert('Файл слишком большой. Максимум 10 МБ');
            return;
        }

        selectedFile = file;
        uploadPlaceholder.style.display = 'none';
        uploadPreview.style.display = 'block';
        previewFilename.textContent = file.name;

        // Показать превью для изображений
        if (file.type.startsWith('image/')) {
            const reader = new FileReader();
            reader.onload = (e) => {
                previewImage.src = e.target.result;
                previewImage.style.display = 'block';
            };
            reader.readAsDataURL(file);
        } else {
            previewImage.style.display = 'none';
        }
    }

    // Подтверждение оплаты
    const confirmPaymentBtn = document.getElementById('confirm-payment-btn');
    if (confirmPaymentBtn) {
        confirmPaymentBtn.addEventListener('click', async () => {
            if (!currentOrderId) return;

            const paymentInfo = document.getElementById('payment-info')?.value?.trim() || '';

            confirmPaymentBtn.disabled = true;
            confirmPaymentBtn.innerHTML = '<i class="fa fa-spinner fa-spin"></i> Отправка...';

            try {
                let response;

                if (selectedFile) {
                    // Отправка с файлом (multipart/form-data)
                    const formData = new FormData();
                    formData.append('order_id', currentOrderId);
                    formData.append('payment_info', paymentInfo);
                    formData.append('payment_proof', selectedFile);

                    response = await fetch('/api/confirm', {
                        method: 'POST',
                        body: formData
                    });
                } else {
                    // Отправка без файла (JSON)
                    response = await fetch('/api/confirm', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({
                            order_id: currentOrderId,
                            payment_info: paymentInfo
                        })
                    });
                }

                const data = await response.json();

                if (data.error) {
                    alert(data.error);
                    confirmPaymentBtn.disabled = false;
                    confirmPaymentBtn.innerHTML = '<i class="fa fa-paper-plane"></i> Я оплатил';
                    return;
                }

                document.getElementById('waiting-order-id').textContent = currentOrderId;
                showStep(3);

                // Очищаем файл
                selectedFile = null;
                if (fileInput) fileInput.value = '';
                if (uploadPlaceholder) uploadPlaceholder.style.display = 'block';
                if (uploadPreview) uploadPreview.style.display = 'none';

                confirmPaymentBtn.disabled = false;
                confirmPaymentBtn.innerHTML = '<i class="fa fa-paper-plane"></i> Я оплатил';

            } catch (e) {
                alert('Ошибка отправки');
                confirmPaymentBtn.disabled = false;
                confirmPaymentBtn.innerHTML = '<i class="fa fa-paper-plane"></i> Я оплатил';
                console.error('Confirm payment error:', e);
            }
        });
    }

    // Назад к шагу 1
    const backBtn = document.getElementById('back-to-step1-btn');
    if (backBtn) {
        backBtn.addEventListener('click', () => showStep(1));
    }

    // Проверка статуса
    const checkStatusBtn = document.getElementById('check-status-btn');
    if (checkStatusBtn) {
        checkStatusBtn.addEventListener('click', () => checkOrderStatus(currentOrderId));
    }

    // Проверка существующего заказа
    const checkExistingBtn = document.getElementById('check-existing-order-btn');
    if (checkExistingBtn) {
        checkExistingBtn.addEventListener('click', () => {
            const orderId = document.getElementById('check-order-id')?.value?.trim();
            if (orderId) {
                currentOrderId = orderId.toUpperCase();
                checkOrderStatus(currentOrderId);
            }
        });
    }

    // Функция проверки статуса заказа
    async function checkOrderStatus(orderId) {
        if (!orderId) return;

        const statusText = document.getElementById('order-status-text');
        if (statusText) statusText.textContent = '';

        try {
            const response = await fetch(`/api/order/${orderId}`);
            const data = await response.json();

            if (data.error) {
                if (statusText) {
                    statusText.textContent = data.error;
                    statusText.style.color = '#ff6b6b';
                }
                return;
            }

            if (data.status === 'completed' && data.vless_key) {
                document.getElementById('vless-key').value = data.vless_key;
                generateQRCode(data.vless_key);
                showStep(4);
            } else if (data.status === 'paid') {
                document.getElementById('waiting-order-id').textContent = orderId;
                showStep(3);
                if (statusText) {
                    statusText.textContent = 'Оплата на проверке...';
                    statusText.style.color = 'var(--accent-primary)';
                }
            } else if (data.status === 'pending') {
                if (statusText) {
                    statusText.textContent = 'Заказ ожидает оплаты';
                    statusText.style.color = '#ffc107';
                }
            } else {
                if (statusText) {
                    statusText.textContent = `Статус: ${data.status}`;
                    statusText.style.color = 'var(--text-primary)';
                }
            }
        } catch (e) {
            if (statusText) {
                statusText.textContent = 'Ошибка проверки';
                statusText.style.color = '#ff6b6b';
            }
            console.error('Check status error:', e);
        }
    }

    // Копирование ключа
    const copyKeyBtn = document.getElementById('copy-key-btn');
    if (copyKeyBtn) {
        copyKeyBtn.addEventListener('click', async () => {
            const key = document.getElementById('vless-key')?.value;
            if (!key) return;

            try {
                await navigator.clipboard.writeText(key);
                copyKeyBtn.innerHTML = '<i class="fa fa-check"></i> Скопировано!';
                setTimeout(() => {
                    copyKeyBtn.innerHTML = '<i class="fa fa-copy"></i> Скопировать ключ';
                }, 2000);
            } catch (e) {
                // Fallback
                const ta = document.createElement('textarea');
                ta.value = key;
                ta.style.position = 'fixed';
                ta.style.left = '-9999px';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
                copyKeyBtn.innerHTML = '<i class="fa fa-check"></i> Скопировано!';
                setTimeout(() => {
                    copyKeyBtn.innerHTML = '<i class="fa fa-copy"></i> Скопировать ключ';
                }, 2000);
            }
        });
    }

    // Новый заказ
    const newOrderBtn = document.getElementById('new-order-btn');
    if (newOrderBtn) {
        newOrderBtn.addEventListener('click', () => {
            selectedTariff = null;
            currentOrderId = null;
            document.getElementById('order-contact').value = '';
            document.getElementById('payment-info').value = '';
            document.getElementById('check-order-id').value = '';
            const container = document.getElementById('tariffs-list');
            if (container) {
                container.querySelectorAll('.tariff-item').forEach(i => {
                    i.style.borderColor = 'transparent';
                });
            }
            updateCreateButton();
            showStep(1);
        });
    }

    // Переключение шагов
    function showStep(step) {
        document.querySelectorAll('.order-step').forEach(s => s.style.display = 'none');
        const stepEl = document.getElementById(`order-step-${step}`);
        if (stepEl) stepEl.style.display = 'block';
    }
})();
