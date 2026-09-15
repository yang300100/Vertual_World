/* 生活面板只呈现服务端事实；完成与精力恢复均等待世界结算。 */
let lifeScene = null;
let lifeWorldId = null;
const lifeNames = { rest: "休息", wait: "等待", work: "工作", craft: "制作", repair: "修理", map_review:"地图核对" };
const lifeStatuses = { running: "进行中", completed: "已完成", cancelled: "已结束", interrupted: "被打断" };

async function refreshLife(worldId) {
  if (!currentPlayer()) { lifeScene = null; return; }
  try {
    const scene = await api(`/api/worlds/${worldId}/player/life`);
    if (worldId !== state.worldId) return;
    lifeScene = scene;
    lifeWorldId = worldId;
    renderLifeScene();
  } catch (error) {
    lifeScene = null;
    $("#life-state").textContent = "暂时无法读取";
    $("#life-description").textContent = error.message;
    $("#life-items").replaceChildren();
  }
}

function renderLifeScene() {
  if (!lifeScene || lifeWorldId !== state.worldId) return;
  if (typeof renderInterior === "function") renderInterior(lifeScene.interior);
  if (typeof renderTasks === "function") renderTasks();
  const active = lifeScene.activity;
  $("#life-state").textContent = active ? `${lifeNames[active.kind]}中` : "暂无进行中的活动";
  $("#life-description").textContent = active
    ? `你正在原地${lifeNames[active.kind]}，计划在 ${formatWorldTime(active.ends_world_time)} 结束。`
    : "休息会随世界时间逐步恢复精力；等待不会额外恢复精力。世界暂停时，活动也暂停。";
  $("#life-progress-wrap").hidden = !active;
  if (active) {
    $("#life-progress").value = active.progress;
    $("#life-progress-text").textContent = `已过 ${active.elapsed_minutes} / ${active.duration_minutes} 分钟${active.progress >= 1 ? " · 等待结算" : ""}`;
  }
  $("#life-start").hidden = Boolean(active);
  $("#life-kind").disabled = Boolean(active) || state.busy;
  $("#life-minutes").disabled = Boolean(active) || state.busy;
  $("#life-stop").hidden = !active;
  const last = lifeScene.last_activity;
  const checkLabel = last?.result?.check ? {success:"检定成功",partial:"部分成功",failure:"检定失败"}[last.result.check.outcome] : null;
  $("#life-last").textContent = !active && last
    ? `上次${lifeNames[last.kind]}${checkLabel || lifeStatuses[last.status]}。${last.reason}` : "";
  if (!active && last?.result?.tool_wear?.length) $("#life-last").textContent += " 工具损耗：" + last.result.tool_wear.map((tool) => `${taskCatalog.find((item) => item.id === tool.item_type_id)?.name || "工具"} -${tool.wear}，完好度${tool.condition}/100`).join("；") + "。在原活动地点领取。";
  if (lifeScene.food_discomfort) $("#life-last").textContent += ` 肠胃不适，持续至${formatWorldTime(lifeScene.food_discomfort.expires_world_time)}，新检定条件修正-10。`;
  const nodes = lifeScene.items.map((item) => {
    const row = document.createElement("article");
    row.className = "life-item";
    const label = document.createElement("span");
    const ownership = item.ownership === "self" ? "属于你" : item.ownership === "other" ? "已有主人" : "无登记所有者";
    label.textContent = `${item.name} × ${item.quantity} · ${item.place === "ground" ? "地面" : "背包"} · ${ownership}${item.freshness ? ` · ${item.freshness.label}${item.freshness.spoils_world_time ? `（${formatWorldTime(item.freshness.spoils_world_time)}到期）` : ""}` : ""}`;
    row.append(label);
    if (item.category === "food" && item.place === "bag" && item.ownership === "self") {
      const eat = document.createElement("button"); eat.type = "button"; eat.className = "button secondary";
      const spoiled = item.freshness?.state === "spoiled";
      eat.textContent = spoiled ? `食用${item.name}（已变质）` : `食用${item.name}`;
      eat.disabled = state.busy || Boolean(lifeScene.activity);
      let requestId = null;
      const submitMeal = () => {
        if (state.busy) return;
        requestId ||= crypto.randomUUID();
        lifeMutation(`items/${encodeURIComponent(item.id)}/eat`, {request_id:requestId,accept_spoiled:spoiled});
      };
      eat.addEventListener("click", () => {
        if (!spoiled) { submitMeal(); return; }
        const confirmation = document.createElement("div"); confirmation.className = "life-controls";
        confirmation.setAttribute("role", "alertdialog");
        confirmation.setAttribute("aria-label", "确认食用变质食物");
        confirmation.append(emptyElement("p", "这份食物已变质，不能恢复饱食，并会造成6世界小时肠胃不适、消耗最多8点精力，期间新检定条件修正-10。仍然食用吗？", ""));
        const cancel = document.createElement("button"), accept = document.createElement("button");
        cancel.type = accept.type = "button";
        cancel.className = accept.className = "button secondary";
        cancel.textContent = "取消食用"; accept.textContent = "确认食用变质食物";
        cancel.addEventListener("click", () => { confirmation.remove(); eat.disabled = state.busy || Boolean(lifeScene.activity); eat.focus(); });
        accept.addEventListener("click", submitMeal);
        confirmation.append(cancel, accept); row.append(confirmation); eat.disabled = true; cancel.focus();
      });
      row.append(eat);
    }
    for (const operation of item.actions) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button secondary";
      button.textContent = { inspect: "查看", pickup: "拾取", drop: "放下" }[operation];
      button.setAttribute("aria-label", `${button.textContent}${item.name}`);
      button.disabled = state.busy || (Boolean(active) && operation !== "inspect");
      button.addEventListener("click", () => interactLifeItem(item.id, operation));
      row.append(button);
    }
    return row;
  });
  if (!nodes.length) nodes.push(emptyElement("p", "此处没有可接近的地面物品，你的背包也空着。", "muted"));
  $("#life-items").replaceChildren(...nodes);
}

