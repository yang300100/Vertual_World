# NPC 自主科研与生长科技树 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让现有 NPC 自主提出问题、完成真实研究、认证并实际复用新能力，产生未预设的科技依赖图，将每次突破的完整过程写入世界编年。

**Architecture:** 科研程序复用现有材料账、劳动、量器、证据和通信机制；科技认证只根据提前登记的主张及真实结果结算。新增持久科研会话统一调度、预算和冻结检查，接入日常心跳；测试和真实运行严格分开。

**Tech Stack:** Python 3.11+、SQLite、Pydantic、FastAPI、现有模型后端、原生 JavaScript/SVG、pytest/httpx 假传输；不增加产品依赖。

**Spec:** `docs/superpowers/specs/2026-10-05-emergent-science-design.md`，哥哥于2026-10-05确认；源码审查补充见 `2026-10-05-emergent-science-interface-audit.md`。

## Global Constraints

- 首轮最多12次、累计200000 token；输入、输出与提供方计入的推理用量统一口径，失败、拒绝、截断同样计账，未知用量以预留计入保护额度。
- 单次输出上限8192 token；技术自动重试为0；超预算不得发送请求，不靠开新会话清账。
- 不调用生图，不覆盖 `.env`，不重启旧灾难，不修改浮空城验收存档，不整体提交现有脏工作树。
- 保留原有32位 NPC 的身份、目标、技能、关系和记忆；正式存档通过 SQLite backup 创建一致性副本，迁移只作用于副本。
- 不预置科技名称、路线、节点、依赖边或分工；测试用配方和虚拟人物不得进入真实初始状态或模型输入。
- 通用能力、自然响应和调度在运行前冻结；运行后助手只读观察和导出，不改程序、角色、库存、结果、标准或预算。
- 作者层世界规律与角色已知资料隔离；未支持的提案保留未验证，不能用模型叙述替代实测。
- 科技认证至少两次真实执行、至少两个不同真实样品；需要比较时有真实基线/对照，差异超过量器分辨率；只有真实复用产生依赖边。
- 真实验收至少3项非预置新能力、至少1条实际复用依赖，并有实际使用/继续研究；门槛只给观察者，不给 NPC。
- Windows 命令使用 PowerShell；测试解释器为 `.venv\Scripts\python.exe`。所有离线测试禁止外网访问。

## Review Focus

1. 两个进程同时请求或请求中崩溃：预留必须原子持久化；未知用量不得在重启后归零。任务8覆盖。
2. 操作者消耗/混合/交付过样品后，别人引用旧样品编号：证据不丢，执行只能绑定真实可用物资。任务3、6覆盖。
3. 回收材料和供能组合形成能量循环：损失与供能来源必须真实记账；不能通过调用已认证方法绕过守恒。任务2、6覆盖。
4. 收到旧版本或失效方法、伪造跨世界编号：只能取得实际送达的版本知识，失效方法不得作为已认证父项。任务4、6覆盖。
5. NPC 名称、科技名或报告含 HTML/脚本，世界无科技、候选仍未验证：界面安全显示原文及正确状态，不暗示突破。任务9覆盖。

## 文件职责与执行顺序

新增文件：

| 文件 | 职责 |
|---|---|
| `world_engine/science_schema.py` | 会话、调用账、科技、方法版本、依赖、知识、执行和证据关系的迁移 |
| `world_engine/science_types.py` | 严格科研动作、主张、可复用方法与有限引用契约 |
| `world_engine/craft_physics.py` | 通用工艺响应与测量，不读角色或科技名称 |
| `world_engine/science_workshop.py` | 按现有地点建立有限基础工坊条件，不提供研究计划 |
| `world_engine/science_programs.py` | 科研动作与既有工作/实验程序的适配及结算 |
| `world_engine/science_knowledge.py` | 本人实际已知证据与方法、真实传播 |
| `world_engine/technology_evidence.py` | 主张冻结、验证结果与量器比较 |
| `world_engine/technologies.py` | 认证、查重、版本与动态依赖图 |
| `world_engine/technology_methods.py` | 方法实例化、真实复用、产物来源与防环 |
| `world_engine/science_chronicle.py` | 突破、修订、失效编年与可展开过程 |
| `world_engine/science_runner.py` | 持久预算、事件调度、真实提示词与无重试调用 |
| `world_engine/science_freeze.py` | 运行前指纹、运行中变更检测与恢复 |
| `world_engine/science_exports.py` | 只读全过程与费用导出 |
| `world_engine/api_science.py` | 观察者查询接口，不能给 NPC 灌入全局知识 |
| `world_engine/web/science.js` | 现有页面中的科技图、候选和过程 |
| `scripts/demo_science.py` | 一致性副本、正常工坊准备、冻结和自主运行 |
| `tests/science_fixtures.py` | 离线实物/工具/程序辅助，不被产品模块导入 |
| `tests/test_science_*.py` | 各任务的离线验证 |
| `docs/design/20-emergent-science.md` | 实际实现接口、运行与局限说明 |

