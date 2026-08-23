"use strict";

const state = {
  worldId: null,
  worlds: [],
  snapshot: null,
  busy: false,
  toastTimer: null,
  serverOffsetMs: 0,
  liveClock: null,
};

const elements = {
  dashboard: document.querySelector("#dashboard"),
  emptyState: document.querySelector("#empty-state"),
  worldSelect: document.querySelector("#world-select"),
  connectionStatus: document.querySelector("#connection-status"),
  refreshButton: document.querySelector("#refresh-button"),
  worldTitle: document.querySelector("#world-title"),
  worldStatus: document.querySelector("#world-status"),
  worldTime: document.querySelector("#world-time"),
  timeDescription: document.querySelector("#time-description"),
  speedValue: document.querySelector("#speed-value"),
  speedPresets: document.querySelector("#speed-presets"),
  customSpeedForm: document.querySelector("#custom-speed-form"),
  customSpeed: document.querySelector("#custom-speed"),
  heartbeatButton: document.querySelector("#heartbeat-button"),
  adjudicateButton: document.querySelector("#adjudicate-button"),
  nextAdjudication: document.querySelector("#next-adjudication"),
  worldVersion: document.querySelector("#world-version"),
  tickCount: document.querySelector("#tick-count"),
  providerName: document.querySelector("#provider-name"),
  offlinePolicy: document.querySelector("#offline-policy"),
  workerStatus: document.querySelector("#worker-status"),
  lastHeartbeat: document.querySelector("#last-heartbeat"),
  interventionCharacter: document.querySelector("#intervention-character"),
  interventionButton: document.querySelector("#intervention-button"),
  characterCount: document.querySelector("#character-count"),
  characterGrid: document.querySelector("#character-grid"),
  eventList: document.querySelector("#event-list"),
  adjudicationList: document.querySelector("#adjudication-list"),
  syncHistoryButton: document.querySelector("#sync-history-button"),
  busyOverlay: document.querySelector("#busy-overlay"),
  busyMessage: document.querySelector("#busy-message"),
  toast: document.querySelector("#toast"),
};

const eventLabels = {
  "action.rest": "休息",
  "action.eat": "进食",
  "action.work": "工作",
  "action.travel": "旅行",
  "action.socialize": "交流",
  "action.idle": "观察",
  "action.rejected": "行动失败",
  "world.tick": "旧版轮次",
  "world.adjudication": "模型裁判",
  "world.clock_rate_changed": "时间比例调整",
};

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json")
    ? await response.json()
    : await response.text();
  if (!response.ok) {
    const detail = typeof payload === "object" ? payload.detail : payload;
    throw new Error(detail || `请求失败：HTTP ${response.status}`);
  }
  return payload;
}

function formatWorldTime(value, includeSeconds = true) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "UTC",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    ...(includeSeconds ? { second: "2-digit" } : {}),
    hour12: false,
  }).format(date);
}

function formatRealTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

function formatSpeed(value) {
  const number = Number(value);
  if (number === 0) return "已暂停";
  return `${Number.isInteger(number) ? number : number.toFixed(1)}×`;
}

function setConnection(status, text) {
  elements.connectionStatus.dataset.state = status;
  elements.connectionStatus.textContent = text;
}

function setBusy(busy, message = "正在处理") {
  state.busy = busy;
  elements.busyOverlay.hidden = !busy;
  elements.busyMessage.textContent = message;
  document.querySelectorAll("button").forEach((button) => {
    button.disabled = busy;
  });
}

function showToast(message, isError = false) {
  window.clearTimeout(state.toastTimer);
  elements.toast.textContent = message;
  elements.toast.classList.toggle("error", isError);
  elements.toast.hidden = false;
  state.toastTimer = window.setTimeout(() => {
    elements.toast.hidden = true;
  }, 3600);
}

async function loadWorlds() {
  const worlds = await api("/api/worlds");
  state.worlds = worlds;
  const previousId = state.worldId;
  elements.worldSelect.replaceChildren();
  worlds.forEach((world) => {
    const option = document.createElement("option");
    option.value = world.id;
    option.textContent = world.name;
    elements.worldSelect.append(option);
  });
  state.worldId = worlds.some((world) => world.id === previousId)
    ? previousId
    : worlds[0]?.id || null;
  elements.worldSelect.value = state.worldId || "";
  elements.emptyState.hidden = worlds.length !== 0;
  elements.dashboard.hidden = worlds.length === 0;
}

