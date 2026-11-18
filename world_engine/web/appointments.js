/* 哥哥，未来邀请与实际相处分别展示，玩家始终只确认自己的选择。 */
const appointmentStatuses = {pending:"等待对方回应",accepted:"双方已确认，等待赴约",active:"正在共同相处",completed:"已完成相处",
  missed:"未完成约定",interrupted:"相处中断",declined:"已婉拒",cancelled:"已取消",expired:"未确认已过期",superseded:"双方已确认改期"};
for (const [state, label] of Object.entries(appointmentStatuses)) eventLabels[`social.appointment_${state}`] = `约会：${label}`;
Object.assign(eventLabels, {"social.appointment_proposed":"提出未来安排","social.appointment_started":"到场开始相处","social.appointment_repaired":"补约已兑现"});
const appointmentSeriesStatuses={pending:"周期等待确认",active:"周期进行中",paused:"后续已暂停",resume_pending:"等待另一方确认恢复",ended:"周期已结束",completed:"本轮时段已结算"};
for(const [status,label] of Object.entries(appointmentSeriesStatuses))eventLabels[`social.appointment_series_${status}`]=label;
Object.assign(eventLabels,{"social.appointment_series_proposed":"提出周期相处","social.appointment_series_scheduled":"安排下一次相处","social.appointment_series_skipped":"跳过未排程的过去时段"});
let appointmentWorldEpoch=0;
elements.worldSelect.addEventListener("change",()=>{appointmentWorldEpoch+=1;});
let appointmentDialogEpoch=0;
elements.characterDialog.addEventListener("close",()=>{appointmentDialogEpoch+=1;});

function submitAppointmentChoice(characterId, choice) {
  const person=characterById(characterId),player=currentPlayer();
  const nearby=person&&player&&canInviteCompanion(person)&&distanceKm(player.longitude,player.latitude,person.longitude,person.latitude)<=0.02;
  elements.characterDialog.close();
  return executeNaturalSocialChoice(choice.statement, {target_character_id:characterId, choice_evidence:choice.statement, appointment_choice:choice},nearby?null:"letter");
}