修改入口限于 `database.py`、`material_physics.py`、`materials.py`、`collective_research.py`、`collective_work.py`、`collective_messages.py`、`config.py`、`engine.py`、`clock.py`、`event_log.py`、`event_history.py`、`api.py`、NPC上下文实际装配入口、`web/index.html`、`web/app.js`、`web/styles.css`、`.env.example`。不重写原有模块；先在这些文件已有入口加小适配。

执行前保存本任务涉及文件的原始哈希和 diff，核对现有未提交依赖；按 `using-git-worktrees` 技能决定隔离方式。若创建代码工作树，必须包含当前实际代码和本任务依赖的未提交模块，不能从远端旧代码开始并遗漏它们。后文每次“保存任务改动”只指本任务增量；现有共享文件中的其他改动不得整文件一起提交。真实运行始终使用独立数据库。

## Task 1：持久会话、科技数据与严格契约

**Files:** 新建 `science_schema.py`、`science_types.py`、`tests/test_science_schema.py`；修改 `database.py:966` 附近的迁移装配。

**Interfaces:**

```python
install_science_schema(c) -> None
create_session(c, world_id: str, at, *, max_calls=12,
               token_limit=200000, max_output_tokens=8192) -> str
ScienceProgram.model_validate(value: dict) -> ScienceProgram
ScienceProgram.model_dump() -> dict
```

契约：`ScienceProgram(reason, hypotheses, commands)`；复用现有 `Hypothesis`。命令保留现有研究动作，再增加 `declare`、`revise_claim`、`validate`、`use_method`、`share_method`。动作独立 Pydantic 类型组成 discriminated union，不允许任意 Python/SQL。方法为 `MethodSpec(inputs, parameters, steps)`：输入角色绑定实物，参数有单位与有限范围；最多24步、方法调用深度4、展开后96步。主张为 `ClaimSpec(metric, direction, minimum_delta, conditions, comparator_role)`，`direction` 为 increase/decrease；观察/解释另列，未能执行不登记为工艺科技。

- [ ] **1. 写失败测试，禁止用愿望取得科技，禁止非有限参数。**

```python
def test_program_rejects_unexecutable_declaration():
    import pytest
    from world_engine.science_types import ScienceProgram
    with pytest.raises(ValueError):
        ScienceProgram.model_validate({"reason": "试验", "commands": [
            {"op": "declare", "name": "只写愿望", "method": "无限供能"}
        ]})
```

- [ ] **2. 运行** `.venv\Scripts\python.exe -m pytest tests/test_science_schema.py -q`，确认缺少新模块/契约导致失败。
- [ ] **3. 实现迁移与契约。** 建立设计中8张科技/会话表，并增加 `science_model_calls`、`science_claim_trials`、`science_execution_sources`。所有记录带 world/session、来源、世界时间；外键限定实际实体，跨世界在服务入口额外验证。

```sql
CREATE UNIQUE INDEX IF NOT EXISTS science_one_live_session
ON science_sessions(world_id) WHERE status IN ('prepared','running','stopping');
CREATE UNIQUE INDEX IF NOT EXISTS science_call_once
ON science_model_calls(session_id, actor_id, fingerprint);
CREATE UNIQUE INDEX IF NOT EXISTS technology_method_identity
ON technology_versions(world_id, method_fingerprint, claim_fingerprint);
```

会话状态为 prepared/running/stopping/stopped；预算字段初始化12/200000/8192，所有计账字段非负。`science_schema.create_session` 创建 prepared 会话，不初始化工具、选人或调用模型；有未结束会话时拒绝另开。调用状态 reserved/succeeded/rejected/failed/unknown，保存真实响应与诊断，密钥不得入库。科技状态 verified/invalidated；候选状态 proposed/needs_evidence/verified/unsupported/duplicate。
- [ ] **4. 补充并通过迁移重入、旧存档无付费会话、方法输入类型、有限参数、超深展开、跨世界外键测试。** 在测试库连续运行迁移两次，表内容与既有角色不变。
- [ ] **5. 保存该任务增量。**

## Task 2：通用材料响应、量器与有限工坊

**Files:** 新建 `craft_physics.py`、`science_workshop.py`、`tests/test_science_physics.py`；修改 `material_physics.py`、`materials.py`、`collective_work.py` 的处理/测量入口。

**Interfaces:**

```python
process_craft(state, operation: str, settings: dict, auxiliaries: tuple,
              hours: float, supplied_energy: float, profile: dict) -> tuple
assay_craft(state, metric: str, instrument: dict, profile: dict) -> dict
seed_workshop(c, world_id: str, at, *, seed: int) -> dict
```

