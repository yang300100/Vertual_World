/* 哥哥，多人约定逐人回应；这里始终只提交玩家本人的意愿。 */
const groupJourneyStatuses = {pending:"等待逐人回应",accepted:"全体已接受，等待实际会合",active:"旅行中",paused:"已暂停",resume_pending:"等待留下者逐人恢复",completed:"已完成",ended:"已结束",expired:"期限已到",left:"你已退出"};
const groupJourneyDecisions = {pending:"尚未答复",accepted:"本人已接受",declined:"本人已婉拒"};
const groupJourneyLive = new Set(["pending","accepted","active","paused","resume_pending"]);
Object.assign(eventLabels, {
  "social.group_journey_proposed":"提出有限多人旅行", "social.group_journey_accepted":"一名成员接受旅行",
  "social.group_journey_declined":"一名成员婉拒旅行", "social.group_journey_follow":"本人明确选择跟随",
  "social.group_journey_withdraw_follow":"本人停止跟随", "social.group_journey_departed":"多人旅行实际出发",
  "social.group_journey_arrived":"多人旅行实际到站", "social.group_journey_paused":"多人旅行暂停",
  "social.group_journey_resumed":"一名成员同意恢复", "social.group_journey_left":"一名成员本人退出",
  "social.group_journey_completed":"多人旅行完成", "social.group_journey_ended":"多人旅行结束",
  "social.group_journey_expired":"多人旅行期限已到",
});
let groupJourneyContextKey = "", groupJourneyEpoch = 0, groupJourneyRead = null, groupJourneyReadAt = 0;
const groupJourneyActions = new Set(), groupJourneyDrafts = new Map();

function groupJourneyContext() {
  const worldId = state.worldId, playerId = currentPlayer()?.id;
  const key = worldId && playerId ? worldId + ":" + playerId : "";
  if (key !== groupJourneyContextKey) {
    if (groupJourneyRead) groupJourneyRead.invalidated = true;
    groupJourneyContextKey = key; groupJourneyEpoch += 1; groupJourneyReadAt = 0;
  }
  return {worldId,playerId,key,epoch:groupJourneyEpoch};
}
function groupJourneyCurrent(context) {
  const now = groupJourneyContext();
  return Boolean(context?.key) && context.key === now.key && context.epoch === now.epoch;
}
function groupJourneyText(parent, text, tag = "p") {
  const node = document.createElement(tag); node.textContent = text; parent.append(node); return node;
}
function groupJourneyField(parent, label, tag = "input") {
  const wrap = document.createElement("label"), input = document.createElement(tag);
  wrap.textContent = label; input.setAttribute("aria-label",label); wrap.append(input); parent.append(wrap); return input;
}
function groupJourneyOption(select, value, label) {
  const node = document.createElement("option"); node.value = value; node.textContent = label; select.append(node);
}
function groupJourneyMember(trip, playerId) { return (trip.members || []).find(member => member.character_id === playerId); }
function groupJourneyRecipient(trip, playerId) {
  const ids = (trip.members || []).map(member => member.character_id);
  return [trip.proposer_id,trip.leader_id,...ids].find(id => id && id !== playerId && ids.includes(id)) || null;
}
function groupJourneyContract(trip) {
  return JSON.stringify([trip.id,trip.proposer_id,trip.leader_id,trip.player_id,trip.title,trip.statement,trip.origin_id,
    trip.deadline_at,trip.separation_policy,trip.return_to_origin,(trip.members || []).map(member => member.character_id),
    (trip.stops || []).map(stop => [stop.stop_index,stop.location_id,stop.minutes,stop.is_return])]);
}
function groupJourneyVersion(trip) {
  return JSON.stringify([groupJourneyContract(trip),trip.status,trip.phase,trip.source_event_id,trip.next_index,
    trip.next_depart_at,trip.resume_epoch,trip.following,trip.can_follow,
    (trip.members || []).map(member => [member.character_id,member.decision,member.decision_at,member.left_at,member.resumed]),
    (trip.stops || []).map(stop => stop.arrived_at)]);
}
function groupJourneyAllowed(trip, playerId, operation) {
  const member = groupJourneyMember(trip,playerId);
  if (!member || !groupJourneyLive.has(trip.status)) return false;
  if (operation === "end") return trip.proposer_id === playerId;
  if (member.left_at) return false;
  if (operation === "leave") return trip.status !== "pending";
  if (operation === "accept" || operation === "decline") return trip.status === "pending" && member.decision === "pending";
  if (operation === "pause") return ["accepted","active","resume_pending"].includes(trip.status);
  if (operation === "resume") return ["paused","resume_pending"].includes(trip.status) && member.decision === "accepted" && !member.resumed;
  if (operation === "withdraw_follow") return trip.leader_id !== playerId && trip.following === true;
  if (operation === "follow") return trip.leader_id !== playerId && trip.can_follow === true && trip.following !== true && ["accepted","active"].includes(trip.status);
  if (operation === "depart") return trip.leader_id === playerId && ["accepted","active"].includes(trip.status) && trip.phase !== "moving" && trip.next_index < (trip.stops || []).length;
  return false;
}
function groupJourneyPolicyText(policy) {
  return policy === "continue" ?
    "成员本人退出后，若领队仍在且至少留下两人，其余人按原路线继续；退出者停止随队，保留真实位置和已走路程。领队退出会暂停，少于两人会结束。" :
    "任一成员本人退出后，其余人的旅行也暂停；留下者须逐人同意恢复，NPC 领队还需你重新选择跟随。领队退出后不能擅自换人，少于两人会结束。";
}
async function submitGroupJourney(context, recipientId, choice, options) {
  if (!groupJourneyCurrent(context)) return null;
  if (groupJourneyRead?.context.key === context.key) groupJourneyRead.invalidated = true;
  groupJourneyReadAt = 0;
  const nearby = (options?.members || []).some(member => member.id === recipientId);
  return executeNaturalSocialChoice(choice.statement, {
    target_character_id:recipientId, choice_evidence:choice.statement, group_journey_choice:choice,
  },nearby ? null : "letter");
}

