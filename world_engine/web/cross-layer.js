/* 哥哥，共同通过通道仍由双方各自作决定和实际行动。 */
if(typeof eventLabels!=="undefined")Object.assign(eventLabels,{
  "social.cross_layer_proposed":"提出共同通过通道",
  "social.cross_layer_declined":"婉拒共同通过通道",
  "social.cross_layer_cancelled":"撤回共同通道提议",
  "action.wonder_enter":"本人通过通道登上",
  "action.wonder_exit":"本人通过通道返回地面",
});
let passageContextKey = "", passageContextEpoch = 0, passageRead = null, passageReadAt = 0;
const passageDrafts = new Map(), passagePending = new Map(), passageWrites = new Set();
const passageStatusLabels = {
  pending: "等待对方本人决定", npc_passed: "对方已自行通过，等待你自己的选择",
  completed: "双方均已各自通过", declined: "对方已婉拒", cancelled: "你已撤回",
  expired: "提议已过期",
};

function passageContext() {
  const worldId = state.worldId, playerId = currentPlayer()?.id;
  const key = worldId && playerId ? worldId + ":" + playerId : "";
  if (key !== passageContextKey) {
    if (passageRead) passageRead.invalidated = true;
    passageContextKey = key; passageContextEpoch += 1; passageReadAt = 0;
  }
  return {worldId, playerId, key, epoch:passageContextEpoch};
}
function passageCurrent(context) {
  const now = passageContext();
  return Boolean(context?.key) && now.key === context.key && now.epoch === context.epoch;
}
function passageText(parent, value, tag="p") {
  const node=document.createElement(tag); node.textContent=value; parent.append(node); return node;
}
function passageStorageKey(context) {
  return "virtual-world:cross-layer-proposal:" + encodeURIComponent(context.worldId) + ":" + encodeURIComponent(context.playerId);
}
function passageApproachStorageKey(context, row) {
  return "virtual-world:cross-layer-approach:" + encodeURIComponent(context.worldId) + ":" + encodeURIComponent(context.playerId) + ":" + encodeURIComponent(row.id);
}
function passageGetPending(context) {
  if (passagePending.has(context.key)) return passagePending.get(context.key);
  try {
    const raw = sessionStorage.getItem(passageStorageKey(context));
    if (!raw) return null;
    const value = JSON.parse(raw), p = value?.payload;
    if (p && typeof p.npc_id === "string" && p.npc_id && typeof p.wonder_id === "string" && p.wonder_id &&
        ["enter", "exit"].includes(p.direction) && typeof p.idempotency_key === "string" && /^passage:[a-f0-9-]{36}$/i.test(p.idempotency_key)) {
      value.uncertain=true; passagePending.set(context.key,value); return value;
    }
  } catch (_) { /* 哥哥，损坏的原请求不能覆盖成另一项选择。 */ }
  const invalid={invalid:true}; passagePending.set(context.key,invalid); return invalid;
}
function passageStore(context, value) {
  sessionStorage.setItem(passageStorageKey(context), JSON.stringify(value)); passagePending.set(context.key,value);
}
function passageClear(context, value) {
  if (passagePending.get(context.key)!==value) return;
  sessionStorage.removeItem(passageStorageKey(context)); passagePending.delete(context.key);
}
async function passagePropose(context, pending, notice, update) {
  if (!passageCurrent(context) || passageWrites.has(context.key) || state.busy || state.responsePending) return;
  const uncertain=pending.uncertain;
  try { passageStore(context,pending); }
  catch (_) { notice.textContent="无法保存请求编号，本次没有发送。"; return; }
  passageWrites.add(context.key); update();
  try {
    const result=await api("/api/worlds/"+encodeURIComponent(context.worldId)+"/cross-layer/proposals", {
      method:"POST", body:JSON.stringify(pending.payload),
    });
    if (!result?.id || !result?.proposal_event_id) throw new Error("未收到可核验的提议记录");
    try { passageClear(context,pending); }
    catch (_) { pending.uncertain=true; passagePending.set(context.key,pending); if(passageCurrent(context))notice.textContent="提议已记录，但请求记录未能清除；请用原请求核对。"; return; }
    passageDrafts.delete(context.key);
    passageWrites.delete(context.key);
    if(passageCurrent(context)) await renderCrossLayer(true);
  } catch(error) {
    if(!uncertain && [400,404,409,422].includes(error?.status)) {
      try { passageClear(context,pending); } catch (_) { pending.uncertain=true; }
      if(passageCurrent(context)) notice.textContent="提议未成立："+(error.message||"请重新核对实际状态。")+" 草稿已保留。";
    } else {
      pending.uncertain=true;
      try { passageStore(context,pending); } catch (_) { /* 已在发送前保存。 */ }
      if(passageCurrent(context)) notice.textContent="结果尚未确认，请用原请求核对，不会重复生成邀请。";
    }
  } finally { passageWrites.delete(context.key); if(passageCurrent(context))update(); }
}