返回的处理元组为真实产物状态；测量字典为 value/unit/precision/detection_limit，隐藏 profile 不进报告。`seed_workshop` 返回地点、实物、工具及开放事件 ID，不返回任何研究问题或科技节点。
- [ ] **1. 写失败测试，材料变化受供能限制且质量守恒。**

```python
def test_heating_does_not_create_energy_or_matter():
    from world_engine.material_physics import MaterialState
    from world_engine.craft_physics import process_craft
    import pytest
    raw = MaterialState(carrier="clay", mass=2, water=0.3)
    with pytest.raises(ValueError, match="供能"):
        process_craft(raw, "heat", {"temperature": 900}, (),
                      hours=2, supplied_energy=0, profile={})
```

- [ ] **2. 运行** `.venv\Scripts\python.exe -m pytest tests/test_science_physics.py -q`，确认失败来自缺少功能。
- [ ] **3. 实现材料扩展和固定响应。** 原 `MaterialState` 新字段须有默认值，旧 JSON 可直接读取；增加孔隙、结构结合、形变、游离/固着颜料及几何比，颜料属于 extensive 守恒量。`fraction` 按比例拆 extensive，`combine` 用有物理意义的权重；不可把强度相加。工艺温度可扩展至2000，但既有污染处理仍使用其原操作范围。

```python
def conservative_fraction(total: float, fraction: float) -> tuple[float, float]:
    if not 0 <= fraction <= 1:
        raise ValueError("分配比例超出范围")
    part = total * fraction
    return part, total - part
```

固定规律覆盖成形、热处理/冷却、压实、混合、洗涤、吸附和固着，测量覆盖强度、渗漏、孔隙、颜色保持、形变及现有污染指标。热容量/相变/耗散均记录来源；只返回能够实际测量的量。基线种子和材料类型决定自然系数，响应不读 actor、科技名或目标。共振器仍使用既有固定响应、完整单纹与来源能量；不预造新魔纹效果。
- [ ] **4. 建立有限基础工坊。** 沿现有地点和角色活动范围准备常见材料、手工工具与量器，保留物权；仅公布工具开放事实。未具备技能的角色不能因初始化被提升。给所有角色相同的选择机会，不按期待成果挑人。
- [ ] **5. 通过新测试及** `.venv\Scripts\python.exe -m pytest tests/test_material_physics.py tests/test_materials.py tests/test_material_inventory.py tests/test_science_physics.py -q`。覆盖拆合守恒、热损失、回收循环、颜料账、相同条件重复结果、不同样品差异、低精度无法证明改进；未知操作返回 unsupported。
- [ ] **6. 保存该任务增量。**

## Task 3：NPC 科研程序与真实执行轨迹

**Files:** 新建 `science_programs.py`、`tests/science_fixtures.py`、`tests/test_science_programs.py`；修改 `collective_research.py`、`collective_work.py` 的窄适配入口。

**Interfaces:**

```python
ScienceService.submit(c, session_id: str, actor_id: str, program: dict, at) -> str
ScienceService.tick(c, world_id: str, at, *, heartbeat_id: str | None = None) -> None
ScienceService.context(c, actor_id: str, at) -> dict
```

程序返回 ID；每条命令有 pending/running/completed/rejected/unsupported 状态，保存实际试验、活动、观察、产物及来源 ID。基础实验复用 `CollectiveResearchService.submit` 和真实劳动；不创建灾害或给世界设置 collective 灾害模式。共振器能力通过 `ResearchService.accept_program` 与原工时路径适配；消除科研调度的“必须处于灾害”前置条件，不绕过操作能力。
- [ ] **1. 写失败测试，当前实物与时间决定执行。** `science_fixtures.py` 建立以下共用 fixture（测试内导入，不进产品）：

```python
import pytest
from test_collective_research import lab

@pytest.fixture
def science_lab(lab):
    from world_engine.science_workshop import seed_workshop
    from world_engine.science_schema import create_session
    db, wid, actors, batches, tool, at = lab
    with db.write() as c:
        assets = seed_workshop(c, wid, at, seed=7)
        sid = create_session(c, wid, at, max_calls=12,
                             token_limit=200000, max_output_tokens=8192)
    return db, wid, sid, actors, assets, at
```

`create_session` 已由任务1实现；工坊由任务2准备，程序测试不依赖尚未实现的模型调度器。

```python
def test_text_alone_does_not_complete_experiment(science_lab):
    from world_engine.science_programs import ScienceService
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        pid = ScienceService.submit(c, sid, actors[0],
            {"reason": "只讨论，没有执行", "hypotheses": [], "commands": []}, at)
        assert c.execute("SELECT COUNT(*) FROM technologies WHERE world_id=?",
                         (wid,)).fetchone()[0] == 0
        assert pid
```

