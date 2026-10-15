/**
 * API client — sends authentication only through request headers.
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
            const fragment = new URLSearchParams(window.location.hash.replace(/^#/, ''));
            const token = fragment.get('session') || params.get('session') || '';
            if (token) {
                sessionStorage.setItem('miniapp_session', token);
                params.delete('session');
                fragment.delete('session');
                const query = params.toString();
                history.replaceState(null, '', location.pathname + (query ? '?' + query : '') + (fragment.toString() ? '#' + fragment.toString() : ''));
            }
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
        // Fresh Telegram initData always wins: the bot-signed session token
        // is frozen at keyboard render time (24h TTL) and would lock users
        // out once expired, while initData is re-issued on every launch.
        // The session token is only a fallback when no initData exists.
        const sessionToken = getSessionToken();
        const initData = getInitData();

        // Authentication stays out of URLs and access logs.
        const url = `/api/miniapp${path}`;

        const headers = { 'Content-Type': 'application/json' };
        if (initData) headers['X-Telegram-Init-Data'] = initData;
        else if (sessionToken) headers['X-Miniapp-Session'] = sessionToken;
        const opts = { method, headers };
        if (body) opts.body = JSON.stringify(body);

        const res = await fetchWithReadTimeout(url, opts);
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: 'خطای ناشناخته' }));
            let detail = err.detail;
            // FastAPI 422 returns `detail` as an array of {msg, loc, ...};
            // new Error([...]) would render "[object Object]" in the toast.
            if (Array.isArray(detail)) {
                detail = detail.map(d => (d && d.msg) ? d.msg : '').filter(Boolean).join('، ');
            }
            throw new Error(detail || `خطای سرور (${res.status})`);
        }
        return res.json();
    }

    /**
     * Multipart/form-data variant for file uploads (e.g. card receipt
     * photos). We MUST NOT set Content-Type ourselves — the browser adds
     * the multipart boundary automatically. Auth still travels as the
     * header fields like the JSON `request()`.
     */
    async function requestForm(method, path, formData) {
        // Same precedence as request(): fresh initData first, session token
        // only as a fallback when no initData exists.
        const sessionToken = getSessionToken();
        const initData = getInitData();
        const url = `/api/miniapp${path}`;
        const headers = {};
        if (initData) headers['X-Telegram-Init-Data'] = initData;
        else if (sessionToken) headers['X-Miniapp-Session'] = sessionToken;

        const res = await fetch(url, { method, headers, body: formData });
        if (!res.ok) {
            const err = await res.json().catch(() => ({ detail: 'خطای ناشناخته' }));
            let detail = err.detail;
            if (Array.isArray(detail)) {
                detail = detail.map(d => (d && d.msg) ? d.msg : '').filter(Boolean).join('، ');
            }
            throw new Error(detail || `خطای سرور (${res.status})`);
        }
        return res.json();
    }

    async function qrUrl(data) {
        const headers = {'Content-Type': 'application/json'};
        const initData = getInitData();
        if (initData) headers['X-Telegram-Init-Data'] = initData;
        else headers['X-Miniapp-Session'] = getSessionToken();
        const response = await fetch('/api/miniapp/qr', {method:'POST', headers, body:JSON.stringify({data})});
        if (!response.ok) throw new Error('QR unavailable');
        return URL.createObjectURL(await response.blob());
    }

    // Read-only requests may safely time out without ambiguity about payments.
    // Never automatically replay a purchase, renewal or uploaded receipt.
    async function fetchWithReadTimeout(url, options) {
        if (options.method !== 'GET') return fetch(url, options);
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 20000);
        try {
            const response = await fetch(url, {...options, signal:controller.signal});
            // Keep the body under the same deadline; otherwise a stalled body
            // leaves a skeleton forever even though headers arrived promptly.
            const body = await response.text();
            return {ok:response.ok, status:response.status, json:async () => JSON.parse(body)};
        } catch (error) {
            if (controller.signal.aborted) throw new Error('دریافت اطلاعات طول کشید؛ اتصال را بررسی و دوباره تلاش کنید.');
            throw error;
        } finally {
            clearTimeout(timer);
        }
    }

    return {
        qrUrl,
        getInitData,
        getConfig:       ()           => request('GET', '/config'),
        getDashboard:    ()           => request('GET', '/me'),
        getConfigs:      (page = 1)   => request('GET', `/configs?page=${page}`),
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
        transferUserConfigs:(id, payload) => request('POST', `/admin/users/${encodeURIComponent(id)}/transfer-configs`, payload),
        runAdminAction: (payload)     => request('POST', '/admin/action', payload),
        grantAdminGift:(payload)      => request('POST', '/admin/gifts', payload),
        getAdminCustomerReport:(period = 'daily', userId = '') => request('GET', `/admin/reports/customers?period=${encodeURIComponent(period)}${userId ? `&user_id=${encodeURIComponent(userId)}` : ''}`),
        updateAdminPlanName:(id, name) => request('POST', `/admin/plans/${encodeURIComponent(id)}/name`, { name }),
        updateAdminPlanDuration:(id, duration_days) => request('POST', `/admin/plans/${encodeURIComponent(id)}/duration`, { duration_days }),
        updateAdminPlanPrice:(id, price) => request('POST', `/admin/plans/${encodeURIComponent(id)}/price`, { price }),
        updateAdminPlanStock:(id, sales_limit) => request('POST', `/admin/plans/${encodeURIComponent(id)}/stock`, { sales_limit }),
        updateAdminServerSubScheme:(id, scheme) => request('POST', `/admin/servers/${encodeURIComponent(id)}/sub-scheme`, { scheme }),
        updateCustomPurchaseSettings:(payload) => request('POST', '/admin/custom-purchase', payload),
        createReadyPlan:(payload)     => request('POST', '/admin/ready-configs/plans', payload),
        addReadyConfigs:(id, content) => request('POST', `/admin/ready-configs/${encodeURIComponent(id)}/items`, { content }),
        createTopup:    (payload)     => request('POST', '/wallet/topup', payload),
        uploadCardReceipt:(paymentId, file) => {
            const fd = new FormData();
            fd.append('payment_id', paymentId);
            fd.append('photo', file);
            return requestForm('POST', '/payments/card_receipt', fd);
        },
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