async function refreshAll({ reloadWorlds = false } = {}) {
  try {
    setConnection("loading", "正在同步");
    if (reloadWorlds || state.worlds.length === 0) {
      await loadWorlds();
    }
    if (!state.worldId) {
      setConnection("online", "服务正常");
      return;
    }
    const [snapshot, events, adjudications, health] = await Promise.all([
      api(`/api/worlds/${state.worldId}`),
      api(`/api/worlds/${state.worldId}/events?limit=30`),
      api(`/api/worlds/${state.worldId}/adjudications?limit=20`),
      api("/api/health"),
    ]);
    state.snapshot = snapshot;
    const serverTime = Date.parse(health.server_time);
    state.serverOffsetMs = Number.isNaN(serverTime) ? 0 : serverTime - Date.now();
    renderSnapshot(snapshot, health);
    renderEvents(events);
    renderAdjudications(adjudications);
    setConnection("online", "已连接");
  } catch (error) {
    setConnection("error", "连接失败");
    showToast(error.message, true);
  }
}

function renderSnapshot(snapshot, health) {
  const world = snapshot.world;
  const locationNames = new Map(
    snapshot.locations.map((location) => [location.id, location.name]),
  );
  elements.worldTitle.textContent = world.name;
  elements.worldStatus.textContent = world.status === "running" ? "运行中" : world.status;
  configureLiveClock(world);
  elements.speedValue.textContent = formatSpeed(world.time_scale);
  elements.nextAdjudication.textContent = formatWorldTime(
    world.next_adjudication_time,
    false,
  );
  elements.worldVersion.textContent = `v${world.version}`;
  elements.tickCount.textContent = String(world.tick_count);
  elements.providerName.textContent = health.decision_provider || "—";
  elements.offlinePolicy.textContent =
    world.offline_policy === "pause" ? "离线暂停" : world.offline_policy;
  elements.characterCount.textContent = `${snapshot.characters.length} 人`;
  elements.customSpeed.value = world.time_scale;

  elements.speedPresets.querySelectorAll("button[data-speed]").forEach((button) => {
    button.classList.toggle(
      "active",
      Math.abs(Number(button.dataset.speed) - Number(world.time_scale)) < 0.0001,
    );
  });

  renderCharacters(snapshot.characters, locationNames);
  renderInterventionOptions(snapshot.characters);
}

function configureLiveClock(world) {
  const serverNow = Date.now() + state.serverOffsetMs;
  const workerSeen = Date.parse(world.last_worker_seen_at || "");
  const lastHeartbeat = Date.parse(world.last_heartbeat_real_time || "");
  const heartbeatInterval = Number(world.heartbeat_interval_seconds || 60);
  const staleAfterMs = Math.max(heartbeatInterval * 2500, 150000);
  const workerAgeMs = serverNow - workerSeen;
  const workerOnline =
    !Number.isNaN(workerSeen) && workerAgeMs >= -10000 && workerAgeMs <= staleAfterMs;

  state.liveClock = {
    baseWorldMs: Date.parse(world.current_time),
    lastHeartbeatMs: Number.isNaN(lastHeartbeat) ? null : lastHeartbeat,
    timeScale: Number(world.time_scale),
    workerOnline,
    staleAfterMs,
  };

  elements.workerStatus.textContent = workerOnline ? "心跳正常" : "未自动运行";
  elements.workerStatus.classList.toggle("runtime-ok", workerOnline);
  elements.workerStatus.classList.toggle("runtime-offline", !workerOnline);
  elements.lastHeartbeat.textContent = Number.isNaN(lastHeartbeat)
    ? "尚未执行"
    : formatRealTime(world.last_heartbeat_real_time);
  updateLiveClock();
}

function updateLiveClock() {
  const clock = state.liveClock;
  if (!clock || Number.isNaN(clock.baseWorldMs)) {
    elements.worldTime.textContent = "—";
    return;
  }
  let displayedWorldMs = clock.baseWorldMs;
  if (clock.workerOnline && clock.lastHeartbeatMs !== null && clock.timeScale > 0) {
    const serverNow = Date.now() + state.serverOffsetMs;
    const elapsedMs = Math.max(0, serverNow - clock.lastHeartbeatMs);
    displayedWorldMs += Math.min(elapsedMs, clock.staleAfterMs) * clock.timeScale;
  }
  elements.worldTime.textContent = formatWorldTime(
    new Date(displayedWorldMs).toISOString(),
  );

  if (clock.timeScale === 0) {
    elements.timeDescription.textContent =
      "世界已暂停，状态心跳仍会记录但不会推进世界时间。";
  } else if (clock.workerOnline) {
    elements.timeDescription.textContent =
      `实时估算 · 现实 1 分钟 = 世界 ${clock.timeScale} 分钟`;
  } else {
    elements.timeDescription.textContent =
      "worker未运行或心跳延迟，当前显示已保存的世界时间。";
  }
}

