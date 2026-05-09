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
    document.getElementById('ticket-send-btn').addEventListener('click', async () => {
        const input = document.getElementById('ticket-input');
        const text = input.value.trim();
        if (!text) return;
        try {
            await API.sendTicket(text);
            input.value = '';
            UI.toast('پیام ارسال شد');
            try { tg?.HapticFeedback?.notificationOccurred('success'); } catch {}
            Pages.load_support();
        } catch (e) {
            UI.toast('خطا: ' + e.message, 'error');
        }
    });

    document.getElementById('ticket-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            document.getElementById('ticket-send-btn').click();
        }
    });

    // ─── Pull-to-refresh on dashboard ─────────────────────────────────────
    let pullStartY = 0;
    let isPulling = false;
    const mainContent = document.getElementById('main-content');

    mainContent?.addEventListener('touchstart', (e) => {
        if (window.scrollY <= 0) {
            pullStartY = e.touches[0].clientY;
            isPulling = true;
        }
    }, { passive: true });

    mainContent?.addEventListener('touchmove', (e) => {
        if (!isPulling) return;
        const pullDist = e.touches[0].clientY - pullStartY;
        if (pullDist > 80 && window.scrollY <= 0) {
            // Visual feedback
            mainContent.style.transform = `translateY(${Math.min(pullDist * 0.3, 40)}px)`;
            mainContent.style.transition = 'none';
        }
    }, { passive: true });

    mainContent?.addEventListener('touchend', async () => {
        if (!isPulling) return;
        isPulling = false;
        mainContent.style.transition = 'transform 0.3s ease';
        mainContent.style.transform = '';

        // Check if enough pull distance
        if (mainContent.style.transform === '' || true) {
            const activePage = document.querySelector('.page.active');
            const pageId = activePage?.id?.replace('page-', '');
            if (pageId && typeof Pages !== 'undefined' && Pages[`load_${pageId}`]) {
                try { tg?.HapticFeedback?.impactOccurred('light'); } catch {}
                UI.toast('بروزرسانی...');
                try {
                    await Pages[`load_${pageId}`]();
                } catch {}
            }
        }
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
