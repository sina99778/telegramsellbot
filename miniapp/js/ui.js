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
            toast('✅ کپی شد!');
            try { window.Telegram?.WebApp?.HapticFeedback?.notificationOccurred('success'); } catch {}
        }).catch(() => toast('❌ خطا در کپی', 'error'));
    }

    return {
        toast, showModal, closeModal, navigate,
        formatBytes, formatDate, formatMoney,
        getStatusText, getStatusClass, getUsagePercent, getProgressClass,
        daysLeft, copyToClipboard,
    };
})();
