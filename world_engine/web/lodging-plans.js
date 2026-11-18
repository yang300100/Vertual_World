/* 哥哥，各自的钱和订房选择只由本人处理，界面不把旅伴的预算当作付款许可。 */
const lodgingPlanStatuses = {
  pending: "等待对方回应", accepted: "各自办理订房", completed: "双方已回站报告准备好",
  declined: "已婉拒", cancelled: "已退出筹备", expired: "筹备已过期",
};
Object.assign(eventLabels, {
  "social.appointment_lodging_plan_proposed": "提出各自预算的订房筹备",
  "social.appointment_lodging_plan_accepted": "接受本人预算与订房分工",
  "social.appointment_lodging_plan_quoted": "收到本人房间报价",
  "social.appointment_lodging_plan_reviewed": "本人向房主询问住房条件",
  "social.lodging_plan_reviewed": "本人向房主询问住房条件",
  "social.lodging_plan_booked": "本人实际确认订房",
  "social.appointment_lodging_plan_booked": "本人实际确认订房",
  "social.appointment_lodging_plan_ready": "本人回站报告订房就绪",
  "social.appointment_lodging_plan_completed": "双方订房筹备完成",
  "social.appointment_lodging_plan_cancelled": "退出订房筹备",
});

function lodgingLine(parent, value) {
  const line = document.createElement("p"); line.textContent = value; parent.append(line); return line;
}

async function submitLodgingPlan(characterId, choice) {
  const player = currentPlayer(), person = characterById(characterId);
  if (lodgingLivingRequest?.context === `${state.worldId}:${player?.id}`) lodgingLivingRequest.invalidated = true;
  const near = person && player && canInviteCompanion(person) &&
    distanceKm(player.longitude, player.latitude, person.longitude, person.latitude) <= 0.02;
  elements.characterDialog.close();
  await executeNaturalSocialChoice(choice.statement, {
    target_character_id: characterId, choice_evidence: choice.statement, lodging_plan_choice: choice,
  }, near ? null : "letter");
  lodgingLivingReadAt = 0;
  await renderMyLodgingPlans(true);
}

