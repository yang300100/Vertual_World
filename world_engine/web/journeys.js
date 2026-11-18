/* 哥哥，路线同意与跟随分开确认；每个人仍保留真实位置和随时暂停的选择。 */
const journeyStatuses = {pending:"等待回应",accepted:"已确认，等待实际会合",active:"旅行中",paused:"已暂停",resume_pending:"等待对方同意恢复",completed:"已完成全部路线与停留",declined:"已婉拒",ended:"已结束",expired:"期限已到"};
for (const [key, value] of Object.entries(journeyStatuses)) eventLabels["social.appointment_journey_" + key] = "共同旅行：" + value;
Object.assign(eventLabels, {
  "social.appointment_journey_proposed":"提出共同旅行",
  "social.appointment_journey_departed":"开始本程",
  "social.appointment_journey_arrived":"实际到站",
  "social.appointment_journey_follow":"明确选择跟随领队",
  "social.appointment_journey_withdraw_follow":"停止跟随并暂停",
});
const journeyLiveStatuses = new Set(["pending","accepted","active","paused","resume_pending"]);
let journeyContextKey = "", journeyContextEpoch = 0, journeyProfileRequest = null;
let journeyLivingRequest = null, journeyLivingReadAt = 0, journeyLivingSignature = "";
const journeyActions = new Set(), journeyPauseDrafts = new Map();

function journeyText(parent, text) {
  const node = document.createElement("p"); node.textContent = text; parent.append(node); return node;
}
function journeyPersonName(characterId) {
  if (currentPlayer()?.id === characterId) return "你";
  return state.living?.people?.find(person => person.id === characterId)?.name ||
    state.living?.known_people?.find(person => person.id === characterId)?.name || "尚未认识的人";
}
function journeyContext() {
  const playerId = currentPlayer()?.id, worldId = state.worldId;
  const key = worldId && playerId ? worldId + ":" + playerId : "";
  if (key !== journeyContextKey) {
    if (journeyLivingRequest) journeyLivingRequest.invalidated = true;
    journeyContextKey = key; journeyContextEpoch += 1; journeyProfileRequest = null;
    journeyLivingReadAt = 0; journeyLivingSignature = ""; journeyPauseDrafts.clear();
  }
  return {key, worldId, playerId, epoch:journeyContextEpoch};
}
function journeyContextIsCurrent(context) {
  const current = journeyContext();
  return Boolean(context?.key) && current.key === context.key && current.epoch === context.epoch;
}
function journeyProfileIsCurrent(request) {
  return request && journeyProfileRequest === request && journeyContextIsCurrent(request) && elements.characterDialog.open;
}
function invalidateJourneyReads(context) {
  if (journeyLivingRequest?.context.key === context.key) journeyLivingRequest.invalidated = true;
  if (journeyContextIsCurrent(context)) journeyLivingReadAt = 0;
}
function journeyLeader(trip, playerId) {
  return trip.leader_id || playerId;
}
function journeyIsFollowing(trip, playerId) {
  return journeyLiveStatuses.has(trip.status) && (trip.following === true ||
    trip.following === undefined && trip.follow_authorized_by === playerId);
}
function journeyVersion(trip, playerId) {
  return JSON.stringify({
    id:trip.id, proposer:trip.proposer_id, recipient:trip.recipient_id, leader:journeyLeader(trip,playerId),
    status:trip.status, phase:trip.phase, origin:trip.origin_id, deadline:trip.deadline_at,
    index:trip.next_index, depart:trip.next_depart_at, remaining:trip.remaining_stop_seconds,
    resume:trip.resume_requested_by, source:trip.source_event_id,
    following:journeyIsFollowing(trip,playerId), followBy:trip.follow_authorized_by,
    followAt:trip.follow_authorized_at, followSource:trip.follow_source_event_id,
    canFollow:trip.can_follow,
    stops:(trip.stops || []).map(stop => [stop.stop_index,stop.location_id,stop.minutes,stop.is_return,stop.arrived_at]),
    breaks:(trip.breaks || []).map(pause => [pause.moment_id,pause.status]),
  });
}
function journeySameContract(first, second, playerId) {
  const terms = trip => JSON.stringify({id:trip.id,proposer:trip.proposer_id,recipient:trip.recipient_id,
    leader:journeyLeader(trip,playerId),origin:trip.origin_id,deadline:trip.deadline_at,
    stops:(trip.stops || []).map(stop => [stop.location_id,stop.minutes,stop.is_return])});
  return terms(first) === terms(second);
}
async function readJourney(context, otherId, journeyId) {
  const rows = await api("/api/worlds/" + context.worldId + "/journeys?character_id=" + encodeURIComponent(otherId));
  return rows.find(trip => trip.id === journeyId) || null;
}
function journeyOperationAllowed(trip, playerId, operation) {
  if (operation === "accept" || operation === "decline") return trip.status === "pending" && trip.recipient_id === playerId;
  if (operation === "end") return trip.status === "pending" ? trip.proposer_id === playerId : ["accepted","active","paused","resume_pending"].includes(trip.status);
  if (operation === "depart") return journeyLeader(trip,playerId) === playerId && ["accepted","active"].includes(trip.status) && trip.phase !== "moving" && trip.next_index < (trip.stops || []).length;
  if (operation === "follow") return journeyLeader(trip,playerId) !== playerId && trip.can_follow === true && ["accepted","active"].includes(trip.status);
  if (operation === "withdraw_follow") return journeyLeader(trip,playerId) !== playerId && journeyIsFollowing(trip,playerId);
  if (operation === "pause") return ["active","resume_pending"].includes(trip.status);
  if (operation === "resume") return trip.status === "paused" || trip.status === "resume_pending" && trip.resume_requested_by !== playerId;
  return false;
}

