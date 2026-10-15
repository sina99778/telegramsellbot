/** Display policy is separate from ledger amounts (which remain USD).
 * Decimal strings use integer arithmetic so conversion cannot lose a Toman.
 */
const Money = (() => {
    function policy() {
        const config = window.AppConfig;
        if (!config || !['USD', 'IRT'].includes(config.display_currency)) throw new Error('تنظیم واحد پول دریافت نشده است؛ صفحه را بروزرسانی کنید.');
        if (config.display_currency === 'IRT' && (!Number.isSafeInteger(config.toman_rate) || config.toman_rate <= 0)) throw new Error('نرخ تبدیل تومان معتبر نیست.');
        return config;
    }
    function code() { return policy().display_currency; }
    function unit() { return code() === 'IRT' ? 'تومان' : 'دلار'; }
    function decimal(value) {
        let text = String(value ?? '0');
        if (typeof value === 'number' && Number.isFinite(value) && /e/i.test(text)) text = value.toFixed(8);
        const match = /^(-?)(\d{1,30})(?:\.(\d{1,18}))?$/.exec(text);
        if (!match) throw new Error('مبلغ معتبر نیست.');
        const fraction = match[3] || '';
        return { n:BigInt((match[1] || '') + match[2] + fraction), d:10n ** BigInt(fraction.length) };
    }
    function group(value) { return String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }
    function fixed(value, places) {
        const {n,d} = decimal(value);
        const sign = n < 0n ? '-' : '';
        const scale = 10n ** BigInt(places);
        const absolute = n < 0n ? -n : n;
        const scaled = (absolute * scale * 2n + d) / (2n * d);
        return sign + group(scaled / scale) + (places ? '.' + String(scaled % scale).padStart(places, '0') : '');
    }
    function toman(value) {
        const {n,d} = decimal(value);
        return n * BigInt(policy().toman_rate) / d;
    }
    function amount(value) { return code() === 'IRT' ? group(toman(value)) : fixed(value, 2); }
    function format(value, currency = 'USD') {
        const source = String(currency || 'USD').toUpperCase();
        if (source === 'USD') return code() === 'IRT' ? amount(value) + ' تومان' : '$' + amount(value);
        return payment(value, source);
    }
    // Pay amounts have already been converted by the gateway. Never convert
    // IRT, IRR, TRX, USDT, BTC etc. again using the USD display rate.
    function payment(value, currency = 'USD') {
        const source = String(currency || 'USD').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 12);
        if (source === 'USD') return '$' + fixed(value, 2);
        if (source === 'IRT') return fixed(value, 0) + ' تومان';
        if (source === 'IRR') return fixed(value, 0) + ' ریال';
        return fixed(value, 8).replace(/\.?0+$/, '') + ' ' + source;
    }
    function preset(value) { return code() === 'IRT' ? String(toman(value)) : String(value); }
    function input(raw, currency) {
        if (!['USD','IRT'].includes(currency)) throw new Error('واحد مبلغ مشخص نیست؛ فرم شارژ را دوباره باز کنید.');
        const text = String(raw).trim()
            .replace(/[۰-۹]/g, d => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(d)))
            .replace(/[٠-٩]/g, d => String('٠١٢٣٤٥٦٧٨٩'.indexOf(d)))
            .replace(/[,٬،]/g, '').replace(/٫/g, '.');
        if (!(currency === 'IRT' ? /^\d{1,18}$/ : /^\d{1,18}(?:\.\d{1,8})?$/).test(text) || decimal(text).n <= 0n) throw new Error('مبلغ شارژ معتبر نیست.');
        return text;
    }
    return { policy, code, unit, amount, format, payment, preset, input };
})();
