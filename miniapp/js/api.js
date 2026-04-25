/**
 * API client for the Mini App.
 * Uses current page origin so API requests always go to the same domain.
 */
const API = (() => {
    const BASE = `${window.location.origin}/api/miniapp`;

    function getInitData() {
        try {
            const data = window.Telegram?.WebApp?.initData;
            console.log('[API] initData length:', data?.length || 0);
            if (data) console.log('[API] initData preview:', data.substring(0, 80));
            return data || '';
        } catch (e) {
            console.error('[API] Failed to get initData:', e);
            return '';
        }
    }

    async function request(method, path, body = null) {
        const initData = getInitData();
        if (!initData) {
            console.warn('[API] No initData — are we inside Telegram WebApp?');
        }

        const headers = {
            'Content-Type': 'application/json',
        };
        // Only add auth header if initData is available
        if (initData) {
            headers['X-Telegram-Init-Data'] = initData;
        }

        const opts = { method, headers };
        if (body) opts.body = JSON.stringify(body);

        const url = `${BASE}${path}`;
        console.log('[API] Request:', method, url);

        const res = await fetch(url, opts);
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(err.detail || `HTTP ${res.status}`);
        }
        return res.json();
    }

    return {
        getInitData,
        getDashboard:  ()              => request('GET', '/me'),
        getPlans:      ()              => request('GET', '/plans'),
        getTransactions: (page = 1)    => request('GET', `/wallet/transactions?page=${page}`),
        getTickets:    ()              => request('GET', '/tickets'),
        sendTicket:    (text)          => request('POST', '/tickets/send', { text }),
        getReferral:   ()              => request('GET', '/referral'),
    };
})();
