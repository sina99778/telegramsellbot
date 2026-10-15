// Explicit, loopback-only fixtures. Never imported by the application/server.
import http from 'node:http';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

if (!process.argv.includes('--fixtures')) throw new Error('Pass --fixtures to enable TEST DATA.');
const root = fileURLToPath(new URL('../', import.meta.url));
const port = Number(process.env.MINIAPP_PREVIEW_PORT || 4177);
const id = n => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const date = days => new Date(Date.now() + days * 86400000).toISOString();
const GB = 1024 ** 3;
const subscriptions = [
    { config_name:'Personal-01', plan_name:'سرویس ماهانه', used_bytes:12.4*GB, volume_bytes:50*GB, ends_at:date(23) },
    { config_name:'Work-02', plan_name:'پلن کاری', used_bytes:46.8*GB, volume_bytes:50*GB, ends_at:date(2) },
    { config_name:'Family-03', plan_name:'خانواده', used_bytes:6*GB, volume_bytes:80*GB, ends_at:date(31), panel_sync_pending:true },
    { config_name:'Unlimited-04', plan_name:'حجم نامحدود', used_bytes:120*GB, volume_bytes:0, ends_at:null },
    { config_name:'Old-05', plan_name:'سرویس قبلی', used_bytes:30*GB, volume_bytes:30*GB, ends_at:date(-2), status:'expired' },
    { config_name:'Travel-06', plan_name:'اولین اتصال', used_bytes:0, volume_bytes:20*GB, ends_at:null, status:'pending_activation' },
].map((s,i) => ({id:id(i+1), status:'active', starts_at:date(-7), sub_link:`https://subscription.example.invalid/sub/demo-${i}`, ...s}));
const plans = [
    {name:'ماهانه · سبک', volume_gb:30, duration_days:30, price:'3.50'},
    {name:'ماهانه · استاندارد', volume_gb:60, duration_days:30, price:'6.00'},
    {name:'سه‌ماهه · پرمصرف', volume_gb:180, duration_days:90, price:'15.00'},
    {name:'بدون محدودیت زمان', volume_gb:100, duration_days:0, price:'12.00'},
].map((p,i) => ({id:id(100+i), currency:'USD', protocol:'VLESS', is_unlimited:true, ...p}));
const wallet = {balance:'24.50', credit_limit:'0', hold_balance:'0'};
const json = (res, value, status=200) => {
    res.writeHead(status, {'Content-Type':'application/json; charset=utf-8', 'Cache-Control':'no-store'});
    res.end(JSON.stringify(value));
};

http.createServer(async (req,res) => {
    const url = new URL(req.url, `http://127.0.0.1:${port}`);
    const scenario = /miniapp_preview=(empty|error|admin)/.exec(req.headers.cookie || '')?.[1] || 'default';
    if (url.pathname.startsWith('/api/miniapp/')) {
        const endpoint = url.pathname.slice('/api/miniapp'.length);
        if (endpoint === '/qr') {
            res.writeHead(200, {'Content-Type':'image/svg+xml', 'Cache-Control':'no-store'});
            return res.end('<svg xmlns="http://www.w3.org/2000/svg" width="220" height="180" viewBox="0 0 220 180"><rect width="220" height="180" fill="#f1f5f9"/><g fill="none" stroke="#64748b" stroke-width="4"><rect x="55" y="32" width="35" height="35"/><rect x="130" y="32" width="35" height="35"/><rect x="55" y="92" width="35" height="35"/></g><text x="110" y="157" text-anchor="middle" fill="#475569" font-family="sans-serif" font-size="13">QR preview only</text></svg>');
        }
        if (endpoint === '/renewal/quote') return json(res, {price:'2.50', currency:'USD', message:'قیمت آزمایشی'});
        if (req.method !== 'GET') return json(res, {detail:'پیش‌نمایش: هیچ سفارش، پرداخت یا پیامی ارسال نمی‌شود.'}, 409);
        if (scenario === 'error' && endpoint !== '/config') return json(res, {detail:'خطای آزمایشی دریافت اطلاعات؛ لطفاً دوباره تلاش کنید.'}, 503);
        const subs = scenario === 'empty' ? [] : subscriptions;
        const routes = {
            '/config': {bot_username:'preview_bot', sales_enabled:true, renewals_enabled:true},
            '/me': {first_name:'سینا', is_admin:scenario==='admin', wallet, subscriptions:subs, active_config_count:subs.length ? 4 : 0, total_volume_used:18*GB, total_volume:150*GB, gateways:{card_to_card:true}, sales_enabled:true, renewals_enabled:true},
            '/configs': {subscriptions:subs, page:1, total:subs.length, page_size:20},
            '/plans': {plans:scenario==='empty' ? [] : plans, sales_enabled:true},
            '/wallet/transactions': {transactions:scenario==='empty' ? [] : [{id:id(201), type:'deposit', direction:'credit', amount:'20', balance_before:'4.50', balance_after:'24.50', created_at:date(-1), description:'شارژ حساب آزمایشی'}]},
            '/payments': {payments:scenario==='empty' ? [] : [{id:id(202),provider:'card_to_card',kind:'wallet_topup',payment_status:'pending_approval',price_amount:'10',price_currency:'USD',created_at:date(-1),updated_at:date(-1)}]},
            '/tickets': {tickets:[]},
            '/referral': {ref_code:'PREVIEW',referral_count:3,total_earned:'1.50',enabled:true},
            '/admin/overview': {total_users:120, active_users:94},
        };
        return Object.hasOwn(routes,endpoint) ? json(res,routes[endpoint]) : json(res,{detail:'این بخش در پیش‌نمایش داده ندارد.'},404);
    }
    if (url.pathname === '/') { res.writeHead(302,{Location:'/miniapp/'}); return res.end(); }
    if (!url.pathname.startsWith('/miniapp/')) { res.writeHead(404); return res.end(); }
    let relative;
    try {relative = decodeURIComponent(url.pathname.slice('/miniapp/'.length)) || 'index.html';}
    catch {res.writeHead(400); return res.end();}
    const filename = path.resolve(root,relative);
    if (!filename.startsWith(root) || !['.html','.css','.js'].includes(path.extname(filename)) || relative.startsWith('dev/')) {res.writeHead(404); return res.end();}
    try {
        let data = await fs.readFile(filename);
        const headers = {'Content-Type':{'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8'}[path.extname(filename)],'Cache-Control':'no-store'};
        if (relative === 'index.html') {
            const mode = ['empty','error','admin'].includes(url.searchParams.get('preview')) ? url.searchParams.get('preview') : 'default';
            headers['Set-Cookie'] = `miniapp_preview=${mode}; HttpOnly; SameSite=Strict; Path=/`;
            data = Buffer.from(data.toString().replace('<body>', '<body><div style="text-align:center;background:#fff7e8;color:#785017;font:11px system-ui;padding:6px">پیش‌نمایش · اطلاعات آزمایشی</div>'));
        }
        res.writeHead(200, headers); res.end(data);
    } catch {res.writeHead(404); res.end();}
}).listen(port,'127.0.0.1',() => console.log(`TEST DATA only: http://127.0.0.1:${port}/miniapp/ (preview=empty|error|admin)`));