- [ ] **2. 运行** `.venv\Scripts\python.exe -m pytest tests/test_science_programs.py -q`，确认失败。
- [ ] **3. 实现有限程序解释器。** 注册动作处理映射，按索引执行；异步命令必须等真实工作结束，再解析 `$result:index`。运行中失败保存原因，不自动补齐实物/参数；声明后验证不得倒填假说。多个角色争用材料、器具或同一个身体时用现有预留和事务拒绝冲突。

```python
def next_command(commands: list[dict], completed: int) -> dict | None:
    if completed < 0 or completed > len(commands):
        raise ValueError("程序位置不合法")
    return commands[completed] if completed < len(commands) else None
```

- [ ] **4. 补充真实样品已耗尽、材料远处/在途、本人正在工作、同器具冲突、未来产物、魔法供能不足、未知研究方向保留未验证测试。** 基线与研究不靠聊天结算。
- [ ] **5. 通过新测试及** `tests/test_collective_research.py`、`tests/test_collective_access.py`、`tests/test_autonomous_research.py`，保存增量。

## Task 4：本人知识、方法传播与提示词隔离

**Files:** 新建 `science_knowledge.py`、`tests/test_science_knowledge.py`；修改 `collective_messages.py` 的消息附件和送达入口、NPC实际上下文入口。

**Interfaces:**

```python
known_methods(c, actor_id: str, at) -> list[dict]
science_context(c, actor_id: str, at, *, max_chars: int) -> dict
share_method(c, sender_id: str, recipient_id: str, technology_id: str,
             version: int, at, *, evidence_ids: tuple = ()) -> str
receive_method(c, message_id: str, at) -> None
```

方法包是发送时具体版本与真实报告快照，靠消息送达登记知识；不能接收后按世界最新版本自动更新。本人亲历/研究也登记来源。报告内容引用已有 observation，不重新生成测量事实。
- [ ] **1. 写失败测试，消息未送达时接收者不知道方法。**

```python
def test_new_session_does_not_grant_global_knowledge(science_lab):
    from world_engine.science_knowledge import known_methods
    db, _, _, actors, _, at = science_lab
    with db.read() as c:
        assert known_methods(c, actors[1], at) == []
```

- [ ] **2. 运行** `.venv\Scripts\python.exe -m pytest tests/test_science_knowledge.py -q`，确认失败。
- [ ] **3. 实现本人上下文。** 检索显式 `audiences={"character_common"}`；guardrail 只约束行为，不能伪装成本人掌握的物理知识。输入为原角色身份/目标/技能、现场工具与本人库存、当前计划、已送达证据、本人已知方法和未决问题；不含隐藏系数、全局科技、其他人私有结果或观察者验收门槛。

```python
public_hits = knowledge.search(query, audiences={"character_common"},
                               limit=4, max_total_chars=5000)
```

预算裁剪先保留当前计划所需的方法/证据，再裁无关旧历史；每次裁剪记录诊断，禁止截断 JSON 契约中间字段。语义指纹忽略时间/改写措辞，包含新证据 ID、方法版本、库存/执行能力变化。
- [ ] **4. 通过未送达/未来/跨世界附件拒绝、原文证据一致、两次转发出处、旧版本与失效版本、长历史保留当前方法、作者公式/隐藏系数不进最终 HTTP 请求测试。** 沿用 `tests/test_collective_evidence.py` 和真实 Prompt 装配路径核查。
- [ ] **5. 保存该任务增量。**

## Task 5：预登记主张、科技认证和去重

**Files:** 新建 `technology_evidence.py`、`technologies.py`、`tests/test_science_certification.py`。

**Interfaces:**

```python
TechnologyService.declare(c, session_id: str, actor_id: str, body: dict, at) -> str
TechnologyService.revise(c, candidate_id: str, actor_id: str, body: dict, at) -> int
register_trial(c, candidate_id: str, revision: int, execution_id: str,
               control_execution_id: str, at) -> None
TechnologyService.certify(c, candidate_id: str, actor_id: str, at) -> dict
TechnologyService.list(c, world_id: str) -> list[dict]
```

`certify` 返回 status/technology_id/gaps，不帮人物生成下一实验；只有 verified 返回科技 ID。试验注册必须在执行开始前，主张修订产生新 revision，不能继续用旧验证直接匹配新阈值。
- [ ] **1. 写失败测试，伪造证据不能通过。**

```python
def test_candidate_rejects_foreign_or_missing_evidence(science_lab):
    import pytest
    from world_engine.technologies import TechnologyService
    db, _, sid, actors, _, at = science_lab
    with db.write() as c, pytest.raises(ValueError, match="证据"):
        TechnologyService.declare(c, sid, actors[0], {
            "name": "测试内名称", "problem": "检验一种性质",
            "claim": {"metric": "strength", "direction": "increase",
                      "minimum_delta": 1, "conditions": {}, "comparator_role": "control"},
            "method": {"inputs": {"sample": "material"}, "parameters": {},
                       "steps": [{"op": "store", "input": "sample", "hours": 1}]},
            "evidence_ids": ["不存在的原始证据"]}, at)
```

