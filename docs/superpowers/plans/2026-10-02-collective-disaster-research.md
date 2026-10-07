# 多人自主灾难研究 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现灰雨下多人自主提出、检验、修订和执行应对方案，并用冻结模拟及反事实证明全部建模区域恢复。

**Architecture:** 批次材料规则只计算自然作用；研究、沟通和协作执行只解释 NPC 提交的动作。所有动作连接实际库存、人物活动、权限和世界时间；地区恢复从消费、种植和废物账计算。

**Tech Stack:** Python 3.11+、SQLite、Pydantic、pytest，沿用当前虚拟环境及模型后端，不新增付费服务。

**Spec:** `docs/superpowers/specs/2026-10-02-collective-disaster-research-design.md`

## Global Constraints

- 运行前允许补齐通用能力；正式运行后禁止助手干预。
- 全部 21 个建模区域连续 14 个世界日安全供给不低于其实际消费需求，且结束时保有至少 14 日安全食物。
- 每区至少完成一个登记为 90 世界日的代表性粮作生长周期。
- 最多推进 240 世界日。
- 后续授权与已修订规范采用 6,000 次调用、150,000,000 tokens、单次输出（含推理）最多 131,072 tokens、请求超时 600 秒；每轮运行前冻结。旧 120 次/1,500,000 仅为初版计划额度，旧 32,768/180 秒已在第三轮截断后修正。
- 正式推进每个引擎心跳最多一个世界小时，并在更早的事件边界分割。
- 不注入最终配方、研究路线、预先分工；不自动采纳同行提案。
- 保留原工作区所有既有改动；在隔离工作区复制当前源码与非敏感数据作为依赖基线。

## Review Focus

1. 重启补粮或常规采集可能产生未追踪粮食：启用模式后仍须绑定真实生产来源。
2. 仓储、任务失败和副产物可能丢失污染：每条库存变更需要同事务的材料去向。
3. 已发送而未送达的同行报告不得改变接收方知识。
4. 新版任务不能沿用对旧参数的许可；取消与抢占工具不得重复扣料。
5. 大时间步不能先消费再倒填区间中途完成的净化；对照细步检查累计暴露。

## 执行记录

哥哥于 2026-10-02 回复“可以，继续完成直到达成目标”，批准规范并授权持续实施。采用本会话自行执行，末尾独立审查。依据这项持续执行授权，计划落盘后直接推进，不另设逐任务确认。技能里额外的计划交接确认以哥哥的明确持续执行指令为准。

隔离基线包含此前未提交代码；它不是此次新实现。测试日志、基线清单、每步结果和偏离裁定保存在 `run/collective-development/`，最终交付需将本次代码增量同步回原工作区并再次验证，不把隔离分支留作唯一实现。

### Task 1: 守恒的材料物理

**Files:** Create `world_engine/material_physics.py`; Test `tests/test_material_physics.py`。

**Interfaces:** `MaterialState`（质量、颗粒、mobile/adsorbed/bound 盐、水、活力、营养、酸碱度）；`process(state, operation, settings, hours, auxiliaries=()) -> list[MaterialState]`；`advance(state, hours) -> MaterialState`；`salt_total(state) -> float`。返回所有主体与副产物，不读取人物、国别或方案编号。

- [ ] 写失败测试，验证浸出后原料与污水的总盐保持不变、加热损伤种子、储存返溶的延迟、过滤不能删除溶解盐、处理顺序和规模改变结果。

```python
def test_leaching_accounts_for_salt_in_effluent():
    from world_engine.material_physics import MaterialState, process, salt_total
    original = MaterialState(mass=10, mobile=2, water=0)
    outputs = process(original, 'wash', {'water': 20}, hours=2)
    assert len(outputs) >= 2
    assert abs(sum(salt_total(x) for x in outputs) - 2) < 1e-9
    assert outputs[0].mobile < 2
    assert outputs[1].mobile > 0
```

- [ ] 运行 `.venv\Scripts\python.exe -m pytest tests/test_material_physics.py`，预期缺少能力导致失败。
- [ ] 实现有限速率、容量、热传递及所有输出验证；禁止识别救灾步骤或固定正确组合。
- [ ] 重跑测试，预期全部通过；追加非有限数、负数、输入不变性与物质边界测试。
- [ ] 仅提交本任务文件，记录具体测试与物理简化。

### Task 2: 批次状态、迁移与物质账

**Files:** Create `world_engine/material_schema.py`, `world_engine/materials.py`; Modify `world_engine/database.py`; Test `tests/test_materials.py`。

**Interfaces:** `MaterialService.attach(c, item_id, state)`、`state(c, item_id)`、`split(c, item, quantity, new_id)`、`merge(c, source, target, quantity)`、`consume(c, item, quantity, destination)`、`audit(c, world_id)`；`install_material_schema(c)` 幂等。

