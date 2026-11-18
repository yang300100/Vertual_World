/* 哥哥，邀请只表达本人的想法，世界记录双方真实同意后才显示正在同行。 */
Object.assign(eventLabels, {
  "social.companion_started": "同行开始", "social.companion_declined": "同行未成立",
  "social.companion_ended": "同行结束", "world.travelers_arrival": "旅人抵达",
  "social.appointment_companion_proposed": "提出同行邀请", "social.appointment_companion_countered": "商量另一段同行时长",
  "social.appointment_companion_accepted": "接受同行邀请", "social.appointment_companion_declined": "婉拒同行邀请",
  "social.appointment_companion_cancelled": "撤回同行邀请", "social.appointment_companion_expired": "同行邀请已过期",
});
const companionProposalStatuses = {
  pending: "等待对方独立回应", accepted: "双方已接受", declined: "已婉拒",
  cancelled: "已撤回", expired: "邀请已过期", countered: "已提出另一段时长，等待新邀请的回应",
};
let companionProfileContext = null;
let companionProposalRequest = null;
let companionProposalContext = "";
let companionProposalSignature = "";
let companionProposalReadAt = 0;
let companionContextEpoch = 0;
const companionPendingWrites = new Set();

function companionFor(characterId) {
  return state.living?.companions?.find(session => session.companion_id === characterId);
}

function canInviteCompanion(character) {
  const player = currentPlayer();
  return player && character && !character.is_player && character.health > 0
    && (player.current_room_id || null) === (character.current_room_id || null)
    && (player.space_layer || "ground") === (character.space_layer || "ground")
    && Boolean(state.living?.people?.some(person => person.id === character.id))
    && distanceKm(player.longitude, player.latitude, character.longitude, character.latitude) <= 0.02;
}

function companionContextIsCurrent(worldId, playerId) {
  return Boolean(playerId) && state.worldId === worldId && currentPlayer()?.id === playerId;
}

function companionProfileIsCurrent(request) {
  return request && companionProfileContext === request
    && companionContextIsCurrent(request.worldId, request.playerId) && elements.characterDialog.open;
}

function companionOtherId(proposal, playerId) {
  return proposal.proposer_id === playerId ? proposal.recipient_id : proposal.proposer_id;
}

function companionKnownName(characterId) {
  return livingName(characterById(characterId));
}

function companionPending(proposal) {
  const now = Date.parse(state.living?.world_time || "");
  return proposal.status === "pending" && (!proposal.expires_at || !Number.isFinite(now)
    || Date.parse(proposal.expires_at) > now);
}

function companionLine(parent, value, className = "") {
  const line = document.createElement("p"); line.textContent = value;
  if (className) line.className = className; parent.append(line); return line;
}

function companionStatement(operation, minutes, words = "") {
  const personal = String(words || "").trim();
  return [personal, operation === "counter"
    ? `我提议这次改为同行${minutes}个世界分钟，原邀请不自动成立，请你独立考虑这份新时长。`
    : `我邀请你从现在起短时徒步同行${minutes}个世界分钟；你可以接受、拒绝或提出另一段时长，双方随时可以结束。`].filter(Boolean).join(" ");
}

