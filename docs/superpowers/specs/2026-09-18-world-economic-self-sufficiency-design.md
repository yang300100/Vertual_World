# 世界经济自洽设计（阶段一：止血）

日期：2026-09-18
状态：待实施

## 1. 问题

实际运行的世界中，全部 31 个 NPC 陷入**不可逃脱的生存死锁**，表现为「重复单调、目标不推进」——
即用户观察到的「NPC 不合理」。这不是模型能力问题，而是经济闭环在**资源供给侧**完全断裂。

### 1.1 实测证据

| 观测项 | 值 |
|---|---|
| NPC 平均金钱 | 1.2（27/31 人为 0） |
| NPC 平均饱食度 | **0.0**（全部饿到极限） |
| 世界中食物实例 | **0**（全库仅 2 个物品实例，均非食物） |
| `action.rejected` | **77,152** 次（占全部事件 77%） |
| ├ 其中「没有足够的钱购买食物」 | **17,391** 次 |
| `action.life_interrupted` | **29,679** 次（理由 100% 为「身体状况需要先处理」） |
| `action.npc_activity_started` | 29,704 次（几乎 1:1 被打断） |
| `action.eat` | **0** 次 |
| `action.life_completed` | **0** 次 |
| `npc_todos` | 35 条，**全部 `open`**，无 doing/done |

### 1.2 自锁环

```
satiety ≤ 10 判危急 (daily_life.py:197)
        ↓
掐断正在进行的 work (daily_life.py:203-210)   ← 29,679 次
        ↓
寻找食物三条路全部失败：
  ① _eat 背包空          (actions.py:215)
  ② buy_food 无卖家有货  (economy.py:79 要求卖家身份含 商/店/贩…)
  ③ harvest 无资源来源   (economy.py:286 要求 resources_json[key]>0)
        ↓
money < 3 → 去找工作 (daily_life.py:282-287)
        ↓
工作跑不满 60 分钟又被掐断 → 9 元工资永远到不了手
        ↓
satiety 继续为 0 ────────────────────────────┘
```

### 1.3 断点定位

| # | 断点 | 证据 |
|---|---|---|
| A | **资源供给侧为空** | 618 个地点的 `resources_json` 只有 `source_id/population/elevation_m/capital/port` 五个地理元数据键，**没有任何可采集资源** |
| B | **食物 profile 无产出配置** | `world_item_profiles` 仅 3 条，`resource_location_id` / `resource_key` 全为 `NULL`、`daily_growth = 0` → `harvest` 恒返回 `False`、`tick` 永不再生 |
| C | **无食物生产配方** | `activity_recipes` 仅 1 条（木柄工具，产物非食物）→ `ProductionService` 从未被触发 |
| D | **NPC 背包全空** | 两个 seeder 都不发放初始食物 |
| E | **工作收入被打断** | 见 1.2；`daily_life.py:203-210` 与 `:264` 的时序使 NPC 在饿死前拿不到工资 |
| F | **无 workplace 地点** | 实跑世界 `locations.kind` 仅 `city`(274) / `town`(344) |
| G | **目标系统三套并存且互不相通** | 见 4.3 |

### 1.4 附带发现（独立问题，不属于本设计范围）

- `WORLD_AGENT_TIMEOUT_SECONDS=8`，而实测 `event_director` 平均耗时 **13,595ms**、最大 26,864ms，
  `active_npc` 平均 8,518ms → 30 次 Agent 调用中 **7 次超时**。
- 正式库 schema 落后：缺少 `npc_goal_steps`、`npc_life_goals`（代码会创建，但库自 2026-09-14 未重新初始化）。

## 2. 设计决策（已与项目所有者确认）

| 决策 | 选择 | 含义 |
|---|---|---|
| **经济基调** | 基本保障：NPC 不会饿死 | 采集只消耗精力、不消耗金钱，身无分文也能维持生存 |
| **目标系统** | 以 B 为引擎，C 做展示 | 启用 `npc_life_goals` 作为状态机，`npc_todos` 退化为其可见投影 |

## 3. 核心方案：引入「通用资源」语义

### 3.1 三条候选路线的探测结果

