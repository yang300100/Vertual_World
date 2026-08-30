# Noryia 多 Agent 世界编排架构实施方案

状态：实施前架构方案。适用于现有 `DecisionProvider → ActionProposal → ActionService → SQLite` 链路。

## 1. 目标与硬边界

多 Agent 的目标是提升叙事、角色动机、复杂冲突与战斗决策质量，而不是用模型替代世界规则。

不可变规则：

- SQLite 是世界状态唯一权威；任何 Agent、LLM、前端都没有直接写库权限。
- 唯一提交路径仍是 `ActionService.execute(...)` 与本地事务。
- 路径、地形、桥梁、水路、伤害、物品、金钱、时间、存活和关系数值由本地规则裁决。
- Agent 输入是只读世界快照与按身份过滤后的知识；输出只能是受 Pydantic 校验的提案。
- 隐藏真相、龙族私有知识、作者层文档不能进入普通 NPC、玩家或叙事 Agent 上下文。
- 每个事务内同一角色最多执行一个最终动作；并发 Agent 不能造成同一角色双重行动。

## 2. 总体架构

```text
世界心跳 / 玩家意图 / 战斗触发
                 │
                 ▼
        SceneAssembler（只读场景组装）
                 │
       ┌─────────┼──────────┐
       ▼         ▼          ▼
  事件导演   活跃NPC群      战斗战术
  Agent      Agent          Agent（仅战斗）
       └─────────┼──────────┘
                 ▼
      ProposalCoordinator（去重、排序、冲突检测）
                 ▼
      ActionService / CombatResolver / RoutingPlanner
                 ▼
         单 SQLite 事务提交、事件、记忆、日志
                 ▼
      MemoryCurator（异步提炼，只提交候选记忆）
```

`SceneAssembler`、各 Agent 和 `ProposalCoordinator` 都不持有数据库写连接。协调器输出的最终动作仍由 `WorldEngine.adjudicate` 在同一写事务内执行。

## 3. Agent 职责

| Agent | 输入 | 允许输出 | 调用时机 | 禁止事项 |
|---|---|---|---|---|
| 场景叙事 Agent | 主视角、可见 NPC、近期事件、地形上下文 | 场景文字、对话草稿、感官描述 | 玩家观察、动作结算后 | 提交动作、泄露不可见信息 |
| 事件导演 Agent | 区域局势、活跃 NPC 目标、资源与近期事件 | `EventSeed`、优先级、参与者候选 | 裁判周期、重大事件后 | 改数值、指定必然结果 |
| 活跃 NPC Agent | 单 NPC 身份、目标、关系、可见对象、个人记忆 | 一个 `ActionProposal` | 仅激活 NPC | 代表未激活 NPC 行动、写数据库 |
| 战斗战术 Agent | 已建立战斗遭遇、单位公开状态、地形、路线 | `CombatIntent` | 遭遇进入 `active` 状态 | 直接修改生命、命中或掉落 |
| 关系/记忆 Agent | 已结算事件、参与者、关系前值 | `MemoryCandidate`、关系变化理由 | 事件结算后异步 | 覆盖原始事件、直接改关系值 |
| 规则回退 Agent | 与现有 `RuleDecisionProvider` 相同 | 确定性 `ActionProposal` | 模型失败、预算耗尽、超时 | 无 |

背景 NPC 不单独调用模型。它们只在 `NPCActivationService` 将其激活后才进入“活跃 NPC Agent”候选池；掌权者、高关系 NPC 等持久激活者仍受每轮配额限制。

## 4. 共享数据契约

### 4.1 场景快照

新增只读 Pydantic 模型 `SceneContext`：

```python
class SceneContext(BaseModel):
    world_id: str
    world_time: datetime
    trigger: Literal["heartbeat", "player_intent", "combat", "event_followup"]
    pov_character_id: str | None
    location: LocationState | None
    terrain: TerrainContext | None
    visible_characters: list[CharacterState]
    recent_events: list[EventView]
    available_actions: list[ActionType]
    knowledge: list[KnowledgeHit]
    token_budget: int
```

`terrain` 来自正式导航数据集的只读采样；场景文字和战斗判断必须与路线/地形规则保持一致。

### 4.2 事件种子

```python
class EventSeed(BaseModel):
    id: str
    category: Literal["social", "economic", "political", "travel", "hazard", "combat"]
    priority: int = Field(ge=0, le=100)
    participant_ids: list[str]
    location_id: str | None
    premise: str
    proposed_consequences: list[str]
    expires_at: datetime
```