function passageRow(parent,row,context,current) {
  const section=document.createElement("article"); section.className="cross-layer-record"; parent.append(section);
  const label=row.direction==="enter"?"登上":"返回地面";
  passageText(section,`${row.npc_name||"同行者"} · ${row.wonder_name||"公共通道"} · ${label}`,"h4");
  passageText(section,row.expired?"提议已过期，已发生的行动仍保留。":(passageStatusLabels[row.status]||"跨层记录"));
  if(row.status==="pending"&&!row.expired) {
    passageText(section,"提议不会自动询问或替对方决定；走近和通过各需本人的选择。");
    if(row.can_talk===true) {
      const ask=document.createElement("button"); ask.type="button";ask.className="button secondary";ask.textContent="与对方商量";
      ask.addEventListener("click",()=>{
        if(!current()||state.busy||state.responsePending)return;
        fillIntent(`关于刚才通过${row.wonder_name||"公共通道"}的提议，你愿意自己走到节点并在抵达后再决定是否${label}吗？你可以拒绝，也不需要现在决定。`,row.npc_id);
      }); section.append(ask);
    } else passageText(section,"双方当前不在可商量的现场，等待各自到达或移动结束。");
  }
  if(row.status==="npc_passed"&&!row.expired) {
    const go=document.createElement("button");go.type="button";go.className="button primary";go.textContent="由我本人通过通道";
    go.disabled=row.player_at_node!==true;
    go.addEventListener("click",()=>passageAct(context,row,"traverse",go,current));section.append(go);
  }
  let pendingApproach=false;
  try {pendingApproach=Boolean(sessionStorage.getItem(passageApproachStorageKey(context,row)));}
  catch (_) {pendingApproach=true;}
  if(pendingApproach||(["pending","npc_passed"].includes(row.status)&&!row.expired&&row.player_can_approach===true)) {
    const approach=document.createElement("button");approach.type="button";approach.className="button secondary";approach.textContent=pendingApproach?"核对上次走向节点的请求":"由我本人走到通道节点";
    approach.addEventListener("click",()=>passageApproach(context,row,approach,current));section.append(approach);
  }
  if(["pending","npc_passed"].includes(row.status)) {
    const cancel=document.createElement("button");cancel.type="button";cancel.className="button secondary";cancel.textContent="撤回我的提议";
    cancel.addEventListener("click",()=>passageAct(context,row,"cancel",cancel,current));section.append(cancel);
  }
}
async function passageApproach(context,row,button,current) {
  if(!current()||state.busy||state.responsePending||passageWrites.has(context.key))return;
  const storage=passageApproachStorageKey(context,row), notice=document.querySelector("#cross-layer .cross-layer-notice");
  let request,uncertain=false;
  try {
    const raw=sessionStorage.getItem(storage);
    if(raw) {
      request=JSON.parse(raw);uncertain=true;
      if(typeof request?.idempotency_key!=="string"||!/^passage-approach:[a-f0-9-]{36}$/i.test(request.idempotency_key))throw new Error("原请求不可核验");
    } else {
      request={idempotency_key:"passage-approach:"+crypto.randomUUID()};
      sessionStorage.setItem(storage,JSON.stringify(request));
    }
  } catch (_) {if(notice)notice.textContent="无法保存或读取原行走请求，本次没有发送。";return;}
  passageWrites.add(context.key);button.disabled=true;
  try {
    const result=await api("/api/worlds/"+encodeURIComponent(context.worldId)+"/cross-layer/"+encodeURIComponent(row.id)+"/approach",{method:"POST",body:JSON.stringify(request)});
    if(!result?.movement_id||!result?.approach_event_id)throw new Error("未取得真实行走回执");
    sessionStorage.removeItem(storage);
    if(current()) {
      passageWrites.delete(context.key);await renderCrossLayer(true);
      if(typeof refreshWorldState==="function")await refreshWorldState({silent:true});
      if(passageCurrent(context)&&typeof refreshGameScene==="function")await refreshGameScene();
      const next=document.querySelector("#cross-layer .cross-layer-notice");if(next)next.textContent="你已开始朝通道节点行走，抵达后仍须本人另行选择通过。";
    }
  } catch(error) {
    if(!uncertain&&[400,404,409,422].includes(error?.status)) {
      try {sessionStorage.removeItem(storage);} catch (_) { /* 哥哥，保留原请求仍可避免重复移动。 */ }
      if(current()&&notice)notice.textContent="本次行走未开始："+(error.message||"请重新核对当前状态。");
    } else if(current()&&notice)notice.textContent="行走结果尚未确认，再次选择将使用原请求核对。";
  } finally {passageWrites.delete(context.key);if(current())button.disabled=false;}
}
async function passageAct(context,row,operation,button,current) {
  if(!current()||state.busy||state.responsePending||passageWrites.has(context.key))return;
  passageWrites.add(context.key);button.disabled=true;
  try {
    await api("/api/worlds/"+encodeURIComponent(context.worldId)+"/cross-layer/"+encodeURIComponent(row.id)+"/"+operation,{method:"POST"});
    if(current()) {
      passageWrites.delete(context.key); await renderCrossLayer(true);
      if(typeof refreshWorldState==="function")await refreshWorldState({silent:true});
      if(passageCurrent(context)&&typeof refreshGameScene==="function")await refreshGameScene();
    }
  } catch(error) {
    if(current()) {
      const notice=document.querySelector("#cross-layer .cross-layer-notice");
      if(notice)notice.textContent="暂未确认结果："+(error.message||"请刷新原记录；重试不会替对方行动。");
    }
  } finally { passageWrites.delete(context.key); if(current())button.disabled=false; }
}
function passageForm(parent,wonders,context,current) {
  const form=document.createElement("form");form.className="long-term-form cross-layer-form";parent.append(form);
  const draft=passageDrafts.get(context.key)||{};
  function field(label) { const box=document.createElement("label"),select=document.createElement("select");box.textContent=label;select.setAttribute("aria-label",label);box.append(select);form.append(box);return select; }
  function option(select,value,label) { const entry=document.createElement("option");entry.value=value;entry.textContent=label;select.append(entry); }
  const wonder=field("公共通道"),person=field("商量对象"),direction=field("方向");
  option(wonder,"","请选择通道");for(const w of wonders)option(wonder,w.id,w.name);wonder.value=draft.wonder_id||"";
  function sync() {
    const selected=wonders.find(w=>w.id===wonder.value);const people=selected?.people||[],directions=selected?.directions||[];
    person.replaceChildren();direction.replaceChildren();option(person,"","请选择对象");
    for(const p of people)option(person,p.id,p.name);for(const d of directions)option(direction,d,d==="enter"?"登上":"返回地面");
    person.value=draft.npc_id||"";direction.value=draft.direction||directions[0]||"";
  }
  function save() { if(current()) passageDrafts.set(context.key,{wonder_id:wonder.value,npc_id:person.value,direction:direction.value}); }
  form.passageSaveDraft=save;
  wonder.addEventListener("change",()=>{draft.npc_id="";draft.direction="";sync();save();});
  person.addEventListener("change",save);direction.addEventListener("change",save);sync();
  const submit=document.createElement("button");submit.type="submit";submit.className="button primary";submit.textContent="提出共同通过";form.append(submit);
  const retry=document.createElement("button");retry.type="button";retry.className="button secondary";retry.textContent="用原提议核对结果";form.append(retry);
  const notice=passageText(form,"");notice.setAttribute("aria-live","polite");
  function update() {
    if(!current())return;const pending=passageGetPending(context),locked=Boolean(pending||passageWrites.has(context.key));
    for(const field of [wonder,person,direction])field.disabled=locked;
    submit.disabled=locked||!wonders.length;retry.hidden=!pending;retry.disabled=!pending||pending.invalid||passageWrites.has(context.key);
    if(pending)notice.textContent=pending.invalid?"原请求记录不可读取，不能安全重新提交。":"上次结果尚未确认，请用原提议核对。";
  }
  update();
  form.addEventListener("submit",async event=>{
    event.preventDefault();if(!current()||passageGetPending(context)||passageWrites.has(context.key)||state.busy||state.responsePending)return;
    const selected=wonders.find(w=>w.id===wonder.value);
    if(!selected || !(selected.people||[]).some(p=>p.id===person.value) || !(selected.directions||[]).includes(direction.value)) {
      notice.textContent="请从当前真实可用的通道和对象中选择。";save();return;
    }
    save();let key;try{key="passage:"+crypto.randomUUID();}catch(_){notice.textContent="无法生成请求编号，本次没有发送。";return;}
    await passagePropose(context,{payload:{npc_id:person.value,wonder_id:wonder.value,direction:direction.value,idempotency_key:key},uncertain:false},notice,update);
  });
  retry.addEventListener("click",async()=>{const pending=passageGetPending(context);if(pending&&!pending.invalid&&current())await passagePropose(context,pending,notice,update);});
}