- [ ] **2. 运行** `.venv\Scripts\python.exe -m pytest tests/test_science_certification.py -q`，确认失败。
- [ ] **3. 实现判据。** 每项验证 join 本人可见实测、已完成 execution、预登记 trial、材料来源、量器与实际步骤。比较样品材料/来源/测量条件必须匹配；不能用不同量器刻度造成虚假提升。两份不同实物样品须有独立实际处理，不把同一实物重新编号当作复验。差异须超过主张阈值和测量不确定性。

```python
def proves_improvement(value, baseline, direction, delta, precision):
    signed = value - baseline if direction == "increase" else baseline - value
    return signed >= delta and signed > precision
```

查重指纹不包含名称/解释；保留有意义的步骤次序、参数、条件和能力主张。仅在明确可交换的独立步骤上规范化；不能把不同物理方法粗暴合并。重复已知基础动作不产节点；实际组合带来超出传统基线的新能力才可认证。
- [ ] **4. 通过文字/待完成/私有/未来证据、同一试验多次引用、倒填预测、更改阈值、低精度、无对照、真实两样品复验、名称不同方法相同去重、实体方法版本修订测试。** 测试 helper 必须通过 Task3 执行真实程序获得结果，不直接插入“成功”试验。
- [ ] **5. 保存该任务增量。**

## Task 6：可执行方法、真实依赖和失效传播

**Files:** 新建 `technology_methods.py`、`tests/test_science_methods.py`；修改 `science_programs.py` 的 use_method 入口、`technologies.py` 的依赖/版本管理。

**Interfaces:**

```python
instantiate_method(c, actor_id: str, technology_id: str, version: int,
                   bindings: dict, parameters: dict, at) -> list[dict]
record_execution_source(c, execution_id: str, source_execution_id: str,
                        item_id: str | None, at) -> None
TechnologyService.invalidate(c, technology_id: str, actor_id: str,
                             evidence_ids: tuple, reason: str, at) -> None
TechnologyService.graph(c, world_id: str) -> dict
```

图返回 nodes/edges，节点只来自认证，边只来自方法调用或实际消耗可追溯产物，不读 NPC 的 parents 叙述。
- [ ] **1. 写失败测试，无方法、无实物不得实例化。**

```python
def test_unknown_method_is_not_executable(science_lab):
    import pytest
    from world_engine.technology_methods import instantiate_method
    db, _, _, actors, _, at = science_lab
    with db.read() as c, pytest.raises(ValueError, match="方法|科技"):
        instantiate_method(c, actors[0], "未知科技", 1, {}, {}, at)
```

- [ ] **2. 运行** `.venv\Scripts\python.exe -m pytest tests/test_science_methods.py -q`，确认失败。
- [ ] **3. 实现有类型绑定和有限展开。** 绑定实际同世界、在场可用资源，参数在认证条件内，程序经 Task3 正常成本/权限检查执行。每次复用登记实例、版本、步骤、结果和物质账 ID。

```python
def would_cycle(edges: list[tuple[str, str]], parent: str, child: str) -> bool:
    frontier, seen = [child], set()
    while frontier:
        node = frontier.pop()
        if node == parent:
            return True
        if node in seen:
            continue
        seen.add(node)
        frontier.extend(b for a, b in edges if a == node)
    return False
```

失效须以真实反证追加历史，保留旧知识快照；旧方法可作为尚待研究的假说，但不作为 verified 父项/可靠能力。依赖方法适用域改变时子项标记待复核，不能默默删除历史或继续宣称有效。
- [ ] **4. 通过实际复用形成边、只提名称无边、混合/交付产物来源不丢、耗尽材料不可再用、跨世界/未知版本/失效方法拒绝、无环、多父项、过深调用、方法供能仍受守恒检查测试。**
- [ ] **5. 保存该任务增量。**

## Task 7：突破完整过程与世界编年原子登记

**Files:** 新建 `science_chronicle.py`、`tests/test_science_chronicle.py`；修改 `event_log.py:25`、`event_history.py:32`，在 Task5 认证事务内调用。

**Interfaces:**

```python
record_breakthrough(c, technology_id: str, candidate_id: str, at) -> str
technology_process(c, world_id: str, technology_id: str) -> dict
record_technology_revision(c, technology_id: str, kind: str, at) -> str
```

过程返回 problem/prior_knowledge/hypotheses/trials/failures/revisions/transfers/validation/conditions/dependencies，每项都带原始记录 ID 和时间；缺少事实显示缺少，不能由模板补故事。
- [ ] **1. 写失败测试，突破类型确实进入重大编年。**

