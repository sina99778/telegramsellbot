/**
 * Page renderers — each page has a load_ function that fetches data and renders.
 */
const Pages = (() => {
    let dashboardData = null;
    let plansCache = [];
    let supportTicketsCache = [];
    let adminUserSearchState = { q: '', page: 1 };

    function getBotUsername() {
        return (
            window.AppConfig?.bot_username ||
            window.Telegram?.WebApp?.initDataUnsafe?.bot?.username ||
            ''
        ).replace(/^@/, '');
    }

    function escapeHtml(value) {
        return String(value ?? '').replace(/[&<>"']/g, ch => ({
            '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
        }[ch]));
    }

    // ═══════════════════════════════════════════════════════════════════════
    // DASHBOARD
    // ═══════════════════════════════════════════════════════════════════════
    async function load_dashboard() {
        try {
            dashboardData = await API.getDashboard();
            renderDashboard(dashboardData);
        } catch (e) {
            UI.toast('خطا در بارگذاری: ' + e.message, 'error');
        }
    }

    function renderDashboard(data) {
        window.AppState = data;
        const adminNav = document.getElementById('admin-nav-btn');
        if (adminNav) adminNav.classList.toggle('hidden', !data.is_admin);
        // Update header
        document.getElementById('user-name').textContent = data.first_name || 'کاربر';
        document.getElementById('user-balance').textContent = `$${UI.formatMoney(data.wallet.balance)}`;
        document.getElementById('user-avatar').textContent = (data.first_name || 'U')[0].toUpperCase();

        // Stats
        const usagePct = UI.getUsagePercent(data.total_volume_used, data.total_volume);
        document.getElementById('stats-grid').innerHTML = `
            <div class="stat-card">
                <div class="stat-icon">${UI.icon('server')}</div>
                <div class="stat-value">${data.active_config_count}</div>
                <div class="stat-label">سرویس فعال</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">${UI.icon('wallet')}</div>
                <div class="stat-value">$${UI.formatMoney(data.wallet.balance)}</div>
                <div class="stat-label">موجودی کیف پول</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">${UI.icon('chart')}</div>
                <div class="stat-value">${usagePct}%</div>
                <div class="stat-label">مصرف کل</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">${UI.icon('database')}</div>
                <div class="stat-value">${UI.formatBytes(data.total_volume_used)}</div>
                <div class="stat-label">ترافیک مصرفی</div>
            </div>
        `;

        // Active configs
        const activeSubs = data.subscriptions.filter(s => s.status === 'active' || s.status === 'pending_activation');
        const container = document.getElementById('configs-list');

        if (activeSubs.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">${UI.icon('package')}</div>
                    <p>هنوز سرویسی ندارید</p>
                    <button class="btn btn-primary" style="margin-top:12px" onclick="UI.navigate('store')">${UI.icon('store')} خرید سرویس</button>
                </div>
            `;
            return;
        }

        container.innerHTML = activeSubs.map(sub => renderConfigCard(sub)).join('');
    }

    function renderConfigCard(sub, showActions = false) {
        const pct = UI.getUsagePercent(sub.used_bytes, sub.volume_bytes);
        const pctClass = UI.getProgressClass(pct);
        const statusText = UI.getStatusText(sub.status);
        const statusClass = UI.getStatusClass(sub.status);
        const name = sub.config_name || sub.plan_name || 'سرویس';

        const safeSubLink = sub.sub_link ? encodeURIComponent(sub.sub_link) : '';
        const actionsHtml = `
            <div class="config-actions" onclick="event.stopPropagation()">
                <button class="btn btn-primary btn-sm" onclick="Pages.showRenewal('${sub.id}')">تمدید</button>
                ${sub.sub_link ? `<button class="btn btn-secondary btn-sm" onclick="UI.copyToClipboard(decodeURIComponent('${safeSubLink}'))">کپی لینک</button>` : ''}
                <button class="btn btn-secondary btn-sm" onclick="Pages.showConfigDetail('${sub.id}')">جزئیات</button>
            </div>
        `;

        return `
            <div class="config-card" onclick="Pages.showConfigDetail('${sub.id}')">
                <div class="config-header">
                    <span class="config-name">${name}</span>
                    <span class="config-status ${statusClass}">${statusText}</span>
                </div>
                <div class="progress-bar-container">
                    <div class="progress-bar-fill ${pctClass}" style="width: ${pct}%"></div>
                </div>
                <div class="config-stats">
                    <span>${UI.formatBytes(sub.used_bytes)} / ${UI.formatBytes(sub.volume_bytes)}</span>
                    <span>${UI.daysLeft(sub.ends_at)}</span>
                </div>
                ${actionsHtml}
            </div>
        `;
    }

    function showConfigDetail(subId) {
        if (!dashboardData) return;
        const sub = dashboardData.subscriptions.find(s => s.id === subId);
        if (!sub) return;

        const pct = UI.getUsagePercent(sub.used_bytes, sub.volume_bytes);
        const statusText = UI.getStatusText(sub.status);
        const name = sub.config_name || sub.plan_name || 'سرویس';

        let linkSection = '';
        if (sub.sub_link) {
            const safeSubLink = encodeURIComponent(sub.sub_link);
            linkSection = `
                <div style="margin-top:16px">
                    <p style="font-size:12px;color:var(--text-muted);margin-bottom:6px">لینک اشتراک</p>
                    <div class="copy-box" onclick="UI.copyToClipboard(decodeURIComponent('${safeSubLink}'))">${escapeHtml(sub.sub_link)}</div>
                </div>
                <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
                    <button class="btn btn-secondary btn-sm" onclick="UI.copyToClipboard(decodeURIComponent('${safeSubLink}'))">کپی</button>
                </div>
            `;
        }

        UI.showModal(`
            <div class="modal-title">${UI.icon('configs')} ${name}</div>
            <div style="text-align:center;margin-bottom:16px">
                <div style="font-size:36px;font-weight:800;color:var(--accent-primary)">${pct}%</div>
                <p style="font-size:12px;color:var(--text-muted)">مصرف شده</p>
            </div>
            <div class="progress-bar-container" style="height:8px;margin-bottom:16px">
                <div class="progress-bar-fill ${UI.getProgressClass(pct)}" style="width:${pct}%"></div>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:13px">
                <div>
                    <span style="color:var(--text-muted)">وضعیت</span>
                    <div style="font-weight:600;margin-top:2px">${statusText}</div>
                </div>
                <div>
                    <span style="color:var(--text-muted)">باقیمانده</span>
                    <div style="font-weight:600;margin-top:2px">${UI.daysLeft(sub.ends_at)}</div>
                </div>
                <div>
                    <span style="color:var(--text-muted)">مصرف</span>
                    <div style="font-weight:600;margin-top:2px">${UI.formatBytes(sub.used_bytes)}</div>
                </div>
                <div>
                    <span style="color:var(--text-muted)">کل حجم</span>
                    <div style="font-weight:600;margin-top:2px">${UI.formatBytes(sub.volume_bytes)}</div>
                </div>
                <div>
                    <span style="color:var(--text-muted)">پلن</span>
                    <div style="font-weight:600;margin-top:2px">${sub.plan_name || '-'}</div>
                </div>
                <div>
                    <span style="color:var(--text-muted)">شروع</span>
                    <div style="font-weight:600;margin-top:2px">${UI.formatDate(sub.starts_at)}</div>
                </div>
            </div>
            ${linkSection}
            <button class="btn btn-primary btn-block" style="margin-top:16px" onclick="Pages.showRenewal('${sub.id}')">تمدید سرویس</button>
            <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">بستن</button>
        `);
    }

    function showRenewal(subId) {
        if (!dashboardData) return;
        const sub = dashboardData.subscriptions.find(s => s.id === subId);
        if (!sub) return;
        const name = sub.config_name || sub.plan_name || 'سرویس';
        UI.showModal(`
            <div class="modal-title">تمدید ${escapeHtml(name)}</div>
            <div class="renewal-tabs">
                <button class="renewal-tab active" data-renew-type="time" onclick="Pages.setRenewalType('time')">زمان</button>
                <button class="renewal-tab" data-renew-type="volume" onclick="Pages.setRenewalType('volume')">حجم</button>
            </div>
            <label class="form-label" id="renewal-amount-label" for="renewal-amount">تعداد روز</label>
            <input id="renewal-amount" class="form-input" inputmode="decimal" dir="ltr" placeholder="30" value="30">
            <p class="form-hint" id="renewal-hint">مدت موردنظر را به روز وارد کنید.</p>
            <div class="renewal-price-box" id="renewal-price-box">برای محاسبه قیمت، مقدار را وارد کنید.</div>
            <button class="btn btn-primary btn-block" onclick="Pages.submitRenewal('${sub.id}')">تمدید با کیف پول</button>
            <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">انصراف</button>
        `);
        setRenewalType('time');
        const input = document.getElementById('renewal-amount');
        input?.addEventListener('input', () => updateRenewalQuote(sub.id));
        updateRenewalQuote(sub.id);
    }

    function setRenewalType(type) {
        document.querySelectorAll('.renewal-tab').forEach(btn => {
            btn.classList.toggle('active', btn.dataset.renewType === type);
        });
        const input = document.getElementById('renewal-amount');
        const label = document.getElementById('renewal-amount-label');
        const hint = document.getElementById('renewal-hint');
        if (type === 'volume') {
            if (label) label.textContent = 'حجم اضافه';
            if (hint) hint.textContent = 'حجم موردنظر را به گیگابایت وارد کنید.';
            if (input) input.value = input.value || '10';
        } else {
            if (label) label.textContent = 'تعداد روز';
            if (hint) hint.textContent = 'مدت موردنظر را به روز وارد کنید.';
            if (input) input.value = input.value || '30';
        }
        const subId = document.querySelector('.btn.btn-primary.btn-block[onclick^="Pages.submitRenewal"]')
            ?.getAttribute('onclick')?.match(/'([^']+)'/)?.[1];
        if (subId) updateRenewalQuote(subId);
    }

    function getSelectedRenewalType() {
        return document.querySelector('.renewal-tab.active')?.dataset.renewType || 'time';
    }

    async function updateRenewalQuote(subId) {
        const box = document.getElementById('renewal-price-box');
        const amount = parseFloat(document.getElementById('renewal-amount')?.value || '0');
        const renewType = getSelectedRenewalType();
        if (!box || !amount || amount <= 0) {
            if (box) box.textContent = 'مقدار تمدید معتبر نیست.';
            return;
        }
        try {
            const quote = await API.getRenewalQuote({
                subscription_id: subId,
                renew_type: renewType,
                amount,
            });
            box.innerHTML = `<span>هزینه تمدید</span><strong>$${UI.formatMoney(quote.price)}</strong>`;
        } catch (e) {
            box.textContent = e.message;
        }
    }

    async function submitRenewal(subId) {
        const amount = parseFloat(document.getElementById('renewal-amount')?.value || '0');
        const renewType = getSelectedRenewalType();
        if (!amount || amount <= 0) {
            UI.toast('مقدار تمدید معتبر نیست', 'error');
            return;
        }
        try {
            UI.toast('در حال تمدید...');
            const result = await API.renewConfig({
                subscription_id: subId,
                renew_type: renewType,
                amount,
                payment_method: 'wallet',
            });
            UI.toast(result.message);
            UI.closeModal();
            dashboardData = await API.getDashboard();
            renderDashboard(dashboardData);
            renderAllConfigs(dashboardData.subscriptions);
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // STORE
    // ═══════════════════════════════════════════════════════════════════════
    async function load_store() {
        try {
            const data = await API.getPlans();
            plansCache = data.plans || [];
            renderPlans(data.plans);
        } catch (e) {
            UI.toast('خطا: ' + e.message, 'error');
        }
    }

    function renderPlans(plans) {
        const container = document.getElementById('plans-list');
        if (!plans.length) {
            container.innerHTML = `<div class="empty-state"><div class="empty-icon">${UI.icon('store')}</div><p>هیچ پلنی موجود نیست</p></div>`;
            return;
        }

        container.innerHTML = plans.map((plan, i) => `
            <div class="plan-card ${i === 1 ? 'popular' : ''}">
                <div class="plan-name">${plan.name}</div>
                <div class="plan-specs">
                    <span class="plan-spec">${UI.icon('database')} ${plan.volume_gb} GB</span>
                    <span class="plan-spec">${UI.icon('clock')} ${plan.duration_days} روز</span>
                    <span class="plan-spec">${UI.icon('lock')} ${plan.protocol}</span>
                </div>
                <div class="plan-price">$${UI.formatMoney(plan.price)} <small>/ ${plan.currency}</small></div>
                <button class="btn btn-primary btn-block plan-buy-btn" onclick="Pages.buyPlan('${plan.id}')">
                    ${UI.icon('store')} خرید
                </button>
            </div>
        `).join('');
    }

    function buyPlan(planId) {
        const plan = plansCache.find(item => item.id === planId);
        if (!plan) {
            UI.toast('پلن پیدا نشد', 'error');
            return;
        }

        UI.showModal(`
            <div class="modal-title">خرید ${escapeHtml(plan.name)}</div>
            <div class="checkout-summary">
                <div><span>حجم</span><strong>${escapeHtml(plan.volume_gb)} GB</strong></div>
                <div><span>مدت</span><strong>${escapeHtml(plan.duration_days)} روز</strong></div>
                <div><span>قیمت</span><strong>$${UI.formatMoney(plan.price)}</strong></div>
            </div>
            <label class="form-label" for="checkout-config-name">نام کانفیگ</label>
            <input id="checkout-config-name" class="form-input" dir="ltr" maxlength="32" placeholder="MyVPN" autocomplete="off">
            <p class="form-hint">فقط حروف انگلیسی، عدد، خط تیره و آندرلاین. ۳ تا ۳۲ کاراکتر.</p>
            <div class="checkout-methods">
                <button class="btn btn-primary btn-block" onclick="Pages.submitPurchase('${plan.id}', 'wallet')">پرداخت با کیف پول</button>
                <button class="btn btn-secondary btn-block" onclick="Pages.submitPurchase('${plan.id}', 'tetrapay')">درگاه ریالی تتراپی</button>
                <button class="btn btn-secondary btn-block" onclick="Pages.submitPurchase('${plan.id}', 'nowpayments')">درگاه ارزی NOWPayments</button>
            </div>
            <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">انصراف</button>
        `);
        setTimeout(() => document.getElementById('checkout-config-name')?.focus(), 100);
    }

    async function submitPurchase(planId, paymentMethod) {
        const input = document.getElementById('checkout-config-name');
        const configName = (input?.value || '').trim();
        if (!/^[a-zA-Z0-9_-]{3,32}$/.test(configName)) {
            UI.toast('نام کانفیگ نامعتبر است', 'error');
            input?.focus();
            return;
        }

        try {
            UI.toast('در حال ساخت سفارش...');
            const result = await API.createPurchase({
                plan_id: planId,
                config_name: configName,
                payment_method: paymentMethod,
            });

            if (result.invoice_url) {
                UI.showModal(`
                    <div class="modal-title">فاکتور آماده است</div>
                    <p class="form-hint" style="text-align:center;margin-bottom:14px">${escapeHtml(result.message)}</p>
                    <button class="btn btn-primary btn-block" onclick="Pages.openInvoice(decodeURIComponent('${encodeURIComponent(result.invoice_url)}'))">پرداخت فاکتور</button>
                    <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">بستن</button>
                `);
                return;
            }

            UI.showModal(`
                <div class="modal-title">کانفیگ ساخته شد</div>
                <p class="form-hint" style="text-align:center;margin-bottom:12px">${escapeHtml(result.message)}</p>
                ${result.sub_link ? `<div class="copy-box" onclick="UI.copyToClipboard(decodeURIComponent('${encodeURIComponent(result.sub_link)}'))">${escapeHtml(result.sub_link)}</div>` : ''}
                ${result.vless_uri ? `<div class="copy-box" style="margin-top:10px" onclick="UI.copyToClipboard(decodeURIComponent('${encodeURIComponent(result.vless_uri)}'))">${escapeHtml(result.vless_uri)}</div>` : ''}
                <button class="btn btn-primary btn-block" style="margin-top:12px" onclick="UI.closeModal(); Pages.load_dashboard(); UI.navigate('configs')">مشاهده سرویس‌ها</button>
            `);
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    function openInvoice(url) {
        try {
            window.Telegram?.WebApp?.openLink(url);
        } catch {
            window.open(url, '_blank');
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // CONFIGS
    // ═══════════════════════════════════════════════════════════════════════
    async function load_configs() {
        try {
            if (!dashboardData) dashboardData = await API.getDashboard();
            renderAllConfigs(dashboardData.subscriptions);
        } catch (e) {
            UI.toast('خطا: ' + e.message, 'error');
        }
    }

    function renderAllConfigs(subs) {
        const container = document.getElementById('all-configs-list');
        if (!subs.length) {
            container.innerHTML = `<div class="empty-state"><div class="empty-icon">${UI.icon('package')}</div><p>هنوز سرویسی ندارید</p></div>`;
            return;
        }
        container.innerHTML = subs.map(sub => renderConfigCard(sub, true)).join('');
    }

    // ═══════════════════════════════════════════════════════════════════════
    // WALLET
    // ═══════════════════════════════════════════════════════════════════════
    async function load_wallet() {
        try {
            if (!dashboardData) dashboardData = await API.getDashboard();
            renderWalletCard(dashboardData.wallet);
            const paymentData = await API.getPayments(1);
            renderPayments(paymentData.payments || []);
            const txData = await API.getTransactions(1);
            renderTransactions(txData.transactions);
        } catch (e) {
            UI.toast('خطا: ' + e.message, 'error');
        }
    }

    function renderWalletCard(wallet) {
        document.getElementById('wallet-card').innerHTML = `
                <div class="wallet-label">${UI.icon('wallet')} موجودی کیف پول</div>
            <div class="wallet-balance-display">$${UI.formatMoney(wallet.balance)}</div>
            <div class="wallet-actions">
                <button class="btn" onclick="Pages.topupWallet()">${UI.icon('plus')} شارژ</button>
            </div>
        `;
    }

    function renderPayments(payments) {
        const container = document.getElementById('payments-list');
        if (!container) return;
        if (!payments.length) {
            container.innerHTML = '<div class="empty-state compact"><p>پرداختی ثبت نشده</p></div>';
            return;
        }

        container.innerHTML = payments.map(payment => {
            const canRefresh = ['nowpayments', 'tetrapay'].includes(payment.provider)
                && ['waiting', 'pending', 'confirming'].includes(payment.payment_status);
            const amount = payment.pay_amount
                ? `${UI.formatMoney(payment.pay_amount)} ${payment.pay_currency || ''}`
                : `${UI.formatMoney(payment.price_amount)} ${payment.price_currency}`;
            return `
                <div class="payment-item">
                    <div>
                        <strong>${escapeHtml(payment.provider)} | ${escapeHtml(payment.kind)}</strong>
                        <span>${escapeHtml(payment.payment_status)} | ${escapeHtml(amount)} | ${UI.formatDate(payment.created_at)}</span>
                    </div>
                    ${canRefresh ? `<button class="btn btn-secondary btn-sm" onclick="Pages.refreshPayment('${payment.id}')">بررسی</button>` : ''}
                </div>
            `;
        }).join('');
    }

    async function refreshPayment(paymentId) {
        try {
            UI.toast('در حال بررسی پرداخت...');
            const result = await API.refreshPayment(paymentId);
            UI.toast(result.message || 'وضعیت پرداخت بررسی شد');
            const paymentData = await API.getPayments(1);
            renderPayments(paymentData.payments || []);
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    function renderTransactions(txs) {
        const container = document.getElementById('transactions-list');
        if (!txs.length) {
            container.innerHTML = `<div class="empty-state"><div class="empty-icon">${UI.icon('chart')}</div><p>تراکنشی ثبت نشده</p></div>`;
            return;
        }

        const typeMap = {
            'topup': 'شارژ',
            'purchase': 'خرید',
            'refund': 'بازگشت',
            'referral_bonus': 'پاداش',
            'admin_adjust': 'تنظیم',
            'renewal': 'تمدید',
        };

        container.innerHTML = txs.map(tx => `
            <div class="transaction-item">
                <div class="tx-info">
                    <span class="tx-type">${typeMap[tx.type] || tx.type}</span>
                    <span class="tx-date">${UI.formatDate(tx.created_at)}</span>
                </div>
                <span class="tx-amount ${tx.direction === 'credit' ? 'credit' : 'debit'}">
                    ${tx.direction === 'credit' ? '+' : '-'}$${UI.formatMoney(tx.amount)}
                </span>
            </div>
        `).join('');
    }

    function topupWallet() {
        UI.showModal(`
            <div class="modal-title">افزایش موجودی</div>
            <label class="form-label" for="topup-amount">مبلغ شارژ (دلار)</label>
            <input id="topup-amount" class="form-input" inputmode="decimal" dir="ltr" placeholder="5" value="5">
            <p class="form-hint">بعد از پرداخت موفق، موجودی کیف پول به صورت خودکار بروزرسانی می‌شود.</p>
            <div class="checkout-methods">
                <button class="btn btn-secondary btn-block" onclick="Pages.submitTopup('tetrapay')">درگاه ریالی تتراپی</button>
                <button class="btn btn-secondary btn-block" onclick="Pages.submitTopup('nowpayments')">درگاه ارزی NOWPayments</button>
            </div>
            <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">انصراف</button>
        `);
        setTimeout(() => document.getElementById('topup-amount')?.focus(), 100);
    }

    async function submitTopup(paymentMethod) {
        const amount = parseFloat(document.getElementById('topup-amount')?.value || '0');
        if (!amount || amount <= 0) {
            UI.toast('مبلغ شارژ معتبر نیست', 'error');
            return;
        }
        try {
            UI.toast('در حال ساخت فاکتور...');
            const result = await API.createTopup({ amount, payment_method: paymentMethod });
            UI.showModal(`
                <div class="modal-title">فاکتور شارژ آماده است</div>
                <p class="form-hint" style="text-align:center;margin-bottom:14px">${escapeHtml(result.message)}</p>
                ${result.pay_amount ? `<div class="renewal-price-box"><span>مبلغ پرداخت</span><strong>${UI.formatMoney(result.pay_amount)} ${escapeHtml(result.pay_currency || '')}</strong></div>` : ''}
                <button class="btn btn-primary btn-block" onclick="Pages.openInvoice(decodeURIComponent('${encodeURIComponent(result.invoice_url)}'))">پرداخت فاکتور</button>
                <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal(); Pages.load_wallet()">بستن</button>
            `);
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // SUPPORT
    // ═══════════════════════════════════════════════════════════════════════
    async function load_support() {
        try {
            const data = await API.getTickets();
            supportTicketsCache = data.tickets || [];
            renderTickets(supportTicketsCache);
        } catch (e) {
            UI.toast('خطا: ' + e.message, 'error');
        }
    }

    function renderTickets(tickets) {
        const container = document.getElementById('tickets-container');
        if (!tickets.length) {
            container.innerHTML = `<div class="empty-state"><div class="empty-icon">${UI.icon('support')}</div><p>پیامی وجود ندارد<br>اولین پیام خود را ارسال کنید</p></div>`;
            return;
        }

        const ticket = tickets[0];
        const olderTickets = tickets.slice(1);
        container.innerHTML = `
            <div class="ticket-toolbar">
                <span>تیکت #${String(ticket.id).slice(0, 8)} | ${escapeHtml(UI.getStatusText(ticket.status))}</span>
                ${ticket.status !== 'closed' ? `<button class="btn btn-secondary btn-sm" onclick="Pages.closeTicket('${ticket.id}')">بستن تیکت</button>` : ''}
            </div>
            <div class="ticket-thread">
                ${ticket.messages.length ? ticket.messages.map(m => renderTicketMessage(m)).join('') : '<div class="empty-state compact"><p>هنوز پیامی ثبت نشده</p></div>'}
            </div>
            ${olderTickets.length ? `
                <h3 class="section-title compact-title">تیکت‌های قبلی</h3>
                <div class="ticket-history">
                    ${olderTickets.map((t, index) => `
                        <button class="ticket-history-item" onclick="Pages.showTicketHistory(${index + 1})">
                            <strong>#${String(t.id).slice(0, 8)}</strong>
                            <span>${escapeHtml(UI.getStatusText(t.status))} | ${UI.formatDate(t.created_at)}</span>
                        </button>
                    `).join('')}
                </div>
            ` : ''}
        `;

        setTimeout(() => container.scrollTop = container.scrollHeight, 100);
    }

    function renderTicketMessage(message) {
        return `
            <div class="chat-bubble ${message.sender_type}">
                ${escapeHtml(message.text || 'تصویر')}
                <span class="bubble-time">${UI.formatDate(message.created_at)}</span>
            </div>
        `;
    }

    function showTicketHistory(index) {
        const ticket = supportTicketsCache[index];
        if (!ticket) return;
        UI.showModal(`
            <div class="modal-title">تیکت #${String(ticket.id).slice(0, 8)}</div>
            <div class="ticket-thread modal-thread">
                ${(ticket.messages || []).map(m => renderTicketMessage(m)).join('') || '<div class="empty-state compact"><p>پیامی ثبت نشده</p></div>'}
            </div>
            <button class="btn btn-secondary btn-block" style="margin-top:12px" onclick="UI.closeModal()">بستن</button>
        `);
    }

    async function closeTicket(ticketId) {
        try {
            const result = await API.closeTicket(ticketId);
            UI.toast(result.message || 'تیکت بسته شد');
            await load_support();
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // REFERRAL
    // ═══════════════════════════════════════════════════════════════════════
    async function load_referral() {
        try {
            const data = await API.getReferral();
            renderReferral(data);
        } catch (e) {
            UI.toast('خطا: ' + e.message, 'error');
        }
    }

    async function load_admin() {
        try {
            const data = await API.getAdminOverview();
            renderAdminPanel(data);
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    function renderAdminPanel(data) {
        const overview = document.getElementById('admin-overview');
        const modules = document.getElementById('admin-modules');
        if (!overview || !modules) return;

        overview.innerHTML = `
            <div class="admin-stat"><span>کاربران</span><strong>${data.users_count}</strong></div>
            <div class="admin-stat"><span>مشتریان</span><strong>${data.customers_count}</strong></div>
            <div class="admin-stat"><span>سرویس فعال</span><strong>${data.active_subscriptions_count}</strong></div>
            <div class="admin-stat"><span>تیکت باز</span><strong>${data.open_tickets_count}</strong></div>
            <div class="admin-stat"><span>پرداخت منتظر</span><strong>${data.waiting_payments_count}</strong></div>
            <div class="admin-stat"><span>سرور فعال</span><strong>${data.active_servers_count}</strong></div>
        `;

        modules.innerHTML = data.modules.map(item => `
            <button class="admin-module" onclick="Pages.openAdminModule('${escapeHtml(item.callback.replace('admin:', ''))}')">
                <strong>${escapeHtml(item.title)}</strong>
                <span>${escapeHtml(item.description)}</span>
            </button>
        `).join('');
    }

    async function openAdminModule(section) {
        try {
            const data = await API.getAdminSection(section);
            renderAdminSection(section, data);
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    function renderAdminSection(section, data) {
        const modules = document.getElementById('admin-modules');
        if (!modules) return;
        const items = data.items || [];
        if (section === 'users' || section === 'customers') {
            renderAdminUsers(section, items);
            return;
        }
        const extra = section === 'ready_configs' ? renderReadyConfigTools(items) : '';
        modules.innerHTML = `
            <button class="btn btn-secondary btn-block" onclick="Pages.load_admin()">بازگشت به مدیریت</button>
            <h3 class="section-title" style="margin-top:16px">${escapeHtml(data.title || 'مدیریت')}</h3>
            ${extra}
            <div class="admin-list">
                ${items.length ? items.map(item => renderAdminItem(section, item)).join('') : '<div class="empty-state"><p>موردی برای نمایش نیست</p></div>'}
            </div>
        `;
    }

    function renderAdminUsers(section, items) {
        const modules = document.getElementById('admin-modules');
        if (!modules) return;
        modules.innerHTML = `
            <button class="btn btn-secondary btn-block" onclick="Pages.load_admin()">بازگشت به مدیریت</button>
            <h3 class="section-title" style="margin-top:16px">${section === 'customers' ? 'مشتریان' : 'مدیریت کاربران'}</h3>
            <div class="admin-form">
                <strong>جستجوی کاربر</strong>
                <div class="admin-search-row">
                    <input id="admin-user-search" class="form-input" placeholder="آیدی تلگرام، یوزرنیم یا نام" value="${escapeHtml(adminUserSearchState.q)}">
                    <button class="btn btn-primary" onclick="Pages.searchAdminUsers(1)">جستجو</button>
                </div>
            </div>
            <div id="admin-users-results" class="admin-list">
                ${items.length ? items.map(item => renderAdminUserSummary(item)).join('') : '<div class="empty-state"><p>کاربری برای نمایش نیست</p></div>'}
            </div>
        `;
        document.getElementById('admin-user-search')?.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') searchAdminUsers(1);
        });
    }

    function renderAdminUserSummary(item) {
        return `
            <div class="admin-item">
                <div>
                    <strong>${escapeHtml(item.title ?? item.name ?? '-')}</strong>
                    <span>${escapeHtml(item.subtitle ?? `${item.telegram_id || '-'} | ${item.role || '-'} | ${item.status || '-'}`)}</span>
                </div>
                <div class="admin-actions">
                    <button class="btn btn-primary btn-sm" onclick="Pages.openAdminUser('${escapeHtml(item.id)}')">پروفایل</button>
                    <button class="btn btn-secondary btn-sm" onclick="Pages.runAdminAction('users', 'toggle_user_ban', '${escapeHtml(item.id)}')">بن/رفع بن</button>
                    <button class="btn btn-secondary btn-sm" onclick="Pages.runAdminAction('users', 'reset_trial', '${escapeHtml(item.id)}')">ریست تست</button>
                </div>
            </div>
        `;
    }

    async function searchAdminUsers(page = 1) {
        const input = document.getElementById('admin-user-search');
        const q = (input?.value || '').trim();
        adminUserSearchState = { q, page };
        try {
            const data = await API.searchAdminUsers(q, page);
            const container = document.getElementById('admin-users-results');
            if (!container) return;
            container.innerHTML = `
                ${data.items.length ? data.items.map(item => renderAdminUserSummary(item)).join('') : '<div class="empty-state"><p>نتیجه‌ای پیدا نشد</p></div>'}
                <div class="admin-pager">
                    <button class="btn btn-secondary btn-sm" ${data.page <= 1 ? 'disabled' : ''} onclick="Pages.searchAdminUsers(${data.page - 1})">قبلی</button>
                    <span>${data.page} / ${Math.max(1, Math.ceil(data.total / data.page_size))}</span>
                    <button class="btn btn-secondary btn-sm" ${!data.has_next ? 'disabled' : ''} onclick="Pages.searchAdminUsers(${data.page + 1})">بعدی</button>
                </div>
            `;
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    async function openAdminUser(userId) {
        try {
            const user = await API.getAdminUser(userId);
            renderAdminUserProfile(user);
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    function renderAdminUserProfile(user) {
        const modules = document.getElementById('admin-modules');
        if (!modules) return;
        const subs = user.subscriptions || [];
        modules.innerHTML = `
            <button class="btn btn-secondary btn-block" onclick="Pages.openAdminModule('users')">بازگشت به کاربران</button>
            <div class="admin-profile">
                <div>
                    <strong>${escapeHtml(user.name || '-')}</strong>
                    <span>${escapeHtml(user.telegram_id)} | @${escapeHtml(user.username || '-')}</span>
                </div>
                <div class="admin-profile-grid">
                    <div><span>نقش</span><strong>${escapeHtml(user.role)}</strong></div>
                    <div><span>وضعیت</span><strong>${escapeHtml(user.status)}</strong></div>
                    <div><span>موجودی</span><strong>$${UI.formatMoney(user.wallet_balance)}</strong></div>
                    <div><span>تست گرفته</span><strong>${user.has_received_free_trial ? 'بله' : 'خیر'}</strong></div>
                </div>
                <div class="admin-actions wide">
                    <button class="btn btn-secondary btn-sm" onclick="Pages.runAdminUserAction('${user.id}', 'toggle_user_ban')">بن/رفع بن</button>
                    <button class="btn btn-secondary btn-sm" onclick="Pages.runAdminUserAction('${user.id}', 'toggle_user_role')">تغییر نقش</button>
                    <button class="btn btn-secondary btn-sm" onclick="Pages.runAdminUserAction('${user.id}', 'reset_trial')">ریست تست</button>
                </div>
            </div>
            <div class="admin-form">
                <strong>تغییر موجودی</strong>
                <input id="admin-balance-amount" class="form-input" inputmode="decimal" placeholder="مثلا 5 یا -2.5">
                <button class="btn btn-primary btn-block" onclick="Pages.adjustAdminUserBalance('${user.id}')">ثبت تغییر موجودی</button>
            </div>
            <div class="admin-form">
                <strong>ارسال پیام به کاربر</strong>
                <textarea id="admin-user-message" class="admin-ticket-reply" rows="4" placeholder="متن پیام..."></textarea>
                <button class="btn btn-primary btn-block" onclick="Pages.sendAdminUserMessage('${user.id}')">ارسال پیام</button>
            </div>
            <h3 class="section-title compact-title">سرویس‌های کاربر</h3>
            <div class="admin-list">
                ${subs.length ? subs.map(sub => `
                    <div class="admin-item">
                        <div>
                            <strong>${escapeHtml(sub.config_name || sub.plan_name || 'سرویس')}</strong>
                            <span>${escapeHtml(sub.status)} | ${UI.formatBytes(sub.used_bytes)} / ${UI.formatBytes(sub.volume_bytes)} | ${UI.daysLeft(sub.ends_at)}</span>
                        </div>
                        ${sub.sub_link ? `<button class="btn btn-secondary btn-sm" onclick="UI.copyToClipboard(decodeURIComponent('${encodeURIComponent(sub.sub_link)}'))">کپی</button>` : ''}
                    </div>
                `).join('') : '<div class="empty-state compact"><p>سرویسی برای این کاربر ثبت نشده</p></div>'}
            </div>
        `;
    }

    function renderReadyConfigTools(items) {
        const pools = items.filter(item => item.id && item.id !== 'ready_configs_help');
        return `
            <div class="admin-form">
                <strong>ساخت پلن آماده</strong>
                <input id="ready-plan-name" class="form-input" placeholder="نام پلن">
                <div class="form-grid">
                    <input id="ready-plan-days" class="form-input" inputmode="numeric" placeholder="روز">
                    <input id="ready-plan-volume" class="form-input" inputmode="numeric" placeholder="GB">
                    <input id="ready-plan-price" class="form-input" inputmode="decimal" placeholder="قیمت دلار">
                </div>
                <button class="btn btn-primary btn-block" onclick="Pages.createReadyConfigPlan()">ساخت پلن</button>
            </div>
            ${pools.length ? `
                <div class="admin-form">
                    <strong>افزودن کانفیگ آماده</strong>
                    <select id="ready-pool-select" class="form-input">
                        ${pools.map(item => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`).join('')}
                    </select>
                    <textarea id="ready-config-content" class="admin-ticket-reply" rows="6" placeholder="هر خط یک کانفیگ"></textarea>
                    <button class="btn btn-primary btn-block" onclick="Pages.addReadyConfigItems()">افزودن به موجودی</button>
                </div>
            ` : ''}
        `;
    }

    function renderAdminItem(section, item) {
        const actions = item.actions || [];
        return `
            <div class="admin-item">
                <div>
                    <strong>${escapeHtml(item.title ?? item.value ?? '-')}</strong>
                    <span>${escapeHtml(item.subtitle ?? (item.value !== undefined ? item.value : ''))}</span>
                </div>
                ${actions.length ? `<div class="admin-actions">
                    ${actions.map(action => `
                        <button class="btn btn-secondary btn-sm" onclick="${action.action === 'view_ticket' ? `Pages.openAdminTicket('${escapeHtml(item.id)}')` : `Pages.runAdminAction('${section}', '${escapeHtml(action.action)}', '${escapeHtml(item.id)}')`}">${escapeHtml(action.label)}</button>
                    `).join('')}
                </div>` : ''}
            </div>
        `;
    }

    async function openAdminTicket(ticketId) {
        try {
            const ticket = await API.getAdminTicket(ticketId);
            renderAdminTicket(ticket);
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    function renderAdminTicket(ticket) {
        const modules = document.getElementById('admin-modules');
        if (!modules) return;
        modules.innerHTML = `
            <button class="btn btn-secondary btn-block" onclick="Pages.openAdminModule('tickets')">بازگشت به تیکت‌ها</button>
            <div class="admin-ticket-head">
                <strong>تیکت #${String(ticket.id).slice(0, 8)}</strong>
                <span>${escapeHtml(ticket.user?.name || '-')} | ${escapeHtml(ticket.user?.telegram_id || '-')} | ${escapeHtml(UI.getStatusText(ticket.status))}</span>
            </div>
            <div class="ticket-thread admin-ticket-thread">
                ${(ticket.messages || []).map(m => renderTicketMessage(m)).join('') || '<div class="empty-state compact"><p>پیامی ثبت نشده</p></div>'}
            </div>
            ${ticket.status !== 'closed' ? `
                <textarea id="admin-ticket-reply" class="admin-ticket-reply" rows="4" placeholder="پاسخ ادمین..."></textarea>
                <button class="btn btn-primary btn-block" onclick="Pages.submitAdminTicketReply('${ticket.id}')">ارسال پاسخ</button>
                <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="Pages.runAdminAction('tickets', 'close_ticket', '${ticket.id}')">بستن تیکت</button>
            ` : '<div class="empty-state compact"><p>این تیکت بسته شده است</p></div>'}
        `;
        setTimeout(() => {
            const thread = modules.querySelector('.admin-ticket-thread');
            if (thread) thread.scrollTop = thread.scrollHeight;
        }, 100);
    }

    async function submitAdminTicketReply(ticketId) {
        const input = document.getElementById('admin-ticket-reply');
        const text = (input?.value || '').trim();
        if (!text) {
            UI.toast('متن پاسخ خالی است', 'error');
            input?.focus();
            return;
        }
        try {
            UI.toast('در حال ارسال پاسخ...');
            const result = await API.replyAdminTicket(ticketId, text);
            UI.toast(result.message || 'پاسخ ارسال شد');
            if (result.ticket) renderAdminTicket(result.ticket);
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    async function runAdminAction(section, action, id) {
        try {
            const result = await API.runAdminAction({ action, id });
            UI.toast(result.message || 'انجام شد');
            await openAdminModule(section);
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    async function runAdminUserAction(userId, action) {
        try {
            const result = await API.runAdminAction({ action, id: userId });
            UI.toast(result.message || 'انجام شد');
            await openAdminUser(userId);
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    async function adjustAdminUserBalance(userId) {
        const amount = document.getElementById('admin-balance-amount')?.value || '0';
        try {
            const result = await API.adjustAdminUserBalance(userId, amount);
            UI.toast(result.message || 'موجودی تغییر کرد');
            if (result.user) renderAdminUserProfile(result.user);
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    async function sendAdminUserMessage(userId) {
        const input = document.getElementById('admin-user-message');
        const text = (input?.value || '').trim();
        if (!text) {
            UI.toast('متن پیام خالی است', 'error');
            return;
        }
        try {
            const result = await API.sendAdminUserMessage(userId, text);
            UI.toast(result.message || 'پیام ارسال شد');
            if (input) input.value = '';
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    async function createReadyConfigPlan() {
        const payload = {
            name: document.getElementById('ready-plan-name')?.value.trim(),
            duration_days: Number(document.getElementById('ready-plan-days')?.value || 0),
            volume_gb: Number(document.getElementById('ready-plan-volume')?.value || 0),
            price: document.getElementById('ready-plan-price')?.value || '0',
        };
        try {
            const result = await API.createReadyPlan(payload);
            UI.toast(result.message || 'پلن آماده ساخته شد');
            await openAdminModule('ready_configs');
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    async function addReadyConfigItems() {
        const poolId = document.getElementById('ready-pool-select')?.value;
        const content = document.getElementById('ready-config-content')?.value || '';
        if (!poolId || !content.trim()) {
            UI.toast('پلن و متن کانفیگ را وارد کنید', 'error');
            return;
        }
        try {
            const result = await API.addReadyConfigs(poolId, content);
            UI.toast(result.message || 'کانفیگ‌ها اضافه شدند');
            await openAdminModule('ready_configs');
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    function openBotAdmin() {
        const botUsername = getBotUsername();
        if (botUsername) {
            window.Telegram?.WebApp?.openTelegramLink(`https://t.me/${botUsername}?start=admin`);
        } else {
            UI.toast('یوزرنیم ربات پیدا نشد', 'error');
        }
    }

    function renderReferral(data) {
        const container = document.getElementById('referral-container');
        const botUsername = getBotUsername() || 'bot';

        if (!data.enabled) {
            container.innerHTML = `<div class="empty-state"><div class="empty-icon">${UI.icon('lock')}</div><p>سیستم دعوت فعلاً غیرفعال است</p></div>`;
            return;
        }

        const refLink = data.ref_code ? `https://t.me/${botUsername}?start=ref_${data.ref_code}` : '';

        container.innerHTML = `
            <div class="referral-card">
                <div style="text-align:center;margin-bottom:8px">
                    <span class="empty-icon">${UI.icon('users')}</span>
                </div>
                <p style="text-align:center;font-size:14px;color:var(--text-secondary);margin-bottom:12px">
                    لینک دعوت خود را با دوستانتان به اشتراک بگذارید
                </p>
                ${data.ref_code ? `
                    <div class="ref-code-box" onclick="UI.copyToClipboard('${refLink}')">
                        ${data.ref_code}
                        <div style="font-size:10px;color:var(--text-muted);margin-top:4px">برای کپی لمس کنید</div>
                    </div>
                    <button class="btn btn-primary btn-block" onclick="shareRefLink('${refLink}')">${UI.icon('share')} اشتراک‌گذاری</button>
                ` : '<p style="text-align:center;color:var(--text-muted)">کد رفرال شما هنوز ایجاد نشده</p>'}

                <div class="ref-stats">
                    <div class="stat-card">
                        <div class="stat-value">${data.referral_count}</div>
                        <div class="stat-label">دعوت شده</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value">$${UI.formatMoney(data.total_earned)}</div>
                        <div class="stat-label">درآمد کل</div>
                    </div>
                </div>
            </div>
        `;
    }

    // ─── Expose ─────────────────────────────────────────────────────────
    return {
        load_dashboard, load_store, load_configs,
        load_wallet, load_support, load_referral, load_admin,
        showConfigDetail, showRenewal, setRenewalType, submitRenewal,
        buyPlan, submitPurchase, openInvoice, topupWallet, submitTopup, refreshPayment,
        showTicketHistory, closeTicket, openAdminModule, openAdminTicket, submitAdminTicketReply, runAdminAction,
        searchAdminUsers, openAdminUser, runAdminUserAction, adjustAdminUserBalance, sendAdminUserMessage,
        createReadyConfigPlan, addReadyConfigItems, openBotAdmin,
    };
})();

// Global share helper
function shareRefLink(link) {
    try {
        window.Telegram?.WebApp?.openTelegramLink(
            `https://t.me/share/url?url=${encodeURIComponent(link)}&text=${encodeURIComponent('با لینک من عضو شو و تخفیف بگیر!')}`
        );
    } catch {
        UI.copyToClipboard(link);
    }
}
