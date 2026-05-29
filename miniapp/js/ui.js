/**
 * UI utilities — toast, modal, navigation, formatters, icons, haptics.
 *
 * Everything that touches the DOM lives here so pages.js can stay
 * declarative. All public functions are stable across the redesign
 * (toast, showModal, closeModal, navigate, formatBytes, formatDate,
 * formatMoney, copyToClipboard, icon, …) — pages.js continues to work
 * unchanged.
 */
const UI = (() => {
    const _tg = (typeof window !== 'undefined' && window.Telegram && window.Telegram.WebApp) || null;
    const _PERSIAN_DIGITS = ['۰', '۱', '۲', '۳', '۴', '۵', '۶', '۷', '۸', '۹'];

    // ─── Haptics ────────────────────────────────────────────────────────
    function haptic(kind = 'selection') {
        if (!_tg || !_tg.HapticFeedback) return;
        try {
            switch (kind) {
                case 'light':   _tg.HapticFeedback.impactOccurred('light'); break;
                case 'medium':  _tg.HapticFeedback.impactOccurred('medium'); break;
                case 'heavy':   _tg.HapticFeedback.impactOccurred('heavy'); break;
                case 'success': _tg.HapticFeedback.notificationOccurred('success'); break;
                case 'warning': _tg.HapticFeedback.notificationOccurred('warning'); break;
                case 'error':   _tg.HapticFeedback.notificationOccurred('error'); break;
                default:        _tg.HapticFeedback.selectionChanged();
            }
        } catch (_) { /* old Telegram client */ }
    }

    // ─── Toast ──────────────────────────────────────────────────────────
    let toastTimer = null;
    function toast(message, type = 'success') {
        const el = document.getElementById('toast');
        if (!el) return;
        el.textContent = message;
        el.className = `toast show ${type}`;

        if (type === 'error')   haptic('error');
        else if (type === 'success') haptic('success');

        const baseDuration = type === 'error' ? 5000 : 3500;
        const lengthBonus = Math.min(2500, Math.max(0, (message.length - 30) * 40));
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => { el.className = 'toast'; }, baseDuration + lengthBonus);
    }

    // ─── Modal ──────────────────────────────────────────────────────────
    let _tgBackHandler = null;
    let _modalKeyAttached = false;

    function showModal(html) {
        const overlay = document.getElementById('modal-overlay');
        const content = document.getElementById('modal-content');
        if (!overlay || !content) return;
        content.innerHTML = html;
        overlay.classList.remove('hidden');
        overlay.onclick = (e) => { if (e.target === overlay) closeModal(); };
        if (!_modalKeyAttached) {
            document.addEventListener('keydown', _onModalKey);
            _modalKeyAttached = true;
        }
        if (_tg && _tg.BackButton && !_tgBackHandler) {
            _tgBackHandler = () => closeModal();
            try { _tg.BackButton.show(); _tg.BackButton.onClick(_tgBackHandler); }
            catch (_) {}
        }
        haptic('light');
    }
    function _onModalKey(ev) {
        if (ev.key === 'Escape' || ev.key === 'Esc') closeModal();
    }
    function closeModal() {
        const overlay = document.getElementById('modal-overlay');
        if (overlay) overlay.classList.add('hidden');
        if (_modalKeyAttached) {
            document.removeEventListener('keydown', _onModalKey);
            _modalKeyAttached = false;
        }
        if (_tg && _tg.BackButton && _tgBackHandler) {
            try { _tg.BackButton.offClick(_tgBackHandler); _tg.BackButton.hide(); }
            catch (_) {}
            _tgBackHandler = null;
        }
    }

    // Confirm dialog (returns a Promise<boolean>).
    function confirm(opts = {}) {
        const {
            title = 'تأیید',
            message = '',
            confirmText = 'تأیید',
            cancelText = 'انصراف',
            danger = false,
        } = opts;
        return new Promise(resolve => {
            const html = `
                <h2>${escapeHtml(title)}</h2>
                ${message ? `<p class="text-muted" style="margin-block-end:var(--space-3)">${escapeHtml(message)}</p>` : ''}
                <div class="modal-actions">
                    <button class="btn btn-ghost" data-act="cancel">${escapeHtml(cancelText)}</button>
                    <button class="btn ${danger ? 'btn-danger' : 'btn-primary'}" data-act="ok">${escapeHtml(confirmText)}</button>
                </div>`;
            showModal(html);
            const content = document.getElementById('modal-content');
            content.querySelector('[data-act="cancel"]').addEventListener('click', () => { closeModal(); resolve(false); });
            content.querySelector('[data-act="ok"]').addEventListener('click', () => { closeModal(); resolve(true); });
        });
    }

    // ─── Navigation ─────────────────────────────────────────────────────
    function navigate(pageName) {
        document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
        document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));

        const page = document.getElementById(`page-${pageName}`);
        const btn = document.querySelector(`.nav-btn[data-page="${pageName}"]`);
        if (page) page.classList.add('active');
        if (btn)  btn.classList.add('active');

        if (typeof Pages !== 'undefined' && Pages[`load_${pageName}`]) {
            Pages[`load_${pageName}`]();
        }
        // Scroll the new page to top — small UX win when bouncing between tabs.
        const scroll = page?.querySelector('.page-scroll');
        if (scroll) scroll.scrollTop = 0;
        haptic('selection');
    }

    // ─── Escape helper ──────────────────────────────────────────────────
    function escapeHtml(input) {
        if (input === null || input === undefined) return '';
        return String(input)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }
    // Expose globally too, so pages.js call sites that use escapeHtml continue to work.
    if (typeof window !== 'undefined') window.escapeHtml = escapeHtml;

    // ─── Persian helpers ────────────────────────────────────────────────
    function toPersianDigits(s) {
        return String(s).replace(/[0-9]/g, d => _PERSIAN_DIGITS[d]);
    }

    // ─── Formatters ─────────────────────────────────────────────────────
    function formatBytes(bytes) {
        // X-UI treats totalGB=0 as UNLIMITED traffic. Imported subs that
        // landed without a known volume show up as 0 here — but the
        // panel-side client is actually uncapped, so the right user-
        // facing label is "نامحدود", not "0 B".
        if (bytes === 0 || bytes === '0' || bytes === null || bytes === undefined) return 'نامحدود';
        if (bytes < 0) return '0 B';
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(1024));
        return (bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 2) + ' ' + sizes[i];
    }

    function formatDate(dateStr) {
        if (!dateStr) return '—';
        const d = new Date(dateStr);
        if (isNaN(d)) return '—';
        const date = d.toLocaleDateString('fa-IR');
        const time = d.toLocaleTimeString('fa-IR', { hour: '2-digit', minute: '2-digit' });
        return `${date} ${time}`;
    }
    function formatDateShort(dateStr) {
        if (!dateStr) return '—';
        const d = new Date(dateStr);
        if (isNaN(d)) return '—';
        return d.toLocaleDateString('fa-IR');
    }
    function formatRelative(dateStr) {
        if (!dateStr) return '—';
        const ts = new Date(dateStr).getTime();
        if (isNaN(ts)) return '—';
        const diff = Math.max(0, (Date.now() - ts) / 1000);
        if (diff < 60)    return 'لحظاتی پیش';
        if (diff < 3600)  return Math.floor(diff / 60) + ' دقیقه پیش';
        if (diff < 86400) return Math.floor(diff / 3600) + ' ساعت پیش';
        if (diff < 604800) return Math.floor(diff / 86400) + ' روز پیش';
        return formatDateShort(dateStr);
    }
    function formatMoney(amount) {
        const n = parseFloat(amount || 0);
        if (isNaN(n)) return '0.00';
        return n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    }

    // ─── Status helpers ────────────────────────────────────────────────
    const _STATUS_TEXT = {
        active: 'فعال',
        pending_activation: 'در انتظار',
        expired: 'منقضی',
        disabled: 'غیرفعال',
        open: 'باز',
        answered: 'پاسخ داده شده',
        closed: 'بسته',
    };
    const _STATUS_CLASS = {
        active: 'active',
        pending_activation: 'pending',
        expired: 'expired',
        disabled: 'expired',
        open: 'pending',
        answered: 'active',
        closed: 'expired',
    };
    function getStatusText(s)  { return _STATUS_TEXT[s] || s || '—'; }
    function getStatusClass(s) { return _STATUS_CLASS[s] || ''; }

    function getUsagePercent(used, total) {
        const u = Number(used) || 0;
        const t = Number(total) || 0;
        if (t <= 0) return 0;
        return Math.min(100, Math.round((u / t) * 100));
    }
    function getProgressClass(pct) {
        if (pct >= 90) return 'danger';
        if (pct >= 75) return 'warn';
        return '';
    }

    const _PAYMENT_STATUS_TEXT = {
        waiting: 'در انتظار پرداخت',
        waiting_hash: 'در انتظار هش تراکنش',
        waiting_receipt: 'در انتظار رسید',
        pending: 'در حال بررسی',
        pending_approval: 'در انتظار تأیید ادمین',
        confirming: 'در حال تأیید',
        confirmed: 'تأیید شده',
        finished: 'موفق',
        completed: 'تکمیل شده',
        failed: 'ناموفق',
        expired: 'منقضی شده',
        refunded: 'بازگشت داده شده',
        rejected: 'رد شده',
    };
    const _PAYMENT_STATUS_CLASS = {
        finished: 'active', completed: 'active', confirmed: 'active',
        waiting: 'pending', waiting_hash: 'pending', waiting_receipt: 'pending',
        pending: 'pending', pending_approval: 'pending', confirming: 'pending',
        failed: 'expired', expired: 'expired', refunded: 'expired', rejected: 'expired',
    };
    function getPaymentStatusText(s)  { return _PAYMENT_STATUS_TEXT[s] || s || '—'; }
    function getPaymentStatusClass(s) { return _PAYMENT_STATUS_CLASS[s] || ''; }

    const _PROVIDER_NAMES = {
        nowpayments: 'NOWPayments',
        tetrapay: 'تتراپی',
        tronado: 'ترونادو',
        manual_crypto: 'کریپتو دستی',
        card_to_card: 'کارت به کارت',
        wallet: 'کیف پول',
    };
    function getProviderName(p) { return _PROVIDER_NAMES[p] || p || '—'; }

    const _KIND_TEXT = {
        wallet_topup: 'شارژ حساب',
        direct_purchase: 'خرید مستقیم',
        direct_renewal: 'تمدید مستقیم',
    };
    function getKindText(k) { return _KIND_TEXT[k] || k || '—'; }

    function daysLeft(endsAt) {
        if (!endsAt) return null;
        const t = new Date(endsAt).getTime();
        if (isNaN(t)) return null;
        return Math.max(0, Math.ceil((t - Date.now()) / 86400000));
    }

    // ─── Clipboard ─────────────────────────────────────────────────────
    async function copyToClipboard(text) {
        if (!text) return;
        try {
            if (navigator.clipboard && window.isSecureContext) {
                await navigator.clipboard.writeText(text);
            } else {
                const ta = document.createElement('textarea');
                ta.value = text;
                ta.style.position = 'fixed';
                ta.style.opacity = '0';
                document.body.appendChild(ta);
                ta.select();
                document.execCommand('copy');
                document.body.removeChild(ta);
            }
            toast('✓ کپی شد', 'success');
            haptic('light');
        } catch (_) {
            toast('کپی نشد — متن را دستی انتخاب کنید', 'error');
        }
    }

    // ─── Async button helper ───────────────────────────────────────────
    async function withButtonLoading(btn, fn) {
        if (!btn) return await fn();
        const wasDisabled = btn.disabled;
        btn.classList.add('is-loading');
        btn.disabled = true;
        try { return await fn(); }
        finally {
            btn.classList.remove('is-loading');
            btn.disabled = wasDisabled;
        }
    }

    // ─── Icon system (SVG strings) ─────────────────────────────────────
    function icon(name, className = 'inline-icon') {
        const icons = {
            home:     '<path d="M3 12 12 3l9 9"/><path d="M5 10v10h14V10"/>',
            store:    '<path d="M3 9 5 3h14l2 6"/><path d="M3 9v11h18V9"/><path d="M9 14h6"/>',
            configs:  '<rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01"/>',
            wallet:   '<rect x="3" y="6" width="18" height="14" rx="3"/><path d="M3 10h18"/><path d="M16 14h2"/>',
            support:  '<path d="M4 5h16v11H7l-3 3z"/><path d="M8 9h8"/><path d="M8 13h5"/>',
            users:    '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
            admin:    '<path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5z"/><path d="M9 12l2 2 4-4"/>',
            server:   '<rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01"/>',
            chart:    '<path d="M3 3v18h18"/><path d="M8 17V9"/><path d="M13 17V5"/><path d="M18 17v-6"/>',
            package:  '<path d="m21 8-9-5-9 5 9 5 9-5Z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
            clock:    '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
            lock:     '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
            share:    '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 10.6 6.8-4.2"/><path d="m8.6 13.4 6.8 4.2"/>',
            copy:     '<rect x="9" y="9" width="13" height="13" rx="2"/><rect x="2" y="2" width="13" height="13" rx="2"/>',
            plus:     '<path d="M12 5v14"/><path d="M5 12h14"/>',
            check:    '<path d="m5 12 4 4L19 7"/>',
            x:        '<path d="M18 6 6 18"/><path d="M6 6l12 12"/>',
            sliders:  '<path d="M4 21v-7"/><path d="M4 10V3"/><path d="M12 21v-9"/><path d="M12 8V3"/><path d="M20 21v-5"/><path d="M20 12V3"/><path d="M2 14h4"/><path d="M10 8h4"/><path d="M18 16h4"/>',
            refresh:  '<path d="M21 2v6h-6"/><path d="M3 12a9 9 0 1 0 2.13-5.88L21 8"/>',
            zap:      '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10"/>',
            data:     '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
            database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
            arrowDown:'<path d="M12 5v14"/><path d="m19 12-7 7-7-7"/>',
            arrowUp:  '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
            warning:  '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>',
            info:     '<circle cx="12" cy="12" r="9"/><path d="M12 16v-4"/><path d="M12 8h.01"/>',
        };
        const body = icons[name] || icons.package;
        return `<svg class="${className}" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
    }

    return {
        toast, showModal, closeModal, confirm,
        navigate, haptic,
        icon, escapeHtml, toPersianDigits,
        formatBytes, formatDate, formatDateShort, formatRelative, formatMoney,
        getStatusText, getStatusClass, getUsagePercent, getProgressClass,
        getPaymentStatusText, getPaymentStatusClass, getProviderName, getKindText,
        daysLeft, copyToClipboard, withButtonLoading,
    };
})();