function renderCharacters(characters, locationNames) {
  const fragment = document.createDocumentFragment();
  characters.forEach((character) => {
    const card = document.createElement("article");
    card.className = "character-card";

    const head = document.createElement("div");
    head.className = "character-head";
    const identity = document.createElement("div");
    const name = document.createElement("h3");
    name.textContent = character.name;
    const location = document.createElement("span");
    location.className = "location-label";
    location.textContent = locationNames.get(character.location_id) || "未知地点";
    identity.append(name, location);
    const money = document.createElement("span");
    money.className = "money";
    money.textContent = `${character.money} 枚`;
    head.append(identity, money);

    const meters = document.createElement("div");
    meters.className = "meter-list";
    meters.append(
      createMeter("精力", character.energy),
      createMeter("饱食", Math.max(0, 100 - character.hunger)),
    );

    const chips = document.createElement("div");
    chips.className = "chip-row";
    if (character.is_core) chips.append(createChip("核心人物"));
    character.traits.forEach((trait) => chips.append(createChip(trait)));

    const goals = document.createElement("p");
    goals.className = "goal-text";
    goals.textContent = character.goals.length
      ? `目标：${character.goals.join(" · ")}`
      : "暂时没有明确目标";

    card.append(head, meters, chips, goals);
    fragment.append(card);
  });
  elements.characterGrid.replaceChildren(fragment);
}

function createMeter(label, value) {
  const row = document.createElement("div");
  row.className = "meter-row";
  const name = document.createElement("span");
  name.textContent = label;
  const track = document.createElement("div");
  track.className = "meter-track";
  const fill = document.createElement("div");
  fill.className = "meter-fill";
  fill.style.setProperty("--meter-value", Math.max(0, Math.min(100, Number(value))));
  track.append(fill);
  const number = document.createElement("span");
  number.className = "meter-value";
  number.textContent = String(value);
  row.append(name, track, number);
  return row;
}

function createChip(text) {
  const chip = document.createElement("span");
  chip.className = "chip";
  chip.textContent = text;
  return chip;
}

function renderInterventionOptions(characters) {
  const selected = elements.interventionCharacter.value;
  const fragment = document.createDocumentFragment();
  characters.forEach((character) => {
    const option = document.createElement("option");
    option.value = character.id;
    option.textContent = character.name;
    fragment.append(option);
  });
  elements.interventionCharacter.replaceChildren(fragment);
  if (characters.some((character) => character.id === selected)) {
    elements.interventionCharacter.value = selected;
  }
  elements.interventionButton.disabled = state.busy || characters.length === 0;
}

function renderEvents(events) {
  if (events.length === 0) {
    elements.eventList.replaceChildren(createEmptyList("还没有世界事件"));
    return;
  }
  const fragment = document.createDocumentFragment();
  events.forEach((event) => {
    const item = document.createElement("li");
    item.className = "event-item";
    const meta = document.createElement("div");
    meta.className = "event-meta";
    const type = document.createElement("span");
    type.textContent = eventLabels[event.event_type] || event.event_type;
    const time = document.createElement("span");
    time.textContent = formatWorldTime(event.occurred_at, false);
    meta.append(type, time);
    const summary = document.createElement("p");
    summary.className = "event-summary";
    summary.textContent = humanizeEventSummary(event.summary);
    item.append(meta, summary);
    fragment.append(item);
  });
  elements.eventList.replaceChildren(fragment);
}

function humanizeEventSummary(summary) {
  let readable = String(summary);
  (state.snapshot?.characters || []).forEach((character) => {
    readable = readable.replace(character.id, character.name);
  });
  return readable;
}

