window.ModalUtils = {
    openModal(modalBgId, fields = {}) {
        document.getElementById(modalBgId).style.display = 'block';
        for (const [id, value] of Object.entries(fields)) {
            const el = document.getElementById(id);
            if (el) {
                if (el.type === 'checkbox') {
                    el.checked = !!value;
                } else {
                    el.value = value;
                }
            }
        }
    },
    closeModal(modalBgId) {
        document.getElementById(modalBgId).style.display = 'none';
    },
    attachBgClose(modalBgId, closeFn) {
        document.getElementById(modalBgId).onclick = function(e) {
            if (e.target === this) closeFn();
        };
    },
    attachAjaxForm(formId, modalBgId, tableSelector) {
        const form = document.getElementById(formId);
        if (!form) return;
        form.onsubmit = function(e) {
            e.preventDefault();
            const formData = new FormData(form);
            fetch(form.action, {
                method: 'POST',
                body: formData,
                headers: { 'x-requested-with': 'XMLHttpRequest' }
            })
            .then(r => r.json())
            .then(data => {
                if (data.success) {
                    fetch(window.location.pathname, {headers: {'x-requested-with': 'XMLHttpRequest'}})
                        .then(r=>r.text())
                        .then(html=>{
                            const parser = new DOMParser();
                            const doc = parser.parseFromString(html, 'text/html');
                            const newTable = doc.querySelector(tableSelector);
                            document.querySelector(tableSelector).outerHTML = newTable.outerHTML;
                            ModalUtils.closeModal(modalBgId);
                        });
                }
            });
        };
    },
    attachDeleteBtn(btnSelector, urlFn, tableSelector) {
        document.querySelectorAll(btnSelector).forEach(btn => {
            btn.onclick = function() {
                if (!confirm('Удалить?')) return;
                fetch(urlFn(btn), {
                    method: 'POST',
                    headers: { 'x-requested-with': 'XMLHttpRequest' }
                })
                .then(r => r.json())
                .then(data => {
                    if (data.success) {
                        fetch(window.location.pathname, {headers: {'x-requested-with': 'XMLHttpRequest'}})
                            .then(r=>r.text())
                            .then(html=>{
                                const parser = new DOMParser();
                                const doc = parser.parseFromString(html, 'text/html');
                                const newTable = doc.querySelector(tableSelector);
                                document.querySelector(tableSelector).outerHTML = newTable.outerHTML;
                            });
                    }
                });
            };
        });
    }
};

function showModalBg(id) {
  document.getElementById(id).style.display = 'block';
}
function hideModalBg(id) {
  document.getElementById(id).style.display = 'none';
}

document.addEventListener('DOMContentLoaded', function() {
    const themeSwitch = document.getElementById('themeSwitch');
    const themeIcon = document.getElementById('themeIcon');
    const themeLabel = document.getElementById('themeLabel');
    const emojiPicker = document.querySelector('emoji-picker');
    if (!themeSwitch) return;

    const savedTheme = localStorage.getItem('theme');
    let isDark = savedTheme !== 'light';

    function applyTheme(dark) {
        if (dark) {
            document.body.classList.add('very-dark');
            themeSwitch.classList.add('ant-switch-checked');
            themeSwitch.setAttribute('aria-checked', 'true');
            if (themeIcon) themeIcon.textContent = '🌙';
            if (themeLabel) themeLabel.textContent = 'Тёмная тема';
            if (emojiPicker) {
                emojiPicker.classList.add('dark');
                emojiPicker.style.setProperty('--background', '#2c313a');
                emojiPicker.style.setProperty('--category-font-color', '#e0e0e0');
                emojiPicker.style.setProperty('--input-font-color', '#e0e0e0');
                emojiPicker.style.setProperty('--input-border-color', '#333');
                emojiPicker.style.setProperty('--button-background', '#2e3338');
                emojiPicker.style.setProperty('--button-hover-background', '#00a884');
                emojiPicker.style.setProperty('--button-active-background', '#008771');
                emojiPicker.style.setProperty('--indicator-color', '#6ee7b7');
            }
        } else {
            document.body.classList.remove('very-dark');
            themeSwitch.classList.remove('ant-switch-checked');
            themeSwitch.setAttribute('aria-checked', 'false');
            if (themeIcon) themeIcon.textContent = '☀️';
            if (themeLabel) themeLabel.textContent = 'Светлая тема';
            if (emojiPicker) {
                emojiPicker.classList.remove('dark');
                emojiPicker.style.setProperty('--background', '#e7edf0');
                emojiPicker.style.setProperty('--category-font-color', '#23272b');
                emojiPicker.style.setProperty('--input-font-color', '#23272b');
                emojiPicker.style.setProperty('--input-border-color', '#d0d7de');
                emojiPicker.style.setProperty('--button-background', '#f0f3f6');
                emojiPicker.style.setProperty('--button-hover-background', '#00c99a');
                emojiPicker.style.setProperty('--button-active-background', '#00a884');
                emojiPicker.style.setProperty('--indicator-color', '#00a884');
            }
        }
    }
    applyTheme(isDark);
    themeSwitch.addEventListener('click', function() {
        isDark = !isDark;
        localStorage.setItem('theme', isDark ? 'dark' : 'light');
        applyTheme(isDark);
    });
}); 