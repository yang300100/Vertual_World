# 世界经济自洽（阶段一：止血）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让世界的 NPC 能靠采集获得食物而不依赖金钱，从而打破「饿死 → 无法工作 → 没钱 → 更饿」的自锁环。

**Architecture:** 引入「通用资源」语义——`resource_key` 有值而 `resource_location_id` 为 `NULL` 表示「任何地点均可采集」。这样既能表达「所有城镇都产粮」，又不触碰 `world_item_profiles.item_type_id` 的主键唯一性（有 7+ 处 JOIN 依赖它）。数据侧为每个城镇写入 `resources_json.food`，由已有的 `harvest` 路径消费。

**Tech Stack:** Python 3.11+、SQLite、pytest、FastAPI（本计划不涉及 API 层）

**Spec:** `docs/superpowers/specs/2026-09-18-world-economic-self-sufficiency-design.md`

---

## 文件结构

| 文件 | 职责 | 状态 |
|---|---|---|
| `world_engine/food_supply.py` | 通用食物资源的定义与播种；被迁移/seeder/CLI 共用 | **新建** |
| `world_engine/economy.py` | 放宽 profile 校验、harvest 与 tick 支持通用资源 | 修改 |
| `world_engine/actions.py` | `_eat` 的 profile 检查同时匹配通用资源 | 修改 |
| `world_engine/daily_life.py` | `food_ids` 兼容 `NULL` | 修改 |
| `world_engine/migrations.py` | 已有世界自动补齐食物资源 | 修改 |
| `world_engine/noryia_seeder.py` | 新世界创建时同步播种 | 修改 |
| `world_engine/cli.py` | 手动维护入口 | 修改 |
| `tests/test_food_supply.py` | 资源语义与播种的单元测试 | **新建** |
| `tests/test_npc_survival.py` | 端到端存活验收 | **新建** |

---

### Task 1: 放宽 profile 校验，允许通用资源

**Files:**
- Modify: `world_engine/economy.py:137-138`
- Test: `tests/test_food_supply.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_food_supply.py`：

```python
"""通用食物资源的语义与播种。"""

from __future__ import annotations

from uuid import uuid4

import pytest

from world_engine.economy import CommoditySpec, EconomyService
from world_engine.repository import utc_now
from world_engine.seeder import create_iserra_world


@pytest.fixture
def world(database):
    return create_iserra_world(database)


def make_registration(connection, world_id: str, element_type: str = "commodity") -> str:
    """登记表要求 source_event_id 非空，因此先造一条事件。"""
    event_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO world_events(
            id, world_id, tick_id, occurred_at, event_type, summary,
            importance, payload_json, created_at
        ) VALUES (?, ?, ?, ?, 'world.element_proposed', '为测试造的事件',
                  'routine', '{}', ?)
        """,
        (event_id, world_id, str(uuid4()), utc_now().isoformat(), utc_now().isoformat()),
    )
    registration_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO element_registration_requests(
            id, world_id, element_type, source_event_id, idempotency_key,
            status, payload_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'applied', '{}', ?, ?)
        """,
        (
            registration_id, world_id, element_type, event_id, registration_id,
            utc_now().isoformat(), utc_now().isoformat(),
        ),
    )
    return registration_id


def test_general_profile_registers_without_location(database, world) -> None:
    """resource_key 有值、resource_location_id 为 None ⇒ 通用资源，应当被接受。"""
    with database.write() as connection:
        registration_id = make_registration(connection, world)
        item_type_id = EconomyService.register_item(
            connection,
            world,
            registration_id,
            CommoditySpec(
                name="通用粮食",
                category="food",
                nutrition=20,
                price=3,
                resource_key="food",
                initial_resource=0,
                daily_growth=5,
                resource_capacity=200,
            ),
            utc_now(),
        )
        row = connection.execute(
            "SELECT resource_location_id, resource_key FROM world_item_profiles WHERE item_type_id=?",
            (item_type_id,),
        ).fetchone()
    assert row["resource_location_id"] is None
    assert row["resource_key"] == "food"


def test_profile_without_key_is_still_rejected(database, world) -> None:
    """既无地点也无资源名 ⇒ 仍是非法组合。"""
    with database.write() as connection:
        registration_id = make_registration(connection, world)
        with pytest.raises(ValueError, match="资源名"):
            EconomyService.register_item(
                connection,
                world,
                registration_id,
                CommoditySpec(name="无来源物品", category="material"),
                utc_now(),
            )
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -v`
Expected: `test_general_profile_registers_without_location` FAIL，错误为 `ValueError: 资源来源地点与资源名需要同时填写`

- [ ] **Step 3: 实现**