- [ ] 先测真实 SQLite 库中的拆分、合并、删除、消耗流入隔离汇和重新初始化；以固定已知总盐断言，禁止用被测函数生成期望值。

```python
def test_material_split_preserves_total_mass(database):
    from world_engine.materials import MaterialService
    assert hasattr(MaterialService, 'split')
```

完整夹具创建实际 world/item，设置 10 份与 2 单位污染，拆出 3 份应为 0.6，原堆为 1.4。真实动作夹具由本任务测试定义。

- [ ] 运行本文件验证红色；实现关联表、不可变流记录、事务内守恒与模式登记。
- [ ] 重跑验证绿色，检查未启用模式的旧存档不增加供给或材料状态。
- [ ] 提交新增文件；已有 database.py 改动基线单独保存，提交/同步仅包括此次补丁。

### Task 3: 库存、仓储和制作连接

**Files:** Modify `world_engine/inventory.py`, `interiors.py`, `activity_tasks.py`, `food.py`, `economy.py`, `food_supply.py`; Test `tests/test_material_inventory.py`。

**Interfaces:** 现有 `InventoryService.transfer/consume` 在追踪物品上调用 Task 2；新增去向参数保留旧调用兼容。托管仅转移，不消费。制作需要明确物质输出映射；未提供映射时拒绝使用追踪原料。

- [ ] 写实际转移、仓储拆合、托管取消、失败损耗、生产产物污染与采集容量失败回滚测试。

```python
def test_tracked_food_transfer_keeps_contamination(database):
    from world_engine.materials import MaterialService
    assert callable(MaterialService.merge)
```

测试通过实际背包和 fixture_storage 动作验证 10 份/2 单位污染转移 3 份后，两端分别为 7/1.4 与 3/0.6；合并不清除状态。

- [ ] 红色测试后实现共享入口；查询所有直接修改 item_instances 的生产路径并封闭绕行。
- [ ] 测食物摄入进入实际人物暴露账，旧过期机制不混同污染。
- [ ] 测新模式重启、经济再生和采集不会产生未追踪保底食物；旧供给行为继续通过。
- [ ] 运行上述测试及 `test_food_freshness.py`, `test_food_supply.py`, `test_production.py`, `test_interiors.py`；提交增量并登记。

### Task 4: 假说、实验与可执行契约

**Files:** Create `world_engine/collective_types.py`, `collective_schema.py`, `collective_research.py`; Test `tests/test_collective_research.py`。

**Interfaces:** `CollectiveProgram(reason, hypotheses, commands)`；命令动作涵盖 inspect、experiment、propose、accept、revise、send、produce、plant、observe、wait。`CollectiveResearchService.context(c, actor_id, at)`、`submit(c, actor_id, program, at, call_id=None)`、`tick(c, world_id, at)`。

- [ ] 写预测必须先于结果、未知批次拒绝、技能/工具/实际地点约束、检测限和远方知识隔离的失败测试。

```python
def test_unknown_remote_batch_is_not_available(database):
    from world_engine.collective_research import CollectiveResearchService
    assert callable(CollectiveResearchService.context)
```

- [ ] 实现不可变计划和实验前状态、对照及实际采样；开始时通过 LifeActivityService 占用人物，完成后才创建实测证据。
- [ ] 延迟观察仅在到期且实际取样完成后产生；取消保留已发生的副作用与未耗材料。
- [ ] 全量运行此任务测试并提交，记录检测工具的能力边界。

### Task 5: 多人版本、同行质疑与真实共同劳动

**Files:** Create `world_engine/collective_cooperation.py`, `collective_messages.py`; Modify Task 4 调度；Test `tests/test_collective_cooperation.py`。

**Interfaces:** `CooperativeService.propose/revise/accept/start(c, actor_id, spec, at)`；`MessageService.send/deliver(c, ..., at)`。提案版本引用已有批次、具体任务、参与者与资源；各人的 accept 来自自己的提交。

- [ ] 测未送达信件不进入知识、新版本令受影响接受失效、他人拒绝后不强行开工、工具不能重复预留。

```python
def test_cooperation_requires_each_actual_worker_acceptance(database):
    from world_engine.collective_cooperation import CooperativeService
    assert callable(CooperativeService.start)
```

- [ ] 实现通信延迟、真实货物、参与者活动、设备劳动需求与完整来源。研究贡献保留作者和版本变化，不靠职业人数门槛改物理。
- [ ] 测小批一人允许、大批设备互斥依实际劳动决定；中断后不重复扣料或生成结果。
- [ ] 运行本任务及 Task 4 测试，提交并登记。

### Task 6: 种植、暴露、废物与地区恢复

**Files:** Create `world_engine/collective_regions.py`, `collective_production.py`; Test `tests/test_collective_regions.py`。

**Interfaces:** `RegionService.tick/view(c, world_id, at)`；`ProductionService.start(c, actor_id, command, at)`；收成、消费和废物都引用 Task 2 批次。地区需求排除已单独建模的人物。