`EventSeed` 不是世界事件；只有协调器选中并由规则执行后才会产生正式 `world_events` 行。

### 4.3 战斗意图

```python
class CombatIntent(BaseModel):
    actor_id: str
    intent: Literal["attack", "defend", "withdraw", "use_item", "move"]
    target_id: str | None
    preferred_position: str | None
    reason: str
```

战斗 Agent 只产生战术偏好。`CombatResolver` 根据行动点、射程、地形、障碍、物品和伤害规则决定结果，并转换为现有 `ActionProposal` 或新的受控战斗动作。

### 4.4 记忆候选

```python
class MemoryCandidate(BaseModel):
    character_id: str
    event_id: str
    summary: str
    importance: int = Field(ge=1, le=5)
    confidence: float = Field(ge=0, le=1)
    memory_type: Literal["experienced", "heard", "inferred"]
```

记忆 Agent 不能编造事件；每个候选必须引用已存在的 `event_id`，并通过参与者、位置、可见性和时间校验。

## 5. ProposalCoordinator

新增 `world_engine/orchestration.py`，职责：

1. 读取同一版本的世界快照并冻结为 `SceneContext`。
2. 并行调用允许的 Agent；每个调用有独立超时、最大 token 和预算标签。
3. 对所有输出执行 Pydantic 校验、身份/知识过滤、引用完整性校验。
4. 按优先级排序：玩家明确动作 > 生存/安全规则 > 已激活 NPC > 事件导演 > 背景规则。
5. 同一 actor 只保留一项；冲突目标、资源、地点或路线时保留优先级更高者，其余写为拒绝原因。
6. 将最终 `ActionProposal` 列表交给 `WorldEngine` 的既有执行循环。

协调器不决定数值结果。例如两个 NPC 都要求同一物品，协调器只排队；`ActionService` 以事务内库存检查决定谁成功。

## 6. 数据库设计

正式世界状态仍使用现有表。新增审计与异步工作表：

