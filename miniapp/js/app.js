/**
 * App initialization.
 */
(async function init() {
    const tg = window.Telegram?.WebApp;
    if (tg) {
        tg.ready();
        tg.expand();
        try { tg.enableClosingConfirmation(); } catch {}
    }

    // Navigation
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const page = btn.dataset.page;
            if (page) UI.navigate(page);
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
            UI.toast('✅ پیام ارسال شد');
            Pages.load_support();
        } catch (e) {
            UI.toast('❌ خطا: ' + e.message, 'error');
        }
    });

    document.getElementById('ticket-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            document.getElementById('ticket-send-btn').click();
        }
    });

    // Show UI immediately
    document.getElementById('loading-screen').classList.add('hidden');
    document.getElementById('app-header').classList.remove('hidden');
    document.getElementById('main-content').classList.remove('hidden');
    document.getElementById('bottom-nav').classList.remove('hidden');

    // Load dashboard
    try {
        window.AppConfig = await API.getConfig().catch(() => ({}));
        await Pages.load_dashboard();
    } catch (e) {
        UI.toast('❌ ' + e.message, 'error');
    }
})();
