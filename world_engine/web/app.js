"use strict";

const state = {
  worlds: [], worldId: null, snapshot: null, events: [], logs: [], adjudications: [], health: null,
  agentRuns: [], agentProposals: [],
  registrations: [], constructionProjects: [],
  busy: false, currentRoom: window.location.hash.slice(1) || "scene", eventFilter: "all",
  liveClockBase: null, liveClockCapturedAt: 0, toastTimer: null,
  lastClockPaintAt: 0,
  mapView: { scale: 1, x: 0, y: 0, dragging: false, moved: false, startX: 0, startY: 0, originX: 0, originY: 0 },
  relationshipPositions: new Map(), relationshipKey: "", notebook: [],
  mapTileKey: "", inspectedMap: null, worldMapViewBeforeDetail: null, pendingMapView: null, detailExitArmedUntil: 0, setupMapScale: 1,
  activeMap: null, mapFiles: [], mapImage: null, manualView: false,
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const elements = {
  dashboard: $("#dashboard"), emptyState: $("#empty-state"), worldSelect: $("#world-select"),
  connectionStatus: $("#connection-status"), refreshButton: $("#refresh-button"), playerSetup: $("#player-setup"),
  playerContent: $$('[data-player-content]'), sideNavigation: $("#side-navigation"), mobileNavToggle: $("#mobile-nav-toggle"),
  sceneLocation: $("#scene-location"), sceneTime: $("#scene-time"), sceneSpeed: $("#scene-speed"),
  sceneCoordinate: $("#scene-coordinate"), navLocation: $("#nav-location"), navCoordinate: $("#nav-coordinate"),
  playerName: $("#player-name"), playerIdentity: $("#player-identity"), playerObjective: $("#player-objective"),
  playerMonogram: $("#player-monogram"), playerHealth: $("#player-health"), playerHealthBar: $("#player-health-bar"),
  playerEnergy: $("#player-energy"), playerEnergyBar: $("#player-energy-bar"), playerSatiety: $("#player-satiety"),
  playerSatietyBar: $("#player-satiety-bar"), playerMoney: $("#player-money"), sceneNarration: $("#scene-narration"),
  sceneCharacters: $("#scene-characters"), nearbyCharacters: $("#nearby-characters"), storyCards: $("#story-cards"),
  locationMap: $("#location-map"), actionResult: $("#action-result"), combatPanel: $("#combat-panel"),
  combatAllies: $("#combat-allies"), combatEnemies: $("#combat-enemies"), playerSkills: $("#player-skills"),
  playerBag: $("#player-bag"), playerGear: $("#player-gear"), playerIntentForm: $("#player-intent-form"),
  playerIntent: $("#player-intent"), suggestedActions: $("#suggested-actions"),
  playerCreateForm: $("#player-create-form"), playerCreateName: $("#player-create-name"),
  playerCreateIdentity: $("#player-create-identity"), playerCreateLocation: $("#player-create-location"),
  playerCreateLocationName: $("#player-create-location-name"), playerCreateMapMarkers: $("#player-create-map-markers"), setupMapShell: $("#setup-map-shell"), setupMapCamera: $("#setup-map-camera"),
  playerCreateSecondaryLocations: $("#player-create-secondary-locations"), playerCreateTraits: $("#player-create-traits"),
  playerCreateGoal: $("#player-create-goal"), mapShell: $("#world-map-shell"), mapCamera: $("#map-camera"),
  mapTileLayer: $("#map-tile-layer"), mapPoints: $("#map-points"), mapCoordinate: $("#map-coordinate"),
  mapBounds: $("#map-bounds"), mapZoom: $("#map-zoom"), mapHoverCoordinate: $("#map-hover-coordinate"), mapLayerLegend: $("#map-layer-legend"),
  mapRoute: $("#map-route"), mapRoutePolyline: $("#map-route-polyline"),
  mapStyleSelect: $("#map-style"),
  mapContextName: $("#map-context-name"),
  mapZoomIn: $("#map-zoom-in"), mapZoomOut: $("#map-zoom-out"), mapReset: $("#map-reset"),
  eventList: $("#event-list"), logList: $("#log-list"), eventFilters: $("#event-filters"), relationshipGraph: $("#relationship-graph"),
  characterDialog: $("#character-dialog"), characterProfile: $("#character-profile"), characterDialogClose: $("#character-dialog-close"),
  playerProfileButton: $("#player-profile-button"), notebookList: $("#notebook-list"), notebookForm: $("#notebook-form"),
  notebookId: $("#notebook-id"), notebookTitle: $("#notebook-title"), notebookBody: $("#notebook-body"),
  notebookCharacter: $("#notebook-character"), notebookLocation: $("#notebook-location"), notebookEvent: $("#notebook-event"),
  notebookSearch: $("#notebook-search"), notebookNew: $("#notebook-new"), notebookDelete: $("#notebook-delete"),
  worldTitle: $("#world-title"), worldStatus: $("#world-status"), worldTime: $("#world-time"), timeDescription: $("#time-description"),
  speedValue: $("#speed-value"), speedPresets: $("#speed-presets"), customSpeedForm: $("#custom-speed-form"),
  customSpeed: $("#custom-speed"), nextAdjudication: $("#next-adjudication"), worldVersion: $("#world-version"),
  tickCount: $("#tick-count"), providerName: $("#provider-name"), offlinePolicy: $("#offline-policy"),
  workerStatus: $("#worker-status"), workerStatusDot: $("#worker-status-dot"), lastHeartbeat: $("#last-heartbeat"),
  heartbeatButton: $("#heartbeat-button"), adjudicateButton: $("#adjudicate-button"), syncHistoryButton: $("#sync-history-button"),
  interventionCharacter: $("#intervention-character"), interventionButton: $("#intervention-button"),
  characterCount: $("#character-count"), characterGrid: $("#character-grid"), adjudicationList: $("#adjudication-list"),
  registrationCount: $("#registration-count"), registrationList: $("#registration-list"), constructionList: $("#construction-list"),
  movementStatus: $("#movement-status"), movementTitle: $("#movement-title"), movementDetail: $("#movement-detail"),
  movementProgressBar: $("#movement-progress-bar"), movementProgressText: $("#movement-progress-text"), movementCancel: $("#movement-cancel"),
  playerTransport: $("#player-transport"), transportType: $("#transport-type"), transportSpeed: $("#transport-speed"), transportHint: $("#transport-hint"),
  busyOverlay: $("#busy-overlay"), busyMessage: $("#busy-message"), toast: $("#toast"),
};

const eventLabels = { attack: "战斗", socialize: "交谈", travel: "开始行旅", movement_started: "开始移动", movement_cancelled: "取消移动", movement_encounter: "途中相遇", movement_arrived: "抵达目标", rest: "休息", eat: "进食", work: "工作", gather: "采集", use: "使用", idle: "观察", rejected: "未能成立", target_down: "击倒", "world.initial": "世界初启", "world.major_death": "重大死亡", "world.adjudication": "世界裁判", "world.clock_rate_changed": "流速调整", "world.player_defeat": "玩家败北", "world.warden_intervention": "守卫介入", world_created: "世界初启", player_created: "旅人到来" };
const movementTypeLabels = { land: "陆行", flight: "飞行", ship: "轮船", underground: "钻地", water: "水行" };

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try { message = (await response.json()).detail || message; } catch (_) { /* 保留通用错误 */ }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function setBusy(busy, message = "正在处理") {
  state.busy = busy; elements.busyMessage.textContent = message; elements.busyOverlay.hidden = !busy;
  $$('button, input, textarea, select').forEach((node) => { if (!node.closest("#character-dialog")) node.disabled = busy; });
}

function showToast(message, error = false) {
  clearTimeout(state.toastTimer); elements.toast.textContent = message; elements.toast.classList.toggle("error", error); elements.toast.hidden = false;
  state.toastTimer = window.setTimeout(() => { elements.toast.hidden = true; }, 3200);
}

function formatSpeed(value) { const number = Number(value); return `${Number.isInteger(number) ? number : number.toFixed(1)}×`; }
function formatWorldTime(value, withTime = true) {
  if (!value) return "—"; const date = new Date(value); if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric", ...(withTime ? { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false } : {}) }).format(date);
}
function formatRealTime(value) { if (!value) return "—"; return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value)); }
function formatCoordinate(longitude, latitude, precision = 3) {
  if (!Number.isFinite(Number(longitude)) || !Number.isFinite(Number(latitude))) return "坐标未知";
  const lon = Number(longitude); const lat = Number(latitude);
  return `${lon < 0 ? "W" : "E"} ${Math.abs(lon).toFixed(precision)}° · ${lat < 0 ? "S" : "N"} ${Math.abs(lat).toFixed(precision)}°`;
}
function currentPlayer() { return state.snapshot?.characters.find((item) => item.is_player && item.is_pov) || null; }
function locationById(id) { return state.snapshot?.locations.find((item) => item.id === id) || null; }
function characterById(id) { return state.snapshot?.characters.find((item) => item.id === id) || null; }
function relationFor(characterId) {
  const player = currentPlayer(); if (!player) return null;
  const candidates = state.snapshot.relationships.filter((rel) => (rel.source_character_id === player.id && rel.target_character_id === characterId) || (rel.target_character_id === player.id && rel.source_character_id === characterId));
  if (!candidates.length) return null;
  return { affinity: Math.round(candidates.reduce((sum, rel) => sum + rel.affinity, 0) / candidates.length), trust: Math.round(candidates.reduce((sum, rel) => sum + rel.trust, 0) / candidates.length) };
}
function relationKind(characterId) { const rel = relationFor(characterId); const score = rel ? rel.affinity + rel.trust : 0; return score >= 50 ? "friend" : score <= -30 ? "enemy" : "neutral"; }
function normalizedEventType(eventType) { return String(eventType || "").replace(/^action\./, ""); }
function distanceKm(aLongitude, aLatitude, bLongitude, bLatitude) {
  const radians = (value) => value * Math.PI / 180; const lat1 = radians(aLatitude); const lat2 = radians(bLatitude);
  const deltaLat = lat2 - lat1; const deltaLon = radians(bLongitude - aLongitude);
  const value = Math.sin(deltaLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(deltaLon / 2) ** 2;
  return 2 * 6371.0088 * Math.asin(Math.min(1, Math.sqrt(value)));
}
function nearbyCharacters(character, radiusKm = 5) { return (state.snapshot?.characters || []).filter((item) => item.id !== character.id && distanceKm(character.longitude, character.latitude, item.longitude, item.latitude) <= radiusKm); }
function currentMovement() { const player = currentPlayer(); return player ? (state.snapshot?.movements || []).find((item) => item.character_id === player.id && item.status === "moving") || null : null; }

async function refreshAll({ reloadWorlds = false, silent = false } = {}) {
  try {
    if (reloadWorlds || !state.worlds.length) {
      state.worlds = await api("/api/worlds"); renderWorldOptions();
      if (!state.worlds.length) { elements.dashboard.hidden = true; elements.emptyState.hidden = false; return; }
      if (!state.worldId || !state.worlds.some((world) => world.id === state.worldId)) state.worldId = state.worlds[0].id;
    }
    elements.worldSelect.value = state.worldId;
    const [snapshot, events, logs, adjudications, health, agentRuns, agentProposals, registrations, constructionProjects] = await Promise.all([
      api(`/api/worlds/${state.worldId}`), api(`/api/worlds/${state.worldId}/events?limit=200&scope=chronicle`),
      api(`/api/worlds/${state.worldId}/events?limit=200&scope=log`),
      api(`/api/worlds/${state.worldId}/adjudications?limit=40`), api("/api/health"),
      api(`/api/worlds/${state.worldId}/agents/runs?limit=30`), api(`/api/worlds/${state.worldId}/agents/proposals?limit=40`),
      api(`/api/worlds/${state.worldId}/registrations?limit=100`), api(`/api/worlds/${state.worldId}/construction-projects`),
    ]);
    const capturedAt = performance.now(); let liveClockBase = new Date(snapshot.world.current_time);
    if (state.snapshot?.world.id === snapshot.world.id && state.liveClockBase) { const previousElapsed = Math.max(0, (capturedAt - state.liveClockCapturedAt) / 1000 * Number(state.snapshot.world.time_scale)); const previousShown = new Date(state.liveClockBase.getTime() + previousElapsed * 1000); const workerFresh = snapshot.world.last_worker_seen_at && Date.now() - new Date(snapshot.world.last_worker_seen_at).getTime() < 180000; if (workerFresh && previousShown > liveClockBase) liveClockBase = previousShown; }
    state.snapshot = snapshot; state.events = events; state.logs = logs; state.adjudications = adjudications; state.health = health;
    state.agentRuns = agentRuns; state.agentProposals = agentProposals;
    state.registrations = registrations; state.constructionProjects = constructionProjects;
    state.liveClockBase = liveClockBase; state.liveClockCapturedAt = capturedAt;
    elements.emptyState.hidden = true; elements.dashboard.hidden = false; setConnection(true); renderAll();
  } catch (error) { setConnection(false); if (!silent) showToast(error.message, true); }
}

function renderWorldOptions() {
  const selected = state.worldId; elements.worldSelect.replaceChildren(...state.worlds.map((world) => { const option = document.createElement("option"); option.value = world.id; option.textContent = world.name; return option; }));
  if (state.worlds.some((world) => world.id === selected)) elements.worldSelect.value = selected;
}
function setConnection(ok) { elements.connectionStatus.dataset.state = ok ? "online" : "offline"; elements.connectionStatus.textContent = ok ? "已同步" : "连接中断"; }

function renderAll() {
  const player = currentPlayer(); elements.playerSetup.hidden = Boolean(player); elements.playerContent.forEach((node) => { node.hidden = !player; });
  if (!player) { renderPlayerCreation(state.snapshot.locations); return; }
  renderPlayerExperience(state.snapshot); renderClock(state.snapshot.world); renderEvents(); renderLogs(); renderStoryCards(); renderMap(); renderRelationshipGraph(state.snapshot);
  renderNotebookReferences(); loadNotebook(); renderCharacters(state.snapshot.characters); renderInterventionOptions(state.snapshot.characters); renderAdjudications(state.adjudications); renderAgentAudit(); renderRegistrations(); switchRoom(state.currentRoom, false);
}

function renderRegistrations() {
  if (!elements.registrationList || !elements.constructionList) return;
  elements.registrationCount.textContent = `${state.registrations.length} 项`;
  elements.registrationList.replaceChildren(...state.registrations.map((item) => { const node = document.createElement("article"); const pending = item.status === "proposed"; node.className = "registration-item"; node.innerHTML = `<strong>${escapeHtml(item.payload?.name || item.payload?.title || item.element_type)}</strong><span>${escapeHtml(item.element_type)} · ${escapeHtml(item.status)}</span>${item.rejection_reason ? `<small>${escapeHtml(item.rejection_reason)}</small>` : ""}${pending ? `<div class="registration-actions"><button class="button primary" data-registration-confirm="${escapeHtml(item.id)}" type="button">确认登记</button><button class="button danger" data-registration-reject="${escapeHtml(item.id)}" type="button">拒绝</button></div>` : ""}`; return node; }));
  if (!state.registrations.length) elements.registrationList.append(emptyElement("p", "暂无注册记录", "empty-list"));
  elements.constructionList.replaceChildren(...state.constructionProjects.map((item) => { const node = document.createElement("article"); node.className = "registration-item"; const canStart = ["planned", "surveyed"].includes(item.status); const canCancel = item.status === "constructing"; node.innerHTML = `<strong>${escapeHtml(item.target_name)}</strong><span>${escapeHtml(item.project_type)} · ${escapeHtml(item.status)} · ${(Number(item.progress) * 100).toFixed(1)}%</span>${canStart ? `<button class="button secondary" data-construction-start="${escapeHtml(item.registration_id)}" type="button">开始施工</button>` : ""}${canCancel ? `<button class="button danger" data-construction-cancel="${escapeHtml(item.registration_id)}" type="button">取消并退款</button>` : ""}`; return node; }));
  if (!state.constructionProjects.length) elements.constructionList.append(emptyElement("p", "暂无建设项目", "empty-list"));
}

function renderAgentAudit() {
  // 只读展示：最近一次场景叙事 + Agent 调用来源 + 提案被拒原因（不暴露隐藏 prompt）。
  try {
    const panel = document.querySelector("#agent-audit-panel") || (() => {
      const node = document.createElement("section");
      node.id = "agent-audit-panel";
      node.className = "agent-audit-panel";
      (elements.adjudicationList?.closest("section") || document.querySelector("#dashboard") || document.body).appendChild(node);
      return node;
    })();
    const narrative = (state.agentProposals || []).find((item) => item.proposal_type === "scene_narrative" && item.validation_status === "accepted");
    const runs = (state.agentRuns || []).slice(0, 12);
    const rejected = (state.agentProposals || []).filter((item) => item.validation_status === "rejected").slice(0, 8);
    const rows = runs.map((run) => `<li>${escapeHtml(run.agent_name)} · ${escapeHtml(run.status)}${run.model_name ? " · " + escapeHtml(run.model_name) : ""}${run.latency_ms != null ? " · " + run.latency_ms + "ms" : ""}${run.input_tokens != null ? " · in " + run.input_tokens : ""}${run.output_tokens != null ? " · out " + run.output_tokens : ""}</li>`).join("");
    const rejectedText = rejected.map((item) => `<li>${escapeHtml(item.proposal_type || "proposal")}${item.actor_id ? " · " + escapeHtml(item.actor_id) : ""} · ${escapeHtml(item.validation_status)}${item.rejection_reason ? "：" + escapeHtml(item.rejection_reason) : ""}</li>`).join("");
    panel.innerHTML = `<h3>Agent 编排来源（只读审计）</h3>` +
      (narrative ? `<p class="agent-narrative">场景叙事：${escapeHtml(narrative.payload?.text || "")}</p>` : "") +
      (rows ? `<ul class="agent-runs">${rows}</ul>` : "<p class=\"muted\">暂无 Agent 调用记录（编排可关闭）。</p>") +
      (rejectedText ? `<h4>提案被拒原因</h4><ul class="agent-rejections">${rejectedText}</ul>` : "");
  } catch (_error) {
    // 前端展示是增强项，任何异常都不影响主循环。
  }
}

function renderPlayerCreation(locations) {
  const selected = elements.playerCreateLocation.value || locations[0]?.id || "";
  elements.playerCreateLocation.replaceChildren(...locations.map((location) => { const option = document.createElement("option"); option.value = location.id; option.textContent = `${location.name} · ${formatCoordinate(location.longitude, location.latitude, 2)}`; return option; }));
  elements.playerCreateLocation.value = locations.some((item) => item.id === selected) ? selected : locations[0]?.id || "";
  const markers = locations.map((location) => { const button = document.createElement("button"); button.type = "button"; button.className = "setup-map-marker"; button.dataset.locationId = location.id; button.style.left = `${((location.longitude + 180) / 360) * 100}%`; button.style.top = `${((90 - location.latitude) / 180) * 100}%`; button.title = `${location.name} · 人口 ${Number(location.resources?.population || 0).toLocaleString()} · ${formatCoordinate(location.longitude, location.latitude, 2)}`; button.innerHTML = `<span>${escapeHtml(location.name)}</span>`; return button; });
  elements.playerCreateMapMarkers.replaceChildren(...markers); updatePlayerCreationMapSelection();
  elements.playerCreateSecondaryLocations.replaceChildren(...locations.slice(0, 9).map((location) => { const button = document.createElement("button"); button.type = "button"; button.textContent = location.name; button.dataset.locationId = location.id; return button; }));
}
function updatePlayerCreationMapSelection() {
  const selected = elements.playerCreateLocation.value; const location = state.snapshot?.locations.find((item) => item.id === selected);
  elements.playerCreateLocationName.textContent = location ? `${location.name} · ${formatCoordinate(location.longitude, location.latitude, 2)}` : "尚未选择";
  $$(".setup-map-marker").forEach((marker) => marker.classList.toggle("selected", marker.dataset.locationId === selected));
}

function renderPlayerExperience(snapshot) {
  const player = currentPlayer(); if (!player) return; const location = locationById(player.current_location_id) || resolveLocationAt(player.longitude, player.latitude); const nearby = nearbyCharacters(player);
  elements.sceneLocation.textContent = location?.name || "未命名之地"; elements.navLocation.textContent = location?.name || "未命名之地";
  const coordinate = formatCoordinate(player.longitude, player.latitude); elements.sceneCoordinate.textContent = coordinate; elements.navCoordinate.textContent = coordinate;
  elements.sceneTime.textContent = formatWorldTime(snapshot.world.current_time); elements.sceneSpeed.textContent = formatSpeed(snapshot.world.time_scale);
  elements.playerName.textContent = player.name; elements.playerIdentity.textContent = player.identity || "旅人"; elements.playerObjective.textContent = player.goals.length ? player.goals.join(" · ") : "自由地经历这个世界"; elements.playerMonogram.textContent = player.name.slice(0, 1);
  setMeter(elements.playerHealth, elements.playerHealthBar, player.health); setMeter(elements.playerEnergy, elements.playerEnergyBar, player.energy); setMeter(elements.playerSatiety, elements.playerSatietyBar, player.satiety); elements.playerMoney.textContent = String(player.money);
  const movement = currentMovement();
  elements.sceneNarration.textContent = movement ? `你正在离开${location?.name || "上一处地点"}，沿着选定方向持续前进。道路、天气与途中相遇都可能改变这段旅程。` : nearby.length ? `你在${location?.name || "这里"}停下脚步。${nearby.map((item) => item.name).join("、")}也在近旁，世界仍在你没有注视的地方继续运转。` : `你此刻独自在${location?.name || "这片土地"}。风景与时间仍在缓慢变化，等待你的下一步。`;
  renderNearby(nearby); renderTravel(snapshot.locations, player); renderTools(player); renderTransport(player); renderMovement(movement); renderSuggestions(nearby, snapshot.locations, player); renderCombatPanel(snapshot);
}
function setMeter(label, bar, value) { const normalized = Math.max(0, Math.min(100, Number(value))); label.textContent = String(value); bar.style.width = `${normalized}%`; }
function renderNearby(nearby) {
  elements.sceneCharacters.replaceChildren(...nearby.map((character) => { const button = document.createElement("button"); button.type = "button"; button.className = `presence-chip ${relationKind(character.id)}`; button.textContent = character.name; button.addEventListener("click", () => openCharacterProfile(character.id)); return button; }));
  if (!nearby.length) elements.sceneCharacters.append(emptyElement("span", "暂时没有别人", "muted-chip"));
  elements.nearbyCharacters.replaceChildren(...nearby.map((character) => { const item = document.createElement("li"); const rel = relationFor(character.id); const activation = character.activation_state === "active" ? "激活" : "背景"; item.innerHTML = `<button type="button"><span class="avatar">${escapeHtml(character.name.slice(0, 1))}</span><span><strong>${escapeHtml(character.name)}</strong><small>${escapeHtml(character.identity || "身份未明")} · ${activation}${rel ? ` · 亲和 ${rel.affinity}` : ""}</small></span><b>查看 →</b></button>`; item.querySelector("button").addEventListener("click", () => openCharacterProfile(character.id)); return item; }));
  if (!nearby.length) elements.nearbyCharacters.append(emptyElement("li", "视野内暂时没有其他人物", "empty-list"));
}
function renderTravel(locations, player) {
  const choices = locations.filter((location) => location.id !== player.location_id).slice(0, 6);
  elements.locationMap.replaceChildren(...choices.map((location) => { const button = document.createElement("button"); button.type = "button"; button.innerHTML = `<span>前往 ${escapeHtml(location.name)}</span><small>${formatCoordinate(location.longitude, location.latitude, 2)}</small>`; button.addEventListener("click", () => fillIntent(`前往${location.name}`)); return button; }));
}
function renderTools(player) {
  renderToolList(elements.playerSkills, player.skills.map((name) => ({ label: name, intent: `使用${name}` })), "尚未习得技能");
  renderToolList(elements.playerBag, player.inventory.map((item) => ({ label: `${item.name}${item.quantity > 1 ? ` ×${item.quantity}` : ""}`, intent: `使用${item.name}` })), "背包还是空的");
  renderToolList(elements.playerGear, player.equipment.map((item) => ({ label: item.name, intent: `使用装备${item.name}` })), "尚未穿戴装备");
}
function renderTransport(player) {
  const vehicles = (state.snapshot.vehicles || []).filter((item) => item.owner_character_id === player.id && item.is_available);
  const selected = player.active_vehicle_id || "";
  elements.playerTransport.replaceChildren(new Option("徒步 · 陆行 · 5 km/h", ""), ...vehicles.map((vehicle) => new Option(`${vehicle.name} · ${movementTypeLabels[vehicle.movement_type] || vehicle.movement_type} · ${Number(vehicle.speed_kmh).toFixed(Number.isInteger(Number(vehicle.speed_kmh)) ? 0 : 1)} km/h`, vehicle.id)));
  elements.playerTransport.value = selected; elements.playerTransport.disabled = Boolean(currentMovement());
  elements.transportType.textContent = movementTypeLabels[player.movement_type] || player.movement_type;
  elements.transportSpeed.textContent = `${Number(player.movement_speed_kmh).toFixed(Number.isInteger(Number(player.movement_speed_kmh)) ? 0 : 1)} km/h`;
  elements.transportHint.textContent = currentMovement() ? "移动中不能更换交通工具。" : vehicles.length ? "切换后下一段移动使用所选交通工具。" : "当前没有其他可用载具，默认徒步。";
}
function renderMovement(movement) {
  elements.movementStatus.hidden = !movement; if (!movement) return;
  const progress = movement.total_distance_km > 0 ? Math.min(1, movement.distance_travelled_km / movement.total_distance_km) : 1;
  elements.movementTitle.textContent = `前往 ${formatCoordinate(movement.destination_longitude, movement.destination_latitude, 2)}`;
  const routeNote = movement.route ? routeSummary(movement.route) : "";
  elements.movementDetail.textContent = `${movementTypeLabels[movement.movement_type] || movement.movement_type} · ${movement.speed_kmh} km/h · 预计 ${formatWorldTime(movement.estimated_arrival_world)}${routeNote}`;
  elements.movementProgressBar.style.width = `${progress * 100}%`; elements.movementProgressText.textContent = `${(progress * 100).toFixed(1)}% · ${movement.distance_travelled_km.toFixed(1)} / ${movement.total_distance_km.toFixed(1)} km`;
}
function routeSummary(route) {
  const parts = [];
  if (route.distance_km) parts.push(`路线 ${Number(route.distance_km).toFixed(1)} km`);
  const surfaces = (route.segments || []).map((segment) => `${segment.surface || "地形"} ${Number(segment.speed_kmh).toFixed(1)}km/h`).slice(0, 3).join("、");
  if (surfaces) parts.push(`地表 ${surfaces}`);
  if (route.requirements && route.requirements.length) parts.push(`需 ${route.requirements.join("、")}`);
  if (route.dataset_status) parts.push(`数据集 ${route.dataset_status === "approved" ? "已审核" : route.dataset_status}`);
  return (parts.length ? ` · ` : "") + parts.join(" · ");
}
function predictedMovementPosition(movement, shownWorldTime) {
  const elapsedHours = Math.max(0, (shownWorldTime.getTime() - new Date(movement.updated_at_world).getTime()) / 3600000);
  if (movement.route && Array.isArray(movement.route.polyline) && movement.route.polyline.length >= 2 && Array.isArray(movement.route.edges)) {
    return predictedRoutePosition(movement, elapsedHours);
  }
  const travelled = Math.min(movement.total_distance_km, movement.distance_travelled_km + movement.speed_kmh * elapsedHours); const progress = movement.total_distance_km > 0 ? travelled / movement.total_distance_km : 1;
  let longitudeDelta = movement.destination_longitude - movement.origin_longitude; if (longitudeDelta > 180) longitudeDelta -= 360; if (longitudeDelta < -180) longitudeDelta += 360;
  let longitude = movement.origin_longitude + longitudeDelta * progress; if (longitude > 180) longitude -= 360; if (longitude < -180) longitude += 360;
  const latitude = movement.origin_latitude + (movement.destination_latitude - movement.origin_latitude) * progress;
  return { longitude, latitude, travelled, progress };
}
function predictedRoutePosition(movement, elapsedHours) {
  const points = movement.route.polyline; const edges = movement.route.edges;
  const cumulative = []; let running = 0.0; edges.forEach((edge) => { running += Number(edge.length_km); cumulative.push(running); });
  const total = cumulative[cumulative.length - 1] || 0;
  const startDistance = Math.min(movement.distance_travelled_km, total); const startIndex = Math.min(movement.route_index || 0, edges.length);
  let distance = startDistance; let index = startIndex; let hoursLeft = Math.max(0, elapsedHours);
  while (hoursLeft > 1e-9 && index < edges.length) {
    const edge = edges[index]; const segStart = cumulative[index] - Number(edge.length_km); const remaining = cumulative[index] - distance; const speed = Number(edge.speed_kmh);
    if (speed <= 0) break; const timeToFinish = remaining / speed;
    if (hoursLeft >= timeToFinish) { hoursLeft -= timeToFinish; distance = cumulative[index]; index += 1; }
    else { distance += hoursLeft * speed; hoursLeft = 0; }
  }
  const travelled = Math.min(distance, total); const progress = total > 0 ? travelled / total : 1;
  let longitude = points[0][0]; let latitude = points[0][1];
  for (let i = 0; i < edges.length; i += 1) { const segStart = cumulative[i] - Number(edges[i].length_km); if (travelled <= cumulative[i] + 1e-9) { const ratio = Number(edges[i].length_km) > 0 ? (travelled - segStart) / Number(edges[i].length_km) : 0; const first = points[i]; const second = points[i + 1]; let lonDelta = second[0] - first[0]; if (lonDelta > 180) lonDelta -= 360; else if (lonDelta < -180) lonDelta += 360; let lon = first[0] + lonDelta * ratio; if (lon > 180) lon -= 360; else if (lon < -180) lon += 360; longitude = lon; latitude = first[1] + (second[1] - first[1]) * ratio; break; } }
  if (travelled >= total - 1e-9) { longitude = points[points.length - 1][0]; latitude = points[points.length - 1][1]; }
  return { longitude, latitude, travelled, progress };
}
function updatePredictedMovement(shownWorldTime, updateText = true) {
  const movement = currentMovement(); if (!movement) return; const position = predictedMovementPosition(movement, shownWorldTime);
  if (updateText) { const coordinate = formatCoordinate(position.longitude, position.latitude, 5); const location = resolveLocationAt(position.longitude, position.latitude); elements.sceneCoordinate.textContent = coordinate; elements.navCoordinate.textContent = coordinate; elements.mapCoordinate.textContent = coordinate; elements.sceneLocation.textContent = location?.name || "无名地域"; elements.navLocation.textContent = location?.name || "无名地域"; elements.movementProgressBar.style.width = `${position.progress * 100}%`; elements.movementProgressText.textContent = `${(position.progress * 100).toFixed(2)}% · ${position.travelled.toFixed(2)} / ${movement.total_distance_km.toFixed(1)} km`; }
  const desiredMap = selectDisplayMap(position.longitude, position.latitude); if (state.activeMap?.id !== desiredMap.id) { renderMap(position); return; }
  const marker = elements.mapPoints.querySelector(".map-point.player"); if (marker) { const projected = projectToMap(position.longitude, position.latitude, desiredMap); marker.dataset.mapX = String(projected.x / 100); marker.dataset.mapY = String(projected.y / 100); updateMapPointScreenPosition(marker); }
}
function renderToolList(container, items, emptyText) { container.replaceChildren(...items.map((item) => { const button = document.createElement("button"); button.type = "button"; button.textContent = item.label; button.addEventListener("click", () => fillIntent(item.intent)); return button; })); if (!items.length) container.append(emptyElement("p", emptyText, "empty-list")); }
function renderSuggestions(nearby, locations, player) {
  const suggestions = [{ label: "观察四周", intent: "仔细观察四周" }, { label: "休息片刻", intent: "停下来休息" }];
  nearby.slice(0, 2).forEach((item) => suggestions.push({ label: `与${item.name}交谈`, intent: `与${item.name}交谈` }));
  locations.filter((item) => item.id !== player.location_id).slice(0, 2).forEach((item) => suggestions.push({ label: `前往${item.name}`, intent: `前往${item.name}` }));
  elements.suggestedActions.replaceChildren(...suggestions.map((suggestion) => { const button = document.createElement("button"); button.type = "button"; button.textContent = suggestion.label; button.addEventListener("click", () => fillIntent(suggestion.intent)); return button; }));
}
function fillIntent(text) { elements.playerIntent.value = text; saveIntentDraft(); elements.playerIntent.focus(); }

function eventCategory(event) { const type = normalizedEventType(event.event_type); if (["world.major_death"].includes(type)) return "conflict"; if (["world.treaty", "world.regime_change"].includes(type)) return "politics"; if (["world.catastrophe"].includes(type)) return "disaster"; return "other"; }
function visibleEvents() { return state.eventFilter === "all" ? state.events : state.events.filter((event) => eventCategory(event) === state.eventFilter); }
function renderEvents() {
  const events = visibleEvents(); elements.eventList.replaceChildren(...events.map(createEventItem));
  if (!events.length) elements.eventList.append(emptyElement("li", "这个筛选下还没有事件", "empty-list"));
}
function renderLogs() {
  elements.logList.replaceChildren(...state.logs.map(createEventItem));
  if (!state.logs.length) elements.logList.append(emptyElement("li", "还没有日常行动日志", "empty-list"));
}
function createEventItem(event) {
  const item = document.createElement("li"); item.className = `event-item event-${eventCategory(event)}`; item.id = `event-${event.id}`;
  const actors = [characterById(event.actor_id)?.name, characterById(event.target_id)?.name].filter(Boolean).join(" · ");
  const type = normalizedEventType(event.event_type);
  item.innerHTML = `<button class="event-card-button" type="button"><span class="event-meta"><b>${escapeHtml(eventLabels[type] || type)}</b><time>${escapeHtml(formatWorldTime(event.occurred_at, false))}</time></span><strong>${escapeHtml(humanizeEventSummary(event.summary))}</strong>${actors ? `<small>${escapeHtml(actors)}</small>` : ""}<span class="event-detail">${escapeHtml(event.payload?.reason || event.payload?.dialogue || "点击展开事件原文")}</span></button>`;
  item.querySelector("button").addEventListener("click", () => item.classList.toggle("expanded")); return item;
}
function renderStoryCards() {
  const player = currentPlayer(); if (!player) return;
  const events = state.events.filter((event) => event.location_id === player.current_location_id || event.actor_id === player.id || event.target_id === player.id).slice(0, 5);
  elements.storyCards.replaceChildren(...events.map((event) => { const item = document.createElement("li"); const type = normalizedEventType(event.event_type); item.innerHTML = `<button type="button"><span>${escapeHtml(eventLabels[type] || type)}</span><strong>${escapeHtml(humanizeEventSummary(event.summary))}</strong><small>${escapeHtml(formatWorldTime(event.occurred_at, false))}</small></button>`; item.querySelector("button").addEventListener("click", () => { switchRoom("chronicle"); requestAnimationFrame(() => $(`#event-${CSS.escape(event.id)}`)?.scrollIntoView({ behavior: "smooth", block: "center" })); }); return item; }));
  if (!events.length) elements.storyCards.append(emptyElement("li", "你尚未感知到可记录的事件", "empty-list"));
}
function humanizeEventSummary(summary) { let text = String(summary || ""); state.snapshot?.characters.forEach((character) => { text = text.split(character.id).join(character.name); }); return text; }

function globalMapDefinition() { return (state.snapshot?.maps || []).find((item) => item.zoom_level === 0 && !item.location_id) || { id: "world", name: "世界地图", map_role: "world", review_status: "approved", min_longitude: -180, max_longitude: 180, min_latitude: -90, max_latitude: 90 }; }
function mapContains(map, longitude, latitude) { return longitude >= map.min_longitude && longitude <= map.max_longitude && latitude >= map.min_latitude && latitude <= map.max_latitude; }
function resolveLocationAt(longitude, latitude) { const matches = (state.snapshot?.locations || []).map((location) => ({ location, distance: distanceKm(longitude, latitude, location.longitude, location.latitude) })).filter((item) => item.distance <= Number(item.location.area_radius_km || 0)); matches.sort((first, second) => Number(second.location.area_priority || 0) - Number(first.location.area_priority || 0) || Number(first.location.area_radius_km || 0) - Number(second.location.area_radius_km || 0) || first.distance - second.distance); return matches[0]?.location || null; }
function locationAndAncestorIds(location) { const byId = new Map((state.snapshot?.locations || []).map((item) => [item.id, item])); const result = new Set(); let current = location; while (current && !result.has(current.id)) { result.add(current.id); current = current.parent_location_id ? byId.get(current.parent_location_id) : null; } return result; }
function selectDisplayMap(longitude, latitude) {
  if (state.inspectedMap) return state.inspectedMap;
  // 城镇详图只通过"放大到详图阈值"(resolveZoomDetailMap 设置 inspectedMap)进入；
  // 否则不管焦点坐标是否落在城镇范围内都显示世界图。这样缩小后能稳定退出城镇，
  // 而不会因为焦点仍在城镇内、被重新切回详图导致无法返回世界地图。
  return globalMapDefinition();
}
function projectToMap(longitude, latitude, map = state.activeMap || globalMapDefinition()) { return { x: ((Number(longitude) - map.min_longitude) / (map.max_longitude - map.min_longitude)) * 100, y: ((map.max_latitude - Number(latitude)) / (map.max_latitude - map.min_latitude)) * 100 }; }
function renderMapTiles(map) {
  if (map.map_role === "detail") { const key = `detail:${map.id}:${map.asset_path}`; if (key !== state.mapTileKey || !elements.mapTileLayer.childElementCount) { const image = document.createElement("img"); image.src = `/world-assets/${map.asset_path}`; image.alt = map.name; image.draggable = false; elements.mapTileLayer.replaceChildren(image); state.mapTileKey = key; } return; }
  const tiles = (state.snapshot?.maps || []).filter((item) => item.map_role === "tile" || (item.zoom_level === 1 && !item.location_id)).sort((a, b) => a.tile_row - b.tile_row || a.tile_column - b.tile_column);
  const overlay = state.mapImage ? `navigation/noryia/${state.mapImage}` : "";
  const key = `world:map:${state.mapImage || ""}:${tiles.map((item) => item.asset_path).join("|")}`; if (key === state.mapTileKey && elements.mapTileLayer.childElementCount) return; state.mapTileKey = key;
  if (tiles.length) { const columns = Math.max(...tiles.map((tile) => Number(tile.tile_column))) + 1; const rows = Math.max(...tiles.map((tile) => Number(tile.tile_row))) + 1; elements.mapTileLayer.replaceChildren(...tiles.map((tile) => { const image = document.createElement("img"); image.src = `/world-assets/${tile.asset_path}`; image.alt = ""; image.draggable = false; image.style.left = `${Number(tile.tile_column) / columns * 100}%`; image.style.top = `${Number(tile.tile_row) / rows * 100}%`; image.style.width = `${100 / columns}%`; image.style.height = `${100 / rows}%`; return image; })); return; }
  const master = (state.snapshot?.maps || []).find((item) => item.zoom_level === 0); if (master) { const image = document.createElement("img"); image.src = `/world-assets/${overlay || master.asset_path}`; image.alt = overlay ? state.mapImage : master.name; image.draggable = false; elements.mapTileLayer.replaceChildren(image); }
}
function renderMap(positionOverride = null) {
  const player = currentPlayer(); if (!player) return;
  let longitude, latitude;
  if (positionOverride) { longitude = positionOverride.longitude; latitude = positionOverride.latitude; }
  else if (state.manualView) { const center = mapCenterCoordinate(); longitude = center.longitude; latitude = center.latitude; }
  else { longitude = player.longitude; latitude = player.latitude; }
  const map = selectDisplayMap(longitude, latitude); const changed = state.activeMap?.id !== map.id; state.activeMap = map; renderMapTiles(map); elements.mapShell.style.setProperty("--map-aspect", `${map.width_pixels || 2552} / ${map.height_pixels || 1304}`); elements.mapContextName.textContent = `${map.name}${map.review_status === "candidate" ? " · 候选" : ""}`; elements.mapCoordinate.textContent = formatCoordinate(longitude, latitude); elements.mapBounds.textContent = `范围：${formatCoordinate(map.min_longitude, map.min_latitude, 2)} ↔ ${formatCoordinate(map.max_longitude, map.max_latitude, 2)}`;
  const points = [];
  if (map.map_role === "detail") elements.mapLayerLegend.textContent = "城镇详细地图";
  if (map.map_role !== "detail") {
    if (state.mapView.scale >= 7) { elements.mapLayerLegend.textContent = "城镇标签"; state.snapshot.locations.filter((location) => mapContains(map, location.longitude, location.latitude)).forEach((location) => points.push(createMapPoint(location.longitude, location.latitude, "location", location.name, map, `人口 ${Number(location.resources?.population || 0).toLocaleString()}`, location.id))); }
    else if (state.mapView.scale >= 3) { elements.mapLayerLegend.textContent = "省份标签（国家兜底）"; (state.snapshot.map_features || []).filter((feature) => ["province", "capital"].includes(feature.feature_type) && mapContains(map, feature.longitude, feature.latitude)).forEach((feature) => points.push(createMapPoint(feature.longitude, feature.latitude, "feature", feature.name, map, feature.feature_type === "province" ? "省份行政中心" : "国家首都"))); }
    else { elements.mapLayerLegend.textContent = "国家标签"; (state.snapshot.map_features || []).filter((feature) => feature.feature_type === "capital" && mapContains(map, feature.longitude, feature.latitude)).forEach((feature) => points.push(createMapPoint(feature.longitude, feature.latitude, "feature", feature.name, map, "国家首都"))); }
  }
  points.push(createMapPoint(player.longitude, player.latitude, "player", "我", map));
  const movement = currentMovement(); if (movement && mapContains(map, movement.destination_longitude, movement.destination_latitude)) points.push(createMapPoint(movement.destination_longitude, movement.destination_latitude, "destination", "目标", map));
  elements.mapPoints.replaceChildren(...points); if (changed) { const restore = state.pendingMapView; state.pendingMapView = null; state.mapView.scale = restore?.scale ?? 1; state.mapView.x = restore?.x ?? 0; state.mapView.y = restore?.y ?? 0; } applyMapTransform();
}
function createMapPoint(longitude, latitude, kind, label, map, detail = "", locationId = null) { const point = document.createElement("span"); const projected = projectToMap(longitude, latitude, map); point.className = `map-point ${kind}`; point.dataset.mapX = String(projected.x / 100); point.dataset.mapY = String(projected.y / 100); if (locationId) point.dataset.locationId = locationId; point.title = `${label}${detail ? ` · ${detail}` : ""} · ${formatCoordinate(longitude, latitude)}`; point.innerHTML = `<i></i><span>${escapeHtml(label)}</span>`; return point; }
function clampMapView() {
  const width = elements.mapShell.clientWidth; const height = elements.mapShell.clientHeight; const view = state.mapView;
  view.x = Math.min(0, Math.max(width * (1 - view.scale), view.x)); view.y = Math.min(0, Math.max(height * (1 - view.scale), view.y));
}
function updateMapPointScreenPosition(point) { const view = state.mapView; const width = elements.mapShell.clientWidth; const height = elements.mapShell.clientHeight; point.style.left = `${view.x + Number(point.dataset.mapX) * width * view.scale}px`; point.style.top = `${view.y + Number(point.dataset.mapY) * height * view.scale}px`; }
function updateMapPointScreenPositions() { elements.mapPoints.querySelectorAll(".map-point").forEach(updateMapPointScreenPosition); updateRouteOverlay(); }
function updateRouteOverlay() {
  const movement = currentMovement(); const polyline = elements.mapRoutePolyline; if (!polyline) return;
  if (!movement || !movement.route || !Array.isArray(movement.route.polyline) || movement.route.polyline.length < 2) { elements.mapRoute.dataset.visible = "false"; polyline.setAttribute("points", ""); return; }
  elements.mapRoute.dataset.visible = "true";
  const map = state.activeMap; if (!map) return;
  const width = elements.mapShell.clientWidth; const height = elements.mapShell.clientHeight; const view = state.mapView;
  const points = movement.route.polyline.map((coordinate) => {
    const projected = projectToMap(coordinate[0], coordinate[1], map);
    return `${(view.x + (projected.x / 100) * width * view.scale).toFixed(1)},${(view.y + (projected.y / 100) * height * view.scale).toFixed(1)}`;
  }).join(" ");
  polyline.setAttribute("points", points);
}
function loadMapFiles() {
  /* 从后端读取存放地图文件夹(navigation/noryia)下的文件名，填充下拉；选哪个就把底图换成哪个。 */
  api("/api/world-map-layers")
    .then((data) => {
      const files = (data.files || []).slice();
      state.mapFiles = files;
      if (elements.mapStyleSelect) {
        elements.mapStyleSelect.replaceChildren(
          new Option("默认世界图", ""),
          ...files.map((name) => new Option(name, name)),
        );
        elements.mapStyleSelect.value = state.mapImage || "";
      }
    })
    .catch(() => { /* 拉取失败则下拉保持当前 */ });
}
function applyMapImage(value) {
  const next = value || null;
  if (next === state.mapImage) return;
  state.mapImage = next;
  renderMap();
}
function applyMapTransform() { clampMapView(); const view = state.mapView; elements.mapCamera.style.width = `${view.scale * 100}%`; elements.mapCamera.style.height = `${view.scale * 100}%`; elements.mapCamera.style.transform = `translate(${view.x}px, ${view.y}px)`; updateMapPointScreenPositions(); elements.mapZoom.textContent = `${Math.round(view.scale * 100)}%`; }
function mapCenterCoordinate() { const rect = elements.mapShell.getBoundingClientRect(); return coordinateFromMapPointer({ clientX: rect.left + rect.width / 2, clientY: rect.top + rect.height / 2 }); }
const DETAIL_MAP_MIN_SCALE = 16;
const DETAIL_MAP_ACTIVATION_PADDING_PX = 12;
const DETAIL_MAP_MAX_PADDING_DEGREES = 0.25;
const DETAIL_MARKER_ACTIVATION_RADIUS_PX = 72;
function mapContainsForDetailActivation(map, coordinate, nextScale) {
  const rect = elements.mapShell.getBoundingClientRect();
  const sourceMap = state.activeMap || globalMapDefinition();
  const longitudePadding = Math.min(DETAIL_MAP_MAX_PADDING_DEGREES, (sourceMap.max_longitude - sourceMap.min_longitude) * DETAIL_MAP_ACTIVATION_PADDING_PX / Math.max(1, rect.width * nextScale));
  const latitudePadding = Math.min(DETAIL_MAP_MAX_PADDING_DEGREES, (sourceMap.max_latitude - sourceMap.min_latitude) * DETAIL_MAP_ACTIVATION_PADDING_PX / Math.max(1, rect.height * nextScale));
  return coordinate.longitude >= map.min_longitude - longitudePadding && coordinate.longitude <= map.max_longitude + longitudePadding && coordinate.latitude >= map.min_latitude - latitudePadding && coordinate.latitude <= map.max_latitude + latitudePadding;
}
function detailMapNearScreenPoint(clientPoint) {
  if (!clientPoint) return null;
  const rect = elements.mapShell.getBoundingClientRect();
  const markers = [...elements.mapPoints.querySelectorAll(".map-point.location[data-location-id]")]
    .map((marker) => ({
      locationId: marker.dataset.locationId,
      distance: Math.hypot(clientPoint.x - (rect.left + Number.parseFloat(marker.style.left || "0")), clientPoint.y - (rect.top + Number.parseFloat(marker.style.top || "0"))),
    }))
    .filter((item) => item.distance <= DETAIL_MARKER_ACTIVATION_RADIUS_PX)
    .sort((first, second) => first.distance - second.distance);
  const locationId = markers[0]?.locationId;
  return locationId ? (state.snapshot?.maps || []).find((map) => map.map_role === "detail" && map.review_status === "approved" && map.location_id === locationId) || null : null;
}
function resolveZoomDetailMap(nextScale, focusCoordinate = null, focusClientPoint = null) {
  if (nextScale < DETAIL_MAP_MIN_SCALE || state.activeMap?.map_role === "detail") return null;
  const markerDetail = detailMapNearScreenPoint(focusClientPoint);
  if (markerDetail) return markerDetail;
  const coordinate = focusCoordinate || mapCenterCoordinate();
  const candidates = (state.snapshot?.maps || [])
    .filter((map) => map.map_role === "detail" && map.review_status === "approved" && mapContainsForDetailActivation(map, coordinate, nextScale))
    .map((map) => ({
      map,
      area: (map.max_longitude - map.min_longitude) * (map.max_latitude - map.min_latitude),
      distance: distanceKm(coordinate.longitude, coordinate.latitude, (map.min_longitude + map.max_longitude) / 2, (map.min_latitude + map.max_latitude) / 2),
    }));
  candidates.sort((first, second) => first.distance - second.distance || first.area - second.area);
  return candidates[0]?.map || null;
}
function enterDetailMap(detail, worldView) {
  state.worldMapViewBeforeDetail = worldView;
  state.inspectedMap = detail;
  state.detailExitArmedUntil = 0;
  state.mapView.scale = 1;
  state.mapView.x = 0;
  state.mapView.y = 0;
  renderMap();
}
function zoomMap(factor, clientX = null, clientY = null) {
  const rect = elements.mapShell.getBoundingClientRect(); const view = state.mapView; const oldScale = view.scale; const nextScale = Math.max(1, Math.min(30, oldScale * factor));
  if (state.inspectedMap && factor < 1 && oldScale <= 1) { if (Date.now() <= state.detailExitArmedUntil) { state.pendingMapView = state.worldMapViewBeforeDetail; state.worldMapViewBeforeDetail = null; state.inspectedMap = null; state.detailExitArmedUntil = 0; renderMap(); } else { state.detailExitArmedUntil = Date.now() + 1800; showToast("城镇地图已处于最小倍率；再次缩小可返回全球地图"); } return; }
  if (state.inspectedMap && factor < 1 && nextScale <= 1) state.detailExitArmedUntil = Date.now() + 1800;
  const focusX = clientX === null ? rect.width / 2 : clientX - rect.left; const focusY = clientY === null ? rect.height / 2 : clientY - rect.top; const focusCoordinate = coordinateFromMapPointer({ clientX: rect.left + focusX, clientY: rect.top + focusY }); const worldX = (focusX - view.x) / oldScale; const worldY = (focusY - view.y) / oldScale;
  const nextView = { scale: nextScale, x: focusX - worldX * nextScale, y: focusY - worldY * nextScale };
  const detail = resolveZoomDetailMap(nextScale, focusCoordinate, { x: rect.left + focusX, y: rect.top + focusY }); if (detail) { state.manualView = true; enterDetailMap(detail, nextView); return; }
  if (nextScale === oldScale) return;
  state.manualView = true;
  if (state.inspectedMap && factor > 1) state.detailExitArmedUntil = 0;
  view.scale = nextView.scale; view.x = nextView.x; view.y = nextView.y; renderMap();
}
function resetMapView() { state.inspectedMap = null; state.worldMapViewBeforeDetail = null; state.pendingMapView = null; state.detailExitArmedUntil = 0; state.manualView = false; state.mapView.scale = 1; state.mapView.x = 0; state.mapView.y = 0; renderMap(); }
function coordinateFromMapPointer(event) {
  const rect = elements.mapShell.getBoundingClientRect(); const view = state.mapView; const x = (event.clientX - rect.left - view.x) / view.scale; const y = (event.clientY - rect.top - view.y) / view.scale;
  const map = state.activeMap || globalMapDefinition(); return { longitude: Math.max(map.min_longitude, Math.min(map.max_longitude, map.min_longitude + x / rect.width * (map.max_longitude - map.min_longitude))), latitude: Math.max(map.min_latitude, Math.min(map.max_latitude, map.max_latitude - y / rect.height * (map.max_latitude - map.min_latitude))) };
}
function startMapMovement(longitude, latitude) {
  if (currentMovement()) { showToast("你已经在移动中，请先取消当前行程", true); return; }
  const player = currentPlayer(); perform("正在规划移动路线", () => api(`/api/worlds/${state.worldId}/player/move`, { method: "POST", body: JSON.stringify({ destination_longitude: longitude, destination_latitude: latitude, vehicle_id: player?.active_vehicle_id || null }) }), `已经开始前往 ${formatCoordinate(longitude, latitude, 2)}`, () => switchRoom("map"));
}

function renderCombatPanel(snapshot) {
  const player = currentPlayer(); if (!player) return; const nearby = nearbyCharacters(player); const enemies = nearby.filter((character) => relationKind(character.id) === "enemy"); const allies = nearby.filter((character) => relationKind(character.id) === "friend"); elements.combatPanel.hidden = !enemies.length; if (!enemies.length) return;
  elements.combatAllies.replaceChildren(createUnitCard(player, "player"), ...allies.map((character) => createUnitCard(character, "friend"))); elements.combatEnemies.replaceChildren(...enemies.map((character) => createUnitCard(character, "enemy")));
}
function createUnitCard(character, kind) { const button = document.createElement("button"); button.type = "button"; button.className = `unit-card ${kind}`; button.innerHTML = `<span><strong>${escapeHtml(character.name)}</strong><small>${escapeHtml(character.identity || "身份未明")}</small></span><i><b style="width:${Math.max(0, Math.min(100, character.health))}%"></b></i><em>${character.health}</em>`; button.addEventListener("click", () => kind === "enemy" ? fillIntent(`攻击${character.name}`) : openCharacterProfile(character.id)); return button; }

function renderRelationshipGraph(snapshot) {
  const player = currentPlayer(); if (!player) return; const connected = snapshot.relationships.filter((rel) => rel.source_character_id === player.id || rel.target_character_id === player.id); const ids = [...new Set(connected.map((rel) => rel.source_character_id === player.id ? rel.target_character_id : rel.source_character_id))]; const key = `${player.id}:${connected.map((rel) => `${rel.source_character_id}:${rel.target_character_id}:${rel.affinity}:${rel.trust}`).join("|")}`;
  if (state.relationshipKey === key && elements.relationshipGraph.childElementCount) return; state.relationshipKey = key;
  const width = 1000, height = 650, center = { x: width / 2, y: height / 2 }; if (!state.relationshipPositions.has(player.id)) state.relationshipPositions.set(player.id, center);
  ids.forEach((id, index) => { if (!state.relationshipPositions.has(id)) { const angle = (index / Math.max(1, ids.length)) * Math.PI * 2 - Math.PI / 2; const radius = 210 + (index % 2) * 58; state.relationshipPositions.set(id, { x: center.x + Math.cos(angle) * radius, y: center.y + Math.sin(angle) * radius }); } });
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg"); svg.setAttribute("viewBox", `0 0 ${width} ${height}`); svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "玩家关系网");
  const lineLayer = document.createElementNS(svg.namespaceURI, "g"); lineLayer.classList.add("relation-lines"); const nodeLayer = document.createElementNS(svg.namespaceURI, "g"); nodeLayer.classList.add("relation-nodes"); svg.append(lineLayer, nodeLayer);
  ids.forEach((otherId) => { const rel = relationFor(otherId) || { affinity: 0, trust: 0 }; const a = state.relationshipPositions.get(player.id), b = state.relationshipPositions.get(otherId); const line = document.createElementNS(svg.namespaceURI, "line"); line.dataset.otherId = otherId; line.setAttribute("x1", a.x); line.setAttribute("y1", a.y); line.setAttribute("x2", b.x); line.setAttribute("y2", b.y); line.setAttribute("class", rel.affinity + rel.trust >= 50 ? "friend" : rel.affinity + rel.trust <= -30 ? "enemy" : "neutral"); line.setAttribute("stroke-width", String(Math.max(1.5, (Math.abs(rel.affinity) + Math.abs(rel.trust)) / 40))); lineLayer.append(line); });
  [player.id, ...ids].forEach((id) => { const character = characterById(id); if (!character) return; const position = state.relationshipPositions.get(id); const group = document.createElementNS(svg.namespaceURI, "g"); group.classList.add("relation-node"); if (id === player.id) group.classList.add("player"); else group.classList.add(relationKind(id)); group.dataset.characterId = id; group.setAttribute("transform", `translate(${position.x} ${position.y})`); group.innerHTML = `<circle r="${id === player.id ? 31 : 24}"></circle><text y="${id === player.id ? 49 : 42}" text-anchor="middle">${escapeHtml(character.name)}</text>`; group.addEventListener("click", () => openCharacterProfile(id)); enableNodeDrag(svg, group, id, lineLayer); nodeLayer.append(group); });
  elements.relationshipGraph.replaceChildren(svg);
}
function enableNodeDrag(svg, group, id, lineLayer) { let dragging = false; const move = (event) => { if (!dragging) return; const point = svg.createSVGPoint(); point.x = event.clientX; point.y = event.clientY; const local = point.matrixTransform(svg.getScreenCTM().inverse()); const position = { x: Math.max(35, Math.min(965, local.x)), y: Math.max(35, Math.min(615, local.y)) }; state.relationshipPositions.set(id, position); group.setAttribute("transform", `translate(${position.x} ${position.y})`); lineLayer.querySelectorAll("line").forEach((line) => { if (line.dataset.otherId === id) { line.setAttribute("x2", position.x); line.setAttribute("y2", position.y); } if (id === currentPlayer()?.id) { line.setAttribute("x1", position.x); line.setAttribute("y1", position.y); } }); };
  group.addEventListener("pointerdown", (event) => { dragging = true; group.setPointerCapture(event.pointerId); event.preventDefault(); }); group.addEventListener("pointermove", move); group.addEventListener("pointerup", (event) => { dragging = false; group.releasePointerCapture(event.pointerId); }); }

async function openCharacterProfile(characterId) {
  const character = characterById(characterId); if (!character) return; const location = locationById(character.current_location_id) || resolveLocationAt(character.longitude, character.latitude); const rel = relationFor(character.id); const activationLabel = character.is_player ? "主视角" : character.activation_state === "active" ? "激活NPC" : "背景NPC"; elements.characterProfile.innerHTML = `<header class="profile-head"><span class="profile-avatar">${escapeHtml(character.name.slice(0, 1))}</span><div><p class="kicker">人物档案 · ${activationLabel}</p><h1>${escapeHtml(character.name)}</h1><p>${escapeHtml(character.identity || "身份未明")} · ${escapeHtml(location?.name || "位置未知")}</p><strong>${formatCoordinate(character.longitude, character.latitude)}</strong></div></header><dl class="profile-stats"><div><dt>生命</dt><dd>${character.health}</dd></div><div><dt>精力</dt><dd>${character.energy}</dd></div><div><dt>饱食</dt><dd>${character.satiety}</dd></div><div><dt>亲和</dt><dd>${rel?.affinity ?? "未知"}</dd></div><div><dt>信任</dt><dd>${rel?.trust ?? "未知"}</dd></div></dl><section><h2>近期回忆</h2><div id="profile-memories"><p class="empty-list">正在读取……</p></div></section><div class="profile-actions"><button class="button primary" data-profile-action="talk" type="button">与其交谈</button>${character.is_player ? "" : `<button class="button danger" data-profile-action="attack" type="button">向其攻击</button>`}<button class="button secondary" data-profile-action="plan" type="button">加入计划</button></div>`;
  elements.characterProfile.querySelectorAll("[data-profile-action]").forEach((button) => button.addEventListener("click", () => { const prefix = { talk: "与", attack: "攻击", plan: "计划与" }[button.dataset.profileAction]; fillIntent(button.dataset.profileAction === "talk" ? `与${character.name}交谈` : `${prefix}${character.name}`); elements.characterDialog.close(); }));
  elements.characterDialog.showModal();
  try { const memories = await api(`/api/worlds/${state.worldId}/characters/${characterId}/memories?limit=8`); const box = $("#profile-memories"); box.replaceChildren(...memories.map((memory) => { const article = document.createElement("article"); article.innerHTML = `<strong>${escapeHtml(memory.summary)}</strong><small>重要度 ${memory.importance} · ${escapeHtml(formatRealTime(memory.created_at))}</small>`; return article; })); if (!memories.length) box.append(emptyElement("p", "还没有可读取的回忆", "empty-list")); } catch (error) { $("#profile-memories").textContent = error.message; }
}

function renderNotebookReferences() {
  const keep = (select, records, label) => { const value = select.value; select.replaceChildren(new Option("无", ""), ...records.map((record) => new Option(label(record), record.id))); if ([...select.options].some((option) => option.value === value)) select.value = value; };
  keep(elements.notebookCharacter, state.snapshot.characters, (item) => item.name); keep(elements.notebookLocation, state.snapshot.locations, (item) => item.name); keep(elements.notebookEvent, state.events.slice(0, 100), (item) => humanizeEventSummary(item.summary).slice(0, 45));
}
function loadNotebook() { let all = []; try { all = JSON.parse(localStorage.getItem("iserra.notebook") || "[]"); } catch (_) { all = []; } state.notebook = Array.isArray(all) ? all.filter((item) => item.world_id === state.worldId) : []; renderNotebookList(); }
function saveNotebookCollection() { let all = []; try { all = JSON.parse(localStorage.getItem("iserra.notebook") || "[]"); } catch (_) { all = []; } const others = Array.isArray(all) ? all.filter((item) => item.world_id !== state.worldId) : []; localStorage.setItem("iserra.notebook", JSON.stringify([...others, ...state.notebook])); }
function renderNotebookList() { const query = elements.notebookSearch.value.trim().toLowerCase(); const records = [...state.notebook].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).filter((item) => !query || `${item.title} ${item.body}`.toLowerCase().includes(query)); elements.notebookList.replaceChildren(...records.map((item) => { const button = document.createElement("button"); button.type = "button"; button.innerHTML = `<strong>${escapeHtml(item.title)}</strong><span>${escapeHtml(item.body.slice(0, 70))}</span><small>${escapeHtml(formatRealTime(item.updated_at))}</small>`; button.addEventListener("click", () => editNotebook(item.id)); return button; })); if (!records.length) elements.notebookList.append(emptyElement("p", "还没有笔记", "empty-list")); }
function resetNotebookForm() { elements.notebookForm.reset(); elements.notebookId.value = ""; elements.notebookDelete.hidden = true; elements.notebookTitle.focus(); }
function editNotebook(id) { const item = state.notebook.find((record) => record.id === id); if (!item) return; elements.notebookId.value = item.id; elements.notebookTitle.value = item.title; elements.notebookBody.value = item.body; elements.notebookCharacter.value = item.linked_character_id || ""; elements.notebookLocation.value = item.linked_location_id || ""; elements.notebookEvent.value = item.linked_event_id || ""; elements.notebookDelete.hidden = false; }

