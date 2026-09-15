/* 规则决定耗时与产物，界面不提前宣告完成。 */
let taskCatalog = [], taskCatalogWorld = null, taskCatalogLoading = false;
let taskWaitRequest = null;
let taskRiskKey = null;
let taskStartRequest = null;

function fillTaskSelect(selector, options) {
  const select = $(selector), value = select.value;
  select.replaceChildren(...options.map(([name, id]) => new Option(name, id)));
  if (options.some((item) => item[1] === value)) select.value = value;
}

async function loadTaskCatalog() {
  if (taskCatalogLoading || !state.worldId || taskCatalogWorld === state.worldId) return;
  taskCatalogLoading = true;
  const worldId = state.worldId;
  try {
    const data = await api(`/api/worlds/${worldId}/activity-recipes/catalog`);
    if (worldId !== state.worldId) return;
    taskCatalog = data.item_types; taskCatalogWorld = worldId;
    fillTaskSelect("#recipe-location", state.snapshot.locations.map((item) => [item.name, item.id]));
    for (const selector of ["#recipe-input", "#recipe-output"]) fillTaskSelect(selector, taskCatalog.map((item) => [item.name, item.id]));
    fillTaskSelect("#recipe-check-tool", [["无需专用工具", ""], ...taskCatalog.map((item) => [item.name, item.id])]);
    renderTasks();
  } catch (error) { showToast(error.message, true); }
  finally { taskCatalogLoading = false; }
}

function recipeProposalDetails(spec) {
  if (spec.kind === "map_review") return "核对本人留下的现场记录，耗时30分钟、消耗6点精力。仅评价记录范围内的整理质量，未知路段仍未知。";
  const itemName = (id) => taskCatalog.find((item) => item.id === id)?.name || id;
  const location = state.snapshot.locations.find((item) => item.id === spec.location_id)?.name || spec.location_id;
  const tools = [...(spec.tools || [])];
  if (spec.kind === "repair" && spec.check_tool_type_id && !tools.some((item) => item.item_type_id === spec.check_tool_type_id)) tools.push({item_type_id:spec.check_tool_type_id,wear:1});
  const toolText = tools.length ? `<p>需自备工具：${tools.map((item) => `${escapeHtml(itemName(item.item_type_id))} × 1（本次磨损 ${item.wear}）`).join("、")}。开工前完好度须足够，期间占用；中止按已用时间比例向上取整磨损，结束后在原地领取。</p>` : "";
  return `<p>地点：${escapeHtml(location)}<br>耗时：${spec.duration_minutes}分钟 · 精力消耗：${spec.energy_cost}<br>原料：${spec.ingredients.map((item) => `${escapeHtml(itemName(item.item_type_id))} × ${item.quantity}`).join("、")}<br>${spec.kind === "craft" ? `产物：${escapeHtml(itemName(spec.output_item_type_id))} × ${spec.output_quantity}` : `修理类别：${escapeHtml(spec.repair_category)}，成功时完好度 +${spec.repair_points}；${escapeHtml(spec.check_skill || "修理")}检定，难度${spec.check_difficulty ?? 40}，最低熟练度${spec.check_min_proficiency ?? 0}，工具：${escapeHtml(spec.check_tool_type_id ? itemName(spec.check_tool_type_id) : "无需专用工具")}`}</p>${toolText}`;
}

function renderTasks() {
  if (!lifeScene) return;
  const recipes = lifeScene.recipes || [], active = lifeScene.activity;
  fillTaskSelect("#task-recipe", [["一小时工作（工作场所）", ""], ...recipes.map((item) => [item.name, item.id]), ...(lifeScene.map_records || []).map((item) => [`核对地图：${item.title} · ${formatRealTime(item.created_at)}`, `map:${item.id}`])]);
  const selectedMap = $("#task-recipe").value.startsWith("map:");
  const recipe = active ? (active.kind === "work" ? null : active.task_plan)
    : selectedMap ? {kind:"map_review"} : recipes.find((item) => item.id === $("#task-recipe").value);
  $("#task-target-label").hidden = recipe?.kind !== "repair";
  fillTaskSelect("#task-target", (lifeScene.items || []).filter((item) => item.place === "bag" && item.ownership === "self" && item.category === recipe?.repair_category).map((item) => [item.name, item.id]));
  const payment = active?.task_plan?.payment ?? lifeScene.work_payment;
  $("#task-preview").innerHTML = recipe ? recipeProposalDetails(recipe) : payment == null ? "这里尚未登记可用工作，请先抵达工作场所。" : `一小时工作消耗16点精力和8点饱食，完成后获得${Number(payment)}枚货币。开始时不发放报酬，中止不计完成。`;
  $("#task-preview").hidden = Boolean(active && !active.task_plan);
  $("#task-start-form").hidden = Boolean(active);
  $("#life-advance").hidden = !active;
  const outputs = (lifeScene.pending_outputs || []).map((item) => {
    const row = document.createElement("div"); row.className = "life-item";
    const label = document.createElement("span"); label.textContent = `${item.name} × ${item.quantity} · 完好度 ${item.condition}/100 · 在原活动地点领取${item.freshness?.spoils_world_time ? ` · ${item.freshness.label} · ${formatWorldTime(item.freshness.spoils_world_time)}到期` : ""}`;
    const button = document.createElement("button"); button.type = "button"; button.className = "button secondary"; button.textContent = `领取${item.name}（1份）`;
    button.disabled = state.busy || Boolean(active);
    button.addEventListener("click", () => lifeMutation(`outputs/${encodeURIComponent(item.id)}/claim`, {quantity:1}));
    row.append(label, button); return row;
  });
  $("#task-outputs").replaceChildren(...outputs);
  renderCheckHistory();
  if (active) {
    taskRiskKey = null;
    const check = active.task_plan?.check_preview;
    $("#task-check-risk").textContent = check ? `本次已固定：${check.skill_name}，成功率${check.chance}%。结果在活动结束后公开；中止再试不会重掷。` : "";
  } else refreshCheckPreview();
}