| 路线 | 结论 | 否决/采纳依据 |
|---|---|---|
| ① 每地点一条 profile，复用同一 `item_type` | ❌ 不可行 | `world_item_profiles.item_type_id` 是 **PRIMARY KEY**（`schema.py:90`），一个 item_type 只允许一条 profile |
| ② 改 schema 为复合主键，实现「一物多产地」 | ❌ 危险 | 有 **7+ 处** `JOIN world_item_profiles p ON p.item_type_id = i.item_type_id` 用于查价格/营养（`economy.py:85,221`、`interiors.py:130`、`life_api.py:83`、`living_api.py:357,394,444`）。一物多 profile 会让这些 JOIN **笛卡尔积爆炸** |
| ③ 每地点一个独立 item_type（名字带地名） | ⚠️ 可行但劣 | 零代码改动，但产生 618 个 item_type + 618 条 registration；NPC 背包里会出现「粮食（澜誓城）」这类冗长物品名 |
| ④ **引入「通用资源」语义** | ✅ **采纳** | 5 处小改动，数据干净，语义正确 |

### 3.2 方案 ④ 的语义定义

> `resource_key` 有值、`resource_location_id` 为 `NULL`
> ⇒ 该资源在**任何地点**都可采集。

这解决了既有模型无法表达「一类地点都有某资源」的根本缺陷，且不触碰 `item_type_id` 的唯一性。

### 3.3 改动清单（5 处）

| # | 位置 | 现状 | 改后 |
|---|---|---|---|
| 1 | `economy.py:137` | `bool(location_id) != bool(key)` 即报错 | 允许 `key` 有值而 `location_id` 为 `NULL`（表示通用） |
| 2 | `economy.py:272` | `AND p.resource_location_id=?` | `AND (p.resource_location_id=? OR p.resource_location_id IS NULL)` |
| 3 | `economy.py:332` | 排除 `resource_location_id IS NULL` | 通用资源对**每个活跃地点**再生 |
| 4 | `actions.py:226` | 只查该地点的 food profile | 同时匹配通用 profile（否则 `_eat` ④ 会绕过采集直接免费拿） |
| 5 | `daily_life.py:296` | 收集 `resource_location_id` 集合 | 集合含 `NULL` 时视为「所有地点均可」 |

**改动 3 的实现要点**：`last_growth_world_time` 存在 profile 上，通用 profile 只有一个时间戳，
因此再生需遍历所有活跃地点、对每个地点的 `resource_key` 存量做上限封顶增长：

```python
if row["resource_location_id"] is None:
    targets = c.execute("SELECT id, resources_json FROM locations WHERE is_active=1").fetchall()
else:
    targets = [c.execute("SELECT id, resources_json FROM locations WHERE id=? AND is_active=1",
                         (row["resource_location_id"],)).fetchone()]
    targets = [t for t in targets if t is not None]
```

### 3.4 数据播种

**资源数值的推导依据**（实测得出，勿凭感觉调整）：

- NPC 饱食度消耗：实测 `08:40 → 09:00` 掉 1 点 = **每 20 分钟世界时间 1 点**，
  即 **每小时 3 点**，与 `WORLD_SATIETY_LOSS_PER_WORLD_HOUR=3.0` 一致
- 每天消耗 `3 × 24 = 72` 点；一份食物恢复 **42** 点（`actions.py:235`）
- ⇒ **单个 NPC 每天需要约 1.7 份食物**

播种内容：

1. **一个通用食物 item_type**（如「粮食」，`category='food'`, `nutrition=20`, `price=3`）
2. **一条对应的通用 profile**：`resource_location_id = NULL`、`resource_key = 'food'`、
   `daily_growth = 5`、`resource_capacity = 200`，并附带一条 `element_registration_requests`
   记录（`registration_id` 为 NOT NULL 外键）
3. **每个 `kind IN ('city','town')` 地点的资源存量**：
   `resources_json.food = clamp(round(population / 100), 10, 100)`
   （人口 1,163 → 12 份；6,155 → 62 份）

> **数值校准**：`daily_growth=5` 是按「单城常驻少量 NPC」估算的全局下限。
> 实施后应实测 NPC 的实际聚集分布，若出现「某城资源被采空后长期不恢复」，
> 再按该城常驻 NPC 数调整。

4. **幂等**：迁移函数可重复执行；已存在该 `resource_key` 的通用 profile 时跳过。

### 3.5 数据流

```
每个城镇 resources_json.food ─┐
                               ├─→ harvest(food_only=True) ─→ NPC 背包
通用 profile(location=NULL) ───┘        (耗 3 精力，零金钱)        │
                                                                  ↓
                                                   下个 tick  _eat ① 成功
                                                       satiety += 42  │
                                                                      ↓
EconomyService.tick ─→ 通用资源逐地点再生          脱离危急 → 工作跑满 60 分钟 → 工资到账
                     (受 resource_capacity 封顶)
```