function lodgingPlanRow(plan, player, otherId, viewIsCurrent = () => true) {
  const rowWorldId = state.worldId;
  const row = document.createElement("article"); row.className = "life-item";
  const rowIsCurrent = () => row.isConnected && state.worldId === rowWorldId && currentPlayer()?.id === player.id && viewIsCurrent();
  lodgingLine(row, `${lodgingPlanStatuses[plan.status] || plan.status} · ${plan.location_name || "原旅行站点"}`);
  lodgingLine(row, `回到原站室外报告就绪：${formatWorldTime(plan.ready_by_at || plan.ready_by_world_time)}之前；各自房间许可须覆盖至 ${formatWorldTime(plan.required_until_at || plan.required_until_world_time)}。`);
  const roles = Array.isArray(plan.roles) ? plan.roles : [];
  const own = roles.find(role => role.character_id === player.id);
  for (const role of roles) {
    const self = role.character_id === player.id;
    const progress = {awaiting: "待本人办理", offer_ready: "本人已有报价", blocked: "本人需先处理订房条件", booked: "本人已取得许可", ready: "本人已回站报告就绪", unreported: "尚未向你报告就绪"};
    lodgingLine(row, `${self ? "你" : "对方"}负责 ${role.room_name || "约定房间"}，本人总支出上限 ${role.spending_limit}枚；${self ? progress[role.status] || role.status : role.status === "ready" ? "已向你报告就绪" : "尚未向你报告就绪"}。`);
  }
  if (plan.statement) lodgingLine(row, `提议：${plan.statement}${plan.response ? ` / 回应：${plan.response}` : ""}`);
  if (own && own.spent !== undefined) lodgingLine(row, `本人本次已实际支出 ${own.spent}枚；退出筹备会保留已付租约和费用。`);
  if (own?.private_reason) lodgingLine(row, own.private_reason);
  if (plan.reason) lodgingLine(row, plan.reason);
  const choose = (label, choice) => {
    const button = document.createElement("button"); button.type = "button";
    button.className = "button secondary"; button.textContent = label;
    button.dataset.lodgingAction = choice.operation;
    button.addEventListener("click", async () => {
      if (state.busy || state.responsePending || button.disabled || !rowIsCurrent()) return;
      const panel = row.closest("section") || row;
      panel.querySelectorAll("button[data-lodging-action]").forEach(item => { item.disabled = true; });
      try { await submitLodgingPlan(otherId, {plan_id: plan.id, ...choice}); }
      finally { panel.querySelectorAll("button[data-lodging-action]").forEach(item => { item.disabled = false; }); }
    });
    row.append(button); return button;
  };
  if (plan.status === "pending") {
    if (plan.recipient_id === player.id) {
      choose("接受本人预算与订房分工", {operation: "accept", statement: "我接受这份筹备记录中的各自房间、各自预算和两项期限，只授权本人预算内办理自己的房间，保留生活储备，不替你付费或同意后续休息。"});
      choose("婉拒这份订房筹备", {operation: "decline", statement: "谢谢你的提议，我暂时不接受这份订房预算与分工安排。"});
    }
    if (plan.proposer_id === player.id) choose("撤回我的订房提议", {operation: "cancel", statement: "我撤回这份订房筹备提议，已经发生的租约和费用保留，原旅行另议。"});
  }
  if (plan.status === "accepted" && own) {
    const quote = own.quote;
    if (quote && own.quote_id && quote.status === "open" && Date.parse(quote.expires_at) > Date.parse(state.living?.world_time || "")) {
      const total = Number(quote.total);
      lodgingLine(row, `本人房间的准确报价：${Number.isFinite(total) ? total : "待重新核对"}枚${quote.duration_days ? `，租期${quote.duration_days}日` : ""}${quote.expires_at ? `，报价有效至 ${formatWorldTime(quote.expires_at)}` : ""}。`);
      if (Number.isFinite(total)) choose(`确认本人支付准确报价 ${total}枚`, {
        operation: "confirm", quote_id: own.quote_id, expected_total: total,
        statement: `我已核对本人房间这份有效报价，明确确认从本人钱包支付准确总额${total}枚，严格绑定这份报价；不替另一人订房或付款。`,
      });
    }
    if (own.status !== "ready") {
      choose(quote ? "重新询问本人房间报价" : "向房主询问本人房间报价", {operation: "quote", statement: "我选择向真实房主询问这份筹备中本人房间覆盖约定期限的准确报价，这一步尚不付款。"});
      choose("在原站室外报告本人已准备好", {operation: "report_ready", statement: "我已取得本人约定房间覆盖完整期限的合法许可，并实际回到原旅行站点室外，明确报告本人已准备好；不代你报告。"});
    }
    choose("退出本人订房筹备", {operation: "cancel", statement: "我退出这份订房筹备，保留已经发生的租约和实际费用，不自动退款或恢复旅行。"});
  }
  if (plan.status === "completed") {
    lodgingLine(row, "双方已各自报告准备好。请另行商量分房休息与醒后会合；此记录不自动进屋、开始休息或恢复旅行。");
    appendLodgingStayHandoff(row, plan, player, otherId, rowIsCurrent);
  }
  return row;
}

/* 哥哥，已订好的房间只用于复核新邀请；打开表单也不会替任何一方同意休息。 */
function lodgingHandoffHasLiveStay(stays, planId) {
  return stays.some(stay => stay.source_lodging_plan_id === planId && (
    ["pending", "accepted", "active"].includes(stay.status) ||
    stay.status === "completed" && (stay.reunion_status === "waiting" || stay.can_end_reunion_wait)
  ));
}