- [ ] 先测真实 90 日周期前无粮食产出、死亡不清空需求账、连续安全天数可被新暴露中断、废液污染下游与维护不落实不能 resolved。

```python
def test_unharvested_crop_cannot_feed_region(database):
    from world_engine.collective_regions import RegionService
    assert callable(RegionService.view)
```

- [ ] 实现时间积分的根系吸收、产量、种子留存及营养需求；消费先验证真实库存，物质流入人体隔离汇。
- [ ] 安全饮食规则只读该人物已知的检测/警告；隐藏状态仅供自然结算。
- [ ] 测全部21区域、14日稳定和下轮储粮；重跑 Task 1–3 测试，提交。

### Task 7: 模型调度与记录

**Files:** Create `world_engine/collective_runner.py`; Modify `engine.py`, `config.py`; Test `tests/test_collective_runner.py`。

**Interfaces:** `CollectiveRunner(database, backend, limits).decide(world_id, at)`、`usage(world_id)`；实际 JSON Schema 来自 Task 4，后端复用 `complete(system_prompt, user_payload, schema, label, max_output_tokens)`。

- [ ] 测多专业公平调用、在途信件不堵其他问题、普通饥饿变化不重复请求、截断/空输出计入失败用量并停止。

```python
def test_runner_preserves_failed_call_usage(database):
    from world_engine.collective_runner import CollectiveRunner
    assert callable(CollectiveRunner.usage)
```

- [ ] 实现每次上下文只包含本人已知事实；独立提交同行判断，不向模型发送物理公式或推荐流程。
- [ ] 测实际程序执行拒绝可成为后续证据、技术错误不被自动修成解法；运行旧模型边界测试后提交。

### Task 8: 时钟边界与加速

**Files:** Create `world_engine/collective_clock.py`; Modify `clock.py`; Test `tests/test_collective_clock.py`。

**Interfaces:** `next_boundary(c, world_id, at, limit_hours=1)`；通过 WorldEngine heartbeat 逐边界结算，模式未启用沿用旧时钟。

- [ ] 测细步与加速的摄入、任务完成、警告到达、库存与随机检定一致。

```python
def test_next_boundary_never_skips_due_observation(database):
    from world_engine.collective_clock import next_boundary
    assert callable(next_boundary)
```

- [ ] 实现中途事件拆分及冻结控制记录；接口检查重复心跳不重复结算。
- [ ] 运行时钟与生活回归，提交。

### Task 9: 灾前世界、一次注入与冻结演示

**Files:** Create `scripts/demo_collective_disaster.py`; Test `tests/test_collective_scenario.py`。

**Interfaces:** `build_baseline(db)`、`inject_initial_disaster(db, world_id, at)`、`source_manifest()`、`export_report(...)`；独立目录保存 baseline-world.db/world.db/freeze.json。

- [ ] 测全部目录政体、无救灾程序/配方/指定研究者、源负荷有限、NPC 只获得本地目击、重启不补粮。

```python
def test_scenario_covers_current_polities():
    from scripts.demo_collective_disaster import build_baseline
    assert callable(build_baseline)
```

- [ ] 初始化职业、库存和工具采用正常手工生活用途与实际权限；清洁储粮缓冲的期限由需求计算。
- [ ] 实现 --init-only/--offline/正式运行，冻结代码、数据、物理、需求、策略边界与API额度。
- [ ] 跑离线无模型240日对照与技术错误停止检查，确认没有自动解灾后提交。

### Task 10: 因果审计与全目标验证

**Files:** Create `world_engine/collective_audit.py`; Test `tests/test_collective_audit.py`; Update 报告脚本。

**Interfaces:** `audit_run(db_path, baseline_path, manifest) -> dict`；独立副本撤回组件、计算带全部知识/最终方案的单人能力与劳动下界；不调用 API，不修改正式DB。

- [ ] 测虚构预测后补、缺失物質去向、全员照搬一个方案、把签名当贡献均不能通过；合理的单人可达下界不能被标为多人必要。

```python
def test_audit_rejects_signature_only_cooperation():
    from world_engine.collective_audit import audit_run
    assert callable(audit_run)
```

- [ ] 建立规范逐项证据表，核对三位实质研究者、21区域、90日收获、14日连续供给、废物/身体/种子与单人反事实。
- [ ] 运行本任务、所有新增测试及项目全套测试；真实模型验收与离线能力验收分开。
- [ ] 使用 requesting-code-review 技能执行一次独立整体审查；修复重要问题后重新相关验证。
- [ ] 同步本次增量到原工作区，检查原有修改保留、复核测试与冻结清单。
- [ ] 正式调用API运行；只读跟踪、记录调用用量和停止原因。结束后执行审计、生成 report.md/report.json/audit.json/verification.md。
- [ ] 只有规范全部证据真实通过才 update_goal complete；失败保留存档，修通用能力后作为新冻结轮次重试，不改正式运行的历史。
