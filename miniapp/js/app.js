/**
 * App initialization — boot sequence, event binding, Telegram WebApp integration.
 */
(async function init() {
    // ─── Telegram WebApp setup ──────────────────────────────────────────
    const tg = window.Telegram?.WebApp;
    if (tg) {
        tg.ready();
        tg.expand();
        tg.enableClosingConfirmation();

        // Apply Telegram theme colors if available
        if (tg.themeParams) {
            const root = document.documentElement;
            if (tg.themeParams.bg_color) {
                // Keep our dark theme — don't override
            }
        }
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
        await Pages.load_dashboard();

        // Show UI
        document.getElementById('loading-screen').classList.add('hidden');
        document.getElementById('app-header').classList.remove('hidden');
        document.getElementById('main-content').classList.remove('hidden');
        document.getElementById('bottom-nav').classList.remove('hidden');
    } catch (e) {
        document.getElementById('loading-screen').innerHTML = `
            <div class="empty-state">
                <div class="empty-icon">❌</div>
                <p>خطا در بارگذاری</p>
                <p style="font-size:12px;color:var(--text-muted);margin-top:8px">${e.message}</p>
                <button class="btn btn-primary" style="margin-top:16px" onclick="location.reload()">🔄 تلاش مجدد</button>
            </div>
        `;
    }
})();