function appendLodgingStayHandoff(row, originalPlan, player, otherId, rowIsCurrent) {
  const worldId = state.worldId;
  const button = document.createElement("button"); button.type = "button"; button.className = "button secondary";
  button.textContent = "用这次订房商量分房休息"; button.setAttribute("aria-expanded", "false");
  button.dataset.lodgingHandoff = "open"; row.append(button);
  let attempt = null, form = null;
  const fresh = async () => {
    const [plans, trips, stays] = await Promise.all([
      api(`/api/worlds/${worldId}/lodging-plans?character_id=${encodeURIComponent(otherId)}`),
      api(`/api/worlds/${worldId}/journeys?character_id=${encodeURIComponent(otherId)}`),
      api(`/api/worlds/${worldId}/separate-stays?character_id=${encodeURIComponent(otherId)}`),
    ]);
    const plan = plans.find(item => item.id === originalPlan.id);
    const trip = plan && trips.find(item => item.id === plan.journey_id);
    const own = plan?.roles?.find(role => role.character_id === player.id);
    const other = plan?.roles?.find(role => role.character_id === otherId);
    const pair = plan && [plan.proposer_id, plan.recipient_id];
    const stop = trip?.stops?.[plan?.stop_index];
    if (!plan || plan.status !== "completed" || !pair.includes(player.id) || !pair.includes(otherId) ||
        !trip || !["active", "paused"].includes(trip.status) || trip.phase !== "stopped" ||
        trip.next_index !== plan.stop_index + 1 || !stop?.arrived_at || stop.location_id !== plan.location_id ||
        !own?.room_id || !other?.room_id || own.room_id === other.room_id) {
      return {reason: "这次订房对应的旅行、到站位置或准备状态已经变化，请重新查看现有安排。"};
    }
    if (lodgingHandoffHasLiveStay(stays, plan.id)) return {reason: "这次订房已有正在商量或执行的分房与会合安排，请先查看并处理现有安排。"};
    return {plan, trip, own, other};
  };
  button.addEventListener("click", async () => {
    if (!rowIsCurrent() || state.busy || state.responsePending || button.disabled || attempt) return;
    if (form) { form.remove(); form = null; button.setAttribute("aria-expanded", "false"); return; }
    const request = {}; attempt = request; button.disabled = true;
    const note = lodgingLine(row, "正在重新核对这次订房和原旅行……"); note.setAttribute("aria-live", "polite");
    try {
      const details = await fresh();
      if (attempt !== request || !rowIsCurrent()) return;
      if (details.reason) { note.textContent = details.reason; return; }
      note.remove();
      const {plan, trip, own, other} = details;
      let currentDetails = details, blockedReason = "";
      form = document.createElement("form"); form.className = "long-term-form";
      form.dataset.lodgingHandoff = "form";
      const currentForm = form;
      const formIsCurrent = () => rowIsCurrent() && form === currentForm && currentForm.isConnected;
      lodgingLine(form, `沿用本次各自订好的房间：你住 ${own.room_name || "自己的约定房间"}；对方住 ${other.room_name || "对方的约定房间"}。`);
      lodgingLine(form, `最晚完整休息结束须在 ${formatWorldTime(plan.required_until_at)}之前；会合须在原旅行期限 ${formatWorldTime(trip.deadline_at)}之前完成。房间许可会在提出和接受时重新核对。`);
      const field = (label, input) => {
        const wrap = document.createElement("label"); wrap.textContent = label; input.setAttribute("aria-label", label);
        input.required = true; wrap.append(input); currentForm.append(wrap); return input;
      };
      const arrival = field("本次分房入住窗开始（世界时间 UTC）", document.createElement("input")); arrival.type = "datetime-local";
      const windowMinutes = field("本次入住窗长度（15–120世界分钟）", document.createElement("input"));
      const minutes = field("本次各自完整休息时长（240–720世界分钟）", document.createElement("input"));
      const reunionMinutes = field("本次休息后会合窗长度（15–120世界分钟）", document.createElement("input"));
      for (const [input, min, max, value] of [[windowMinutes, 15, 120, 60], [minutes, 240, 720, 480], [reunionMinutes, 15, 120, 60]]) {
        input.type = "number"; input.min = String(min); input.max = String(max); input.step = "1"; input.value = String(value);
      }
      const speech = field("这次想对对方说的话", document.createElement("textarea")); speech.maxLength = 80;
      speech.placeholder = "请明确说出这次的想法，对方仍可调整或拒绝。";
      const timing = lodgingLine(form, "请填写明确的入住开始时间，并复核休息与会合时间。 "); timing.setAttribute("aria-live", "polite");
      lodgingLine(form, "这是一次新的分房休息与会合邀请。对方需另行决定；接受后仍各自进屋、开始自己的休息，原旅行另行商量恢复。");
      const submit = document.createElement("button"); submit.type = "submit"; submit.className = "button primary";
      submit.textContent = "我已复核，提出这次分房休息与会合"; submit.dataset.lodgingHandoff = "submit"; form.append(submit);
      const sync = (latest = currentDetails) => {
        const now = Date.parse(state.living?.world_time || ""), start = Date.parse(`${arrival.value}Z`);
        const window = Number(windowMinutes.value), duration = Number(minutes.value), reunion = Number(reunionMinutes.value);
        const restEnd = start + (window + duration) * 60000, reunionEnd = restEnd + reunion * 60000;
        const permitEnd = Date.parse(latest.plan.required_until_at), deadline = Date.parse(latest.trip.deadline_at);
        const validNumbers = [[window, 15, 120], [duration, 240, 720], [reunion, 15, 120]].every(([value, min, max]) => Number.isInteger(value) && value >= min && value <= max);
        const lastStart = Math.min(permitEnd - (window + duration) * 60000, deadline - (window + duration + reunion) * 60000, now + 86400000);
        if (Number.isFinite(now)) arrival.min = new Date(now + 60000).toISOString().slice(0, 16);
        if (Number.isFinite(lastStart)) arrival.max = new Date(lastStart).toISOString().slice(0, 16);
        let reason = blockedReason;
        if (!reason && (!Number.isFinite(now) || !Number.isFinite(permitEnd) || !Number.isFinite(deadline))) reason = "暂时无法核对世界时间和住宿期限，请重新打开安排。";
        else if (!reason && Number.isFinite(start) && (start <= now || start > now + 86400000)) reason = "请选当前世界时间之后、24个世界小时以内的入住开始时间。";
        else if (!reason && Number.isFinite(restEnd) && restEnd > permitEnd) reason = "最晚入住后的完整休息会超出这次房间许可覆盖期限，请调整时间。";
        else if (!reason && Number.isFinite(reunionEnd) && reunionEnd > deadline) reason = "休息后的会合会超出原旅行期限，请调整时间。";
        arrival.setCustomValidity(reason); speech.setCustomValidity(speech.value.trim() ? "" : "请亲自填写这次想说的话。");
        timing.textContent = reason || (Number.isFinite(reunionEnd) && validNumbers ?
          `最晚入住后完整休息至 ${formatWorldTime(new Date(restEnd).toISOString())}；会合窗 ${formatWorldTime(new Date(restEnd).toISOString())} 至 ${formatWorldTime(new Date(reunionEnd).toISOString())}，地点为 ${latest.plan.location_name || "原旅行站点"}的室外场景。` : "请填写明确的入住开始时间和完整时长。");
        submit.disabled = Boolean(attempt) || !formIsCurrent() || !validNumbers || !Number.isFinite(start) || !speech.value.trim() || Boolean(reason);
        return !reason && validNumbers && Number.isFinite(start) && Boolean(speech.value.trim());
      };
      for (const input of [arrival, windowMinutes, minutes, reunionMinutes, speech]) input.addEventListener("input", () => sync());
      currentForm.addEventListener("submit", async event => {
        event.preventDefault();
        if (!formIsCurrent() || state.busy || state.responsePending || attempt || !sync() || !currentForm.reportValidity()) return;
        const operation = {}; attempt = operation; submit.disabled = true; button.disabled = true;
        const inputs = [arrival, windowMinutes, minutes, reunionMinutes, speech]; inputs.forEach(input => { input.disabled = true; });
        let retryNotice = "";
        try {
          const latest = await fresh();
          if (attempt !== operation || !formIsCurrent()) return;
          if (latest.reason) { blockedReason = latest.reason; timing.textContent = latest.reason; return; }
          currentDetails = latest;
          if (latest.own.room_id !== own.room_id || latest.other.room_id !== other.room_id || !sync(latest)) {
            if (latest.own.room_id !== own.room_id || latest.other.room_id !== other.room_id) blockedReason = "各自房间已有变化，请重新打开安排后复核。";
            return;
          }
          if (state.busy || state.responsePending) { retryNotice = "当前行动仍在处理中，请完成后重新核对并提交。"; return; }
          const duration = Number(minutes.value), reunion = Number(reunionMinutes.value);
          const statement = speech.value.trim() + ` 我重新提出各住本次订好的自己的房间，在所填入住窗各自休息${duration}分钟，再按${reunion}分钟会合窗回原站室外。你可以拒绝；各自入住，原旅行另议。`;
          if (lodgingLivingRequest?.context === `${worldId}:${player.id}`) lodgingLivingRequest.invalidated = true;
          await submitSeparateStay(otherId, {operation: "propose", source_lodging_plan_id: plan.id, journey_id: plan.journey_id,
            own_room_id: own.room_id, other_room_id: other.room_id, arrival_world_time: new Date(`${arrival.value}Z`).toISOString(),
            window_minutes: Number(windowMinutes.value), minutes: duration, reunion_window_minutes: reunion, statement});
          if (formIsCurrent()) { currentForm.remove(); form = null; button.setAttribute("aria-expanded", "false"); }
          lodgingLivingReadAt = 0;
          if (state.worldId === worldId && currentPlayer()?.id === player.id) {
            await renderMyLodgingPlans(true);
          }
        } catch {
          retryNotice = "暂时无法核对或提交，请查看已有分房安排后重试。";
        } finally {
          if (attempt === operation) attempt = null;
          if (formIsCurrent()) { inputs.forEach(input => { input.disabled = false; }); sync(); if (retryNotice) timing.textContent = retryNotice; }
          if (rowIsCurrent()) button.disabled = false;
        }
      });
      row.append(form); button.setAttribute("aria-expanded", "true");
      attempt = null; sync();
    } catch {
      if (rowIsCurrent()) note.textContent = "暂时无法核对订房与旅行，请稍后重试。";
    } finally {
      if (attempt === request) attempt = null;
      if (rowIsCurrent()) button.disabled = false;
    }
  });
}