把 `world_engine/economy.py:137-138` 替换为：

```python
        # 通用资源：resource_key 有值而 resource_location_id 为 None，表示任何地点均可采集。
        # 两者同时为空是合法的「无资源来源」物品。
        if spec.resource_location_id and not spec.resource_key:
            raise ValueError("指定资源地点时必须同时填写资源名")
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -v`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add tests/test_food_supply.py world_engine/economy.py
git commit -m "feat(economy): 允许 resource_key 有值而 location 为空的通用资源"
```

---

### Task 2: harvest 支持通用资源

**Files:**
- Modify: `world_engine/economy.py:272`
- Test: `tests/test_food_supply.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_food_supply.py`：

```python
def test_harvest_works_at_any_town(database, world) -> None:
    """通用 profile 应让任意有 food 存量的地点都能采集。"""
    with database.write() as connection:
        registration_id = make_registration(connection, world)
        EconomyService.register_item(
            connection,
            world,
            registration_id,
            CommoditySpec(
                name="通用粮食", category="food", nutrition=20,
                resource_key="food", initial_resource=0,
                daily_growth=5, resource_capacity=200,
            ),
            utc_now(),
        )
        # 给两个不同地点写入 food 存量
        place_ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id, resources_json FROM locations WHERE world_id=? LIMIT 2", (world,)
            ).fetchall()
        ]
        for place_id in place_ids:
            connection.execute(
                "UPDATE locations SET resources_json=? WHERE id=?",
                ('{"food": 20}', place_id),
            )
        actor_id = connection.execute(
            "SELECT id FROM characters WHERE world_id=? LIMIT 1", (world,)
        ).fetchone()["id"]

    for place_id in place_ids:
        with database.write() as connection:
            connection.execute(
                "UPDATE characters SET location_id=?, current_location_id=?, current_room_id=NULL, energy=100 "
                "WHERE id=?",
                (place_id, place_id, actor_id),
            )
            actor = connection.execute(
                "SELECT * FROM characters WHERE id=?", (actor_id,)
            ).fetchone()
            assert EconomyService.harvest(connection, actor, utc_now(), food_only=True) is True
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py::test_harvest_works_at_any_town -v`
Expected: FAIL（`harvest` 返回 False，因为查询只匹配 `resource_location_id = 该地点`）

- [ ] **Step 3: 实现**

把 `world_engine/economy.py:272` 的查询改为：

```python
            "SELECT p.*,t.name,t.stack_limit,t.slot_size FROM world_item_profiles p JOIN item_types t ON t.id=p.item_type_id "
            "WHERE p.world_id=? AND (p.resource_location_id=? OR p.resource_location_id IS NULL) "
            "ORDER BY p.resource_location_id IS NULL",
```

`ORDER BY ... IS NULL` 让**地点专属 profile 排在前面**，保证专属资源优先于通用资源被采集。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -v`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add tests/test_food_supply.py world_engine/economy.py
git commit -m "feat(economy): harvest 支持通用资源，专属资源优先"
```

---

### Task 3: tick 为通用资源逐地点再生

**Files:**
- Modify: `world_engine/economy.py:331-357`
- Test: `tests/test_food_supply.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_food_supply.py`：

```python
def test_generic_resource_regenerates_all_locations(database, world) -> None:
    """通用资源应在每个活跃地点各自再生，且不超过 resource_capacity。"""
    from datetime import timedelta

    from world_engine.repository import from_iso, to_iso

    with database.write() as connection:
        registration_id = make_registration(connection, world)
        EconomyService.register_item(
            connection,
            world,
            registration_id,
            CommoditySpec(
                name="再生粮食", category="food", nutrition=20,
                resource_key="food", initial_resource=0,
                daily_growth=5, resource_capacity=12,
            ),
            utc_now(),
        )
        place_ids = [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM locations WHERE world_id=? LIMIT 2", (world,)
            ).fetchall()
        ]
        for place_id in place_ids:
            connection.execute(
                "UPDATE locations SET resources_json=? WHERE id=?", ('{"food": 0}', place_id)
            )
        start = utc_now()
        connection.execute(
            "UPDATE world_item_profiles SET last_growth_world_time=? WHERE resource_key='food'",
            (to_iso(start),),
        )
        EconomyService.tick(connection, world, start + timedelta(days=1))
        values = [
            json.loads(
                connection.execute(
                    "SELECT resources_json FROM locations WHERE id=?", (place_id,)
                ).fetchone()["resources_json"]
            ).get("food")
            for place_id in place_ids
        ]
    # 一天 × 每天 5 份 = 5，未触及上限 12
    assert values == [5, 5]


