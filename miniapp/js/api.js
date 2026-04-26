/**
 * API client — sends initData as query param to avoid header issues.
 */
const API = (() => {
    const INIT_DATA_WAIT_MS = 3000;
    const INIT_DATA_POLL_MS = 100;

    function readTelegramDataFromUrl() {
        const sources = [window.location.hash, window.location.search];
        for (const source of sources) {
            if (!source) continue;
            const queryStart = source.indexOf('tgWebAppData=');
            const rawParams = queryStart >= 0 ? source.slice(queryStart) : source.replace(/^[#?]/, '');
            const params = new URLSearchParams(rawParams);
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

    function sleep(ms) {
        return new Promise(resolve => setTimeout(resolve, ms));
    }

    async function waitForInitData() {
        const startedAt = Date.now();
        let initData = getInitData();
        while (!initData && Date.now() - startedAt < INIT_DATA_WAIT_MS) {
            await sleep(INIT_DATA_POLL_MS);
            initData = getInitData();
        }
        return initData;
    }

    async function request(method, path, body = null) {
        const initData = await waitForInitData();

        // Send initData as query parameter (avoids header stripping by proxies)
        const separator = path.includes('?') ? '&' : '?';
        const url = initData
            ? `/api/miniapp${path}${separator}_auth=${encodeURIComponent(initData)}`
            : `/api/miniapp${path}`;

        const headers = { 'Content-Type': 'application/json' };
        if (initData) headers['X-Telegram-Init-Data'] = initData;
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
