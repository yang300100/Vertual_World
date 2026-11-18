/* 哥哥，执行真实约定前端脚本，接口只用隔离模拟。 */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.resolve(__dirname,'../../world_engine/web/appointments.js'),'utf8');

function environment(){
  const roots=[],calls=[],writes=[],deferred=[];
  const descend=node=>node.children.flatMap(child=>[child,...descend(child)]);
  const matches=(node,selector)=>selector.startsWith('#')?node.id===selector.slice(1):selector.startsWith('.')?(node.className||'').split(' ').includes(selector.slice(1)):node.tagName===selector;
  class Element {
    constructor(tag){Object.assign(this,{tagName:tag,children:[],parentNode:null,handlers:{},attributes:{},_value:'',textContent:'',disabled:false,hidden:false});}
    get isConnected(){return roots.includes(this)||Boolean(this.parentNode?.isConnected);}
    get options(){return this.tagName==='select'?this.children:undefined;}
    get selectedOptions(){return this.tagName==='select'?this.children.filter(child=>child.value===this.value):[];}
    get value(){return this._value;}
    set value(value){this._value=String(value??'');}
    append(...nodes){for(const node of nodes){node.remove();node.parentNode=this;this.children.push(node);if(this.tagName==='select'&&this.children.length===1)this.value=node.value;}}
    replaceChildren(...nodes){for(const node of this.children)node.parentNode=null;this.children=[];this.append(...nodes);}
    remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(node=>node!==this);else{const i=roots.indexOf(this);if(i>=0)roots.splice(i,1);}this.parentNode=null;}
    contains(node){return node===this||descend(this).includes(node);}
    setAttribute(name,value){this.attributes[name]=value;}
    addEventListener(type,fn){(this.handlers[type] ||= []).push(fn);}
    querySelector(selector){return descend(this).find(node=>matches(node,selector))||null;}
    reportValidity(){return true;}
    setCustomValidity(value){this.validationMessage=value;}
    scrollIntoView(){} focus(){}
    async fire(type){if(this.disabled)return;return Promise.all((this.handlers[type]||[]).map(fn=>fn({preventDefault(){}})));}
  }
  const profile=new Element('section'),worldSelect=new Element('select'),dialog=new Element('dialog');roots.push(profile,worldSelect);
  dialog.open=true;dialog.close=()=>{dialog.open=false;for(const fn of dialog.handlers.close||[])fn();};dialog.showModal=()=>{dialog.open=true;};
  const document={createElement:tag=>new Element(tag),querySelector:selector=>roots.flatMap(n=>[n,...descend(n)]).find(n=>n.isConnected&&matches(n,selector))||null};
  const appointment=(status='accepted',other='npc')=>({id:'a1',other_id:other,proposer_id:'player',recipient_id:other,kind:'talk',status,label:'见面',location_id:'loc',location_name:'广场',starts_at:'2040-01-01T10:00:00Z',ends_at:'2040-01-01T11:00:00Z',minutes:15,statement:'原邀请'});
  const state={worldId:'A',living:{world_time:'2040-01-01T09:00:00Z',social_appointments:[appointment()]}};
  let player='player',mode='success',reply=[{...appointment(),own_arrived_at:'2040-01-01T10:02:00Z'}],seriesReply=[],initialSeries=[];
  const api=(url,config={})=>{
    calls.push({url,config});
    if(config.method){writes.push({url,config});return Promise.resolve({});}
    if(url.includes('/appointments/options/')){
      if(mode==='defer_options')return new Promise(resolve=>deferred.push(resolve));
      return Promise.resolve({place_options:[{id:'loc',name:'广场'}],room_options:[],journey_options:[],series:initialSeries});
    }
    if(mode==='fail')return Promise.reject(Error('读取失败'));
    if(mode==='defer')return new Promise(resolve=>deferred.push(resolve));
    return Promise.resolve(JSON.parse(JSON.stringify(url.includes('/appointments/series?')?seriesReply:reply)));
  };
  const elements={characterProfile:profile,worldSelect,characterDialog:dialog};
  const sandbox={document,elements,state,currentPlayer:()=>player?{id:player}:null,api,eventLabels:{},
    characterById:()=>null,canInviteCompanion:()=>false,distanceKm:()=>0,executeNaturalSocialChoice:(...args)=>writes.push(args),
    formatWorldTime:value=>value,openCharacterProfile:async()=>{},openKnownProfile:()=>{},console};
  vm.createContext(sandbox);vm.runInContext(source,sandbox);
  const run=code=>vm.runInContext(code,sandbox), panel=()=>document.querySelector('#appointment-profile');
  const all=()=>[panel(),...descend(panel())],field=label=>all().find(node=>node.attributes['aria-label']===label),
    button=label=>all().find(node=>node.tagName==='button'&&node.textContent===label),text=()=>all().map(node=>node.textContent).join(' ');
  return {state,profile,worldSelect,dialog,calls,writes,deferred,appointment,run,panel,field,button,text,
    open:id=>run(`appendAppointmentProfile(${JSON.stringify(id)})`),mode:value=>{mode=value;},setReply:value=>{reply=value;},setSeries:value=>{seriesReply=value;},setInitialSeries:value=>{initialSeries=value;seriesReply=value;},setPlayer:value=>{player=value;},
    clear:()=>profile.replaceChildren(),flush:()=>new Promise(resolve=>setImmediate(resolve))};
}

