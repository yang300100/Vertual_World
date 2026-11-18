/* 哥哥，燃料只由本人装卸真实物品；当前还不能用它驱动车辆。 */
let fuelContextKey = "", fuelContextEpoch = 0, fuelRead = null, fuelReadAt = 0;
const fuelDrafts = new Map(), fuelPending = new Map(), fuelInFlight = new Set(), fuelNotices = new Map();

function fuelContext() {
  const worldId = state.worldId, playerId = currentPlayer()?.id;
  const key = worldId && playerId ? worldId + ":" + playerId : "";
  if (key !== fuelContextKey) {
    if (fuelRead) fuelRead.invalidated = true;
    fuelContextKey = key; fuelContextEpoch += 1; fuelReadAt = 0;
  }
  return {worldId, playerId, key, epoch: fuelContextEpoch};
}
function fuelCurrent(context) {
  const now = fuelContext();
  return Boolean(context?.key) && now.key === context.key && now.epoch === context.epoch;
}
function fuelStorageKey(context) {
  return "virtual-world:fuel-pending:" + encodeURIComponent(context.worldId) + ":" + encodeURIComponent(context.playerId);
}
function fuelValidPending(value) {
  const p = value?.payload;
  return p && ["load", "unload"].includes(p.operation) && typeof p.vehicle_id === "string" && p.vehicle_id &&
    typeof p.item_id === "string" && p.item_id && Number.isSafeInteger(p.quantity) && p.quantity > 0 &&
    typeof p.idempotency_key === "string" && /^fuel:[a-f0-9-]{36}$/i.test(p.idempotency_key);
}
function fuelGetPending(context) {
  if (fuelPending.has(context.key)) return fuelPending.get(context.key);
  try {
    const raw = sessionStorage.getItem(fuelStorageKey(context));
    if (!raw) return null;
    const value = JSON.parse(raw);
    if (fuelValidPending(value)) { value.uncertain = true; fuelPending.set(context.key, value); return value; }
  } catch (_) { /* 原请求不可读时不能覆盖它。 */ }
  const blocked = {invalid:true}; fuelPending.set(context.key, blocked); return blocked;
}
function fuelPersistPending(context, value) {
  sessionStorage.setItem(fuelStorageKey(context), JSON.stringify(value));
  fuelPending.set(context.key, value);
}
function fuelClearPending(context, pending) {
  if (fuelPending.get(context.key) !== pending) return;
  sessionStorage.removeItem(fuelStorageKey(context));
  fuelPending.delete(context.key);
}
function fuelText(parent, value, tag="p") {
  const node = document.createElement(tag); node.textContent = value; parent.append(node); return node;
}
function fuelField(parent, label, tag="input") {
  const box = document.createElement("label"), field = document.createElement(tag);
  box.textContent = label; field.setAttribute("aria-label", label); box.append(field); parent.append(box); return field;
}
function fuelOption(select, value, label, disabled=false) {
  const option = document.createElement("option"); option.value = value; option.textContent = label; option.disabled = disabled; select.append(option);
}
function fuelDraftEqual(draft, payload) {
  return draft && draft.vehicle_id === payload.vehicle_id && draft.operation === payload.operation &&
    draft.item_id === payload.item_id && String(draft.quantity) === String(payload.quantity);
}

async function fuelSend(context, pending, notice, update) {
  if (!fuelCurrent(context) || fuelInFlight.has(context.key) || state.busy || state.responsePending) return;
  const wasUncertain = pending.uncertain;
  try {
    // 哥哥，先持久化请求，再发送；刷新页面也必须沿用同一幂等键。
    fuelPersistPending(context, pending);
  } catch (_) { notice.textContent = "无法保存本次请求的重试信息，暂不发送；请检查浏览器会话存储。"; return; }
  fuelInFlight.add(context.key); update();
  try {
    const result = await api("/api/worlds/" + encodeURIComponent(context.worldId) + "/vehicle-fuel/transfer", {
      method: "POST", body: JSON.stringify(pending.payload),
    });
    if (!result?.event_id || !["applied", "already"].includes(result.status)) throw new Error("未取得可核验的装卸回执");
    try { fuelClearPending(context, pending); }
    catch (_) { pending.uncertain = true; fuelPending.set(context.key, pending); if (fuelCurrent(context)) notice.textContent = "已收到回执，但无法清除重试记录；再次核对将使用原请求。"; return; }
    if (fuelDraftEqual(fuelDrafts.get(context.key), pending.payload)) fuelDrafts.delete(context.key);
    fuelNotices.set(context.key, result.summary || "本人燃料装卸已记录。");
    // 哥哥，新表单渲染前释放锁，避免新按钮保留旧的禁用状态。
    fuelInFlight.delete(context.key);
    if (fuelCurrent(context)) {
      const summary = document.querySelector("#vehicle-fuel")?.querySelector(".vehicle-fuel-result");
      if (summary) summary.textContent = fuelNotices.get(context.key);
      await renderVehicleFuel(true);
    }
  } catch (error) {
    if (!wasUncertain && [400, 404, 409, 422].includes(error?.status)) {
      try { fuelClearPending(context, pending); } catch (_) { pending.uncertain = true; }
      if (fuelCurrent(context)) notice.textContent = "本次未执行：" + (error.message || "请重新核对实际状态。") + " 草稿已保留。";
    } else {
      pending.uncertain = true;
      try { fuelPersistPending(context, pending); } catch (_) { /* 已在发送前保存原请求。 */ }
      if (fuelCurrent(context)) notice.textContent = "尚未确认结果；请用原请求核对，不会重新生成请求编号。";
    }
  } finally {
    fuelInFlight.delete(context.key);
    if (fuelCurrent(context)) update();
  }
}

