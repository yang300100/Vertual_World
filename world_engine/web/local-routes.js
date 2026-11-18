/* 哥哥，编年者核对实地通路，候选仍在登记清单中单独确认。 */
Object.assign(eventLabels, {
  "world.local_route_registered": "核实局部通路",
  "world.local_route_closed": "局部通路关闭",
  "world.local_route_reopened": "局部通路重新开放",
});
function localRouteProposalDetails(payload) {
  const points = Array.isArray(payload.polyline) ? payload.polyline : [];
  return `<p>已勘察的既存通路 · ${escapeHtml(payload.bidirectional ? "双向" : "按折线方向单向")} · 步行限速 ${escapeHtml(payload.speed_limit_kmh)} 公里/小时</p><p>地点 ${escapeHtml(payload.location_id)} · 导航数据集 ${escapeHtml(payload.dataset_id)}</p><details><summary>核对全部 ${points.length} 个经纬度点</summary><pre>${escapeHtml(JSON.stringify(points, null, 2))}</pre></details>`;
}

(() => {
  const root = document.querySelector("#room-observer-registrations");
  if (!root) return;
  const section = document.createElement("section");
  section.className = "observer-card";
  section.id = "local-route-management";
  root.append(section);

  function text(parent, value, tag = "p") {
    const node = document.createElement(tag);
    node.textContent = value;
    parent.append(node);
    return node;
  }
  function field(parent, label, tag = "input") {
    const box = document.createElement("label");
    box.textContent = label;
    const input = document.createElement(tag);
    input.setAttribute("aria-label", label);
    box.append(input);
    parent.append(box);
    return input;
  }
  function option(select, value, label) {
    const node = document.createElement("option");
    node.value = value;
    node.textContent = label;
    select.append(node);
  }
  function button(parent, label) {
    const node = document.createElement("button");
    node.type = "button";
    node.className = "button secondary";
    node.textContent = label;
    parent.append(node);
    return node;
  }

  text(section, "核实既存通路", "h2");
  text(section, "填写勘察确认的实际折线。提交后，在登记清单中核对来源和全部坐标，再确认通路。行走时会按通路逐段推进。");
  const load = button(section, "读取通路与勘察来源");
  const form = document.createElement("form");
  form.className = "long-term-form";
  section.append(form);
  const name = field(form, "通路名称");
  name.maxLength = 120;
  name.required = true;
  const dataset = field(form, "已批准的导航数据集", "select");
  const location = field(form, "勘察地点", "select");
  const source = field(form, "同地点的来源事件", "select");
  for (const input of [dataset, location, source]) input.required = true;
  const polyline = field(form, "经纬度折线（JSON 数组）", "textarea");
  polyline.rows = 6;
  polyline.required = true;
  polyline.placeholder = "[[经度, 纬度], [经度, 纬度]]";
  const speed = field(form, "步行限速（公里/小时）");
  speed.type = "number";
  speed.min = "0";
  speed.step = "any";
  speed.value = "5";
  speed.required = true;
  const bidirectional = field(form, "允许双向通行");
  bidirectional.type = "checkbox";
  bidirectional.checked = true;
  const submit = button(form, "提交通路候选");
  submit.type = "submit";
  const retry = button(form, "核对上次提交结果");
  retry.hidden = true;
  const notice = text(section, "请先读取当前世界的通路和来源。");
  notice.setAttribute("aria-live", "polite");
  const rows = document.createElement("div");
  rows.className = "registration-list";
  section.append(rows);

  let world = "";
  let epoch = 0;
  let data = null;
  let sending = false;
  let loadSequence = 0;
  const fields = [name, dataset, location, source, polyline, speed, bidirectional];
  const storageKey = id => `virtual-world:local-route-pending:${encodeURIComponent(id)}`;
  const url = id => `/api/worlds/${encodeURIComponent(id)}`;
  const current = (id, generation) => state.worldId === id && world === id && epoch === generation;

  function pending() {
    if (!world) return null;
    try {
      const raw = sessionStorage.getItem(storageKey(world));
      if (!raw) return null;
      const value = JSON.parse(raw);
      if (value.world === world && value.request?.payload?.element_type === "local_route"
          && typeof value.request.idempotency_key === "string"
          && typeof value.request.source_event_id === "string") return value;
    } catch (_) { /* 哥哥，无法读取的请求留给用户核对。 */ }
    return { invalid: true };
  }

  function updateControls() {
    const stored = pending();
    for (const input of fields) input.disabled = sending || Boolean(stored);
    submit.disabled = sending || Boolean(stored) || !data;
    retry.hidden = !stored;
    retry.disabled = sending || !stored || stored.invalid;
    load.disabled = sending;
  }

  function syncWorld() {
    const selected = state.worldId || "";
    if (world === selected) return;
    world = selected;
    epoch += 1;
    data = null;
    name.value = "";
    polyline.value = "";
    speed.value = "5";
    bidirectional.checked = true;
    for (const input of [dataset, location, source]) input.replaceChildren();
    rows.replaceChildren();
    notice.textContent = selected ? "请读取当前世界的通路和来源。" : "请先选择世界。";
    const stored = pending();
    if (stored) notice.textContent = stored.invalid ? "上次请求记录无法读取，请核对登记清单。" : "上次提交结果待核对，请使用原请求查询。";
    updateControls();
  }

  function sourceOptions(select, placeId) {
    select.replaceChildren();
    option(select, "", "请选择来源事件");
    const place = data?.locations.find(item => item.id === placeId);
    for (const event of place?.sources || []) {
      option(select, event.id, `${formatWorldTime(event.occurred_at)} · ${event.summary || event.id}`);
    }
  }
  location.addEventListener("change", () => sourceOptions(source, location.value));

  function renderRoutes(routes, selectedWorld, generation) {
    rows.replaceChildren();
    if (!routes.length) text(rows, "尚无已确认的局部通路。");
    for (const route of routes) {
      const row = document.createElement("article");
      row.className = "registration-item";
      text(row, route.name, "strong");
      text(row, `${route.is_open ? "开放" : "关闭"} · ${route.bidirectional ? "双向" : "单向"} · 限速 ${route.speed_limit_kmh} 公里/小时`);
      const details = document.createElement("details");
      text(details, "查看折线", "summary");
      text(details, JSON.stringify(route.polyline, null, 2), "pre");
      row.append(details);
      const changeSource = field(row, `变更 ${route.name} 的来源事件`, "select");
      sourceOptions(changeSource, route.location_id);
      const toggle = button(row, route.is_open ? "关闭通路" : "重新开放通路");
      toggle.addEventListener("click", async () => {
        if (!current(selectedWorld, generation) || sending) return;
        if (!changeSource.value) {
          notice.textContent = "请先选择与这次通路状态变化有关的来源事件。";
          return;
        }
        toggle.disabled = true;
        try {
          await api(`${url(selectedWorld)}/local-routes/${encodeURIComponent(route.id)}/state`, {
            method: "PATCH",
            body: JSON.stringify({ is_open: !route.is_open, expected_state_version: route.state_version,
              source_event_id: changeSource.value, requested_by_character_id: null }),
          });
          if (current(selectedWorld, generation)) await loadCurrent();
        } catch (error) {
          if (current(selectedWorld, generation)) notice.textContent = `请刷新通路状态后核对结果。${error.message || ""}`;
        } finally {
          toggle.disabled = false;
        }
      });
      rows.append(row);
    }
  }

  async function loadCurrent() {
    syncWorld();
    const selectedWorld = world;
    const generation = epoch;
    if (!selectedWorld || sending) return;
    const sequence = ++loadSequence;
    const requestCurrent = () => current(selectedWorld, generation) && sequence === loadSequence;
    load.disabled = true;
    try {
      const [options, routes] = await Promise.all([
        api(`${url(selectedWorld)}/local-routes/options`), api(`${url(selectedWorld)}/local-routes`),
      ]);
      if (!requestCurrent()) return;
      if (!Array.isArray(options.datasets) || !Array.isArray(options.locations) || !Array.isArray(routes)) throw new Error("通路记录格式无效");
      data = options;
      const priorDataset = dataset.value;
      const priorLocation = location.value;
      const priorSource = source.value;
      dataset.replaceChildren();
      location.replaceChildren();
      option(dataset, "", "请选择数据集");
      option(location, "", "请选择地点");
      for (const item of data.datasets) option(dataset, item.id, item.name);
      for (const item of data.locations) option(location, item.id, item.name);
      if (data.datasets.some(item => item.id === priorDataset)) dataset.value = priorDataset;
      if (data.locations.some(item => item.id === priorLocation)) location.value = priorLocation;
      sourceOptions(source, location.value);
      if (Array.from(source.options).some(item => item.value === priorSource)) source.value = priorSource;
      renderRoutes(routes, selectedWorld, generation);
      notice.textContent = "已读取通路与近期来源，填写中的折线已保留。";
    } catch (error) {
      if (requestCurrent()) notice.textContent = `读取失败。${error.message || "请稍后重试。"}`;
    } finally {
      if (requestCurrent()) updateControls();
    }
  }
  load.addEventListener("click", loadCurrent);

  async function send(value) {
    syncWorld();
    const selectedWorld = world;
    const generation = epoch;
    if (sending || value.invalid || selectedWorld !== value.world) return;
    try {
      sessionStorage.setItem(storageKey(selectedWorld), JSON.stringify(value));
    } catch (_) {
      notice.textContent = "无法保存原请求，暂时无法提交。";
      return;
    }
    sending = true;
    updateControls();
    try {
      const result = await api(`${url(selectedWorld)}/registrations`, { method: "POST", body: JSON.stringify(value.request) });
      if (!result?.id || result.element_type !== "local_route" || !["proposed", "applied", "rejected", "failed"].includes(result.status)) throw new Error("登记结果尚未确认");
      sessionStorage.removeItem(storageKey(selectedWorld));
      if (current(selectedWorld, generation)) {
        notice.textContent = result.status === "proposed" ? "通路候选已提交，请在登记清单中核对全部坐标与来源后确认。" : `原请求当前为 ${result.status}，请在登记清单中核对。`;
        if (typeof refreshWorldState === "function") {
          try { await refreshWorldState({ silent: true }); }
          catch (_) {
            if (current(selectedWorld, generation)) notice.textContent += " 请刷新登记清单查看。";
          }
        }
      }
    } catch (error) {
      if (!value.uncertain && [400, 404, 409, 422].includes(error.status)) {
        try { sessionStorage.removeItem(storageKey(selectedWorld)); } catch (_) { value.uncertain = true; }
        if (current(selectedWorld, generation)) notice.textContent = `本次未提交，请修改后重试。${error.message || ""}`;
      } else {
        value.uncertain = true;
        try { sessionStorage.setItem(storageKey(selectedWorld), JSON.stringify(value)); } catch (_) { /* 已保存发送前的原请求。 */ }
        if (current(selectedWorld, generation)) notice.textContent = `提交结果待核对，请使用原请求重试。${error.message || ""}`;
      }
    } finally {
      sending = false;
      syncWorld();
      updateControls();
    }
  }

  form.addEventListener("submit", async event => {
    event.preventDefault();
    syncWorld();
    if (!world || !data || pending() || sending || !form.reportValidity()) return;
    let points;
    try { points = JSON.parse(polyline.value); } catch (_) {
      notice.textContent = "折线应为经纬度点的 JSON 数组。";
      return;
    }
    if (!Array.isArray(points) || points.length < 2 || points.length > 512 || points.some(point =>
      !Array.isArray(point) || point.length !== 2 || !point.every(Number.isFinite)
      || Math.abs(point[0]) > 180 || Math.abs(point[1]) > 90)) {
      notice.textContent = "请填写 2 至 512 个有效经纬度点。";
      return;
    }
    const limit = Number(speed.value);
    const place = data.locations.find(item => item.id === location.value);
    if (!name.value.trim() || !Number.isFinite(limit) || limit <= 0
        || !data.datasets.some(item => item.id === dataset.value)
        || !place?.sources.some(item => item.id === source.value)) {
      notice.textContent = "请核对名称、来源、数据集与正数限速。";
      return;
    }
    const value = { world, request: { source_event_id: source.value, idempotency_key: `local-route:${crypto.randomUUID()}`,
      requested_by_character_id: null, payload: { element_type: "local_route", origin_mode: "surveyed_existing", name: name.value.trim(),
        dataset_id: dataset.value, location_id: location.value, polyline: points, allowed_modes: ["land"],
        bidirectional: bidirectional.checked, speed_limit_kmh: limit } } };
    await send(value);
  });
  retry.addEventListener("click", async () => {
    syncWorld();
    const stored = pending();
    if (stored && !stored.invalid) await send(stored);
  });
  elements.worldSelect.addEventListener("change", () => {
    epoch += 1;
    syncWorld();
  });
  syncWorld();
})();