## 4. 分阶段实施

### 阶段 1：止血（本设计的实施范围）

| 步骤 | 内容 | 文件 |
|---|---|---|
| 1.1 | 新增通用资源播种模块（三处共用，DRY） | 新建 `world_engine/food_supply.py` |
| 1.2 | 5 处代码改动，支持通用资源语义 | `economy.py`、`actions.py`、`daily_life.py` |
| 1.3 | 接入迁移，使已有世界自动补齐 | `world_engine/migrations.py` |
| 1.4 | 接入 Noryia seeder，使新世界一致 | `world_engine/noryia_seeder.py` |
| 1.5 | CLI 增加手动维护入口 | `world_engine/cli.py` |
| 1.6 | 重启服务触发 `initialize()`，补齐 4 张缺失表 | — |

**验收指标**（全部可量化）：

| 指标 | 现状 | 目标 |
|---|---|---|
| `action.eat` 事件 | 0 | > 0 且持续增长 |
| `action.resource_harvested` | 3 | 持续增长 |
| NPC 平均饱食度 | 0.0 | > 50 |
| `life_interrupted` 增速 | 与 started 1:1 | 显著低于 started |

### 阶段 2：生计

补 `workplace` 地点与工资账户，使工作报酬可达；复核 `daily_life.py:203-210` 的中断时序，
确保 NPC 在饱食度恢复后能跑满一次完整工作周期。

验收：工资到账事件 > 0；`action.life_completed` > 0。

### 阶段 3：目标（启用 B 引擎）

建 `npc_life_goals` / `npc_goal_steps`；为 NPC 配置 `ambitions`；将 `npc_todos` 改为
`npc_life_goals` 的可见投影，并废弃 `engine.py:_create_npc_owned_plans` 这条
「只写不读」的路径（其创建的 todo 因 `contract_id` 为 `NULL`，
永远匹配不到 `contracts.py:250` / `:356` 的推进 UPDATE）。

验收：`npc_todos` 出现 `doing` / `done`。

### 阶段 4：生产

补食物 craft 配方、资源再生调参、买卖闭环。

验收：NPC 能自产食物；出现真实交易事件。

## 5. 测试策略

| 层级 | 测试 | 断言 |
|---|---|---|
| 单元 | `test_general_profile_registers_without_location` | 允许 `resource_key` 有值而 `resource_location_id` 为 NULL |
| 单元 | `test_city_locations_get_food_resource` | 播种后每个 city/town 有 `food > 0` |
| 单元 | `test_harvest_works_at_any_town` | 任意城镇地点 harvest 成功（通用 profile 命中） |
| 单元 | `test_harvest_prefers_location_specific_profile` | 专属 profile 仍优先于通用 profile |
| 单元 | `test_generic_resource_regenerates_all_locations` | 通用资源在每个活跃地点都再生，且单点不超 `resource_capacity` |
| 单元 | `test_eat_rejects_free_pickup_when_generic_profile_exists` | 有通用 profile 时 `_eat` 不绕过采集 |
| 单元 | `test_seeding_is_idempotent` | 重复播种不产生重复 profile |
| **端到端** | `test_impoverished_npc_survives` | `money=0` 的 NPC 推进 N 轮后 `satiety > 0` |
| 回归 | 现有 396 个测试 | 全绿 |

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 通用资源被采空 | NPC 重新陷入饥饿 | `daily_growth` 逐地点再生 + `resource_capacity` 封顶 |
| **通用 profile 影响 7+ 处 JOIN** | 价格/营养查询出现歧义 | 通用 profile 仍是**独立 item_type**（「粮食」），不与既有物品类型共用 `item_type_id`，因此 JOIN 保持唯一 |
| NPC 行为剧变 | 原有平衡被打破 | 指标化验收，先小规模观察再全量 |
| 迁移污染已有世界 | 数据损坏 | 幂等、只增不删；实施前备份 `data/world.db` |
| 新地点无资源 | 局部无法采集 | 播种函数可重复执行；seeder 同步产出 |
| 室内无法采集 | NPC 在家采不到 | `harvest` 要求户外（`economy.py:269`）——NPC 会自行走到户外，属预期行为 |

## 7. 不在本设计范围

- 前端体验层（工程 B）：信息呈现、引导、操作流
- Agent 超时预算调整（1.4 节的附带发现）
- `npc_life_goals` 的目标内容配置（属阶段 3）
- 复杂谈判、谎言、组织摩擦所需的第二阶段模型冲突裁判