$("#task-recipe").addEventListener("change", renderTasks);
function selectedTaskPayload() {
  const value = $("#task-recipe").value;
  if (value.startsWith("map:")) return {map_record_id:value.slice(4)};
  return {recipe_id:value || null, target_item_id:$("#task-target-label").hidden ? null : $("#task-target").value || null};
}

async function refreshCheckPreview() {
  const payload = selectedTaskPayload();
  const key = JSON.stringify([state.worldId, state.snapshot.world.version, payload]);
  if (taskRiskKey === key) return;
  taskRiskKey = key;
  try {
    const result = await api(`/api/worlds/${state.worldId}/player/life/tasks/preview`, {method:"POST",body:JSON.stringify(payload)});
    if (taskRiskKey !== key) return;
    const check = result.check;
    $("#task-check-risk").textContent = check ? `把握：${check.automatic ? "当前条件可稳定完成" : `成功率${check.chance}%`}。${check.skill_name} ${check.skill}，难度${check.difficulty}，条件修正${check.modifier}。可能部分成功或失败；耗时与已投入成本不会退回。` : "该活动不需要随机检定。";
  } catch (error) {
    if (taskRiskKey === key) $("#task-check-risk").textContent = error.message;
  }
}

function renderCheckHistory() {
  const nodes = (lifeScene.checks || []).map((check) => {
    const node = document.createElement("p");
    const outcome = check.status === "resolved" ? {success:"成功",partial:"部分成功",failure:"失败"}[check.outcome] : "尚未结算，点数保密";
    node.textContent = `${check.skill_name} · 成功率${check.chance}% · ${outcome}${check.status === "resolved" ? ` · ${check.automatic ? "稳定完成" : `点数${check.roll}`}` : ""}`;
    return node;
  });
  if (!nodes.length) nodes.push(emptyElement("p", "尚无检定尝试", "muted"));
  $("#task-check-history").replaceChildren(...nodes);
}

$("#task-target").addEventListener("change", refreshCheckPreview);
$("#task-start-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = selectedTaskPayload();
  const key = JSON.stringify([state.worldId, payload]);
  if (taskStartRequest?.key !== key) taskStartRequest = {key, request_id:crypto.randomUUID()};
  const result = await lifeMutation("tasks", {...payload, request_id:taskStartRequest.request_id});
  if (result) taskStartRequest = null;
});

$("#life-advance").addEventListener("click", async () => {
  if (state.busy || !lifeScene?.activity) return;
  const worldId = state.worldId, activityId = lifeScene.activity.id;
  if (!taskWaitRequest || taskWaitRequest.worldId !== worldId || taskWaitRequest.activityId !== activityId) taskWaitRequest = {
    worldId, activityId, body:{expected_version:state.snapshot.world.version, request_id:crypto.randomUUID()},
  };
  setBusy(true, "正在等候到下一处变化");
  try {
    const result = await api(`/api/worlds/${worldId}/player/life/activities/${activityId}/advance`, {method:"POST",body:JSON.stringify(taskWaitRequest.body)});
    taskWaitRequest = null;
    $("#action-result").textContent = result.summary; $("#action-result").hidden = false;
    await refreshAll();
  } catch (error) {
    if (error.status === 409) taskWaitRequest = null;
    showToast(error.message, true); await refreshAll();
  } finally { setBusy(false); renderLifeScene(); }
});

$("#recipe-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!$("#recipe-form").reportValidity()) return;
  const kind = $("#recipe-kind").value;
  let tools;
  try {
    tools = JSON.parse($("#recipe-tools").value || "[]");
    if (!Array.isArray(tools)) throw new Error("工具配置应为 JSON 数组");
  } catch (error) { showToast(error.message, true); return; }
  const recipe = {name:$("#recipe-name").value.trim(), kind, location_id:$("#recipe-location").value,
    duration_minutes:Number($("#recipe-duration").value), energy_cost:Number($("#recipe-energy").value),
    tools, ingredients:[{item_type_id:$("#recipe-input").value, quantity:Number($("#recipe-input-count").value)}],
    output_item_type_id:kind === "craft" ? $("#recipe-output").value : null,
    output_quantity:Number($("#recipe-output-count").value), repair_category:kind === "repair" ? $("#recipe-category").value.trim() : null,
    repair_points:Number($("#recipe-points").value),
    check_skill:$("#recipe-check-skill").value.trim() || "修理", check_difficulty:Number($("#recipe-check-difficulty").value),
    check_min_proficiency:Number($("#recipe-check-min").value), check_tool_type_id:$("#recipe-check-tool").value || null};
  perform("正在提交配方审核", () => api(`/api/worlds/${state.worldId}/activity-recipes`, {method:"POST",body:JSON.stringify({idempotency_key:crypto.randomUUID(),recipe})}), "配方已进入审核，请核对后确认");
});
Object.assign(eventLabels, { time_waited:"等候推进", activity_claimed:"领取活动成果" });
