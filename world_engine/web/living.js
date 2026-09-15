/* 主视角只显示实际可知的经历，编年者仍保留创作视图。 */
function livingName(character) {
  if (!character || character.is_player || state.currentRoom.startsWith("observer")) return character?.name || "人物";
  return state.living?.people.find((item) => item.id === character.id)?.name
    || state.living?.known_people.find((item) => item.id === character.id)?.name || "尚未认识的人";
}

function renderLiving() {
  const view = state.living;
  if (!view) return;
  renderAppointments();
  if(typeof renderPublicRoutines === "function") renderPublicRoutines();
  renderBeliefs();
  if (activeSequencePlan?.worldId !== state.worldId) activeSequencePlan = null;
  const pending = state.pendingSequences?.[0];
  if (!activeSequencePlan && pending) activeSequencePlan = {worldId:state.worldId,request_id:pending.request_id,steps:pending.steps,target_character_id:pending.target_character_id,delivery:pending.delivery};
  $("#sequence-resume").hidden = !activeSequencePlan;
  $("#living-emotion").textContent = view.personal_state ? `你此刻感到${view.personal_state.emotion}；这份情绪会随时间缓和。` : "你可以按自己的意愿安排接下来的生活。";
  $("#living-notifications").replaceChildren(...view.notifications.filter((item) => !item.read_at).map((item) => {
    const row = document.createElement("div"); row.className = "life-item";
    row.append(emptyElement("span", item.title, ""));
    const button = document.createElement("button"); button.type = "button"; button.className = "button secondary"; button.textContent = "已读";
    button.addEventListener("click", () => perform("正在记录已读", () => api(`/api/worlds/${state.worldId}/player/notifications/${item.id}/read`, {method:"POST"}), "通知已读"));
    row.append(button); return row;
  }));
  $("#living-rumors").replaceChildren(...view.events.filter((event) => event.source_kind === "heard").slice(0,10).map((event) => emptyElement("p", `${event.summary}（听闻，可信度约${Math.round(event.confidence*100)}%，消息发生于${formatWorldTime(event.occurred_at)}）`, "")));
  const bonds = [];
  for (const home of view.residences.filter((item) => item.status === "active")) bonds.push(emptyElement("p", `住处：${home.name}，租期至${formatWorldTime(home.due_world_time)}。`, ""));
  for (const work of view.work_history) bonds.push(emptyElement("p", `你在${work.name}完成过${work.completed_units}次工作${work.completed_units>=3 ? "，这里已经成为你生活经历的一部分" : ""}。`, ""));
  $("#living-bonds").replaceChildren(...bonds);
  $("#living-goals").replaceChildren(...view.goals.map((goal) => emptyElement("p", `${goal.title}：${goal.progress}/${goal.quantity}${goal.achieved ? " · 已达成" : ""}`, "")));
  renderLivingMarket();
  renderLodgingOptions();
  if ($("#economy-location")) {
    fillTaskSelect("#economy-location", state.snapshot.locations.map((item) => [item.name,item.id]));
    fillTaskSelect("#economy-owner", [["公共资源",""],...state.snapshot.characters.map((item) => [item.name,item.id])]);
  }
}

let activeSequencePlan = null;

function renderBeliefs() {
  const view=state.living;
  if(!view)return;
  const labels={confirmed:"有直接依据",reported:"尚待核实",disputed:"证据有冲突",superseded:"旧说法，保留对照",stale:"消息可能已过时"};
  const methods={observed:"亲眼所见",reported:"对方的说法",document:"联络署名",heard:"交谈中听闻"};
  $("#living-beliefs").replaceChildren(...(view.beliefs || []).map((belief)=>{
    const row=document.createElement("article");row.className="registration-item";
    const title=document.createElement("strong");title.textContent=`${belief.subject_name} · ${belief.predicate==="name" ? "姓名或署名" : "所在位置"}：${belief.value}`;row.append(title);
    row.append(emptyElement("p",`${labels[belief.status] || belief.status} · 把握约${Math.round(belief.confidence*100)}%`,""));
    const details=document.createElement("details"),summary=document.createElement("summary");summary.textContent="最近的依据";details.append(summary);
    for(const evidence of belief.evidence){
      const source=evidence.source_character_id ? ` · 来源：${characterById(evidence.source_character_id)?.name || "尚未认识的人"}` : "";
      details.append(emptyElement("p",`${methods[evidence.method]}${source} · ${formatWorldTime(evidence.fact_world_time)}：${evidence.quote}`,""));
    }
    row.append(details);
    if(belief.predicate==="location" && view.people.some((person)=>person.id===belief.subject_id)){
      const button=document.createElement("button");button.type="button";button.className="button secondary";button.textContent="用眼前所见核对";
      button.addEventListener("click",()=>perform("正在核对眼前的人物",()=>api(`/api/worlds/${state.worldId}/player/knowledge/observe/${belief.subject_id}`,{method:"POST"}),"位置记录已更新，旧说法仍保留"));row.append(button);
    }
    return row;
  }));
  if(!view.beliefs?.length)$("#living-beliefs").append(emptyElement("p","你还没有留下可对照的身份或位置说法。","muted"));
  $("#living-dispositions").replaceChildren(...(view.dispositions?.recent_changes || []).map((change)=>emptyElement("p",`${formatWorldTime(change.occurred_at)} · ${change.reason}（${change.trait}：${change.before_value} → ${change.after_value}）。这些倾向不会替你作出选择。`,"")));
  if(!view.dispositions?.recent_changes?.length)$("#living-dispositions").append(emptyElement("p","普通闲聊不会改写你的长期特质。","muted"));
}