def test_generic_resource_respects_capacity(database, world) -> None:
    """再生不得超过 resource_capacity。"""
    from datetime import timedelta

    from world_engine.repository import to_iso

    with database.write() as connection:
        registration_id = make_registration(connection, world)
        EconomyService.register_item(
            connection,
            world,
            registration_id,
            CommoditySpec(
                name="上限粮食", category="food", nutrition=20,
                resource_key="food", initial_resource=0,
                daily_growth=100, resource_capacity=7,
            ),
            utc_now(),
        )
        place_id = connection.execute(
            "SELECT id FROM locations WHERE world_id=? LIMIT 1", (world,)
        ).fetchone()["id"]
        connection.execute(
            "UPDATE locations SET resources_json=? WHERE id=?", ('{"food": 0}', place_id)
        )
        start = utc_now()
        connection.execute(
            "UPDATE world_item_profiles SET last_growth_world_time=? WHERE resource_key='food'",
            (to_iso(start),),
        )
        EconomyService.tick(connection, world, start + timedelta(days=1))
        value = json.loads(
            connection.execute(
                "SELECT resources_json FROM locations WHERE id=?", (place_id,)
            ).fetchone()["resources_json"]
        )["food"]
    assert value == 7
```

在文件顶部的 import 中加入 `import json`。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -k regenerates -v`
Expected: FAIL（通用 profile 被 `resource_location_id IS NOT NULL` 过滤掉，资源仍为 0）

- [ ] **Step 3: 实现**

把 `world_engine/economy.py:331-357` 的循环体改为：

```python
        rows = c.execute(
            "SELECT * FROM world_item_profiles WHERE world_id=? AND daily_growth>0",
            (wid,),
        ).fetchall()
        for row in rows:
            elapsed = (at - from_iso(row["last_growth_world_time"])).days
            if elapsed <= 0:
                continue
            if row["resource_location_id"] is None:
                # 通用资源：对每个活跃地点各自再生（上限按单点计算）。
                targets = c.execute(
                    "SELECT id, name, resources_json FROM locations WHERE is_active=1"
                ).fetchall()
            else:
                found = c.execute(
                    "SELECT id, name, resources_json FROM locations WHERE id=? AND is_active=1",
                    (row["resource_location_id"],),
                ).fetchone()
                targets = [found] if found is not None else []
            grew_anywhere = False
            for loc in targets:
                resources = json.loads(loc["resources_json"] or "{}")
                before = int(resources.get(row["resource_key"], 0))
                after = min(row["resource_capacity"], before + elapsed * row["daily_growth"])
                if after == before:
                    continue
                resources[row["resource_key"]] = after
                c.execute(
                    "UPDATE locations SET resources_json=? WHERE id=?",
                    (json.dumps(resources, ensure_ascii=False), loc["id"]),
                )
                grew_anywhere = True
                ActionService._record_event(
                    c,
                    world_id=wid,
                    tick_id=str(uuid4()),
                    occurred_at=at,
                    event_type="world.resource_regenerated",
                    actor_id=None,
                    target_id=None,
                    location_id=loc["id"],
                    summary=f"{loc['name']}的可再生资源增加了{after - before}份。",
                    payload={
                        "resource": row["resource_key"],
                        "before": before,
                        "after": after,
                        "registration_id": row["registration_id"],
                    },
                )
            if grew_anywhere:
                c.execute(
                    "UPDATE world_item_profiles SET last_growth_world_time=? WHERE item_type_id=?",
                    (
                        to_iso(from_iso(row["last_growth_world_time"]) + timedelta(days=elapsed)),
                        row["item_type_id"],
                    ),
                )
```

> 注意：原实现对「某地点已达上限」仍会推进 `last_growth_world_time`；上面改为只在**至少一个地点真的增长**时推进，避免全部满仓时时间戳空转。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -v`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add tests/test_food_supply.py world_engine/economy.py
git commit -m "feat(economy): 通用资源逐地点再生并受容量封顶"
```

---

### Task 4: `_eat` 不绕过通用资源

**Files:**
- Modify: `world_engine/actions.py:226`
- Test: `tests/test_food_supply.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_food_supply.py`：

