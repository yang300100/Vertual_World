/* 哥哥，运行真实前端函数，所有网络仅使用隔离模拟。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=path.resolve(__dirname,'../../world_engine/web/cross-layer.js');
function environment(shared=new Map()) {
  const roots=[],calls=[],writes=[],reads=[],filled=[];
  const descendants=node=>node.children.flatMap(child=>[child,...descendants(child)]);
  function matches(node,selector){if(selector.startsWith('#'))return node.id===selector.slice(1);if(selector.startsWith('.'))return (node.className||'').split(' ').includes(selector.slice(1));return node.tagName===selector;}
  class Element {
    constructor(tag){Object.assign(this,{tagName:tag,children:[],parentNode:null,dataset:{},attributes:{},handlers:{},value:'',textContent:'',disabled:false,hidden:false});}
    get isConnected(){return roots.includes(this)||Boolean(this.parentNode?.isConnected);}
    append(...nodes){for(const node of nodes){node.remove();node.parentNode=this;this.children.push(node);if(this.tagName==='select'&&this.children.length===1)this.value=node.value;}}
    replaceChildren(...nodes){this.children.forEach(node=>{node.parentNode=null;});this.children=[];if(this.tagName==='select')this.value='';this.append(...nodes);}
    setAttribute(name,value){this.attributes[name]=value;}
    addEventListener(type,callback){(this.handlers[type]||=[]).push(callback);}
    querySelector(selector){return descendants(this).find(node=>matches(node,selector))||null;}
    after(node){node.remove();if(this.parentNode){node.parentNode=this.parentNode;this.parentNode.children.splice(this.parentNode.children.indexOf(this)+1,0,node);}else roots.splice(roots.indexOf(this)+1,0,node);}
    remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(node=>node!==this);else{const i=roots.indexOf(this);if(i>=0)roots.splice(i,1);}this.parentNode=null;}
    async fire(type){if(this.disabled)return;return Promise.all((this.handlers[type]||[]).map(fn=>fn({preventDefault(){}})));}
  }
  const scene=new Element('div');roots.push(scene);
  const document={createElement:tag=>new Element(tag),querySelector:selector=>{const [first,...rest]=selector.split(' ');const node=roots.flatMap(n=>[n,...descendants(n)]).find(n=>n.isConnected&&matches(n,first))||null;return rest.length&&node?node.querySelector(rest.join(' ')):node;}};
  const sessionStorage={getItem:key=>shared.get(key)||null,setItem:(key,value)=>shared.set(key,value),removeItem:key=>shared.delete(key)};
  const state={worldId:'A',living:{},busy:false,responsePending:false};let player='player',mode='success',delay=false,resolvePost;
  let options={wonders:[{id:'tower',name:'真实通道',directions:['enter'],people:[{id:'npc',name:'对方'}]}]},rows={passages:[]};
  const api=(url,config={})=>{
    calls.push({url,config});
    if(config.method==='POST'){
      const payload=config.body?JSON.parse(config.body):null;writes.push({url,payload});
      if(mode==='lost')return Promise.reject(Error('断线'));
      if(mode==='defer')return new Promise(resolve=>{resolvePost=resolve;});
      if(mode==='rejected')return Promise.reject(Object.assign(Error('状态变化'),{status:409}));
      if(url.endsWith('/proposals'))return Promise.resolve({id:'proposal',proposal_event_id:'event',status:'pending'});
      if(url.endsWith('/approach'))return Promise.resolve({id:'proposal',status:'pending',approach_event_id:'approach-event',movement_id:'movement'});
      return Promise.resolve({id:'proposal',status:'completed',player_event_id:'actual'});
    }
    if(mode==='read_failed')return Promise.reject(Error('读取失败'));
    if(delay)return new Promise(resolve=>reads.push({url,resolve}));
    return Promise.resolve(JSON.parse(JSON.stringify(url.endsWith('/options')?options:rows)));
  };
  let serial=0;const sandbox={state,currentPlayer:()=>player?{id:player}:null,document,elements:{sceneCharacters:scene},sessionStorage,api,
    crypto:{randomUUID:()=>`00000000-0000-4000-8000-${String(++serial).padStart(12,'0')}`},renderLiving(){},fillIntent:(...args)=>filled.push(args),refreshWorldState:async()=>{},console};
  vm.createContext(sandbox);vm.runInContext(fs.readFileSync(source,'utf8'),sandbox);
  const run=code=>vm.runInContext(code,sandbox),panel=()=>document.querySelector('#cross-layer');
  const field=label=>descendants(panel()).find(node=>node.attributes['aria-label']===label),button=label=>descendants(panel()).find(node=>node.tagName==='button'&&node.textContent===label);
  const text=node=>[node.textContent,...descendants(node).map(n=>n.textContent)].join(' '),flush=()=>new Promise(resolve=>setImmediate(resolve));
  async function open(){await run('renderCrossLayer(true)');}
  async function fill(){field('公共通道').value='tower';await field('公共通道').fire('change');field('商量对象').value='npc';await field('商量对象').fire('change');}
  return {state,calls,writes,reads,shared,filled,panel,field,button,text,flush,open,fill,run,
    submit:()=>panel().querySelector('.cross-layer-form').fire('submit'),setMode:v=>{mode=v;},delay:v=>{delay=v;},setPlayer:v=>{player=v;},
    setOptions:v=>{options=v;},setRows:v=>{rows=v;},finish:v=>resolvePost(v)};
}
async function invitationIsPlayerOnly(){const e=environment();await e.open();await e.fill();await e.submit();assert.equal(e.writes.length,1);assert.equal(e.writes[0].payload.npc_id,'npc');assert.equal(e.writes[0].payload.direction,'enter');assert.match(e.writes[0].payload.idempotency_key,/^passage:/);assert.equal(e.filled.length,0);}
async function pendingAskOnlyFillsDraft(){const e=environment();e.setRows({passages:[{id:'p',status:'pending',npc_id:'npc',npc_name:'对方',wonder_name:'真实通道',direction:'enter',can_talk:true}]});await e.open();await e.button('与对方商量').fire('click');assert.equal(e.filled.length,1);assert.equal(e.filled[0][1],'npc');assert.equal(e.writes.length,0);}
async function playerPassIsSeparate(){const e=environment();e.setRows({passages:[{id:'p',status:'npc_passed',npc_id:'npc',direction:'enter',player_at_node:true}]});await e.open();await e.button('由我本人通过通道').fire('click');assert.equal(e.writes.length,1);assert(e.writes[0].url.endsWith('/p/traverse'));assert.equal(e.writes[0].payload,null);}
async function approachIsPersonalAndNeverMovesNpc(){const e=environment();e.setRows({passages:[{id:'p',status:'pending',npc_id:'npc',direction:'enter',player_can_approach:true}]});await e.open();await e.button('由我本人走到通道节点').fire('click');assert.equal(e.writes.length,1);assert(e.writes[0].url.endsWith('/p/approach'));assert.match(e.writes[0].payload.idempotency_key,/^passage-approach:/);assert.deepEqual(Object.keys(e.writes[0].payload),['idempotency_key']);assert.equal(e.filled.length,0);}
async function approachWorksAfterNpcPassed(){const e=environment();e.setRows({passages:[{id:'p',status:'npc_passed',npc_id:'npc',direction:'enter',player_can_approach:true}]});await e.open();assert(e.button('由我本人通过通道').disabled);await e.button('由我本人走到通道节点').fire('click');assert(e.writes[0].url.endsWith('/p/approach'));assert(!e.writes.some(w=>w.url.endsWith('/traverse')));}
async function lostApproachReusesOriginalRequestAfterReload(){const shared=new Map(),e=environment(shared),rows={passages:[{id:'p',status:'pending',npc_id:'npc',direction:'enter',player_can_approach:true}]};e.setRows(rows);await e.open();e.setMode('lost');await e.button('由我本人走到通道节点').fire('click');assert.equal(shared.size,1);const original=e.writes[0].payload;const next=environment(shared);next.setRows({passages:[{...rows.passages[0],player_can_approach:false,expired:true}]});await next.open();await next.button('核对上次走向节点的请求').fire('click');assert.deepEqual(next.writes[0].payload,original);assert.equal(shared.size,0);}
async function lostReplyRetainsOriginalKeyEvenWhenReadFails(){const shared=new Map(),e=environment(shared);await e.open();await e.fill();e.setMode('lost');await e.submit();assert.equal(shared.size,1);const first=e.writes[0].payload;const next=environment(shared);next.setMode('read_failed');await next.open();assert(!next.button('用原提议核对结果').disabled);next.setMode('success');await next.button('用原提议核对结果').fire('click');assert.deepEqual(next.writes[0].payload,first);assert.equal(shared.size,0);}
async function worldSwitchCannotShowOldReply(){const e=environment();e.delay(true);const first=e.run('renderCrossLayer(true)');await e.flush();e.state.worldId='B';e.setPlayer('playerB');const second=e.run('renderCrossLayer(true)');await e.flush();for(const x of e.reads.slice(2))x.resolve(x.url.endsWith('/options')?{wonders:[]}:{passages:[]});await second;for(const x of e.reads.slice(0,2))x.resolve(x.url.endsWith('/options')?{wonders:[{id:'secret',name:'旧世界',directions:[],people:[]}]}:{passages:[]});await first;assert(!e.text(e.panel()).includes('旧世界'));}
(async()=>{const cases=[invitationIsPlayerOnly,pendingAskOnlyFillsDraft,playerPassIsSeparate,approachIsPersonalAndNeverMovesNpc,approachWorksAfterNpcPassed,lostApproachReusesOriginalRequestAfterReload,lostReplyRetainsOriginalKeyEvenWhenReadFails,worldSwitchCannotShowOldReply];for(const test of cases)await test();console.log(JSON.stringify({status:'verified',cases:cases.length,method:'actual_UI_VM_mocked_API_no_browser'}));})().catch(error=>{console.error(error.stack);process.exitCode=1;});