let lodgingProfileContext = null;

function lodgingProfileIsCurrent(request) {
  return request && lodgingProfileContext === request && state.worldId === request.worldId &&
    currentPlayer()?.id === request.playerId && elements.characterDialog.open;
}

async function appendLodgingPlans(characterId, profileRequest) {
  const player = currentPlayer();
  if (!lodgingProfileIsCurrent(profileRequest) || !player || player.id === characterId || document.querySelector("#lodging-plans-profile")) return;
  const worldId = profileRequest.worldId;
  const panel = document.createElement("section"); panel.id = "lodging-plans-profile";
  const heading = document.createElement("h2"); heading.textContent = "各自预算的住宿筹备与订房"; panel.append(heading);
  lodgingLine(panel, "实际到站后，可以商量各自负责一间真实房间和各自总预算。对方分别决定是否接受；你核对本人准确报价后确认付款，NPC 在自己的预算与生活储备内办理自己的房间。两人回到原站室外分别报告就绪，才算筹备完成。");
  elements.characterProfile.append(panel);
  let plans, trips, options;
  try {
    [plans, trips, options] = await Promise.all([
      api(`/api/worlds/${worldId}/lodging-plans?character_id=${encodeURIComponent(characterId)}`),
      api(`/api/worlds/${worldId}/journeys?character_id=${encodeURIComponent(characterId)}`),
      api(`/api/worlds/${worldId}/lodging-plans/options/${encodeURIComponent(characterId)}`),
    ]);
  } catch { if (panel.isConnected && lodgingProfileIsCurrent(profileRequest)) lodgingLine(panel, "暂时无法读取订房筹备，请稍后重新打开档案。"); return; }
  if (!panel.isConnected || !lodgingProfileIsCurrent(profileRequest)) return;
  for (const plan of plans) panel.append(lodgingPlanRow(plan, player, characterId, () => lodgingProfileIsCurrent(profileRequest)));
  const eligible = trips.filter(trip => ["active", "paused"].includes(trip.status) && trip.phase === "stopped" && trip.next_index > 0 && trip.stops?.[trip.next_index - 1]?.arrived_at);
  if (!eligible.length) { lodgingLine(panel, "先与对方确认有限行程并实际到站，再商量各自订房。"); return; }
  const rooms = options.room_options || [];
  const form = document.createElement("form"); form.className = "long-term-form";
  const field = (label, input) => {
    const wrap = document.createElement("label"); wrap.textContent = label;
    input.setAttribute("aria-label", label); wrap.append(input); form.append(wrap); input.required = true; return input;
  };
  const option = (select, value, text) => { const item = document.createElement("option"); item.value = value; item.textContent = text; select.append(item); };
  const journey = field("订房筹备的已到站原旅行", document.createElement("select"));
  option(journey, "", "请选择实际已到站的旅行");
  for (const trip of eligible) option(journey, trip.id, `${trip.title} · ${trip.stops[trip.next_index - 1].name} · 期限 ${formatWorldTime(trip.deadline_at)}`);
  const ownRoom = field("我自己负责订的房间", document.createElement("select"));
  const otherRoom = field("提议由对方自己订的房间", document.createElement("select"));
  const ownBudget = field("我本人订房总支出上限（0–1000枚）", document.createElement("input"));
  const otherBudget = field("提议对方本人订房总支出上限（0–1000枚）", document.createElement("input"));
  for (const input of [ownBudget, otherBudget]) { input.type = "number"; input.min = "0"; input.max = "1000"; input.step = "1"; }
  const readyBy = field("各自回到原站室外报告就绪的最晚时间（世界时间 UTC）", document.createElement("input"));
  const until = field("各自房间许可必须覆盖至（世界时间 UTC）", document.createElement("input"));
  const now = Date.parse(state.living?.world_time || "");
  for (const input of [readyBy, until]) { input.type = "datetime-local"; if (Number.isFinite(now)) input.min = new Date(now + 60000).toISOString().slice(0, 16); }
  const speech = field("想怎样商量各自的订房预算", document.createElement("textarea")); speech.maxLength = 60;
  const help = lodgingLine(form, "预算是本人支出上限，不是房价。先询价再确认准确付款；已有完整许可可以报告就绪。接待、食物、进入彼此房间与后续休息分别同意。");
  help.setAttribute("aria-live", "polite");
  const submit = document.createElement("button"); submit.type = "submit"; submit.className = "button primary"; submit.textContent = "提出各自预算与订房分工，听听对方决定"; form.append(submit);
  const sync = () => {
    const same = ownRoom.value && ownRoom.value === otherRoom.value;
    otherRoom.setCustomValidity(same ? "请选择两间独立且不同的房间。" : "");
    const ready = Date.parse(readyBy.value + ":00Z"), end = Date.parse(until.value + ":00Z");
    until.setCustomValidity(Number.isFinite(ready) && Number.isFinite(end) && end <= ready ? "房间许可须覆盖报告就绪之后的完整住宿时段。" : "");
    submit.disabled = !journey.value || !ownRoom.value || !otherRoom.value || Boolean(same);
  };
  journey.addEventListener("change", () => {
    const trip = eligible.find(item => item.id === journey.value), locationId = trip?.stops[trip.next_index - 1]?.location_id;
    for (const select of [ownRoom, otherRoom]) {
      select.replaceChildren(); option(select, "", trip ? "请选择该站已知真实房间" : "请先选择原旅行");
      for (const room of rooms.filter(item => item.location_id === locationId)) option(select, room.id, room.name);
    }
    if (trip) { readyBy.max = new Date(trip.deadline_at).toISOString().slice(0, 16); until.max = readyBy.max; }
    sync();
  });
  for (const input of [ownRoom, otherRoom, readyBy, until]) input.addEventListener("input", sync);
  sync();
  form.addEventListener("submit", event => {
    event.preventDefault(); if (!lodgingProfileIsCurrent(profileRequest)) return;
    sync(); if (!form.reportValidity() || submit.disabled) return;
    const trip = eligible.find(item => item.id === journey.value);
    const own = rooms.find(room => room.id === ownRoom.value), other = rooms.find(room => room.id === otherRoom.value);
    if (!trip || !own || !other) return;
    const statement = speech.value.trim() + ` 我提出暂停原旅行，各自订表单中指定给自己的独立房间；我本人总预算${ownBudget.value}枚，你本人总预算${otherBudget.value}枚。各自取得覆盖所填期限的许可，按所填期限回原站室外报告就绪；报价各自准确确认，保留生活储备，不代付，后续休息另议。`;
    submitLodgingPlan(characterId, {operation: "propose", journey_id: trip.id, own_room_id: own.id, other_room_id: other.id,
      own_budget: Number(ownBudget.value), other_budget: Number(otherBudget.value), ready_by_world_time: new Date(readyBy.value + ":00Z").toISOString(),
      required_until_world_time: new Date(until.value + ":00Z").toISOString(), statement});
  });
  panel.append(form);
}