```python
def test_eat_rejects_free_pickup_when_generic_profile_exists(database, world) -> None:
    """存在通用 food profile 时，_eat 必须要求走采集，而不是就地免费拿。"""
    from world_engine.actions import ActionRuleError, ActionService
    from world_engine.domain import ActionProposal, ActionType

    with database.write() as connection:
        registration_id = make_registration(connection, world)
        EconomyService.register_item(
            connection,
            world,
            registration_id,
            CommoditySpec(
                name="通用粮食", category="food", nutrition=20,
                resource_key="food", initial_resource=0,
                daily_growth=5, resource_capacity=200,
            ),
            utc_now(),
        )
        place_id = connection.execute(
            "SELECT id FROM locations WHERE world_id=? LIMIT 1", (world,)
        ).fetchone()["id"]
        connection.execute(
            "UPDATE locations SET resources_json=? WHERE id=?", ('{"food": 30}', place_id)
        )
        actor_id = connection.execute(
            "SELECT id FROM characters WHERE world_id=? LIMIT 1", (world,)
        ).fetchone()["id"]
        connection.execute(
            "UPDATE characters SET location_id=?, current_location_id=?, money=100 WHERE id=?",
            (place_id, place_id, actor_id),
        )
        actor = connection.execute("SELECT * FROM characters WHERE id=?", (actor_id,)).fetchone()
        with pytest.raises(ActionRuleError, match="登记库存"):
            ActionService()._eat(connection, actor, utc_now())
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -k free_pickup -v`
Expected: FAIL（未抛异常——通用 profile 未被检查，NPC 直接白拿了一份）

- [ ] **Step 3: 实现**

把 `world_engine/actions.py:226` 替换为：

```python
        if connection.execute(
            "SELECT 1 FROM world_item_profiles WHERE world_id=? "
            "AND (resource_location_id=? OR resource_location_id IS NULL) AND resource_key='food'",
            (actor["world_id"], self._require_current_location(actor)),
        ).fetchone():
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -v`
Expected: 6 passed

- [ ] **Step 5: 提交**

```bash
git add tests/test_food_supply.py world_engine/actions.py
git commit -m "fix(actions): _eat 不再绕过通用食物资源直接免费拿取"
```

---

### Task 5: `daily_life` 的 food_ids 兼容通用资源

**Files:**
- Modify: `world_engine/daily_life.py:288-304`
- Test: `tests/test_food_supply.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_food_supply.py`：

```python
def test_food_places_include_generic_resource_locations(database, world) -> None:
    """存在通用 food profile 时，所有有存量的地点都应被视为可用餐地点。"""
    from world_engine.daily_life import DailyLifeService

    with database.write() as connection:
        registration_id = make_registration(connection, world)
        EconomyService.register_item(
            connection,
            world,
            registration_id,
            CommoditySpec(
                name="通用粮食", category="food", nutrition=20,
                resource_key="food", initial_resource=0,
                daily_growth=5, resource_capacity=200,
            ),
            utc_now(),
        )
        place_id = connection.execute(
            "SELECT id FROM locations WHERE world_id=? LIMIT 1", (world,)
        ).fetchone()["id"]
        connection.execute(
            "UPDATE locations SET resources_json=? WHERE id=?", ('{"food": 30}', place_id)
        )
        actor_id = connection.execute(
            "SELECT id FROM characters WHERE world_id=? LIMIT 1", (world,)
        ).fetchone()["id"]
        places = DailyLifeService.food_places(
            connection, world, [place_id], actor_id=actor_id
        )
    assert places == [place_id]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -k food_places -v`
Expected: FAIL，`AttributeError: type object 'DailyLifeService' has no attribute 'food_places'`

- [ ] **Step 3: 实现**

在 `world_engine/daily_life.py` 的 `DailyLifeService` 类中新增静态方法：

```python
    @staticmethod
    def food_places(
        c, world_id: str, candidate_ids: list[str], *, actor_id: str
    ) -> list[str]:
        """从候选地点里筛出当前可获得食物的地点，返回地点 id。

        判定规则与原实现一致，额外支持通用资源：
        - 地点绑定了 food profile（`resource_location_id` 精确匹配，或属于该角色）
        - 或存在通用 food profile（`resource_location_id IS NULL`，任何地点可采）
        两种情况都还要求该地点当前 `resources_json.food > 0`。
        """
        if not candidate_ids:
            return []
        placeholders = ",".join("?" for _ in candidate_ids)
        bound_rows = c.execute(
            f"SELECT resource_location_id FROM world_item_profiles "
            f"WHERE world_id=? AND resource_key='food' AND nutrition>0 "
            f"AND (resource_owner_id IS NULL OR resource_owner_id=?) "
            f"AND resource_location_id IS NOT NULL "
            f"AND resource_location_id IN ({placeholders})",  # noqa: S608 - 占位符按候选数生成
            (world_id, actor_id, *candidate_ids),
        ).fetchall()
        has_generic = bool(
            c.execute(
                "SELECT 1 FROM world_item_profiles WHERE world_id=? AND resource_key='food' "
                "AND nutrition>0 AND (resource_owner_id IS NULL OR resource_owner_id=?) "
                "AND resource_location_id IS NULL",
                (world_id, actor_id),
            ).fetchone()
        )
        bound = {row["resource_location_id"] for row in bound_rows}
        result: list[str] = []
        for place_id in candidate_ids:
            if place_id not in bound and not has_generic:
                continue
            row = c.execute(
                "SELECT resources_json FROM locations WHERE id=?", (place_id,)
            ).fetchone()
            if row is None:
                continue
            if int(json.loads(row["resources_json"] or "{}").get("food", 0)) > 0:
                result.append(place_id)
        return result
```

