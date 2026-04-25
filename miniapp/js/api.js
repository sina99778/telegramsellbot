/**
 * API client for the Mini App.
 * Handles all communication with the backend through Telegram initData auth.
 */
const API = (() => {
    const BASE = '/api/miniapp';

    function getInitData() {
        try {
            return window.Telegram?.WebApp?.initData || '';
        } catch {
            return '';
        }
    }

    async function request(method, path, body = null) {
        const headers = {
            'X-Telegram-Init-Data': getInitData(),
            'Content-Type': 'application/json',
        };
        const opts = { method, headers };
        if (body) opts.body = JSON.stringify(body);

        const res = await fetch(`${BASE}${path}`, opts);
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: 'Unknown error' }));
            throw new Error(err.detail || `HTTP ${res.status}`);
        }
        return res.json();
    }

    return {
        getDashboard:  ()              => request('GET', '/me'),
        getPlans:      ()              => request('GET', '/plans'),
        getTransactions: (page = 1)    => request('GET', `/wallet/transactions?page=${page}`),
        getTickets:    ()              => request('GET', '/tickets'),
        sendTicket:    (text)          => request('POST', '/tickets/send', { text }),
        getReferral:   ()              => request('GET', '/referral'),
    };
})();
