/**
 * API client — sends initData as query param to avoid header issues.
 */
const API = (() => {

    function readTelegramDataFromUrl() {
        const sources = [window.location.hash, window.location.search];
        for (const source of sources) {
            if (!source) continue;
            const params = new URLSearchParams(source.replace(/^[#?]/, ''));
            const webAppData = params.get('tgWebAppData');
            if (webAppData) return webAppData;
        }
        return '';
    }

    function getInitData() {
        try {
            const sdkInitData = window.Telegram?.WebApp?.initData || '';
            const initData = sdkInitData || readTelegramDataFromUrl();
            if (initData) {
                sessionStorage.setItem('telegram_init_data', initData);
                return initData;
            }
            return sessionStorage.getItem('telegram_init_data') || '';
        } catch {
            return '';
        }
    }

    async function request(method, path, body = null) {
        const initData = getInitData();

        // Send initData as query parameter (avoids header stripping by proxies)
        const separator = path.includes('?') ? '&' : '?';
        const url = initData
            ? `/api/miniapp${path}${separator}_auth=${encodeURIComponent(initData)}`
            : `/api/miniapp${path}`;

        const headers = { 'Content-Type': 'application/json' };
        const opts = { method, headers };
        if (body) opts.body = JSON.stringify(body);

        const res = await fetch(url, opts);
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(err.detail || `HTTP ${res.status}`);
        }
        return res.json();
    }

    return {
        getInitData,
        getConfig:       ()           => request('GET', '/config'),
        getDashboard:    ()           => request('GET', '/me'),
        getPlans:        ()           => request('GET', '/plans'),
        getTransactions: (page = 1)   => request('GET', `/wallet/transactions?page=${page}`),
        getTickets:      ()           => request('GET', '/tickets'),
        sendTicket:      (text)       => request('POST', '/tickets/send', { text }),
        getReferral:     ()           => request('GET', '/referral'),
    };
})();