function renderClock(world) {
  elements.worldTitle.textContent = world.name; elements.worldStatus.textContent = world.status === "running" ? "运行中" : world.status; elements.worldTime.textContent = formatWorldTime(world.current_time); elements.timeDescription.textContent = world.time_scale === 0 ? "世界已暂停" : `现实 1 秒推进世界 ${formatSpeed(world.time_scale).replace("×", " 秒")}`; elements.speedValue.textContent = formatSpeed(world.time_scale); elements.nextAdjudication.textContent = formatWorldTime(world.next_adjudication_time); elements.worldVersion.textContent = String(world.version); elements.tickCount.textContent = String(world.tick_count); elements.providerName.textContent = state.health?.decision_provider || "—"; elements.offlinePolicy.textContent = world.offline_policy === "pause" ? "离线冻结" : world.offline_policy; elements.lastHeartbeat.textContent = formatRealTime(world.last_heartbeat_real_time);
  const workerOnline = world.last_worker_seen_at && Date.now() - new Date(world.last_worker_seen_at).getTime() < 180000; elements.workerStatus.textContent = workerOnline ? "自动运行正常" : "自动运行未连接"; elements.workerStatusDot.classList.toggle("online", Boolean(workerOnline));
  elements.speedPresets.querySelectorAll("[data-speed]").forEach((button) => button.classList.toggle("active", Number(button.dataset.speed) === Number(world.time_scale)));
}
function updateLiveClock() {
  if (!state.snapshot || !state.liveClockBase) return; const now = performance.now(); const world = state.snapshot.world; const heartbeatFresh = world.last_worker_seen_at && Date.now() - new Date(world.last_worker_seen_at).getTime() < 180000; const elapsed = heartbeatFresh && world.time_scale > 0 ? (now - state.liveClockCapturedAt) / 1000 * world.time_scale : 0; const shown = new Date(state.liveClockBase.getTime() + elapsed * 1000); const updateText = now - state.lastClockPaintAt >= 200; if (updateText) { elements.sceneTime.textContent = formatWorldTime(shown); elements.worldTime.textContent = formatWorldTime(shown); state.lastClockPaintAt = now; } updatePredictedMovement(shown, updateText);
}
function runLiveFrame() { if (document.visibilityState === "visible") updateLiveClock(); window.requestAnimationFrame(runLiveFrame); }
function renderCharacters(characters) { elements.characterCount.textContent = `${characters.length} 人`; elements.characterGrid.replaceChildren(...characters.map((character) => { const location = locationById(character.current_location_id) || resolveLocationAt(character.longitude, character.latitude); const card = document.createElement("button"); card.type = "button"; card.className = "character-card"; card.innerHTML = `<span class="avatar">${escapeHtml(character.name.slice(0, 1))}</span><span><strong>${escapeHtml(character.name)}</strong><small>${escapeHtml(character.identity || "身份未明")} · ${escapeHtml(location?.name || "位置未知")}</small><em>${formatCoordinate(character.longitude, character.latitude, 2)}</em></span><b>${character.is_player ? "玩家" : character.activation_state === "active" ? "激活" : "背景"}</b>`; card.addEventListener("click", () => openCharacterProfile(character.id)); return card; })); }
function renderInterventionOptions(characters) { const value = elements.interventionCharacter.value; const available = characters.filter((character) => !character.is_player); elements.interventionCharacter.replaceChildren(...available.map((character) => new Option(character.name, character.id))); if (available.some((item) => item.id === value)) elements.interventionCharacter.value = value; }
function renderAdjudications(runs) { elements.adjudicationList.replaceChildren(...runs.map((run) => { const item = document.createElement("li"); item.innerHTML = `<span><b>${escapeHtml(run.trigger_type === "player_intervention" ? "局部介入" : run.trigger_type === "manual" ? "手动裁判" : "自主裁判")}</b><time>${escapeHtml(formatRealTime(run.completed_at))}</time></span><strong>${escapeHtml(run.provider)}${run.fallback_used ? " · 已降级" : ""}</strong><small>${run.selected_character_ids.length} 名人物 · ${run.final_event_ids.length} 条事件</small>`; return item; })); if (!runs.length) elements.adjudicationList.append(emptyElement("li", "还没有裁判记录", "empty-list")); }