```python
def test_breakthrough_is_major():
    from world_engine.event_log import importance_for
    assert importance_for("world.technology_breakthrough") == "major"
```

- [ ] **2. 运行** `.venv\Scripts\python.exe -m pytest tests/test_science_chronicle.py -q`，确认失败。
- [ ] **3. 实现突破、修订与失效事件。** 范围登记地方/科研影响，调用 `EventHistoryService.link` 连接真实试验、修订、交接和父方法。认证与事件同一事务，唯一来源键阻止重启重复突破；模板从原始记录构建，不调用总结模型。

```python
from world_engine.event_history import EventHistoryService
for cause_id in trial_event_ids:
    EventHistoryService.link(c, breakthrough_event_id, cause_id,
                             relation="research_evidence")
```

- [ ] **4. 通过 `scope=chronicle` 实际查询、同事务错误回滚、重复认证幂等、完整失败修订链、实际传播、父项出处、失效保留旧历史测试。** 同时运行 `tests/test_history.py`，避免破坏现有编年。
- [ ] **5. 保存该任务增量。**

## Task 8：公平按需调度、统一预算和安全恢复

**Files:** 新建 `science_runner.py`、`tests/test_science_runner.py`；修改 `config.py`、`.env.example` 的显式 science 开关（默认关闭）。

**Interfaces:**

```python
ScienceRunner(database, backend, *, session_id: str)
ScienceRunner.decide(world_id: str, at) -> bool
ScienceRunner.usage() -> dict
ScienceRunner.stop(reason: str, at) -> None
```

消费任务1的 `science_schema.create_session`；会话读取已持久化上限，构造器不能覆盖它。已停机恢复使用同 session 与账目。费用聚合为 calls/known_tokens/accounted_tokens/unknown_calls/reserved_tokens/stop_reason。调用预算包括首轮启动后的所有外部模型请求；旧灾害裁判、自动对话等不得绕过入口偷偷花费。
- [ ] **1. 写失败测试，无后端不会调用或扣费。**

```python
def test_no_backend_no_paid_calls(science_lab):
    from world_engine.science_runner import ScienceRunner
    db, wid, sid, _, _, at = science_lab
    runner = ScienceRunner(db, None, session_id=sid)
    assert runner.decide(wid, at) is False
    assert runner.usage()["calls"] == 0
```

- [ ] **2. 运行** `.venv\Scripts\python.exe -m pytest tests/test_science_runner.py -q`，确认失败。
- [ ] **3. 实现预算预留。** 在 `database.write()` 事务中检查累计已用和在途预留，再插入 reserved 调用；HTTP 在事务外执行。原子语义指纹/公平轮转防止同证据重复花费。输入采用保守 token 估算并给完整输出预留，预留不足拒绝发送。后端 max_retries 必须0；实际统计遵循提供方 output 是否已含 reasoning 的口径，记录原 usage 以便核查。

```python
def permitted(calls, accounted, reserve, max_calls, token_limit):
    return calls < max_calls and accounted + reserve <= token_limit
```

成功/解析拒绝/执行拒绝/网络失败均留原始响应诊断；无用量按预留保护。请求中崩溃转 unknown，不自动再次发送。HTTP失败停机；研究无行动不强迫突破。排序固定：新结果或收到新方法优先，再按上次调用时间轮转，无关空闲不反复调用；不在运行中人工挑人。
- [ ] **4. 通过两进程抢最后额度、12次上限、200000累计上限、失败/未知计账、reasoning重复计数防护、请求中崩溃恢复、重复指纹、等待培养不调用、实际新证据唤醒、无自动修响应、所有实际 HTTP 路径计账测试。** 用 `httpx.MockTransport`，不能请求真实地址。
- [ ] **5. 保存该任务增量。**

## Task 9：正常心跳、观察接口和科技页面

**Files:** 新建 `api_science.py`、`web/science.js`、`tests/test_science_runtime.py`、`tests/test_science_api.py`；修改 `engine.py:201`、`clock.py:208`、`api.py:339`、`web/index.html`、`web/app.js`、`web/styles.css`、NPC当前上下文装配入口。

**Interfaces:**

```text
GET /api/worlds/{world_id}/science
GET /api/worlds/{world_id}/technologies
GET /api/worlds/{world_id}/technologies/graph
GET /api/worlds/{world_id}/technologies/{technology_id}/process
GET /api/worlds/{world_id}/science/candidates
```

观察者路由只读；按 world_id 校验，未知编号404，不能暴露密钥或作者物理 profile。已知方法加入正常 NPC 提示词；全局图只供玩家观察。
- [ ] **1. 写失败测试，旧世界不会自动开始付费科研。**

