/* 哥哥，分房安排只确认各自休息与之后会合，不替另一人进屋或开始休息。 */
const separateStayStatuses = {
  pending: "等待对方回应", accepted: "双方已确认分房与会合安排", active: "本人正在休息",
  completed: "双方已完成休息", declined: "已婉拒", cancelled: "已取消",
  expired: "未确认已过期", missed: "未在约定窗口完成", interrupted: "安排已中断",
};
for (const [status, label] of Object.entries(separateStayStatuses)) {
  eventLabels[`social.appointment_separate_stay_${status}`] = `分房住宿：${label}`;
}
Object.assign(eventLabels, {
  "social.appointment_separate_stay_proposed": "提出分房住宿与会合",
  "social.appointment_separate_stay_checked_in": "实际开始自己的分房休息",
  "social.appointment_separate_stay_participant_completed": "完成本人的分房休息",
  "social.appointment_separate_stay_participant_interrupted": "本人的分房休息中断",
  "social.appointment_separate_stay_participant_missed": "错过本人的分房入住",
  "social.appointment_separate_stay_participant_cancelled": "取消本人的分房休息",
  "social.appointment_separate_stay_reunited": "按约实际会合",
  "social.appointment_separate_stay_reunion_missed": "未在约定窗口内会合",
  "social.appointment_separate_stay_reunion_wait_ended": "结束已会合后的剩余等候",
  "social.appointment_separate_stay_change_proposed": "商量调整分房入住时间",
  "social.appointment_separate_stay_change_accepted": "双方同意分房改期",
  "social.appointment_separate_stay_change_declined": "婉拒分房改期",
  "social.appointment_separate_stay_change_withdrawn": "撤回分房改期提议",
  "social.appointment_separate_stay_change_expired": "分房改期提议已过期",
  "social.appointment_separate_stay_change_cancelled": "分房改期提议已结束",
});

async function submitSeparateStay(characterId, choice) {
  const player = currentPlayer(), person = characterById(characterId);
  const worldId = state.worldId;
  if (!player || !worldId || state.busy || state.responsePending) return;
  if (separateLivingFetch?.context === `${worldId}:${player.id}`) separateLivingFetch.invalidated = true;
  const near = person && player && canInviteCompanion(person) &&
    distanceKm(player.longitude, player.latitude, person.longitude, person.latitude) <= 0.02;
  elements.characterDialog.close();
  try {
    await executeNaturalSocialChoice(choice.statement, {
      target_character_id: characterId, choice_evidence: choice.statement, separate_stay_choice: choice,
    }, near ? null : "letter");
  } finally {
    separateLivingReadAt = 0;
    if (state.worldId === worldId && currentPlayer()?.id === player.id) await renderMySeparateStays(true);
  }
}

function separateStayChangeFingerprint(stay) {
  return JSON.stringify([stay.id, stay.proposer_id, stay.recipient_id, stay.journey_id,
    stay.stop_index, stay.arrival_event_id, stay.pause_event_id, stay.source_event_id,
    stay.source_lodging_plan_id, stay.own_room_id, stay.other_room_id,
    stay.arrival_starts_at, stay.arrival_ends_at, stay.latest_end_at, stay.minutes,
    stay.reunion_starts_at, stay.reunion_ends_at]);
}

