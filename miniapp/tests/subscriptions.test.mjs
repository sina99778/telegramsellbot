import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';

const rules = vm.runInNewContext(readFileSync(new URL('../js/subscriptions.js', import.meta.url),'utf8') + '\nSubscriptionUI;');
const now = Date.parse('2026-10-15T12:00:00Z');
const active = {status:'active', volume_bytes:1000, used_bytes:200, ends_at:'2026-10-20T12:00:00Z'};

test('rounded 100% does not expire an available service', () => {
    const state = rules.health({...active,used_bytes:996},now);
    assert.equal(state.pct,100);
    assert.equal(state.isExpired,false);
    assert.equal(state.remainingBytes,4);
    assert.equal(rules.health({...active,used_bytes:1000},now).isExpired,true);
});
test('unlimited data and time remain distinct from zero remaining', () => {
    const state = rules.health({...active,volume_bytes:0,used_bytes:2000,ends_at:null},now);
    assert.equal(state.unlimited,true);
    assert.equal(state.remainingBytes,null);
    assert.equal(state.isExpired,false);
    assert.equal(state.daysLabel,'بدون انقضای زمانی');
    assert.equal(rules.health({...active,status:'pending_activation',ends_at:null},now).daysLabel,'با اولین اتصال');
});
test('pending sync has a warning tone even for an expired desired state', () => {
    const state = rules.health({...active,status:'expired',panel_sync_pending:true},now);
    assert.equal(state.pending,true);
    assert.equal(state.statusClass,'pending');
    assert.equal(state.needsAttention,true);
});
test('expiry, disabled access, exhausted data and upcoming limits require attention', () => {
    for (const sub of [{...active,ends_at:'2026-10-14T00:00:00Z'},{...active,status:'disabled'},{...active,used_bytes:1000}]) {
        const state = rules.health(sub,now);
        assert.equal(state.isExpired,true);
        assert.equal(state.needsAttention,true);
    }
    assert.equal(rules.health({...active,used_bytes:910},now).needsAttention,true);
    assert.equal(rules.health({...active,ends_at:'2026-10-17T12:00:00Z'},now).needsAttention,true);
});
test('search normalizes Persian/Arabic forms and excludes names outside the query', () => {
    const subs = [{...active,ends_at:null,config_name:'علي كاري'}, {...active,ends_at:null,config_name:'Work-01',panel_sync_pending:true}];
    assert.equal(rules.filter(subs,'علی کاری').length,1);
    assert.equal(rules.filter(subs,'work','active').length,0);
    assert.equal(rules.filter(subs,'WORK','attention').length,1);
    assert.equal(rules.filter(subs,'missing').length,0);
});
