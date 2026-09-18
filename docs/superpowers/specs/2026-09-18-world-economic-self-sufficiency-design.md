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

## 3. 核心方案：不修改既有业务逻辑

> 措辞澄清：本方案需要**新增**迁移函数与 seeder 产出逻辑，但**不修改** `harvest` /
> `tick` / `_eat` / `daily_life.py` 的任何既有分支。风险面因此限于「新增数据是否正确」，
> 而非「既有行为是否被改坏」。

### 3.1 为什么不需要改既有逻辑

现有 `_eat` 逻辑已经包含了完整的四分支（`actions.py:213-238`）：

```python
① EconomyService.food(...)              # 背包有食物 → 直接吃
② if actor["money"] < 3: raise          # ← 死锁在此（27 人 money=0）
③ if 该地点有 food profile: raise       # 要求走购买/采集
④ if resources.food <= 0: raise
   resources.food -= 1; satiety += 42   # 免费拿，但扣 3 元
```

绕开死锁的关键是**走采集路径而非直接拿**：

```
NPC 饿 → _eat ① 背包空 → ② 有 profile 被拒 → 抛错
      → daily_life.py:280 harvest(food_only=True)
            → profile 精确匹配当前地点     ✓ (economy.py:272)
            → resources_json.food > 0      ✓ (economy.py:286)
            → energy >= 3                  ✓ (economy.py:288) 只花精力，不花钱
      → 食物进背包 → 下个 tick _eat ① 成功 → satiety +42
```

`harvest` **不要求金钱**，因此 27 个身无分文的 NPC 同样能存活。

### 3.2 被否决的方案

**方案 α（已否决）**：将 `resource_location_id` 语义扩展为「`NULL` = 任意地点可采集」。
否决原因：破坏两处既有契约——
- `economy.py:137` 要求 `resource_location_id` 与 `resource_key` 必须同时有值或同时为空
- `economy.py:332` 的再生查询显式排除 `resource_location_id IS NULL`
且波及 `harvest` / `tick` / `_eat` / `daily_life.py:296` 共 5 个调用点。

**方案 γ（已否决）**：仅写 `resources_json.food` 而不建 profile。
否决原因：`_eat` ④ 仍要求 `money >= 3`（`actions.py:218`），27 个零资产 NPC 依旧死锁。

### 3.3 实施内容

**资源数值的推导依据**（实测得出，勿凭感觉调整）：

- NPC 饱食度消耗：实测 `08:40 → 09:00` 掉 1 点 = **每 20 分钟世界时间 1 点**，
  即 **每小时 3 点**，与 `WORLD_SATIETY_LOSS_PER_WORLD_HOUR=3.0` 一致
- 每天消耗 `3 × 24 = 72` 点；一份食物恢复 **42** 点（`actions.py:235`）
- ⇒ **单个 NPC 每天需要约 1.7 份食物**

对每个 `kind IN ('city','town')` 的地点：

1. **写资源存量**
   在 `locations.resources_json` 增加 `food` 键：
   `food = clamp(round(population / 100), 10, 100)`
   （人口 1,163 → 12 份；6,155 → 62 份）

2. **建食物 profile**（复用同一 `item_type_id`）
   直接 SQL 写入 `world_item_profiles`，参数：
   - `resource_location_id` = 该地点 id
   - `resource_key` = `'food'`
   - `initial_resource` = 上述存量
   - `resource_capacity` = 存量 × 2
   - `daily_growth` = `clamp(round(population / 500), 3, 20)`
     即每城每天再生 3–20 份，可支撑约 **2–12 个 NPC** 的日常消耗

   > 必须绕过 `EconomyService.register_item`：它每次调用都新建 `item_types` 记录，
   > 而 `economy.py:132-136` 禁止世界内同名 item_type。618 条 profile 复用同一个
   > `item_type_id` 即可。

   > **数值校准**：上述系数按「NPC 均匀分布」估算。实施后应实测 NPC 的实际聚集分布，
   > 若出现「某城资源被采空后长期不恢复」，则按该城常驻 NPC 数提高 `daily_growth`。

3. **幂等**：迁移函数可重复执行；已存在 `(world_id, location_id, 'food')` 组合时跳过。

### 3.4 数据流

```
locations.resources_json.food ──┐
                                 ├─→ harvest() ─→ NPC 背包 ─→ EAT ─→ satiety ↑
world_item_profiles(resource_key)┘   (耗 3 精力)                      │
                                                                      ↓
EconomyService.tick ─→ daily_growth 每日再生             脱离危急 → 工作跑满 → 工资到账
                    (受 resource_capacity 上限约束)
```

## 4. 分阶段实施

### 阶段 1：止血（本设计的实施范围）

| 步骤 | 内容 | 文件 |
|---|---|---|
| 1.1 | 写资源存量与 profile 的迁移函数 | `world_engine/migrations.py` |
| 1.2 | 让 Noryia seeder 同步产出资源（新世界一致） | `world_engine/noryia_seeder.py` |
| 1.3 | CLI 增加维护入口，可对已有世界补资源 | `world_engine/cli.py` |
| 1.4 | 重启服务触发 `initialize()`，补齐 4 张缺失表 | — |

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
| 单元 | `test_city_locations_get_food_resource` | 迁移后每个 city/town 有 `food > 0` |
| 单元 | `test_food_profile_reused_single_item_type` | 618 条 profile 指向同一 `item_type_id` |
| 单元 | `test_harvest_works_at_any_town` | 任意城镇地点 harvest 成功 |
| 单元 | `test_resource_regenerates_with_cap` | 再生生效且不超过 `resource_capacity` |
| 单元 | `test_migration_is_idempotent` | 重复执行不产生重复 profile |
| **端到端** | `test_impoverished_npc_survives` | `money=0` 的 NPC 推进 N 轮后 `satiety > 0` |
| 回归 | 现有 396 个测试 | 全绿 |

## 6. 风险与缓解

| 风险 | 影响 | 缓解 |
|---|---|---|
| 资源被采空 | NPC 重新陷入饥饿 | `daily_growth` 再生 + `resource_capacity` 上限；再生量随人口缩放 |
| NPC 行为剧变 | 原有平衡被打破 | 指标化验收，先小规模观察再全量 |
| 迁移污染已有世界 | 数据损坏 | 幂等、只增不删；实施前备份 `data/world.db` |
| 618 条 profile 与地点脱节 | 新增地点无资源 | 迁移函数可重复执行；seeder 同步产出 |
| 室内无法采集 | NPC 在家采不到 | `harvest` 要求户外（`economy.py:269`）——NPC 会自行走到户外，属预期行为 |

## 7. 不在本设计范围

- 前端体验层（工程 B）：信息呈现、引导、操作流
- Agent 超时预算调整（1.4 节的附带发现）
- `npc_life_goals` 的目标内容配置（属阶段 3）
- 复杂谈判、谎言、组织摩擦所需的第二阶段模型冲突裁判