function groupJourneyRow(trip, context, isCurrent = () => true) {
  const row = document.createElement("article"); row.className = "life-item group-journey-row"; row.dataset.groupJourneyId = trip.id;
  const current = () => row.isConnected && groupJourneyCurrent(context) && isCurrent();
  const self = groupJourneyMember(trip,context.playerId), npcLed = trip.leader_id !== context.playerId;
  groupJourneyText(row,(trip.title || "有限多人旅行") + " · " + (groupJourneyStatuses[trip.status] || "行程记录"),"strong");
  groupJourneyText(row,"集合：" + (trip.origin_name || "原约定地点") + " · 期限：" + formatWorldTime(trip.deadline_at));
  groupJourneyText(row,"领队：" + (npcLed ? trip.leader_name || "原约定成员" : "你") + "。每个人分别决定，不由一位成员代表全体答应。接受路线不等于选择跟随。");
  const members = document.createElement("ul"); members.className = "group-journey-members";
  for (const member of trip.members || []) {
    let label = (member.character_id === context.playerId ? "你" : member.name || "原约定成员") + "：" +
      (member.left_at ? "本人已退出" : groupJourneyDecisions[member.decision] || "答复记录");
    if (!member.left_at && ["paused","resume_pending"].includes(trip.status)) label += member.resumed ? " · 本人已同意本次恢复" : " · 尚未同意本次恢复";
    groupJourneyText(members,label,"li");
  }
  row.append(members);
  for (const stop of trip.stops || []) groupJourneyText(row,(stop.stop_index + 1) + ". " + (stop.name || "原约定地点") +
    (stop.is_return ? "（返程）" : "") + " · 停留 " + stop.minutes + " 世界分钟" +
    (stop.arrived_at ? " · 已实际到站 " + formatWorldTime(stop.arrived_at) : " · 尚未实际到站"));
  groupJourneyText(row,trip.statement || "");
  groupJourneyText(row,groupJourneyPolicyText(trip.separation_policy));
  if (trip.reason) groupJourneyText(row,trip.reason);
  if (trip.phase === "moving") groupJourneyText(row,"本程正在实际行走，尚未抵达；每个人保留自己的移动记录。");
  if (trip.phase === "stopped" && trip.next_depart_at) groupJourneyText(row,"本次停留至少至 " + formatWorldTime(trip.next_depart_at) + "，之后仍须满足出发条件。");
  if (trip.status === "left" || self?.left_at) groupJourneyText(row,"你已退出自己的安排，不会被原行程恢复或再次带走。其他成员此后的状态由他们各自处理。");
  else if (npcLed && groupJourneyLive.has(trip.status)) groupJourneyText(row,trip.following ?
    "你已明确选择跟随这份有限路线；这不代表已经出发或抵达。" :
    "你尚未选择跟随，或原跟随已随暂停撤销。全体接受或逐人恢复后，你仍须单独重新选择跟随。");
  const notice = groupJourneyText(row,""); notice.setAttribute("aria-live","polite");
  let stale = false;
  const controls = () => row.querySelectorAll("button[data-group-journey-action]");
  async function act(operation, statement) {
    if (!current() || stale || state.busy || state.responsePending || groupJourneyActions.has(context.key)) return;
    groupJourneyActions.add(context.key); controls().forEach(button => { button.disabled = true; });
    try {
      const [rows,options] = await Promise.all([
        api("/api/worlds/" + context.worldId + "/group-journeys"),
        api("/api/worlds/" + context.worldId + "/group-journeys/options"),
      ]);
      if (!current()) return;
      const latest = rows.find(item => item.id === trip.id);
      const stopping = ["pause","withdraw_follow","leave","end"].includes(operation);
      if (!latest || !groupJourneyAllowed(latest,context.playerId,operation) ||
          (stopping ? groupJourneyContract(latest) !== groupJourneyContract(trip) : groupJourneyVersion(latest) !== groupJourneyVersion(trip))) {
        stale = true; notice.textContent = "这份行程或逐人回应已变化，请先刷新记录，再作自己的决定。"; return;
      }
      const recipient = groupJourneyRecipient(latest,context.playerId);
      if (!recipient) { notice.textContent = "原名单中没有可索引的其他成员，请刷新记录。"; return; }
      const result = await submitGroupJourney(context,recipient,{operation,journey_id:latest.id,statement},options);
      if (!current()) return;
      if (result?.event_id) await renderMyGroupJourneys(true);
      else notice.textContent = "这次本人选择尚未确认保存，原记录已保留，请稍后核对并重试。";
    } catch {
      if (current()) notice.textContent = "暂时无法核对或保存本人选择，原记录已保留，请稍后重试。";
    } finally {
      groupJourneyActions.delete(context.key);
      if (current()) controls().forEach(button => { button.disabled = stale; });
    }
  }
  const choose = (label,operation,statement) => {
    if (!groupJourneyAllowed(trip,context.playerId,operation)) return;
    const button = document.createElement("button"); button.type = "button"; button.className = "button secondary";
    button.textContent = label; button.dataset.groupJourneyAction = operation;
    button.addEventListener("click",() => act(operation,statement)); row.append(button);
  };
  choose("仅我接受原完整行程","accept","我本人接受原记录中的完整名单、领队、集合点、期限、各站停留、返程与退出条款；不代其他成员答应，也不因此开始跟随。");
  choose("仅我婉拒这份行程","decline","这份完整行程我本人暂时不参加；这只是我的答复，不代表其他成员。");
  choose("我愿意跟随这份有限路线","follow","我已核对原完整名单、领队、路线、停留、返程、期限及退出条款，明确选择跟随原 NPC 领队；暂停后这次跟随即结束。");
  choose("撤回我的跟随并暂停","withdraw_follow","我本人现在撤回跟随并暂停原旅行，保留每个人的真实位置和已经走过的路程。");
  choose(trip.next_index ? "由我开始下一程" : "实际会合后由我出发","depart","我作为原领队本人选择开始已约定的下一程；仍核验留下者逐人同意、真实会合与各自道路条件，不代任何人瞬移。");
  choose("我要求暂停旅行","pause","我本人要求暂停这份旅行，保留每个人真实位置和已有结果；留下者恢复与我的重新跟随分别确认。");
  choose("仅我同意本次恢复","resume","我本人同意按原完整条款恢复，仍等待每位留下者独立同意；恢复不重新授予我的跟随，也不让已经退出的人重新加入。");
  choose("仅退出我自己的安排","leave","我本人退出这份有限旅行，按原退出条款处理其余行程；不代其他成员结束、同意或恢复，保留各自真实位置和已走路程。");
  choose("作为提出者结束整份约定","end","我作为原提出者明确结束整份有限旅行约定，保留所有人的真实位置、财物和已经发生的结果。");
  return row;
}