async function submitCompanionChoice(otherId, choice, isCurrent = () => true) {
  const playerId = currentPlayer()?.id, worldId = state.worldId, epoch = companionContextEpoch;
  if (!playerId || !worldId || !isCurrent() || state.busy || state.responsePending) return null;
  const key = `${worldId}:${playerId}:${otherId}`;
  if (companionPendingWrites.has(key)) return null;
  companionPendingWrites.add(key);
  if (companionProposalRequest?.context === `${worldId}:${playerId}`) companionProposalRequest.invalidated = true;
  try {
    const current = () => isCurrent() && companionContextIsCurrent(worldId, playerId) && companionContextEpoch === epoch;
    if (choice.operation === "end") {
      const sessions = await api(`/api/worlds/${worldId}/companions`);
      if (!current()) return null;
      if (!sessions.some(session => session.id === choice.session_id && session.companion_id === otherId)) {
        throw new Error("这段同行已经变化，请重新查看当前安排。");
      }
    } else {
      const proposals = await api(`/api/worlds/${worldId}/companion-proposals?character_id=${encodeURIComponent(otherId)}`);
      if (!current()) return null;
      if (choice.operation === "propose") {
        if (proposals.some(companionPending)) throw new Error("你们已有待回应的同行邀请，请先查看或回应它。");
        if (!canInviteCompanion(characterById(otherId))) throw new Error("请先走近对方，再商量新的短时同行。");
        const sessions = await api(`/api/worlds/${worldId}/companions`);
        if (!current()) return null;
        if (sessions.some(session => session.companion_id === otherId)) throw new Error("你们已经在同行，请先查看当前安排。");
      } else {
        const proposal = proposals.find(item => item.id === choice.proposal_id);
        if (!proposal || companionOtherId(proposal, playerId) !== otherId || !companionPending(proposal)) throw new Error("这份同行邀请已变化或过期，请重新查看。");
        const role = choice.operation === "cancel" ? "proposer_id" : "recipient_id";
        if (proposal[role] !== playerId) throw new Error("请只表达本人对这份邀请的选择。");
        if (choice.operation !== "cancel" && !canInviteCompanion(characterById(otherId))) throw new Error("请先走近对方，再当面回应这份同行邀请。");
        if (choice.operation === "counter" && proposal.minutes === choice.minutes) throw new Error("请填写与原邀请不同的同行时长。");
      }
    }
    if (!current() || state.busy || state.responsePending) return null;
    const result = await executeNaturalSocialChoice(choice.statement, {
      target_character_id: otherId, choice_evidence: choice.statement, companion_choice: choice,
    });
    if (result && companionContextIsCurrent(worldId, playerId)) {
      companionProposalReadAt = 0;
      await renderMyCompanionProposals(true, otherId);
    }
    return result;
  } finally {
    companionPendingWrites.delete(key);
  }
}

function inviteCompanion(characterId, minutes, words = "", isCurrent = () => true) {
  if (!Number.isInteger(minutes) || minutes < 5 || minutes > 240) return Promise.reject(new Error("请选择5至240世界分钟的同行时长。"));
  return submitCompanionChoice(characterId, {operation: "propose", minutes, statement: companionStatement("propose", minutes, words)}, isCurrent);
}

function endCompanion(sessionId, isCurrent = () => true) {
  const session = state.living?.companions?.find(item => item.id === sessionId);
  if (!session) return Promise.reject(new Error("请重新查看当前正在进行的同行。"));
  return submitCompanionChoice(session.companion_id, {operation: "end", session_id: sessionId,
    statement: "我决定结束这段同行，双方保留已经走过的路程和当前真实位置。"}, isCurrent);
}

function companionChoiceButton(parent, label, otherId, choice, isCurrent, notice) {
  const button = document.createElement("button"); button.type = "button";
  button.className = "button secondary"; button.textContent = label; button.dataset.companionAction = choice.operation;
  button.addEventListener("click", async () => {
    if (button.disabled || !isCurrent() || state.busy || state.responsePending) return;
    const controls = parent.querySelectorAll("button[data-companion-action]"); controls.forEach(item => { item.disabled = true; });
    try {
      const result = await submitCompanionChoice(otherId, choice, isCurrent);
      if (!result && isCurrent()) notice.textContent = "本次选择尚未保存，请保留原话并稍后重试。";
    } catch (error) { if (isCurrent()) notice.textContent = error.message; }
    finally { if (isCurrent()) controls.forEach(item => { item.disabled = false; }); }
  });
  parent.append(button); return button;
}