async function submitJourney(characterId, choice) {
  const context = journeyContext(), player = currentPlayer(), person = characterById(characterId);
  const near = person && player && canInviteCompanion(person) &&
    distanceKm(player.longitude,player.latitude,person.longitude,person.latitude) <= 0.02;
  invalidateJourneyReads(context);
  const result = await executeNaturalSocialChoice(choice.statement, {
    target_character_id:characterId, choice_evidence:choice.statement, journey_choice:choice,
  }, near ? null : "letter");
  if (journeyContextIsCurrent(context)) {
    journeyLivingReadAt = 0;
    await renderMyJourneys(true);
  }
  return result;
}

function journeyRow(trip, player, otherId, isCurrent, closeProfile = false) {
  const context = journeyContext(), row = document.createElement("article"); row.className = "life-item";
  const leaderId = journeyLeader(trip,player.id), npcLed = leaderId !== player.id;
  const rowIsCurrent = () => row.isConnected && journeyContextIsCurrent(context) && isCurrent();
  const lockKey = context.key + ":" + trip.id; let stale = false;
  journeyText(row,(trip.title || "共同旅行") + " · " + (journeyStatuses[trip.status] || trip.status) +
    " · 集合 " + (trip.origin_name || "约定地点") + " · 期限 " + formatWorldTime(trip.deadline_at));
  journeyText(row,"本次领队：" + (npcLed ? trip.leader_name || journeyPersonName(leaderId) : "你") +
    (npcLed ? "。接受行程后，你仍需另行明确选择是否跟随；可随时停止跟随并暂停。" : "。每一程仍由你选择出发。"));
  for (const stop of trip.stops || []) {
    journeyText(row,String(stop.stop_index + 1) + ". " + stop.name + (stop.is_return ? "（返程）" : "") +
      " · 停留" + stop.minutes + "分钟" + (stop.arrived_at ? " · 已实际到站 " + formatWorldTime(stop.arrived_at) : " · 尚未到站"));
  }
  journeyText(row,(trip.statement || "") + (trip.response ? " / " + trip.response : ""));
  if (trip.reason) journeyText(row,trip.reason);
  for (const pause of trip.breaks || []) {
    journeyText(row,"第" + (pause.stop_index + 1) + "站的" + (pause.kind === "meal" ? "共同用餐" : "一起休息") +
      " · " + ({pending:"等待回应",active:"正在进行",completed:"已完成",declined:"已婉拒",cancelled:"已撤回",interrupted:"已中止"}[pause.status] || pause.status) +
      " · " + pause.minutes + "分钟；不会自动恢复旅行。");
  }
  if (trip.phase === "stopped" && trip.next_depart_at) {
    journeyText(row,["paused","resume_pending"].includes(trip.status) ?
      "停留计时已暂停，恢复后还需约" + Math.ceil((trip.remaining_stop_seconds || 0) / 60) + "分钟。" :
      "本次停留至少至 " + formatWorldTime(trip.next_depart_at) + "。");
  }
  if (npcLed && journeyLiveStatuses.has(trip.status) && trip.status !== "pending") {
    journeyText(row,journeyIsFollowing(trip,player.id) ?
      "你已明确选择按这份有限路线跟随。领队仍须按真实集合、身体、日程和道路条件出发；接受不保证已经抵达。" :
      "你尚未选择跟随，或上次跟随已因暂停结束。双方恢复原旅行后，也需要你重新明确选择跟随。");
  }
  const notice = journeyText(row,""); notice.setAttribute("aria-live","polite");
  const buttons = () => row.querySelectorAll("button[data-journey-action]");
  async function act(operation, statement, action) {
    if (!rowIsCurrent() || stale || state.busy || state.responsePending || journeyActions.has(lockKey)) return null;
    journeyActions.add(lockKey); buttons().forEach(button => { button.disabled = true; });
    try {
      const latest = await readJourney(context,otherId,trip.id);
      if (!rowIsCurrent()) return null;
      const stopping = ["withdraw_follow","pause","end"].includes(operation);
      if (!latest || (stopping ? !journeySameContract(latest,trip,player.id) : journeyVersion(latest,player.id) !== journeyVersion(trip,player.id)) ||
          operation && !journeyOperationAllowed(latest,player.id,operation)) {
        stale = true; notice.textContent = "这份行程的状态或跟随选择已变化，请重新查看后再作决定。";
        await renderMyJourneys(true); return null;
      }
      const result = action ? await action(latest) : await submitJourney(otherId,{operation,journey_id:latest.id,statement});
      if (rowIsCurrent() && result?.event_id && closeProfile) elements.characterDialog.close();
      else if (rowIsCurrent() && !result?.event_id) notice.textContent = "这次选择尚未确认保存，请查看现有行程后重试。";
      return result;
    } catch {
      if (rowIsCurrent()) notice.textContent = "暂时无法核对或提交行程，原约定按已有记录继续处理，请稍后重试。";
      return null;
    } finally {
      journeyActions.delete(lockKey);
      if (rowIsCurrent()) buttons().forEach(button => { button.disabled = stale; });
    }
  }
  function choose(label, operation, statement) {
    if (!journeyOperationAllowed(trip,player.id,operation)) return;
    const button = document.createElement("button"); button.type = "button"; button.className = "button secondary";
    button.textContent = label; button.dataset.journeyAction = operation;
    button.addEventListener("click",() => act(operation,statement)); row.append(button);
  }
  if (trip.status === "pending") {
    choose("接受这份具体行程","accept",npcLed ?
      "我接受这份记录的集合点、各站停留、返程、期限及由你领路的安排；是否开始跟随，我会另行明确选择。" :
      "我接受这份记录的集合点、各站停留、返程和期限，由我领路；实际会合后再选择出发。");
    choose("婉拒这份旅行","decline","谢谢你愿意同行，但这份行程我暂时不参加。");
    choose("撤回旅行提议","end","我先撤回这份共同旅行提议。");
  }
  if (!npcLed) choose(trip.started_at ? "开始下一程" : "会合后实际出发","depart","我已实际会合，愿意按已确认行程开始下一程，不改变路线或代对方花费。");
  if (npcLed) {
    choose("我愿意按这份路线跟随","follow","我已核对这份有限路线、各站停留、返程及期限，明确愿意跟随你领路；我可以随时停止跟随并暂停。");
    choose("停止跟随并暂停","withdraw_follow","我现在撤回这份行程的跟随选择并暂停，保留各自真实位置和已经走过的路程。");
  }
  choose("暂停旅行，先处理眼前需要","pause","我想先暂停共同旅行，保留真实位置和已走路程。");
  choose("提出恢复这份行程","resume",npcLed ?
    "我愿意按原行程恢复，仍等待你本人明确同意；恢复后我会再独立决定是否重新跟随。" :
    "我愿意按原行程恢复，仍等待你本人明确同意。");
  choose("结束这份共同旅行","end","我决定结束这份共同旅行，保留各自当前位置、财物和已发生的实际结果。");
  if (["active","paused"].includes(trip.status) && trip.phase === "stopped" &&
      !(trip.breaks || []).some(pause => ["pending","active"].includes(pause.status))) {
    const form = document.createElement("form"); form.className = "long-term-form";
    const draftKey = context.key + ":" + trip.id, draft = journeyPauseDrafts.get(draftKey) || {minutes:"15",supply:"each_own"};
    const field = (label,input) => { const wrap = document.createElement("label"); wrap.textContent = label; input.setAttribute("aria-label",label); wrap.append(input); form.append(wrap); return input; };
    const minutes = field("到站用餐或休息时长（世界分钟）",document.createElement("input"));
    minutes.type = "number"; minutes.min = "1"; minutes.max = "120"; minutes.step = "1"; minutes.value = draft.minutes; minutes.required = true;
    const supply = field("到站用餐供餐安排",document.createElement("select"));
    for (const [value,text] of [["each_own","各自吃一份自己的随身食物"],["proposer","我提供双方各一份自己的食物"]]) {
      const option = document.createElement("option"); option.value = value; option.textContent = text; supply.append(option);
    }
    supply.value = draft.supply;
    const save = () => journeyPauseDrafts.set(draftKey,{minutes:minutes.value,supply:supply.value});
    minutes.addEventListener("input",save); supply.addEventListener("change",save);
    for (const [kind,label] of [["meal","暂停行程，邀请到站用餐"],["rest","暂停行程，邀请到站休息"]]) {
      const button = document.createElement("button"); button.type = "button"; button.className = "button secondary";
      button.textContent = label; button.dataset.journeyAction = kind;
      button.addEventListener("click",async () => {
        if (!rowIsCurrent() || !form.reportValidity()) return;
        const duration = Number(minutes.value);
        if (!Number.isInteger(duration) || duration < 1 || duration > 120) return;
        save();
        const text = "我选择先暂停原旅行，在已经实际到达的第" + trip.next_index + "站邀请你" +
          (kind === "meal" ? "共同用餐" : "一起休息") + duration + "个世界分钟。" +
          (kind === "meal" ? supply.value === "proposer" ? "我愿意提供双方各一份自己的随身食物。" : "双方各自准备一份自己的随身食物。" : "") +
          "这只是邀请，不代表你已同意；结束后也不自动恢复旅行。";
        await act(null,text,async latest => {
          if (!["active","paused"].includes(latest.status) || latest.phase !== "stopped" ||
              (latest.breaks || []).some(pause => ["pending","active"].includes(pause.status))) return null;
          return submitSharedLife(otherId,{operation:"invite",kind,minutes:duration,journey_id:latest.id,meal_supply:kind === "meal" ? supply.value : "each_own",statement:text});
        });
      });
      form.append(button);
    }
    row.append(form);
  }
  return row;
}