async function successPreservesDraft(){
  const e=environment();await e.open('npc');const input=e.field('想向对方说的话');input.value='这份草稿要保留';
  const form=e.panel().querySelector('form');await e.button('刷新约定状态').fire('click');
  assert.equal(e.panel().querySelector('form'),form);assert.equal(e.field('想向对方说的话'),input);assert.equal(input.value,'这份草稿要保留');
  assert(e.text().includes('你曾在 2040-01-01T10:02:00Z 实际到场'));assert(e.text().includes('约定状态已刷新'));
  assert.equal(e.calls.filter(c=>c.url==='/api/worlds/A/appointments?character_id=npc').length,2);assert.equal(e.writes.length,0);
}
async function failurePreservesEverything(){
  const e=environment();await e.open('npc');e.field('想向对方说的话').value='保留草稿';e.mode('fail');await e.button('刷新约定状态').fire('click');
  assert.equal(e.field('想向对方说的话').value,'保留草稿');assert(e.text().includes('暂时无法刷新约定状态'));assert(e.text().includes('双方已确认，等待赴约'));
}
async function replacesStatusAndGuardsStaleSelection(){
  const e=environment();await e.open('npc');const purpose=e.field('这次想商量');purpose.value='reschedule:a1';e.field('想向对方说的话').value='保留文字';
  e.setReply([{...e.appointment('completed'),own_arrived_at:'2040-01-01T10:02:00Z'},{...e.appointment('accepted','other'),id:'foreign'}]);
  await e.button('刷新约定状态').fire('click');assert(e.text().includes('已完成相处'));assert(!e.text().includes('等待赴约'));assert.equal(purpose.value,'reschedule:a1');
  assert(purpose.selectedOptions[0].disabled);assert.equal(e.field('想向对方说的话').value,'保留文字');
  await e.panel().querySelector('form').fire('submit');assert.equal(e.writes.length,0);
}
async function worldRoundTripCannotLeakOldResponse(){
  const e=environment();await e.open('npc');e.field('想向对方说的话').value='原草稿';e.mode('defer');const pending=e.button('刷新约定状态').fire('click');await e.flush();
  e.state.worldId='B';await e.worldSelect.fire('change');e.state.worldId='A';await e.worldSelect.fire('change');
  e.deferred[0]([{...e.appointment('completed'),statement:'旧响应不可显示'}]);e.deferred[1]([]);await pending;
  assert(!e.text().includes('旧响应不可显示'));assert(e.text().includes('双方已确认，等待赴约'));assert.equal(e.writes.length,0);
  assert.equal(e.field('想向对方说的话').value,'原草稿');assert(!e.button('刷新约定状态').disabled);
  e.mode('success');await e.button('刷新约定状态').fire('click');assert(e.text().includes('约定状态已刷新'));assert.equal(e.field('想向对方说的话').value,'原草稿');
}
async function replacedProfileCannotReceiveOldResponse(){
  const e=environment();await e.open('npc');e.mode('defer');const pending=e.button('刷新约定状态').fire('click');await e.flush();
  e.clear();e.setReply([{...e.appointment('pending','npc2'),id:'b2',statement:'新人物'}]);e.mode('success');await e.open('npc2');
  e.deferred[0]([{...e.appointment('completed'),statement:'旧人物'}]);e.deferred[1]([]);await pending;
  assert(e.text().includes('新人物'));assert(!e.text().includes('旧人物'));assert.equal(e.writes.length,0);
}
async function noAutomaticAction(){
  const e=environment();await e.open('npc');await e.button('刷新约定状态').fire('click');assert(e.button('到场后开始约定的相处'));assert.equal(e.writes.length,0);
}
async function initialOptionsDoNotCrossWorldSwitch(){
  const e=environment();e.mode('defer_options');const pending=e.open('npc');await e.flush();e.state.worldId='B';await e.worldSelect.fire('change');e.state.worldId='A';await e.worldSelect.fire('change');
  e.deferred[0]({place_options:[{id:'old',name:'旧世界地点'}],room_options:[],journey_options:[],series:[]});await pending;
  assert(!e.text().includes('旧世界地点'));assert.equal(e.writes.length,0);
}
async function repeatedClicksUseSingleRead(){
  const e=environment();await e.open('npc');e.mode('defer');const first=e.button('刷新约定状态').fire('click');await e.flush();await e.button('刷新约定状态').fire('click');
  assert.equal(e.deferred.length,2);e.deferred[0]([e.appointment('accepted')]);e.deferred[1]([]);await first;assert.equal(e.writes.length,0);
}
async function refreshesOnlyThisPersonsSeries(){
  const e=environment();const cycle={id:'s1',other_id:'npc',status:'active',interval_days:2,occurrence_count:2,completed_count:0,missed_count:0,statement:'周期原话',occurrences:[]};
  e.setInitialSeries([cycle]);await e.open('npc');assert(e.text().includes('周期进行中'));e.field('想向对方说的话').value='保留';
  e.setSeries([{...cycle,status:'paused'}, {...cycle,id:'s2',other_id:'other',statement:'他人周期'}]);await e.button('刷新约定状态').fire('click');
  assert(e.text().includes('后续已暂停'));assert(!e.text().includes('周期进行中'));assert(!e.text().includes('他人周期'));assert.equal(e.field('想向对方说的话').value,'保留');
}
async function closedDialogDiscardsOldResponse(){
  const e=environment();await e.open('npc');e.mode('defer');const pending=e.button('刷新约定状态').fire('click');await e.flush();
  e.dialog.close();e.deferred[0]([{...e.appointment('completed'),statement:'关闭前的迟到响应'}]);e.deferred[1]([]);await pending;
  assert(!e.text().includes('关闭前的迟到响应'));assert(e.text().includes('双方已确认，等待赴约'));
  e.clear();e.dialog.showModal();e.mode('success');await e.open('npc');assert(!e.text().includes('关闭前的迟到响应'));assert(e.button('刷新约定状态'));assert.equal(e.writes.length,0);
}
async function requestsAreScopedBeforeServerLimit(){
  const e=environment();await e.open('npc');assert(e.text().includes('双方已确认，等待赴约'));await e.button('刷新约定状态').fire('click');
  const calls=e.calls.filter(c=>c.url.includes('/appointments?')||c.url.includes('/appointments/series?'));
  assert.equal(calls.length,4);assert(calls.every(c=>c.url.endsWith('?character_id=npc')));assert.equal(e.writes.length,0);
}
(async()=>{const cases=[successPreservesDraft,failurePreservesEverything,replacesStatusAndGuardsStaleSelection,worldRoundTripCannotLeakOldResponse,replacedProfileCannotReceiveOldResponse,noAutomaticAction,initialOptionsDoNotCrossWorldSwitch,repeatedClicksUseSingleRead,refreshesOnlyThisPersonsSeries,closedDialogDiscardsOldResponse,requestsAreScopedBeforeServerLimit];for(const test of cases)await test();console.log(JSON.stringify({status:'verified',cases:cases.length,method:'actual_UI_VM_mocked_API_no_browser'}));})().catch(error=>{console.error(error.stack);process.exitCode=1;});
