/* 房间、门与容器操作只使用服务端返回的对象与状态。 */
function interiorButton(label, path, operation, disabled = false) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "button secondary";
  button.textContent = label;
  button.disabled = disabled || state.busy || Boolean(lifeScene?.activity);
  button.addEventListener("click", () => lifeMutation(path, { operation }));
  return button;
}

function interiorTransferForm(fixture, item, deposit) {
  const form = document.createElement("form");
  form.className = "life-controls interior-transfer";
  const label = document.createElement("label");
  label.textContent = `${deposit ? "存入" : "取出"}${item.name}（可用 ${item.quantity}）${item.freshness?.spoils_world_time ? ` · ${item.freshness.label} · ${formatWorldTime(item.freshness.spoils_world_time)}到期` : ""}`;
  const input = document.createElement("input");
  input.type = "number"; input.min = "1"; input.max = String(item.quantity); input.step = "1";
  input.value = "1"; input.required = true;
  label.append(input);
  const button = document.createElement("button");
  button.type = "submit"; button.className = "button secondary";
  button.textContent = `${deposit ? "存入" : "取出"}${item.name}`;
  button.disabled = state.busy || Boolean(lifeScene?.activity);
  form.append(label, button);
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (form.reportValidity()) lifeMutation(`fixtures/${encodeURIComponent(fixture.id)}/items`, {
      item_id: item.id, quantity: Number(input.value), operation: deposit ? "deposit" : "withdraw",
    });
  });
  return form;
}

function renderInterior(interior) {
  if (!interior) return;
  $("#interior-location").textContent = interior.room
    ? `你在${interior.room.name}里面${interior.seated ? "，正坐在座位上" : ""}。`
    : "你在室外。走近已登记的房间入口，可以开门进入。";
  if (interior.room) {
    elements.sceneLocation.textContent = interior.room.name;
    elements.navLocation.textContent = interior.room.name;
    elements.sceneNarration.textContent = `你在${interior.room.name}停留。眼前的门和家具可以查看、操作，变化会留在这里。`;
  }
  const doors = interior.doors.map((door) => {
    const row = document.createElement("article"); row.className = "life-item";
    const label = document.createElement("span");
    label.textContent = `${door.name}的门 · ${door.locked ? "已上锁" : door.open ? "敞开" : "关闭"}`;
    row.append(label);
    const path = `doors/${encodeURIComponent(door.id)}`;
    if (!door.inside) {
      const knock = document.createElement("button"); knock.type = "button";
      knock.className = "button secondary"; knock.textContent = `敲${door.name}的门`;
      knock.disabled = state.busy || Boolean(lifeScene?.activity) || interior.seated;
      let requestId = null;
      knock.addEventListener("click", () => {
        requestId ||= crypto.randomUUID();
        lifeMutation(`${path}/knock`, {request_id:requestId});
      });
      row.append(knock);
    }
    if (door.locked) {
      if (door.keyed || door.inside) row.append(interiorButton(`解锁${door.name}的门`, path, "unlock"));
    } else if (door.allowed || door.inside) {
      row.append(interiorButton(`${door.open ? "关上" : "打开"}${door.name}的门`, path, door.open ? "close" : "open"));
      if (door.open) row.append(interiorButton(`${door.inside ? "离开" : "进入"}${door.name}`, path, door.inside ? "exit" : "enter", interior.seated));
      if (!door.open && door.owner) row.append(interiorButton(`锁上${door.name}的门`, path, "lock"));
    }
    if (!door.allowed && !door.inside) row.append(emptyElement("small", "需要进入许可", "muted"));
    if (door.owner) {
      const form = document.createElement("form"); form.className = "life-controls";
      const label = document.createElement("label"); label.textContent = `${door.name}的使用许可`;
      const select = document.createElement("select");
      for (const npc of state.snapshot.characters.filter((item) => !item.is_player)) select.append(new Option(npc.name, npc.id));
      label.append(select); form.append(label);
      for (const allow of [true, false]) {
        const button = document.createElement("button"); button.type = "button";
        button.className = "button secondary"; button.textContent = allow ? "授予许可" : "撤回许可";
        button.addEventListener("click", () => {
          if (select.value) lifeMutation(`rooms/${encodeURIComponent(door.id)}/access`, {character_id: select.value, allow});
        });
        form.append(button);
      }
      row.append(form);
    }
    return row;
  });
  if (!doors.length) doors.push(emptyElement("p", "附近没有已登记的房间入口。", "muted"));
  $("#interior-doors").replaceChildren(...doors);
  const visitLabels = {pending:"等待应门",invited:"已获一次进入邀请",entered:"已使用邀请进入",declined:"未获邀请",cancelled:"已撤回",expired:"已过期，未收到有效进入邀请"};
  for (const visit of interior.visits || []) {
    const row = document.createElement("article"); row.className = "life-item";
    row.append(emptyElement("p", `${visit.room_name} · ${visit.mine ? "你的拜访" : visit.visitor_label} · ${visitLabels[visit.status]}${visit.status === "invited" ? `，有效至${formatWorldTime(visit.expires_world_time)}，不含储物权限` : ""}`, ""));
    if (visit.can_respond) for (const accept of [true, false]) {
      const button = document.createElement("button"); button.type = "button"; button.className = "button secondary";
      button.textContent = accept ? "开门并邀请进入" : "不接待这次拜访";
      button.disabled = state.busy;
      button.addEventListener("click", () => lifeMutation(`visits/${encodeURIComponent(visit.id)}/respond`, {accept}));
      row.append(button);
    }
    if (["pending","invited"].includes(visit.status)) {
      const button = document.createElement("button"); button.type = "button"; button.className = "button secondary";
      button.textContent = visit.mine ? "取消拜访" : "撤回邀请"; button.disabled = state.busy;
      button.addEventListener("click", () => lifeMutation(`visits/${encodeURIComponent(visit.id)}/withdraw`, {}));
      row.append(button);
    }
    $("#interior-doors").append(row);
  }
  const fixtures = interior.fixtures.map((fixture) => {
    const row = document.createElement("article"); row.className = "interior-fixture";
    const heading = document.createElement("h4"); heading.textContent = fixture.name; row.append(heading);
    const path = `fixtures/${encodeURIComponent(fixture.id)}`;
    if (fixture.kind === "seat") {
      row.append(interiorButton(fixture.seated_here ? `从${fixture.name}起身` : `在${fixture.name}坐下`, path,
        fixture.seated_here ? "stand" : "sit", fixture.occupied && !fixture.seated_here));
    } else {
      row.append(emptyElement("p", `容量 ${fixture.capacity} 格 · ${fixture.locked ? "已上锁" : fixture.open ? "打开" : "关闭"}`, "muted"));
      if (fixture.usable) {
        if (fixture.locked) row.append(interiorButton(`解锁${fixture.name}`, path, "unlock"));
        else {
          row.append(interiorButton(`${fixture.open ? "关闭" : "打开"}${fixture.name}`, path, fixture.open ? "close" : "open"));
          if (!fixture.open) row.append(interiorButton(`锁上${fixture.name}`, path, "lock"));
        }
        if (fixture.open && !fixture.locked) {
          if (!fixture.contents.length) row.append(emptyElement("p", "容器里没有物品。", "muted"));
          for (const item of fixture.contents) {
            if (item.owned) row.append(interiorTransferForm(fixture, item, false));
            else row.append(emptyElement("p", `${item.name} × ${item.quantity} · 属于别人`, "muted"));
          }
          for (const item of lifeScene.items.filter((item) => item.place === "bag" && item.ownership === "self")) row.append(interiorTransferForm(fixture, item, true));
        }
      } else row.append(emptyElement("p", "需要所有者授予使用许可。", "muted"));
    }
    return row;
  });
  $("#interior-fixtures").replaceChildren(...fixtures);
}

