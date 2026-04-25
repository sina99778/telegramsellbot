/**
 * Page renderers — each page has a load_ function that fetches data and renders.
 */
const Pages = (() => {
    let dashboardData = null;

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
        // Update header
        document.getElementById('user-name').textContent = data.first_name || 'کاربر';
        document.getElementById('user-balance').textContent = `$${UI.formatMoney(data.wallet.balance)}`;
        document.getElementById('user-avatar').textContent = (data.first_name || 'U')[0].toUpperCase();

        // Stats
        const usagePct = UI.getUsagePercent(data.total_volume_used, data.total_volume);
        document.getElementById('stats-grid').innerHTML = `
            <div class="stat-card">
                <div class="stat-icon">📱</div>
                <div class="stat-value">${data.active_config_count}</div>
                <div class="stat-label">سرویس فعال</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">💰</div>
                <div class="stat-value">$${UI.formatMoney(data.wallet.balance)}</div>
                <div class="stat-label">موجودی کیف پول</div>
            </div>
            <div class="stat-card">
                <div class="stat-icon">📊</div>
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

        let actionsHtml = '';
        if (showActions && sub.sub_link) {
            actionsHtml = `
                <div class="config-actions">
                    <button class="btn btn-secondary btn-sm" onclick="UI.copyToClipboard('${sub.sub_link}')">📋 کپی لینک</button>
                    <button class="btn btn-secondary btn-sm" onclick="Pages.showConfigDetail('${sub.id}')">🔍 جزئیات</button>
                </div>
            `;
        }

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
            linkSection = `
                <div style="margin-top:16px">
                    <p style="font-size:12px;color:var(--text-muted);margin-bottom:6px">🔗 لینک اشتراک:</p>
                    <div class="copy-box" onclick="UI.copyToClipboard('${sub.sub_link}')">${sub.sub_link}</div>
                </div>
                <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
                    <button class="btn btn-secondary btn-sm" onclick="UI.copyToClipboard('${sub.sub_link}')">📋 کپی</button>
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
            <button class="btn btn-secondary btn-block" style="margin-top:16px" onclick="UI.closeModal()">بستن</button>
        `);
    }

    // ═══════════════════════════════════════════════════════════════════════
    // STORE
    // ═══════════════════════════════════════════════════════════════════════
    async function load_store() {
        try {
            const data = await API.getPlans();
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
                <button class="btn btn-primary btn-block plan-buy-btn" onclick="Pages.buyPlan('${plan.id}', '${plan.name}')">
                    🛒 خرید
                </button>
            </div>
        `).join('');
    }

    function buyPlan(planId, planName) {
        // Redirect to bot with deep link
        const botUsername = window.Telegram?.WebApp?.initDataUnsafe?.bot?.username;
        if (botUsername) {
            window.Telegram.WebApp.openTelegramLink(`https://t.me/${botUsername}?start=buy_${planId}`);
        } else {
            UI.toast('لطفاً از داخل ربات خرید کنید', 'error');
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
        const botUsername = window.Telegram?.WebApp?.initDataUnsafe?.bot?.username;
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

    function renderReferral(data) {
        const container = document.getElementById('referral-container');
        const botUsername = window.Telegram?.WebApp?.initDataUnsafe?.bot?.username || 'bot';

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
        load_wallet, load_support, load_referral,
        showConfigDetail, buyPlan, topupWallet,
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
