/**
 * App initialization — boot sequence, event binding, Telegram WebApp integration.
 */
(async function init() {
    // ─── Telegram WebApp setup ──────────────────────────────────────────
    const tg = window.Telegram?.WebApp;

    console.log('[App] Telegram WebApp available:', !!tg);
    console.log('[App] initData:', tg?.initData?.substring(0, 60) + '...');
    console.log('[App] initDataUnsafe:', JSON.stringify(tg?.initDataUnsafe || {}));

    if (tg) {
        tg.ready();
        tg.expand();
        try { tg.enableClosingConfirmation(); } catch {}
    }

    // ─── Navigation binding ─────────────────────────────────────────────
    document.querySelectorAll('.nav-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            const page = btn.dataset.page;
            if (page) UI.navigate(page);
        });
    });

    // ─── Ticket send ────────────────────────────────────────────────────
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

    // Also support Enter key for sending tickets
    document.getElementById('ticket-input').addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            document.getElementById('ticket-send-btn').click();
        }
    });

    // ─── Load dashboard ─────────────────────────────────────────────────
    try {
        // Small delay to ensure Telegram SDK is fully ready
        if (tg && !tg.initData) {
            console.log('[App] Waiting for initData...');
            await new Promise(resolve => setTimeout(resolve, 500));
            console.log('[App] initData after wait:', tg.initData?.substring(0, 60));
        }

        await Pages.load_dashboard();

        // Show UI
        document.getElementById('loading-screen').classList.add('hidden');
        document.getElementById('app-header').classList.remove('hidden');
        document.getElementById('main-content').classList.remove('hidden');
        document.getElementById('bottom-nav').classList.remove('hidden');
    } catch (e) {
        console.error('[App] Boot error:', e);

        // Show UI anyway with error
        document.getElementById('loading-screen').classList.add('hidden');
        document.getElementById('app-header').classList.remove('hidden');
        document.getElementById('main-content').classList.remove('hidden');
        document.getElementById('bottom-nav').classList.remove('hidden');

        UI.toast('❌ خطا در بارگذاری: ' + e.message, 'error');
    }
})();