```python
def test_old_world_has_no_science_session(database):
    from world_engine.repository import WorldRepository
    with database.write() as c:
        wid = WorldRepository().create_world(c, name="旧世界", minutes_per_tick=60,
                                            seed_demo=False, seed_maps=False)
        assert c.execute("SELECT COUNT(*) FROM science_sessions WHERE world_id=?",
                         (wid,)).fetchone()[0] == 0
```

- [ ] **2. 运行** `.venv\Scripts\python.exe -m pytest tests/test_science_runtime.py tests/test_science_api.py -q`，确认缺少接口/装配失败。
- [ ] **3. 接入时钟。** science 活跃模式完成真实工作、通信、材料推进，再按需调度；仍保留日常生活/经济/来访。避免既有 collective 与 research runner 再做同一科研判断；science 会话期间所有模型调用统一预算，不允许另一裁判后端并行出账。大跨度心跳按真实工作/消息边界推进，不能跳过验证前登记时间。
- [ ] **4. 接入 API 与页面。** 科技图用节点/边数据生成 SVG，候选单独展示；点击节点显示原始过程、真实条件与来源。显示预算与停止原因，不在页面提供“强制突破”按钮。

```javascript
const label = document.createElement("span");
label.textContent = technology.name;
```

所有人物/方法/报告文本采用 textContent 或现有 escapeHtml。空图明确“尚无已验证科技”；待验证与失效状态清晰显示。
- [ ] **5. 通过正常 heartbeat 驱动完整研究链、生活不停止、旧模式不重复调用、重复心跳幂等、跨世界路由404、作者资料不泄露、恶意 HTML 安全显示、空图/多父节点/失效节点测试。** 浏览器检查测试库页面，不启动付费服务。
- [ ] **6. 保存该任务增量。**

## Task 10：一致性副本、冻结、自主运行与全部过程导出

**Files:** 新建 `science_freeze.py`、`science_exports.py`、`scripts/demo_science.py`、`tests/test_science_freeze.py`、`docs/design/20-emergent-science.md`。

**Interfaces:**

```python
backup_world(source_path, destination_path) -> None
freeze_session(c, session_id: str, *, source_paths: tuple, world_paths: tuple) -> dict
verify_freeze(c, session_id: str, *, source_paths: tuple, world_paths: tuple) -> bool
export_science(database, session_id: str, output_dir) -> dict
```

命令行：`--source-db`、`--world-id`、`--output`、`--prepare-only`、`--run`、`--resume`、`--export-only`。prepare-only 不调用模型；run 必须完成冻结/离线验收清单，resume 同一会话同一账。没有绕过预算或手动指定研究者的参数。
- [ ] **1. 写失败测试，副本包含主库未 checkpoint 的 WAL 提交。**

```python
def test_backup_includes_wal(tmp_path):
    import sqlite3
    from world_engine.science_freeze import backup_world
    source, target = tmp_path / "source.db", tmp_path / "copy.db"
    with sqlite3.connect(source) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE marker (value TEXT)")
        c.execute("INSERT INTO marker VALUES ('已提交')")
        c.commit()
        backup_world(source, target)
    with sqlite3.connect(target) as copy:
        assert copy.execute("SELECT value FROM marker").fetchone()[0] == "已提交"
```

- [ ] **2. 运行** `.venv\Scripts\python.exe -m pytest tests/test_science_freeze.py -q`，确认失败。
- [ ] **3. 实现正常一致性备份。** 来源以只读连接读取，目的新文件通过 backup 创建，再在目的执行正常迁移。不得裸复制 db/WAL；检查目标路径不同于来源/浮空城/旧灾难存档。源库备份前后角色/世界/物资关键状态摘要核对一致，不以 WAL/SHM 文件哈希证明业务状态。

```python
import sqlite3
from pathlib import Path
def backup_world(source_path, destination_path):
    source, destination = Path(source_path).resolve(), Path(destination_path).resolve()
    if source == destination or destination.exists():
        raise ValueError("备份必须写入新的独立文件")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
```

- [ ] **4. 冻结事实与执行边界。** 清单含源码、世界观、通用自然规则、初始角色/物资摘要、工具、调度、预算、种子、启动事件、实际模型配置（无密钥）。运行动态消耗按账更新，不能把合法消耗误判初始数据篡改；未经过执行事务的角色/库存/预算修改须检测并停机。冻结配置改动不自动重建指纹。
- [ ] **5. 实现只读导出。** 生成 `处理过程.md`、`全部人物与通信.md`、`科技图.json`、`科技图.svg`、`突破过程/`、`编年.md`、`api-usage.json`、`freeze.json`、`最终验收.md`；来源编号、世界时间、请求原文与拒绝原因可追溯。敏感认证字段过滤，编年不额外调用总结 API。
- [ ] **6. 通过源码/配置/库存外部修改停机、合法消耗不误报、恢复不清账、只读导出不改库、源目标路径保护、WAL一致性、原角色目标/记忆保留、旧灾难/浮空城存档不变测试。**
- [ ] **7. 更新实际运行文档并保存增量。**