async function renderCrossLayer(force=false) {
  const context=passageContext();
  if(!context.key||!state.living){if(passageRead)passageRead.invalidated=true;document.querySelector("#cross-layer")?.remove();passageReadAt=0;return;}
  let panel=document.querySelector("#cross-layer");
  if(!panel){const anchor=document.querySelector("#my-rides")||document.querySelector("#my-journeys")||elements.sceneCharacters;if(!anchor)return;
    panel=document.createElement("section");panel.id="cross-layer";panel.setAttribute("aria-labelledby","cross-layer-heading");anchor.after(panel);}
  const token=context.key+":"+context.epoch;
  if(panel.dataset.passageContext!==token){panel.querySelector(".cross-layer-form")?.passageSaveDraft?.();panel.dataset.passageContext=token;panel.replaceChildren();
    passageText(panel,"共同通过公共通道","h3").id="cross-layer-heading";
    passageText(panel,"双方先在公共通道附近相遇，可各自选择实际走到节点；抵达节点后分别决定是否通过。提议不会自动替任何一方行动。");
    const notice=passageText(panel,"");notice.className="cross-layer-notice";notice.setAttribute("aria-live","polite");
    const refresh=document.createElement("button");refresh.type="button";refresh.className="button secondary";refresh.textContent="刷新共同通道安排";refresh.addEventListener("click",()=>renderCrossLayer(true));panel.append(refresh);
    const list=document.createElement("div");list.className="cross-layer-list";panel.append(list);
    const form=document.createElement("div");form.className="cross-layer-proposal";panel.append(form);
  }
  if(!force&&(state.busy||state.responsePending||passageWrites.has(context.key)||passageRead&&!passageRead.invalidated||Date.now()-passageReadAt<4000))return;
  if(passageRead)passageRead.invalidated=true;const request={context,invalidated:false};passageRead=request;
  const current=()=>passageRead===request&&!request.invalidated&&panel.isConnected&&panel.dataset.passageContext===token&&passageCurrent(context);
  try {
    const [options,records]=await Promise.all([api("/api/worlds/"+encodeURIComponent(context.worldId)+"/cross-layer/options"),api("/api/worlds/"+encodeURIComponent(context.worldId)+"/cross-layer")]);
    if(!current())return;passageReadAt=Date.now();
    const list=panel.querySelector(".cross-layer-list"),form=panel.querySelector(".cross-layer-proposal");
    list.replaceChildren();const rows=Array.isArray(records?.passages)?records.passages:[];
    if(!rows.length)passageText(list,"暂无共同通过安排。");for(const row of rows)passageRow(list,row,context,()=>panel.isConnected&&panel.dataset.passageContext===token&&passageCurrent(context));
    form.querySelector(".cross-layer-form")?.passageSaveDraft?.();form.replaceChildren();
    const wonders=Array.isArray(options?.wonders)?options.wonders:[];
    if(!wonders.length)passageText(form,"目前没有双方均在附近、可以共同商量的开放通道。");
    passageForm(form,wonders,context,()=>panel.isConnected&&panel.dataset.passageContext===token&&passageCurrent(context));
  } catch (_) {
    if(current()){
      const form=panel.querySelector(".cross-layer-proposal");
      if(!form.querySelector(".cross-layer-form"))passageForm(form,[],context,()=>panel.isConnected&&panel.dataset.passageContext===token&&passageCurrent(context));
      const notice=panel.querySelector(".cross-layer-notice");if(notice)notice.textContent="暂时无法读取安排；原草稿和未确认请求保留。";
    }
  } finally {if(passageRead===request)passageRead=null;}
}

const passageOriginalLiving=renderLiving;
renderLiving=function(){passageOriginalLiving();renderCrossLayer();};
