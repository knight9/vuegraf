// Standalone fake DOM/clock test, not an alteration of the user's browser.
const fs = require('node:fs'), vm = require('node:vm'), assert = require('node:assert/strict');
let now = 1_000_000, fetches = 0, sequence = 0;
const timers = new Map(), elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {value:'',textContent:'',selectedOptions:[],handlers:{},
    addEventListener(name, fn) { this.handlers[name] = fn; }, replaceChildren(){}, append(){}, click(){}});
  return elements.get(id);
}
class Clock extends Date { constructor(...args) { super(...(args.length ? args : [now])); } static now() { return now; } }
const context = {
  Date: Clock, console, URL, setTimeout(fn) { const id = ++sequence; timers.set(id,fn); return id; },
  clearTimeout(id) { timers.delete(id); },
  document:{getElementById:element,addEventListener(){},createElement:()=>element('new')},
  fetch:async()=>{ fetches++; return {ok:true,json:async()=>({jobs:{},circuits:[],ready:true,storage:{},recovery:{}})}; }
};
vm.createContext(context);
vm.runInContext(fs.readFileSync('src/vuegraf/web/admin.js','utf8'), context);
assert.equal(vm.runInContext('duration(0)', context), '0 seconds');
assert.equal(vm.runInContext('duration(63072000)', context), '2 years');
const flush = () => new Promise(resolve => setImmediate(resolve));
(async()=>{
  await flush(); assert.equal(fetches,1); assert.equal(timers.size,1);
  now += 300001;
  const callback = [...timers.values()][0]; timers.clear(); callback(); await flush();
  assert.equal(fetches,1); assert.match(element('polling').textContent,/paused/); assert.equal(timers.size,0);
  element('check').handlers.click(); await flush();
  assert.equal(fetches,2); assert.match(element('polling').textContent,/active/); assert.equal(timers.size,1);
  vm.runInContext('poll(); poll();',context); await flush();
  assert.equal(fetches,3); assert.equal(timers.size,1);
  console.log('PASS: one-second polling, five-minute inactivity pause, Check now resume, no overlapping/duplicate polling');
})().catch(error=>{console.error(error);process.exitCode=1;});