function appendGroupJourneyProposal(panel, options, context, isCurrent = () => true) {
  const form = document.createElement("form"); form.className = "long-term-form group-journey-proposal";
  form.dataset.groupJourneyProposal = "true";
  const current = () => form.isConnected && groupJourneyCurrent(context) && isCurrent();
  const candidates = (options.members || []).filter(member => member.id === context.playerId || !member.is_player);
  if (!candidates.some(member => member.id === context.playerId)) candidates.unshift({id:context.playerId,name:"你",is_player:true});
  const places = options.places || [], draft = groupJourneyDrafts.get(context.key) || {};
  const title = groupJourneyField(form,"多人旅行名称"); title.required = true; title.maxLength = 60; title.value = draft.title || "有限多人旅行";
  const memberBox = document.createElement("fieldset"); groupJourneyText(memberBox,"完整邀请名单（含你，共2至8人）","legend");
  const selected = new Set(draft.member_ids || [context.playerId]), checks = [];
  for (const member of candidates) {
    const checkbox = groupJourneyField(memberBox,member.id === context.playerId ? "你（提出者，本人意愿已包含在提议中）" : member.name || "眼前成员");
    checkbox.type = "checkbox"; checkbox.value = member.id; checkbox.dataset.groupJourneyMember = member.id;
    checkbox.checked = member.id === context.playerId || selected.has(member.id); checkbox.disabled = member.id === context.playerId;
    checks.push(checkbox);
  }
  const missingMembers = [...selected].filter(id => !candidates.some(member => member.id === id));
  for (const id of missingMembers) {
    const checkbox = groupJourneyField(memberBox,"原选成员（当前不在可邀范围，可取消后重新核对）");
    checkbox.type = "checkbox"; checkbox.value = id; checkbox.dataset.groupJourneyMember = id; checkbox.checked = true; checks.push(checkbox);
  }
  if (missingMembers.length) groupJourneyText(memberBox,"草稿中有原选成员已离开可邀范围，完整名单仍保留；请等待实际会合或明确取消该成员，再重新核对领队。");
  form.append(memberBox);
  const leader = groupJourneyField(form,"这次多人旅行由谁领路","select"); leader.required = true;
  const leaderHelp = groupJourneyText(form,"");
  const meeting = groupJourneyField(form,"多人旅行集合地点","select"); meeting.required = true;
  const fillPlace = (select,value) => {
    for (const place of places) groupJourneyOption(select,place.id,place.name);
    if (value && !places.some(place => place.id === value)) groupJourneyOption(select,value,"原选地点当前不可用于新提议");
    if (value) select.value = value;
  };
  fillPlace(meeting,draft.meeting_location_id);
  const routeBox = document.createElement("div"); form.append(routeBox); const stops = [];
  const addStop = values => {
    if (stops.length >= 6) return;
    const box = document.createElement("div"); box.className = "life-item";
    const place = groupJourneyField(box,"多人旅行第" + (stops.length + 1) + "站","select"); place.required = true;
    fillPlace(place,values?.location_id || places.find(item => item.id !== meeting.value)?.id);
    const minutes = groupJourneyField(box,"多人旅行第" + (stops.length + 1) + "站停留分钟");
    minutes.type = "number"; minutes.min = "0"; minutes.max = "120"; minutes.step = "1"; minutes.required = true; minutes.value = String(values?.minutes ?? 15);
    place.addEventListener("change",save); minutes.addEventListener("input",save);
    routeBox.append(box); stops.push({place,minutes,box});
  };
  const add = document.createElement("button"); add.type = "button"; add.className = "button secondary"; add.textContent = "添加多人旅行下一站（最多六站）";
  const remove = document.createElement("button"); remove.type = "button"; remove.className = "button secondary"; remove.textContent = "移除多人旅行最后一站";
  add.addEventListener("click",() => { addStop(); syncRoute(); save(); });
  remove.addEventListener("click",() => { if (stops.length > 1) stops.pop().box.remove(); syncRoute(); save(); }); form.append(add,remove);
  const back = groupJourneyField(form,"多人旅行包含返回集合地点"); back.type = "checkbox"; back.checked = draft.return_to_origin ?? true;
  const deadline = groupJourneyField(form,"多人旅行期限（世界时间 UTC）"); deadline.type = "datetime-local"; deadline.required = true;
  const worldAt = Date.parse(state.living?.world_time || "");
  deadline.value = draft.deadline || (Number.isFinite(worldAt) ? new Date(worldAt + 4 * 3600000).toISOString().slice(0,16) : "");
  const policy = groupJourneyField(form,"有人本人退出时，其余人的行程","select");
  groupJourneyOption(policy,"pause","任一人退出，其余人暂停（默认）"); groupJourneyOption(policy,"continue","领队仍在且至少两人时，其余人继续原路线"); policy.value = draft.separation_policy || "pause";
  const policyHelp = groupJourneyText(form,groupJourneyPolicyText(policy.value));
  const speech = groupJourneyField(form,"想怎样商量多人旅行","textarea"); speech.required = true; speech.maxLength = 120; speech.value = draft.statement || "";
  speech.placeholder = "写下你的原话，其他成员会分别决定是否参加。";
  const submit = document.createElement("button"); submit.type = "submit"; submit.className = "button primary"; submit.textContent = "提出完整行程，逐人听取回应"; form.append(submit);
  const notice = groupJourneyText(form,""); notice.setAttribute("aria-live","polite");
  const memberIds = () => checks.filter(input => input.checked).map(input => input.value);
  function save() {
    groupJourneyDrafts.set(context.key,{title:title.value,member_ids:memberIds(),leader_id:leader.value,meeting_location_id:meeting.value,
      stops:stops.map(stop => ({location_id:stop.place.value,minutes:stop.minutes.value})),return_to_origin:back.checked,
      deadline:deadline.value,separation_policy:policy.value,statement:speech.value});
  }
  form.groupJourneySaveDraft = save;
  function syncLeader(value = leader.value) {
    leader.replaceChildren();
    for (const member of candidates.filter(member => memberIds().includes(member.id))) groupJourneyOption(leader,member.id,member.id === context.playerId ? "由我领路，每程由我选择出发" : "请" + (member.name || "这位成员") + "领路");
    if (value && memberIds().includes(value) && !candidates.some(member => member.id === value)) groupJourneyOption(leader,value,"原选领队（当前不在可邀范围）");
    if (memberIds().includes(value)) leader.value = value;
    leaderHelp.textContent = leader.value === context.playerId ? "你只提出自己的意愿，其余每一位成员仍需本人接受。" : "请 NPC 领队也需本人接受；全体答应后，你仍须另行明确选择跟随，暂停后重新选择。";
  }
  function syncRoute() { add.disabled = stops.length >= 6; remove.disabled = stops.length <= 1; }
  for (const checkbox of checks) checkbox.addEventListener("change",() => { syncLeader(); save(); });
  for (const input of [title,speech,deadline]) input.addEventListener("input",save);
  for (const input of [meeting,back]) input.addEventListener("change",save);
  leader.addEventListener("change",() => { syncLeader(); save(); });
  policy.addEventListener("change",() => { policyHelp.textContent = groupJourneyPolicyText(policy.value); save(); });
  for (const stop of draft.stops?.length ? draft.stops : [{}]) addStop(stop);
  syncLeader(draft.leader_id); syncRoute();
  submit.disabled = candidates.length < 2 || !places.length;
  if (candidates.length < 2) groupJourneyText(form,"目前没有同一空间内20米可见的可邀成员；已有行程仍可在上方管理。");
  if (!places.length) groupJourneyText(form,"目前没有可用于新提议的实际到访地点。");
  groupJourneyText(form,"提议只包含你的意愿；其他人逐一答应前不能出发。食宿、费用与房间许可分别处理，不因组团而共享。");
  form.addEventListener("submit",async event => {
    event.preventDefault();
    if (!current() || state.busy || state.responsePending || submit.disabled || groupJourneyActions.has(context.key)) return;
    speech.setCustomValidity("");
    if (!form.reportValidity()) return;
    const ids = memberIds(), end = new Date(deadline.value + "Z");
    if (ids.length < 2 || ids.length > 8 || !ids.includes(context.playerId) || !ids.includes(leader.value) || !Number.isFinite(end.getTime()) ||
        stops.some(stop => !Number.isInteger(Number(stop.minutes.value)) || Number(stop.minutes.value) < 0 || Number(stop.minutes.value) > 120)) {
      notice.textContent = "请核对2至8人完整名单、名单中的领队、有效期限及每站0至120分钟停留。"; return;
    }
    const statement = speech.value.trim() + " 我提出此表内的完整名单、领队、集合点、各站停留、返程、期限与退出条款；其余成员各自决定。接受不等于跟随，暂停后重新授权，食宿费用另议。";
    if (statement.length > 240) { speech.setCustomValidity("请把原话缩短至120字以内，完整约定仍保留在表单中。"); form.reportValidity(); return; }
    const choice = {operation:"propose",title:title.value.trim(),member_ids:ids,leader_id:leader.value,meeting_location_id:meeting.value,
      deadline_world_time:end.toISOString(),stops:stops.map(stop => ({location_id:stop.place.value,minutes:Number(stop.minutes.value)})),
      return_to_origin:Boolean(back.checked),separation_policy:policy.value,statement};
    save(); groupJourneyActions.add(context.key);
    const inputs = form.querySelectorAll("input, select, textarea, button"); inputs.forEach(input => { input.disabled = true; });
    try {
      const [rows,latestOptions] = await Promise.all([
        api("/api/worlds/" + context.worldId + "/group-journeys"),
        api("/api/worlds/" + context.worldId + "/group-journeys/options"),
      ]);
      if (!current()) return;
      if (rows.some(trip => groupJourneyLive.has(trip.status) && !groupJourneyMember(trip,context.playerId)?.left_at)) {
        notice.textContent = "你已有尚未结束的多人旅行或待回应提议，请先处理上方原约定；草稿已保留。"; return;
      }
      const freshIds = new Set((latestOptions.members || []).map(member => member.id));
      const freshPlaces = new Set((latestOptions.places || []).map(place => place.id));
      if (latestOptions.player_id !== context.playerId || ids.some(id => !freshIds.has(id)) ||
          !freshPlaces.has(choice.meeting_location_id) || choice.stops.some(stop => !freshPlaces.has(stop.location_id))) {
        notice.textContent = "可邀成员或实际到访地点已变化，请刷新候选后核对；原话与草稿已保留。"; return;
      }
      const recipient = ids.find(id => id !== context.playerId);
      const result = await submitGroupJourney(context,recipient,choice,latestOptions);
      if (!current()) return;
      if (result?.event_id) { groupJourneyDrafts.delete(context.key); form.groupJourneySaveDraft = null; await renderMyGroupJourneys(true); }
      else notice.textContent = "这份提议尚未确认保存，原话与完整草稿已保留，请稍后核对并重试。";
    } catch {
      if (current()) notice.textContent = "暂时无法保存多人旅行，原话与完整草稿已保留，请稍后重试。";
    } finally {
      groupJourneyActions.delete(context.key);
      if (current()) {
        inputs.forEach(input => { input.disabled = false; }); checks.find(input => input.value === context.playerId).disabled = true;
        syncRoute(); submit.disabled = candidates.length < 2 || !places.length;
      }
    }
  });
  panel.append(form); return form;
}

