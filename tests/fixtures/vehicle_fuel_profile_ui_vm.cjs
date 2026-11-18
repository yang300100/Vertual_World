/* 哥哥，使用真实前端函数与隔离模拟接口验证申请和审核。 */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const root=path.resolve(__dirname,'../..');
const source=fs.readFileSync(path.join(root,'world_engine/web/vehicle-fuel-profile.js'),'utf8');
const app=fs.readFileSync(path.join(root,'world_engine/web/app.js'),'utf8');
const helper=app.slice(app.indexOf('function fuelProfileProposalDetails('),app.indexOf('function renderRegistrations()'));

function environment(store=new Map()) {
  class Element {
    constructor(tag){Object.assign(this,{tag,children:[],handlers:{},value:'',disabled:false,hidden:false,textContent:'',attributes:{}});}
    append(...nodes){for(const n of nodes){this.children.push(n);if(this.tag==='select'&&this.children.length===1)this.value=n.value;}}
    replaceChildren(...nodes){this.children=[];this.value='';this.append(...nodes);}
    setAttribute(k,v){this.attributes[k]=v;}
    addEventListener(type,fn){(this.handlers[type] ||= []).push(fn);}
    reportValidity(){return true;}
    async fire(type){if(!this.disabled) await Promise.all((this.handlers[type]||[]).map(f=>f({preventDefault(){}})));}
  }
  const section=new Element('section'), state={worldId:'A'}, writes=[];
  let mode='ok', serial=0;
  const data={vehicles:[{id:'car',name:'小车',location_name:'车站',sources:[{id:'source',occurred_at:'2030-01-01',location_id:'loc'}]}],item_types:[{id:'fuel',name:'油'}]};
  const storage={getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)};
  const api=async (url,options={})=>{
    if(options.method==='POST'){
      const body=JSON.parse(options.body);writes.push({url,body});
      if(mode==='lost')throw Error('网络断开');
      if(mode==='rejected')throw Object.assign(Error('状态变更'),{status:409});
      return {id:'proposal',element_type:'vehicle_fuel_profile',status:'proposed'};
    }
    return data;
  };
  const sandbox={document:{querySelector:()=>section,createElement:tag=>new Element(tag)},state,sessionStorage:storage,api,
    crypto:{randomUUID:()=>`00000000-0000-4000-8000-${String(++serial).padStart(12,'0')}`},
    escapeHtml:value=>String(value??'').replace(/[&<>'"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]))};
  vm.createContext(sandbox);vm.runInContext(source,sandbox);vm.runInContext(helper,sandbox);
  const run=s=>vm.runInContext(s,sandbox);
  async function load(){await run('fuelProfileLoad').fire('click');}
  async function fill(){run('fuelProfileVehicle').value='car';await run('fuelProfileVehicle').fire('change');run('fuelProfileItem').value='fuel';run('fuelProfileSource').value='source';run('fuelProfileCapacity').value='12';run('fuelProfileNumerator').value='3';run('fuelProfileDenominator').value='10';}
  return {state,store,writes,run,load,fill,mode:value=>{mode=value;},submit:()=>run('fuelProfileForm').fire('submit')};
}

async function main(){
  const a=environment(); await a.load(); await a.fill(); await a.submit();
  assert.equal(a.writes.length,1); assert.equal(a.store.size,0);
  assert.deepEqual(a.writes[0].body.payload,{element_type:'vehicle_fuel_profile',vehicle_id:'car',item_type_id:'fuel',capacity_units:12,rate_numerator:3,rate_denominator:10});
  assert.equal(a.writes[0].body.source_event_id,'source');
  const b=environment();await b.load();await b.fill();b.run('fuelProfileSource').value='forged';await b.submit();assert.equal(b.writes.length,0);
  b.run('fuelProfileSource').value='source';b.run('fuelProfileDenominator').value='1.5';await b.submit();assert.equal(b.writes.length,0);
  const c=environment();await c.load();await c.fill();c.mode('lost');await c.submit();assert.equal(c.store.size,1);
  const original=c.writes[0].body;c.mode('ok');await c.run('fuelProfileRetry').fire('click');assert.deepEqual(c.writes[1].body,original);assert.equal(c.store.size,0);
  const d=environment();await d.load();await d.fill();d.mode('rejected');await d.submit();assert.equal(d.store.size,0);assert.equal(d.run('fuelProfileCapacity').value,'12');
  const e=environment();await e.load();await e.fill();e.state.worldId='B';await e.submit();assert.equal(e.writes.length,0);
  const detail=a.run('fuelProfileProposalDetails')({source_event_id:'<source>',payload:{vehicle_id:'<car>',item_type_id:'fuel',capacity_units:12,rate_numerator:3,rate_denominator:10}});
  for(const expected of ['&lt;source&gt;','&lt;car&gt;','fuel','12','3','10'])assert(detail.includes(expected));
  assert(!detail.includes('<source>'));assert(!detail.includes('<car>'));
  assert(app.includes('item.element_type === "vehicle_fuel_profile" ? fuelProfileProposalDetails(item)'));
  process.stdout.write(JSON.stringify({status:'verified',cases:6,method:'actual_UI_VM_mocked_API_no_browser'})+'\n');
}
main().catch(e=>{console.error(e);process.exit(1);});