function renderAppointmentOptions() {
  const kind=elements.longTermType.value;
  const isMeeting=["约定","约定改期"].includes(kind);
  $("#long-term-duration").closest("label").hidden=isMeeting;
  $("#long-term-duration").disabled=isMeeting;
  $("#long-term-meeting-time").required=isMeeting;
  $("#long-term-meeting-minutes").required=isMeeting;
  elements.longTermPayment.disabled=kind==="约定改期";
  if(kind==="约定改期") elements.longTermPayment.value="0";
  const locations=(state.snapshot?.locations || []).filter((item)=>item.is_active!==false);
  fillTaskSelect("#long-term-meeting-location",locations.map((item)=>[item.name,item.id]));
  const appointments=(state.living?.appointments || []).filter((item)=>item.recipient_id===elements.longTermRecipient.value);
  fillTaskSelect("#long-term-appointment",appointments.map((item)=>[`${locationById(item.location_id)?.name || "约定地点"} · ${formatWorldTime(item.starts_world_time || item.due_world_time)}`,item.id]));
  $("#long-term-meeting-location").disabled=kind==="约定改期";
  if(kind==="约定改期") syncAppointmentLocation();
}

function syncAppointmentLocation() {
  const meeting=state.living?.appointments.find((item)=>item.id===$("#long-term-appointment").value);
  if(meeting) $("#long-term-meeting-location").value=meeting.location_id;
}
$("#long-term-appointment").addEventListener("change",syncAppointmentLocation);

function renderAppointments() {
  const target=$("#appointment-list");
  if(!target)return;
  target.replaceChildren(...(state.living?.appointments || []).map((meeting)=>{
    const row=document.createElement("article");row.className="registration-item";
    const name=characterById(meeting.recipient_id)?.name || "对方";
    row.append(emptyElement("strong",`${name} · ${locationById(meeting.location_id)?.name || "约定地点"}`,""));
    row.append(emptyElement("p",meeting.starts_world_time ? `${formatWorldTime(meeting.starts_world_time)} 至 ${formatWorldTime(meeting.ends_world_time)}` : `原约定最晚期限：${formatWorldTime(meeting.due_world_time)}`,""));
    if(meeting.status==="overdue")row.append(emptyElement("p","这项约定已经逾期；重新约定不会抹掉此前的迟到。",""));
    const button=document.createElement("button");button.type="button";button.className="button secondary";button.textContent="商议改期";
    button.addEventListener("click",()=>{
      elements.longTermType.value="约定改期";elements.longTermRecipient.value=meeting.recipient_id;
      updateContractFields();$("#long-term-appointment").value=meeting.id;
      $("#long-term-meeting-location").value=meeting.location_id;
      $("#long-term-meeting-minutes").value=meeting.starts_world_time ? String((new Date(meeting.ends_world_time)-new Date(meeting.starts_world_time))/60000) : "60";
      elements.longTermTitle.value="重新商议见面时间";
      $("#long-term-meeting-time").value="";$("#long-term-meeting-time").focus();
    });row.append(button);return row;
  }));
  if(!target.childElementCount)target.append(emptyElement("p","暂时没有进行中的见面约定。","muted"));
}