function appendCompanionForm(parent, otherId, isCurrent, proposal = null) {
  const form = document.createElement("form"); form.className = "long-term-form";
  form.dataset.companionForm = proposal ? "counter" : "propose";
  const field = (label, input) => {
    const wrap = document.createElement("label"); wrap.textContent = label;
    input.setAttribute("aria-label", label); wrap.append(input); form.append(wrap); return input;
  };
  const minutes = field(proposal ? "希望改为同行的时长（世界分钟）" : "同行时长（世界分钟）", document.createElement("input"));
  minutes.type = "number"; minutes.min = "5"; minutes.max = "240"; minutes.step = "1";
  minutes.required = true; minutes.value = String(proposal?.minutes || 60);
  const speech = field(proposal ? "想怎样商量另一段同行时长" : "想怎样邀请对方同行", document.createElement("textarea"));
  speech.maxLength = 100; speech.placeholder = "说说自己的想法，也给对方保留拒绝或调整的空间。";
  const notice = companionLine(form, proposal ? "这是一份新的时长提议，对方另行接受后才成立。" : "这里只提出你的邀请；对方独立回应后，才可能开始同行。", "field-note");
  notice.setAttribute("aria-live", "polite");
  const submit = document.createElement("button"); submit.type = "submit";
  submit.className = "button primary"; submit.textContent = proposal ? "提出另一段同行时长" : "邀请同行";
  submit.dataset.companionAction = proposal ? "counter" : "propose"; form.append(submit);
  let pending = false;
  const sync = () => {
    const value = Number(minutes.value);
    minutes.setCustomValidity(!Number.isInteger(value) || value < 5 || value > 240 ? "请输入5至240之间的整数分钟。"
      : proposal && value === proposal.minutes ? "请填写与原邀请不同的同行时长。" : "");
    submit.disabled = pending || !isCurrent() || Boolean(minutes.validationMessage);
  };
  minutes.addEventListener("input", sync); sync();
  form.addEventListener("submit", async event => {
    event.preventDefault(); sync();
    if (pending || submit.disabled || !isCurrent() || state.busy || state.responsePending || !form.reportValidity()) return;
    pending = true; submit.disabled = true; minutes.disabled = true; speech.disabled = true;
    let message = "";
    try {
      const value = Number(minutes.value);
      const choice = proposal ? {operation: "counter", proposal_id: proposal.id, minutes: value,
        statement: companionStatement("counter", value, speech.value)} : null;
      const result = choice ? await submitCompanionChoice(otherId, choice, isCurrent)
        : await inviteCompanion(otherId, value, speech.value, isCurrent);
      if (!isCurrent()) return;
      if (result) {
        speech.value = "";
        message = result.status === "active" ? "双方已实际确认同行。" : "本人提议已记录，结果以对方真实回应和当前安排为准。";
      } else message = "本次选择尚未保存，你填写的原话仍保留，请稍后重试。";
    } catch (error) { message = error.message; }
    finally {
      pending = false;
      if (isCurrent()) { minutes.disabled = false; speech.disabled = false; sync(); if (message) notice.textContent = message; }
    }
  });
  parent.append(form); return form;
}

function companionProposalRow(proposal, playerId, isCurrent) {
  const row = document.createElement("article"); row.className = "life-item";
  const otherId = companionOtherId(proposal, playerId);
  const rowCurrent = () => isCurrent() && row.isConnected;
  const pending = companionPending(proposal), own = proposal.proposer_id === playerId;
  const status = proposal.status === "pending" && !pending ? "expired" : proposal.status;
  companionLine(row, `${own ? "你向" : "来自"}${companionKnownName(otherId)}${own ? "提出" : "的"}同行邀请 · ${companionProposalStatuses[status] || status}`);
  companionLine(row, `约定同行${proposal.minutes}个世界分钟${proposal.expires_at ? `；回应截止 ${formatWorldTime(proposal.expires_at)}` : ""}。`);
  if (proposal.parent_proposal_id) companionLine(row, "这份邀请来自对上一份时长的重新商量，仍需另一方明确回应。");
  if (proposal.statement) companionLine(row, `${own ? "你的邀请" : "对方的邀请"}：${proposal.statement}`);
  if (proposal.response) companionLine(row, `回应：${proposal.response}`);
  const notice = companionLine(row, "", "field-note"); notice.setAttribute("aria-live", "polite");
  if (pending && own) companionChoiceButton(row, "撤回本人的同行邀请", otherId,
    {operation: "cancel", proposal_id: proposal.id, statement: "我撤回本人这份尚未成立的同行邀请，保留此前已经发生的事实。"}, rowCurrent, notice);
  if (pending && !own) {
    companionChoiceButton(row, `接受这份${proposal.minutes}分钟的同行邀请`, otherId,
      {operation: "accept", proposal_id: proposal.id, statement: `我明确接受这份记录中的${proposal.minutes}分钟短时同行邀请，双方仍可随时结束。`}, rowCurrent, notice);
    companionChoiceButton(row, "婉拒这份同行邀请", otherId,
      {operation: "decline", proposal_id: proposal.id, statement: "谢谢你的邀请，我本人这次不接受这份同行安排。"}, rowCurrent, notice);
    const details = document.createElement("details"), summary = document.createElement("summary"); summary.textContent = "商量另一段同行时长";
    details.append(summary); row.append(details); appendCompanionForm(details, otherId, rowCurrent, proposal);
  }
  return row;
}

