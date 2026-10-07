"use strict";

// 哥哥，人物文本只作为文本显示，所有请求都是只读查询。
const scienceNamespace = "http://www.w3.org/2000/svg";
const scienceStatus = {verified: "已验证", needs_review: "待复核", invalidated: "已失效", proposed: "已提出", needs_evidence: "待证据", duplicate: "重复方法", unsupported: "暂不可执行"};
function scienceSvg(name, attrs = {}) {
  const el = document.createElementNS(scienceNamespace, name);
  for (const [key, value] of Object.entries(attrs)) el.setAttribute(key, String(value));
  return el;
}
async function scienceGet(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`读取失败：${response.status}`);
  return response.json();
}
function scienceRenderGraph(host, graph, open) {
  host.replaceChildren();
  if (!graph.nodes.length) { host.textContent = "尚无已验证科技"; return; }
  const rank = new Map(graph.nodes.map(n => [n.id, 0]));
  for (let step = 0; step < graph.nodes.length; step++) {
    let changed = false;
    for (const edge of graph.edges) {
      if (!rank.has(edge.parent_id) || !rank.has(edge.child_id)) continue;
      const next = rank.get(edge.parent_id) + 1;
      if (next > rank.get(edge.child_id)) { rank.set(edge.child_id, next); changed = true; }
    }
    if (!changed) break;
  }
  const rows = new Map(), positions = new Map();
  for (const node of graph.nodes) {
    const level = rank.get(node.id), row = rows.get(level) || 0;
    rows.set(level, row + 1);
    positions.set(node.id, {x: 16 + level * 290, y: 20 + row * 104});
  }
  const width = 290 * (Math.max(...rank.values()) + 1), height = 104 * Math.max(...rows.values()) + 24;
  const svg = scienceSvg("svg", {viewBox: `0 0 ${width} ${height}`, width, height, role: "img", "aria-label": "科技依赖图"});
  for (const edge of graph.edges) {
    const a = positions.get(edge.parent_id), b = positions.get(edge.child_id);
    if (a && b) svg.append(scienceSvg("path", {d: `M ${a.x + 252} ${a.y + 36} L ${b.x} ${b.y + 36}`, class: "science-edge"}));
  }
  for (const node of graph.nodes) {
    const p = positions.get(node.id), status = node.version_status || node.status;
    const group = scienceSvg("g", {transform: `translate(${p.x} ${p.y})`, tabindex: 0, role: "button", "aria-label": node.name, class: `science-node ${status}`});
    group.append(scienceSvg("rect", {width: 252, height: 74, rx: 10}));
    const title = scienceSvg("text", {x: 12, y: 27});
    const letters = Array.from(node.name);
    title.textContent = letters.length > 16 ? letters.slice(0, 16).join("") + "…" : node.name;
    const desc = scienceSvg("text", {x: 12, y: 53, class: "science-node-status"}); desc.textContent = `版本 ${node.current_version} · ${scienceStatus[status] || status}`;
    const tooltip = scienceSvg("title"); tooltip.textContent = node.name;
    group.append(title, desc, tooltip);
    group.addEventListener("click", () => open(node));
    group.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(node); } });
    svg.append(group);
  }
  host.append(svg);
}

const scienceLink = document.getElementById("science-link");
if (scienceLink) scienceLink.addEventListener("click", () => {
  scienceLink.href = `/ui/science.html?world=${encodeURIComponent(document.getElementById("world-select").value)}`;
});
const scienceWorld = document.getElementById("science-world");
if (scienceWorld) {
  async function refreshScience() {
    const world = scienceWorld.value, base = `/api/worlds/${encodeURIComponent(world)}`;
    if (!world) return;
    try {
      const [summary, graph, candidates] = await Promise.all([scienceGet(`${base}/science`), scienceGet(`${base}/technologies/graph`), scienceGet(`${base}/science/candidates`)]);
      const u = summary.usage;
      document.getElementById("science-budget").textContent = summary.session ? `会话 ${summary.session.status} · API ${u.calls}/${u.max_calls} 次 · 已计入 ${u.accounted_tokens}/${u.token_limit} token · 在途预留 ${u.reserved_tokens} · ${u.stop_reason || "尚未停机"}` : "该世界尚未开启科研会话";
      scienceRenderGraph(document.getElementById("science-graph"), graph, async node => {
        document.getElementById("science-process-title").textContent = `${node.name} · 原始过程`;
        try { document.getElementById("science-process").textContent = JSON.stringify(await scienceGet(`${base}/technologies/${encodeURIComponent(node.id)}/process`), null, 2); }
        catch (e) { document.getElementById("science-process").textContent = e.message; }
      });
      const list = document.getElementById("science-candidates"); list.replaceChildren();
      for (const candidate of candidates) {
        const item = document.createElement("p"); item.textContent = `${candidate.name} · ${scienceStatus[candidate.status] || candidate.status} · ${candidate.problem} · 缺口：${candidate.gaps_json}`; list.append(item);
      }
      if (!candidates.length) list.textContent = "尚无候选";
    } catch (e) { document.getElementById("science-budget").textContent = e.message; }
  }
  scienceGet("/api/worlds").then(worlds => {
    for (const world of worlds) { const option = document.createElement("option"); option.value = world.id; option.textContent = world.name; scienceWorld.append(option); }
    const requested = new URLSearchParams(location.search).get("world");
    if ([...scienceWorld.options].some(o => o.value === requested)) scienceWorld.value = requested;
    return refreshScience();
  }).catch(e => { document.getElementById("science-budget").textContent = e.message; });
  scienceWorld.addEventListener("change", refreshScience);
  document.getElementById("science-refresh").addEventListener("click", refreshScience);
}
