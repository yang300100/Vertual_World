/* 哥哥，编年者提交的燃料规格仍需要在登记清单中另行确认。 */
let fuelProfileWorld = "", fuelProfileData = null, fuelProfileSending = false;
const fuelProfileRoot = document.querySelector("#room-observer-registrations");
const fuelProfileSection = document.createElement("section");
fuelProfileSection.className = "observer-card";
fuelProfileSection.id = "vehicle-fuel-profile-form-section";
function fuelProfileText(parent, value, tag="p") { const node=document.createElement(tag); node.textContent=value; parent.append(node); return node; }
function fuelProfileField(parent, label, tag="input") {
  const box=document.createElement("label"), field=document.createElement(tag);
  box.textContent=label; field.setAttribute("aria-label",label); box.append(field); parent.append(box); return field;
}
function fuelProfileOption(select, value, text) { const option=document.createElement("option"); option.value=value; option.textContent=text; select.append(option); }
fuelProfileText(fuelProfileSection,"登记载具燃料规格","h2");
fuelProfileText(fuelProfileSection,"先读取世界中已核实的车辆、物品和来源；数字由编年者填写。提交后还要在登记清单中核对并确认，并不产生燃料或证明参数经过实测。");
const fuelProfileLoad=document.createElement("button"); fuelProfileLoad.type="button"; fuelProfileLoad.textContent="读取可登记车辆与来源"; fuelProfileLoad.className="button secondary"; fuelProfileSection.append(fuelProfileLoad);
const fuelProfileForm=document.createElement("form"); fuelProfileForm.className="long-term-form"; fuelProfileSection.append(fuelProfileForm);
const fuelProfileVehicle=fuelProfileField(fuelProfileForm,"已核实车辆","select");
const fuelProfileItem=fuelProfileField(fuelProfileForm,"已登记燃料物品类型","select");
const fuelProfileSource=fuelProfileField(fuelProfileForm,"同地点已发生的来源事件","select");
const fuelProfileCapacity=fuelProfileField(fuelProfileForm,"油箱容量（物品份）");
const fuelProfileNumerator=fuelProfileField(fuelProfileForm,"每公里消耗分子（物品份）");
const fuelProfileDenominator=fuelProfileField(fuelProfileForm,"每公里消耗分母");
for(const input of [fuelProfileCapacity,fuelProfileNumerator,fuelProfileDenominator]) { input.type="number"; input.min="1"; input.max="1000000000"; input.step="1"; input.required=true; }
for(const input of [fuelProfileVehicle,fuelProfileItem,fuelProfileSource]) input.required=true;
const fuelProfileSubmit=document.createElement("button"); fuelProfileSubmit.type="submit"; fuelProfileSubmit.className="button primary"; fuelProfileSubmit.textContent="提交规格待审核"; fuelProfileForm.append(fuelProfileSubmit);
const fuelProfileRetry=document.createElement("button"); fuelProfileRetry.type="button"; fuelProfileRetry.className="button secondary"; fuelProfileRetry.textContent="用原请求核对结果"; fuelProfileForm.append(fuelProfileRetry);
const fuelProfileNotice=fuelProfileText(fuelProfileForm,"先读取候选。", "p"); fuelProfileNotice.setAttribute("aria-live","polite");
if(fuelProfileRoot) fuelProfileRoot.append(fuelProfileSection);

