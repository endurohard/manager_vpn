document.addEventListener('DOMContentLoaded', function() {
    function createMobileMenuButton() {
        const existingBtn = document.querySelector('.mobile-menu-btn');
        if (existingBtn) return;
        
        const menuBtn = document.createElement('button');
        menuBtn.className = 'mobile-menu-btn';
        menuBtn.innerHTML = '☰';
        menuBtn.setAttribute('aria-label', 'Открыть меню');
        document.body.appendChild(menuBtn);
        
        const overlay = document.createElement('div');
        overlay.className = 'mobile-overlay';
        document.body.appendChild(overlay);
        
        return { menuBtn, overlay };
    }
    
    function initMobileMenu() {
        const sidebar = document.querySelector('.sidebar');
        if (!sidebar) return;
        
        const { menuBtn, overlay } = createMobileMenuButton();
        if (!menuBtn) return;
        
        function openMenu() {
            sidebar.classList.add('mobile-open');
            overlay.classList.add('active');
            menuBtn.innerHTML = '✕';
            menuBtn.setAttribute('aria-label', 'Закрыть меню');
            document.body.style.overflow = 'hidden';
        }
        
        function closeMenu() {
            sidebar.classList.remove('mobile-open');
            overlay.classList.remove('active');
            menuBtn.innerHTML = '☰';
            menuBtn.setAttribute('aria-label', 'Открыть меню');
            document.body.style.overflow = '';
        }
        
        function toggleMenu() {
            if (sidebar.classList.contains('mobile-open')) {
                closeMenu();
            } else {
                openMenu();
            }
        }
        
        menuBtn.addEventListener('click', toggleMenu);
        overlay.addEventListener('click', closeMenu);
        
        const sidebarLinks = sidebar.querySelectorAll('a');
        sidebarLinks.forEach(link => {
            link.addEventListener('click', () => {
                if (window.innerWidth <= 768) {
                    closeMenu();
                }
            });
        });
        
        window.addEventListener('resize', () => {
            if (window.innerWidth > 768) {
                closeMenu();
                sidebar.classList.remove('mobile-open');
            }
        });
        
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape' && sidebar.classList.contains('mobile-open')) {
                closeMenu();
            }
        });
    }
    
    function makeTablesResponsive() {
        const tables = document.querySelectorAll('.table-narrow');
        
        tables.forEach(table => {
            let headers = table.querySelectorAll('thead th');
            if (headers.length === 0) {
                const firstRow = table.querySelector('tr');
                if (firstRow) {
                    headers = firstRow.querySelectorAll('th');
                }
            }
            
            if (headers.length === 0) return;
            
            const headerTexts = Array.from(headers).map(header => header.textContent.trim());
            
            const dataRows = table.querySelectorAll('tr:has(td), tr td');
            const rows = Array.from(table.querySelectorAll('tr')).filter(row => {
                return row.querySelectorAll('td').length > 0;
            });
            
            rows.forEach(row => {
                const cells = row.querySelectorAll('td');
                cells.forEach((cell, index) => {
                    if (headerTexts[index]) {
                        cell.setAttribute('data-label', headerTexts[index]);
                    }
                });
            });
        });
    }
    
    function enhanceMobileForms() {
        const modals = document.querySelectorAll('.modal');
        modals.forEach(modal => {
            const observer = new MutationObserver(() => {
                if (modal.style.display === 'block' || modal.style.display === 'flex') {
                    const firstInput = modal.querySelector('input, select, textarea');
                    if (firstInput && window.innerWidth > 480) {
                        setTimeout(() => firstInput.focus(), 100);
                    }
                }
            });
            observer.observe(modal, { attributes: true, attributeFilter: ['style'] });
        });
        
        const checkboxes = document.querySelectorAll('input[type="checkbox"]');
        checkboxes.forEach(checkbox => {
            checkbox.addEventListener('touchstart', function(e) {
                e.preventDefault();
                this.checked = !this.checked;
                
                const event = new Event('change', { bubbles: true });
                this.dispatchEvent(event);
            });
        });
    }
    
    function optimizeIOSScrolling() {
        if (/iPad|iPhone|iPod/.test(navigator.userAgent)) {
            const scrollableElements = document.querySelectorAll('.modal, .sidebar, .main-content');
            scrollableElements.forEach(element => {
                element.style.webkitOverflowScrolling = 'touch';
            });
        }
    }
    
    function setupViewport() {
        let viewport = document.querySelector('meta[name="viewport"]');
        if (!viewport) {
            viewport = document.createElement('meta');
            viewport.name = 'viewport';
            document.head.appendChild(viewport);
        }
        viewport.content = 'width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover';
    }
    
    function detectDevice() {
        const isMobile = window.innerWidth <= 768;
        const isTablet = window.innerWidth > 768 && window.innerWidth <= 1024;
        const isTouch = 'ontouchstart' in window || navigator.maxTouchPoints > 0;
        
        document.body.classList.toggle('is-mobile', isMobile);
        document.body.classList.toggle('is-tablet', isTablet);
        document.body.classList.toggle('is-touch', isTouch);
        
        window.addEventListener('resize', () => {
            const newIsMobile = window.innerWidth <= 768;
            const newIsTablet = window.innerWidth > 768 && window.innerWidth <= 1024;
            
            document.body.classList.toggle('is-mobile', newIsMobile);
            document.body.classList.toggle('is-tablet', newIsTablet);
        });
    }
    
    function init() {
        setupViewport();
        detectDevice();
        initMobileMenu();
        makeTablesResponsive();
        enhanceMobileForms();
        optimizeIOSScrolling();
        manageMobileMenuButton();
        
        const observer = new MutationObserver(() => {
            makeTablesResponsive();
        });
        
        const mainContent = document.querySelector('.main-content');
        if (mainContent) {
            observer.observe(mainContent, { 
                childList: true, 
                subtree: true 
            });
        }
    }
    
    init();
    
    function manageMobileMenuButton() {
        const menuBtn = document.querySelector('.mobile-menu-btn');
        if (!menuBtn) return;
        
        function checkModals() {
            const modals = ['#inboundsModalBg', '#clientModalBg', '#trafficModalBg', '#addSubModalBg', '#serverModalBg', '#modal-bg', '#editModalBg', '#subsInfoModalBg', '#promoModalBg'];
            const hasActiveModal = modals.some(selector => {
                const modal = document.querySelector(selector);
                return modal && modal.style.display === 'block';
            });
            
            if (hasActiveModal) {
                menuBtn.style.display = 'none';
            } else {
                menuBtn.style.display = window.innerWidth <= 768 ? 'block' : 'none';
            }
        }
        
        const modals = document.querySelectorAll('#inboundsModalBg, #clientModalBg, #trafficModalBg, #addSubModalBg, #serverModalBg, #modal-bg, #editModalBg, #subsInfoModalBg, #promoModalBg');
        modals.forEach(modal => {
            if (modal) {
                const observer = new MutationObserver(checkModals);
                observer.observe(modal, { 
                    attributes: true, 
                    attributeFilter: ['style'] 
                });
            }
        });
        
        window.addEventListener('resize', checkModals);
        
        checkModals();
    }
    
    window.MobileAdmin = {
        openMenu: () => {
            const sidebar = document.querySelector('.sidebar');
            if (sidebar) sidebar.classList.add('mobile-open');
        },
        closeMenu: () => {
            const sidebar = document.querySelector('.sidebar');
            if (sidebar) sidebar.classList.remove('mobile-open');
        },
        refreshTables: makeTablesResponsive,
        isMobile: () => window.innerWidth <= 768,
        isTouch: () => 'ontouchstart' in window || navigator.maxTouchPoints > 0,
        checkMenuButton: manageMobileMenuButton
    };
}); 