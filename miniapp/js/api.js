/**
 * API client — sends initData as query param to avoid header issues.
 */
const API = (() => {
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

    function getSessionToken() {
        try {
            const params = new URLSearchParams(window.location.search.replace(/^[?]/, ''));
            const token = params.get('session') || '';
            if (token) sessionStorage.setItem('miniapp_session', token);
            return token || sessionStorage.getItem('miniapp_session') || '';
        } catch {
            return '';
        }
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
        const sessionToken = getSessionToken();
        const initData = sessionToken ? '' : getInitData();

        // Send initData as query parameter (avoids header stripping by proxies)
        const separator = path.includes('?') ? '&' : '?';
        let url = `/api/miniapp${path}`;
        if (initData) {
            url += `${separator}_auth=${encodeURIComponent(initData)}`;
        } else if (sessionToken) {
            url += `${separator}_session=${encodeURIComponent(sessionToken)}`;
        }

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
        createPurchase:  (payload)    => request('POST', '/purchase', payload),
        getRenewalQuote:(payload)    => request('POST', '/renewal/quote', payload),
        renewConfig:     (payload)    => request('POST', '/renewal', payload),
        getAdminOverview:()           => request('GET', '/admin/overview'),
        getAdminSection:(section)     => request('GET', `/admin/section/${encodeURIComponent(section)}`),
        searchAdminUsers:(q = '', page = 1) => request('GET', `/admin/users/search?q=${encodeURIComponent(q)}&page=${page}`),
        getAdminUser:   (id)          => request('GET', `/admin/users/${encodeURIComponent(id)}`),
        adjustAdminUserBalance:(id, amount) => request('POST', `/admin/users/${encodeURIComponent(id)}/balance`, { amount }),
        sendAdminUserMessage:(id, text) => request('POST', `/admin/users/${encodeURIComponent(id)}/message`, { text }),
        runAdminAction: (payload)     => request('POST', '/admin/action', payload),
        createReadyPlan:(payload)     => request('POST', '/admin/ready-configs/plans', payload),
        addReadyConfigs:(id, content) => request('POST', `/admin/ready-configs/${encodeURIComponent(id)}/items`, { content }),
        createTopup:    (payload)     => request('POST', '/wallet/topup', payload),
        getTransactions: (page = 1)   => request('GET', `/wallet/transactions?page=${page}`),
        getPayments:     (page = 1)   => request('GET', `/payments?page=${page}`),
        refreshPayment:  (id)         => request('POST', `/payments/${encodeURIComponent(id)}/refresh`),
        getTickets:      ()           => request('GET', '/tickets'),
        sendTicket:      (text)       => request('POST', '/tickets/send', { text }),
        closeTicket:     (id)         => request('POST', `/tickets/${encodeURIComponent(id)}/close`),
        getAdminTicket:  (id)         => request('GET', `/admin/tickets/${encodeURIComponent(id)}`),
        replyAdminTicket:(id, text)   => request('POST', `/admin/tickets/${encodeURIComponent(id)}/reply`, { text }),
        getReferral:     ()           => request('GET', '/referral'),
    };
})();