async function appendJourneys(characterId, profileRequest) {
  const player = currentPlayer();
  if (!player || player.id === characterId || !journeyProfileIsCurrent(profileRequest) || document.querySelector("#journeys-profile")) return;
  const panel = document.createElement("section"); panel.id = "journeys-profile";
  const heading = document.createElement("h2"); heading.textContent = "一起走一段旅程"; panel.append(heading);
  journeyText(panel,"双方先确认有限路线、领队与期限。由你领路时每程自己选择出发；请对方领路时，接受行程后仍要另行明确选择跟随。暂停会结束本次跟随，恢复后重新决定。食宿、费用及房间许可分别商量。");
  elements.characterProfile.append(panel);
  let trips, places, plans;
  try {
    [trips,places,plans] = await Promise.all([
      api("/api/worlds/" + profileRequest.worldId + "/journeys?character_id=" + encodeURIComponent(characterId)),
      api("/api/worlds/" + profileRequest.worldId + "/journeys/options"),
      api("/api/worlds/" + profileRequest.worldId + "/shared-plans?character_id=" + encodeURIComponent(characterId)),
    ]);
  } catch {
    if (panel.isConnected && journeyProfileIsCurrent(profileRequest)) journeyText(panel,"暂时无法读取行程，请稍后重新打开档案。");
    return;
  }
  const isCurrent = () => panel.isConnected && journeyProfileIsCurrent(profileRequest);
  if (!isCurrent()) return;
  for (const trip of trips) panel.append(journeyRow(trip,player,characterId,isCurrent,true));
  appendJourneyProposal(panel,places,plans,characterId,player,isCurrent);
}