/* 哥哥，改期只是新的商量；另一人答应之前，原来的时间仍然生效。 */
function appendSeparateStayChanges(row, stay, player, otherId, contextIsCurrent) {
  const changes = Array.isArray(stay.reschedule_requests) ? stay.reschedule_requests : [];
  if (!changes.length && !stay.can_reschedule) return;
  const worldId = state.worldId, originalFingerprint = separateStayChangeFingerprint(stay);
  const isCurrent = () => row.isConnected && state.worldId === worldId &&
    currentPlayer()?.id === player.id && contextIsCurrent();
  const line = (parent, value) => {
    const item = document.createElement("p"); item.textContent = value; parent.append(item); return item;
  };
  const section = document.createElement("section"); section.className = "separate-stay-changes";
  const heading = document.createElement("h4"); heading.textContent = "商量调整入住时间"; section.append(heading);
  row.append(section);
  const notice = line(section, ""); notice.setAttribute("aria-live", "polite");
  let attempt = null, form = null;
  const lock = () => {
    const locked = new Map(Array.from(row.querySelectorAll("button[data-separate-stay-action]"), item => [item, item.disabled]));
    for (const item of locked.keys()) item.disabled = true;
    return () => { if (isCurrent()) for (const [item, disabled] of locked) item.disabled = disabled; };
  };
  const fresh = async () => {
    const [stays, trips] = await Promise.all([
      api(`/api/worlds/${worldId}/separate-stays?character_id=${encodeURIComponent(otherId)}`),
      api(`/api/worlds/${worldId}/journeys?character_id=${encodeURIComponent(otherId)}`),
    ]);
    const latest = stays.find(item => item.id === stay.id);
    const trip = latest && trips.find(item => item.id === latest.journey_id);
    const pair = latest && [latest.proposer_id, latest.recipient_id];
    const stop = trip?.stops?.[latest?.stop_index];
    if (!latest || !pair.includes(player.id) || !pair.includes(otherId) ||
        separateStayChangeFingerprint(latest) !== originalFingerprint ||
        !trip || trip.status !== "paused" || trip.phase !== "stopped" ||
        trip.next_index !== latest.stop_index + 1 || !stop?.arrived_at || stop.location_id !== latest.location_id) {
      return {reason: "原分房安排或旅行站点已经变化，请重新查看最新安排后再商量。"};
    }
    return {stay: latest, trip};
  };
  const pending = changes.find(item => item.status === "pending");
  for (const change of changes) {
    const label = {pending: "等待对方决定", accepted: "双方已同意改期", declined: "已婉拒改期",
      withdrawn: "已撤回改期", expired: "改期回应期限已过", cancelled: "改期提议已结束"};
    const detail = document.createElement("div"); detail.className = "life-item"; section.append(detail);
    line(detail, `${change.proposer_id === player.id ? "你提出的改期" : "对方提出的改期"} · ${label[change.status] || "这份改期已有结果"}`);
    line(detail, `原入住窗 ${formatWorldTime(change.old_arrival_starts_at)} 至 ${formatWorldTime(change.old_arrival_ends_at)}；最晚完整休息至 ${formatWorldTime(change.old_latest_end_at)}，会合窗最晚至 ${formatWorldTime(change.old_reunion_ends_at)}。`);
    line(detail, `提议新入住窗 ${formatWorldTime(change.new_arrival_starts_at)} 至 ${formatWorldTime(change.new_arrival_ends_at)}；最晚完整休息至 ${formatWorldTime(change.new_latest_end_at)}，室外会合窗 ${formatWorldTime(change.new_latest_end_at)} 至 ${formatWorldTime(change.new_reunion_ends_at)}。`);
    if (change.statement) line(detail, `${change.statement}${change.response ? ` / 回应：${change.response}` : ""}`);
    if (change.status !== "pending") continue;
    line(detail, `双方明确同意前原时间继续有效。请在 ${formatWorldTime(change.expires_at)}之前回应；只改入住开始，不改变各自房间、休息时长或会合窗长度。`);
    const choose = (label, operation, statement) => {
      const button = document.createElement("button"); button.type = "button";
      button.className = "button secondary"; button.textContent = label; button.dataset.separateStayAction = "true";
      button.addEventListener("click", async () => {
        if (!isCurrent() || state.busy || state.responsePending || button.disabled || attempt) return;
        const request = {}; attempt = request; const unlock = lock();
        try {
          const current = await fresh();
          if (attempt !== request || !isCurrent()) return;
          if (current.reason) { notice.textContent = current.reason; return; }
          const latest = current.stay.reschedule_requests?.find(item => item.id === change.id && item.status === "pending");
          const now = Date.parse(state.living?.world_time || "");
          if (!latest || !Number.isFinite(now) || now >= Date.parse(latest.expires_at) ||
              latest.proposer_id !== change.proposer_id || latest.recipient_id !== change.recipient_id ||
              latest.new_arrival_starts_at !== change.new_arrival_starts_at ||
              latest.new_arrival_ends_at !== change.new_arrival_ends_at ||
              latest.new_latest_end_at !== change.new_latest_end_at || latest.new_reunion_ends_at !== change.new_reunion_ends_at) {
            notice.textContent = "这份改期已变化或超过回应期限，请重新查看当前安排。"; return;
          }
          if (state.busy || state.responsePending) { notice.textContent = "请先完成当前行动，再核对这份改期。"; return; }
          await submitSeparateStay(otherId, {operation, stay_id: stay.id, change_id: latest.id, statement});
        } catch {
          if (isCurrent()) notice.textContent = "暂时无法核对或提交这份改期，请查看已有安排后重试。";
        } finally { if (attempt === request) attempt = null; unlock(); }
      });
      detail.append(button);
    };
    if (change.recipient_id === player.id) {
      choose("同意这次改期", "accept_change", "我明确同意这份改期记录中的新入住与会合时间，其余房间、完整休息时长和会合窗长度沿用原约定；各自入住，恢复旅行另议。");
      choose("保留原约，婉拒改期", "decline_change", "我暂时不接受这次改期，仍保留原来已经确认的分房休息与会合时间。");
    } else if (change.proposer_id === player.id) {
      choose("只撤回这次改期提议", "withdraw_change", "我撤回本人这份改期提议，原来已经确认的分房休息与会合安排继续有效。");
    }
  }
  if (!stay.can_reschedule || pending) return;
  const open = document.createElement("button"); open.type = "button"; open.className = "button secondary";
  open.textContent = "商量新的入住开始时间"; open.dataset.separateStayAction = "true";
  open.dataset.separateStayChange = "open"; open.setAttribute("aria-expanded", "false"); section.append(open);
  open.addEventListener("click", async () => {
    if (!isCurrent() || state.busy || state.responsePending || open.disabled || attempt) return;
    if (form) { form.remove(); form = null; open.setAttribute("aria-expanded", "false"); return; }
    const request = {}; attempt = request; const unlock = lock(); notice.textContent = "正在核对原时间和可商量的时段……";
    try {
      let current = await fresh();
      if (attempt !== request || !isCurrent()) return;
      if (current.reason) { notice.textContent = current.reason; return; }
      if (!current.stay.can_reschedule || current.stay.reschedule_requests?.some(item => item.status === "pending")) {
        notice.textContent = "当前安排已有改期提议或已不在可改期时段，请先查看现有安排。"; return;
      }
      const windowMs = Date.parse(stay.arrival_ends_at) - Date.parse(stay.arrival_starts_at);
      const restMs = Number(stay.minutes) * 60000;
      const reunionMs = Date.parse(stay.reunion_ends_at) - Date.parse(stay.reunion_starts_at);
      if (![windowMs, restMs, reunionMs].every(value => Number.isFinite(value) && value > 0)) {
        notice.textContent = "原时间条款暂时无法完整核对，请重新查看安排。"; return;
      }
      notice.textContent = "原分房休息和会合时间继续生效，只有对方接受这次改期后才调整。";
      form = document.createElement("form"); form.className = "long-term-form"; form.dataset.separateStayChange = "form";
      const activeForm = form;
      const formIsCurrent = () => isCurrent() && form === activeForm && activeForm.isConnected;
      line(form, `原入住窗 ${formatWorldTime(stay.arrival_starts_at)} 至 ${formatWorldTime(stay.arrival_ends_at)}。本次只移动开始时间，入住窗仍为${windowMs / 60000}分钟，各自完整休息${stay.minutes}分钟，会合窗仍为${reunionMs / 60000}分钟。`);
      const field = (label, input) => {
        const wrap = document.createElement("label"); wrap.textContent = label; input.setAttribute("aria-label", label);
        input.required = true; wrap.append(input); activeForm.append(wrap); return input;
      };
      const arrival = field("改期后的入住窗开始（世界时间 UTC）", document.createElement("input")); arrival.type = "datetime-local";
      const speech = field("这次想怎样商量改期", document.createElement("textarea")); speech.maxLength = 80;
      const timing = line(form, "请填写新的入住开始时间和本人想说的话。"); timing.setAttribute("aria-live", "polite");
      line(form, "房间、已付租约和各自完整休息时长照原约定，许可与日程会重新核验；对方可以拒绝或不接受，这一步不开始休息。");
      const submit = document.createElement("button"); submit.type = "submit"; submit.className = "button primary";
      submit.textContent = "提出改期，听听对方决定"; submit.dataset.separateStayAction = "true"; submit.dataset.separateStayChange = "submit"; form.append(submit);
      let blockedReason = "", errorNotice = "";
      const sync = () => {
        const now = Date.parse(state.living?.world_time || ""), start = Date.parse(`${arrival.value}Z`);
        const oldStart = Date.parse(current.stay.arrival_starts_at), permit = Date.parse(current.stay.source_lodging_plan?.required_until_at || "");
        const deadline = Date.parse(current.trip.deadline_at), restEnd = start + windowMs + restMs, reunionEnd = restEnd + reunionMs;
        let reason = blockedReason;
        if (!reason && (!Number.isFinite(now) || !Number.isFinite(oldStart) || !Number.isFinite(deadline))) reason = "暂时无法核对当前世界时间与原约定，请重新查看安排。";
        else if (!reason && now >= oldStart) reason = "原入住窗已经开始，请按现有安排处理；这份时间已不能这样改期。";
        else if (!reason && Number.isFinite(start) && (start <= now || start > now + 86400000)) reason = "新入住开始须在当前世界时间之后、24个世界小时以内。";
        else if (!reason && start === oldStart) reason = "新时间与原时间相同，请填写确实想商量的新开始时间。";
        else if (!reason && Number.isFinite(permit) && restEnd > permit) reason = "新时间下的完整休息会超过原订房许可终点，请调整开始时间。";
        else if (!reason && Number.isFinite(reunionEnd) && reunionEnd > deadline) reason = "新时间下的会合窗会超过原旅行期限，请调整开始时间。";
        if (Number.isFinite(now)) arrival.min = new Date(now + 60000).toISOString().slice(0, 16);
        const lastStart = Math.min(now + 86400000, deadline - windowMs - restMs - reunionMs, Number.isFinite(permit) ? permit - windowMs - restMs : Infinity);
        if (Number.isFinite(lastStart)) arrival.max = new Date(lastStart).toISOString().slice(0, 16);
        arrival.setCustomValidity(reason); speech.setCustomValidity(speech.value.trim() ? "" : "请亲自填写这次想说的话。");
        timing.textContent = errorNotice || reason || (Number.isFinite(reunionEnd) ?
          `新入住窗 ${formatWorldTime(new Date(start).toISOString())} 至 ${formatWorldTime(new Date(start + windowMs).toISOString())}；最晚完整休息至 ${formatWorldTime(new Date(restEnd).toISOString())}，室外会合窗 ${formatWorldTime(new Date(restEnd).toISOString())} 至 ${formatWorldTime(new Date(reunionEnd).toISOString())}。回应须早于原、新入住窗开始，原旅行期限 ${formatWorldTime(current.trip.deadline_at)}${Number.isFinite(permit) ? `，原订房许可终点 ${formatWorldTime(current.stay.source_lodging_plan.required_until_at)}` : ""}。` : "请填写新的入住开始时间和本人想说的话。");
        submit.disabled = Boolean(attempt) || !formIsCurrent() || !Number.isFinite(start) || !speech.value.trim() || Boolean(reason);
        return !reason && Number.isFinite(start) && Boolean(speech.value.trim());
      };
      for (const input of [arrival, speech]) input.addEventListener("input", () => { errorNotice = ""; sync(); });
      activeForm.addEventListener("submit", async event => {
        event.preventDefault();
        if (!formIsCurrent() || state.busy || state.responsePending || attempt || !sync() || !activeForm.reportValidity()) return;
        const request = {}; attempt = request; const unlock = lock();
        arrival.disabled = true; speech.disabled = true; errorNotice = "";
        try {
          const latest = await fresh();
          if (attempt !== request || !formIsCurrent()) return;
          if (latest.reason) { blockedReason = latest.reason; return; }
          if (!latest.stay.can_reschedule || latest.stay.reschedule_requests?.some(item => item.status === "pending")) {
            blockedReason = "已有另一份改期或原安排已不能改期，请重新查看现有安排。"; return;
          }
          current = latest;
          if (!sync()) return;
          if (state.busy || state.responsePending) { errorNotice = "请先完成当前行动，再核对并提出改期。"; return; }
          const statement = speech.value.trim() + ` 我提议把这份已确认分房的入住窗开始改到世界时间${new Date(`${arrival.value}Z`).toISOString()}，其余房间、完整休息时长和会合窗长度照原约定；在你明确接受前原时间继续有效。`;
          await submitSeparateStay(otherId, {operation: "reschedule", stay_id: stay.id, arrival_world_time: new Date(`${arrival.value}Z`).toISOString(), statement});
          if (formIsCurrent()) {
            // 哥哥，保存失败时旧页面可能仍在，先核对真实提议，再收起本人刚写的话。
            const saved = await api(`/api/worlds/${worldId}/separate-stays?character_id=${encodeURIComponent(otherId)}`);
            if (!formIsCurrent()) return;
            const recorded = saved.find(item => item.id === stay.id)?.reschedule_requests?.some(change =>
              change.proposer_id === player.id && ["pending", "accepted"].includes(change.status) &&
              Date.parse(change.new_arrival_starts_at) === Date.parse(`${arrival.value}Z`));
            if (recorded) { activeForm.remove(); form = null; open.setAttribute("aria-expanded", "false"); }
            else errorNotice = "暂未核对到已保存的这次改期，原时间继续有效；请查看已有提议后重试。";
          }
        } catch {
          if (formIsCurrent()) errorNotice = "暂时无法核对或提交改期，请查看已有提议后重试。";
        } finally {
          if (attempt === request) attempt = null; unlock();
          if (formIsCurrent()) { arrival.disabled = false; speech.disabled = false; sync(); }
        }
      });
      section.append(form); open.setAttribute("aria-expanded", "true");
      attempt = null; sync();
    } catch {
      if (isCurrent()) notice.textContent = "暂时无法核对原安排，请稍后重试。";
    } finally { if (attempt === request) attempt = null; unlock(); }
  });
}

