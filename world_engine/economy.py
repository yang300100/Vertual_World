"""真实库存、食物、资源补给和工资账户组成的日常生计规则。"""

import json
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from world_engine.inventory import InventoryError, InventoryService
from world_engine.life import LifeSceneService
from world_engine.repository import from_iso, to_iso

# 一个 NPC 每日维持基本生存所需的资源份数（按饱食度消耗 72 点/日、每份恢复 42 点折算）。
NPC_DAILY_RESOURCE_NEED = 2


class CommoditySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    element_type: Literal["commodity"] = "commodity"
    name: str = Field(min_length=1, max_length=60)
    category: Literal["food", "material", "tool", "map"]
    stack_limit: int = Field(default=10, ge=1, le=100, strict=True)
    slot_size: int = Field(default=1, ge=1, le=10, strict=True)
    price: int = Field(default=3, ge=1, le=100000, strict=True)
    nutrition: int = Field(default=20, ge=0, le=60, strict=True)
    shelf_life_hours: int | None = Field(default=None, ge=1, le=8760, strict=True)
    resource_location_id: str | None = None
    resource_key: str | None = Field(default=None, max_length=40)
    resource_owner_id: str | None = None
    initial_resource: int = Field(default=0, ge=0, le=10000, strict=True)
    daily_growth: int = Field(default=0, ge=0, le=100, strict=True)
    resource_capacity: int = Field(default=100, ge=1, le=10000, strict=True)


class WorkplaceBudgetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    element_type: Literal["workplace_budget"] = "workplace_budget"
    name: str = Field(default="工作场所工资账户", min_length=1, max_length=80)
    location_id: str
    initial_funds: int = Field(ge=0, le=1000000, strict=True)
    wage: int = Field(default=9, ge=1, le=10000, strict=True)