async function lifeMutation(path, body) {
  if (state.busy || !state.worldId) return;
  const worldId = state.worldId;
  setBusy(true, "正在处理你的生活行动");
  try {
    const result = await api(`/api/worlds/${worldId}/player/life/${path}`, {
      method: "POST", ...(body ? { body: JSON.stringify(body) } : {}),
    });
    if (worldId !== state.worldId) return;
    $("#action-result").textContent = result.summary;
    $("#action-result").hidden = false;
    $("#life-item-detail").hidden = true;
    await refreshAll();
    return result;
  } catch (error) {
    showToast(error.message, true);
    await refreshLife(worldId);
  } finally {
    setBusy(false);
    renderLifeScene();
  }
}

async function interactLifeItem(itemId, operation) {
  if (operation !== "inspect") return lifeMutation(`items/${encodeURIComponent(itemId)}`, { operation });
  const worldId = state.worldId;
  try {
    const item = await api(`/api/worlds/${worldId}/player/life/items/${encodeURIComponent(itemId)}`);
    if (worldId !== state.worldId) return;
    $("#life-item-detail").textContent = `${item.name}：数量 ${item.quantity}，完好度 ${item.condition}/100，${item.place === "ground" ? "放在当前地点" : "收在你的背包中"}。`;
    $("#life-item-detail").hidden = false;
  } catch (error) { showToast(error.message, true); }
}

$("#life-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!$("#life-form").reportValidity()) return;
  lifeMutation("activities", {
    kind: $("#life-kind").value,
    duration_minutes: Number($("#life-minutes").value),
  });
});
$("#life-stop").addEventListener("click", () => {
  if (lifeScene?.activity) lifeMutation(`activities/${encodeURIComponent(lifeScene.activity.id)}/stop`);
});
Object.assign(eventLabels, { life_completed: "生活活动完成", life_cancelled: "生活活动结束", life_interrupted: "生活活动中断" });