function fuelRenderForm(panel, vehicles, context) {
  const form = document.createElement("form"); form.className = "long-term-form vehicle-fuel-form";
  const saved = fuelDrafts.get(context.key) || {};
  const vehicle = fuelField(form, "选择载具", "select"), operation = fuelField(form, "装入或取回", "select");
  const item = fuelField(form, "本人燃料批次", "select"), quantity = fuelField(form, "数量");
  quantity.type = "number"; quantity.min = "1"; quantity.step = "1"; quantity.required = true;
  fuelOption(vehicle, "", "请选择载具");
  for (const row of vehicles) fuelOption(vehicle, row.vehicle_id, `${row.name}（${row.loaded_units}/${row.capacity_units}份）`);
  if (saved.vehicle_id && !vehicles.some(row => row.vehicle_id === saved.vehicle_id)) fuelOption(vehicle, saved.vehicle_id, "原选载具当前不可用", true);
  vehicle.value = saved.vehicle_id || "";
  fuelOption(operation, "load", "装入本人燃料"); fuelOption(operation, "unload", "取回本人燃料");
  operation.value = saved.operation || "load"; quantity.value = saved.quantity || "1";
  const note = fuelText(form, "请选择本人持有的实际燃料批次。");
  const submit = document.createElement("button"); submit.type = "submit"; submit.className = "button primary"; submit.textContent = "确认本人装卸"; form.append(submit);
  const retry = document.createElement("button"); retry.type = "button"; retry.className = "button secondary"; retry.textContent = "用原请求核对结果"; form.append(retry);
  const notice = fuelText(form, ""); notice.setAttribute("aria-live", "polite");
  const current = () => panel.isConnected && fuelCurrent(context);
  function save() {
    if (fuelCurrent(context)) fuelDrafts.set(context.key, {vehicle_id:vehicle.value, operation:operation.value, item_id:item.value, quantity:quantity.value});
  }
  form.fuelSaveDraft = save;
  function syncItems(preferred) {
    const car = vehicles.find(row => row.vehicle_id === vehicle.value);
    const choices = car && Array.isArray(car[operation.value === "load" ? "loadable" : "unloadable"]) ? car[operation.value === "load" ? "loadable" : "unloadable"] : [];
    item.replaceChildren(); fuelOption(item, "", "请选择本人燃料批次");
    for (const row of choices) fuelOption(item, row.id, `${row.name}（最多${row.max_quantity}份）`);
    if (preferred && !choices.some(row => row.id === preferred)) fuelOption(item, preferred, "原选批次当前不可用", true);
    item.value = preferred || "";
    const selected = choices.find(row => row.id === item.value);
    quantity.max = selected ? String(selected.max_quantity) : "";
    note.textContent = selected ? `本次最多${selected.max_quantity}份，具体结果由世界重新核验。` : "当前没有所选载具与操作可用的本人批次。";
    return selected;
  }
  function update() {
    if (!current()) return;
    const pending = fuelGetPending(context), locked = Boolean(pending || fuelInFlight.has(context.key));
    for (const field of [vehicle, operation, item, quantity]) field.disabled = locked;
    submit.disabled = locked || !vehicles.length;
    retry.hidden = !pending; retry.disabled = !pending || pending.invalid || fuelInFlight.has(context.key);
    if (pending) notice.textContent = pending.invalid ? "原请求记录不可读取，请先恢复该会话记录；不能安全地重新提交。" : fuelInFlight.has(context.key) ? "正在等待本人装卸回执……" : "上次结果尚未确认，只能用原请求核对，避免重复装卸。";
  }
  vehicle.addEventListener("change", () => { syncItems(""); save(); });
  operation.addEventListener("change", () => { syncItems(""); save(); });
  item.addEventListener("change", () => { syncItems(item.value); save(); });
  quantity.addEventListener("input", save);
  syncItems(saved.item_id); update();
  form.addEventListener("submit", async event => {
    event.preventDefault();
    if (!current() || fuelGetPending(context) || fuelInFlight.has(context.key) || state.busy || state.responsePending || !form.reportValidity()) return;
    const selected = syncItems(item.value), count = Number(quantity.value);
    if (!selected || !Number.isSafeInteger(count) || count < 1 || count > selected.max_quantity || count > 1000000000) {
      notice.textContent = "请从本人当前可用批次中选择有效整数数量。"; save(); return;
    }
    save();
    let id;
    try { id = "fuel:" + crypto.randomUUID(); } catch (_) { notice.textContent = "无法生成安全的请求编号，暂不发送。"; return; }
    await fuelSend(context, {payload:{operation:operation.value, vehicle_id:vehicle.value, item_id:item.value, quantity:count, idempotency_key:id}, uncertain:false}, notice, update);
  });
  retry.addEventListener("click", async () => {
    const pending = fuelGetPending(context); if (pending && !pending.invalid && current()) await fuelSend(context, pending, notice, update);
  });
  panel.append(form);
}

