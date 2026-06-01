/**
 * App initialization — premium experience with haptic feedback & pull-to-refresh.
 */
(async function init() {
    const tg = window.Telegram?.WebApp;
    if (tg) {
        tg.ready();
        tg.expand();
        try { tg.enableClosingConfirmation(); } catch {}
        // Match the native header/background to the app's --bg-app (#0b0d12)
        // and the <meta theme-color> — otherwise a different black creates a
        // visible seam at the top of the screen.
        try { tg.setHeaderColor('#070611'); } catch {}
        try { tg.setBackgroundColor('#070611'); } catch {}
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

    // Pull-to-refresh was removed in v6 — users reported accidental
    // refreshes mid-scroll because the threshold was too low and the
    // gesture overlapped with normal upward scrolling. The header
    // refresh button (top-right) still triggers a manual reload.

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