function appendJourneyProposal(panel, places, plans, characterId, player, isCurrent) {
  const context = journeyContext(), form = document.createElement("form"); form.className = "long-term-form"; form.dataset.journeyProposal = "true";
  const field = (label,input) => { const wrap = document.createElement("label"); wrap.textContent = label; input.setAttribute("aria-label",label); wrap.append(input); form.append(wrap); return input; };
  const option = (select,value,text) => { const item = document.createElement("option"); item.value = value; item.textContent = text; select.append(item); };
  const title = field("旅行名称",document.createElement("input")); title.maxLength = 60; title.value = "附近的共同散步"; title.required = true;
  const leader = field("这次希望由谁领路",document.createElement("select")); leader.required = true;
  option(leader,player.id,"由我领路，每程由我选择出发"); option(leader,characterId,"请对方领路，我另行决定是否跟随"); leader.value = player.id;
  const help = journeyText(form,""); help.setAttribute("aria-live","polite");
  const updateLeader = () => { help.textContent = leader.value === player.id ?
    "这份提议由你领路，对方仍需独立决定是否参加。" :
    "这份提议请对方领路。对方接受后，你仍须核对完整路线并另行明确选择跟随，才能开始由对方带路。"; };
  leader.addEventListener("change",updateLeader); updateLeader();
  const meeting = field("旅行集合地点",document.createElement("select")); meeting.required = true;
  for (const place of places) option(meeting,place.id,place.name);
  const routeBox = document.createElement("div"); form.append(routeBox); const stops = [];
  const addStop = () => {
    if (stops.length >= 6) return;
    const box = document.createElement("div"); box.className = "life-item";
    const label = document.createElement("label"); label.textContent = "旅行第" + (stops.length + 1) + "站";
    const place = document.createElement("select"); place.required = true; place.setAttribute("aria-label",label.textContent);
    for (const item of places) option(place,item.id,item.name);
    if (places.length > 1) place.value = places.find(item => item.id !== meeting.value).id;
    label.append(place); box.append(label);
    const timeLabel = document.createElement("label"); timeLabel.textContent = "第" + (stops.length + 1) + "站停留分钟";
    const minutes = document.createElement("input"); minutes.type = "number"; minutes.min = "0"; minutes.max = "120"; minutes.step = "1"; minutes.value = "15"; minutes.required = true;
    minutes.setAttribute("aria-label",timeLabel.textContent); timeLabel.append(minutes); box.append(timeLabel);
    routeBox.append(box); stops.push({place,minutes,box});
  };
  addStop();
  const add = document.createElement("button"); add.type = "button"; add.className = "button secondary"; add.textContent = "添加下一站（最多六站）";
  add.addEventListener("click",() => { addStop(); add.disabled = stops.length >= 6; }); form.append(add);
  const remove = document.createElement("button"); remove.type = "button"; remove.className = "button secondary"; remove.textContent = "移除最后一站";
  remove.addEventListener("click",() => { if (stops.length > 1) stops.pop().box.remove(); add.disabled = false; }); form.append(remove);
  const back = field("行程包含返回集合地点",document.createElement("input")); back.type = "checkbox"; back.checked = true;
  const deadline = field("旅行期限（世界时间 UTC）",document.createElement("input")); deadline.type = "datetime-local"; deadline.required = true;
  const at = Date.parse(state.living?.world_time || "");
  if (Number.isFinite(at)) deadline.value = new Date(at + 4 * 3600000).toISOString().slice(0,16);
  const source = field("已经完成的筹备来源（可选）",document.createElement("select")); option(source,"","另行商量的独立行程");
  for (const plan of plans.filter(plan => plan.status === "completed")) option(source,plan.id,plan.title);
  const speech = field("想怎样商量旅行",document.createElement("textarea")); speech.required = true; speech.maxLength = 100; speech.placeholder = "说明目的，也给对方拒绝和调整的空间。";
  const submit = document.createElement("button"); submit.type = "submit"; submit.className = "button primary"; submit.textContent = "提出行程，听听对方的决定"; submit.disabled = places.length < 2; form.append(submit);
  const notice = journeyText(form,""); notice.setAttribute("aria-live","polite");
  if (places.length < 2) journeyText(form,"目前不足两个实际认识的地点，可以先探索附近，再商量有限行程。");
  const lockKey = context.key + ":proposal:" + characterId;
  form.addEventListener("submit",async event => {
    event.preventDefault();
    if (!isCurrent() || !journeyContextIsCurrent(context) || state.busy || state.responsePending || submit.disabled || journeyActions.has(lockKey)) return;
    speech.setCustomValidity("");
    if (!form.reportValidity()) return;
    const end = new Date(deadline.value + ":00Z"), leaderId = leader.value;
    if (!Number.isFinite(end.getTime()) || ![player.id,characterId].includes(leaderId) ||
        stops.some(stop => !Number.isInteger(Number(stop.minutes.value)) || Number(stop.minutes.value) < 0 || Number(stop.minutes.value) > 120)) return;
    const text = speech.value.trim() + " 我明确提出表单中记录的集合点、各站及停留，" +
      (back.checked ? "包含返程" : "不包含返程") + "，在所选期限内徒步完成；" +
      (leaderId === player.id ? "由我领路，每程由我选择出发。" : "请你领路，我会在接受行程后另行明确决定是否跟随。") +
      "双方仍各自同意，食宿费用另行商量。";
    if (text.length > 240) { speech.setCustomValidity("请简短说明旅行想法，保留表单中的完整约定。"); form.reportValidity(); return; }
    journeyActions.add(lockKey); submit.disabled = true;
    const inputs = [title,leader,meeting,back,deadline,source,speech,add,remove,...stops.flatMap(stop => [stop.place,stop.minutes])];
    inputs.forEach(input => { input.disabled = true; });
    try {
      const rows = await api("/api/worlds/" + context.worldId + "/journeys?character_id=" + encodeURIComponent(characterId));
      if (!isCurrent() || !journeyContextIsCurrent(context)) return;
      if (rows.some(trip => trip.status === "pending")) {
        notice.textContent = "双方已有待回应行程，请先查看并处理现有提议。"; return;
      }
      const result = await submitJourney(characterId,{operation:"propose",title:title.value.trim(),leader_id:leaderId,
        meeting_location_id:meeting.value,deadline_world_time:end.toISOString(),
        stops:stops.map(stop => ({location_id:stop.place.value,minutes:Number(stop.minutes.value)})),
        return_to_origin:back.checked,preparation_plan_id:source.value || null,statement:text});
      if (!isCurrent() || !journeyContextIsCurrent(context)) return;
      if (result?.event_id) elements.characterDialog.close();
      else notice.textContent = "这份行程尚未确认保存，表单内容已保留，请查看现有记录后重试。";
    } catch {
      if (isCurrent() && journeyContextIsCurrent(context)) notice.textContent = "暂时无法保存行程，表单内容已保留，请稍后重试。";
    } finally {
      journeyActions.delete(lockKey);
      if (isCurrent() && journeyContextIsCurrent(context)) {
        inputs.forEach(input => { input.disabled = false; }); submit.disabled = places.length < 2;
      }
    }
  });
  panel.append(form); return form;
}