let separateProfileContext = null;
function separateProfileIsCurrent(request) {
  return request && separateProfileContext === request && state.worldId === request.worldId &&
    currentPlayer()?.id === request.playerId && elements.characterDialog.open;
}

async function appendSeparateStays(characterId, profileRequest) {
  const player = currentPlayer();
  if (!separateProfileIsCurrent(profileRequest) || !player || player.id === characterId || document.querySelector("#separate-stays-profile")) return;
  const worldId = profileRequest.worldId;
  const panel = document.createElement("section"); panel.id = "separate-stays-profile";
  const heading = document.createElement("h2"); heading.textContent = "分房住宿与休息后会合"; panel.append(heading);
  const text = (parent, value) => {
    const line = document.createElement("p"); line.textContent = value; parent.append(line); return line;
  };
  text(panel, "在已实际到达的旅行站点，商量各住一间房、各自休息4–12小时，再回到原站点会合。双方分别同意，各自取得完整房间许可并实际进屋；你只开始自己的休息，NPC 自己决定并执行入住。先休息完的人可以自行离屋，原旅行仍需双方另行同意恢复。");
  elements.characterProfile.append(panel);
  let stays, trips, options;
  try {
    [stays, trips, options] = await Promise.all([
      api(`/api/worlds/${worldId}/separate-stays?character_id=${encodeURIComponent(characterId)}`),
      api(`/api/worlds/${worldId}/journeys?character_id=${encodeURIComponent(characterId)}`),
      api(`/api/worlds/${worldId}/separate-stays/options/${encodeURIComponent(characterId)}`),
    ]);
  } catch {
    if (panel.isConnected && separateProfileIsCurrent(profileRequest)) text(panel, "暂时无法读取分房住宿安排，请稍后重新打开档案。"); return;
  }
  if (!panel.isConnected || !separateProfileIsCurrent(profileRequest)) return;
  const rooms = options.room_options || [];
  const roomName = roomId => rooms.find(room => room.id === roomId)?.name || "约定房间";
  for (const stay of stays) {
    const row = document.createElement("div"); row.className = "life-item";
    text(row, `${separateStayStatuses[stay.status] || stay.status} · ${stay.location_name || "原旅行站点"} · 各休息${stay.minutes}分钟（${Number((stay.minutes / 60).toFixed(2))}小时）`);
    text(row, `原旅行第${stay.stop_index + 1}站 · 各自入住窗 ${formatWorldTime(stay.arrival_starts_at)} 至 ${formatWorldTime(stay.arrival_ends_at)}`);
    const participants = Array.isArray(stay.participants) ? stay.participants : [];
    for (const participant of participants) {
      const isSelf = participant.character_id === player.id, identity = isSelf ? "你" : "对方";
      let detail = `${identity}的房间：${participant.room_name || roomName(participant.room_id)}`;
      const progress = {unknown: "尚未向你确认", waiting: "等待本人入住", active: "本人已开始休息", completed: "本人已完成休息，按约会合", missed: "错过本人入住窗", interrupted: "本人休息已中断", cancelled: "本人休息已取消"};
      if (progress[participant.status]) detail += ` · ${progress[participant.status]}`;
      if (isSelf && participant.checked_in_at) detail += ` · 实际开始 ${formatWorldTime(participant.checked_in_at)}`;
      if (isSelf && participant.ends_at) detail += ` · 约定休息至 ${formatWorldTime(participant.ends_at)}`;
      if (isSelf && participant.finished_at) detail += ` · 实际结束 ${formatWorldTime(participant.finished_at)}`;
      text(row, detail);
    }
    if (!participants.length) {
      text(row, `提出者住 ${stay.own_room_name || roomName(stay.own_room_id)}，另一方住 ${stay.other_room_name || roomName(stay.other_room_id)}；接受不交换房间。`);
    }
    if (stay.reunion_starts_at && stay.reunion_ends_at) {
      text(row, `会合窗 ${formatWorldTime(stay.reunion_starts_at)} 至 ${formatWorldTime(stay.reunion_ends_at)} · ${stay.reunion_location_name || stay.location_name || "原旅行站点"}的室外场景。请自行逐门离屋并前往。`);
    } else {
      text(row, `各自完整休息后按约回到原站点室外会合，会合窗${stay.reunion_window_minutes || 60}个世界分钟；具体起止以已确认记录为准。`);
    }
    if (stay.reunion_status) {
      const reunionStatus = {waiting: "等待各自回到会合地点", met: "已实际会合", missed: "未在窗口内会合", cancelled: "会合安排已取消"};
      text(row, `休息后会合：${reunionStatus[stay.reunion_status] || stay.reunion_status}；恢复旅行仍需双方另行同意。`);
    }
    if (stay.can_end_reunion_wait) text(row, "已实际会合；在原会合窗口内留在这里等候后续决定。你可以提出恢复原旅行，也可以明确结束本次会合等候。");
    text(row, `${stay.proposer_id === player.id ? "你的邀请" : "对方的邀请"}：${stay.statement}${stay.response ? ` / ${stay.response}` : ""}`);
    if (stay.reason) text(row, stay.reason);
    const choose = (label, operation, statement) => {
      const button = document.createElement("button"); button.type = "button";
      button.className = "button secondary"; button.textContent = label;
      button.dataset.separateStayAction = "true";
      button.addEventListener("click", async () => {
        if (button.disabled || !panel.isConnected || !separateProfileIsCurrent(profileRequest) || state.busy || state.responsePending) return;
        button.disabled = true;
        try { await submitSeparateStay(characterId, {operation, stay_id: stay.id, statement}); }
        finally { if (panel.isConnected && separateProfileIsCurrent(profileRequest)) button.disabled = false; }
      });
      row.append(button);
    };
    if (stay.status === "pending") {
      if (stay.recipient_id === player.id) {
        choose("接受各自房间和会合安排", "accept", "我接受这份记录中各自的房间、入住窗、完整休息时长和会合安排；我们各自进自己的房间、开始自己的休息，再按约回到原站点室外会合。");
        choose("婉拒这份分房住宿邀请", "decline", "谢谢你的邀请，这份分房住宿与会合安排我暂时不接受，原旅行另行商量。");
      } else choose("撤回我的分房住宿邀请", "cancel", "我先撤回这份分房住宿与会合邀请，原旅行仍另行商量是否恢复。");
    }
    const own = participants.find(participant => participant.character_id === player.id);
    if (["accepted", "active"].includes(stay.status) && own?.status === "waiting" && !own.checked_in_at) {
      choose("在自己的约定房间开始休息", "check_in", "我已实际进入自己在这份分房记录中的房间，选择在入住窗内开始自己的完整时长休息，不替你入住。");
    }
    if (["accepted", "active"].includes(stay.status) || stay.status === "completed" && stay.reunion_status === "waiting") {
      choose("结束这份分房住宿与会合安排", "cancel", "我决定结束这份分房住宿与会合安排，保留已经发生的休息和实际消耗，原旅行不自动恢复。");
    }
    if (stay.can_end_reunion_wait) {
      choose("结束本次会合等候", "cancel", "我们已经实际会合，我选择结束本次会合窗口内的后续等候；保留已经完成的各自休息和会合事实，原旅行另行商量。");
    }
    appendSeparateStayChanges(row, stay, player, characterId, () => panel.isConnected && separateProfileIsCurrent(profileRequest));
    panel.append(row);
  }
  const eligible = trips.filter(trip => ["active", "paused"].includes(trip.status) && trip.phase === "stopped" &&
    trip.next_index > 0 && trip.stops?.[trip.next_index - 1]?.arrived_at);
  if (!eligible.length) {
    text(panel, "先与对方确认有限行程并实际到站，再商量分房住宿。原旅行期限需覆盖最晚入住、完整休息和会合窗口。"); return;
  }
  const form = document.createElement("form"); form.className = "long-term-form";
  const field = (label, element) => {
    const wrap = document.createElement("label"); wrap.textContent = label;
    element.setAttribute("aria-label", label); wrap.append(element); form.append(wrap); return element;
  };
  const option = (element, value, label) => {
    const item = document.createElement("option"); item.value = value; item.textContent = label; element.append(item);
  };
  const journey = field("分房住宿的已到站原旅行", document.createElement("select")); journey.required = true;
  option(journey, "", "请选择已实际到站的旅行");
  for (const trip of eligible) option(journey, trip.id, `${trip.title} · 第${trip.next_index}站 ${trip.stops[trip.next_index - 1].name} · 期限 ${formatWorldTime(trip.deadline_at)}`);
  const ownRoom = field("我自己休息的真实房间", document.createElement("select")); ownRoom.required = true;
  const otherRoom = field("邀请对方自己休息的真实房间", document.createElement("select")); otherRoom.required = true;
  const arrival = field("分房入住窗开始（世界时间 UTC）", document.createElement("input"));
  arrival.type = "datetime-local"; arrival.required = true;
  const now = Date.parse(state.living?.world_time || "");
  if (Number.isFinite(now)) {
    arrival.min = new Date(now + 60000).toISOString().slice(0, 16);
    arrival.max = new Date(now + 86400000).toISOString().slice(0, 16);
    arrival.value = new Date(now + 30 * 60000).toISOString().slice(0, 16);
  }
  const windowMinutes = field("分房入住窗长度（15–120世界分钟）", document.createElement("input"));
  windowMinutes.type = "number"; windowMinutes.min = "15"; windowMinutes.max = "120";
  windowMinutes.step = "1"; windowMinutes.value = "60"; windowMinutes.required = true;
  const minutes = field("各自完整休息时长（240–720世界分钟）", document.createElement("input"));
  minutes.type = "number"; minutes.min = "240"; minutes.max = "720";
  minutes.step = "1"; minutes.value = "480"; minutes.required = true;
  const reunionMinutes = field("休息后会合窗长度（15–120世界分钟）", document.createElement("input"));
  reunionMinutes.type = "number"; reunionMinutes.min = "15"; reunionMinutes.max = "120";
  reunionMinutes.step = "1"; reunionMinutes.value = "60"; reunionMinutes.required = true;
  const speech = field("想怎样商量分房住宿和会合", document.createElement("textarea"));
  speech.required = true; speech.maxLength = 80; speech.placeholder = "说说自己的想法，也给对方保留调整或拒绝的空间。";
  const timing = text(form, ""); timing.setAttribute("aria-live", "polite");
  const help = text(form, "");
  const submit = document.createElement("button"); submit.type = "submit"; submit.className = "button primary";
  submit.textContent = "提出分房与会合安排，听听对方决定"; submit.disabled = true; form.append(submit);
  const helpText = "两间房须在同一已到站地点，分别有各自完整时段的合法许可；不能把内外嵌套房当作两间独立房。房租先走住房协商，这里不代订房、付款或授予彼此进房许可，也不自动用餐。";
  const syncTiming = () => {
    const start = Date.parse(arrival.value + ":00Z"), trip = eligible.find(item => item.id === journey.value);
    const restEnd = start + (Number(windowMinutes.value) + Number(minutes.value)) * 60000;
    const reunionEnd = restEnd + Number(reunionMinutes.value) * 60000;
    timing.textContent = Number.isFinite(reunionEnd) ?
      `最晚入住后各自完整休息至 ${formatWorldTime(new Date(restEnd).toISOString())}；会合窗最晚结束 ${formatWorldTime(new Date(reunionEnd).toISOString())}。${trip ? `原旅行期限 ${formatWorldTime(trip.deadline_at)}。` : ""}` :
      "请填写明确的入住时间窗、完整休息时长和会合窗。";
  };
  const syncRooms = () => {
    const own = rooms.find(item => item.id === ownRoom.value), other = rooms.find(item => item.id === otherRoom.value);
    otherRoom.setCustomValidity(own && other && own.id === other.id ? "请选择两间不同的房间。" : "");
    submit.disabled = !journey.value || !own || !other || own.id === other.id;
  };
  const syncJourney = () => {
    const trip = eligible.find(item => item.id === journey.value), locationId = trip?.stops[trip.next_index - 1]?.location_id;
    const available = rooms.filter(item => item.location_id === locationId);
    for (const select of [ownRoom, otherRoom]) {
      select.replaceChildren(); option(select, "", trip ? "请选择该站的真实房间" : "请先选择原旅行");
      for (const room of available) option(select, room.id, `${room.location_name} · ${room.name}`);
    }
    help.textContent = trip && available.length < 2 ?
      "该站目前不足两间可用于分房安排的已知房间。请先取得各自的完整租住许可，再重新打开档案。" : helpText;
    syncRooms(); syncTiming();
  };
  journey.addEventListener("change", syncJourney);
  ownRoom.addEventListener("change", syncRooms); otherRoom.addEventListener("change", syncRooms);
  for (const input of [arrival, windowMinutes, minutes, reunionMinutes]) input.addEventListener("input", syncTiming);
  syncJourney();
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!panel.isConnected || !separateProfileIsCurrent(profileRequest) || state.busy || state.responsePending || submit.disabled) return;
    syncRooms(); if (!form.reportValidity()) return;
    const trip = eligible.find(item => item.id === journey.value);
    const own = rooms.find(item => item.id === ownRoom.value), other = rooms.find(item => item.id === otherRoom.value);
    const start = new Date(arrival.value + ":00Z");
    if (!trip || !own || !other || own.id === other.id || !Number.isFinite(start.getTime())) return;
    const locationId = trip.stops[trip.next_index - 1].location_id;
    if (own.location_id !== locationId || other.location_id !== locationId) return;
    const duration = Number(minutes.value), reunion = Number(reunionMinutes.value);
    const statement = speech.value.trim() + ` 我提出暂停原旅行，各住表单中指定给自己的不同房间，在入住窗各自进屋休息${duration}个世界分钟，再按${reunion}分钟会合窗回到原站点室外。房租各自处理，不授予彼此进房许可，旅行另行商量恢复。`;
    submit.disabled = true;
    try {
      await submitSeparateStay(characterId, {
        operation: "propose", journey_id: trip.id, own_room_id: own.id, other_room_id: other.id,
        arrival_world_time: start.toISOString(), window_minutes: Number(windowMinutes.value),
        minutes: duration, reunion_window_minutes: reunion, statement,
      });
    } finally { if (panel.isConnected && separateProfileIsCurrent(profileRequest)) syncRooms(); }
  });
  panel.append(form);
}
const separateStaysOriginalProfile = openCharacterProfile;
openCharacterProfile = async function(characterId) {
  const request = {worldId: state.worldId, playerId: currentPlayer()?.id, characterId}; separateProfileContext = request;
  await separateStaysOriginalProfile(characterId);
  if (separateProfileIsCurrent(request)) await appendSeparateStays(characterId, request);
};
const separateStaysOriginalKnownProfile = openKnownProfile;
openKnownProfile = function(characterId) {
  const request = {worldId: state.worldId, playerId: currentPlayer()?.id, characterId}; separateProfileContext = request;
  separateStaysOriginalKnownProfile(characterId); appendSeparateStays(characterId, request);
};

