"use strict";

// 哥哥，旧成果的原理归纳与真实研发分开显示，人物文本始终作为文本渲染。
const scienceNamespace = "http://www.w3.org/2000/svg";
const scienceStatus = {researched:"研发达成",rejected:"评审未通过",verified:"已验证",needs_review:"待复核",needs_evidence:"尚需实证",invalidated:"已失效",proposed:"已提出",duplicate:"重复方法",organized:"原理归纳"};
const scienceImpactStatus = {completed:"应用已结算",running:"应用正在进行",interrupted:"应用已中断",cancelled:"应用已取消",unproven:"尚未证实"};
const scienceDomainLabels = {physical:"物理技术",magic:"魔法技术",hybrid:"魔工结合"};
const scienceRelationLabels = {requires:"必需前置",improves:"技术改进",complements:"协同互补",related:"机制联系 · 归纳"};
const scienceAttemptLabels = {needs_food:"缺少可用食物",needs_skill:"缺少适用技能",needs_location:"缺少适用场所",needs_prerequisite:"缺少前置技术",needs_equipment:"原型不在可使用的现场",not_adopted:"尚未采用",pending:"应用待完成",no_applicable_effect:"没有可证实收益",applied:"应用已完成"};
const scienceCycleStatus={researching:"研究中",paused:"研发暂缓",unmapped:"当前阶段状态待核对",blocked:"研发受阻",completed:"研发达成"};
const scienceOperationLabels={heat:"加热",cool:"冷却",wash:"洗涤",dry:"干燥",mix:"混合",screen:"筛分",filter:"过滤",adsorb:"吸附",bind:"结合处理",settle:"静置沉降",store:"储存",form:"成形",compact:"压实",assay:"量器检验",cast:"已知单纹供能",transfer:"相容器物能量转移",inspect:"现场观察",observe:"仪器观察",experiment:"仪器试验",configure:"调节装置",measure:"量测",wait:"等待",rest:"休息"};
const scienceMetricLabels={strength:"相对承重",leakage:"渗漏",porosity:"孔隙",color_retention:"色料固着",strain:"形变",extractable_salt:"可提取组分",particles:"颗粒",moisture:"含水比例",germination:"发芽活力",soil_ph:"酸碱度",charge_band:"储能档位"};
function scienceHours(minutes){return `${Math.round(minutes/6)/10}世界小时`;}
function scienceRenderCycle(host,cycle,name) {
  if(!cycle){host.append(scienceText("p","历史成果未记录研发周期。"));return;}
  if(cycle.kind==="empirical") {
    host.append(scienceText("h3","实际试验进度"),scienceText("p",`${scienceCycleStatus[cycle.status]} · 已结算 ${cycle.settled_steps} 段试验与观察 · 已结算投入 ${scienceHours(cycle.earned_minutes)}`));
    const stage=cycle.current_stage;
    if(stage) {
      host.append(scienceText("p",`当前工序：${stage.sample_name} · ${scienceOperationLabels[stage.operation]||stage.operation} · ${stage.status==="queued"?"待开工":"等待实际结算"}`));
      if(stage.is_research===false)host.append(scienceText("p","当前为等待或生活阶段，不计作研究投入。"));
      else if(stage.planned_minutes>0) {
        const progress=document.createElement("progress");progress.max=stage.planned_minutes;progress.value=stage.worked_minutes;progress.setAttribute("aria-label",`${name}当前试验工序进度`);
        host.append(progress,scienceText("p",`当前工序实际投入 ${scienceHours(stage.worked_minutes)} / 计划 ${scienceHours(stage.planned_minutes)}；尚未结算的工序不计作试验结果。`));
      }
    }
    host.append(scienceText("p",cycle.reason));return;
  }
  const progress=document.createElement("progress");progress.max=100;progress.value=cycle.progress;progress.setAttribute("aria-label",`${name}研发进度`);
  host.append(scienceText("h3","研发周期"),progress,scienceText("p",`${scienceCycleStatus[cycle.status]} · ${cycle.progress}% · 已投入 ${scienceHours(cycle.earned_minutes)} / 所需 ${scienceHours(cycle.required_minutes)}`));
  if(cycle.status!=="completed")host.append(scienceText("p",`剩余有效研究时间：${scienceHours(cycle.remaining_minutes)}；按正常生活安排分段投入。${cycle.reason?" 当前阻碍："+cycle.reason:""}`));
}
function scienceSvg(name, attrs = {}) {
  const el = document.createElementNS(scienceNamespace,name);
  for (const [key,value] of Object.entries(attrs)) el.setAttribute(key,String(value));
  return el;
}
function scienceText(tag,text,className) {
  const el = document.createElement(tag); el.textContent = text;
  if (className) el.className = className;
  return el;
}
async function scienceGet(url) {
  const response = await fetch(url);
  if (!response.ok) {
    let message = `读取失败（${response.status}）`;
    try { const data = await response.json(); if (typeof data.detail === "string") message = data.detail; } catch (_) { /* 哥哥，无错误详情时保留状态码。 */ }
    throw new Error(message);
  }
  return response.json();
}
function scienceRanks(graph) {
  const ranks = new Map(graph.nodes.map(n=>[n.id,0]));
  const degrees = new Map(graph.nodes.map(n=>[n.id,0]));
  const children = new Map(graph.nodes.map(n=>[n.id,[]]));
  for (const edge of graph.edges) {
    if (!degrees.has(edge.parent_id) || !degrees.has(edge.child_id) || edge.kind === "complements") continue;
    degrees.set(edge.child_id,degrees.get(edge.child_id)+1);
    children.get(edge.parent_id).push(edge.child_id);
  }
  const queue = graph.nodes.filter(n=>degrees.get(n.id)===0).map(n=>n.id);
  for (let cursor=0;cursor<queue.length;cursor++) {
    const id=queue[cursor];
    for (const child of children.get(id)) {
      ranks.set(child,Math.max(ranks.get(child),ranks.get(id)+1));
      degrees.set(child,degrees.get(child)-1);
      if (degrees.get(child)===0) queue.push(child);
    }
  }
  return ranks;
}
function scienceRenderGraph(host,graph,open) {
  host.replaceChildren();
  if (!graph.nodes.length) {host.textContent="该领域尚无已研发技术；世界规则与研究方向不计作研究成果。";return;}
  const rank=scienceRanks(graph),rows=new Map(),positions=new Map();
  for (const node of graph.nodes) {
    const level=rank.get(node.id),row=rows.get(level)||0;
    rows.set(level,row+1);positions.set(node.id,{x:16+level*306,y:20+row*116});
  }
  const width=306*(Math.max(...rank.values())+1),height=116*Math.max(...rows.values())+24;
  const svg=scienceSvg("svg",{viewBox:`0 0 ${width} ${height}`,width,height,role:"img","aria-label":"科技原理联系图"});
  const defs=scienceSvg("defs"),marker=scienceSvg("marker",{id:"science-arrow",viewBox:"0 0 10 10",refX:9,refY:5,markerWidth:6,markerHeight:6,orient:"auto-start-reverse"});
  marker.append(scienceSvg("path",{d:"M 0 0 L 10 5 L 0 10 z",fill:"#8c99ae"}));defs.append(marker);svg.append(defs);
  for (const edge of graph.edges) {
    const a=positions.get(edge.parent_id),b=positions.get(edge.child_id);if (!a||!b) continue;
    const path=scienceSvg("path",{d:`M ${a.x+262} ${a.y+44} C ${a.x+284} ${a.y+44}, ${b.x-22} ${b.y+44}, ${b.x} ${b.y+44}`,class:`science-edge ${edge.kind}`,"marker-end":"url(#science-arrow)"});
    const title=scienceSvg("title");title.textContent=`${scienceRelationLabels[edge.kind]||edge.kind}：${edge.explanation}`;path.append(title);svg.append(path);
  }
  for (const node of graph.nodes) {
    const p=positions.get(node.id),group=scienceSvg("g",{transform:`translate(${p.x} ${p.y})`,tabindex:0,role:"button","aria-label":node.name,class:`science-node ${node.status}`});
    group.append(scienceSvg("rect",{width:262,height:88,rx:12}));
    const title=scienceSvg("text",{x:12,y:28});title.textContent=node.name.length>16?Array.from(node.name).slice(0,16).join("")+"…":node.name;
    const detail=scienceSvg("text",{x:12,y:55,class:"science-node-status"});detail.textContent=`${scienceStatus[node.status]||node.status} · 发明 ${node.inventions.length} 项`;
    const tip=scienceSvg("title");tip.textContent=node.name;group.append(title,detail,tip);
    group.addEventListener("click",()=>open(node));group.addEventListener("keydown",e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();open(node);}});svg.append(group);
  }
  host.append(svg);
}
const scienceLink=document.getElementById("science-link");
if(scienceLink) scienceLink.addEventListener("click",()=>{scienceLink.href=`/ui/science.html?world=${encodeURIComponent(document.getElementById("world-select").value)}`;});
const scienceWorld=document.getElementById("science-world");
if(scienceWorld) {
  let generation=0,detailGeneration=0;
  const budget=document.getElementById("science-budget"),refreshButton=document.getElementById("science-refresh"),content=document.getElementById("science-content");
  function setScienceState(status,message) {
    content.setAttribute("aria-busy",String(status==="loading"));
    budget.setAttribute("role",status==="error"?"alert":"status");
    if(message)budget.textContent=message;
    for(const section of content.children)if(section!==budget)section.hidden=status!=="ready";
  }
  function syncScienceWorld() {
    const url=new URL(location.href);
    if(scienceWorld.value)url.searchParams.set("world",scienceWorld.value);
    else url.searchParams.delete("world");
    history.replaceState(null,"",url);
    document.getElementById("science-return").href=scienceWorld.value?`/ui/?world=${encodeURIComponent(scienceWorld.value)}`:"/ui/";
  }
  async function refreshScience() {
    const world=scienceWorld.value,base=`/api/worlds/${encodeURIComponent(world)}`,version=++generation;
    syncScienceWorld();
    if(!world){refreshButton.disabled=false;setScienceState("empty","还没有可查看的世界。创建世界后，点击刷新重新读取。");return;}
    ++detailGeneration;
    refreshButton.disabled=true;
    setScienceState("loading","正在读取当前世界的科研记录…");
    try {
      const [summary,tree,candidates,impacts,magic,projects,mechanisms]=await Promise.all([scienceGet(`${base}/science`),scienceGet(`${base}/science/tree`),scienceGet(`${base}/science/candidates`),scienceGet(`${base}/science/impacts`),scienceGet(`${base}/science/magic`),scienceGet(`${base}/science/projects`),scienceGet(`${base}/science/mechanisms`)]);
      if(version!==generation)return;
      const selectedDomain=document.getElementById("science-domain").value;
      const visibleNodes=tree.nodes.filter(n=>selectedDomain==="all"||(n.domain||"physical")===selectedDomain);
      const visibleIds=new Set(visibleNodes.map(n=>n.id));
      const visibleTree={...tree,nodes:visibleNodes,edges:tree.edges.filter(e=>visibleIds.has(e.parent_id)&&visibleIds.has(e.child_id))};
      document.getElementById("science-domain-counts").textContent=Object.entries(tree.domain_counts||{}).map(([k,v])=>`${scienceDomainLabels[k]} ${v} 项`).join(" · ");
      document.getElementById("science-magic-rules").replaceChildren(...magic.rules.map(r=>scienceText("li",r)));
      document.getElementById("science-magic-state").textContent=`已登记完整魔纹 ${magic.registered_patterns} 项 · 已登记储能器物 ${magic.registered_devices} 件。${magic.notice}`;
      const mechanismHost=document.getElementById("science-mechanisms");mechanismHost.replaceChildren();
      for(const mechanism of mechanisms.mechanisms){
        const box=scienceText("section","","science-invention");
        box.append(scienceText("p",mechanism.description),scienceText("p",`可进行：${mechanism.operations.map(op=>scienceOperationLabels[op]||op).join("、")}`));
        if(mechanism.metrics.length)box.append(scienceText("p",`可观测：${mechanism.metrics.map(metric=>scienceMetricLabels[metric]||metric).join("、")}`));
        mechanismHost.append(box);
      }
      document.getElementById("science-tech-count").textContent=tree.nodes.length;
      document.getElementById("science-invention-count").textContent=tree.invention_count;
      document.getElementById("science-edge-count").textContent=tree.edges.length;
      document.getElementById("science-impact-count").textContent=impacts.filter(i=>i.source_status==="completed").length;
      document.getElementById("science-tree-note").textContent=`${tree.notice} 原始研发记录 ${tree.research_record_count} 项；本结构中独立研发的技术 ${tree.researched_technology_count} 项；真实登记的技术关系 ${tree.reviewed_relation_count} 条。`;
      const u=summary.usage,total=u.cumulative||u,status={prepared:"准备中",running:"科研进行中",stopping:"停止新增判断，已提交动作继续结算",completed:"验收通过",stopped:"科研已停止"};
      document.getElementById("science-budget").textContent=summary.session?`${status[summary.session.status]||summary.session.status} · 本轮 ${u.calls} 次 · 累计 ${total.calls}/${total.max_calls} 次 · 已计账 ${total.accounted_tokens}/${total.token_limit} token · ${u.stop_reason||"尚在进行"}`:"该世界尚未开启科研会话";
      function open(node) {
        const selected=++detailGeneration,host=document.getElementById("science-detail");host.replaceChildren();
        document.getElementById("science-process-title").textContent=`${node.name} · 技术卡片`;
        host.append(scienceText("p",node.basis==="derived_summary"?"依据：从既有发明归纳的技术原理，不计为新增研发。":"依据：NPC提案与独立原理评审。","science-basis"));
        host.append(scienceText("p",`研究领域：${scienceDomainLabels[node.domain||"physical"]}`));
        scienceRenderCycle(host,node.research_cycle,node.name);
        host.append(scienceText("h3","可复用原理"),scienceText("p",node.principle));
        if(node.unlocks?.length)host.append(scienceText("h3","可支持的能力"),scienceText("p",node.unlocks.join("；")));
        host.append(scienceText("h3","技术联系与影响"));
        const relations=tree.edges.filter(e=>e.parent_id===node.id||e.child_id===node.id);
        if(!relations.length)host.append(scienceText("p","尚无有依据的技术联系。"));
        for(const relation of relations) {
          const other=tree.nodes.find(n=>n.id===(relation.parent_id===node.id?relation.child_id:relation.parent_id));
          const section=scienceText("section","","science-relation"),button=scienceText("button",`${scienceRelationLabels[relation.kind]} · ${other.name}`,"button secondary");
          button.type="button";button.addEventListener("click",()=>open(other));
          section.append(button,scienceText("p",relation.explanation));
          if(relation.basis==="derived_summary")section.append(scienceText("small","展示归纳：未登记为研究依赖，不会自动赠送能力。"));
          host.append(section);
        }
        host.append(scienceText("h3","衍生发明与实际应用"));
        for(const invention of node.inventions) {
          const box=document.createElement("details"),head=scienceText("summary",invention.name);box.className="science-invention";box.append(head);
          box.append(scienceText("p",invention.principle),scienceText("p",`前提：${invention.requirements.join("、")}`),scienceText("p",`限制：${invention.limitations}`),scienceText("p",`用途：${invention.application}`));
          const affected=impacts.filter(i=>i.technology_id===invention.technology_id&&invention.effect_supported&&(!invention.invention_id||i.invention_id===invention.invention_id));
          if(!invention.effect_supported)box.append(scienceText("p",invention.status==="rejected"?"该发明的独立应用评审未通过。":"这是衍生设计方案，尚无单独应用能力契约。"));
          else if(!affected.length)box.append(scienceText("p","尚无已证实的实际影响。"));
          for(const impact of affected) {
            const before=JSON.parse(impact.before_json),after=JSON.parse(impact.after_json);
            box.append(scienceText("p",`${impact.actor_name}：${Object.entries(before).map(([key,value])=>`${key} ${value} → ${after[key]}`).join("；")} · ${scienceImpactStatus[impact.source_status]||"状态未知"}`));
          }
          const button=scienceText("button","查看独立评审与原始记录","button secondary");button.type="button";
          button.addEventListener("click",async()=>{
            try {
              const process=await scienceGet(`${base}/technologies/${encodeURIComponent(invention.technology_id)}/process`);
              if(selected!==detailGeneration)return;
              document.getElementById("science-process").textContent=JSON.stringify(process,null,2);
              const evidence=box.querySelector(".science-review")||scienceText("div","","science-review");evidence.replaceChildren();
              evidence.append(scienceText("h4","独立原理评审"),scienceText("p",invention.review?.reasoning||process.review?.reasoning||"完整过程见原始记录。"));
              for(const product of process.prototypes||[]) {
                if(product.invention_id===invention.invention_id) evidence.append(scienceText("p",`已制成原型 · 完好度 ${product.condition} · 材料强度指数 ${product.strength} · 加工精度指数 ${product.precision}（指数基准为1）`));
              }
              const last=process.application_attempts?.filter(a=>!invention.invention_id||a.invention_id===invention.invention_id).at(-1);
              if(last) {const result=JSON.parse(last.result_json);evidence.append(scienceText("p",`最近应用：${scienceAttemptLabels[result.status]||result.status}${result.reason?" · "+result.reason:""}`));}
              box.append(evidence);
            }catch(e){if(selected===detailGeneration)document.getElementById("science-process").textContent=e.message;}
          });
          box.append(button);host.append(box);
        }
        host.append(scienceText("p",node.limitations||"适用限制见各项发明。"));
        document.getElementById("science-process-title").scrollIntoView({behavior:"smooth",block:"start"});
      }
      scienceRenderGraph(document.getElementById("science-graph"),visibleTree,open);
      const cards=document.getElementById("science-node-cards");cards.replaceChildren();
      const levels=scienceRanks(tree);
      for(const node of [...visibleNodes].sort((a,b)=>levels.get(a.id)-levels.get(b.id))) {
        const button=scienceText("button",`${node.name} · ${node.inventions.length} 项发明 · ${scienceDomainLabels[node.domain||"physical"]}`,"science-technology-card");button.type="button";button.addEventListener("click",()=>open(node));cards.append(button);
      }
      if(!visibleNodes.length)cards.append(scienceText("p","该领域尚无已研发技术，等待真实研究与独立评审。"));
      const projectCards=document.getElementById("science-projects");projectCards.replaceChildren();
      const visibleProjects=projects.filter(p=>selectedDomain==="all"||p.domain===selectedDomain);
      for(const project of visibleProjects) {
        const stage=project.cycle.current_stage;
        const empiricalCaption=stage?`${stage.sample_name} · ${scienceOperationLabels[stage.operation]||stage.operation} · ${stage.status==="queued"?"待开工":"待结算"}`:"实物试验，待实证验收";
        const caption=project.cycle.kind==="empirical"?empiricalCaption:`${project.cycle.progress}%`;
        const card=scienceText("button",`${project.name} · ${scienceCycleStatus[project.cycle.status]} · ${caption}`,"science-technology-card science-project-card");card.type="button";
        card.addEventListener("click",()=>{
          ++detailGeneration;
          const host=document.getElementById("science-detail");host.replaceChildren();
          document.getElementById("science-process-title").textContent=`${project.name} · 研发中`;
          host.append(scienceText("p",`研究领域：${scienceDomainLabels[project.domain]} · 尚未解锁科技`));
          scienceRenderCycle(host,project.cycle,project.name);
          host.append(scienceText("h3",project.cycle.kind==="empirical"?"本次试验目的与未知点":"拟研究原理"),scienceText("p",project.principle),scienceText("p",`前提：${project.requirements.join("、")}`),scienceText("p",`限制：${project.limitations}`));
          document.getElementById("science-process").textContent=project.cycle.kind==="empirical"?JSON.stringify(project,null,2):"研究周期尚未完成，没有科技达成记录。";
          document.getElementById("science-process-title").scrollIntoView({behavior:"smooth",block:"start"});
        });
        projectCards.append(card);
      }
      if(!visibleProjects.length)projectCards.append(scienceText("p","该领域暂无进行中的研发项目。"));
      document.getElementById("science-process-title").textContent="研发详情";
      document.getElementById("science-detail").textContent="选择原理节点或技术卡片，展开查看衍生发明、技术关系与实际影响。";
      document.getElementById("science-process").textContent="尚未选择原始记录。";
      const list=document.getElementById("science-candidates");list.replaceChildren();
      const visibleCandidates=candidates.filter(candidate=>!projects.some(p=>p.id===candidate.id));
      for(const candidate of visibleCandidates) {
        let reason=candidate.gaps_json;try{reason=JSON.parse(reason)?.reasoning||reason;}catch(_){/* 哥哥，未评审的文字直接展示。 */}
        list.append(scienceText("p",`${candidate.name} · ${scienceStatus[candidate.status]||candidate.status} · ${reason}`));
      }
      if(!visibleCandidates.length)list.textContent="尚无候选";
      const impactList=document.getElementById("science-impacts");impactList.replaceChildren();
      for(const impact of impacts) {
        const row=scienceText("article","","science-impact"),before=JSON.parse(impact.before_json),after=JSON.parse(impact.after_json);
        row.append(scienceText("strong",`${impact.actor_name} · ${impact.technology_name}`),scienceText("p",`${Object.entries(before).map(([key,value])=>`${key} ${value} → ${after[key]}`).join("；")} · ${scienceImpactStatus[impact.source_status]||"状态尚未确认"}`));impactList.append(row);
        if(impact.notice)row.append(scienceText("p",impact.notice));
      }
      if(!impacts.length)impactList.textContent="尚无实际应用带来的变化。";
      setScienceState("ready");
    }catch(e){if(version===generation)setScienceState("error",`读取科研记录失败：${e.message}。请点击“刷新”重试。`);}
    finally{if(version===generation)refreshButton.disabled=false;}
  }
  async function loadScienceWorlds() {
    refreshButton.disabled=true;
    setScienceState("loading","正在读取世界列表…");
    try {
      const worlds=await scienceGet("/api/worlds");
      scienceWorld.replaceChildren();
      for(const world of worlds){const option=scienceText("option",world.name);option.value=world.id;scienceWorld.append(option);}
      const requested=new URLSearchParams(location.search).get("world");if([...scienceWorld.options].some(o=>o.value===requested))scienceWorld.value=requested;
      await refreshScience();
    }catch(e){setScienceState("error",`读取世界列表失败：${e.message}。请点击“刷新”重试。`);refreshButton.disabled=false;}
  }
  loadScienceWorlds();
  document.getElementById("science-domain").addEventListener("change",refreshScience);
  scienceWorld.addEventListener("change",refreshScience);refreshButton.addEventListener("click",()=>scienceWorld.options.length?refreshScience():loadScienceWorlds());
}
