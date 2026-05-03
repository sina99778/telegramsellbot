/**
 * UI utilities — toast, modal, formatters, navigation.
 */
const UI = (() => {
    // ─── Toast ──────────────────────────────────────────────────────────
    let toastTimer = null;
    function toast(message, type = 'success') {
        const el = document.getElementById('toast');
        el.textContent = message;
        el.className = `toast show ${type}`;
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.className = 'toast hidden', 3000);
    }

    // ─── Modal ──────────────────────────────────────────────────────────
    function showModal(html) {
        const overlay = document.getElementById('modal-overlay');
        const content = document.getElementById('modal-content');
        content.innerHTML = html;
        overlay.classList.remove('hidden');
        overlay.onclick = (e) => {
            if (e.target === overlay) closeModal();
        };
    }

    function closeModal() {
        document.getElementById('modal-overlay').classList.add('hidden');
    }

    // ─── Navigation ─────────────────────────────────────────────────────
    function navigate(pageName) {
        document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
        document.querySelectorAll('.nav-btn').forEach(b => b.classList.remove('active'));

        const page = document.getElementById(`page-${pageName}`);
        const btn = document.querySelector(`.nav-btn[data-page="${pageName}"]`);
        if (page) page.classList.add('active');
        if (btn) btn.classList.add('active');

        // Trigger page load
        if (typeof Pages !== 'undefined' && Pages[`load_${pageName}`]) {
            Pages[`load_${pageName}`]();
        }
    }

    // ─── Formatters ─────────────────────────────────────────────────────
    function formatBytes(bytes) {
        if (bytes === 0) return '0 B';
        const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
        const i = Math.floor(Math.log(bytes) / Math.log(1024));
        return (bytes / Math.pow(1024, i)).toFixed(2) + ' ' + sizes[i];
    }

    function formatDate(dateStr) {
        if (!dateStr) return '-';
        const d = new Date(dateStr);
        return d.toLocaleDateString('fa-IR');
    }

    function formatMoney(amount) {
        return parseFloat(amount || 0).toFixed(2);
    }

    function getStatusText(status) {
        const map = {
            'active': 'فعال',
            'pending_activation': 'در انتظار',
            'expired': 'منقضی',
            'disabled': 'غیرفعال',
            'open': 'باز',
            'answered': 'پاسخ داده شده',
            'closed': 'بسته',
        };
        return map[status] || status;
    }

    function getStatusClass(status) {
        const map = {
            'active': 'active',
            'pending_activation': 'pending',
            'expired': 'expired',
            'disabled': 'expired',
            'open': 'pending',
            'answered': 'active',
            'closed': 'expired',
        };
        return map[status] || '';
    }

    function getUsagePercent(used, total) {
        if (!total || total === 0) return 0;
        return Math.min(Math.round((used / total) * 100), 100);
    }

    function getProgressClass(pct) {
        if (pct >= 90) return 'danger';
        if (pct >= 70) return 'warning';
        return '';
    }

    function daysLeft(endDate) {
        if (!endDate) return '—';
        const now = new Date();
        const end = new Date(endDate);
        const diff = Math.ceil((end - now) / (1000 * 60 * 60 * 24));
        return diff > 0 ? `${diff} روز` : 'منقضی';
    }

    function copyToClipboard(text) {
        navigator.clipboard.writeText(text).then(() => {
            toast('کپی شد');
            try { window.Telegram?.WebApp?.HapticFeedback?.notificationOccurred('success'); } catch {}
        }).catch(() => toast('خطا در کپی', 'error'));
    }

    function icon(name, className = 'icon') {
        const icons = {
            home: '<path d="m3 10 9-7 9 7"/><path d="M5 10v10h14V10"/><path d="M9 20v-6h6v6"/>',
            store: '<path d="M6 2h12l2 7H4l2-7Z"/><path d="M4 9v11h16V9"/><path d="M9 20v-6h6v6"/>',
            configs: '<path d="M8 2h8l4 4v14H4V2h4Z"/><path d="M14 2v6h6"/><path d="M8 13h8"/><path d="M8 17h6"/>',
            wallet: '<path d="M3 7h18v12H3z"/><path d="M16 12h5v4h-5z"/><path d="M3 7l3-4h12l3 4"/>',
            support: '<path d="M4 5h16v11H7l-3 3z"/><path d="M8 9h8"/><path d="M8 13h5"/>',
            users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/>',
            admin: '<path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5z"/><path d="M9 12l2 2 4-4"/>',
            server: '<rect x="3" y="4" width="18" height="6" rx="2"/><rect x="3" y="14" width="18" height="6" rx="2"/><path d="M7 7h.01M7 17h.01"/>',
            chart: '<path d="M3 3v18h18"/><path d="M8 17V9"/><path d="M13 17V5"/><path d="M18 17v-6"/>',
            database: '<ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5"/><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
            package: '<path d="m21 8-9-5-9 5 9 5 9-5Z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
            clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
            lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/>',
            share: '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 10.6 6.8-4.2"/><path d="m8.6 13.4 6.8 4.2"/>',
            copy: '<rect x="9" y="9" width="13" height="13" rx="2"/><rect x="2" y="2" width="13" height="13" rx="2"/>',
            plus: '<path d="M12 5v14"/><path d="M5 12h14"/>',
            sliders: '<path d="M4 21v-7"/><path d="M4 10V3"/><path d="M12 21v-9"/><path d="M12 8V3"/><path d="M20 21v-5"/><path d="M20 12V3"/><path d="M2 14h4"/><path d="M10 8h4"/><path d="M18 16h4"/>',
        };
        const body = icons[name] || icons.package;
        return `<svg class="${className}" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
    }

    return {
        toast, showModal, closeModal, navigate,
        icon,
        formatBytes, formatDate, formatMoney,
        getStatusText, getStatusClass, getUsagePercent, getProgressClass,
        daysLeft, copyToClipboard,
    };
})();
