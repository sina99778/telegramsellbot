import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const source = name => readFileSync(new URL(`../js/${name}.js`,import.meta.url),'utf8');
const subId='00000000-0000-4000-8000-000000000001';

function harness() {
    const elements = new Map();
    const el = id => {
        if (!elements.has(id)) elements.set(id,{innerHTML:'',textContent:'',value:'',dataset:{},style:{},isConnected:true,classList:{toggle(){},contains(){return false;}},listeners:{},addEventListener(name,fn){this.listeners[name]=fn;}});
        return elements.get(id);
    };
    const dashboard = {first_name:'<b>test</b>',wallet:{balance:24.5},active_config_count:1,gateways:{},subscriptions:[{id:subId,status:'active',used_bytes:0,volume_bytes:0,ends_at:null,config_name:'safe',plan_name:'<img src=x onerror=alert(1)>'}]};
    const quotes=[]; const notices=[]; let modal=''; let writes=0;
    const config={display_currency:'USD',toman_rate:100000};
    const plans=[{id:subId,name:'test',price:'6',currency:'USD',volume_gb:50,duration_days:30,is_unlimited:true}];
    const API={
        getConfig:async()=>structuredClone(config),
        getPlans:async()=>({plans:structuredClone(plans),sales_enabled:true}),
        getDashboard:async()=>structuredClone(dashboard),
        getConfigs:async()=>({subscriptions:structuredClone(dashboard.subscriptions),total:1,page:1,page_size:20}),
        getPayments:async()=>({payments:[]}),
        getTransactions:async()=>({transactions:[]}),
        getRenewalQuote:payload=>new Promise((resolve,reject)=>quotes.push({payload,resolve,reject})),
        renewConfig:async()=>{writes++;return {invoice_url:'https://example.invalid/invoice'};},
    };
    const UI={icon:()=>'',toPersianDigits:String,formatMoney:n=>Number(n).toFixed(2),formatBytes:n=>n+' B',formatDate:()=>'',getProgressClass:()=>'',loadError:(id,_,message)=>{el(id).innerHTML='ERROR: '+message;},toast:msg=>notices.push(msg),showModal:html=>{
        modal=html;
        el('renewal-amount').value='30';
        el('renewal-price-box').dataset={subscriptionId:subId};
    }};
    const context={API,UI,window:{},setTimeout:()=>0,document:{getElementById:el,querySelectorAll:()=>[],querySelector:selector=>selector==='.renewal-tab.active'?{dataset:{renewType:'time'}}:null},URL,Map,Date,console};
    const pages=vm.runInNewContext(source('money')+'\n'+source('subscriptions')+'\n'+source('pages')+'\nPages;',context);
    return {pages,API,config,el,dashboard,quotes,notices,get modal(){return modal;},get writes(){return writes;}};
}
test('detail escapes plan data and unlimited data is readable',async()=>{
    const h=harness(); await h.pages.load_dashboard(); h.pages.showConfigDetail(subId);
    assert.match(h.modal,/&lt;img src=x onerror=alert\(1\)&gt;/);
    assert.doesNotMatch(h.modal,/<img src=x/);
    assert.match(h.modal,/نامحدود/);
});
test('a refreshed pending marker blocks renewal even after configs were cached',async()=>{
    const h=harness(); await h.pages.load_configs();
    h.dashboard.subscriptions[0].panel_sync_pending=true;
    await h.pages.load_dashboard(); h.pages.showRenewal(subId);
    assert.equal(h.quotes.length,0); assert.match(h.notices.at(-1),/در دسترس نیست/);
});
test('out-of-order renewal quotes cannot replace the latest price or permit premature submission',async()=>{
    const h=harness(); await h.pages.load_dashboard(); h.pages.showRenewal(subId);
    assert.equal(h.quotes.length,1);
    h.el('renewal-amount').value='60'; h.el('renewal-amount').listeners.input();
    await h.pages.submitRenewal(subId,'wallet'); assert.equal(h.writes,0);
    h.quotes[1].resolve({price:'6'}); await new Promise(resolve=>setImmediate(resolve));
    assert.match(h.el('renewal-price-box').innerHTML,/6.00/);
    h.quotes[0].resolve({price:'3'}); await new Promise(resolve=>setImmediate(resolve));
    assert.match(h.el('renewal-price-box').innerHTML,/6.00/);
    await h.pages.submitRenewal(subId,'wallet'); assert.equal(h.writes,1);
});
test('wallet refresh reads current balance and isolates payment history failure',async()=>{
    const h=harness(); await h.pages.load_dashboard();
    h.dashboard.wallet.balance=44.25;
    h.API.getPayments=async()=>{throw new Error('payment history unavailable');};
    await h.pages.load_wallet();
    assert.match(h.el('wallet-card').innerHTML,/44.25/);
    assert.doesNotMatch(h.el('wallet-card').innerHTML,/ERROR/);
    assert.match(h.el('payments-list').innerHTML,/ERROR: payment history unavailable/);
    assert.match(h.el('transactions-list').innerHTML,/هنوز تراکنشی ثبت نشده/);
});
test('Toman settings drive wallet, plan, checkout and renewal prices',async()=>{
    const h=harness(); h.config.display_currency='IRT'; h.config.toman_rate=95000;
    await h.pages.load_dashboard();
    assert.match(h.el('home-summary').innerHTML,/2,327,500/);
    assert.match(h.el('home-summary').innerHTML,/تومان/);
    await h.pages.load_store(); assert.match(h.el('plans-list').innerHTML,/570,000 تومان/);
    assert.doesNotMatch(h.el('plans-list').innerHTML,/USD|\$/);
    h.pages.buyPlan(subId); assert.match(h.modal,/570,000 تومان/);
    h.pages.showRenewal(subId); h.quotes[0].resolve({price:'2.5',currency:'USD'});
    await new Promise(resolve=>setImmediate(resolve));
    assert.match(h.el('renewal-price-box').innerHTML,/237,500 تومان/);
});
test('topup keeps its input currency when display settings change after opening',async()=>{
    const h=harness(); h.config.display_currency='IRT';
    await h.pages.load_dashboard(); h.pages.topupWallet();
    assert.match(h.modal,/data-currency="IRT"/);
    assert.match(h.modal,/value="500000"/);
    h.el('topup-amount').dataset.currency='IRT'; h.el('topup-amount').value='۵۰۰٬۰۰۰';
    h.config.display_currency='USD'; await h.pages.load_dashboard();
    let payload;
    h.API.createTopup=async body=>{payload=body; return {message:'test',invoice_url:'https://example.invalid'};};
    await h.pages.submitTopup('card_to_card');
    assert.equal(payload.amount,'500000'); assert.equal(payload.currency,'IRT');
});
test('an unsuccessful settings read cannot render fallback dollar prices',async()=>{
    const h=harness(); h.API.getConfig=async()=>{throw new Error('settings unavailable');};
    await h.pages.load_store(); assert.match(h.el('plans-list').innerHTML,/ERROR: settings unavailable/);
    assert.doesNotMatch(h.el('plans-list').innerHTML,/\$6/);
});
