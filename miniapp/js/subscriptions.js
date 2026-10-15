/** Shared presentation rules. No network, credentials, or mutations. */
const SubscriptionUI = (() => {
    function health(sub, now = Date.now()) {
        const used = Math.max(0, Number(sub.used_bytes) || 0);
        const total = Math.max(0, Number(sub.volume_bytes) || 0);
        const unlimited = total === 0;
        const pct = unlimited ? 0 : Math.min(100, Math.max(0, Math.round(used / total * 100)));
        const end = sub.ends_at ? Date.parse(sub.ends_at) : NaN;
        const days = Number.isFinite(end) ? Math.max(0, Math.ceil((end - now) / 86400000)) : null;
        // Never expire a service because a rounded percentage reached 100.
        const isExpired = ['expired', 'disabled'].includes(sub.status) || days === 0 || (!unlimited && used >= total);
        const pending = !!sub.panel_sync_pending;
        const isNearLimit = !unlimited && pct >= 90;
        const needsAttention = pending || isExpired || isNearLimit || (days !== null && days <= 3);
        const status = isExpired && sub.status === 'active' ? 'expired' : sub.status;
        const labels = { active: 'فعال', expired: 'منقضی', disabled: 'غیرفعال', pending_activation: 'آمادهٔ اتصال' };
        let warning = '';
        if (pending) warning = 'درخواست ثبت شده؛ تغییرات در انتظار اعمال روی پنل است. برای تمدید دوباره منتظر بمانید.';
        else if (isExpired) warning = sub.status === 'disabled' ? 'سرویس غیرفعال است؛ برای بررسی با پشتیبانی تماس بگیرید.' : 'زمان یا حجم سرویس به پایان رسیده است.';
        else if (isNearLimit) warning = 'حجم سرویس رو به پایان است.';
        else if (days !== null && days <= 3) warning = 'زمان سرویس رو به پایان است.';
        return {
            used, total, unlimited, pct, days, pending, isExpired, isNearLimit, needsAttention, warning,
            remainingBytes: unlimited ? null : Math.max(total - used, 0),
            pctClass: pct >= 90 ? 'danger' : pct >= 75 ? 'warn' : '',
            statusText: pending ? 'در انتظار پنل' : (labels[status] || 'نامشخص'),
            statusClass: pending ? 'pending' : isExpired ? 'expired' : status === 'pending_activation' ? 'pending' : 'active',
            daysLabel: days === null ? (sub.status === 'pending_activation' ? 'با اولین اتصال' : 'بدون انقضای زمانی') : days === 0 ? 'منقضی' : `${days} روز`,
        };
    }

    function filter(subs, query = '', mode = 'all') {
        const normalize = value => String(value || '').normalize('NFKC').replace(/ي/g, 'ی').replace(/ك/g, 'ک').toLocaleLowerCase('fa').trim();
        const needle = normalize(query);
        return subs.filter(sub => {
            const state = health(sub);
            const matches = !needle || normalize(`${sub.config_name || ''} ${sub.plan_name || ''}`).includes(needle);
            return matches && (mode === 'attention' ? state.needsAttention : mode === 'active' ? !state.isExpired && !state.pending && sub.status === 'active' : true);
        });
    }
    return { health, filter };
})();