function switchRoom(room, updateHash = true) { const valid = $(`[data-room-panel="${CSS.escape(room)}"]`) ? room : "scene"; state.currentRoom = valid; document.body.dataset.room = valid; $$('[data-room-panel]').forEach((panel) => { panel.hidden = panel.dataset.roomPanel !== valid; panel.classList.toggle("active", panel.dataset.roomPanel === valid); }); $$('[data-room]').forEach((button) => button.classList.toggle("active", button.dataset.room === valid)); if (updateHash) history.replaceState(null, "", `#${valid}`); elements.sideNavigation.classList.remove("open"); if (valid === "map") renderMap(); if (valid === "relationships") { state.relationshipKey = ""; renderRelationshipGraph(state.snapshot); } }
async function perform(message, action, successMessage, afterSuccess = null) { if (state.busy || !state.worldId) return; setBusy(true, message); try { const result = await action(); await refreshAll({ reloadWorlds: true, silent: true }); if (afterSuccess) afterSuccess(result); showToast(successMessage); } catch (error) { showToast(error.message, true); } finally { setBusy(false); } }
function renderActionResult(outcome) { if (!outcome?.summary) return; elements.actionResult.textContent = humanizeEventSummary(outcome.summary); elements.actionResult.hidden = false; elements.actionResult.classList.remove("flash"); void elements.actionResult.offsetWidth; elements.actionResult.classList.add("flash"); }
function emptyElement(tag, text, className) { const node = document.createElement(tag); node.textContent = text; if (className) node.className = className; return node; }
function escapeHtml(value) { return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[character]); }
function saveIntentDraft() { if (state.worldId) localStorage.setItem(`virtual-world-intent:${state.worldId}`, elements.playerIntent.value); }