然后把 `world_engine/daily_life.py:288-304` 的 `food_places` 内联构造替换为：

```python
                        food_places = [
                            by_id[place_id]
                            for place_id in cls.food_places(
                                c, wid, [loc["id"] for loc in local_places], actor_id=npc["id"]
                            )
                        ]
```

> 依据：`locations` 是 `c.execute(...).fetchall()`（`daily_life.py:176`），
> `by_id = {row["id"]: row for row in locations}`（`:179`），`local_places` 是 `locations` 的子集，
> 因此 `by_id[place_id]` 必定命中，且后续 `food_places` 需要一个 `sqlite3.Row` 列表
> （下游用 `loc["longitude"]` 与 `min(..., key=...)`）。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -v`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add tests/test_food_supply.py world_engine/daily_life.py
git commit -m "feat(daily_life): 用餐地点候选支持通用食物资源"
```

---

### Task 6: 新建 food_supply 播种模块

**Files:**
- Create: `world_engine/food_supply.py`
- Test: `tests/test_food_supply.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_food_supply.py`：

```python
def test_seed_writes_food_stock_to_every_town(database, world) -> None:
    """播种后每个 city/town 都应有 food 存量。"""
    from world_engine.food_supply import seed_food_supply

    with database.write() as connection:
        stats = seed_food_supply(connection, world)
        without = connection.execute(
            "SELECT COUNT(*) FROM locations WHERE world_id=? AND kind IN ('city','town') "
            "AND COALESCE(json_extract(resources_json,'$.food'),0) <= 0",
            (world,),
        ).fetchone()[0]
    assert stats.locations_seeded > 0
    assert without == 0


def test_seed_is_idempotent(database, world) -> None:
    """重复播种不产生重复 profile，也不改变已有存量。"""
    from world_engine.food_supply import seed_food_supply

    with database.write() as connection:
        seed_food_supply(connection, world)
        first = connection.execute(
            "SELECT COUNT(*) FROM world_item_profiles WHERE world_id=? AND resource_key='food'",
            (world,),
        ).fetchone()[0]
    with database.write() as connection:
        seed_food_supply(connection, world)
        second = connection.execute(
            "SELECT COUNT(*) FROM world_item_profiles WHERE world_id=? AND resource_key='food'",
            (world,),
        ).fetchone()[0]
    assert first == second == 1
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -k seed -v`
Expected: FAIL，`ModuleNotFoundError: No module named 'world_engine.food_supply'`

- [ ] **Step 3: 实现**

创建 `world_engine/food_supply.py`：