async function appendCompanionProposals(characterId, request, providedProposals = null) {
  const player = currentPlayer();
  if (!companionProfileIsCurrent(request) || !player || player.id === characterId || document.querySelector("#companion-proposals-profile")) return;
  const panel = document.createElement("section"); panel.id = "companion-proposals-profile";
  const heading = document.createElement("h2"); heading.textContent = "商量短时同行"; panel.append(heading);
  companionLine(panel, "邀请、时长反提与接受分别表达。对方可以拒绝或提出自己的安排，双方确认后才开始真实同行。");
  elements.characterProfile.append(panel);
  const isCurrent = () => companionProfileIsCurrent(request) && panel.isConnected;
  try {
    const [proposals, sessions] = await Promise.all([
      providedProposals || api(`/api/worlds/${request.worldId}/companion-proposals?character_id=${encodeURIComponent(characterId)}`),
      api(`/api/worlds/${request.worldId}/companions`),
    ]);
    if (!isCurrent()) return;
    const ownProposals = proposals.filter(proposal => [proposal.proposer_id, proposal.recipient_id].includes(player.id)
      && companionOtherId(proposal, player.id) === characterId);
    request.proposalSignature = JSON.stringify(ownProposals);
    request.sessionSignature = JSON.stringify(sessions.filter(session => session.companion_id === characterId));
    for (const proposal of ownProposals) panel.append(companionProposalRow(proposal, player.id, isCurrent));
    const session = sessions.find(item => item.companion_id === characterId);
    if (session) {
      const row = document.createElement("div"); row.className = "life-item";
      companionLine(row, `正在与你同行 · 约定至 ${formatWorldTime(session.until_world_time)}`);
      const notice = companionLine(row, "", "field-note");
      companionChoiceButton(row, "结束同行", characterId, {operation: "end", session_id: session.id,
        statement: "我决定结束这段同行，双方保留已经走过的路程和当前真实位置。"}, () => isCurrent() && row.isConnected, notice);
      panel.append(row);
    } else if (!ownProposals.some(companionPending) && canInviteCompanion(characterById(characterId))) {
      appendCompanionForm(panel, characterId, isCurrent);
    }
  } catch { if (isCurrent()) companionLine(panel, "暂时无法读取同行邀请，请稍后重新打开档案。", "field-note"); }
}

async function refreshCompanionProfile(request, proposals = null) {
  if (!companionProfileIsCurrent(request)) return;
  document.querySelector("#companion-proposals-profile")?.remove();
  await appendCompanionProposals(request.characterId, request, proposals);
}

async function syncCompanionProfile(proposals, playerId, changedCharacterId = null) {
  const request = companionProfileContext;
  if (!companionProfileIsCurrent(request)) return;
  const own = proposals.filter(item => [item.proposer_id, item.recipient_id].includes(playerId)
    && companionOtherId(item, playerId) === request.characterId);
  const sessions = (state.living?.companions || []).filter(item => item.companion_id === request.characterId);
  if (changedCharacterId === request.characterId || request.proposalSignature !== JSON.stringify(own)
      || request.sessionSignature !== JSON.stringify(sessions)) await refreshCompanionProfile(request, own);
}