function fuelProfileStorageKey(world) { return "virtual-world:fuel-profile-pending:"+encodeURIComponent(world); }
function fuelProfilePending(world) {
  try {
    const raw=sessionStorage.getItem(fuelProfileStorageKey(world));
    if(!raw) return null;
    const data=JSON.parse(raw), p=data?.payload;
    if(data.world===world && p?.payload?.element_type==="vehicle_fuel_profile" && typeof p.source_event_id==="string" &&
       /^fuel-profile:[a-f0-9-]{36}$/i.test(p.idempotency_key) && typeof p.payload.vehicle_id==="string" &&
       typeof p.payload.item_type_id==="string" && [p.payload.capacity_units,p.payload.rate_numerator,p.payload.rate_denominator].every(n=>Number.isSafeInteger(n)&&n>=1&&n<=1000000000)) return data;
  } catch (_) { /* 哥哥，原请求不可读取时不能覆盖或重复提交。 */ }
  return {invalid:true};
}
function fuelProfileStatus() {
  const pending=fuelProfileWorld ? fuelProfilePending(fuelProfileWorld) : null, locked=Boolean(pending||fuelProfileSending);
  for(const field of [fuelProfileVehicle,fuelProfileItem,fuelProfileSource,fuelProfileCapacity,fuelProfileNumerator,fuelProfileDenominator]) field.disabled=locked;
  fuelProfileSubmit.disabled=locked||!fuelProfileData||fuelProfileData.world!==fuelProfileWorld;
  fuelProfileRetry.hidden=!pending; fuelProfileRetry.disabled=!pending||pending.invalid||fuelProfileSending;
  if(pending) fuelProfileNotice.textContent=pending.invalid ? "原请求记录无法读取，不能安全地重新提交。" : "上次请求结果尚未确认，只能用原请求核对。";
}
function fuelProfileSyncWorld() {
  const world=state.worldId || "";
  if(world===fuelProfileWorld) return;
  fuelProfileWorld=world; fuelProfileData=null;
  for(const select of [fuelProfileVehicle,fuelProfileItem,fuelProfileSource]) select.replaceChildren();
  for(const input of [fuelProfileCapacity,fuelProfileNumerator,fuelProfileDenominator]) input.value="";
  fuelProfileNotice.textContent=world ? "先读取当前世界的候选。" : "请先选择世界。";
  fuelProfileStatus();
}
function fuelProfileSources() {
  fuelProfileSource.replaceChildren(); fuelProfileOption(fuelProfileSource,"","请选择来源事件");
  const vehicle=fuelProfileData?.vehicles.find(row=>row.id===fuelProfileVehicle.value);
  for(const source of vehicle?.sources || []) fuelProfileOption(fuelProfileSource,source.id,`${source.occurred_at} · ${source.id}`);
}
fuelProfileVehicle.addEventListener("change",fuelProfileSources);
fuelProfileLoad.addEventListener("click",async()=>{
  fuelProfileSyncWorld(); const world=fuelProfileWorld;
  if(!world || fuelProfileSending) return;
  fuelProfileNotice.textContent="正在读取已有事实……";
  try {
    const data=await api("/api/worlds/"+encodeURIComponent(world)+"/vehicle-fuel/profile-options");
    if(world!==state.worldId) { fuelProfileSyncWorld(); return; }
    fuelProfileData={world,vehicles:Array.isArray(data?.vehicles)?data.vehicles:[],items:Array.isArray(data?.item_types)?data.item_types:[]};
    fuelProfileVehicle.replaceChildren(); fuelProfileOption(fuelProfileVehicle,"","请选择车辆");
    for(const v of fuelProfileData.vehicles) fuelProfileOption(fuelProfileVehicle,v.id,`${v.name} · ${v.location_name} · ${v.id}`);
    fuelProfileItem.replaceChildren(); fuelProfileOption(fuelProfileItem,"","请选择物品类型");
    for(const i of fuelProfileData.items) fuelProfileOption(fuelProfileItem,i.id,`${i.name} · ${i.id}`);
    fuelProfileSources();
    fuelProfileNotice.textContent="仅展示原登记事件与全世界最近 1000 条事件中的同地点有效来源；提交和确认时会再次核验。";
  } catch(error) { if(world===state.worldId) fuelProfileNotice.textContent="读取失败："+(error.message||"请稍后重试。"); }
  fuelProfileStatus();
});
async function fuelProfileSend(data) {
  const world=data.world;
  if(!world || fuelProfileSending || world!==state.worldId || data.invalid) return;
  const wasUncertain=Boolean(data.uncertain);
  try { sessionStorage.setItem(fuelProfileStorageKey(world),JSON.stringify(data)); }
  catch (_) { fuelProfileNotice.textContent="无法保存原请求，暂不提交。"; return; }
  fuelProfileSending=true; fuelProfileStatus();
  try {
    const result=await api("/api/worlds/"+encodeURIComponent(world)+"/registrations",{method:"POST",body:JSON.stringify(data.payload)});
    if(!result?.id || result.element_type!=="vehicle_fuel_profile" || !["proposed","applied","rejected","failed"].includes(result.status)) throw new Error("没有取得可核验的登记回执");
    sessionStorage.removeItem(fuelProfileStorageKey(world));
    if(world===state.worldId) {
      fuelProfileData=null;
      const message=result.status==="proposed" ? "规格已提交待审核；请在最近注册中核对完整参数与来源后，再决定是否确认。"
        : result.status==="applied" ? "原请求已经确认登记，请在登记清单中查看结果。"
        : "原请求已结束："+(result.rejection_reason||"未能登记")+"。请在登记清单中查看结果。";
      fuelProfileNotice.textContent=message;
      if(typeof refreshWorldState==="function") {
        try { await refreshWorldState({silent:true}); }
        catch (_) { fuelProfileNotice.textContent=message+" 刷新登记清单可查看详情。"; }
      }
    }
  } catch(error) {
    if(!wasUncertain && [400,404,409,422].includes(error?.status)) {
      try { sessionStorage.removeItem(fuelProfileStorageKey(world)); }
      catch (_) { data.uncertain=true; }
      if(world===state.worldId) fuelProfileNotice.textContent="本次未提交："+(error.message||"请核对状态。")+" 参数已保留。";
    } else {
      data.uncertain=true;
      try { sessionStorage.setItem(fuelProfileStorageKey(world),JSON.stringify(data)); } catch (_) { /* 发送前已保存原请求。 */ }
      if(world===state.worldId) fuelProfileNotice.textContent="尚未确认请求结果；请用原请求核对。";
    }
  } finally { fuelProfileSending=false; fuelProfileStatus(); }
}
fuelProfileForm.addEventListener("submit",async event=>{
  event.preventDefault(); fuelProfileSyncWorld(); const world=fuelProfileWorld, data=fuelProfileData;
  if(!world || !data || data.world!==world || fuelProfilePending(world) || fuelProfileSending || !fuelProfileForm.reportValidity()) return;
  const vehicle=data.vehicles.find(v=>v.id===fuelProfileVehicle.value), item=data.items.find(i=>i.id===fuelProfileItem.value);
  const source=vehicle?.sources.find(s=>s.id===fuelProfileSource.value);
  const values=[fuelProfileCapacity,fuelProfileNumerator,fuelProfileDenominator].map(i=>Number(i.value));
  if(!vehicle || !item || !source || values.some(n=>!Number.isSafeInteger(n)||n<1||n>1000000000)) { fuelProfileNotice.textContent="请核对已有车辆、物品、来源和正整数参数。"; return; }
  let key; try { key="fuel-profile:"+crypto.randomUUID(); } catch (_) { fuelProfileNotice.textContent="无法生成请求编号，暂不提交。"; return; }
  await fuelProfileSend({world,payload:{source_event_id:source.id,idempotency_key:key,payload:{element_type:"vehicle_fuel_profile",vehicle_id:vehicle.id,item_type_id:item.id,capacity_units:values[0],rate_numerator:values[1],rate_denominator:values[2]}}});
});
fuelProfileRetry.addEventListener("click",async()=>{ fuelProfileSyncWorld(); const pending=fuelProfilePending(fuelProfileWorld); if(pending&&!pending.invalid) await fuelProfileSend(pending); });
fuelProfileSyncWorld();