```python
"""城镇食物资源的播种：让世界具备可采集的食物供给。

背景：世界的经济闭环曾在资源供给侧断裂——地点没有任何可采集资源，
物品 profile 的 resource_location_id 全为空且 daily_growth 为 0，
导致 `EconomyService.harvest` 恒返回 False，NPC 一旦耗尽初始饱食度
就再也无法进食，进而陷入「饿 → 无法工作 → 没钱 → 更饿」的死锁。

本模块提供幂等的播种：一个通用食物 profile（resource_location_id 为
NULL，表示任何地点均可采集）加每个城镇的食物存量。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from world_engine.repository import to_iso, utc_now

FOOD_ITEM_NAME = "粮食"
FOOD_RESOURCE_KEY = "food"
FOOD_NUTRITION = 20
FOOD_PRICE = 3
FOOD_DAILY_GROWTH = 5
FOOD_RESOURCE_CAPACITY = 200

MIN_STOCK = 10
MAX_STOCK = 100
STOCK_PER_POPULATION = 100


@dataclass(frozen=True, slots=True)
class FoodSupplyStats:
    """一次播种的结果。"""

    item_type_id: str
    locations_seeded: int
    profile_created: bool


def food_stock_for(population: int) -> int:
    """按人口规模折算一座城镇的食物存量。

    单个 NPC 每天消耗 72 点饱食度（每小时 3 点 × 24），一份食物恢复 42 点，
    即每人每天约需 1.7 份；因此 100 人对应 1 份的存量足以支撑多日采集。
    """
    return max(MIN_STOCK, min(MAX_STOCK, round(population / STOCK_PER_POPULATION)))


def _population_of(resources_json: str | None) -> int:
    try:
        return int(json.loads(resources_json or "{}").get("population", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0


def _ensure_registration(connection: sqlite3.Connection, world_id: str) -> str:
    """通用资源需要一个 registration_id（外键非空），幂等地造一条系统登记。"""
    key = f"system:food-supply:{world_id}"
    existing = connection.execute(
        "SELECT id FROM element_registration_requests WHERE world_id=? AND idempotency_key=?",
        (world_id, key),
    ).fetchone()
    if existing is not None:
        return str(existing["id"])
    event_id = str(uuid4())
    now = to_iso(utc_now())
    connection.execute(
        """
        INSERT INTO world_events(
            id, world_id, tick_id, occurred_at, event_type, summary,
            importance, payload_json, created_at
        ) VALUES (?, ?, ?, ?, 'world.food_supply_initialized', '世界的基础食物供给已就位。',
                  'routine', '{}', ?)
        """,
        (event_id, world_id, str(uuid4()), now, now),
    )
    registration_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO element_registration_requests(
            id, world_id, element_type, source_event_id, idempotency_key,
            status, payload_json, created_at, updated_at
        ) VALUES (?, ?, 'commodity', ?, ?, 'applied', '{}', ?, ?)
        """,
        (registration_id, world_id, event_id, key, now, now),
    )
    return registration_id


def _ensure_generic_profile(connection: sqlite3.Connection, world_id: str) -> tuple[str, bool]:
    """确保存在通用食物 profile，返回 (item_type_id, 是否新建)。"""
    existing = connection.execute(
        "SELECT item_type_id FROM world_item_profiles "
        "WHERE world_id=? AND resource_key=? AND resource_location_id IS NULL",
        (world_id, FOOD_RESOURCE_KEY),
    ).fetchone()
    if existing is not None:
        return str(existing["item_type_id"]), False

    registration_id = _ensure_registration(connection, world_id)
    item_type_id = str(uuid4())
    connection.execute(
        "INSERT INTO item_types(id,name,category,stack_limit,slot_size,usable) "
        "VALUES (?,?,'food',10,1,1)",
        (item_type_id, FOOD_ITEM_NAME),
    )
    connection.execute(
        "INSERT INTO world_item_profiles VALUES (?,?,?,?,?,NULL,?,NULL,?,?,?)",
        (
            world_id,
            item_type_id,
            registration_id,
            FOOD_PRICE,
            FOOD_NUTRITION,
            FOOD_RESOURCE_KEY,
            FOOD_DAILY_GROWTH,
            FOOD_RESOURCE_CAPACITY,
            to_iso(utc_now()),
        ),
    )
    return item_type_id, True


def seed_food_supply(connection: sqlite3.Connection, world_id: str) -> FoodSupplyStats:
    """幂等地为世界补齐通用食物 profile 与各城镇的食物存量。"""
    item_type_id, created = _ensure_generic_profile(connection, world_id)
    seeded = 0
    rows = connection.execute(
        "SELECT id, resources_json FROM locations "
        "WHERE world_id=? AND kind IN ('city','town') AND is_active=1",
        (world_id,),
    ).fetchall()
    for row in rows:
        resources = json.loads(row["resources_json"] or "{}")
        stock = food_stock_for(_population_of(row["resources_json"]))
        if int(resources.get(FOOD_RESOURCE_KEY, 0)) >= stock:
            continue
        resources[FOOD_RESOURCE_KEY] = stock
        connection.execute(
            "UPDATE locations SET resources_json=? WHERE id=? AND world_id=?",
            (json.dumps(resources, ensure_ascii=False), row["id"], world_id),
        )
        seeded += 1
    return FoodSupplyStats(
        item_type_id=item_type_id, locations_seeded=seeded, profile_created=created
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -v`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add tests/test_food_supply.py world_engine/food_supply.py
git commit -m "feat: 新增幂等的城镇食物供给播种模块"
```

---

### Task 7: 接入迁移，已有世界自动补齐

**Files:**
- Modify: `world_engine/migrations.py`
- Modify: `world_engine/database.py`（`initialize()` 调用点）
- Test: `tests/test_food_supply.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_food_supply.py`：

```python
def test_initialize_backfills_food_supply(database, world) -> None:
    """已存在的世界在 initialize 后应自动获得食物供给。"""
    with database.write() as connection:
        connection.execute("UPDATE locations SET resources_json='{}' WHERE world_id=?", (world,))
        connection.execute(
            "DELETE FROM world_item_profiles WHERE world_id=? AND resource_key='food'", (world,)
        )
    database.initialize()
    with database.read() as connection:
        profiles = connection.execute(
            "SELECT COUNT(*) FROM world_item_profiles "
            "WHERE world_id=? AND resource_key='food' AND resource_location_id IS NULL",
            (world,),
        ).fetchone()[0]
        stock = connection.execute(
            "SELECT COUNT(*) FROM locations WHERE world_id=? "
            "AND COALESCE(json_extract(resources_json,'$.food'),0) > 0",
            (world,),
        ).fetchone()[0]
    assert profiles == 1
    assert stock > 0
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -k backfills -v`
Expected: FAIL（profiles == 0）

- [ ] **Step 3: 实现**

在 `world_engine/migrations.py` 末尾追加：

```python
def backfill_food_supply(connection: sqlite3.Connection) -> None:
    """为所有已有世界补齐食物供给（幂等）。

    世界的食物资源曾在资源供给侧整体缺失，导致 NPC 饿死后无法恢复。
    此迁移在一次 initialize 内即可修复全部存量世界。
    """
    from world_engine.food_supply import seed_food_supply

    for row in connection.execute("SELECT id FROM worlds").fetchall():
        seed_food_supply(connection, str(row["id"]))