async function renderMyCompanionProposals(force = false, changedCharacterId = null) {
  const player = currentPlayer(), worldId = state.worldId;
  if (companionProfileContext && !companionContextIsCurrent(companionProfileContext.worldId, companionProfileContext.playerId)) companionProfileContext = null;
  if (companionIntentFormContext && !companionContextIsCurrent(companionIntentFormContext.worldId, companionIntentFormContext.playerId)) companionIntentFormContext = null;
  if (!player || !worldId || !state.living) {
    if (companionProposalRequest) companionProposalRequest.invalidated = true;
    if (companionProposalContext) companionContextEpoch += 1;
    document.querySelector("#my-companion-proposals")?.remove(); companionProposalContext = "";
    companionProposalSignature = ""; companionProposalReadAt = 0; return;
  }
  const context = `${worldId}:${player.id}`;
  let panel = document.querySelector("#my-companion-proposals");
  if (!panel) {
    const anchor = document.querySelector("#companion-party") || elements.sceneCharacters;
    if (!anchor) return;
    panel = document.createElement("section"); panel.id = "my-companion-proposals";
    panel.setAttribute("aria-labelledby", "my-companion-proposals-heading"); anchor.after(panel);
  }
  if (companionProposalContext !== context) {
    if (companionProposalRequest) companionProposalRequest.invalidated = true;
    companionContextEpoch += 1;
    companionProposalContext = context; companionProposalSignature = ""; companionProposalReadAt = 0;
    panel.replaceChildren(); const heading = document.createElement("h3"); heading.id = "my-companion-proposals-heading";
    heading.textContent = "我的同行邀请"; panel.append(heading); companionLine(panel, "正在读取本人参与的同行邀请……");
  }
  if (companionProposalRequest?.context === context) {
    if (!force) return;
    // 哥哥，新操作之后重新读取，不必等旧查询迟迟返回；旧回包仍会核对请求身份。
    companionProposalRequest.invalidated = true;
    companionProposalRequest = null;
  }
  if (!force && Date.now() - companionProposalReadAt < 4000) return;
  const request = {context, invalidated: false}; request.done = new Promise(resolve => { request.finish = resolve; }); companionProposalRequest = request;
  try {
    const proposals = await api(`/api/worlds/${worldId}/companion-proposals`);
    if (companionProposalRequest !== request || request.invalidated || !panel.isConnected || !companionContextIsCurrent(worldId, player.id)) return;
    companionProposalReadAt = Date.now(); panel.querySelector(".companion-read-error")?.remove();
    const signature = JSON.stringify([proposals, state.living?.companions || []]);
    if (signature === companionProposalSignature) {
      if (changedCharacterId) await syncCompanionProfile(proposals, player.id, changedCharacterId);
      return;
    }
    companionProposalSignature = signature;
    panel.replaceChildren(); const heading = document.createElement("h3"); heading.id = "my-companion-proposals-heading";
    heading.textContent = "我的同行邀请"; panel.append(heading);
    companionLine(panel, "这里只办理本人参与的邀请，待回应不表示已经开始同行。");
    if (!proposals.length) companionLine(panel, "目前没有已记录的同行邀请，可以走近人物后从档案商量。");
    const isCurrent = () => panel.isConnected && companionContextIsCurrent(worldId, player.id);
    for (const proposal of proposals.filter(item => [item.proposer_id, item.recipient_id].includes(player.id))) {
      panel.append(companionProposalRow(proposal, player.id, isCurrent));
    }
    await syncCompanionProfile(proposals, player.id, changedCharacterId);
  } catch {
    if (companionProposalRequest === request && !request.invalidated && panel.isConnected && companionContextIsCurrent(worldId, player.id)
        && !panel.querySelector(".companion-read-error")) companionLine(panel, "暂时无法刷新同行邀请，已有记录可能变化，请稍后重试。", "companion-read-error field-note");
  } finally { if (companionProposalRequest === request) companionProposalRequest = null; request.finish(); }
}

intentFormLabels.companion = "提出短时同行邀请";
const companionOriginalIntentForm = openIntentForm;
let companionIntentFormContext = null;
openIntentForm = function(preview) {
  companionOriginalIntentForm(preview);
  if (preview.operation === "companion") {
    companionIntentFormContext = {worldId: state.worldId, playerId: currentPlayer()?.id, preview};
    for (const option of elements.intentFormFields.querySelectorAll("select[name=target_character_id] option")) {
      if (!option.value) continue;
      const person = state.living?.people?.find(item => item.id === option.value);
      option.textContent = `${companionKnownName(option.value)}${person?.species_name ? ` · ${person.species_name}` : ""}`;
    }
    const input = addIntentField("同行时长（世界分钟）", "minutes", preview.minutes || 60, "number");
    input.min = "5"; input.max = "240"; input.step = "1";
    const words = addIntentField("想怎样邀请对方同行", "companion_statement", String(preview.route_detail || "").slice(0, 100)); words.maxLength = 100;
    elements.intentFormNote.textContent = "确认只保存你的邀请；对方可以独立接受、婉拒或商量另一段时长，回应前尚未同行。";
  } else companionIntentFormContext = null;
};
elements.intentForm.addEventListener("submit", async event => {
  const request = companionIntentFormContext, preview = state.intentPreview;
  if (preview?.operation !== "companion") return;
  event.preventDefault(); event.stopImmediatePropagation();
  if (!request || request.pending || request.preview !== preview || !companionContextIsCurrent(request.worldId, request.playerId) || state.busy || state.responsePending) return;
  const values = Object.fromEntries(new FormData(elements.intentForm).entries());
  const target = preview.target_options.find(item => item.id === values.target_character_id), minutes = Number(values.minutes);
  if (!target || !Number.isInteger(minutes) || minutes < 5 || minutes > 240) return showToast("请选择在场人物与5至240世界分钟的时长", true);
  request.pending = true;
  const controls = elements.intentForm.querySelectorAll("input, textarea, select, button");
  controls.forEach(input => { input.disabled = true; });
  try {
    const result = await inviteCompanion(target.id, minutes, values.companion_statement,
      () => companionIntentFormContext === request && companionContextIsCurrent(request.worldId, request.playerId));
    if (result && companionIntentFormContext === request) elements.intentFormDialog.close();
  } catch (error) { if (companionIntentFormContext === request) showToast(error.message, true); }
  finally { request.pending = false; if (companionIntentFormContext === request) controls.forEach(input => { input.disabled = false; }); }
}, {capture: true});