async function executeSequencePlan(steps = null) {
  if (state.busy) return;
  if (steps) activeSequencePlan = {worldId:state.worldId, request_id:crypto.randomUUID(), steps,
    target_character_id:state.conversationTargetId, delivery:$("#player-delivery")?.value || "normal"};
  if (!activeSequencePlan || activeSequencePlan.worldId !== state.worldId) return;
  const {worldId,...payload}=activeSequencePlan;
  setBusy(true,"正在依次执行动作与发言");
  try {
    const result=await api(`/api/worlds/${worldId}/player/sequence`,{method:"POST",body:JSON.stringify(payload)});
    const text=result.results.map((step)=>`${step.step}. ${step.summary}`).join("\n")+(result.error?`\n后续尚未执行：${result.error}`:"");
    elements.actionResult.textContent=text;elements.actionResult.hidden=false;
    if (result.status==="completed") {activeSequencePlan=null;elements.playerIntent.value="";saveIntentDraft();}
    $("#sequence-resume").hidden=result.status==="completed";
    await refreshAll();
  } catch(error) {showToast(error.message,true);}
  finally {setBusy(false);}
}
$("#sequence-resume").addEventListener("click",()=>executeSequencePlan());

function renderLivingMarket() {
  const market=state.market || {offers:[],rooms:[],merchants:[],sell_items:[]};
  const nodes=[];
  for (const offer of market.offers) {
    const row=document.createElement("div");row.className="life-item";
    row.append(emptyElement("span",`${offer.seller_name}出售${offer.name}，${offer.price}枚/份，现货${offer.quantity}份。${offer.freshness?.spoils_world_time ? `${offer.freshness.label}，${formatWorldTime(offer.freshness.spoils_world_time)}到期。` : ""}`,""));
    const button=document.createElement("button");button.type="button";button.className="button";button.textContent=`购买${offer.name}（1份）`;
    button.addEventListener("click",()=>perform("正在进行交易",()=>api(`/api/worlds/${state.worldId}/player/trade`,{method:"POST",body:JSON.stringify({seller_id:offer.seller_id,item_id:offer.item_id,quantity:1,operation:"buy",expected_total:offer.price,request_id:crypto.randomUUID()})}),"交易已完成"));
    row.append(button);nodes.push(row);
  }
  for (const merchant of market.merchants) for (const item of market.sell_items) {
    const button=document.createElement("button");button.type="button";button.className="button secondary";button.textContent=`向${merchant.name}出售${item.name}（${item.price}枚）`;
    button.addEventListener("click",()=>perform("正在出售物品",()=>api(`/api/worlds/${state.worldId}/player/trade`,{method:"POST",body:JSON.stringify({seller_id:merchant.id,item_id:item.id,quantity:1,operation:"sell",expected_total:item.price,request_id:crypto.randomUUID()})}),"交易已完成"));nodes.push(button);
  }
  if (!nodes.length) nodes.push(emptyElement("p","附近暂时没有可成交的已登记商品。","muted"));
  $("#living-market").replaceChildren(...nodes);
  $("#living-rooms").replaceChildren(...market.rooms.map((room)=> {
    const row=document.createElement("div");row.className="life-item";row.append(emptyElement("span",`${room.name} · 每世界日${room.nightly_rate}枚货币`,""));
    const button=document.createElement("button");button.type="button";button.className="button secondary";button.textContent="商议住宿";
    button.addEventListener("click",()=>{switchRoom("agreements");elements.longTermRecipient.value=room.owner_character_id;elements.longTermType.value="住房";updateContractFields();$("#long-term-room").value=room.id;$("#long-term-duration").value="1";elements.longTermPayment.value=room.nightly_rate;elements.longTermTitle.value=`租住${room.name}`;});row.append(button);return row;
  }));
}

function renderLodgingOptions() {
  const rooms=(state.market?.rooms || []).filter((item)=>item.owner_character_id===elements.longTermRecipient.value);
  fillTaskSelect("#long-term-room",rooms.map((item)=>[`${item.name} · ${item.nightly_rate}枚/日`,item.id]));
}

