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
    const API={
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
    const context={API,UI,window:{},document:{getElementById:el,querySelectorAll:()=>[],querySelector:selector=>selector==='.renewal-tab.active'?{dataset:{renewType:'time'}}:null},URL,Map,Date,console};
    const pages=vm.runInNewContext(source('subscriptions')+'\n'+source('pages')+'\nPages;',context);
    return {pages,API,el,dashboard,quotes,notices,get modal(){return modal;},get writes(){return writes;}};
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
