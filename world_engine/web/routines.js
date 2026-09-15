/* 作息配置在作者视角审核；玩家只读居民明确公开的时段。 */
function renderRoutineOptions() {
  fillTaskSelect("#routine-character",(state.snapshot?.characters || []).filter((person)=>!person.is_player).map((person)=>[person.name,person.id]));
}

function routineTimeZone(offset) {
  const minutes=Math.abs(Number(offset));
  return `UTC${offset>=0?"+":"-"}${String(Math.floor(minutes/60)).padStart(2,"0")}:${String(minutes%60).padStart(2,"0")}`;
}

function routineSlotText(slot) {
  const days=["周一","周二","周三","周四","周五","周六","周日"];
  const week=slot.weekdays.length===7?"每天":slot.weekdays.map((day)=>days[day]).join("、");
  const [hour,minute]=slot.starts_at.split(":").map(Number),end=hour*60+minute+slot.duration_minutes;
  const finish=`${String(Math.floor(end/60)%24).padStart(2,"0")}:${String(end%60).padStart(2,"0")}${end>=1440?"（次日）":""}`;
  return `${week} ${slot.starts_at}–${finish} · ${slot.name} · ${locationById(slot.location_id)?.name || "约定地点"}`;
}

function routineProposalDetails(spec) {
  const person=state.snapshot.characters.find((item)=>item.id===spec.character_id)?.name || spec.character_id;
  return `<p>人物：${escapeHtml(person)} · ${spec.enabled?"启用":"停用"} · ${escapeHtml(routineTimeZone(spec.utc_offset_minutes))}<br>每日自动劳动上限：${spec.max_work_minutes_per_day}分钟 · 目标：${spec.goal==="maintain_reserve"?"维持生活储备":"稳定作息"}</p><ul>${spec.slots.map((slot)=>`<li>${escapeHtml(routineSlotText(slot))} · ${slot.visibility==="public"?"公开":"私人"}</li>`).join("")}</ul><details><summary>核对完整配置</summary><pre>${escapeHtml(JSON.stringify(spec,null,2))}</pre></details>`;
}

function renderPublicRoutines() {
  const container=$("#public-routines");
  if(!container)return;
  container.replaceChildren(...(state.living?.public_routines || []).map((routine)=>{
    const row=document.createElement("article");row.className="registration-item";
    row.append(emptyElement("strong",routine.name,""));
    row.append(emptyElement("small",`以下为人物当地作息时刻（${routineTimeZone(routine.utc_offset_minutes)}）；临时事务和身体需要可能使安排改变。`,""));
    for(const slot of routine.slots)row.append(emptyElement("p",routineSlotText(slot),""));
    return row;
  }));
  if(!container.childElementCount)container.append(emptyElement("p","你认识的居民暂时没有公开固定作息。","muted"));
}

$("#routine-load").addEventListener("click",async()=>{
  const cid=$("#routine-character").value,wid=state.worldId;
  try {
    const plans=await api(`/api/worlds/${wid}/npc-routines`);
    if(wid!==state.worldId || cid!==$("#routine-character").value)return;
    const plan=plans.find((item)=>item.spec.character_id===cid);
    if(!plan){$("#routine-current").textContent="该人物尚未登记周期作息。请按填写说明准备配置。";return;}
    $("#routine-json").value=JSON.stringify({...plan.spec,expected_revision:plan.revision},null,2);
    const labels={planned:"计划中",travelling:"正在赴场",active:"进行中",deferred:"暂缓",completed:"完成",partial:"部分完成",missed:"未完成",skipped:"主动略过",superseded:"旧计划已替换"};
    $("#routine-current").replaceChildren(...plan.recent_occurrences.map((entry)=>emptyElement("p",`${formatWorldTime(entry.starts_world_time)} · ${labels[entry.status] || entry.status}：${entry.reason}`,"")));
    for (const supply of plan.recent_supplies || []) $("#routine-current").append(emptyElement("p", `${formatWorldTime(supply.world_time)} · ${supply.method === "purchase" ? "采购" : "收取"}${supply.name} 1份 · 花费${supply.spent}枚货币`, ""));
    const goalLabels={waiting:"等待前置目标",active:"推进中",blocked:"暂时受阻",completed:"已达成",expired:"期限已到",cancelled:"已取消"};
    for(const item of plan.life_goals || []){
      const row=document.createElement("article");row.className="registration-item";
      row.append(emptyElement("strong",`${item.goal.title} · ${goalLabels[item.status] || item.status}`,""));
      row.append(emptyElement("p",`进展 ${item.progress}/${item.goal.target} · 优先级${item.goal.priority} · 已采购支出${item.spent}。${item.reason}`,""));
      if(item.goal.depends_on.length)row.append(emptyElement("p",`前置目标：${item.goal.depends_on.join("、")}`,""));
      if(item.retry_world_time)row.append(emptyElement("p",`下次评估：${formatWorldTime(item.retry_world_time)}`,""));
      for(const step of item.recent_steps)row.append(emptyElement("small",`${formatWorldTime(step.world_time)} · ${step.summary}`,""));
      $("#routine-current").append(row);
    }
    for(const item of plan.life_goal_history || []) $("#routine-current").append(emptyElement("p",`历史目标：${item.goal.title} · ${goalLabels[item.status] || item.status} · ${item.reason}`,"muted"));
  } catch(error){showToast(error.message,true);}
});

$("#routine-form").addEventListener("submit",(event)=>{
  event.preventDefault();
  let routine;
  try {
    routine=JSON.parse($("#routine-json").value);
    if(!routine || Array.isArray(routine) || typeof routine!=="object")throw new Error("配置必须是 JSON 对象");
    if(routine.character_id && routine.character_id!==$("#routine-character").value)throw new Error("配置中的人物与当前选择不一致，请核对");
    routine={...routine,element_type:"npc_routine",character_id:$("#routine-character").value};
  } catch(error){showToast(error.message,true);return;}
  perform("正在提交周期作息",()=>api(`/api/worlds/${state.worldId}/npc-routines`,{method:"POST",body:JSON.stringify({idempotency_key:crypto.randomUUID(),routine})}),"作息待审核，请在登记记录中核对后确认");
});