async function renderMyJourneys(force = false) {
  const context = journeyContext(), player = currentPlayer();
  if (!context.key || !state.living || !player) {
    if (journeyLivingRequest) journeyLivingRequest.invalidated = true;
    document.querySelector("#my-journeys")?.remove(); journeyLivingReadAt = 0; journeyLivingSignature = ""; return;
  }
  let panel = document.querySelector("#my-journeys");
  if (!panel) {
    const anchor = document.querySelector("#companion-party") || elements.sceneCharacters;
    if (!anchor) return;
    panel = document.createElement("section"); panel.id = "my-journeys"; panel.setAttribute("aria-labelledby","my-journeys-heading"); anchor.after(panel);
  }
  if (panel.dataset.journeyContext !== context.key + ":" + context.epoch) {
    panel.dataset.journeyContext = context.key + ":" + context.epoch;
    const heading = document.createElement("h3"); heading.id = "my-journeys-heading"; heading.textContent = "我的共同旅行";
    panel.replaceChildren(heading); journeyText(panel,"正在读取本人参与的行程……");
  }
  if (!force && (state.busy || state.responsePending || journeyLivingRequest?.context.key === context.key && !journeyLivingRequest.invalidated)) return;
  if (!force && Date.now() - journeyLivingReadAt < 4000) return;
  if (journeyLivingRequest) journeyLivingRequest.invalidated = true;
  const request = {context,invalidated:false}; journeyLivingRequest = request;
  try {
    const trips = await api("/api/worlds/" + context.worldId + "/journeys");
    if (journeyLivingRequest !== request || request.invalidated || !panel.isConnected || !journeyContextIsCurrent(context)) return;
    journeyLivingReadAt = Date.now(); panel.querySelector(".journey-read-error")?.remove();
    const live = trips.filter(trip => journeyLiveStatuses.has(trip.status));
    const shown = live.length ? live : trips.slice(0,1);
    const signature = JSON.stringify(shown);
    if (signature === journeyLivingSignature) return;
    journeyLivingSignature = signature;
    const heading = document.createElement("h3"); heading.id = "my-journeys-heading"; heading.textContent = "我的共同旅行"; panel.replaceChildren(heading);
    journeyText(panel,"这里只办理你本人的行程与跟随选择。接受路线和开始跟随分开确认；暂停后重新决定，旅行不代办食宿或花费。");
    if (!shown.length) journeyText(panel,"目前没有已记录的共同旅行。可以从人物档案商量有限路线和领队。");
    for (const trip of shown) {
      const otherId = trip.other_id || (trip.proposer_id === player.id ? trip.recipient_id : trip.proposer_id);
      panel.append(journeyRow(trip,player,otherId,() => panel.isConnected && journeyContextIsCurrent(context)));
    }
  } catch {
    if (journeyLivingRequest === request && !request.invalidated && panel.isConnected && journeyContextIsCurrent(context) && !panel.querySelector(".journey-read-error")) {
      journeyText(panel,"暂时无法刷新共同旅行，现有记录可能变化，请稍后重试。").className = "journey-read-error muted";
    }
  } finally { if (journeyLivingRequest === request) journeyLivingRequest = null; }
}

const journeysOriginalProfile = openCharacterProfile;
openCharacterProfile = async function(characterId) {
  const request = {...journeyContext(),characterId}; journeyProfileRequest = request;
  await journeysOriginalProfile(characterId);
  if (journeyProfileIsCurrent(request)) await appendJourneys(characterId,request);
};
const journeysOriginalKnownProfile = openKnownProfile;
openKnownProfile = async function(characterId) {
  const request = {...journeyContext(),characterId}; journeyProfileRequest = request;
  await journeysOriginalKnownProfile(characterId);
  if (journeyProfileIsCurrent(request)) await appendJourneys(characterId,request);
};
const journeysOriginalLiving = renderLiving;
renderLiving = function() { journeysOriginalLiving(); renderMyJourneys(); };