async function renderMyGroupJourneys(force = false) {
  const context = groupJourneyContext();
  if (!context.key || !state.living) {
    if (groupJourneyRead) groupJourneyRead.invalidated = true;
    document.querySelector("#my-group-journeys")?.remove(); groupJourneyReadAt = 0; return;
  }
  let panel = document.querySelector("#my-group-journeys");
  if (!panel) {
    const anchor = document.querySelector("#my-journeys") || document.querySelector("#companion-party") || elements.sceneCharacters;
    if (!anchor) return;
    panel = document.createElement("section"); panel.id = "my-group-journeys"; panel.setAttribute("aria-labelledby","my-group-journeys-heading"); anchor.after(panel);
  }
  const token = context.key + ":" + context.epoch;
  if (panel.dataset.groupJourneyContext !== token) {
    panel.querySelector("form")?.groupJourneySaveDraft?.(); panel.dataset.groupJourneyContext = token;
    panel.replaceChildren(); groupJourneyText(panel,"我的多人旅行","h3").id = "my-group-journeys-heading";
    groupJourneyText(panel,"这里始终管理你本人参加过的有限行程。伙伴离开视野后，你仍可查看原约定、暂停或退出自己的安排；没有任何按钮代全体答应。");
    const refresh = document.createElement("button"); refresh.type = "button"; refresh.className = "button secondary"; refresh.textContent = "刷新多人旅行与可邀候选";
    refresh.addEventListener("click",() => { if (!state.busy && !state.responsePending && !groupJourneyActions.has(context.key)) return renderMyGroupJourneys(true); }); panel.append(refresh);
    const list = document.createElement("div"); list.className = "group-journey-list"; groupJourneyText(list,"正在读取本人参与的多人旅行……"); panel.append(list);
    const proposal = document.createElement("details"); proposal.className = "group-journey-proposal-box"; groupJourneyText(proposal,"商量一份新的多人有限行程","summary"); panel.append(proposal);
  }
  if (!force && (state.busy || state.responsePending || groupJourneyActions.has(context.key) || groupJourneyRead && !groupJourneyRead.invalidated)) return;
  if (!force && Date.now() - groupJourneyReadAt < 4000) return;
  if (groupJourneyRead) groupJourneyRead.invalidated = true;
  const request = {context,invalidated:false}; groupJourneyRead = request;
  const current = () => groupJourneyRead === request && !request.invalidated && panel.isConnected && panel.dataset.groupJourneyContext === token && groupJourneyCurrent(context);
  try {
    const [rows,options] = await Promise.all([
      api("/api/worlds/" + context.worldId + "/group-journeys"),
      api("/api/worlds/" + context.worldId + "/group-journeys/options"),
    ]);
    if (!current()) return;
    groupJourneyReadAt = Date.now(); panel.querySelector(".group-journey-read-error")?.remove();
    const list = panel.querySelector(".group-journey-list"), proposal = panel.querySelector(".group-journey-proposal-box");
    const live = rows.filter(trip => groupJourneyLive.has(trip.status)), shown = [...live,...rows.filter(trip => !groupJourneyLive.has(trip.status)).slice(0,2)];
    const signature = JSON.stringify(shown);
    if (list.dataset.signature !== signature) {
      list.dataset.signature = signature; list.replaceChildren();
      if (!shown.length) groupJourneyText(list,"目前没有已记录的多人旅行，可在下方明确邀请眼前成员。");
      for (const trip of shown) list.append(groupJourneyRow(trip,context,() => panel.isConnected && panel.dataset.groupJourneyContext === token));
    }
    const optionSignature = JSON.stringify(options);
    if (proposal.dataset.signature !== optionSignature || !proposal.querySelector("form")?.groupJourneySaveDraft) {
      proposal.querySelector("form")?.groupJourneySaveDraft?.(); proposal.dataset.signature = optionSignature;
      proposal.replaceChildren(); groupJourneyText(proposal,"商量一份新的多人有限行程","summary");
      appendGroupJourneyProposal(proposal,options,context,() => panel.isConnected && panel.dataset.groupJourneyContext === token);
    }
  } catch {
    if (current() && !panel.querySelector(".group-journey-read-error")) groupJourneyText(panel,"暂时无法刷新多人旅行，现有记录与草稿已保留，请稍后重试。").className = "group-journey-read-error muted";
  } finally { if (groupJourneyRead === request) groupJourneyRead = null; }
}

const groupJourneysOriginalLiving = renderLiving;
renderLiving = function() { groupJourneysOriginalLiving(); renderMyGroupJourneys(); };