## Task 11：离线总验收、首次真实科研与完成审计

**Files:** `tests/test_science_end_to_end.py`、`scripts/demo_science.py`、本次独立 `run/science/<timestamp>/` 产物；所有成果以原始证据为准。

**Interfaces:** 此任务只消费 Task1–10 接口，不增加“为成功特调”的运行动作。
- [ ] **1. 编写离线端到端测试。** 假后端返回固定测试程序，但所有成果必须经过真实物理/劳动/通信/认证。断言两样品复验、编年、已知方法学习、实际复用形成依赖及费用累计；假后端成功不能证明真实 NPC 达成目标。

```python
def test_no_technology_without_model_actions(science_lab):
    from world_engine.science_programs import ScienceService
    from datetime import timedelta
    db, wid, _, _, _, at = science_lab
    with db.write() as c:
        ScienceService.tick(c, wid, at + timedelta(hours=24))
        assert c.execute("SELECT COUNT(*) FROM technologies WHERE world_id=?",
                         (wid,)).fetchone()[0] == 0
```

- [ ] **2. 运行离线科研检查。** 整个进程使用假后端或禁网保护，PowerShell 将通配符先解析为文件数组：

```powershell
$scienceTestFiles = @(Get-ChildItem -Path tests\test_science_*.py -File | ForEach-Object { $_.FullName })
& .\.venv\Scripts\python.exe -m pytest @scienceTestFiles -q
```
- [ ] **3. 运行相关回归一次。** 材料、collective研究/访问/证据/runner、autonomous research、history、API、NPC上下文/模型计账、clock，以及浮空城测试。只在新改动/失败后重跑对应项；不重复扩大测试消耗。所有测试禁网/生图，不读取真实密钥构造付费客户端。
- [ ] **4. 做实际 diff 审查。** 对照设计逐项检查：没有科技种子/待发现清单、真实物理规则与世界观吻合、最终模型请求不含隐藏知识、所有模型路径受预算、主库与其他存档未改。若选择 Native，按其技能完成一次独立整体审查；不能把尚未审查写成通过。
- [ ] **5. 准备独立存档并保存冻结清单。**

```powershell
& .\.venv\Scripts\python.exe scripts\demo_science.py --source-db data\world.db --world-id eb06b024-61f8-4ea8-b4e3-2695e4ccebf6 --output run\science\first --prepare-only
```

若此目录已经存在，换一个新时间戳目录；不删除/覆盖旧输出。核对角色基线、工坊物权、0项科技和0项预写计划、12/200000限制、冻结摘要以及无密钥导出。
- [ ] **6. 只启动这一份冻结存档一次。**

```powershell
& .\.venv\Scripts\python.exe scripts\demo_science.py --output run\science\first --run
```

命令中 output 使用第5步实际新目录。观察只读状态和日志；进程观测超时先核对同一会话/进程，不另开运行。余额/API错误/预算耗尽停止，失败仍计账。之后不改代码或库存再接着同一实验伪装不干预；需要修复则本轮明确结束并留下失败事实。
- [ ] **7. 导出并逐项完成审计。** 科技来自真实 API 原文→NPC程序→实物账→已知证据→认证→编年链；核对至少3项新能力、至少1条实际依赖及真实后续使用。费用不超过12次/200000保护额度，无重试/旁路请求；冻结期间无外部干预。
- [ ] **8. 如实报告。** 满足全部要求才将目标标记 complete。若预算内不足，展示人物实际选择、假说、试验、失败、候选、传播、费用及停机原因，目标仍未达成；不提高预算，不用测试成果替代真实结果。

## 计划自查与交付边界

| 设计要求 | 对应任务 |
|---|---|
| 自选问题、保留现有角色、未预置科技路线 | 2、3、8、10、11 |
| 世界自然规律、有限资源、单纹起点、未知方向 | 2、3、6 |
| 原始证据、预登记、两样品复验、去重 | 3、4、5 |
| 可执行方法、真实来源、动态无环科技图 | 5、6 |
| 知识传播、私有/未来证据拒绝、长历史 | 4、9 |
| 突破完整过程、编年、原子与幂等、失效保留 | 6、7 |
| 12次/200000额度、失败/推理、并发预留、恢复 | 1、8、10 |
| 正常心跳、NPC输入、观察界面、兼容旧存档 | 1、3、4、9 |
| 一致性副本、冻结、只读全过程导出 | 10、11 |
| 真实自主生长而非故事/测试后端成功 | 11 |

计划待哥哥审阅并选择执行方式，产品代码尚未开始；书面设计确认只完成上一阶段，不代表这份计划已获批。建议 Native：本会话逐项执行，完成离线检查后做一次整体独立审查；这些任务共用同一套实物/证据接口，频繁分派会增加上下文和协调成本。
