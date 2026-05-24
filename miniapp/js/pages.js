/**
 * Page renderers — each page has a load_ function that fetches data and renders.
 */
const Pages = (() => {
    let dashboardData = null;
    let configsCache = [];
    let plansCache = [];
    let supportTicketsCache = [];
    let adminUserSearchState = { q: '', page: 1 };
    let configsState = { page: 1, total: 0, pageSize: 20 };
    let salesEnabled = true;
    let renewalsEnabled = true;
    let isAdmin = false;
    let gateways = { tetrapay: true, tronado: false, nowpayments: true, manual_crypto: false, card_to_card: false };

    /**
     * Generate gateway payment buttons based on active gateways.
     * @param {string} callbackPrefix - JS function call prefix, e.g. "Pages.submitPurchase('plan-id',"
     * @param {boolean} includeWallet - whether to include wallet button
     */
    function gatewayButtons(callbackPrefix, includeWallet = true) {
        const buttons = [];
        if (includeWallet) {
            buttons.push(`<button class="btn btn-primary btn-block" onclick="${callbackPrefix} 'wallet')">💳 پرداخت با کیف پول</button>`);
        }
        if (gateways.tetrapay) {
            buttons.push(`<button class="btn btn-secondary btn-block" onclick="${callbackPrefix} 'tetrapay')">🏦 درگاه ریالی تتراپی</button>`);
        }
        if (gateways.tronado) {
            buttons.push(`<button class="btn btn-secondary btn-block" onclick="${callbackPrefix} 'tronado')">💰 درگاه ترونادو</button>`);
        }
        if (gateways.nowpayments) {
            buttons.push(`<button class="btn btn-secondary btn-block" onclick="${callbackPrefix} 'nowpayments')">💎 درگاه ارزی NOWPayments</button>`);
        }
        if (gateways.manual_crypto || gateways.card_to_card) {
            buttons.push(`<button class="btn btn-secondary btn-block" onclick="Pages.openBotChat()">🤖 پرداخت دستی (از طریق ربات)</button>`);
        }
        if (!buttons.length) {
            buttons.push(`
                <div class="empty-state compact">
                    <p>هیچ درگاه پرداختی فعال نیست</p>
                    <p class="empty-hint">ادمین باید حداقل یکی از درگاه‌ها را فعال کند.</p>
                    <button class="btn btn-secondary btn-sm" onclick="Pages.openBotChat()">${UI.icon('share')} رفتن به ربات</button>
                </div>
            `);
        }
        return buttons.join('\n');
    }

    /** Topup-specific: no wallet option */
    function topupGatewayButtons(callbackPrefix) {
        const buttons = [];
        if (gateways.tetrapay) {
            buttons.push(`<button class="btn btn-primary btn-block" onclick="${callbackPrefix} 'tetrapay')">💳 درگاه ریالی تتراپی</button>`);
        }
        if (gateways.tronado) {
            buttons.push(`<button class="btn btn-secondary btn-block" onclick="${callbackPrefix} 'tronado')">💰 درگاه ترونادو</button>`);
        }
        if (gateways.nowpayments) {
            buttons.push(`<button class="btn btn-secondary btn-block" onclick="${callbackPrefix} 'nowpayments')">💎 درگاه ارزی NOWPayments</button>`);
        }
        if (gateways.manual_crypto || gateways.card_to_card) {
            // `<a target="_blank">` is silently dropped inside Telegram's
            // WebApp iframe. We need openTelegramLink() to actually leave
            // the miniapp. Wrap it in a button that calls openBotChat().
            buttons.push(`<button class="btn btn-secondary btn-block" onclick="Pages.openBotChat()">🤖 پرداخت دستی (از طریق ربات)</button>`);
        }
        if (!buttons.length) {
            buttons.push(`
                <div class="empty-state compact">
                    <p>هیچ درگاه پرداختی فعال نیست</p>
                    <p class="empty-hint">ادمین باید حداقل یکی از درگاه‌ها (تتراپی / NOWPayments / دستی) را فعال کند.</p>
                    <button class="btn btn-secondary btn-sm" onclick="Pages.openBotChat()">${UI.icon('share')} رفتن به ربات</button>
                </div>
            `);
        }
        return buttons.join('\n');
    }

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
        // Show skeleton while loading
        document.getElementById('stats-grid').innerHTML = `
            <div class="stat-card skeleton" style="height:104px"></div>
            <div class="stat-card skeleton" style="height:104px"></div>
            <div class="stat-card skeleton" style="height:104px"></div>
            <div class="stat-card skeleton" style="height:104px"></div>
        `;
        try {
            dashboardData = await API.getDashboard();
            renderDashboard(dashboardData);
        } catch (e) {
            UI.toast('خطا در بارگذاری: ' + e.message, 'error');
        }
    }

    function renderDashboard(data) {
        window.AppState = data;
        salesEnabled = data.sales_enabled !== false;
        renewalsEnabled = data.renewals_enabled !== false;
        isAdmin = !!data.is_admin;
        if (data.gateways) {
            gateways = {
                tetrapay: !!data.gateways.tetrapay,
                tronado: !!data.gateways.tronado,
                nowpayments: !!data.gateways.nowpayments,
                manual_crypto: !!data.gateways.manual_crypto,
                card_to_card: !!data.gateways.card_to_card,
            };
        }
        const adminNav = document.getElementById('admin-nav-btn');
        if (adminNav) adminNav.classList.toggle('hidden', !data.is_admin);

        // ── Header ──
        document.getElementById('user-name').textContent = data.first_name || 'کاربر';
        document.getElementById('user-balance').textContent = `$${UI.formatMoney(data.wallet.balance)}`;
        document.getElementById('user-avatar').textContent = (data.first_name || 'U')[0].toUpperCase();

        // ── Quick actions (3 columns, icon + label) ──
        const quickActionsEl = document.getElementById('quick-actions');
        if (quickActionsEl) {
            const buyOrManage = (salesEnabled || isAdmin)
                ? `<button class="quick-action" onclick="UI.navigate('store')"><span>🛒</span><strong>خرید سرویس</strong></button>`
                : `<button class="quick-action" onclick="UI.navigate('configs')"><span>📋</span><strong>سرویس‌های من</strong></button>`;
            quickActionsEl.innerHTML = `
                ${buyOrManage}
                <button class="quick-action" onclick="UI.navigate('wallet')"><span>💳</span><strong>شارژ حساب</strong></button>
                <button class="quick-action" onclick="UI.navigate('support')"><span>💬</span><strong>پشتیبانی</strong></button>
            `;
        }

        // ── Stats grid ──
        // Layout: row 1 → [active count] [balance]
        //         row 2 → [usage card spanning both columns]
        const usagePct = UI.getUsagePercent(data.total_volume_used, data.total_volume);
        const usageClass = usagePct >= 90 ? 'danger' : usagePct >= 75 ? 'warn' : '';
        // Mirror the progress-bar tone on the big headline percent.
        const usageHeadlineClass = usagePct >= 90 ? 'is-danger' : usagePct >= 75 ? 'is-warn' : '';
        const banners = [
            (!salesEnabled && !isAdmin)
                ? '<div class="config-warning danger wide">⛔ فروش سرویس موقتاً غیرفعال است.</div>'
                : '',
            (!renewalsEnabled && !isAdmin)
                ? '<div class="config-warning wide">⏸ تمدید سرویس موقتاً غیرفعال است.</div>'
                : '',
        ].filter(Boolean).join('');

        document.getElementById('stats-grid').innerHTML = `
            ${banners}
            <div class="stat-card">
                <div class="stat-label">سرویس فعال</div>
                <div class="stat-value">${(data.active_config_count || 0)}</div>
                <div class="stat-hint">از مجموع سرویس‌های شما</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">موجودی کیف پول</div>
                <div class="stat-value">$${UI.formatMoney(data.wallet.balance)}</div>
                <div class="stat-hint">قابل خرج برای خرید/تمدید</div>
            </div>
            <div class="stat-card wide">
                <div class="row-between">
                    <div>
                        <div class="stat-label">مصرف مجموع ${(data.active_config_count || 0)} سرویس</div>
                        <div class="stat-value stat-value--compact">${UI.formatBytes(data.total_volume_used)}</div>
                        <div class="stat-hint stat-hint--ltr">از ${UI.formatBytes(data.total_volume)} مجموع</div>
                    </div>
                    <div class="usage-headline ${usageHeadlineClass}">${usagePct}<span class="pct-unit">%</span></div>
                </div>
                <div class="progress-bar-container stat-progress">
                    <div class="progress-bar ${usageClass}" style="width:${usagePct}%"></div>
                </div>
            </div>
        `;

        // ── Active configs (max 5 on dashboard; full list lives on /configs) ──
        const activeSubs = (data.subscriptions || [])
            .filter(s => s.status === 'active' || s.status === 'pending_activation')
            .slice(0, 5);
        const container = document.getElementById('configs-list');

        if (activeSubs.length === 0) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">${UI.icon('package')}</div>
                    <p>هنوز سرویسی ندارید</p>
                    <p class="empty-hint">برای شروع، اولین کانفیگ خود را خریداری کنید.</p>
                    <button class="btn btn-primary" onclick="UI.navigate('store')">${UI.icon('plus')} خرید اولین سرویس</button>
                </div>`;
            return;
        }

        container.innerHTML = activeSubs.map(sub => renderConfigCard(sub)).join('');
    }

    function getConfigHealth(sub) {
        const pct = UI.getUsagePercent(sub.used_bytes, sub.volume_bytes);
        const remainingBytes = Math.max((sub.volume_bytes || 0) - (sub.used_bytes || 0), 0);
        const daysLabel = UI.daysLeft(sub.ends_at);
        const isExpired = sub.status === 'expired' || sub.status === 'disabled' || daysLabel === 'منقضی' || pct >= 100;
        let warning = '';
        if (isExpired) {
            warning = 'این سرویس به پایان رسیده و دسترسی آن باید قطع باشد.';
        } else if (pct >= 90) {
            warning = 'حجم سرویس رو به پایان است.';
        } else if (daysLabel === '1 روز') {
            warning = 'کمتر از یک روز تا پایان سرویس باقی مانده است.';
        }
        return {
            pct,
            pctClass: UI.getProgressClass(pct),
            statusText: UI.getStatusText(isExpired && sub.status === 'active' ? 'expired' : sub.status),
            statusClass: UI.getStatusClass(isExpired ? 'expired' : sub.status),
            remainingBytes,
            daysLabel,
            isExpired,
            isNearLimit: pct >= 90,
            warning,
        };
    }

    function renderConfigCard(sub, showActions = false) {
        const health = getConfigHealth(sub);
        const name = sub.config_name || sub.plan_name || 'سرویس';

        const cardClass = [
            'config-card',
            health.isExpired ? 'expired-card' : '',
            health.isNearLimit ? 'near-limit' : '',
        ].filter(Boolean).join(' ');
        const safeSubLink = sub.sub_link ? encodeURIComponent(sub.sub_link) : '';

        return `
            <div class="${cardClass}" onclick="Pages.showConfigDetail('${sub.id}')">
                <div class="config-header">
                    <span class="config-name">${escapeHtml(name)}</span>
                    <span class="config-status ${health.statusClass}">${health.statusText}</span>
                </div>

                <div class="progress-bar-container">
                    <div class="progress-bar ${health.pctClass}" style="width:${health.pct}%"></div>
                </div>
                <div class="config-stats">
                    <span>${UI.formatBytes(sub.used_bytes)} / ${UI.formatBytes(sub.volume_bytes)}</span>
                    <span>${(health.pct)}%</span>
                </div>

                <div class="config-meta-grid">
                    <div class="config-metric">
                        <span>باقی‌مانده حجم</span>
                        <strong>${UI.formatBytes(health.remainingBytes)}</strong>
                    </div>
                    <div class="config-metric">
                        <span>زمان</span>
                        <strong>${escapeHtml(health.daysLabel)}</strong>
                    </div>
                </div>

                ${health.warning ? `<div class="config-warning ${health.isExpired ? 'danger' : ''}">${health.warning}</div>` : ''}

                <div class="config-actions" onclick="event.stopPropagation()">
                    ${(renewalsEnabled && !health.isExpired) ? `<button class="btn btn-primary btn-sm" onclick="Pages.showRenewal('${sub.id}')">${UI.icon('refresh')} تمدید</button>` : ''}
                    ${sub.sub_link ? `<button class="btn btn-secondary btn-sm" onclick="UI.copyToClipboard(decodeURIComponent('${safeSubLink}'))">${UI.icon('copy')} کپی لینک</button>` : ''}
                    <button class="btn btn-ghost btn-sm" onclick="Pages.showConfigDetail('${sub.id}')">${UI.icon('info')} جزئیات</button>
                </div>
            </div>
        `;
    }

    function showConfigDetail(subId) {
        const sub = [
            ...(dashboardData?.subscriptions || []),
            ...configsCache,
        ].find(s => s.id === subId);
        if (!sub) return;

        const health = getConfigHealth(sub);
        const pct = health.pct;
        const statusText = health.statusText;
        const name = sub.config_name || sub.plan_name || 'سرویس';

        let linkSection = '';
        if (sub.sub_link || sub.vless_uri) {
            const rawSub = sub.sub_link || '';
            const safeSubLink = encodeURIComponent(rawSub);
            const rawVless = sub.vless_uri || '';
            const safeVless = encodeURIComponent(rawVless);
            // QR points at the most useful payload: prefer the vless URI
            // (works directly in apps) and fall back to sub_link.
            const qrPayload = rawVless || rawSub;
            const safeQr = encodeURIComponent(qrPayload);

            linkSection = `
                <div class="config-detail-links">
                    <img src="https://api.qrserver.com/v1/create-qr-code/?size=320x320&data=${safeQr}"
                         class="config-detail-qr" alt="QR Code" />

                    ${rawSub ? `
                    <div class="config-detail-link-block">
                        <p class="form-label">🔗 لینک اشتراک (Sub-Link)</p>
                        <div class="copy-box" onclick="UI.copyToClipboard('${rawSub.replace(/'/g, "\\'")}')">${escapeHtml(rawSub)}</div>
                    </div>` : ''}

                    ${rawVless ? `
                    <div class="config-detail-link-block">
                        <p class="form-label">📋 کانفیگ مستقیم (VLESS)</p>
                        <div class="copy-box" onclick="UI.copyToClipboard('${rawVless.replace(/'/g, "\\'")}')">${escapeHtml(rawVless)}</div>
                    </div>` : ''}

                    <div class="config-detail-actions">
                        ${rawSub ? `<button class="btn btn-secondary btn-sm" onclick="UI.copyToClipboard('${rawSub.replace(/'/g, "\\'")}')">${UI.icon('copy')} کپی ساب</button>` : ''}
                        ${rawVless ? `<button class="btn btn-secondary btn-sm" onclick="UI.copyToClipboard('${rawVless.replace(/'/g, "\\'")}')">${UI.icon('copy')} کپی VLESS</button>` : ''}
                        ${rawSub ? `<a href="v2rayng://install-sub/?url=${safeSubLink}" class="btn btn-primary btn-sm" style="text-decoration:none">V2rayNG</a>` : ''}
                        ${rawSub ? `<a href="v2box://install-sub/?url=${safeSubLink}" class="btn btn-primary btn-sm" style="text-decoration:none">V2Box</a>` : ''}
                        ${rawSub ? `<a href="streisand://import/${safeSubLink}" class="btn btn-primary btn-sm" style="text-decoration:none">Streisand</a>` : ''}
                    </div>
                </div>
            `;
        }

        UI.showModal(`
            <div class="modal-title">${UI.icon('configs')} ${escapeHtml(name)}</div>
            <div class="modal-pct">
                <div class="modal-pct__value">${pct}%</div>
                <div class="modal-pct__label">مصرف شده</div>
            </div>
            <div class="progress-bar-container" style="margin-bottom:16px">
                <div class="progress-bar-fill ${UI.getProgressClass(pct)}" style="width:${pct}%"></div>
            </div>
            ${health.warning ? `<div class="config-warning ${health.isExpired ? 'danger' : ''}" style="margin-bottom:12px">${health.warning}</div>` : ''}
            <div class="modal-stats-grid">
                <div>
                    <span>وضعیت</span>
                    <strong>${statusText}</strong>
                </div>
                <div><span>باقیمانده</span><strong>${health.daysLabel}</strong></div>
                <div><span>مصرف</span><strong>${UI.formatBytes(sub.used_bytes)}</strong></div>
                <div><span>کل حجم</span><strong>${UI.formatBytes(sub.volume_bytes)}</strong></div>
                <div><span>باقی‌مانده حجم</span><strong>${UI.formatBytes(health.remainingBytes)}</strong></div>
                <div><span>پلن</span><strong>${sub.plan_name || '-'}</strong></div>
                <div><span>شروع</span><strong>${UI.formatDate(sub.starts_at)}</strong></div>
            </div>
            ${linkSection}
            ${(renewalsEnabled || isAdmin) ? `<button class="btn btn-primary btn-block" style="margin-top:16px" onclick="Pages.showRenewal('${sub.id}')">تمدید سرویس</button>` : '<div class="config-warning" style="margin-top:16px;text-align:center">⏸ تمدید موقتاً غیرفعال است</div>'}
            <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">بستن</button>
        `);
    }

    function showRenewal(subId) {
        const sub = [
            ...(dashboardData?.subscriptions || []),
            ...configsCache,
        ].find(s => s.id === subId);
        if (!sub) return;
        const name = sub.config_name || sub.plan_name || 'سرویس';
        UI.showModal(`
            <div class="modal-title">تمدید ${escapeHtml(name)}</div>
            <div class="renewal-tabs">
                <button class="renewal-tab active" data-renew-type="time" onclick="Pages.setRenewalType('time')">زمان</button>
                <button class="renewal-tab" data-renew-type="volume" onclick="Pages.setRenewalType('volume')">حجم</button>
            </div>
            <div id="renewal-presets" style="display:grid;grid-template-columns:repeat(3, 1fr);gap:8px;margin:12px 0;">
                <button class="btn btn-secondary btn-sm preset-btn" onclick="document.getElementById('renewal-amount').value='30'; document.getElementById('renewal-amount').dispatchEvent(new Event('input'))">+30 روز</button>
                <button class="btn btn-secondary btn-sm preset-btn" onclick="document.getElementById('renewal-amount').value='60'; document.getElementById('renewal-amount').dispatchEvent(new Event('input'))">+60 روز</button>
                <button class="btn btn-secondary btn-sm preset-btn" onclick="document.getElementById('renewal-amount').value='90'; document.getElementById('renewal-amount').dispatchEvent(new Event('input'))">+90 روز</button>
            </div>
            <label class="form-label" id="renewal-amount-label" for="renewal-amount">تعداد روز</label>
            <input id="renewal-amount" class="form-input" inputmode="decimal" dir="ltr" placeholder="30" value="30">
            <p class="form-hint" id="renewal-hint">مدت موردنظر را به روز وارد کنید.</p>
            <div class="renewal-price-box" id="renewal-price-box">برای محاسبه قیمت، مقدار را وارد کنید.</div>
            <div class="checkout-methods">
                ${gatewayButtons(`Pages.submitRenewal('${sub.id}',`)}
            </div>
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
        const presets = document.querySelectorAll('.preset-btn');
        if (type === 'volume') {
            if (label) label.textContent = 'حجم اضافه';
            if (hint) hint.textContent = 'حجم موردنظر را به گیگابایت وارد کنید.';
            if (input) input.value = input.value || '10';
            if (presets[0]) presets[0].textContent = '+10 گیگ';
            if (presets[0]) presets[0].setAttribute('onclick', "document.getElementById('renewal-amount').value='10'; document.getElementById('renewal-amount').dispatchEvent(new Event('input'))");
            if (presets[1]) presets[1].textContent = '+20 گیگ';
            if (presets[1]) presets[1].setAttribute('onclick', "document.getElementById('renewal-amount').value='20'; document.getElementById('renewal-amount').dispatchEvent(new Event('input'))");
            if (presets[2]) presets[2].textContent = '+50 گیگ';
            if (presets[2]) presets[2].setAttribute('onclick', "document.getElementById('renewal-amount').value='50'; document.getElementById('renewal-amount').dispatchEvent(new Event('input'))");
        } else {
            if (label) label.textContent = 'تعداد روز';
            if (hint) hint.textContent = 'مدت موردنظر را به روز وارد کنید.';
            if (input) input.value = input.value || '30';
            if (presets[0]) presets[0].textContent = '+30 روز';
            if (presets[0]) presets[0].setAttribute('onclick', "document.getElementById('renewal-amount').value='30'; document.getElementById('renewal-amount').dispatchEvent(new Event('input'))");
            if (presets[1]) presets[1].textContent = '+60 روز';
            if (presets[1]) presets[1].setAttribute('onclick', "document.getElementById('renewal-amount').value='60'; document.getElementById('renewal-amount').dispatchEvent(new Event('input'))");
            if (presets[2]) presets[2].textContent = '+90 روز';
            if (presets[2]) presets[2].setAttribute('onclick', "document.getElementById('renewal-amount').value='90'; document.getElementById('renewal-amount').dispatchEvent(new Event('input'))");
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

    let _renewalSubmitting = false;
    async function submitRenewal(subId, paymentMethod) {
        if (_renewalSubmitting) return;
        const amount = parseFloat(document.getElementById('renewal-amount')?.value || '0');
        const renewType = getSelectedRenewalType();
        if (!amount || amount <= 0) {
            UI.toast('مقدار تمدید معتبر نیست', 'error');
            return;
        }
        _renewalSubmitting = true;
        try {
            UI.toast('در حال تمدید...');
            const result = await API.renewConfig({
                subscription_id: subId,
                renew_type: renewType,
                amount,
                payment_method: paymentMethod || 'wallet',
            });

            if (result.invoice_url) {
                UI.showModal(`
                    <div class="modal-title">فاکتور تمدید آماده است</div>
                    <p class="form-hint" style="text-align:center;margin-bottom:14px">${escapeHtml(result.message)}</p>
                    <button class="btn btn-primary btn-block" onclick="Pages.openInvoice(decodeURIComponent('${encodeURIComponent(result.invoice_url)}'))">پرداخت فاکتور</button>
                    <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">بستن</button>
                `);
            } else {
                UI.toast(result.message);
                UI.closeModal();
                dashboardData = await API.getDashboard();
                renderDashboard(dashboardData);
            }
        } catch (e) {
            UI.toast(e.message, 'error');
        } finally {
            _renewalSubmitting = false;
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // STORE
    // ═══════════════════════════════════════════════════════════════════════
    async function load_store() {
        document.getElementById('plans-list').innerHTML = `
            <div class="plan-card skeleton" style="height:180px"></div>
            <div class="plan-card skeleton" style="height:180px"></div>
        `;
        try {
            const data = await API.getPlans();
            plansCache = data.plans || [];
            salesEnabled = data.sales_enabled !== false;
            renderPlans(data.plans || [], data.custom_purchase || null);
        } catch (e) {
            UI.toast('خطا: ' + e.message, 'error');
        }
    }

    function renderPlans(plans, customPurchase = null) {
        const container = document.getElementById('plans-list');

        // Sales-closed banner spans the whole grid
        const closedBanner = (!salesEnabled && !isAdmin) ? `
            <div class="empty-state wide is-closed">
                <div class="empty-icon">${UI.icon('lock')}</div>
                <p class="closed-headline">فروش موقتاً بسته شده است</p>
                <p class="empty-hint">در حال حاضر امکان خرید سرویس وجود ندارد. کمی بعد دوباره تلاش کنید.</p>
            </div>` : '';

        if (!plans.length && !customPurchase?.can_purchase) {
            container.innerHTML = closedBanner || `
                <div class="empty-state wide">
                    <div class="empty-icon">${UI.icon('store')}</div>
                    <p>هیچ پلنی موجود نیست</p>
                    <p class="empty-hint">ادمین هنوز پلنی تعریف نکرده است.</p>
                </div>`;
            return;
        }

        // Pick the cheapest-per-day plan as ⭐ "recommended"
        let recommendedIndex = -1;
        if (plans.length > 1) {
            let bestPpd = Infinity;
            plans.forEach((p, i) => {
                const days = Math.max(1, Number(p.duration_days) || 1);
                const ppd = Number(p.price) / days;
                if (ppd < bestPpd) { bestPpd = ppd; recommendedIndex = i; }
            });
        } else if (plans.length === 1) {
            recommendedIndex = 0;
        }

        const customCard = (customPurchase?.can_purchase && (salesEnabled || isAdmin)) ? `
            <div class="plan-card">
                <div class="plan-name">${UI.icon('sliders')} حجم و زمان دلخواه</div>
                <div class="plan-specs">
                    <span class="plan-spec">${UI.icon('database')} هر گیگ: $${UI.formatMoney(customPurchase.price_per_gb)}</span>
                    <span class="plan-spec">${UI.icon('clock')} هر روز: $${UI.formatMoney(customPurchase.price_per_day)}</span>
                </div>
                <div class="plan-price">
                    <span class="plan-price-value">دلخواه</span>
                    <span class="plan-price-currency">/ USD</span>
                </div>
                <button class="btn btn-primary plan-buy-btn" onclick="Pages.buyCustomPlan()">
                    ${UI.icon('plus')} ساخت پلن دلخواه
                </button>
            </div>` : '';

        container.innerHTML = closedBanner + customCard + plans.map((plan, i) => {
            const recommended = i === recommendedIndex;
            const buyDisabled = !(salesEnabled || isAdmin);
            return `
                <div class="plan-card ${recommended ? 'popular' : ''}">
                    <div class="plan-name">${escapeHtml(plan.name)}</div>
                    <div class="plan-specs">
                        <span class="plan-spec">${UI.icon('database')} ${(plan.volume_gb)} گیگ</span>
                        <span class="plan-spec">${UI.icon('clock')} ${(plan.duration_days)} روز</span>
                        ${plan.protocol ? `<span class="plan-spec">${UI.icon('lock')} ${escapeHtml(plan.protocol)}</span>` : ''}
                        ${plan.is_unlimited ? '' : `<span class="plan-spec">${UI.icon('package')} ${(plan.stock_remaining)} موجودی</span>`}
                    </div>
                    <div class="plan-price">
                        <span class="plan-price-value">$${UI.formatMoney(plan.price)}</span>
                        <span class="plan-price-currency">/ ${escapeHtml(plan.currency || 'USD')}</span>
                    </div>
                    ${buyDisabled
                        ? `<button class="btn btn-secondary plan-buy-btn" disabled>${UI.icon('lock')} فروش غیرفعال</button>`
                        : `<button class="btn btn-primary plan-buy-btn" onclick="Pages.buyPlan('${plan.id}')">${UI.icon('store')} خرید این پلن</button>`}
                </div>`;
        }).join('');
    }

    function buyCustomPlan() {
        UI.showModal(`
            <div class="modal-title">خرید دلخواه</div>
            <label class="form-label" for="custom-volume-gb">حجم به گیگابایت</label>
            <input id="custom-volume-gb" class="form-input" inputmode="decimal" dir="ltr" placeholder="25">
            <label class="form-label" for="custom-duration-days">مدت به روز</label>
            <input id="custom-duration-days" class="form-input" inputmode="numeric" dir="ltr" placeholder="30">
            <label class="form-label" for="custom-config-name">نام کانفیگ</label>
            <input id="custom-config-name" class="form-input" dir="ltr" maxlength="32" placeholder="MyVPN" autocomplete="off">
            <p class="form-hint">بعد از پرداخت، کانفیگ با همین حجم و مدت ساخته می‌شود.</p>
            <div class="checkout-methods">
                ${gatewayButtons("Pages.submitCustomPurchase(")}
            </div>
            <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">انصراف</button>
        `);
        setTimeout(() => document.getElementById('custom-volume-gb')?.focus(), 100);
    }

    let _customSubmitting = false;
    async function submitCustomPurchase(paymentMethod) {
        if (_customSubmitting) return;
        const volume = Number((document.getElementById('custom-volume-gb')?.value || '').replace(',', '.'));
        const duration = Number(document.getElementById('custom-duration-days')?.value || 0);
        const configName = (document.getElementById('custom-config-name')?.value || '').trim();
        if (!Number.isFinite(volume) || volume <= 0) {
            UI.toast('حجم معتبر نیست', 'error');
            return;
        }
        if (!Number.isInteger(duration) || duration <= 0) {
            UI.toast('مدت باید عدد صحیح بیشتر از صفر باشد', 'error');
            return;
        }
        if (!/^[a-zA-Z0-9_-]{3,32}$/.test(configName)) {
            UI.toast('نام کانفیگ نامعتبر است', 'error');
            return;
        }
        _customSubmitting = true;
        try {
            UI.toast('در حال ساخت سفارش دلخواه...');
            const result = await API.createPurchase({
                plan_id: null,
                custom_volume_gb: volume,
                custom_duration_days: duration,
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
        } finally {
            _customSubmitting = false;
        }
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
                ${plan.is_unlimited ? '' : `<div><span>موجودی</span><strong>${escapeHtml(plan.stock_remaining)}</strong></div>`}
            </div>
            <label class="form-label" for="checkout-config-name">نام کانفیگ</label>
            <input id="checkout-config-name" class="form-input" dir="ltr" maxlength="32" placeholder="MyVPN" autocomplete="off">
            <p class="form-hint">فقط حروف انگلیسی، عدد، خط تیره و آندرلاین. ۳ تا ۳۲ کاراکتر.</p>
            <div class="checkout-methods">
                ${gatewayButtons(`Pages.submitPurchase('${plan.id}',`)}
            </div>
            <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">انصراف</button>
        `);
        setTimeout(() => document.getElementById('checkout-config-name')?.focus(), 100);
    }

    let _purchaseSubmitting = false;
    async function submitPurchase(planId, paymentMethod) {
        if (_purchaseSubmitting) return;
        const input = document.getElementById('checkout-config-name');
        const configName = (input?.value || '').trim();
        if (!/^[a-zA-Z0-9_-]{3,32}$/.test(configName)) {
            UI.toast('نام کانفیگ نامعتبر است', 'error');
            input?.focus();
            return;
        }

        _purchaseSubmitting = true;
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
        } finally {
            _purchaseSubmitting = false;
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
    async function load_configs(page = 1) {
        try {
            const data = await API.getConfigs(page);
            configsState = {
                page: data.page || page,
                total: data.total || 0,
                pageSize: data.page_size || 20,
            };
            configsCache = data.subscriptions || [];
            renderAllConfigs(data.subscriptions || []);
        } catch (e) {
            UI.toast('خطا: ' + e.message, 'error');
        }
    }

    function renderAllConfigs(subs) {
        const container = document.getElementById('all-configs-list');
        if (!subs.length) {
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">${UI.icon('package')}</div>
                    <p>هنوز سرویسی ندارید</p>
                    <p class="empty-hint">برای شروع، اولین کانفیگ خود را خریداری کنید.</p>
                    <button class="btn btn-primary" onclick="UI.navigate('shop')">${UI.icon('plus')} خرید اولین سرویس</button>
                </div>`;
            return;
        }
        const totalPages = Math.max(1, Math.ceil(configsState.total / configsState.pageSize));
        const pager = totalPages > 1 ? `
            <div class="pager-row">
                <button class="btn btn-secondary btn-sm" ${configsState.page <= 1 ? 'disabled' : ''} onclick="Pages.load_configs(${configsState.page - 1})">قبلی</button>
                <span>${configsState.page} / ${totalPages}</span>
                <button class="btn btn-secondary btn-sm" ${configsState.page >= totalPages ? 'disabled' : ''} onclick="Pages.load_configs(${configsState.page + 1})">بعدی</button>
            </div>
        ` : '';
        container.innerHTML = subs.map(sub => renderConfigCard(sub, true)).join('') + pager;
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
        const credit = parseFloat(wallet.credit_limit || 0);
        const hold = parseFloat(wallet.hold_balance || 0);
        document.getElementById('wallet-card').innerHTML = `
            <div class="wallet-label">${UI.icon('wallet')} موجودی کیف پول</div>
            <div class="wallet-balance-display">$${UI.formatMoney(wallet.balance)}</div>
            ${hold > 0 ? `<div class="row gap-1" style="color:var(--amber);font-size:12px;margin-block-end:var(--space-3)">${UI.icon('clock')} <span>موجودی بلوکه: <strong style="direction:ltr">$${UI.formatMoney(hold)}</strong></span></div>` : ''}
            ${credit > 0 ? `<div class="row gap-1" style="color:var(--text-muted);font-size:11px;margin-block-end:var(--space-3)">${UI.icon('info')} <span>سقف اعتبار: $${UI.formatMoney(credit)}</span></div>` : ''}
            <div class="wallet-actions">
                <button class="btn btn-primary" onclick="Pages.topupWallet()">${UI.icon('plus')} شارژ حساب</button>
                <button class="btn btn-secondary" onclick="Pages.load_wallet()">${UI.icon('refresh')} بروزرسانی</button>
            </div>
        `;
    }

    function renderPayments(payments) {
        const container = document.getElementById('payments-list');
        if (!container) return;
        if (!payments.length) {
            container.innerHTML = `
                <div class="empty-state compact">
                    <p>هنوز پرداختی ثبت نشده است.</p>
                    <button class="btn btn-primary btn-sm" onclick="Pages.topupWallet()">${UI.icon('plus')} شارژ کیف پول</button>
                </div>`;
            return;
        }

        container.innerHTML = payments.map(payment => {
            const canRefresh = ['nowpayments', 'tetrapay', 'tronado'].includes(payment.provider)
                && ['waiting', 'pending', 'confirming'].includes(payment.payment_status);
            const amount = payment.pay_amount
                ? `${UI.formatMoney(payment.pay_amount)} ${escapeHtml(payment.pay_currency || '')}`
                : `${UI.formatMoney(payment.price_amount)} ${escapeHtml(payment.price_currency || '')}`;
            const statusClass = UI.getPaymentStatusClass(payment.payment_status);
            return `
                <div class="payment-item">
                    <div class="tx-info">
                        <span class="tx-type">${escapeHtml(UI.getProviderName(payment.provider))} • ${escapeHtml(UI.getKindText(payment.kind))}</span>
                        <span class="tx-date">
                            <span class="config-status ${statusClass}" style="margin-inline-end:6px">${escapeHtml(UI.getPaymentStatusText(payment.payment_status))}</span>
                            <span dir="ltr">${amount}</span> · ${escapeHtml(UI.formatRelative(payment.created_at))}
                        </span>
                    </div>
                    <div>
                        ${payment.invoice_url && canRefresh ? `<button class="btn btn-primary btn-sm" onclick="Pages.openInvoice(decodeURIComponent('${encodeURIComponent(payment.invoice_url)}'))">پرداخت</button>` : ''}
                        ${canRefresh ? `<button class="btn btn-secondary btn-sm" onclick="Pages.refreshPayment('${payment.id}')">${UI.icon('refresh')}</button>` : ''}
                    </div>
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
            container.innerHTML = `
                <div class="empty-state">
                    <div class="empty-icon">${UI.icon('chart')}</div>
                    <p>هنوز تراکنشی ثبت نشده است</p>
                    <p class="empty-hint">با اولین شارژ یا خرید، تاریخچه‌ی شما اینجا نمایش داده می‌شود.</p>
                </div>`;
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

        container.innerHTML = txs.map(tx => {
            const desc = (tx.description || '').trim();
            return `
            <div class="transaction-item">
                <div class="tx-info">
                    <span class="tx-type">${escapeHtml(typeMap[tx.type] || tx.type || '—')}</span>
                    <span class="tx-date">${desc ? escapeHtml(desc.substring(0, 48)) + ' · ' : ''}${escapeHtml(UI.formatRelative(tx.created_at))}</span>
                </div>
                <span class="tx-amount ${tx.direction === 'credit' ? 'credit' : 'debit'}">
                    $${UI.formatMoney(tx.amount)}
                </span>
            </div>`;
        }).join('');
    }

    function topupWallet() {
        const presetAmounts = [3, 5, 10, 20, 50];
        UI.showModal(`
            <div class="modal-title">افزایش موجودی</div>
            <label class="form-label" for="topup-amount">مبلغ شارژ (دلار)</label>
            <div style="display:flex;gap:8px;margin-bottom:10px;flex-wrap:wrap">
                ${presetAmounts.map(a => `<button class="btn btn-secondary btn-sm" onclick="document.getElementById('topup-amount').value='${a}'" style="flex:1;min-width:48px">$${a}</button>`).join('')}
            </div>
            <input id="topup-amount" class="form-input" inputmode="decimal" dir="ltr" placeholder="5" value="5">
            <p class="form-hint">بعد از پرداخت موفق، موجودی کیف پول به صورت خودکار بروزرسانی می‌شود.</p>
            <div class="checkout-methods">
                ${topupGatewayButtons("Pages.submitTopup(")}
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
        document.getElementById('tickets-container').innerHTML = '<div class="skeleton" style="height:200px"></div>';
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
            container.innerHTML = `
                <div class="support-empty">
                    <div class="support-empty-icon">${UI.icon('support')}</div>
                    <h4>به پشتیبانی خوش آمدید</h4>
                    <p>سوالی دارید؟ پیام خود را در کادر پایین بنویسید و ما در اسرع وقت پاسخ می‌دهیم.</p>
                </div>
            `;
            return;
        }

        const ticket = tickets[0];
        const olderTickets = tickets.slice(1);
        const statusBadgeClass = ticket.status === 'answered' ? 'answered' : ticket.status === 'closed' ? 'closed' : 'open';

        container.innerHTML = `
            <div class="ticket-toolbar">
                <div>
                    <span class="ticket-id">#${String(ticket.id).slice(0, 8)}</span>
                    <span class="ticket-status-badge ${statusBadgeClass}">${escapeHtml(UI.getStatusText(ticket.status))}</span>
                </div>
                ${ticket.status !== 'closed' ? `<button class="btn btn-secondary btn-sm" onclick="Pages.closeTicket('${ticket.id}')">بستن تیکت</button>` : `<button class="btn btn-secondary btn-sm" onclick="Pages.load_support()">${UI.icon('refresh')} بروزرسانی</button>`}
            </div>
            <div class="ticket-thread" id="active-thread">
                ${ticket.messages.length ? renderTicketMessages(ticket.messages) : '<div class="support-empty" style="padding:20px"><p>هنوز پیامی ثبت نشده</p></div>'}
            </div>
            ${olderTickets.length ? `
                <h3 class="section-title compact-title">📋 تیکت‌های قبلی</h3>
                <div class="ticket-history">
                    ${olderTickets.map((t, index) => {
                        const sBadge = t.status === 'answered' ? 'answered' : t.status === 'closed' ? 'closed' : 'open';
                        const iconName = t.status === 'answered' ? 'support' : t.status === 'closed' ? 'lock' : 'clock';
                        return `
                        <button class="ticket-history-card" onclick="Pages.showTicketHistory(${index + 1})">
                            <div class="ticket-history-icon ${sBadge}">${UI.icon(iconName)}</div>
                            <div class="ticket-history-info">
                                <strong>تیکت #${String(t.id).slice(0, 8)}</strong>
                                <span>${escapeHtml(UI.getStatusText(t.status))} • ${UI.formatDateShort(t.created_at)}</span>
                            </div>
                            <div class="ticket-history-meta">
                                <span class="msg-count">${t.messages?.length || 0}</span>
                            </div>
                        </button>
                    `}).join('')}
                </div>
            ` : ''}
        `;

        // Auto-scroll to bottom of thread
        setTimeout(() => {
            const thread = document.getElementById('active-thread');
            if (thread) thread.scrollTop = thread.scrollHeight;
        }, 150);
    }

    function renderTicketMessages(messages) {
        let html = '';
        let lastDate = '';
        messages.forEach((m, i) => {
            // Date separator
            const msgDate = UI.formatDateShort(m.created_at);
            if (msgDate !== lastDate) {
                html += `<div class="chat-date-sep"><span>${msgDate}</span></div>`;
                lastDate = msgDate;
            }
            html += renderTicketMessage(m, i);
        });
        return html;
    }

    function renderTicketMessage(message, index = 0) {
        const isAdmin = message.sender_type === 'admin';
        const senderLabel = isAdmin ? '🛡️ پشتیبانی' : '👤 شما';
        const time = new Date(message.created_at);
        const timeStr = time.toLocaleTimeString('fa-IR', { hour: '2-digit', minute: '2-digit' });
        const delay = Math.min(index * 0.05, 0.5);
        return `
            <div class="chat-bubble ${message.sender_type}" style="animation-delay:${delay}s">
                <span class="bubble-sender">${senderLabel}</span>
                <span class="bubble-text">${escapeHtml(message.text || '📷 تصویر')}</span>
                <span class="bubble-time">
                    ${timeStr}
                    ${!isAdmin ? '<span class="read-indicator">✓</span>' : ''}
                </span>
            </div>
        `;
    }

    function showTicketHistory(index) {
        const ticket = supportTicketsCache[index];
        if (!ticket) return;
        const sBadge = ticket.status === 'answered' ? 'answered' : ticket.status === 'closed' ? 'closed' : 'open';
        UI.showModal(`
            <div class="modal-title">تیکت #${String(ticket.id).slice(0, 8)}</div>
            <div style="text-align:center;margin-bottom:12px">
                <span class="ticket-status-badge ${sBadge}">${escapeHtml(UI.getStatusText(ticket.status))}</span>
                <span style="color:var(--text-muted);font-size:11px;margin-right:8px">${UI.formatDateShort(ticket.created_at)}</span>
            </div>
            <div class="ticket-thread modal-thread">
                ${(ticket.messages || []).length ? renderTicketMessages(ticket.messages) : '<div class="support-empty" style="padding:20px"><p>پیامی ثبت نشده</p></div>'}
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

    const ADMIN_MODULE_META = {
        stats:         { icon: 'chart',   accent: 'cyan',    desc: 'آمار فروش و گزارش‌ها' },
        finance:       { icon: 'wallet',  accent: 'emerald', desc: 'مدیریت مالی و درآمد' },
        users:         { icon: 'users',   accent: 'violet',  desc: 'مدیریت کاربران ربات' },
        customers:     { icon: 'users',   accent: 'cyan',    desc: 'مشتریان فعال' },
        subs:          { icon: 'configs', accent: 'emerald', desc: 'مدیریت سرویس‌ها' },
        gifts:         { icon: 'package', accent: 'amber',   desc: 'هدیه گروهی' },
        plans:         { icon: 'store',   accent: 'cyan',    desc: 'پلن‌های فروش' },
        ready_configs: { icon: 'package', accent: 'violet',  desc: 'کانفیگ‌های آماده' },
        servers:       { icon: 'server',  accent: 'emerald', desc: 'سرورهای X-UI' },
        tickets:       { icon: 'support', accent: 'amber',   desc: 'پاسخ به پشتیبانی' },
        discounts:     { icon: 'zap',     accent: 'rose',    desc: 'کدهای تخفیف' },
        settings:      { icon: 'sliders', accent: 'cyan',    desc: 'تنظیمات ربات' },
        audit:         { icon: 'database', accent: 'violet', desc: 'لاگ عملیات‌ها' },
        broadcast:     { icon: 'share',   accent: 'rose',    desc: 'پیام همگانی' },
        retargeting:   { icon: 'clock',   accent: 'amber',   desc: 'بازاریابی مجدد' },
        backup:        { icon: 'database', accent: 'emerald', desc: 'بکاپ دیتابیس' },
    };

    function renderAdminPanel(data) {
        const overview = document.getElementById('admin-overview');
        const modules = document.getElementById('admin-modules');
        if (!overview || !modules) return;

        overview.innerHTML = `
            <div class="admin-hero">
                <div class="admin-hero-text">
                    <span class="home-eyebrow">پنل مدیریت</span>
                    <h2>خلاصه‌ی وضعیت سیستم</h2>
                    <p class="text-muted" style="font-size:12.5px;margin-block-start:6px">یک نگاه به سلامت ربات و کاربران فعال.</p>
                </div>
                <div class="admin-hero-stat">
                    <span class="stat-label">سرویس فعال</span>
                    <span class="stat-value" style="color:var(--emerald)">${(data.active_subscriptions_count || 0)}</span>
                </div>
            </div>
            <div class="admin-stats-grid">
                <div class="admin-stat-card amber">
                    <span class="stat-label">تیکت باز</span>
                    <span class="stat-value">${(data.open_tickets_count || 0)}</span>
                </div>
                <div class="admin-stat-card cyan">
                    <span class="stat-label">کاربران</span>
                    <span class="stat-value">${(data.users_count || 0)}</span>
                </div>
                <div class="admin-stat-card violet">
                    <span class="stat-label">مشتریان</span>
                    <span class="stat-value">${(data.customers_count || 0)}</span>
                </div>
                <div class="admin-stat-card emerald">
                    <span class="stat-label">سرور فعال</span>
                    <span class="stat-value">${(data.active_servers_count || 0)}</span>
                </div>
            </div>
        `;

        modules.innerHTML = `
            <h3 class="section-title"><span data-icon="admin"></span> بخش‌های مدیریت</h3>
            <div class="admin-tile-grid">
                ${data.modules.map(item => {
                    const section = item.callback.replace('admin:', '');
                    const meta = ADMIN_MODULE_META[section] || { icon: 'package', accent: 'cyan', desc: '' };
                    const safeSection = escapeHtml(section);
                    return `
                    <button class="admin-tile" data-accent="${meta.accent}" onclick="Pages.openAdminModule('${safeSection}')">
                        <div class="admin-tile-icon">${UI.icon(meta.icon)}</div>
                        <div class="admin-tile-body">
                            <div class="admin-tile-title">${escapeHtml(item.title)}</div>
                            <div class="admin-tile-desc">${escapeHtml(meta.desc)}</div>
                        </div>
                        <div class="admin-tile-chev" aria-hidden="true">›</div>
                    </button>`;
                }).join('')}
            </div>
        `;
        // Re-decorate header icon after the new section title hits the DOM.
        modules.querySelectorAll('[data-icon]').forEach(el => {
            el.innerHTML = UI.icon(el.dataset.icon, 'inline-icon');
        });
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
        const meta = ADMIN_MODULE_META[section] || { icon: 'package', accent: 'cyan' };
        const extra = section === 'ready_configs'
            ? renderReadyConfigTools(items)
            : (section === 'gifts' ? renderGiftTools(items) : (section === 'stats' ? renderCustomerReportTools() : ''));
        modules.innerHTML = `
            ${renderAdminSubpageHeader(data.title || 'مدیریت', meta)}
            ${extra}
            <div class="admin-list">
                ${items.length
                    ? items.map(item => renderAdminItem(section, item, meta)).join('')
                    : `<div class="empty-state"><div class="empty-icon">${UI.icon(meta.icon)}</div><p>موردی برای نمایش نیست</p></div>`}
            </div>
        `;
    }

    function renderAdminSubpageHeader(title, meta) {
        const accent = (meta && meta.accent) || 'cyan';
        const iconName = (meta && meta.icon) || 'admin';
        return `
            <div class="admin-subpage-head">
                <button class="admin-back-btn" onclick="Pages.load_admin()" aria-label="بازگشت">
                    <span class="admin-back-chev" aria-hidden="true">›</span>
                </button>
                <div class="admin-subpage-title">
                    <div class="admin-subpage-icon" data-accent="${escapeHtml(accent)}">${UI.icon(iconName)}</div>
                    <h2>${escapeHtml(title)}</h2>
                </div>
            </div>
        `;
    }

    function renderAdminUsers(section, items) {
        const modules = document.getElementById('admin-modules');
        if (!modules) return;
        const meta = ADMIN_MODULE_META[section] || { icon: 'users', accent: 'violet' };
        const title = section === 'customers' ? 'مشتریان' : 'مدیریت کاربران';
        modules.innerHTML = `
            ${renderAdminSubpageHeader(title, meta)}
            <div class="admin-form">
                <label class="form-label">جستجوی کاربر</label>
                <div class="admin-search-row">
                    <input id="admin-user-search" class="form-input" placeholder="آیدی تلگرام، یوزرنیم یا نام" value="${escapeHtml(adminUserSearchState.q)}">
                    <button class="btn btn-primary" onclick="Pages.searchAdminUsers(1)">${UI.icon('zap')} جستجو</button>
                </div>
            </div>
            <div id="admin-users-results" class="admin-list">
                ${items.length
                    ? items.map(item => renderAdminUserSummary(item)).join('')
                    : `<div class="empty-state"><div class="empty-icon">${UI.icon('users')}</div><p>کاربری برای نمایش نیست</p></div>`}
            </div>
        `;
        document.getElementById('admin-user-search')?.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') searchAdminUsers(1);
        });
    }

    function renderAdminUserSummary(item) {
        const name = item.title ?? item.name ?? '-';
        const initial = (name && typeof name === 'string') ? name.trim().charAt(0).toUpperCase() : '?';
        const subtitle = item.subtitle ?? `${item.telegram_id || '-'} | ${item.role || '-'} | ${item.status || '-'}`;
        return `
            <div class="admin-item">
                <div class="admin-item-body">
                    <div class="admin-item-avatar">${escapeHtml(initial)}</div>
                    <div class="admin-item-text">
                        <strong>${escapeHtml(name)}</strong>
                        <span>${escapeHtml(subtitle)}</span>
                    </div>
                </div>
                <div class="admin-actions">
                    <button class="btn btn-primary btn-sm" onclick="Pages.openAdminUser('${escapeHtml(item.id)}')">${UI.icon('info')} پروفایل</button>
                    <button class="btn btn-secondary btn-sm" onclick="Pages.runAdminAction('users', 'toggle_user_ban', '${escapeHtml(item.id)}')">${UI.icon('lock')} بن/رفع بن</button>
                    <button class="btn btn-ghost btn-sm" onclick="Pages.runAdminAction('users', 'reset_trial', '${escapeHtml(item.id)}')">${UI.icon('refresh')} ریست تست</button>
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
        const phoneText = user.phone || user.verified_phone || 'ثبت نشده';
        modules.innerHTML = `
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:16px">
                <button class="btn btn-secondary btn-sm" onclick="Pages.openAdminModule('users')">${UI.icon('home')} بازگشت</button>
                <h3 class="section-title" style="margin:0;flex:1">پروفایل کاربر</h3>
            </div>
            <div class="admin-profile" style="background:var(--bg-elevated);border:1px solid var(--border-strong);box-shadow:var(--shadow-glow)">
                <div style="display:flex;align-items:center;gap:12px;border-bottom:1px solid var(--border);padding-bottom:12px;margin-bottom:12px">
                    <div class="admin-avatar">${(user.name || 'U')[0].toUpperCase()}</div>
                    <div>
                        <strong style="font-size:18px">${escapeHtml(user.name || '-')}</strong>
                        <span style="font-size:12px;opacity:0.7">${escapeHtml(user.telegram_id)} | @${escapeHtml(user.username || '-')}</span>
                    </div>
                </div>
                <div class="admin-profile-grid">
                    <div style="background:rgba(255,255,255,0.02)"><span>نقش</span><strong style="color:var(--cyan)">${escapeHtml(user.role)}</strong></div>
                    <div style="background:rgba(255,255,255,0.02)"><span>وضعیت</span><strong style="${user.status === 'active' ? 'color:var(--emerald)' : 'color:var(--coral)'}">${escapeHtml(user.status)}</strong></div>
                    <div style="background:rgba(255,255,255,0.02)"><span>شماره موبایل</span><strong>${escapeHtml(phoneText)}</strong></div>
                    <div style="background:rgba(255,255,255,0.02)"><span>موجودی</span><strong style="color:var(--emerald)">$${UI.formatMoney(user.wallet_balance)}</strong></div>
                </div>
                <div class="admin-actions wide" style="margin-top:12px">
                    <button class="btn btn-secondary btn-sm" onclick="Pages.runAdminUserAction('${user.id}', 'toggle_user_ban')">${user.status === 'banned' ? 'رفع بن' : 'مسدود کردن'}</button>
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

    function renderGiftTools(items) {
        const servers = items.filter(item => item.id && item.id !== 'summary');
        return `
            <div class="admin-form">
                <strong>اعمال هدیه گروهی</strong>
                <select id="gift-status-scope" class="form-input">
                    <option value="active">فقط کانفیگ‌های فعال</option>
                    <option value="all">همه کانفیگ‌ها</option>
                </select>
                <select id="gift-server-id" class="form-input">
                    <option value="">همه سرورها</option>
                    ${servers.map(server => `<option value="${escapeHtml(server.id)}">${escapeHtml(server.title)}</option>`).join('')}
                </select>
                <select id="gift-type" class="form-input">
                    <option value="time">هدیه زمان</option>
                    <option value="volume">هدیه حجم</option>
                </select>
                <input id="gift-amount" class="form-input" inputmode="decimal" placeholder="مثلاً 7 روز یا 10 گیگ">
                <button class="btn btn-primary btn-block" onclick="Pages.submitAdminGift()">اعمال هدیه</button>
            </div>
        `;
    }

    function renderAdminItem(section, item, meta = null) {
        const actions = item.actions || [];
        const accent = (meta && meta.accent) || ADMIN_MODULE_META[section]?.accent || 'cyan';
        const iconName = (meta && meta.icon) || ADMIN_MODULE_META[section]?.icon || 'package';
        const title = item.title ?? item.value ?? '-';
        const subtitle = item.subtitle ?? (item.value !== undefined ? item.value : '');
        return `
            <div class="admin-item">
                <div class="admin-item-body">
                    <div class="admin-item-icon" data-accent="${escapeHtml(accent)}">${UI.icon(iconName)}</div>
                    <div class="admin-item-text">
                        <strong>${escapeHtml(title)}</strong>
                        ${subtitle ? `<span>${escapeHtml(subtitle)}</span>` : ''}
                    </div>
                </div>
                ${actions.length ? `<div class="admin-actions">
                    ${actions.map(action => `
                        <button class="btn btn-secondary btn-sm" onclick="${getAdminActionHandler(section, action.action, item)}">${escapeHtml(action.label)}</button>
                    `).join('')}
                </div>` : ''}
            </div>
        `;
    }

    function getAdminActionHandler(section, action, item) {
        const id = escapeHtml(item.id);
        if (action === 'view_ticket') return `Pages.openAdminTicket('${id}')`;
        if (action === 'edit_plan_name') return `Pages.showPlanNameEditor('${id}', '${encodeURIComponent(item.title || '')}')`;
        if (action === 'edit_plan_duration') return `Pages.showPlanDurationEditor('${id}')`;
        if (action === 'edit_plan_price') return `Pages.showPlanPriceEditor('${id}')`;
        if (action === 'edit_plan_stock') return `Pages.showPlanStockEditor('${id}')`;
        if (action === 'set_sub_http') return `Pages.setServerSubScheme('${id}', 'http')`;
        if (action === 'set_sub_https') return `Pages.setServerSubScheme('${id}', 'https')`;
        if (action === 'set_sub_panel') return `Pages.setServerSubScheme('${id}', 'panel')`;
        if (action === 'toggle_custom_purchase') return `Pages.toggleCustomPurchase('${escapeHtml(item.subtitle || '')}')`;
        if (action === 'edit_custom_gb') return `Pages.showCustomPurchasePriceEditor('gb')`;
        if (action === 'edit_custom_day') return `Pages.showCustomPurchasePriceEditor('day')`;
        return `Pages.runAdminAction('${section}', '${escapeHtml(action)}', '${id}')`;
    }

    function renderCustomerReportTools() {
        return `
            <div class="admin-form">
                <strong>گزارش خرید مشتری‌ها</strong>
                <div class="admin-search-row">
                    <select id="customer-report-period" class="form-input">
                        <option value="daily">روزانه</option>
                        <option value="weekly">هفتگی</option>
                    </select>
                    <button class="btn btn-primary" onclick="Pages.loadCustomerReport()">دریافت گزارش</button>
                </div>
                <div id="customer-report-results" class="admin-list"></div>
            </div>
        `;
    }

    async function loadCustomerReport(userId = '') {
        const period = document.getElementById('customer-report-period')?.value || 'daily';
        try {
            const report = await API.getAdminCustomerReport(period, userId);
            const container = document.getElementById('customer-report-results');
            if (!container) return;
            container.innerHTML = `
                <div class="admin-item">
                    <strong>${report.period === 'weekly' ? 'گزارش هفتگی' : 'گزارش روزانه'}</strong>
                    <span>${report.total_customers} مشتری | ${report.total_configs} کانفیگ | ${report.total_volume_gb}GB | $${report.total_amount_usd}</span>
                </div>
                ${report.items.length ? report.items.map(item => `
                    <div class="admin-item">
                        <div>
                            <strong>${escapeHtml(item.name || '-')}</strong>
                            <span>${escapeHtml(item.telegram_id || '-')} | ${item.configs_count} کانفیگ | ${item.volume_gb}GB | $${item.amount_usd}</span>
                        </div>
                        <button class="btn btn-secondary btn-sm" onclick="Pages.openAdminUser('${escapeHtml(item.user_id)}')">پروفایل</button>
                    </div>
                `).join('') : '<div class="empty-state compact"><p>خریدی در این بازه ثبت نشده</p></div>'}
            `;
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    function showPlanNameEditor(planId, encodedName = '') {
        const currentName = decodeURIComponent(encodedName || '');
        UI.showModal(`
            <div class="modal-title">تغییر نام پلن</div>
            <label class="form-label" for="admin-plan-name">نام جدید</label>
            <input id="admin-plan-name" class="form-input" value="${escapeHtml(currentName)}" maxlength="80">
            <button class="btn btn-primary btn-block" onclick="Pages.submitPlanName('${planId}')">ثبت نام</button>
            <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">انصراف</button>
        `);
        setTimeout(() => document.getElementById('admin-plan-name')?.focus(), 100);
    }

    async function submitPlanName(planId) {
        const name = (document.getElementById('admin-plan-name')?.value || '').trim();
        if (name.length < 2) {
            UI.toast('نام پلن خیلی کوتاه است', 'error');
            return;
        }
        try {
            const result = await API.updateAdminPlanName(planId, name);
            UI.toast(result.message || 'نام پلن تغییر کرد');
            UI.closeModal();
            await openAdminModule('plans');
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    async function setServerSubScheme(serverId, scheme) {
        try {
            const result = await API.updateAdminServerSubScheme(serverId, scheme);
            UI.toast(result.message || 'نوع لینک ساب تغییر کرد');
            await openAdminModule('servers');
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    function showPlanDurationEditor(planId) {
        UI.showModal(`
            <div class="modal-title">تغییر مدت پلن</div>
            <label class="form-label" for="admin-plan-duration">مدت جدید به روز</label>
            <input id="admin-plan-duration" class="form-input" inputmode="numeric" placeholder="30">
            <p class="form-hint">این مقدار برای خریدهای جدید اعمال می‌شود.</p>
            <button class="btn btn-primary btn-block" onclick="Pages.submitPlanDuration('${planId}')">ثبت مدت</button>
            <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">انصراف</button>
        `);
        setTimeout(() => document.getElementById('admin-plan-duration')?.focus(), 100);
    }

    async function submitPlanDuration(planId) {
        const duration = Number(document.getElementById('admin-plan-duration')?.value || 0);
        if (!Number.isInteger(duration) || duration <= 0) {
            UI.toast('مدت پلن باید عدد صحیح بیشتر از صفر باشد', 'error');
            return;
        }
        try {
            const result = await API.updateAdminPlanDuration(planId, duration);
            UI.toast(result.message || 'مدت پلن تغییر کرد');
            UI.closeModal();
            await openAdminModule('plans');
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    function showPlanPriceEditor(planId) {
        UI.showModal(`
            <div class="modal-title">تغییر قیمت پلن</div>
            <label class="form-label" for="admin-plan-price">قیمت جدید به دلار</label>
            <input id="admin-plan-price" class="form-input" inputmode="decimal" placeholder="3.50">
            <p class="form-hint">این قیمت برای خریدهای جدید اعمال می‌شود.</p>
            <button class="btn btn-primary btn-block" onclick="Pages.submitPlanPrice('${planId}')">ثبت قیمت</button>
            <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">انصراف</button>
        `);
        setTimeout(() => document.getElementById('admin-plan-price')?.focus(), 100);
    }

    async function submitPlanPrice(planId) {
        const rawPrice = document.getElementById('admin-plan-price')?.value || '';
        const normalizedPrice = rawPrice.replace(',', '.');
        const price = Number(normalizedPrice);
        if (!Number.isFinite(price) || price <= 0) {
            UI.toast('قیمت پلن باید عدد بیشتر از صفر باشد', 'error');
            return;
        }
        try {
            const result = await API.updateAdminPlanPrice(planId, normalizedPrice);
            UI.toast(result.message || 'قیمت پلن تغییر کرد');
            UI.closeModal();
            await openAdminModule('plans');
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    function showPlanStockEditor(planId) {
        UI.showModal(`
            <div class="modal-title">تنظیم موجودی فروش</div>
            <label class="form-label" for="admin-plan-stock">حداکثر تعداد فروش</label>
            <input id="admin-plan-stock" class="form-input" inputmode="numeric" placeholder="0">
            <p class="form-hint">عدد 0 یعنی موجودی نامحدود و در ربات به کاربر نمایش داده نمی‌شود.</p>
            <button class="btn btn-primary btn-block" onclick="Pages.submitPlanStock('${planId}')">ثبت موجودی</button>
            <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">انصراف</button>
        `);
        setTimeout(() => document.getElementById('admin-plan-stock')?.focus(), 100);
    }

    async function submitPlanStock(planId) {
        const stock = Number(document.getElementById('admin-plan-stock')?.value || 0);
        if (!Number.isInteger(stock) || stock < 0) {
            UI.toast('موجودی باید عدد صحیح صفر یا بیشتر باشد', 'error');
            return;
        }
        try {
            const result = await API.updateAdminPlanStock(planId, stock);
            UI.toast(result.message || 'موجودی پلن تغییر کرد');
            UI.closeModal();
            await openAdminModule('plans');
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    async function toggleCustomPurchase(subtitle = '') {
        const enabled = String(subtitle).trim().startsWith('فعال');
        try {
            const result = await API.updateCustomPurchaseSettings({ enabled: !enabled });
            UI.toast(result.message || 'تنظیمات خرید دلخواه تغییر کرد');
            await openAdminModule('settings');
        } catch (e) {
            UI.toast(e.message, 'error');
        }
    }

    function showCustomPurchasePriceEditor(type) {
        const isGb = type === 'gb';
        UI.showModal(`
            <div class="modal-title">${isGb ? 'قیمت هر GB' : 'قیمت هر روز'} خرید دلخواه</div>
            <label class="form-label" for="custom-purchase-price">قیمت به دلار</label>
            <input id="custom-purchase-price" class="form-input" inputmode="decimal" dir="ltr" placeholder="${isGb ? '0.10' : '0.05'}">
            <p class="form-hint">این قیمت برای ساخت پلن‌های دلخواه جدید استفاده می‌شود.</p>
            <button class="btn btn-primary btn-block" onclick="Pages.submitCustomPurchasePrice('${type}')">ثبت قیمت</button>
            <button class="btn btn-secondary btn-block" style="margin-top:10px" onclick="UI.closeModal()">انصراف</button>
        `);
        setTimeout(() => document.getElementById('custom-purchase-price')?.focus(), 100);
    }

    async function submitCustomPurchasePrice(type) {
        const raw = document.getElementById('custom-purchase-price')?.value || '';
        const normalized = raw.replace(',', '.');
        const price = Number(normalized);
        if (!Number.isFinite(price) || price <= 0) {
            UI.toast('قیمت باید عدد بیشتر از صفر باشد', 'error');
            return;
        }
        try {
            const payload = type === 'gb' ? { price_per_gb: normalized } : { price_per_day: normalized };
            const result = await API.updateCustomPurchaseSettings(payload);
            UI.toast(result.message || 'قیمت خرید دلخواه تغییر کرد');
            UI.closeModal();
            await openAdminModule('settings');
        } catch (e) {
            UI.toast(e.message, 'error');
        }
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

    async function submitAdminGift() {
        const payload = {
            status_scope: document.getElementById('gift-status-scope')?.value || 'active',
            server_id: document.getElementById('gift-server-id')?.value || null,
            gift_type: document.getElementById('gift-type')?.value || 'time',
            amount: Number(document.getElementById('gift-amount')?.value || 0),
        };
        if (!payload.amount || payload.amount <= 0) {
            UI.toast('مقدار هدیه معتبر نیست', 'error');
            return;
        }
        if (payload.gift_type === 'time' && !Number.isInteger(payload.amount)) {
            UI.toast('هدیه زمان باید عدد صحیح روز باشد', 'error');
            return;
        }
        try {
            const result = await API.grantAdminGift(payload);
            UI.toast(result.message || 'هدیه اعمال شد');
            await openAdminModule('gifts');
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

    function openBotChat(startParam = '') {
        const botUsername = getBotUsername();
        if (!botUsername) {
            UI.toast('یوزرنیم ربات پیدا نشد', 'error');
            return;
        }
        const url = startParam
            ? `https://t.me/${botUsername}?start=${encodeURIComponent(startParam)}`
            : `https://t.me/${botUsername}`;
        const tg = window.Telegram?.WebApp;
        if (tg && tg.openTelegramLink) {
            tg.openTelegramLink(url);
        } else {
            window.open(url, '_blank');
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
            <div class="referral-hero text-center">
                <div class="empty-icon" style="margin:0 auto var(--space-3)">${UI.icon('users')}</div>
                <h2 style="margin-block-end:6px">دعوت دوستان</h2>
                <p class="empty-hint">با هر دوستی که از طریق لینک شما عضو شود و خرید کند، پاداش می‌گیرید.</p>
            </div>

            <div class="ref-stats">
                <div class="stat-card">
                    <div class="stat-label">دعوت شده</div>
                    <div class="stat-value">${(data.referral_count || 0)}</div>
                </div>
                <div class="stat-card">
                    <div class="stat-label">درآمد کل</div>
                    <div class="stat-value">$${UI.formatMoney(data.total_earned)}</div>
                </div>
            </div>

            ${data.ref_code ? `
                <div class="referral-card">
                    <div class="form-label">لینک دعوت اختصاصی شما</div>
                    <div class="ref-code-box" onclick="UI.copyToClipboard('${refLink}')" title="برای کپی، بزنید">
                        <span>${escapeHtml(refLink)}</span>
                        <span class="btn btn-ghost btn-sm">${UI.icon('copy')}</span>
                    </div>
                    <p class="form-hint">برای کپی روی متن بزنید.</p>
                    <button class="btn btn-primary btn-block" style="margin-block-start:var(--space-3)" onclick="shareRefLink('${refLink}')">
                        ${UI.icon('share')} اشتراک‌گذاری در تلگرام
                    </button>
                </div>
            ` : `
                <div class="empty-state">
                    <div class="empty-icon">${UI.icon('info')}</div>
                    <p>کد دعوت شما هنوز ایجاد نشده</p>
                    <p class="empty-hint">برای فعال‌سازی، یک‌بار از پنل ربات /start را بزنید.</p>
                </div>
            `}
        `;
    }

    // ─── Expose ─────────────────────────────────────────────────────────
    return {
        load_dashboard, load_store, load_configs,
        load_wallet, load_support, load_referral, load_admin,
        showConfigDetail, showRenewal, setRenewalType, submitRenewal,
        buyPlan, buyCustomPlan, submitPurchase, submitCustomPurchase, openInvoice, topupWallet, submitTopup, refreshPayment,
        showTicketHistory, closeTicket, openAdminModule, openAdminTicket, submitAdminTicketReply, runAdminAction,
        loadCustomerReport, showPlanNameEditor, submitPlanName, setServerSubScheme,
        showPlanDurationEditor, submitPlanDuration, showPlanPriceEditor, submitPlanPrice, showPlanStockEditor, submitPlanStock,
        toggleCustomPurchase, showCustomPurchasePriceEditor, submitCustomPurchasePrice,
        searchAdminUsers, openAdminUser, runAdminUserAction, adjustAdminUserBalance, sendAdminUserMessage,
        createReadyConfigPlan, addReadyConfigItems, submitAdminGift, openBotAdmin, openBotChat,
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