```

在 `world_engine/database.py` 的 `initialize()` 中，紧跟 `initialize_retention(connection)` 之后加入：

```python
            migrations.backfill_food_supply(connection)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -v`
Expected: 10 passed

- [ ] **Step 5: 提交**

```bash
git add tests/test_food_supply.py world_engine/migrations.py world_engine/database.py
git commit -m "feat(migrations): 已有世界自动补齐食物供给"
```

---

### Task 8: 接入 Noryia seeder

**Files:**
- Modify: `world_engine/noryia_seeder.py`
- Test: `tests/test_food_supply.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_food_supply.py`：

```python
def test_noryia_world_gets_food_supply_on_creation(database) -> None:
    """新建的 Noryia 世界应自带食物供给，无需依赖迁移。"""
    from world_engine.noryia_seeder import create_noryia_world

    world_id = create_noryia_world(database)
    with database.read() as connection:
        profiles = connection.execute(
            "SELECT COUNT(*) FROM world_item_profiles "
            "WHERE world_id=? AND resource_key='food' AND resource_location_id IS NULL",
            (world_id,),
        ).fetchone()[0]
        with_food = connection.execute(
            "SELECT COUNT(*) FROM locations WHERE world_id=? "
            "AND COALESCE(json_extract(resources_json,'$.food'),0) > 0",
            (world_id,),
        ).fetchone()[0]
    assert profiles == 1
    assert with_food > 0
```

> 该测试依赖 `docs/worldbuilding/maps/map_new/data/` 下的 CSV；若本地缺失，跳过而非失败——与仓库中其他 Noryia 测试保持一致的跳过策略。

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -k noryia -v`
Expected: FAIL（profiles == 0）

- [ ] **Step 3: 实现**

在 `world_engine/noryia_seeder.py` 的 `create_noryia_world` 体内，
`Database._ensure_noryia_city_detail_maps(connection)` 之后加入：

```python
        from world_engine.food_supply import seed_food_supply

        seed_food_supply(connection, world_id)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -v`
Expected: 11 passed

- [ ] **Step 5: 提交**

```bash
git add tests/test_food_supply.py world_engine/noryia_seeder.py
git commit -m "feat(seeder): Noryia 新世界创建时同步播种食物供给"
```

---

### Task 9: CLI 维护入口

**Files:**
- Modify: `world_engine/cli.py`
- Test: `tests/test_food_supply.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_food_supply.py`：

```python
def test_cli_registers_seed_food_supply_command() -> None:
    """CLI 应注册 seed-food-supply 子命令并接收 world_id。

    播种逻辑本身已由 Task 6 的单元测试覆盖，此处只验证命令可被正确解析，
    避免为一个薄封装去 mock 整个 CLI 运行时。
    """
    from world_engine.cli import build_parser

    args = build_parser().parse_args(["seed-food-supply", "some-world-id"])
    assert args.command == "seed-food-supply"
    assert args.world_id == "some-world-id"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -k cli -v`
Expected: FAIL，argparse 报 `invalid choice: 'seed-food-supply'`

- [ ] **Step 3: 实现**

在 `world_engine/cli.py` 的 `build_parser()` 中，`adjudications` 之后加入：

```python
    food = subparsers.add_parser(
        "seed-food-supply", help="为世界补齐通用食物资源与各城镇食物存量"
    )
    food.add_argument("world_id")
```

