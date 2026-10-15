import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';
import assert from 'node:assert/strict';
const source=readFileSync(new URL('../js/money.js',import.meta.url),'utf8');
function money(mode='IRT',rate=100000) {
    return vm.runInNewContext(source+'\nMoney;',{window:{AppConfig:{display_currency:mode,toman_rate:rate}}});
}
test('Toman conversion uses exact decimal ledger values and configured rate',()=>{
    assert.equal(money('IRT',95000).format('2.50'),'237,500 تومان');
    assert.equal(money().format('0.00029'),'29 تومان');
    assert.equal(money().format('0'),'0 تومان');
    assert.equal(money().format('-1.25'),'-125,000 تومان');
    assert.equal(money().format('1234567890.12345678'),'123,456,789,012,345 تومان');
});
test('USD mode retains USD prices',()=>{
    assert.equal(money('USD').format('2.50'),'$2.50');
    assert.equal(money('USD').preset(5),'5');
});
test('actual gateway pay amounts are never converted a second time',()=>{
    const m=money();
    assert.equal(m.payment('250005','IRT'),'250,005 تومان');
    assert.equal(m.payment('2500050','IRR'),'2,500,050 ریال');
    assert.equal(m.payment('12.345678','TRX'),'12.345678 TRX');
    assert.equal(m.payment('0.00000001','BTC'),'0.00000001 BTC');
    assert.equal(m.payment('2.50','USD'),'$2.50');
});
test('money input accepts Persian/grouped numbers and rejects invalid units and amounts',()=>{
    const m=money();
    assert.equal(m.input('۵۰۰٬۰۰۰','IRT'),'500000');
    assert.equal(m.input('١٢٫٥٠','USD'),'12.50');
    assert.throws(()=>m.input('0','IRT'));
    assert.throws(()=>m.input('-20','IRT'));
    assert.throws(()=>m.input('1.5','IRT'));
    assert.throws(()=>m.input('1e6','IRT'));
    assert.throws(()=>m.input('500000',undefined));
});
test('unknown policies and invalid conversion rates fail visibly',()=>{
    assert.throws(()=>money('IRT',0).format('5'),/نرخ/);
    assert.throws(()=>money('IRT',-1).preset(5),/نرخ/);
    assert.throws(()=>money('unknown').format('5'),/واحد/);
});