function appendCompanionAppearance(characterId, request) {
  if (!companionProfileIsCurrent(request)) return;
  const actions = elements.characterProfile.querySelector(".profile-actions"), person = state.living?.people?.find(item => item.id === characterId);
  if (actions && person && !elements.characterProfile.querySelector(".companion-species")) {
    const species = document.createElement("p"); species.className = "companion-species";
    species.textContent = `眼前所见：${person.species_name}`; actions.before(species);
  }
}

const companionOriginalProfile = openCharacterProfile;
openCharacterProfile = async function(characterId) {
  const request = {worldId: state.worldId, playerId: currentPlayer()?.id, characterId}; companionProfileContext = request;
  await companionOriginalProfile(characterId);
  if (!companionProfileIsCurrent(request)) return;
  appendCompanionAppearance(characterId, request); await appendCompanionProposals(characterId, request);
};
const companionOriginalKnownProfile = openKnownProfile;
openKnownProfile = function(characterId) {
  const request = {worldId: state.worldId, playerId: currentPlayer()?.id, characterId}; companionProfileContext = request;
  companionOriginalKnownProfile(characterId); appendCompanionAppearance(characterId, request); appendCompanionProposals(characterId, request);
};

const companionOriginalNearby = renderNearby;
renderNearby = function(nearby) {
  companionOriginalNearby(nearby);
  [...elements.nearbyCharacters.children].forEach((row, index) => {
    const person = state.living?.people?.find(item => item.id === nearby[index]?.id), description = row.querySelector("small");
    if (description && person) description.textContent += ` · ${person.species_name}${companionFor(person.id) ? " · 正在同行" : ""}`;
  });
};

const companionOriginalLiving = renderLiving;
renderLiving = function() {
  companionOriginalLiving();
  let panel = document.querySelector("#companion-party");
  if (!panel) { panel = document.createElement("div"); panel.id = "companion-party"; panel.className = "tool-list"; elements.sceneCharacters.after(panel); }
  renderCompanionPanel(panel); renderMyCompanionProposals();
  const aside = document.querySelector("#scene-aside-characters"); let asidePanel = document.querySelector("#companion-aside");
  if (aside && !asidePanel) { asidePanel = document.createElement("div"); asidePanel.id = "companion-aside"; aside.after(asidePanel); }
  if (asidePanel) renderCompanionPanel(asidePanel);
};

function renderCompanionPanel(panel) {
  panel.replaceChildren();
  const worldId = state.worldId, playerId = currentPlayer()?.id;
  if (!worldId || !playerId) { panel.hidden = true; return; }
  for (const session of state.living?.companions || []) {
    const row = document.createElement("div"); row.className = "life-item";
    const label = document.createElement("span"); label.textContent = `${companionKnownName(session.companion_id)}与你同行 · 至${formatWorldTime(session.until_world_time)}`;
    const button = document.createElement("button"); button.type = "button"; button.className = "button secondary"; button.textContent = "结束同行";
    button.addEventListener("click", async () => {
      const isCurrent = () => row.isConnected && companionContextIsCurrent(worldId, playerId);
      if (button.disabled || !isCurrent()) return;
      button.disabled = true;
      try { await endCompanion(session.id, isCurrent); }
      catch (error) { if (isCurrent()) showToast(error.message, true); }
      finally { if (isCurrent()) button.disabled = false; }
    });
    row.append(label, button); panel.append(row);
  }
  panel.hidden = !panel.childElementCount;
}

const companionOriginalAside = renderSceneAside;
renderSceneAside = function() {
  companionOriginalAside();
  const player = currentPlayer(); if (!player) return;
  const nearby = nearbyCharacters(player);
  document.querySelectorAll("#scene-aside-characters li small").forEach((description, index) => {
    const person = state.living?.people?.find(item => item.id === nearby[index]?.id);
    if (person) description.textContent += ` · ${person.species_name}${companionFor(person.id) ? " · 正在同行" : ""}`;
  });
};
