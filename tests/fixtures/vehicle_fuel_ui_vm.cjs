/* 哥哥，真实前端函数在隔离内存页面执行，接口均为受控模拟。 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = path.resolve(__dirname, '../../world_engine/web/vehicle-fuel.js');

function environment(sharedStorage = new Map()) {
  const roots = [], calls = [], reads = [], writes = [];
  const descendants = node => node.children.flatMap(child => [child, ...descendants(child)]);
  function matches(node, selector) {
    if (selector.startsWith('#')) return node.id === selector.slice(1);
    if (selector.startsWith('.')) return (node.className || '').split(' ').includes(selector.slice(1));
    return node.tagName === selector;
  }
  class Element {
    constructor(tag) { Object.assign(this, {tagName:tag, children:[], parentNode:null, dataset:{}, attributes:{}, handlers:{}, value:'', textContent:'', disabled:false, hidden:false}); }
    get isConnected() { return roots.includes(this) || Boolean(this.parentNode?.isConnected); }
    append(...nodes) { for (const node of nodes) { node.remove(); node.parentNode=this; this.children.push(node); if(this.tagName==='select' && this.children.length===1)this.value=node.value; } }
    replaceChildren(...nodes) { this.children.forEach(node=>{node.parentNode=null;}); this.children=[]; if(this.tagName==='select')this.value=''; this.append(...nodes); }
    setAttribute(name,value) { this.attributes[name]=value; }
    addEventListener(type,callback) { (this.handlers[type] ||= []).push(callback); }
    querySelector(selector) { return descendants(this).find(node=>matches(node,selector)) || null; }
    after(node) { node.remove(); if(this.parentNode){ node.parentNode=this.parentNode;this.parentNode.children.splice(this.parentNode.children.indexOf(this)+1,0,node); } else roots.splice(roots.indexOf(this)+1,0,node); }
    remove() { if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(node=>node!==this);else{const i=roots.indexOf(this);if(i>=0)roots.splice(i,1);}this.parentNode=null; }
    reportValidity() { return descendants(this).every(node=>node.disabled || !node.required || String(node.value).trim()); }
    async fire(type) { if(this.disabled)return; return Promise.all((this.handlers[type] || []).map(fn=>fn({preventDefault(){}}))); }
  }
  const scene = new Element('div'); roots.push(scene);
  const state = {worldId:'A', living:{}, busy:false, responsePending:false};
  let player = 'player', denyStorage = false, mode = 'success', delayedReads = false, resolveWrite;
  let data = {vehicles:[{vehicle_id:'car',name:'真实载具',capacity_units:10,loaded_units:2,
    loadable:[{id:'fuel',name:'本人燃料',max_quantity:5}], unloadable:[{id:'tank',name:'本人燃料',max_quantity:2}]}]};
  const document = {createElement:tag=>new Element(tag), querySelector:selector=>roots.flatMap(n=>[n,...descendants(n)]).find(n=>n.isConnected&&matches(n,selector))||null};
  const sessionStorage = {getItem:key=>{if(denyStorage)throw Error('storage');return sharedStorage.get(key)||null;},
    setItem:(key,value)=>{if(denyStorage)throw Error('storage');sharedStorage.set(key,value);},
    removeItem:key=>{if(denyStorage)throw Error('storage');sharedStorage.delete(key);}};
  const api = (url, options={}) => {
    calls.push({url,options});
    if(options.method==='POST') {
      const payload=JSON.parse(options.body);writes.push(payload);
      if(mode==='defer')return new Promise(resolve=>{resolveWrite=resolve;});
      if(mode==='lost')return Promise.reject(Error('断线'));
      if(mode==='rejected')return Promise.reject(Object.assign(Error('状态已变化'),{status:409}));
      return Promise.resolve({status:mode==='already'?'already':'applied', event_id:'receipt', summary:'本人装卸已记录'});
    }
    if(mode==='options_failed')return Promise.reject(Error('读取失败'));
    if(delayedReads)return new Promise(resolve=>reads.push({url, resolve}));
    return Promise.resolve(JSON.parse(JSON.stringify(data)));
  };
  let serial=0;
  const sandbox={state,currentPlayer:()=>player?{id:player}:null,document,elements:{sceneCharacters:scene},sessionStorage,api,
    crypto:{randomUUID:()=>`00000000-0000-4000-8000-${String(++serial).padStart(12,'0')}`}, renderLiving(){},console};
  vm.createContext(sandbox); vm.runInContext(fs.readFileSync(source,'utf8'),sandbox);
  const run=code=>vm.runInContext(code,sandbox);
  const panel=()=>document.querySelector('#vehicle-fuel');
  const field=label=>descendants(panel()).find(node=>node.attributes['aria-label']===label);
  const button=label=>descendants(panel()).find(node=>node.tagName==='button'&&node.textContent===label);
  const text=node=>[node.textContent,...descendants(node).map(n=>n.textContent)].join(' ');
  const flush=()=>new Promise(resolve=>setImmediate(resolve));
  async function open(){ await run('renderVehicleFuel(true)'); }
  async function fill(op='load', car='car', iid='fuel', quantity='2'){
    field('选择载具').value=car;await field('选择载具').fire('change');
    field('装入或取回').value=op;await field('装入或取回').fire('change');
    field('本人燃料批次').value=iid;await field('本人燃料批次').fire('change');
    field('数量').value=quantity;await field('数量').fire('input');
  }
  return {state,roots,calls,reads,writes,sharedStorage,panel,field,button,text,flush,open,fill,run,document,
    submit:()=>panel().querySelector('.vehicle-fuel-form').fire('submit'),
    setData:v=>{data=v;},setMode:v=>{mode=v;},delayReads:v=>{delayedReads=v;},setPlayer:v=>{player=v;},
    denyStorage:v=>{denyStorage=v;},finishWrite:v=>resolveWrite(v)};
}

async function candidatesAndLoad() {
  const e=environment();await e.open();await e.fill();
  assert(e.text(e.panel()).includes('当前尚不能消耗燃料行驶'));
  await e.submit();assert.equal(e.writes.length,1);
  assert.deepEqual({...e.writes[0],idempotency_key:'key'}, {operation:'load',vehicle_id:'car',item_id:'fuel',quantity:2,idempotency_key:'key'});
  assert.match(e.writes[0].idempotency_key,/^fuel:/);assert.equal(e.sharedStorage.size,0);
}
async function unloadAndNoAutomaticSelection() {
  const e=environment();await e.open();await e.submit();assert.equal(e.writes.length,0);
  await e.fill('unload','car','tank','1');await e.submit();assert.equal(e.writes[0].operation,'unload');assert.equal(e.writes[0].item_id,'tank');
}
async function emptyAndInvalidCannotSubmit() {
  const e=environment();await e.open();await e.fill('load','car','fuel','9');await e.submit();assert.equal(e.writes.length,0);
  await e.fill('load','car','someone-else','1');await e.submit();assert.equal(e.writes.length,0);
  e.setData({vehicles:[]});await e.open();assert(e.button('确认本人装卸').disabled);
}
async function definitiveFailurePreservesDraft() {
  const e=environment();await e.open();await e.fill('load','car','fuel','3');e.setMode('rejected');await e.submit();
  assert(e.text(e.panel()).includes('草稿已保留'));assert.equal(e.field('数量').value,'3');assert.equal(e.sharedStorage.size,0);
}
async function lostResponseRetryUsesSameKeyEvenAfterOptionsChange() {
  const e=environment();await e.open();await e.fill();e.setMode('lost');await e.submit();assert.equal(e.sharedStorage.size,1);
  const first=e.writes[0];e.setData({vehicles:[]});e.setMode('already');await e.open();
  assert(!e.button('用原请求核对结果').disabled);await e.button('用原请求核对结果').fire('click');
  assert.deepEqual(e.writes[1],first);assert.equal(e.sharedStorage.size,0);
}
async function pageReloadAndFailedReadStillAllowsOriginalRetry() {
  const storage=new Map(), e=environment(storage);await e.open();await e.fill();e.setMode('lost');await e.submit();
  const reloaded=environment(storage);reloaded.setMode('options_failed');await reloaded.open();
  assert(!reloaded.button('用原请求核对结果').disabled);reloaded.setMode('already');await reloaded.button('用原请求核对结果').fire('click');
  assert.deepEqual(reloaded.writes[0],e.writes[0]);assert.equal(storage.size,0);
}
async function duplicateClickAndWorldSwitchCannotReplayOrMutateOtherWorld() {
  const e=environment();await e.open();await e.fill();e.setMode('defer');const form=e.panel().querySelector('.vehicle-fuel-form');
  const first=form.fire('submit');await e.flush();await form.fire('submit');assert.equal(e.writes.length,1);
  e.state.worldId='B';e.setPlayer('playerB');e.setData({vehicles:[]});e.setMode('success');await e.open();
  e.finishWrite({status:'applied',event_id:'receipt',summary:'A完成'});await first;
  assert(!e.text(e.panel()).includes('A完成'));assert.equal(e.state.worldId,'B');assert.equal(e.sharedStorage.size,0);
}
async function invalidStorageOrStorageFailurePreventsNewRequest() {
  const storage=new Map([['virtual-world:fuel-pending:A:player','{损坏']]), e=environment(storage);await e.open();await e.submit();
  assert.equal(e.writes.length,0);assert(e.text(e.panel()).includes('不可读取'));
  const x=environment();await x.open();await x.fill();x.denyStorage(true);await x.submit();assert.equal(x.writes.length,0);
}
async function failedOldReadCannotOverwriteNewWorld() {
  const e=environment();e.delayReads(true);const old=e.run('renderVehicleFuel(true)');await e.flush();
  e.state.worldId='B';e.setPlayer('playerB');const newer=e.run('renderVehicleFuel(true)');await e.flush();
  e.reads[1].resolve({vehicles:[]});await newer;e.reads[0].resolve({vehicles:[{vehicle_id:'secret',name:'旧世界',loadable:[],unloadable:[]}]});await old;
  assert(!e.text(e.panel()).includes('旧世界'));assert.equal(e.state.worldId,'B');
}

(async()=>{const cases=[candidatesAndLoad,unloadAndNoAutomaticSelection,emptyAndInvalidCannotSubmit,definitiveFailurePreservesDraft,
  lostResponseRetryUsesSameKeyEvenAfterOptionsChange,pageReloadAndFailedReadStillAllowsOriginalRetry,
  duplicateClickAndWorldSwitchCannotReplayOrMutateOtherWorld,invalidStorageOrStorageFailurePreventsNewRequest,failedOldReadCannotOverwriteNewWorld];
  for(const test of cases){await test();process.stdout.write('通过 '+test.name+'\n');}
  process.stdout.write(JSON.stringify({status:'verified',cases:cases.length,method:'actual_UI_VM_mocked_API_no_browser'})+'\n');
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