$$('[data-room]').forEach((button) => button.addEventListener("click", () => switchRoom(button.dataset.room)));
$$('[data-room-jump]').forEach((button) => button.addEventListener("click", () => switchRoom(button.dataset.roomJump)));
elements.mobileNavToggle.addEventListener("click", () => elements.sideNavigation.classList.toggle("open"));
elements.worldSelect.addEventListener("change", async (event) => { state.worldId = event.target.value; resetMapView(); state.relationshipKey = ""; state.mapImage = null; await refreshAll(); loadMapFiles(); elements.playerIntent.value = localStorage.getItem(`virtual-world-intent:${state.worldId}`) || ""; });
elements.refreshButton.addEventListener("click", () => refreshAll({ reloadWorlds: true }));
elements.playerCreateLocation.addEventListener("change", updatePlayerCreationMapSelection);
elements.playerCreateMapMarkers.addEventListener("click", (event) => { const marker = event.target.closest("[data-location-id]"); if (marker) { elements.playerCreateLocation.value = marker.dataset.locationId; updatePlayerCreationMapSelection(); } });
elements.playerCreateSecondaryLocations.addEventListener("click", (event) => { const button = event.target.closest("[data-location-id]"); if (button) { elements.playerCreateLocation.value = button.dataset.locationId; updatePlayerCreationMapSelection(); } });
elements.playerCreateForm.addEventListener("submit", (event) => { event.preventDefault(); const name = elements.playerCreateName.value.trim(); const locationId = elements.playerCreateLocation.value; if (!name || !locationId) return showToast("请填写姓名并选择起始地点", true); const traits = elements.playerCreateTraits.value.split(/[，,]/).map((item) => item.trim()).filter(Boolean).slice(0, 5); perform("正在把你的角色写入世界", () => api(`/api/worlds/${state.worldId}/player`, { method: "POST", body: JSON.stringify({ name, identity: elements.playerCreateIdentity.value.trim() || "旅人", location_id: locationId, traits, goal: elements.playerCreateGoal.value.trim() }) }), `${name}已进入这个世界`); });
elements.setupMapShell.addEventListener("wheel", (event) => { event.preventDefault(); state.setupMapScale = Math.max(1, Math.min(12, state.setupMapScale * Math.exp(-event.deltaY * 0.0015))); elements.setupMapCamera.style.transform = `scale(${state.setupMapScale})`; }, { passive: false });
elements.playerIntentForm.addEventListener("submit", (event) => { event.preventDefault(); const intent = elements.playerIntent.value.trim(); if (!intent) return showToast("先写下你此刻想做的事吧", true); perform("世界正在回应你的行动", () => api(`/api/worlds/${state.worldId}/player/act`, { method: "POST", body: JSON.stringify({ intent }) }), "你的行动已写入世界", (result) => { renderActionResult(result.outcome); elements.playerIntent.value = ""; saveIntentDraft(); }); });
elements.playerIntent.addEventListener("input", saveIntentDraft); elements.playerIntent.addEventListener("keydown", (event) => { if (event.ctrlKey && event.key === "Enter") { event.preventDefault(); elements.playerIntentForm.requestSubmit(); } });
elements.mapShell.addEventListener("wheel", (event) => { event.preventDefault(); zoomMap(Math.exp(-event.deltaY * 0.0015), event.clientX, event.clientY); }, { passive: false });
elements.mapShell.addEventListener("pointerdown", (event) => { if (event.button !== 0) return; const view = state.mapView; view.dragging = true; view.moved = false; view.startX = event.clientX; view.startY = event.clientY; view.originX = view.x; view.originY = view.y; elements.mapShell.setPointerCapture(event.pointerId); });
let terrainHoverTimer = null;
async function refreshTerrainHover(longitude, latitude) {
  if (!state.worldId) return;
  try {
    const terrain = await api(`/api/worlds/${state.worldId}/terrain?longitude=${longitude}&latitude=${latitude}`);
    elements.mapHoverCoordinate.textContent = `${formatCoordinate(longitude, latitude, 2)} · ${terrain.surface_type} · 海拔 ${terrain.elevation_m}m · ${terrain.slope_degrees}°${terrain.water_kind ? ` · ${terrain.water_kind}` : ""}${terrain.crossing_name ? ` · ${terrain.crossing_name}` : ""}${terrain.road_type ? ` · ${terrain.road_type}` : ""}`;
  } catch (_error) {
    elements.mapHoverCoordinate.textContent = formatCoordinate(longitude, latitude, 2);
  }
}
elements.mapShell.addEventListener("pointermove", (event) => { const view = state.mapView; const coordinate = coordinateFromMapPointer(event); elements.mapHoverCoordinate.textContent = formatCoordinate(coordinate.longitude, coordinate.latitude, 2); if (terrainHoverTimer) clearTimeout(terrainHoverTimer); terrainHoverTimer = setTimeout(() => refreshTerrainHover(coordinate.longitude, coordinate.latitude), 120); if (!view.dragging) return; const deltaX = event.clientX - view.startX; const deltaY = event.clientY - view.startY; if (Math.hypot(deltaX, deltaY) > 5) view.moved = true; view.x = view.originX + deltaX; view.y = view.originY + deltaY; applyMapTransform(); });
elements.mapShell.addEventListener("pointerup", (event) => { const view = state.mapView; if (!view.dragging) return; view.dragging = false; elements.mapShell.releasePointerCapture(event.pointerId); if (!view.moved) { const coordinate = coordinateFromMapPointer(event); startMapMovement(coordinate.longitude, coordinate.latitude); } else { state.manualView = true; const rect = elements.mapShell.getBoundingClientRect(); const detail = resolveZoomDetailMap(view.scale, null, { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }); if (detail) enterDetailMap(detail, { scale: view.scale, x: view.x, y: view.y }); else renderMap(); } });
elements.mapShell.addEventListener("pointercancel", () => { state.mapView.dragging = false; });
elements.mapZoomIn.addEventListener("click", () => zoomMap(1.5)); elements.mapZoomOut.addEventListener("click", () => zoomMap(1 / 1.5)); elements.mapReset.addEventListener("click", resetMapView);
if (elements.mapStyleSelect) elements.mapStyleSelect.addEventListener("change", (event) => applyMapImage(event.target.value));
elements.movementCancel.addEventListener("click", () => perform("正在停止移动", () => api(`/api/worlds/${state.worldId}/player/move/cancel`, { method: "POST" }), "已经取消移动，当前位置已保留"));
elements.playerTransport.addEventListener("change", () => perform("正在切换交通工具", () => api(`/api/worlds/${state.worldId}/player/transport`, { method: "PATCH", body: JSON.stringify({ vehicle_id: elements.playerTransport.value || null }) }), "交通方式已更新"));
elements.eventFilters.addEventListener("click", (event) => { const button = event.target.closest("[data-event-filter]"); if (!button) return; state.eventFilter = button.dataset.eventFilter; elements.eventFilters.querySelectorAll("button").forEach((item) => item.classList.toggle("active", item === button)); renderEvents(); });
if (elements.constructionList) elements.constructionList.addEventListener("click", (event) => { const start = event.target.closest("[data-construction-start]"); const cancel = event.target.closest("[data-construction-cancel]"); if (start) perform("正在校验建设资源", () => api(`/api/worlds/${state.worldId}/registrations/${start.dataset.constructionStart}/construction`, { method: "PATCH", body: JSON.stringify({ status: "constructing" }) }), "建设项目已经开工"); if (cancel) perform("正在结算建设退款", () => api(`/api/worlds/${state.worldId}/registrations/${cancel.dataset.constructionCancel}/construction`, { method: "PATCH", body: JSON.stringify({ status: "cancelled" }) }), "建设项目已取消，退款已结算"); });
if (elements.registrationList) elements.registrationList.addEventListener("click", (event) => { const confirm = event.target.closest("[data-registration-confirm]"); const reject = event.target.closest("[data-registration-reject]"); if (confirm) perform("正在确认世界元素", () => api(`/api/worlds/${state.worldId}/registrations/${confirm.dataset.registrationConfirm}/confirm`, { method: "POST" }), "候选已写入世界"); if (reject) perform("正在拒绝候选", () => api(`/api/worlds/${state.worldId}/registrations/${reject.dataset.registrationReject}/reject`, { method: "POST", body: JSON.stringify({ reason: "玩家拒绝该候选" }) }), "候选已拒绝"); });
elements.characterDialogClose.addEventListener("click", () => elements.characterDialog.close()); elements.characterDialog.addEventListener("click", (event) => { if (event.target === elements.characterDialog) elements.characterDialog.close(); }); elements.playerProfileButton.addEventListener("click", () => { const player = currentPlayer(); if (player) openCharacterProfile(player.id); });
$$('[data-desk]').forEach((button) => button.addEventListener("click", () => { const panel = $(`#desk-${button.dataset.desk}`); $$('[id^="desk-"]').filter((item) => item.classList.contains("desk-panel")).forEach((item) => { item.hidden = item !== panel || !panel.hidden; }); })); $$('[data-desk-close]').forEach((button) => button.addEventListener("click", () => { button.closest(".desk-panel").hidden = true; }));
elements.notebookNew.addEventListener("click", resetNotebookForm); elements.notebookSearch.addEventListener("input", renderNotebookList);
elements.notebookForm.addEventListener("submit", (event) => { event.preventDefault(); const now = new Date().toISOString(); const id = elements.notebookId.value || (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`); const existing = state.notebook.find((item) => item.id === id); const record = { id, world_id: state.worldId, created_at: existing?.created_at || now, updated_at: now, title: elements.notebookTitle.value.trim(), body: elements.notebookBody.value.trim(), linked_character_id: elements.notebookCharacter.value || null, linked_location_id: elements.notebookLocation.value || null, linked_event_id: elements.notebookEvent.value || null }; state.notebook = [record, ...state.notebook.filter((item) => item.id !== id)]; saveNotebookCollection(); renderNotebookList(); editNotebook(id); showToast("笔记已经保存"); });
elements.notebookDelete.addEventListener("click", () => { const id = elements.notebookId.value; if (!id) return; state.notebook = state.notebook.filter((item) => item.id !== id); saveNotebookCollection(); renderNotebookList(); resetNotebookForm(); showToast("笔记已经删除"); });
elements.speedPresets.addEventListener("click", (event) => { const button = event.target.closest("[data-speed]"); if (!button) return; const speed = Number(button.dataset.speed); perform("正在调整世界流速", () => api(`/api/worlds/${state.worldId}/clock`, { method: "PATCH", body: JSON.stringify({ time_scale: speed, operator: "traveler_book_ui" }) }), speed === 0 ? "世界已暂停" : `时间比例已调整为 ${formatSpeed(speed)}`); });
elements.customSpeedForm.addEventListener("submit", (event) => { event.preventDefault(); const speed = Number(elements.customSpeed.value); if (!Number.isFinite(speed) || speed < 0 || speed > 10080) return showToast("请输入 0 到 10080 之间的倍率", true); perform("正在调整世界流速", () => api(`/api/worlds/${state.worldId}/clock`, { method: "PATCH", body: JSON.stringify({ time_scale: speed, operator: "traveler_book_ui" }) }), `时间比例已调整为 ${formatSpeed(speed)}`); });
elements.heartbeatButton.addEventListener("click", () => perform("正在执行状态心跳", () => api(`/api/worlds/${state.worldId}/heartbeat`, { method: "POST" }), "状态心跳已完成"));
elements.adjudicateButton.addEventListener("click", () => perform("模型正在进行世界裁判", () => api(`/api/worlds/${state.worldId}/adjudicate`, { method: "POST", body: JSON.stringify({ trigger: "manual", character_ids: null }) }), "模型裁判已完成"));
elements.interventionButton.addEventListener("click", () => { const id = elements.interventionCharacter.value; if (!id) return showToast("请先选择人物", true); perform("正在处理局部介入", () => api(`/api/worlds/${state.worldId}/adjudicate`, { method: "POST", body: JSON.stringify({ trigger: "player_intervention", character_ids: [id] }) }), "局部裁判已完成"); });
elements.syncHistoryButton.addEventListener("click", () => perform("正在同步世界卷宗", () => api(`/api/worlds/${state.worldId}/history/sync`, { method: "POST" }), "世界卷宗已同步"));
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && !state.busy) refreshAll({ silent: true }); });
window.addEventListener("hashchange", () => switchRoom(window.location.hash.slice(1) || "scene", false));
window.setInterval(() => { if (document.visibilityState === "visible" && !state.busy) refreshAll({ silent: true }); }, 5000);
window.requestAnimationFrame(runLiveFrame);
refreshAll({ reloadWorlds: true }).then(() => { if (state.worldId) { elements.playerIntent.value = localStorage.getItem(`virtual-world-intent:${state.worldId}`) || ""; loadMapFiles(); } });