async function appendAppointmentProfile(characterId) {
  const player=currentPlayer();
  if (!player || characterId===player.id || document.querySelector("#appointment-profile")) return;
  const worldId=state.worldId,playerId=player.id,initialEpoch=appointmentWorldEpoch,dialogEpoch=appointmentDialogEpoch;
  const panel=document.createElement("section");panel.id="appointment-profile";
  const heading=document.createElement("h2");heading.textContent="约好下一次见面";panel.append(heading);
  const note=document.createElement("p");note.textContent="双方确认后各自赴约。到场后选择开始，实际度过相处时间才算完成。改期需要对方再次同意。";panel.append(note);
  elements.characterProfile.append(panel);
  const current=()=>panel.isConnected && elements.characterDialog.open && dialogEpoch===appointmentDialogEpoch && state.worldId===worldId && currentPlayer()?.id===playerId && elements.characterProfile.contains(panel);
  const initialCurrent=()=>current()&&appointmentWorldEpoch===initialEpoch;
  const personQuery=`?character_id=${encodeURIComponent(characterId)}`;
  let options,arrangements,series;
  try {
    [options,arrangements,series]=await Promise.all([
      api(`/api/worlds/${worldId}/appointments/options/${encodeURIComponent(characterId)}`),
      api(`/api/worlds/${worldId}/appointments${personQuery}`),
      api(`/api/worlds/${worldId}/appointments/series${personQuery}`),
    ]);
    if(!Array.isArray(arrangements)||!Array.isArray(series))throw new Error("约定记录格式无效");
  }
  catch {if(initialCurrent()){const error=document.createElement("p");error.textContent="暂时无法读取约会安排，请稍后重新打开档案。";panel.append(error);}return;}
  if (!initialCurrent()) return;
  arrangements=arrangements.filter(a=>a.other_id===characterId);
  series=series.filter(s=>s.other_id===characterId);
  const form=document.createElement("form");form.className="long-term-form";
  const addField=(label,field)=>{const el=document.createElement("label");el.textContent=label;field.setAttribute("aria-label",label);el.append(field);form.append(el);return field;};
  const purpose=addField("这次想商量",document.createElement("select"));
  const addOption=(select,value,text)=>{const option=document.createElement("option");option.value=value;option.textContent=text;select.append(option);};
  addOption(purpose,"new","提出新的安排");
  for(const item of arrangements) {
    if(item.status==="accepted")addOption(purpose,`reschedule:${item.id}`,`商量改期：${item.label}`);
    if(["missed","interrupted"].includes(item.status)&&!item.repaired_by)addOption(purpose,`repair:${item.id}`,`提出补约：${item.label}`);
  }
  const kind=addField("计划一起做什么",document.createElement("select"));
  for(const [value,text] of [["talk","留时间聊天"],["meal","共同用餐"],["rest","一起休息"],["care","陪伴休养"]])addOption(kind,value,text);
  const journey=addField("衔接到站旅行（可选）",document.createElement("select"));
  addOption(journey,"","独立安排，不关联旅行");
  for(const trip of options.journey_options||[])addOption(journey,trip.id,`${trip.title} · 第${trip.stop_index+1}站室内短休`);
  const supply=addField("约会供餐安排",document.createElement("select"));
  for(const [value,text] of [["each_own","双方各自准备一份食物"],["proposer","我愿意每次提供双方各一份自己的食物"],["inherit","沿用原约会的供餐承诺"]])addOption(supply,value,text);
  const syncSupply=()=>{const inherited=Array.from(supply.options).find(o=>o.value==="inherit");inherited.hidden=!purpose.value.startsWith("reschedule:");supply.disabled=kind.value!=="meal";if(supply.disabled||inherited.hidden&&supply.value==="inherit")supply.value="each_own";};
  kind.addEventListener("change",syncSupply);syncSupply();
  const place=addField("见面地点",document.createElement("select"));
  for(const item of options.place_options)addOption(place,item.id,item.name);
  const room=addField("具体场景",document.createElement("select"));
  addOption(room,"","该地点的室外场景");
  for(const item of options.room_options||[]) {
    addOption(room,item.id,`${item.location_name} · ${item.name}`);
    if(!Array.from(place.options).some(option=>option.value===item.location_id))addOption(place,item.location_id,item.location_name);
  }
  const allowVisit=addField("我同意在本次时间窗内接待对方进入所选房间",document.createElement("input"));
  allowVisit.type="checkbox";allowVisit.disabled=true;
  const syncRoom=()=>{const chosen=(options.room_options||[]).find(item=>item.id===room.value);allowVisit.checked=false;allowVisit.disabled=!chosen?.may_host;if(chosen)place.value=chosen.location_id;};
  room.addEventListener("change",syncRoom);
  place.addEventListener("change",()=>{room.value="";syncRoom();});
  const roomNote=document.createElement("p");roomNote.textContent="室内需真实走门进入。单次访问许可不延长到整个周期；周期室内相处需要双方已有出入权。门锁、钥匙与私人储物权限分别核验。";form.append(roomNote);
  const time=addField("开始时间（世界时间 UTC）",document.createElement("input"));time.type="datetime-local";time.required=true;
  const at=Date.parse(state.living?.world_time||"");
  if(Number.isFinite(at))time.value=new Date(at+2*3600000).toISOString().slice(0,16);
  const windowMinutes=addField("可见面的时间窗（世界分钟）",document.createElement("input"));windowMinutes.type="number";windowMinutes.min="5";windowMinutes.max="240";windowMinutes.value="60";windowMinutes.required=true;
  const minutes=addField("计划相处时长（世界分钟）",document.createElement("input"));minutes.type="number";minutes.min="5";minutes.max="120";minutes.value="15";minutes.required=true;
  const repeatCount=addField("计划次数（1为单次，最多24次）",document.createElement("input"));repeatCount.type="number";repeatCount.min="1";repeatCount.max="24";repeatCount.value="1";repeatCount.required=true;
  const repeatDays=addField("每次间隔（世界日）",document.createElement("input"));repeatDays.type="number";repeatDays.min="1";repeatDays.max="90";repeatDays.value="7";repeatDays.disabled=true;repeatDays.required=true;
  repeatCount.addEventListener("input",()=>{repeatDays.disabled=Number(repeatCount.value)<=1;});
  const syncPurpose=()=>{const single=purpose.value!=="new";repeatCount.disabled=single;if(single){repeatCount.value="1";repeatDays.disabled=true;}if(purpose.value.startsWith("reschedule:"))supply.value="inherit";syncSupply();};
  purpose.addEventListener("change",syncPurpose);
  const statement=addField("想向对方说的话",document.createElement("textarea"));statement.maxLength=60;statement.required=true;statement.placeholder="说说这次邀请，也给对方自由回应的空间。";
  const submit=document.createElement("button");submit.type="submit";submit.className="button primary";submit.textContent="提出安排，听听对方的想法";submit.disabled=!place.options.length;form.append(submit);
  form.addEventListener("submit",event=>{
    event.preventDefault();if(!current()||!form.reportValidity())return;
    const meeting=new Date(time.value+":00Z");
    if(!Number.isFinite(meeting.getTime()))return;
    if(purpose.selectedOptions[0]?.disabled){statement.setCustomValidity("原安排已经变化，请重新选择。");statement.reportValidity();statement.setCustomValidity("");return;}
    const [mode,id]=purpose.value.split(":");
    const count=mode==="new"?Number(repeatCount.value):1,days=count>1?Number(repeatDays.value):null;
    submitAppointmentChoice(characterId,{operation:mode==="reschedule"?"reschedule":"propose",appointment_id:mode==="reschedule"?id:null,journey_id:mode==="new"?journey.value||null:null,
      repairs_id:mode==="repair"?id:null,kind:kind.value,location_id:place.value,room_id:room.value||null,allow_visit:allowVisit.checked,meeting_world_time:meeting.toISOString(),
      window_minutes:Number(windowMinutes.value),minutes:Number(minutes.value),repeat_every_days:days,repeat_count:count,
      meal_supply:kind.value==="meal"&&supply.value!=="inherit"?supply.value:null,
      statement:statement.value.trim()+(journey.value?" 我选择暂停这份原旅行，另约表单中的同站房间短休；房租与进屋仍各自处理，结束后不自动恢复旅行。":"")+(kind.value==="meal"?(supply.value==="proposer"?" 我愿意在这份安排的每次用餐提供双方各一份自己的随身食物，到场后重新核验，不使用你的钱。":supply.value==="inherit"?" 供餐承诺沿用原约会，不因我提出改期而换人。":" 每次用餐双方各自备齐一份自己的食物。"):"")+(count>1?` 我提议每${days}个世界日一次，共${count}次；你可以只接受这一次或明确接受整个周期。`:"")+(allowVisit.checked?" 我同意在本次约会时间窗内接待你进入这个房间，访问不包含储物或长期居住许可。":"")});
  });
  const syncJourney=()=>{
    const trip=(options.journey_options||[]).find(t=>t.id===journey.value);
    if(trip){kind.value="rest";repeatCount.value="1";repeatDays.disabled=true;place.value=trip.location_id;syncSupply();if(Number.isFinite(at))time.value=new Date(at+30*60000).toISOString().slice(0,16);}
    kind.disabled=!!trip;repeatCount.disabled=!!trip||purpose.value!=="new";place.disabled=!!trip;
    for(const option of Array.from(room.options)){const info=(options.room_options||[]).find(r=>r.id===option.value);option.hidden=!!trip&&(!info||info.location_id!==trip.location_id);}
    if(trip&&!(options.room_options||[]).some(r=>r.id===room.value&&r.location_id===trip.location_id))room.value="";
    room.required=!!trip;allowVisit.checked=false;syncRoom();submit.disabled=!place.options.length||!!trip&&!(options.room_options||[]).some(r=>r.location_id===trip.location_id);
    journey.disabled=purpose.value!=="new";
  };
  journey.addEventListener("change",syncJourney);
  purpose.addEventListener("change",()=>{if(purpose.value!=="new")journey.value="";syncJourney();});
  const journeyHelp=document.createElement("p");journeyHelp.textContent="旅途室内短休仅使用到站地点已有租住权、公共或获准接待的房间。请先单独办理所需住房租约；这里不付房费、不代双方进屋，也不授予储物或长期居住权。旅途短休为5至120分钟；4至12小时的长时住宿可在下方另行商量。";form.append(journeyHelp);
  const refresh=document.createElement("button");refresh.type="button";refresh.className="button secondary";refresh.textContent="刷新约定状态";panel.append(refresh);
  const refreshNotice=document.createElement("p");refreshNotice.setAttribute("aria-live","polite");panel.append(refreshNotice);
  const rows=document.createElement("div");rows.className="appointment-arrangements";panel.append(rows);
  const seriesRows=document.createElement("div");seriesRows.className="appointment-series";panel.append(seriesRows);
  function renderArrangements(items) {
    rows.replaceChildren();
    for(const item of items) {
    const row=document.createElement("div");row.className="life-item";
    const cycle=series.find(s=>s.id===item.series_id);
    const text=document.createElement("p");text.textContent=`${item.label} · ${appointmentStatuses[item.status]} · ${item.location_name}${item.room_name?` / ${item.room_name}`:" / 室外场景"} · ${formatWorldTime(item.starts_at)} 至 ${formatWorldTime(item.ends_at)} · 相处 ${item.minutes} 分钟`;row.append(text);
    if(item.journey_id){const origin=document.createElement("p");origin.textContent=`原旅行第${item.journey_stop_index+1}站的室内短休；双方各自进屋，实际到场后由你选择开始，结束后各自离屋并另行确认是否恢复旅行。`;row.append(origin);}
    const foodTerms=item.kind==="meal"?(item.meal_provider_id?`${item.meal_provider_id===player.id?"我":"对方"}提供双方各一份自己的食物`:"双方各自准备一份自己的食物"):"";
    if(foodTerms){const food=document.createElement("p");food.textContent=`本次供餐：${foodTerms}。${["pending","accepted"].includes(item.status)?"当前只记录承诺，到场后重新核验并消耗。":"实际消耗以共同用餐记录为准。"}`;row.append(food);}
    if(item.visit_host_id){const visit=document.createElement("p");visit.textContent="接待者已明确允许本次室内访问；双方确认、时间窗有效且接待者在场时才可进门。";row.append(visit);}
    const speech=document.createElement("p");speech.textContent=`${item.proposer_id===player.id?"你的邀请":"对方的邀请"}：${item.statement}${item.response?` / ${item.response}`:""}`;row.append(speech);
    if(item.reason){const reason=document.createElement("p");reason.textContent=item.reason;row.append(reason);}
    if(item.own_arrived_at){const arrival=document.createElement("p");arrival.textContent=`你曾在 ${formatWorldTime(item.own_arrived_at)} 实际到场。`;row.append(arrival);}
    if(item.repaired_by){const repaired=document.createElement("p");repaired.textContent="一次补约已实际完成，原记录仍然保留。";row.append(repaired);}
    if(cycle){const source=document.createElement("p");source.textContent=`周期第${item.occurrence_index+1}/${cycle.occurrence_count}次 · 每${cycle.interval_days}个世界日一次。每一次的实际结果分别保留。`;row.append(source);}
    const button=(label,action)=>{const el=document.createElement("button");el.type="button";el.className="button secondary";el.textContent=label;el.addEventListener("click",()=>{if(current())action();});row.append(el);};
    const choose=(label,operation,speech)=>button(label,()=>submitAppointmentChoice(characterId,{operation,appointment_id:item.id,statement:speech}));
    if(item.status==="pending") {
      if(item.proposer_id!==player.id){
        choose(cycle?.status==="pending"?"只接受这一次":"接受这次安排","accept",`我愿意按这份时间、地点和时长安排赴约${foodTerms?`，本次供餐为${foodTerms}`:""}，这次只确认眼前这一场相处。`);
        if(cycle?.status==="pending")button("接受整份周期安排",()=>submitAppointmentChoice(characterId,{operation:"accept",appointment_id:item.id,accept_recurrence:true,
          statement:`我愿意按这份地点和时长安排，每${cycle.interval_days}个世界日见一次，共${cycle.occurrence_count}次${foodTerms?`，每次供餐为${foodTerms}`:""}，明确接受整个周期。`}));
        if((options.room_options||[]).some(room=>room.id===item.room_id&&room.may_host))button("接受，并同意本次室内接待",()=>submitAppointmentChoice(characterId,{operation:"accept",appointment_id:item.id,allow_visit:true,statement:`我接受这次安排${foodTerms?`，供餐为${foodTerms}`:""}，并同意本次接待进入这间房间，访问不包含储物或长期居住许可。`}));
        choose("婉拒邀请","decline","谢谢你的邀请，但我这次不参加。");
      }
      else choose("撤回我的邀请","cancel","我先撤回这次邀请。");
    }
    if(item.status==="accepted") {
      choose("到场后开始约定的相处","attend","我已到场，愿意开始这次约定的相处。");
      button("商量改期",()=>{purpose.value=`reschedule:${item.id}`;journey.value="";syncJourney();kind.value=item.kind;syncPurpose();room.value=item.room_id||"";syncRoom();place.value=item.location_id;form.scrollIntoView({block:"center"});time.focus();});
    }
    if(["accepted","active"].includes(item.status))choose("取消或结束这次安排","cancel","我决定取消或结束这次安排，想如实告诉你。");
    if(["missed","interrupted"].includes(item.status)&&!item.repaired_by)button("商量一次补约",()=>{purpose.value=`repair:${item.id}`;journey.value="";syncJourney();syncPurpose();form.scrollIntoView({block:"center"});statement.focus();});
    rows.append(row);
    }
  }
  function refreshPurposeOptions(items) {
    const selected=purpose.value;
    for(const option of Array.from(purpose.options))if(option.value.startsWith("reschedule:")||option.value.startsWith("repair:"))option.remove();
    for(const item of items){
      if(item.status==="accepted")addOption(purpose,`reschedule:${item.id}`,`商量改期：${item.label}`);
      if(["missed","interrupted"].includes(item.status)&&!item.repaired_by)addOption(purpose,`repair:${item.id}`,`提出补约：${item.label}`);
    }
    if(selected&&!Array.from(purpose.options).some(option=>option.value===selected)){
      addOption(purpose,selected,"原安排已变化，请重新选择");
      purpose.options[purpose.options.length-1].disabled=true;
    }
    purpose.value=selected||"new";
  }
  renderArrangements(arrangements);
  function renderSeries(){seriesRows.replaceChildren();appendAppointmentSeries(seriesRows,characterId,series,player,current);}
  renderSeries();
  refresh.addEventListener("click",async()=>{
    if(!current()||refresh.disabled)return;
    const requestEpoch=appointmentWorldEpoch;
    const requestCurrent=()=>current()&&requestEpoch===appointmentWorldEpoch;
    refresh.disabled=true;refreshNotice.textContent="正在刷新约定状态……";
    try {
      const [updated,updatedSeries]=await Promise.all([
        api(`/api/worlds/${worldId}/appointments${personQuery}`),api(`/api/worlds/${worldId}/appointments/series${personQuery}`),
      ]);
      if(!requestCurrent())return;
      if(!Array.isArray(updated)||!Array.isArray(updatedSeries))throw new Error("约定记录格式无效");
      const relevant=updated.filter(item=>item.other_id===characterId);
      series=updatedSeries.filter(item=>item.other_id===characterId);
      refreshPurposeOptions(relevant);renderArrangements(relevant);renderSeries();
      refreshNotice.textContent="约定状态已刷新；填写中的内容已保留。";
    } catch (_) {
      if(requestCurrent())refreshNotice.textContent="暂时无法刷新约定状态；现有记录和填写内容已保留。";
    } finally {
      if(panel.isConnected){refresh.disabled=false;if(current()&&!requestCurrent())refreshNotice.textContent="世界已切换；请重新刷新，填写内容已保留。";}
    }
  });
  panel.append(form);
}

