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
    """通用资源需要一个 registration_id（外键非空），幂等地造一条系统登记。

    注意：element_registration_requests 的 input_world_version 是
    NOT NULL 且没有默认值，必须显式写入世界当前版本，否则会触发
    NOT NULL constraint failed。
    """
    key = f"system:food-supply:{world_id}"
    existing = connection.execute(
        "SELECT id FROM element_registration_requests WHERE world_id=? AND idempotency_key=?",
        (world_id, key),
    ).fetchone()
    if existing is not None:
        return str(existing["id"])
    event_id = str(uuid4())
    now = to_iso(utc_now())
    row = connection.execute(
        "SELECT version FROM worlds WHERE id=?", (world_id,)
    ).fetchone()
    world_version = int(row["version"]) if row is not None else 0
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
            status, payload_json, input_world_version, created_at, updated_at
        ) VALUES (?, ?, 'commodity', ?, ?, 'applied', '{}', ?, ?, ?)
        """,
        (registration_id, world_id, event_id, key, world_version, now, now),
    )
    return registration_id


def _has_generic_profile(connection: sqlite3.Connection, world_id: str) -> bool:
    """世界是否已存在通用食物 profile。"""
    row = connection.execute(
        "SELECT 1 FROM world_item_profiles "
        "WHERE world_id=? AND resource_key=? AND resource_location_id IS NULL",
        (world_id, FOOD_RESOURCE_KEY),
    ).fetchone()
    return row is not None


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
    """幂等地为世界补齐通用食物 profile 与各城镇的食物存量。

    存量只在「本世界从未播种过」时写入一次；之后完全交由 NPC 采集与
    `EconomyService.tick` 的每日再生管理。这一点至关重要：本函数会被
    `backfill_food_supply()` 在每次 `Database.initialize()`（即每次服务启动）
    调用，如果每次都把 stock 写回去，就等于每次重启都把全世界的食物拉满，
    直接抹掉「采集 → 消耗」的经济机制。

    「已播种」的判据是「通用 profile 是否已存在」，必须在 `_ensure_generic_profile`
    之前取——后者会顺带写下 `system:food-supply:<world_id>` 登记，若拿登记记录
    当判据，首次播种自己就会把自己判成「已播种」，存量永远写不进去。
    """
    already_seeded = _has_generic_profile(connection, world_id)
    item_type_id, created = _ensure_generic_profile(connection, world_id)
    if already_seeded:
        # 已播种过：既不重复写 profile，也不碰任何存量。
        return FoodSupplyStats(
            item_type_id=item_type_id, locations_seeded=0, profile_created=created
        )
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