/* 哥哥，人在自己的房间也能办理本人旧安排，不必先找到另一人的档案入口。 */
let separateLivingFetch = null;
let separateLivingReadAt = 0;
let separateLivingContext = "";
let separateLivingSignature = "";

async function renderMySeparateStays(force = false) {
  const player = currentPlayer(), worldId = state.worldId;
  if (separateProfileContext && (separateProfileContext.worldId !== worldId || separateProfileContext.playerId !== player?.id)) separateProfileContext = null;
  if (!player || !state.living || !worldId) {
    if (separateLivingFetch) separateLivingFetch.invalidated = true;
    document.querySelector("#my-separate-stays")?.remove();
    separateLivingContext = ""; separateLivingSignature = ""; return;
  }
  const context = `${worldId}:${player.id}`;
  let panel = document.querySelector("#my-separate-stays");
  if (!panel) {
    const anchor = document.querySelector("#interior-panel");
    if (!anchor) return;
    panel = document.createElement("section"); panel.id = "my-separate-stays";
    panel.setAttribute("aria-labelledby", "my-separate-stays-heading"); anchor.after(panel);
  }
  const text = (parent, value) => {
    const line = document.createElement("p"); line.textContent = value; parent.append(line); return line;
  };
  if (separateLivingContext !== context) {
    if (separateLivingFetch) separateLivingFetch.invalidated = true;
    separateLivingContext = context; separateLivingReadAt = 0; separateLivingSignature = "";
    const heading = document.createElement("h3"); heading.id = "my-separate-stays-heading";
    heading.textContent = "我的分房与会合安排"; panel.replaceChildren(heading);
    text(panel, "正在读取你已记录的分房安排……");
  }
  if (separateLivingFetch?.context === context) {
    if (force) {
      const pending = separateLivingFetch;
      pending.invalidated = true;
      await pending.done;
      if (state.worldId === worldId && currentPlayer()?.id === player.id) return renderMySeparateStays(true);
    }
    return;
  }
  if (!force && Date.now() - separateLivingReadAt < 4000) return;
  const request = {context, invalidated: false};
  request.done = new Promise(resolve => { request.finish = resolve; });
  separateLivingFetch = request;
  try {
    const stays = await api(`/api/worlds/${worldId}/separate-stays`);
    if (separateLivingFetch !== request || request.invalidated || !panel.isConnected || state.worldId !== worldId || currentPlayer()?.id !== player.id) return;
    separateLivingReadAt = Date.now();
    const ownStays = stays.filter(stay => stay.participants?.some(part => part.character_id === player.id));
    const signature = JSON.stringify(ownStays);
    panel.querySelector(".separate-stays-read-error")?.remove();
    if (signature === separateLivingSignature) return;
    separateLivingSignature = signature;
    const heading = document.createElement("h3"); heading.id = "my-separate-stays-heading";
    heading.textContent = "我的分房与会合安排"; panel.replaceChildren(heading);
    if (!ownStays.length) {
      text(panel, "目前没有已记录的分房安排。实际到站后，可以从人物档案商量各住一间房、休息后会合。"); return;
    }
    text(panel, "这里只办理你本人的入住或退出，不显示对方独处时的活动细节；会合与恢复旅行分别处理。");
    for (const stay of ownStays) {
      const own = stay.participants.find(part => part.character_id === player.id);
      const otherId = stay.other_id || (stay.proposer_id === player.id ? stay.recipient_id : stay.proposer_id);
      const known = state.living?.known_people?.find(person => person.id === otherId);
      const visible = state.living?.people?.find(person => person.id === otherId);
      const name = visible?.name || known?.name || "已约定分房的伙伴";
      const row = document.createElement("article"); row.className = "life-item";
      const title = document.createElement("strong"); title.textContent = `与${name}的分房安排`; row.append(title);
      const ownStatus = {
        waiting: stay.status === "pending" ? "邀请等待回应，尚未确认入住" : "待本人入住",
        active: "本人休息中", completed: "本人休息已完成", missed: "本人错过入住窗",
        interrupted: "本人休息已中断", cancelled: "本人安排已取消",
      };
      const label = ["declined", "cancelled", "expired"].includes(stay.status) ? separateStayStatuses[stay.status] : ownStatus[own.status] || separateStayStatuses[stay.status] || stay.status;
      text(row, `${label} · 我的房间：${own.room_name || "约定房间"} · 完整休息${stay.minutes}个世界分钟`);
      text(row, `本人入住窗 ${formatWorldTime(stay.arrival_starts_at)} 至 ${formatWorldTime(stay.arrival_ends_at)}`);
      if (own.checked_in_at) text(row, `本人实际开始 ${formatWorldTime(own.checked_in_at)}${own.ends_at ? ` · 约定休息至 ${formatWorldTime(own.ends_at)}` : ""}`);
      if (own.finished_at) text(row, `本人实际结束 ${formatWorldTime(own.finished_at)}`);
      if (own.reason) text(row, own.reason);
      const reunion = {waiting: "等待在约定窗口内实际会合", met: "已实际会合", missed: "未在约定窗口内会合", cancelled: "会合安排已取消"};
      text(row, `休息后会合：${reunion[stay.reunion_status] || stay.reunion_status} · ${stay.location_name || "原站点"}的室外场景 · ${formatWorldTime(stay.reunion_starts_at)} 至 ${formatWorldTime(stay.reunion_ends_at)}`);
      if (own.status === "completed" && stay.reunion_status === "waiting") text(row, "你的休息已完成。请自行逐门离屋，按约回到原站点室外会合，原旅行仍未自动恢复。");
      if (stay.can_end_reunion_wait) text(row, "已会合，正在原窗口内等候后续决定；可以另行商量恢复旅行，或结束本次会合等候。");
      const choose = (label, operation, statement) => {
        const button = document.createElement("button"); button.type = "button";
        button.className = "button secondary"; button.textContent = label;
        button.dataset.separateStayAction = "true";
        button.addEventListener("click", async () => {
          if (state.busy || state.responsePending || button.disabled || !row.isConnected || state.worldId !== worldId || currentPlayer()?.id !== player.id || separateLivingContext !== context) return;
          panel.querySelectorAll("button[data-separate-stay-action]").forEach(action => { action.disabled = true; });
          if (separateLivingFetch?.context === context) separateLivingFetch.invalidated = true;
          try { await submitSeparateStay(otherId, {operation, stay_id: stay.id, statement}); }
          finally {
            separateLivingReadAt = 0;
            if (panel.isConnected && state.worldId === worldId && currentPlayer()?.id === player.id) {
              panel.querySelectorAll("button[data-separate-stay-action]").forEach(action => { action.disabled = false; });
            }
          }
        });
        row.append(button);
      };
      if (["accepted", "active"].includes(stay.status) && own.status === "waiting" && !own.checked_in_at) {
        choose("在自己的约定房间开始休息", "check_in", "我已实际进入自己的约定房间，选择在入住窗内开始本人已确认的完整分房休息，不替你入住。");
      }
      if (stay.status === "pending" && stay.proposer_id === player.id) {
        choose("撤回我的分房住宿邀请", "cancel", "我先撤回本人这份分房住宿邀请，原旅行另行商量。");
      } else if (["accepted", "active"].includes(stay.status) || stay.status === "completed" && stay.reunion_status === "waiting") {
        choose("结束这份分房住宿与会合安排", "cancel", "我选择退出这份分房休息与后续会合，保留已发生的本人休息和消耗，原旅行不自动恢复。");
      }
      if (stay.can_end_reunion_wait) {
        choose("结束本次会合等候", "cancel", "我们已经实际会合，我选择结束剩余会合等候，保留已经完成的休息和会合事实，原旅行另行商量。");
      }
      appendSeparateStayChanges(row, stay, player, otherId, () => separateLivingContext === context);
      const detail = document.createElement("button"); detail.type = "button"; detail.className = "button secondary";
      detail.textContent = "查看分房与会合详情";
      detail.addEventListener("click", () => {
        if (row.isConnected && state.worldId === worldId && currentPlayer()?.id === player.id && separateLivingContext === context) openKnownProfile(otherId);
      }); row.append(detail);
      panel.append(row);
    }
  } catch {
    if (separateLivingFetch === request && !request.invalidated && panel.isConnected && state.worldId === worldId && currentPlayer()?.id === player.id && !panel.querySelector(".separate-stays-read-error")) {
      const error = text(panel, "暂时无法刷新分房安排，现有内容可能已变化；请稍后重试。"); error.className = "separate-stays-read-error muted";
    }
  } finally {
    if (separateLivingFetch === request) separateLivingFetch = null;
    request.finish();
  }
}

const separateStaysOriginalLiving = renderLiving;
renderLiving = function() {
  separateStaysOriginalLiving(); renderMySeparateStays();
};