function appendAppointmentSeries(panel,characterId,series,player,current=()=>true) {
  if(!series.length)return;
  const heading=document.createElement("h3");heading.textContent="固定留给彼此的时间";panel.append(heading);
  for(const cycle of series) {
    const row=document.createElement("div");row.className="life-item";
    const text=document.createElement("p");text.textContent=`${appointmentSeriesStatuses[cycle.status]} · 每${cycle.interval_days}个世界日一次，共${cycle.occurrence_count}次 · 实际完成${cycle.completed_count}次，失约${cycle.missed_count}次`;row.append(text);
    if(cycle.meal_provider_id){const food=document.createElement("p");food.textContent=`原周期供餐承诺：${cycle.meal_provider_id===player.id?"你":"对方"}每次提供双方各一份食物。单次改期的条款以该次记录为准。`;row.append(food);}
    const statement=document.createElement("p");statement.textContent=cycle.statement;row.append(statement);
    if(cycle.reason){const reason=document.createElement("p");reason.textContent=cycle.reason;row.append(reason);}
    for(const item of cycle.occurrences||[]) {
      const line=document.createElement("p");line.textContent=`第${item.occurrence_index+1}次 · ${formatWorldTime(item.starts_at)} · ${appointmentStatuses[item.appointment_status]||({skipped:"过去时段未排程，已跳过",blocked:"条件不足，暂缓安排"}[item.scheduling_status])||"等待安排"}`;row.append(line);
    }
    const choose=(label,operation,statement)=>{const button=document.createElement("button");button.type="button";button.className="button secondary";button.textContent=label;
      button.addEventListener("click",()=>{if(current())submitAppointmentChoice(characterId,{operation,series_id:cycle.id,statement});});row.append(button);};
    if(cycle.status==="active"||cycle.status==="resume_pending")choose("暂停后续周期安排","pause_series","我想暂停后续的固定见面，给彼此留些空间。");
    if(cycle.status==="paused")choose("提出恢复周期安排","resume_series","我想恢复未来还没有经过的固定见面时段，愿意听听你的决定。");
    if(cycle.status==="resume_pending"&&cycle.resume_requested_by!==player.id)choose("同意恢复后续周期","resume_series","我也明确同意恢复未来还没经过的固定见面安排。");
    if(["pending","active","paused","resume_pending"].includes(cycle.status))choose("结束整份周期安排","end_series","我决定结束整份周期安排，已经发生的相处与经历仍然保留。");
    panel.append(row);
  }
}

const appointmentOriginalProfile=openCharacterProfile;
openCharacterProfile=async function(characterId){await appointmentOriginalProfile(characterId);await appendAppointmentProfile(characterId);};
const appointmentOriginalKnownProfile=openKnownProfile;
openKnownProfile=function(characterId){appointmentOriginalKnownProfile(characterId);appendAppointmentProfile(characterId);};
