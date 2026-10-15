import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const source = readFileSync(new URL('../js/api.js',import.meta.url),'utf8');
function client(fetch, extra={}) {
    const context = {window:{location:{hash:'',search:''},Telegram:{WebApp:{initData:'test-init-data'}}},sessionStorage:{setItem(){},getItem(){return '';}},URLSearchParams,AbortController,fetch,setTimeout,clearTimeout,...extra};
    return vm.runInNewContext(source+'\nAPI;',context);
}
test('read authentication remains in headers and JSON is parsed', async () => {
    let requested;
    const api = client(async (url,options) => {
        requested = {url,options};
        return {ok:true,status:200,text:async ()=>' {"first_name":"preview"} '};
    });
    assert.equal((await api.getDashboard()).first_name,'preview');
    assert.equal(requested.url,'/api/miniapp/me');
    assert.equal(requested.options.headers['X-Telegram-Init-Data'],'test-init-data');
});
test('a stalled read body times out and is not retried', async () => {
    let expire, started, count=0, cleared=false;
    const bodyStarted = new Promise(resolve => {started=resolve;});
    const api = client(async (_,options) => {
        count++;
        return {ok:true,status:200,text:() => new Promise((_,reject) => {
            options.signal.addEventListener('abort',() => reject(new Error('aborted')));
            started();
        })};
    },{setTimeout:fn=>{expire=fn;return 1;},clearTimeout:()=>{cleared=true;}});
    const pending = api.getDashboard();
    await bodyStarted; expire();
    await assert.rejects(pending,/دریافت اطلاعات طول کشید/);
    assert.equal(count,1); assert.equal(cleared,true);
});
test('financial writes never receive an automatic deadline or replay', async () => {
    let count=0;
    const api = client(async (_,options) => {
        count++; assert.equal(options.signal,undefined);
        return {ok:false,status:409,json:async()=>({detail:'test rejection'})};
    },{setTimeout:()=>{throw new Error('financial timeout must not be scheduled');}});
    await assert.rejects(api.createPurchase({config_name:'test'}),/test rejection/);
    assert.equal(count,1);
});