const lodgingPlansOriginalProfile = openCharacterProfile;
openCharacterProfile = async function(characterId) {
  const request = {worldId: state.worldId, playerId: currentPlayer()?.id, characterId}; lodgingProfileContext = request;
  await lodgingPlansOriginalProfile(characterId);
  if (lodgingProfileIsCurrent(request)) await appendLodgingPlans(characterId, request);
};
const lodgingPlansOriginalKnownProfile = openKnownProfile;
openKnownProfile = function(characterId) {
  const request = {worldId: state.worldId, playerId: currentPlayer()?.id, characterId}; lodgingProfileContext = request;
  lodgingPlansOriginalKnownProfile(characterId); appendLodgingPlans(characterId, request);
};

/* 哥哥，本人订房可以从场景办理，不要求站到旅伴身边才看自己的报价。 */
let lodgingLivingReadAt = 0;
let lodgingLivingRequest = null;
let lodgingLivingContext = "";
let lodgingLivingSignature = "";

async function renderMyLodgingPlans(force = false) {
  const player = currentPlayer(), worldId = state.worldId;
  if (lodgingProfileContext && (lodgingProfileContext.worldId !== worldId || lodgingProfileContext.playerId !== player?.id)) lodgingProfileContext = null;
  if (!player || !state.living || !worldId) {
    if (lodgingLivingRequest) lodgingLivingRequest.invalidated = true;
    document.querySelector("#my-lodging-plans")?.remove(); lodgingLivingContext = ""; lodgingLivingSignature = ""; lodgingLivingReadAt = 0; return;
  }
  const context = `${worldId}:${player.id}`;
  let panel = document.querySelector("#my-lodging-plans");
  if (!panel) {
    const anchor = document.querySelector("#my-separate-stays") || document.querySelector("#interior-panel"); if (!anchor) return;
    panel = document.createElement("section"); panel.id = "my-lodging-plans"; panel.setAttribute("aria-labelledby", "my-lodging-plans-heading"); anchor.after(panel);
  }
  if (lodgingLivingContext !== context) {
    // 哥哥，离开一个世界后，其旧回包即使在切回来时到达，也不能覆盖后来重读的安排。
    if (lodgingLivingRequest) lodgingLivingRequest.invalidated = true;
    lodgingLivingContext = context; lodgingLivingReadAt = 0; lodgingLivingSignature = "";
    const heading = document.createElement("h3"); heading.id = "my-lodging-plans-heading"; heading.textContent = "我的订房筹备";
    panel.replaceChildren(heading); lodgingLine(panel, "正在读取本人订房筹备……");
  }
  if (lodgingLivingRequest?.context === context) {
    if (force) { lodgingLivingRequest.invalidated = true; await lodgingLivingRequest.done; if (state.worldId === worldId && currentPlayer()?.id === player.id) return renderMyLodgingPlans(true); }
    return;
  }
  if (!force && Date.now() - lodgingLivingReadAt < 4000) return;
  const request = {context, invalidated: false}; request.done = new Promise(resolve => { request.finish = resolve; }); lodgingLivingRequest = request;
  try {
    const plans = await api(`/api/worlds/${worldId}/lodging-plans`);
    if (lodgingLivingRequest !== request || request.invalidated || !panel.isConnected || state.worldId !== worldId || currentPlayer()?.id !== player.id) return;
    lodgingLivingReadAt = Date.now(); panel.querySelector(".lodging-read-error")?.remove();
    const signature = JSON.stringify(plans); if (signature === lodgingLivingSignature) return; lodgingLivingSignature = signature;
    const heading = document.createElement("h3"); heading.id = "my-lodging-plans-heading"; heading.textContent = "我的订房筹备"; panel.replaceChildren(heading);
    lodgingLine(panel, "本人询价、核对准确付款、报告就绪或退出，都可在这里办理。对方未报告的私人支出和障碍保持未知；新提议请从人物档案商量。");
    if (!plans.length) lodgingLine(panel, "目前没有已记录的订房筹备。");
    for (const plan of plans) {
      const otherId = plan.other_id || (plan.proposer_id === player.id ? plan.recipient_id : plan.proposer_id);
      panel.append(lodgingPlanRow(plan, player, otherId, () => lodgingLivingContext === context));
    }
  } catch {
    if (lodgingLivingRequest === request && !request.invalidated && panel.isConnected && state.worldId === worldId && currentPlayer()?.id === player.id && !panel.querySelector(".lodging-read-error")) lodgingLine(panel, "暂时无法刷新订房筹备，现有记录可能变化，请稍后重试。").className = "lodging-read-error muted";
  } finally { if (lodgingLivingRequest === request) lodgingLivingRequest = null; request.finish(); }
}

const lodgingPlansOriginalLiving = renderLiving;
renderLiving = function() { lodgingPlansOriginalLiving(); renderMyLodgingPlans(); };
