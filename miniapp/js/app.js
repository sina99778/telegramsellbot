/**
 * App initialization — premium experience with haptic feedback & pull-to-refresh.
 */
(async function init() {
    const tg = window.Telegram?.WebApp;
    if (tg) {
        tg.ready();
        tg.expand();
        try { tg.enableClosingConfirmation(); } catch {}
        // Set header color to match app background
        try { tg.setHeaderColor('#050505'); } catch {}
        try { tg.setBackgroundColor('#050505'); } catch {}
    }

    document.querySelectorAll('[data-icon]').forEach(el => {
        el.innerHTML = UI.icon(el.dataset.icon, el.classList.contains('nav-icon') ? 'nav-svg' : 'inline-icon');
    });

    // Navigation with haptic feedback
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const page = btn.dataset.page;
            if (page) {
                UI.navigate(page);
                try { tg?.HapticFeedback?.selectionChanged(); } catch {}
            }
        });
    });

    // Ticket send
    // Ticket send with animation
    const sendBtn = document.getElementById('ticket-send-btn');
    const ticketInput = document.getElementById('ticket-input');

    sendBtn.addEventListener('click', async () => {
        const text = ticketInput.value.trim();
        if (!text) return;
        sendBtn.disabled = true;
        sendBtn.style.transform = 'scale(0.85) rotate(45deg)';
        try {
            await API.sendTicket(text);
            ticketInput.value = '';
            ticketInput.style.height = 'auto';
            try { tg?.HapticFeedback?.notificationOccurred('success'); } catch {}
            Pages.load_support();
        } catch (e) {
            UI.toast('خطا: ' + e.message, 'error');
        } finally {
            sendBtn.disabled = false;
            sendBtn.style.transform = '';
        }
    });

    // Auto-grow textarea
    ticketInput.addEventListener('input', () => {
        ticketInput.style.height = 'auto';
        ticketInput.style.height = Math.min(ticketInput.scrollHeight, 100) + 'px';
    });

    // Enter to send (Shift+Enter for newline)
    ticketInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            sendBtn.click();
        }
    });

    // ─── Pull-to-refresh (properly gated) ──────────────────────────────────
    const mainContent = document.getElementById('main-content');
    let pullStartY = 0;
    let pullTriggered = false;
    let pullCooldown = false;

    mainContent?.addEventListener('touchstart', (e) => {
        // Only start pull tracking if we're at the very top of the active page-scroll
        const activeScroll = document.querySelector('.page.active .page-scroll');
        if (activeScroll && activeScroll.scrollTop <= 0 && !pullCooldown) {
            pullStartY = e.touches[0].clientY;
            pullTriggered = false;
        } else {
            pullStartY = 0;
        }
    }, { passive: true });

    mainContent?.addEventListener('touchmove', (e) => {
        if (!pullStartY || pullCooldown) return;
        const activeScroll = document.querySelector('.page.active .page-scroll');
        if (!activeScroll || activeScroll.scrollTop > 0) {
            pullStartY = 0;
            return;
        }
        const pullDist = e.touches[0].clientY - pullStartY;
        if (pullDist > 40) {
            mainContent.style.transform = `translateY(${Math.min(pullDist * 0.2, 30)}px)`;
            mainContent.style.transition = 'none';
        }
        if (pullDist > 120) {
            pullTriggered = true;
        }
    }, { passive: true });

    mainContent?.addEventListener('touchend', async () => {
        mainContent.style.transition = 'transform 0.3s ease';
        mainContent.style.transform = '';

        if (!pullTriggered || pullCooldown) {
            pullStartY = 0;
            return;
        }
        pullStartY = 0;
        pullTriggered = false;
        pullCooldown = true;

        const activePage = document.querySelector('.page.active');
        const pageId = activePage?.id?.replace('page-', '');
        if (pageId && typeof Pages !== 'undefined' && Pages[`load_${pageId}`]) {
            try { tg?.HapticFeedback?.impactOccurred('light'); } catch {}
            UI.toast('بروزرسانی...');
            try { await Pages[`load_${pageId}`](); } catch {}
        }
        // Cooldown to prevent double-trigger
        setTimeout(() => { pullCooldown = false; }, 1500);
    });

    // Show UI with staggered entrance
    document.getElementById('loading-screen').classList.add('hidden');
    document.getElementById('app-header').classList.remove('hidden');

    requestAnimationFrame(() => {
        document.getElementById('main-content').classList.remove('hidden');
        setTimeout(() => document.getElementById('bottom-nav').classList.remove('hidden'), 80);
    });

    // Load dashboard
    try {
        window.AppConfig = await API.getConfig().catch(() => ({}));
        await Pages.load_dashboard();
        const params = new URLSearchParams(window.location.search);
        if (params.get('page') === 'admin' && window.AppState?.is_admin) {
            UI.navigate('admin');
        }
    } catch (e) {
        UI.toast(e.message, 'error');
    }
})();