async function renderVehicleFuel(force=false) {
  const context = fuelContext();
  if (!context.key || !state.living) { if (fuelRead) fuelRead.invalidated = true; document.querySelector("#vehicle-fuel")?.remove(); fuelReadAt = 0; return; }
  let panel = document.querySelector("#vehicle-fuel");
  if (!panel) {
    const anchor = document.querySelector("#my-rides") || document.querySelector("#my-group-journeys") || elements.sceneCharacters;
    if (!anchor) return;
    panel = document.createElement("section"); panel.id = "vehicle-fuel"; panel.setAttribute("aria-labelledby", "vehicle-fuel-heading"); anchor.after(panel);
  }
  const token = context.key + ":" + context.epoch;
  if (panel.dataset.fuelContext !== token) {
    panel.querySelector(".vehicle-fuel-form")?.fuelSaveDraft?.(); panel.dataset.fuelContext = token; panel.replaceChildren();
    fuelText(panel, "载具燃料装卸", "h3").id = "vehicle-fuel-heading";
    fuelText(panel, "只可装入或取回本人真实持有的燃料；当前尚不能消耗燃料行驶。");
    const result = fuelText(panel, fuelNotices.get(context.key) || ""); result.className = "vehicle-fuel-result"; result.setAttribute("aria-live", "polite");
    const refresh = document.createElement("button"); refresh.type = "button"; refresh.className = "button secondary"; refresh.textContent = "刷新本人可操作载具";
    refresh.addEventListener("click", () => renderVehicleFuel(true)); panel.append(refresh);
    const content = document.createElement("div"); content.className = "vehicle-fuel-content"; fuelText(content, "正在核对本人可操作的载具……"); panel.append(content);
  }
  if (!force && (state.busy || state.responsePending || fuelInFlight.has(context.key) || fuelRead && !fuelRead.invalidated || Date.now() - fuelReadAt < 4000)) return;
  if (fuelRead) fuelRead.invalidated = true;
  const request = {context, invalidated:false}; fuelRead = request;
  const current = () => fuelRead === request && !request.invalidated && panel.isConnected && panel.dataset.fuelContext === token && fuelCurrent(context);
  try {
    const result = await api("/api/worlds/" + encodeURIComponent(context.worldId) + "/vehicle-fuel/options");
    if (!current()) return;
    const vehicles = Array.isArray(result?.vehicles) ? result.vehicles : [];
    fuelReadAt = Date.now(); const content = panel.querySelector(".vehicle-fuel-content"), signature = JSON.stringify(vehicles);
    if (content.dataset.signature !== signature || !content.querySelector(".vehicle-fuel-form")) {
      content.querySelector(".vehicle-fuel-form")?.fuelSaveDraft?.(); content.dataset.signature = signature; content.replaceChildren();
      if (!vehicles.length) fuelText(content, "附近暂无本人可以装卸燃料的已核实载具。");
      fuelRenderForm(content, vehicles, context);
    }
    panel.querySelector(".vehicle-fuel-error")?.remove();
  } catch (_) {
    if (current()) {
      const content = panel.querySelector(".vehicle-fuel-content");
      if (!content.querySelector(".vehicle-fuel-form")) { content.replaceChildren(); fuelRenderForm(content, [], context); }
      if (!panel.querySelector(".vehicle-fuel-error")) fuelText(panel, "暂时无法刷新载具，草稿和未确认请求仍保留。", "p").className = "vehicle-fuel-error muted";
    }
  } finally { if (fuelRead === request) fuelRead = null; }
}

const fuelOriginalLiving = renderLiving;
renderLiving = function() { fuelOriginalLiving(); renderVehicleFuel(); };
