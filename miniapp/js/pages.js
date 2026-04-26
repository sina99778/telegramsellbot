/**
 * Page renderers — each page has a load_ function that fetches data and renders.
 */
const Pages = (() => {
    let dashboardData = null;
    let plansCache = [];

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
            UI.toast('❌ خطا در بارگذاری: ' + e.message, 'error');
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
                <div class="stat-icon">📶</div>
                <div class="stat-value">${data.active_config_count}</div>
                <div class="stat-label">سرویس فعال</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">💰</div>
                <div class="stat-value">$${UI.formatMoney(data.wallet.balance)}</div>
                <div class="stat-label">موجودی کیف پول</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">📈</div>
                <div class="stat-value">${usagePct}%</div>
                <div class="stat-label">مصرف کل</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">💾</div>
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
                    <div class="empty-icon">📦</div>
                    <p>هنوز سرویسی ندارید</p>
                    <button class="btn btn-primary" style="margin-top:12px" onclick="UI.navigate('store')">🛒 خرید سرویس</button>
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
            <div class="modal-title">📦 ${name}</div>
            <div style="text-align:center;margin-bottom:16px">
                <div style="font-size:36px;font-weight:800;color:var(--accent-primary)">${pct}%</div>
                <p style="font-size:12px;color:var(--text-muted)">مصرف شده</p>
            </div>
            <div class="progress-bar-container" style="height:8px;margin-bottom:16px">
                <div class="progress-bar-fill ${UI.getProgressClass(pct)}" style="width:${pct}%"></div>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:13px">
                <div>
                    <span style="color:var(--text-muted)">📊 وضعیت</span>
                    <div style="font-weight:600;margin-top:2px">${statusText}</div>
                </div>
                <div>
                    <span style="color:var(--text-muted)">📅 باقیمانده</span>
                    <div style="font-weight:600;margin-top:2px">${UI.daysLeft(sub.ends_at)}</div>
                </div>
                <div>
                    <span style="color:var(--text-muted)">💾 مصرف</span>
                    <div style="font-weight:600;margin-top:2px">${UI.formatBytes(sub.used_bytes)}</div>
                </div>
                <div>
                    <span style="color:var(--text-muted)">📦 کل حجم</span>
                    <div style="font-weight:600;margin-top:2px">${UI.formatBytes(sub.volume_bytes)}</div>
                </div>
                <div>
                    <span style="color:var(--text-muted)">📛 پلن</span>
                    <div style="font-weight:600;margin-top:2px">${sub.plan_name || '-'}</div>
                </div>
                <div>
                    <span style="color:var(--text-muted)">📅 شروع</span>
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
            UI.toast('❌ ' + e.message, 'error');
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
            UI.toast('❌ خطا: ' + e.message, 'error');
        }
    }

    function renderPlans(plans) {
        const container = document.getElementById('plans-list');
        if (!plans.length) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">🛒</div><p>هیچ پلنی موجود نیست</p></div>';
            return;
        }

        container.innerHTML = plans.map((plan, i) => `
            <div class="plan-card ${i === 1 ? 'popular' : ''}">
                <div class="plan-name">${plan.name}</div>
                <div class="plan-specs">
                    <span class="plan-spec">💾 ${plan.volume_gb} GB</span>
                    <span class="plan-spec">📅 ${plan.duration_days} روز</span>
                    <span class="plan-spec">🔐 ${plan.protocol}</span>
                </div>
                <div class="plan-price">$${UI.formatMoney(plan.price)} <small>/ ${plan.currency}</small></div>
                <button class="btn btn-primary btn-block plan-buy-btn" onclick="Pages.buyPlan('${plan.id}')">
                    🛒 خرید
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
            UI.toast('❌ ' + e.message, 'error');
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
            UI.toast('❌ خطا: ' + e.message, 'error');
        }
    }

    function renderAllConfigs(subs) {
        const container = document.getElementById('all-configs-list');
        if (!subs.length) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">📦</div><p>هنوز سرویسی ندارید</p></div>';
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
            const txData = await API.getTransactions(1);
            renderTransactions(txData.transactions);
        } catch (e) {
            UI.toast('❌ خطا: ' + e.message, 'error');
        }
    }

    function renderWalletCard(wallet) {
        document.getElementById('wallet-card').innerHTML = `
            <div class="wallet-label">💰 موجودی کیف پول</div>
            <div class="wallet-balance-display">$${UI.formatMoney(wallet.balance)}</div>
            <div class="wallet-actions">
                <button class="btn" onclick="Pages.topupWallet()">➕ شارژ</button>
            </div>
        `;
    }

    function renderTransactions(txs) {
        const container = document.getElementById('transactions-list');
        if (!txs.length) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">📜</div><p>تراکنشی ثبت نشده</p></div>';
            return;
        }

        const typeMap = {
            'topup': '💳 شارژ',
            'purchase': '🛒 خرید',
            'refund': '🔄 بازگشت',
            'referral_bonus': '🎁 پاداش',
            'admin_adjust': '⚙️ تنظیم',
            'renewal': '🔄 تمدید',
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
        const botUsername = getBotUsername();
        if (botUsername) {
            window.Telegram.WebApp.openTelegramLink(`https://t.me/${botUsername}?start=topup`);
        } else {
            UI.toast('لطفاً از داخل ربات شارژ کنید', 'error');
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // SUPPORT
    // ═══════════════════════════════════════════════════════════════════════
    async function load_support() {
        try {
            const data = await API.getTickets();
            renderTickets(data.tickets);
        } catch (e) {
            UI.toast('❌ خطا: ' + e.message, 'error');
        }
    }

    function renderTickets(tickets) {
        const container = document.getElementById('tickets-container');
        if (!tickets.length || !tickets[0].messages.length) {
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">💬</div><p>پیامی وجود ندارد<br>اولین پیام خود را ارسال کنید</p></div>';
            return;
        }

        // Show messages from the most recent ticket
        const ticket = tickets[0];
        container.innerHTML = ticket.messages.map(m => `
            <div class="chat-bubble ${m.sender_type}">
                ${m.text || '📷 تصویر'}
                <span class="bubble-time">${UI.formatDate(m.created_at)}</span>
            </div>
        `).join('');

        // Scroll to bottom
        setTimeout(() => container.scrollTop = container.scrollHeight, 100);
    }

    // ═══════════════════════════════════════════════════════════════════════
    // REFERRAL
    // ═══════════════════════════════════════════════════════════════════════
    async function load_referral() {
        try {
            const data = await API.getReferral();
            renderReferral(data);
        } catch (e) {
            UI.toast('❌ خطا: ' + e.message, 'error');
        }
    }

    async function load_admin() {
        try {
            const data = await API.getAdminOverview();
            renderAdminPanel(data);
        } catch (e) {
            UI.toast('❌ ' + e.message, 'error');
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
            UI.toast('❌ ' + e.message, 'error');
        }
    }

    function renderAdminSection(section, data) {
        const modules = document.getElementById('admin-modules');
        if (!modules) return;
        const items = data.items || [];
        modules.innerHTML = `
            <button class="btn btn-secondary btn-block" onclick="Pages.load_admin()">بازگشت به مدیریت</button>
            <h3 class="section-title" style="margin-top:16px">${escapeHtml(data.title || 'مدیریت')}</h3>
            <div class="admin-list">
                ${items.length ? items.map(item => renderAdminItem(section, item)).join('') : '<div class="empty-state"><p>موردی برای نمایش نیست</p></div>'}
            </div>
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
                        <button class="btn btn-secondary btn-sm" onclick="Pages.runAdminAction('${section}', '${escapeHtml(action.action)}', '${escapeHtml(item.id)}')">${escapeHtml(action.label)}</button>
                    `).join('')}
                </div>` : ''}
            </div>
        `;
    }

    async function runAdminAction(section, action, id) {
        try {
            const result = await API.runAdminAction({ action, id });
            UI.toast(result.message || 'انجام شد');
            await openAdminModule(section);
        } catch (e) {
            UI.toast('❌ ' + e.message, 'error');
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
            container.innerHTML = '<div class="empty-state"><div class="empty-icon">🔒</div><p>سیستم دعوت فعلاً غیرفعال است</p></div>';
            return;
        }

        const refLink = data.ref_code ? `https://t.me/${botUsername}?start=ref_${data.ref_code}` : '';

        container.innerHTML = `
            <div class="referral-card">
                <div style="text-align:center;margin-bottom:8px">
                    <span style="font-size:40px">🎁</span>
                </div>
                <p style="text-align:center;font-size:14px;color:var(--text-secondary);margin-bottom:12px">
                    لینک دعوت خود را با دوستانتان به اشتراک بگذارید
                </p>
                ${data.ref_code ? `
                    <div class="ref-code-box" onclick="UI.copyToClipboard('${refLink}')">
                        ${data.ref_code}
                        <div style="font-size:10px;color:var(--text-muted);margin-top:4px">برای کپی لمس کنید</div>
                    </div>
                    <button class="btn btn-primary btn-block" onclick="shareRefLink('${refLink}')">📤 اشتراک‌گذاری</button>
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
        buyPlan, submitPurchase, openInvoice, topupWallet, openAdminModule, runAdminAction, openBotAdmin,
    };
})();

// Global share helper
function shareRefLink(link) {
    try {
        window.Telegram?.WebApp?.openTelegramLink(
            `https://t.me/share/url?url=${encodeURIComponent(link)}&text=${encodeURIComponent('🎁 با لینک من عضو شو و تخفیف بگیر!')}`
        );
    } catch {
        UI.copyToClipboard(link);
    }
}
