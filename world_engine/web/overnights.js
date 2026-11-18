/* 哥哥，长时住宿由双方单独确认，实际进屋后才开始各自的休息。 */
const overnightStatuses={pending:"等待对方回应",accepted:"双方已确认，等待实际入住",active:"正在休息",completed:"已完成住宿",declined:"已婉拒",cancelled:"已取消",expired:"未确认已过期",missed:"未在到场窗入住",interrupted:"住宿已中断"};
for(const [status,label] of Object.entries(overnightStatuses))eventLabels[`social.appointment_overnight_${status}`]=`长时住宿：${label}`;
Object.assign(eventLabels,{"social.appointment_overnight_proposed":"提出旅途住宿","social.appointment_overnight_started":"实际开始长时休息"});

function submitOvernight(characterId,choice) {
  const player=currentPlayer(),person=characterById(characterId);
  const near=person&&player&&canInviteCompanion(person)&&distanceKm(player.longitude,player.latitude,person.longitude,person.latitude)<=0.02;
  elements.characterDialog.close();
  return executeNaturalSocialChoice(choice.statement,{target_character_id:characterId,choice_evidence:choice.statement,overnight_choice:choice},near?null:"letter");
}

async function appendOvernights(characterId) {
  const player=currentPlayer();
  if(!player||player.id===characterId||document.querySelector("#overnights-profile"))return;
  const panel=document.createElement("section");panel.id="overnights-profile";
  const heading=document.createElement("h2");heading.textContent="商量旅途中的长时住宿";panel.append(heading);
  const note=document.createElement("p");note.textContent="在已实际到达的旅行站点，另行商量4–12小时的同房休息。双方确认后各自逐门进屋，由你在到场窗内选择开始；晚到仍休息完整约定时长。身体、照护和房间许可变化可能中断住宿，结束后另行决定是否恢复旅行。";panel.append(note);
  elements.characterProfile.append(panel);
  let stays,trips,options;
  try{[stays,trips,options]=await Promise.all([
    api(`/api/worlds/${state.worldId}/overnights?character_id=${encodeURIComponent(characterId)}`),
    api(`/api/worlds/${state.worldId}/journeys?character_id=${encodeURIComponent(characterId)}`),
    api(`/api/worlds/${state.worldId}/overnights/options/${encodeURIComponent(characterId)}`),
  ]);}
  catch{const error=document.createElement("p");error.textContent="暂时无法读取住宿安排，请稍后重新打开档案。";panel.append(error);return;}
  if(!panel.isConnected)return;
  const rooms=options.room_options||[];
  const text=(parent,value)=>{const line=document.createElement("p");line.textContent=value;parent.append(line);return line;};
  for(const stay of stays) {
    const row=document.createElement("div");row.className="life-item";
    text(row,`${overnightStatuses[stay.status]||stay.status} · ${stay.location_name} / ${stay.room_name} · 休息${stay.minutes}分钟（${Number((stay.minutes/60).toFixed(2))}小时）`);
    text(row,`原旅行第${stay.stop_index+1}站 · 到场窗 ${formatWorldTime(stay.arrival_starts_at)} 至 ${formatWorldTime(stay.arrival_ends_at)} · 最晚结束 ${formatWorldTime(stay.latest_end_at)}`);
    if(stay.started_at)text(row,`实际开始 ${formatWorldTime(stay.started_at)}${stay.ends_at?` · 约定休息至 ${formatWorldTime(stay.ends_at)}`:""}；实际结果以住宿状态为准。`);
    text(row,`${stay.proposer_id===player.id?"你的邀请":"对方的邀请"}：${stay.statement}${stay.response?` / ${stay.response}`:""}`);
    if(stay.visit_host_id)text(row,`${stay.visit_host_id===player.id?"你":"对方"}已明确允许本次房间接待；双方确认、时间有效且接待者在场时才可进门。`);
    if(stay.reason)text(row,stay.reason);
    const choose=(label,operation,statement,extra={})=>{const button=document.createElement("button");button.type="button";button.className="button secondary";button.textContent=label;button.addEventListener("click",()=>submitOvernight(characterId,{operation,stay_id:stay.id,statement,...extra}));row.append(button);return button;};
    if(stay.status==="pending") {
      if(stay.recipient_id===player.id) {
        choose("接受这份住宿安排","accept","我接受这份住宿记录的房间、到场窗和休息时长；双方各自实际进屋后，再由我选择开始休息。");
        if(rooms.some(room=>room.id===stay.room_id&&room.may_host))choose("接受，并明确允许本次接待","accept","我接受这份住宿安排，并另行同意本次住宿时间内接待你进入这间房间；双方仍各自进屋，之后由我选择开始休息。",{allow_visit:true});
        choose("婉拒这份住宿邀请","decline","谢谢你的邀请，这份住宿安排我暂时不接受，原旅行也不自动恢复。");
      } else choose("撤回我的住宿邀请","cancel","我先撤回这份住宿邀请，原旅行仍另行商量是否恢复。");
    }
    if(stay.status==="accepted")choose("双方实际进屋后开始住宿","check_in","我已实际进入约定房间，选择在到场窗内开始双方已确认的完整时长休息。");
    if(["accepted","active"].includes(stay.status))choose(stay.status==="active"?"结束本次住宿":"取消这份住宿安排","cancel","我决定取消或结束这份住宿，保留已经实际发生的休息与经历，原旅行不自动恢复。");
    panel.append(row);
  }
  const eligible=trips.filter(trip=>["active","paused"].includes(trip.status)&&trip.phase==="stopped"&&trip.next_index>0&&trip.stops?.[trip.next_index-1]?.arrived_at);
  if(!eligible.length){text(panel,"先与对方确认有限行程并实际到站，再商量该站的长时住宿。原旅行期限需要覆盖最晚入住及完整休息时长。");return;}
  const form=document.createElement("form");form.className="long-term-form";
  const field=(label,el)=>{const wrap=document.createElement("label");wrap.textContent=label;el.setAttribute("aria-label",label);wrap.append(el);form.append(wrap);return el;};
  const option=(el,value,label)=>{const item=document.createElement("option");item.value=value;item.textContent=label;el.append(item);};
  const journey=field("已实际到站的原旅行",document.createElement("select"));journey.required=true;option(journey,"","请选择已到站的旅行");
  for(const trip of eligible)option(journey,trip.id,`${trip.title} · 第${trip.next_index}站 ${trip.stops[trip.next_index-1].name} · 期限 ${formatWorldTime(trip.deadline_at)}`);
  const room=field("本次住宿的真实房间",document.createElement("select"));room.required=true;option(room,"","请先选择原旅行");
  const arrival=field("到场窗开始（世界时间 UTC）",document.createElement("input"));arrival.type="datetime-local";arrival.required=true;
  const now=Date.parse(state.living?.world_time||"");
  if(Number.isFinite(now)){arrival.min=new Date(now+60000).toISOString().slice(0,16);arrival.max=new Date(now+86400000).toISOString().slice(0,16);arrival.value=new Date(now+30*60000).toISOString().slice(0,16);}
  const windowMinutes=field("到场窗长度（15–120世界分钟）",document.createElement("input"));windowMinutes.type="number";windowMinutes.min="15";windowMinutes.max="120";windowMinutes.step="1";windowMinutes.value="60";windowMinutes.required=true;
  const minutes=field("完整休息时长（240–720世界分钟）",document.createElement("input"));minutes.type="number";minutes.min="240";minutes.max="720";minutes.step="1";minutes.value="480";minutes.required=true;
  const allowVisit=field("我另行同意在本次住宿时间内接待对方进入所选房间",document.createElement("input"));allowVisit.type="checkbox";allowVisit.disabled=true;
  const speech=field("想怎样商量住宿",document.createElement("textarea"));speech.required=true;speech.maxLength=80;speech.placeholder="说说自己的想法，给对方保留拒绝的空间。";
  const timing=text(form,"");timing.setAttribute("aria-live","polite");
  const help=text(form,"房租请先按住房协商流程办理，进入许可须覆盖最晚入住至完整休息结束。这里不代订房或付费，也不自动用餐。同房休息只确认这一项安排；身体接触、储物和长期居住分别征得同意。");
  const submit=document.createElement("button");submit.type="submit";submit.className="button primary";submit.textContent="提出住宿安排，听听对方的决定";submit.disabled=true;form.append(submit);
  const syncTiming=()=>{const start=Date.parse(arrival.value+":00Z"),trip=eligible.find(item=>item.id===journey.value);const end=start+(Number(windowMinutes.value)+Number(minutes.value))*60000;
    timing.textContent=Number.isFinite(end)?`最晚入住后完整休息至 ${formatWorldTime(new Date(end).toISOString())}。${trip?`原旅行期限 ${formatWorldTime(trip.deadline_at)}。`:""}`:"请填写明确的到场时间窗和休息时长。";};
  const syncRoom=()=>{const chosen=rooms.find(item=>item.id===room.value);allowVisit.checked=false;allowVisit.disabled=!chosen?.may_host;submit.disabled=!journey.value||!chosen;};
  const syncJourney=()=>{const trip=eligible.find(item=>item.id===journey.value),locationId=trip?.stops[trip.next_index-1]?.location_id;room.replaceChildren();option(room,"",trip?"请选择该站的住宿房间":"请先选择原旅行");
    const available=rooms.filter(item=>item.location_id===locationId);for(const item of available)option(room,item.id,`${item.location_name} · ${item.name}`);
    help.textContent=trip&&!available.length?"该站暂无双方可用的真实房间。请先取得完整时段的租住或接待许可，再重新打开档案商量住宿。":"房租请先按住房协商流程办理，进入许可须覆盖最晚入住至完整休息结束。这里不代订房或付费，也不自动用餐。同房休息只确认这一项安排；身体接触、储物和长期居住分别征得同意。";
    syncRoom();syncTiming();};
  journey.addEventListener("change",syncJourney);room.addEventListener("change",syncRoom);
  for(const input of [arrival,windowMinutes,minutes])input.addEventListener("input",syncTiming);
  syncJourney();
  form.addEventListener("submit",event=>{event.preventDefault();if(!form.reportValidity())return;
    const trip=eligible.find(item=>item.id===journey.value),chosen=rooms.find(item=>item.id===room.value),start=new Date(arrival.value+":00Z");
    if(!trip||!chosen||chosen.location_id!==trip.stops[trip.next_index-1].location_id||!Number.isFinite(start.getTime()))return;
    const duration=Number(minutes.value),window=Number(windowMinutes.value);
    const statement=speech.value.trim()+` 我提出先暂停原旅行，在表单选定的同站房间于约定到场窗入住，共同休息${duration}个世界分钟；双方各自进屋后由我选择开始，结束后另行商量恢复旅行。`+(allowVisit.checked?" 我另行同意本次住宿时间内接待你进入这间房间。":"");
    submitOvernight(characterId,{operation:"propose",journey_id:trip.id,room_id:chosen.id,arrival_world_time:start.toISOString(),window_minutes:window,minutes:duration,allow_visit:allowVisit.checked,statement});
  });
  panel.append(form);
}
const overnightsOriginalProfile=openCharacterProfile;
openCharacterProfile=async function(characterId){await overnightsOriginalProfile(characterId);await appendOvernights(characterId);};
const overnightsOriginalKnownProfile=openKnownProfile;
openKnownProfile=function(characterId){overnightsOriginalKnownProfile(characterId);appendOvernights(characterId);};