class EconomyService:
    @staticmethod
    def buy_food(c, actor, at):
        return bool(EconomyService.buy_supply(c, actor, at, food_only=True))

    @staticmethod
    def buy_supply(c, actor, at, *, item_type_id=None, minimum_condition=0,
                   budget=None, food_only=False):
        from world_engine.actions import ActionService
        from world_engine.geo import great_circle_distance_km
        from world_engine.proximity import same_room

        actor = c.execute("SELECT * FROM characters WHERE id=?", (actor["id"],)).fetchone()
        if actor["health"] <= 0 or c.execute(
            "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
            (actor["id"],),
        ).fetchone():
            return None
        budget = actor["money"] if budget is None else min(budget, actor["money"])
        for seller in c.execute(
            "SELECT * FROM characters WHERE world_id=? AND id!=? AND health>0 AND is_player=0",
            (actor["world_id"], actor["id"]),
        ).fetchall():
            if (
                not same_room(actor, seller)
                or (actor["current_location_id"] or actor["location_id"])
                != (seller["current_location_id"] or seller["location_id"])
                or c.execute(
                    "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                    (seller["id"],),
                ).fetchone()
                or great_circle_distance_km(
                    actor["longitude"], actor["latitude"], seller["longitude"], seller["latitude"]
                )
                > 0.1
            ):
                continue
            if not any(
                word in (seller["identity"] or "") for word in ("商", "店", "贩", "医", "匠", "厨")
            ):
                continue
            item = c.execute(
                "SELECT i.*,t.name,t.stack_limit,t.slot_size,p.price FROM item_instances i "
                "JOIN item_types t ON t.id=i.item_type_id JOIN world_item_profiles p ON p.item_type_id=i.item_type_id "
                "WHERE i.world_id=? AND p.world_id=i.world_id AND i.container_type='character_inventory' "
                "AND i.container_id=? AND i.owner_character_id=? AND i.quantity>0 "
                "AND (?=0 OR p.nutrition>0) AND (? IS NULL OR i.item_type_id=?) "
                "AND i.condition>=? AND p.price<=? AND (i.spoils_world_time IS NULL OR i.spoils_world_time>?) ORDER BY p.price,i.id LIMIT 1",
                (actor["world_id"], seller["id"], seller["id"], food_only,
                 item_type_id, item_type_id, minimum_condition, budget, to_iso(at)),
            ).fetchone()
            if item is None:
                continue
            before = InventoryService.snapshot(c, actor["world_id"])
            c.execute("SAVEPOINT buy_supply")
            try:
                InventoryService.transfer(c, item, actor["id"], 1)
                c.execute(
                    "UPDATE characters SET money=money-? WHERE id=?", (item["price"], actor["id"])
                )
                c.execute(
                    "UPDATE characters SET money=money+? WHERE id=?", (item["price"], seller["id"])
                )
                event = ActionService._record_event(
                    c,
                    world_id=actor["world_id"],
                    tick_id=str(uuid4()),
                    occurred_at=at,
                    event_type="action.trade",
                    actor_id=actor["id"],
                    target_id=seller["id"],
                    location_id=actor["current_location_id"] or actor["location_id"],
                    summary=f"{actor['name']}购买了一份{item['name']}。",
                    payload={"item_id": item["id"], "quantity": 1, "total": item["price"]},
                )
                InventoryService.audit(c, actor["world_id"], event, before)
            except Exception as exc:
                c.execute("ROLLBACK TO SAVEPOINT buy_supply")
                c.execute("RELEASE SAVEPOINT buy_supply")
                if isinstance(exc, InventoryError):
                    return None
                raise
            c.execute("RELEASE SAVEPOINT buy_supply")
            return {"spent": item["price"], "source_event_id": event}
        return None

    @staticmethod
    def register_item(c, wid, rid, spec, at):
        if spec.shelf_life_hours is not None and spec.category != "food":
            raise ValueError("只有食物可登记保鲜期限")
        if c.execute(
            "SELECT 1 FROM world_item_profiles p JOIN item_types t ON t.id=p.item_type_id WHERE p.world_id=? AND t.name=?",
            (wid, spec.name),
        ).fetchone():
            raise ValueError("当前世界已登记同名物品类型")
        # 通用资源：resource_key 有值而 resource_location_id 为 None，表示任何地点均可采集。
        # 两者同时为空是合法的「无资源来源」物品。
        if spec.resource_location_id and not spec.resource_key:
            raise ValueError("指定资源地点时必须同时填写资源名")
        if spec.initial_resource > spec.resource_capacity:
            raise ValueError("初始资源不能超过储量上限")
        if (
            spec.resource_owner_id
            and not c.execute(
                "SELECT 1 FROM characters WHERE id=? AND world_id=?", (spec.resource_owner_id, wid)
            ).fetchone()
        ):
            raise ValueError("资源所有者不属于当前世界")
        if spec.resource_location_id:
            if c.execute("SELECT 1 FROM world_item_profiles WHERE world_id=? AND resource_location_id=? AND resource_key=?",(wid,spec.resource_location_id,spec.resource_key)).fetchone():
                raise ValueError("同一地点的资源来源已登记，不能重复定义补给或所有者")
            loc = c.execute(
                "SELECT resources_json FROM locations WHERE id=? AND world_id=? AND is_active=1",
                (spec.resource_location_id, wid),
            ).fetchone()
            if loc is None:
                raise ValueError("资源地点不存在")
            resources = json.loads(loc["resources_json"] or "{}")
            if spec.resource_key in resources and int(resources[spec.resource_key]) > spec.resource_capacity:
                raise ValueError("登记上限不能低于现有储量，请先核对已有资源")
            if spec.resource_key not in resources:
                resources[spec.resource_key] = spec.initial_resource
            c.execute(
                "UPDATE locations SET resources_json=? WHERE id=?",
                (json.dumps(resources, ensure_ascii=False), spec.resource_location_id),
            )
        iid = str(uuid4())
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit,slot_size,usable) VALUES (?,?,?,?,?,?)",
            (
                iid,
                spec.name,
                spec.category,
                spec.stack_limit,
                spec.slot_size,
                int(spec.category == "food"),
            ),
        )
        c.execute(
            "INSERT INTO world_item_profiles VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                wid,
                iid,
                rid,
                spec.price,
                spec.nutrition if spec.category == "food" else 0,
                spec.resource_location_id,
                spec.resource_key,
                spec.resource_owner_id,
                spec.daily_growth,
                spec.resource_capacity,
                to_iso(at),
            ),
        )
        if spec.shelf_life_hours is not None:
            c.execute("INSERT INTO food_storage_rules VALUES (?,?,?)", (iid, wid, spec.shelf_life_hours))
        return iid

    @staticmethod
    def register_budget(c, wid, rid, spec):
        if not c.execute(
            "SELECT 1 FROM locations WHERE id=? AND world_id=? AND is_active=1",
            (spec.location_id, wid),
        ).fetchone():
            raise ValueError("需要有效地点；确认工资登记后该地点才可提供工作")
        if c.execute(
            "SELECT 1 FROM workplace_accounts WHERE location_id=?", (spec.location_id,)
        ).fetchone():
            raise ValueError("该工作场所已有工资账户，不能重复初始化资金")
        c.execute(
            "INSERT INTO workplace_accounts VALUES (?,?,?,?,?)",
            (wid, spec.location_id, rid, spec.initial_funds, spec.wage),
        )
        return spec.location_id

    @staticmethod
    def food(c, actor, event_id=None, *, at=None):
        from world_engine.food import FoodService

        at = at or from_iso(c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (actor["world_id"],)).fetchone()[0])
        item = c.execute(
            "SELECT i.*,p.nutrition,t.name FROM item_instances i JOIN world_item_profiles p ON p.item_type_id=i.item_type_id "
            "JOIN item_types t ON t.id=i.item_type_id WHERE i.world_id=? AND i.container_id=? AND i.container_type='character_inventory' "
            "AND i.owner_character_id=? AND p.nutrition>0 AND i.quantity>0 "
            "AND (i.spoils_world_time IS NULL OR i.spoils_world_time>?) "
            "ORDER BY i.spoils_world_time IS NULL,i.spoils_world_time,i.id LIMIT 1",
            (actor["world_id"], actor["id"], actor["id"], to_iso(at)),
        ).fetchone()
        if item is None:
            return None
        effect = FoodService.eat(c, actor, item, at)
        return f"{actor['name']}吃了一份{item['name']}，补充{effect['nutrition']}点饱食。"

    @staticmethod
    def collect_outputs(c, actor, at):
        from world_engine.actions import ActionService

        loc = LifeSceneService.ground_location(c, actor)
        if loc is None:
            return
        rows = c.execute(
            "SELECT i.*,t.name,t.stack_limit,t.slot_size FROM item_instances i JOIN item_types t ON t.id=i.item_type_id JOIN character_life_activities a ON a.id=i.container_id JOIN activity_task_details d ON d.activity_id=a.id WHERE i.world_id=? AND i.owner_character_id=? AND i.container_type='activity_output' AND a.location_id=? AND d.room_id IS ?",
            (actor["world_id"], actor["id"], loc["id"], actor["current_room_id"]),
        ).fetchall()
        for item in rows:
            before = InventoryService.snapshot(c, actor["world_id"])
            try:
                InventoryService.transfer(c, item, actor["id"], item["quantity"])
            except InventoryError:
                continue
            eid = ActionService._record_event(
                c,
                world_id=actor["world_id"],
                tick_id=str(uuid4()),
                occurred_at=at,
                event_type="action.stock_collected",
                actor_id=actor["id"],
                target_id=None,
                location_id=loc["id"],
                summary=f"{actor['name']}收取了已完成的{item['name']}。",
                payload={"item_id": item["id"]},
            )
            InventoryService.audit(c, actor["world_id"], eid, before)

    @staticmethod
    def harvest(c, actor, at, *, food_only=False, item_type_ids=None, return_event=False):
        from world_engine.actions import ActionService

        loc = LifeSceneService.ground_location(c, actor)
        if loc is None or actor["current_room_id"]:
            return False
        for profile in c.execute(
            "SELECT p.*,t.name,t.stack_limit,t.slot_size FROM world_item_profiles p JOIN item_types t ON t.id=p.item_type_id "
            "WHERE p.world_id=? AND (p.resource_location_id=? OR p.resource_location_id IS NULL) "
            "ORDER BY p.resource_location_id IS NULL",
            (actor["world_id"], loc["id"]),
        ).fetchall():
            if item_type_ids is not None and profile["item_type_id"] not in item_type_ids:
                continue
            if food_only and profile["nutrition"] <= 0:
                continue
            if profile["resource_owner_id"] not in {None, actor["id"]}:
                continue
            resources = json.loads(
                c.execute(
                    "SELECT resources_json FROM locations WHERE id=?", (loc["id"],)
                ).fetchone()[0]
            )
            if int(resources.get(profile["resource_key"], 0)) <= 0:
                continue
            if actor["energy"] < 3:
                return False
            before = InventoryService.snapshot(c, actor["world_id"])
            iid = str(uuid4())
            c.execute(
                "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,quantity,condition,owner_character_id) VALUES (?,?,?,'resource_pickup',?,1,100,?)",
                (iid, actor["world_id"], profile["item_type_id"], actor["id"], actor["id"]),
            )
            from world_engine.food import FoodService

            FoodService.stamp(c, iid, profile["item_type_id"], at)
            item = c.execute("SELECT i.*,t.stack_limit,t.slot_size FROM item_instances i JOIN item_types t ON t.id=i.item_type_id WHERE i.id=?", (iid,)).fetchone()
            try:
                InventoryService.transfer(c, item, actor["id"], 1)
            except InventoryError:
                c.execute("DELETE FROM item_instances WHERE id=?", (iid,))
                return False
            resources[profile["resource_key"]] -= 1
            c.execute(
                "UPDATE locations SET resources_json=? WHERE id=?",
                (json.dumps(resources, ensure_ascii=False), loc["id"]),
            )
            c.execute("UPDATE characters SET energy=energy-3 WHERE id=?", (actor["id"],))
            eid = ActionService._record_event(
                c,
                world_id=actor["world_id"],
                tick_id=str(uuid4()),
                occurred_at=at,
                event_type="action.resource_harvested",
                actor_id=actor["id"],
                target_id=None,
                location_id=loc["id"],
                summary=f"{actor['name']}收取了一份{profile['name']}。",
                payload={"item_type_id": profile["item_type_id"], "quantity": 1},
            )
            InventoryService.audit(c, actor["world_id"], eid, before)
            return eid if return_event else True
        return False

    @staticmethod
    def tick(c, wid, at):
        from world_engine.actions import ActionService

        rows = c.execute(
            "SELECT * FROM world_item_profiles WHERE world_id=? AND daily_growth>0",
            (wid,),
        ).fetchall()
        # 各地点在场的 NPC 数：通用资源的再生量据此缩放，避免多人聚集的城镇被采空。
        in_place = dict(
            c.execute(
                "SELECT location_id, COUNT(*) FROM characters "
                "WHERE world_id=? AND is_player=0 GROUP BY location_id",
                (wid,),
            ).fetchall()
        )
        for row in rows:
            elapsed = (at - from_iso(row["last_growth_world_time"])).days
            if elapsed <= 0:
                continue
            if row["resource_location_id"] is None:
                # 通用资源：对每个活跃地点各自再生（上限按单点计算）。
                targets = c.execute(
                    "SELECT id, name, resources_json FROM locations "
                    "WHERE world_id=? AND is_active=1",
                    (wid,),
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
                if row["resource_location_id"] is None:
                    # 通用资源：按该地点的实际人口缩放。一个 NPC 每天约需 2 份
                    # （消耗 72 点饱食度、每份恢复 42 点）。取 daily_growth 与需求量
                    # 的较大者，保证稀疏地区仍有基础再生，而聚集城镇不会被采空。
                    demand = in_place.get(loc["id"], 0) * NPC_DAILY_RESOURCE_NEED
                    growth = max(row["daily_growth"], demand)
                else:
                    # 地点专属资源（如某个矿点的木材）与人口无关，保持原有再生速度。
                    growth = row["daily_growth"]
                after = min(row["resource_capacity"], before + elapsed * growth)
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