function renderInteriorOptions() {
  const keep = (selector, options) => {
    const select = $(selector), value = select.value;
    select.replaceChildren(...options.map(([name, id]) => new Option(name, id)));
    if (options.some((item) => item[1] === value)) select.value = value;
  };
  keep("#interior-layout-location", state.snapshot.locations.map((item) => [item.name, item.id]));
  keep("#interior-layout-owner", [["无个人所有者", ""], ...state.snapshot.characters.map((item) => [item.name, item.id])]);
  keep("#interior-layout-parent", [["直接通向室外", ""], ...state.registrations.filter((item) => item.element_type === "interior_room" && item.status === "applied").map((item) => [item.payload.name, item.result_entity_id])]);
}

function interiorProposalDetails(payload) {
  const location = state.snapshot.locations.find((item) => item.id === payload.location_id)?.name || payload.location_id;
  const owner = state.snapshot.characters.find((item) => item.id === payload.owner_character_id)?.name || "无个人所有者";
  const parent = payload.parent_room_id ? (state.registrations.find((item) => item.result_entity_id === payload.parent_room_id)?.payload?.name || payload.parent_room_id) : "室外";
  return `<p>地点：${escapeHtml(location)} · 入口：${escapeHtml(parent)}<br>所有者：${escapeHtml(owner)} · ${payload.access_policy === "private" ? "私人空间" : "公共空间"}<br>钥匙：${escapeHtml(payload.key_item_type_id || "沿用权限锁")}<br>接待规则：${escapeHtml(({manual:"仅手动",authorized_only:"仅已有许可者",trusted_contacts:"已有许可者及信任的熟人"})[payload.visitor_policy || "authorized_only"])}</p><ul>${payload.fixtures.map((item) => `<li>${escapeHtml(item.name)} · ${item.kind === "seat" ? "座位" : `储物容器，${item.capacity}格`}</li>`).join("")}</ul>`;
}

let pendingInteriorKey = null;
$("#interior-layout-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy || !$("#interior-layout-form").reportValidity()) return;
  const fixtures = [];
  if ($("#interior-layout-container").value.trim()) fixtures.push({name: $("#interior-layout-container").value.trim(), kind:"container", capacity:Number($("#interior-layout-capacity").value)});
  if ($("#interior-layout-seat").value.trim()) fixtures.push({name: $("#interior-layout-seat").value.trim(), kind:"seat"});
  pendingInteriorKey ||= crypto.randomUUID();
  const body = {idempotency_key: pendingInteriorKey, room: {
    name: $("#interior-layout-name").value.trim(), location_id: $("#interior-layout-location").value,
    parent_room_id: $("#interior-layout-parent").value || null, owner_character_id: $("#interior-layout-owner").value || null,
    access_policy: $("#interior-layout-policy").value, nightly_rate:Number($("#interior-layout-rent").value), fixtures,
    key_item_type_id: $("#interior-layout-key").value.trim() || null,
    visitor_policy: $("#interior-layout-visitors").value,
  }};
  setBusy(true, "正在提交室内布局");
  try {
    const result = await api(`/api/worlds/${state.worldId}/interior-layouts`, {method:"POST",body:JSON.stringify(body)});
    pendingInteriorKey = null; showToast(result.summary); await refreshAll();
  } catch (error) { showToast(error.message, true); }
  finally { setBusy(false); }
});