function openKnownProfile(characterId) {
  const person=state.living?.people.find((item)=>item.id===characterId);
  const known=state.living?.known_people.find((item)=>item.id===characterId);
  const name=person?.name || known?.name || "尚未认识的人";
  elements.characterProfile.innerHTML=`<header class="profile-head"><h1>${escapeHtml(name)}</h1></header><p>${known ? `关系阶段：${escapeHtml(known.stage)}，共同经历${known.shared_experiences}次。` : "你尚未了解这个人的身份。"}</p><p>${person ? `目前距你约${person.distance_m}米。` : `当前不在视野内${known?.last_seen ? `，最后见于${escapeHtml(formatWorldTime(known.last_seen))}` : known ? "，目前只从消息中听闻" : ""}。`}</p><div class="profile-actions"></div>`;
  if (person) {
    const button=document.createElement("button");button.type="button";button.className="button primary";button.textContent="与其交谈";
    button.addEventListener("click",()=>{fillIntent(`说话：你好，我叫${currentPlayer().name}，该怎么称呼你？`,characterId);elements.characterDialog.close();});elements.characterProfile.querySelector(".profile-actions").append(button);
    const contact=document.createElement("button");contact.type="button";contact.className="button secondary";contact.textContent="交换联络信笺";
    contact.addEventListener("click",()=>{elements.characterDialog.close();perform("正在询问对方是否交换信笺",()=>api(`/api/worlds/${state.worldId}/contacts`,{method:"POST",body:JSON.stringify({recipient_id:characterId})}),"联络请求已得到回应");});elements.characterProfile.querySelector(".profile-actions").append(contact);
    const attack=document.createElement("button");attack.type="button";attack.className="button danger";attack.textContent="发起攻击";
    attack.addEventListener("click",()=>{fillIntent(`动作：攻击${name}`,characterId);elements.characterDialog.close();});elements.characterProfile.querySelector(".profile-actions").append(attack);
  }
  elements.characterDialog.showModal();
}

$("#living-harvest").addEventListener("click",()=>perform("正在收取资源",()=>api(`/api/worlds/${state.worldId}/player/harvest`,{method:"POST"}),"资源已收入背包"));
$("#living-goal-form").addEventListener("submit",(event)=>{
  event.preventDefault();const kind=$("#living-goal-kind").value;let target=$("#living-goal-target").value.trim();
  if(kind==="work"&&!target) target=currentPlayer().current_location_id;
  if(kind==="friend") target=state.living.known_people.find((item)=>item.name===target)?.id || target;
  if(kind==="reside") target=state.living.residences.find((item)=>item.name===target)?.room_id || target;
  perform("正在记录个人目标",()=>api(`/api/worlds/${state.worldId}/player/life-goals`,{method:"POST",body:JSON.stringify({kind,title:$("#living-goal-title").value,target_id:target || null,quantity:Number($("#living-goal-quantity").value)})}),"目标已记录");
});

function economyProposalDetails(payload) {
  const location=state.snapshot.locations.find((item)=>item.id===(payload.location_id || payload.resource_location_id))?.name || "无资源来源";
  if(payload.element_type==="workplace_budget")return `<p>场所：${escapeHtml(location)} · 初始工资资金${payload.initial_funds} · 每次工资${payload.wage}</p>`;
  return `<p>${escapeHtml(payload.category)} · 堆叠${payload.stack_limit} · 单格${payload.slot_size} · 售价${payload.price} · 食物饱食恢复${payload.nutrition} · 保鲜${payload.shelf_life_hours == null ? "未登记" : `${payload.shelf_life_hours}世界小时`}<br>资源地点：${escapeHtml(location)} · 资源名${escapeHtml(payload.resource_key || "无")} · 初始${payload.initial_resource}（仅缺失时补齐）· 每日补给${payload.daily_growth} · 储量上限${payload.resource_capacity}</p>`;
}

$("#economy-form").addEventListener("submit",(event)=>{
  event.preventDefault();const kind=$("#economy-kind").value;const location=$("#economy-location").value;
  const payload=kind==="workplace_budget" ? {element_type:kind,name:$("#economy-name").value,location_id:location,initial_funds:Number($("#economy-funds").value),wage:Number($("#economy-price").value)} : {
    element_type:"commodity",name:$("#economy-name").value,category:kind,price:Number($("#economy-price").value),nutrition:kind==="food"?Number($("#economy-nutrition").value):0,stack_limit:Number($("#economy-stack").value),slot_size:1,shelf_life_hours:kind==="food" && $("#economy-shelf-life").value ? Number($("#economy-shelf-life").value) : null,
    resource_key:$("#economy-resource").value.trim() || null,resource_location_id:$("#economy-resource").value.trim()?location:null,resource_owner_id:$("#economy-owner").value || null,initial_resource:Number($("#economy-initial").value),daily_growth:Number($("#economy-growth").value),resource_capacity:Number($("#economy-capacity").value)};
  perform("正在提交经济规则",()=>api(`/api/worlds/${state.worldId}/economy-definitions`,{method:"POST",body:JSON.stringify({idempotency_key:crypto.randomUUID(),payload})}),"请在元素注册中核对完整规则后确认");
});