在 `main()` 中，`adjudications` 分支之后加入：

```python
    if args.command == "seed-food-supply":
        from world_engine.food_supply import seed_food_supply

        with database.write() as connection:
            stats = seed_food_supply(connection, args.world_id)
        _print_json(
            {
                "world_id": args.world_id,
                "item_type_id": stats.item_type_id,
                "profile_created": stats.profile_created,
                "locations_seeded": stats.locations_seeded,
            }
        )
        return 0
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_food_supply.py -v`
Expected: 12 passed

- [ ] **Step 5: 提交**

```bash
git add tests/test_food_supply.py world_engine/cli.py
git commit -m "feat(cli): 新增 seed-food-supply 维护命令"
```

---

### Task 10: 端到端存活验收

**Files:**
- Create: `tests/test_npc_survival.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_npc_survival.py`：

```python
"""端到端验收：身无分文的 NPC 能靠采集活下来。

这是本次修复的核心验收——修复前，NPC 会陷入
「饿 → 无法工作 → 没钱 → 更饿」的死锁，饱食度永久为 0。
"""

from __future__ import annotations

from dataclasses import replace

from world_engine.engine import WorldEngine
from world_engine.food_supply import seed_food_supply
from world_engine.repository import WorldRepository
from world_engine.seeder import create_iserra_world


def test_impoverished_npc_does_not_starve(database, settings) -> None:
    """穷光蛋 NPC 在推进多轮后饱食度应回升，且能吃到东西。"""
    world_id = create_iserra_world(database)
    with database.write() as connection:
        seed_food_supply(connection, world_id)
        # 制造最坏情况：所有 NPC 身无分文且饿到极限。
        connection.execute(
            "UPDATE characters SET money=0, satiety=0 WHERE world_id=? AND is_player=0",
            (world_id,),
        )
    engine = WorldEngine(database, replace(settings, world_agent_enabled=False))
    for _ in range(20):
        engine.heartbeat(world_id, elapsed_seconds=3600)

    with database.read() as connection:
        satiety = connection.execute(
            "SELECT AVG(satiety) FROM characters WHERE world_id=? AND is_player=0",
            (world_id,),
        ).fetchone()[0]
        ate = connection.execute(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? AND event_type='action.eat'",
            (world_id,),
        ).fetchone()[0]
        harvested = connection.execute(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? "
            "AND event_type='action.resource_harvested'",
            (world_id,),
        ).fetchone()[0]
    assert ate > 0, "NPC 应当吃到过食物"
    assert harvested > 0, "NPC 应当采集过资源"
    assert satiety > 0, f"平均饱食度应回升，实际为 {satiety}"
```

- [ ] **Step 2: 运行测试确认失败**

先用修复前的代码验证它确实能暴露问题：

Run: `git stash && .venv/Scripts/python.exe -m pytest tests/test_npc_survival.py -v; git stash pop`
Expected: FAIL（`ate == 0`、`satiety == 0`）

- [ ] **Step 3: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m pytest tests/test_npc_survival.py -v`
Expected: PASS

- [ ] **Step 4: 跑全量回归**

Run: `.venv/Scripts/python.exe -m pytest -q`
Expected: 全部通过（原 396 项 + 本次新增）

- [ ] **Step 5: 提交**

```bash
git add tests/test_npc_survival.py
git commit -m "test: 端到端验收零资产 NPC 能靠采集存活"
```

---

## 上线步骤（代码合入后）

- [ ] **备份正式库**：`cp data/world.db data/world.db.bak-20260918`
- [ ] **停止服务**（若在运行）
- [ ] **触发迁移**：`python -m world_engine.cli list`（`initialize()` 会自动补齐 4 张缺失表与食物供给）
- [ ] **验证指标**：

```sql
SELECT COUNT(*) FROM world_item_profiles WHERE resource_key='food';        -- 期望 1
SELECT COUNT(*) FROM locations WHERE json_extract(resources_json,'$.food')>0; -- 期望 618
SELECT COUNT(*) FROM npc_life_goals;                                        -- 表应已存在（0 行）
```

- [ ] **重启服务并观察 24 小时**，检查 `action.eat` 与 `action.resource_harvested` 是否持续增长

---

## 验收指标

| 指标 | 修复前 | 目标 |
|---|---|---|
| `action.eat` 事件 | 0 | > 0 且持续增长 |
| `action.resource_harvested` | 3 | 持续增长 |
| NPC 平均饱食度 | 0.0 | > 50 |
| `life_interrupted` 增速 | 与 started 约 1:1 | 显著低于 started |