function renderAdjudications(runs) {
  if (runs.length === 0) {
    elements.adjudicationList.replaceChildren(createEmptyList("还没有新架构裁判记录"));
    return;
  }
  const fragment = document.createDocumentFragment();
  runs.forEach((run) => {
    const item = document.createElement("li");
    item.className = "adjudication-item";
    const meta = document.createElement("div");
    meta.className = "adjudication-meta";
    const trigger = document.createElement("span");
    trigger.textContent = triggerLabel(run.trigger_type);
    const provider = document.createElement("span");
    provider.className = "provider-tag";
    provider.textContent = run.provider;
    const time = document.createElement("span");
    time.textContent = formatRealTime(run.completed_at);
    meta.append(trigger, provider, time);
    if (run.fallback_used) {
      const fallback = document.createElement("span");
      fallback.className = "fallback-tag";
      fallback.textContent = "已降级";
      meta.append(fallback);
    }
    const summary = document.createElement("p");
    summary.className = "adjudication-summary";
    summary.textContent = `${run.selected_character_ids.length} 名人物 · ${run.final_event_ids.length} 条最终事件`;
    item.append(meta, summary);
    fragment.append(item);
  });
  elements.adjudicationList.replaceChildren(fragment);
}

function triggerLabel(trigger) {
  return {
    scheduled_12h: "12小时自主裁判",
    player_intervention: "主视角介入",
    manual: "手动裁判",
  }[trigger] || trigger;
}

function createEmptyList(message) {
  const item = document.createElement("li");
  item.className = "empty-list";
  item.textContent = message;
  return item;
}

async function perform(message, action, successMessage) {
  if (state.busy || !state.worldId) return;
  setBusy(true, message);
  try {
    await action();
    await refreshAll({ reloadWorlds: true });
    showToast(successMessage);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    setBusy(false);
  }
}

elements.worldSelect.addEventListener("change", async (event) => {
  state.worldId = event.target.value;
  await refreshAll();
});

elements.refreshButton.addEventListener("click", () => {
  refreshAll({ reloadWorlds: true });
});

elements.speedPresets.addEventListener("click", (event) => {
  const button = event.target.closest("button[data-speed]");
  if (!button) return;
  const speed = Number(button.dataset.speed);
  perform(
    "正在调整世界速度",
    () =>
      api(`/api/worlds/${state.worldId}/clock`, {
        method: "PATCH",
        body: JSON.stringify({ time_scale: speed, operator: "main_view_ui" }),
      }),
    speed === 0 ? "世界已暂停" : `时间比例已调整为 ${formatSpeed(speed)}`,
  );
});

elements.customSpeedForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const speed = Number(elements.customSpeed.value);
  if (!Number.isFinite(speed) || speed < 0 || speed > 10080) {
    showToast("请输入0到10080之间的时间比例", true);
    return;
  }
  perform(
    "正在应用自定义速度",
    () =>
      api(`/api/worlds/${state.worldId}/clock`, {
        method: "PATCH",
        body: JSON.stringify({ time_scale: speed, operator: "main_view_ui" }),
      }),
    `时间比例已调整为 ${formatSpeed(speed)}`,
  );
});

elements.heartbeatButton.addEventListener("click", () => {
  perform(
    "正在执行状态心跳",
    () => api(`/api/worlds/${state.worldId}/heartbeat`, { method: "POST" }),
    "状态心跳已完成",
  );
});

elements.adjudicateButton.addEventListener("click", () => {
  perform(
    "模型正在进行世界裁判",
    () =>
      api(`/api/worlds/${state.worldId}/adjudicate`, {
        method: "POST",
        body: JSON.stringify({ trigger: "manual", character_ids: null }),
      }),
    "模型裁判已完成",
  );
});

elements.interventionButton.addEventListener("click", () => {
  const characterId = elements.interventionCharacter.value;
  if (!characterId) {
    showToast("请先选择人物", true);
    return;
  }
  const characterName = state.snapshot?.characters.find(
    (character) => character.id === characterId,
  )?.name;
  perform(
    "正在处理主视角介入",
    () =>
      api(`/api/worlds/${state.worldId}/adjudicate`, {
        method: "POST",
        body: JSON.stringify({
          trigger: "player_intervention",
          character_ids: [characterId],
        }),
      }),
    `${characterName || "人物"}的局部裁判已完成`,
  );
});

elements.syncHistoryButton.addEventListener("click", () => {
  perform(
    "正在同步世界历史日志",
    () => api(`/api/worlds/${state.worldId}/history/sync`, { method: "POST" }),
    "世界历史日志已同步",
  );
});

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && !state.busy) refreshAll();
});

window.setInterval(() => {
  if (document.visibilityState === "visible" && !state.busy) refreshAll();
}, 5000);

window.setInterval(updateLiveClock, 250);

refreshAll({ reloadWorlds: true });