```sql
CREATE TABLE agent_runs (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  trigger TEXT NOT NULL,
  agent_name TEXT NOT NULL,
  input_snapshot_version INTEGER NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('running','succeeded','failed','timed_out','skipped')),
  model_name TEXT,
  input_tokens INTEGER,
  output_tokens INTEGER,
  latency_ms INTEGER,
  error_text TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT
);

CREATE TABLE agent_proposals (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  actor_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
  proposal_type TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  validation_status TEXT NOT NULL CHECK(validation_status IN ('accepted','rejected','superseded')),
  rejection_reason TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE memory_jobs (
  id TEXT PRIMARY KEY,
  world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
  event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK(status IN ('pending','processing','done','failed')),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

不要建立“Agent 自己的世界状态表”。Agent 的长期偏好应写入已有角色目标、关系、记忆或可审计的提案记录。

## 7. 调用与预算策略

### 心跳

- 每个常规心跳：只运行本地移动、状态衰减、NPC 激活和规则逻辑。
- 到达裁判边界或出现玩家动作：事件导演一次 + 最多 `active_character_limit` 名活跃 NPC。
- 战斗未发生时不调用战斗 Agent。
- 记忆整理放入低优先级队列，不阻塞世界时间。

### 默认上限

| 类别 | 每次触发上限 | 超时 | 失败行为 |
|---|---:|---:|---|
| 事件导演 | 1 | 12 秒 | 无事件种子，继续规则裁判 |
| NPC 行为 | 8 名 | 每名 8 秒 | 该 NPC 回退规则决策 |
| 战斗战术 | 每阵营 1 | 10 秒 | 使用确定性目标优先级 |
| 场景叙事 | 1 | 10 秒 | 使用规则模板描述 |
| 记忆整理 | 10 个事件/批 | 20 秒 | 保留原始事件，稍后重试 |

预算由 `Settings` 控制：`WORLD_AGENT_BUDGET_PER_HEARTBEAT`、`WORLD_AGENT_MAX_CONCURRENCY`、`WORLD_AGENT_TIMEOUT_SECONDS`。达到预算时优先保留玩家动作和战斗安全，放弃叙事润色。

## 8. 角色知识与提示词

每次调用构造四段上下文：

1. **不可变规则摘要**：只包含对该 Agent 必须知道的动作格式与安全边界。
2. **角色身份包**：角色自身目标、记忆、关系、物品、地形位置、移动状态。
3. **可见场景包**：激活 NPC、可见地点、已知事件、地形和路线事实。
4. **按角色授权的 RAG 命中**：只读取相应 audience 的知识。

提示词必须明确：输出 JSON；不自行结算伤害、不创建物品、不移动到不可达位置、不引用隐藏术语。现有 `DeepSeekDecisionProvider._validate_character_perspective` 保留，并扩展到每种 Agent 输出。

## 9. 与现有模块的改造点

| 现有模块 | 改造 |
|---|---|
| `decisions.py` | 保留 `DecisionProvider`；新增多 Agent provider/adapters，不删除规则与 DeepSeek 回退 |
| `engine.py` | 在 `adjudicate` 前组装场景、调用协调器；最终仍调用 `_filter_proposals` 与 `ActionService.execute` |
| `activation.py` | 提供活跃 NPC 的稳定排序和每轮候选池，不改变距离激活规则 |
| `actions.py` | 保持唯一写入职责；为战斗/记忆候选增加验证入口 |
| `knowledge.py` | 提供按 `character_id`、Agent 类型和 audience 的检索门面 |
| `movement.py` | 只由规则/路线系统处理，Agent 可请求目的地但不能给路径坐标折线 |
| `web/app.js` | 显示事件来源、提案被拒原因的简短说明、战斗与叙事结果；不暴露隐藏 prompt |

## 10. 战斗子系统

战斗需要独立于叙事 Agent 的 `CombatResolver`：

1. `ActionService` 判定攻击或敌对事件是否能创建遭遇。
2. Resolver 读取双方公开属性、装备、生命、距离、地形、道路/掩体和可撤退路线。
3. 战斗 Agent 为每方输出一个 `CombatIntent`。
4. Resolver 按先攻、行动点、射程、随机种子和规则结算。
5. 结果转换为 `world_events`、生命/物品/关系变化和记忆任务。

随机必须使用保存到 `combat_encounters.random_seed` 的本地种子，保证回放与调试可复现。模型不得生成命中、伤害、掉落或死亡结论。

## 11. 失败、并发与恢复

- Agent 超时、格式错误、网络错误：写 `agent_runs`，立即走规则回退；世界心跳不能被阻塞。
- 多 Agent 对同一 NPC 输出：协调器按角色只取一项；其余标记 `superseded`。
- 世界版本变化：提交前比较 `snapshot.version`；版本不一致时重新取快照、最多重试一次，随后回退规则。
- 外部模型不可用：不重试无限次；保持现有 deterministic provider。
- 记忆任务失败：不影响已结算事件，保留 `pending/failed` 任务供后续重试。

## 12. 实施阶段

### 阶段 A：审计与空壳

1. 新增表和 Pydantic 契约。
2. 实现 `SceneAssembler`、`ProposalCoordinator`，但只调用现有规则 provider。
3. 写提案审计记录，不改变最终行为。

### 阶段 B：活跃 NPC 与事件导演

1. 接入事件导演 Agent，仅输出 `EventSeed`。
2. 接入活跃 NPC Agent；每轮最多 8 名。
3. 经协调后走既有 `ActionService`。

### 阶段 C：叙事与记忆

1. 加场景叙事 Agent，仅生成展示文本。
2. 加异步记忆候选与验证写入。
3. 验证 RAG 权限隔离与事件可追溯性。

### 阶段 D：战斗 Agent

1. 建立确定性 CombatResolver 与遭遇表。
2. 再接入战斗战术 Agent。
3. 进行回放、随机种子、撤退与地形测试。

## 13. 验收测试

1. Agent 不能直接修改 SQLite；仅 `ActionService` 产生状态变化。
2. 模型输出非法动作、隐藏术语、越界坐标、伪造物品时被拒绝并记录原因。
3. 同一角色多个提案仅执行一个，且事务回放结果一致。
4. 模型服务断开时，心跳、移动和战斗规则持续正常运行。
5. 未激活 NPC 不产生模型调用；特殊 NPC 的调用也遵守预算。
6. 战斗 Agent 的不同建议不会改变伤害随机种子或跳过地形/射程验证。
7. 角色只能看到其身份和距离允许的知识、事件与对话。
8. 压测下每心跳 token、延迟、失败率和 SQLite 锁等待均有审计数据。

## 14. 明确禁止

- 不给每个背景 NPC 常驻一个模型会话。
- 不让 Agent 返回 SQL、Python、任意 JSON Patch 或原始数据库字段更新。
- 不让战斗叙事直接写生命、掉落或胜负。
- 不让事件导演跳过移动、路线、物品、关系和权限规则。
- 不把作者层/龙族/轨道巨构知识作为通用上下文。
