"""有耗时的工作、制作和修理，材料预留与成果交付均由规则执行。"""

from __future__ import annotations

import json
import math
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from world_engine.inventory import InventoryService
from world_engine.life import LifeActivityError, LifeActivityService
from world_engine.repository import to_iso, utc_now


class RecipeIngredient(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_type_id: str
    quantity: int = Field(ge=1, le=100, strict=True)


class RecipeTool(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_type_id: str
    wear: int = Field(default=1, ge=1, le=100, strict=True)


class ActivityRecipeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    element_type: Literal["activity_recipe"] = "activity_recipe"
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["craft", "repair"]
    location_id: str
    duration_minutes: int = Field(ge=5, le=480, strict=True)
    energy_cost: int = Field(default=12, ge=1, le=50, strict=True)
    ingredients: list[RecipeIngredient] = Field(min_length=1, max_length=8)
    tools: list[RecipeTool] = Field(default_factory=list, max_length=8)
    output_item_type_id: str | None = None
    output_quantity: int = Field(default=1, ge=1, le=100, strict=True)
    repair_category: str | None = Field(default=None, max_length=40)
    repair_points: int = Field(default=20, ge=1, le=50, strict=True)
    check_skill: str = Field(default="修理", min_length=1, max_length=40)
    check_difficulty: int = Field(default=40, ge=0, le=100, strict=True)
    check_min_proficiency: int = Field(default=0, ge=0, le=100, strict=True)
    check_tool_type_id: str | None = None


class TaskService:
    @staticmethod
    def view(connection, row, at):
        result = LifeActivityService.view(row, at)
        detail = connection.execute(
            "SELECT spec_json,result_json FROM activity_task_details WHERE activity_id=?",
            (row["id"],),
        ).fetchone()
        if detail:
            spec = json.loads(detail["spec_json"])
            result["title"] = spec["name"]
            result["task_plan"] = {
                key: value
                for key, value in spec.items()
                if key
                not in {
                    "reserved_items",
                    "target_item_id",
                    "check_tool_item_id",
                    "check_attempt_id",
                    "reserved_tools",
                }
            }
            result["result"] = json.loads(detail["result_json"])
        return result

    @staticmethod
    def register(connection, world_id, registration_id, spec):
        if (
            connection.execute(
                "SELECT 1 FROM locations WHERE id=? AND world_id=? AND is_active=1",
                (spec.location_id, world_id),
            ).fetchone()
            is None
        ):
            raise LifeActivityError("配方地点不属于当前世界")
        ids = [item.item_type_id for item in spec.ingredients]
        if len(ids) != len(set(ids)):
            raise LifeActivityError("相同原料请合并数量")
        tool_ids = [item.item_type_id for item in spec.tools]
        if len(tool_ids) != len(set(tool_ids)):
            raise LifeActivityError("同一种工具只需声明一次")
        if set(ids) & set(tool_ids):
            raise LifeActivityError("工具不能同时作为本配方消耗的原料")
        if spec.check_tool_type_id:
            if spec.check_tool_type_id in ids:
                raise LifeActivityError("检定工具不能同时作为本配方消耗的原料")
            ids.append(spec.check_tool_type_id)
        ids.extend(tool_ids)
        if spec.kind == "craft":
            if not spec.output_item_type_id:
                raise LifeActivityError("制作配方必须明确产物类型")
            ids.append(spec.output_item_type_id)
        elif not spec.repair_category or spec.output_item_type_id:
            raise LifeActivityError("修理配方须明确目标物品类别，不能额外生成新物品")
        for iid in ids:
            if connection.execute("SELECT 1 FROM item_types WHERE id=?", (iid,)).fetchone() is None:
                raise LifeActivityError("配方必须引用已有物品类型")
        if connection.execute(
            "SELECT 1 FROM activity_recipes WHERE world_id=? AND name=?", (world_id, spec.name)
        ).fetchone():
            raise LifeActivityError("同名配方已经登记")
        rid = str(uuid4())
        connection.execute(
            "INSERT INTO activity_recipes VALUES (?,?,?,?,?)",
            (rid, world_id, registration_id, spec.name, spec.model_dump_json()),
        )
        return rid

    @staticmethod
    def _reserve(connection, actor, item, amount, activity_id):
        if amount == item["quantity"]:
            connection.execute(
                "UPDATE item_instances SET container_type='activity_escrow',container_id=? "
                "WHERE id=?",
                (activity_id, item["id"]),
            )
            return item["id"]
        InventoryService.consume(connection, item, amount)
        iid = str(uuid4())
        connection.execute(
            "INSERT INTO "
            "item_instances(id,world_id,item_type_id,container_type,container_id,quantity,c"
            "ondition,owner_character_id) "
            "VALUES (?,?,?,'activity_escrow',?,?,?,?)",
            (
                iid,
                actor["world_id"],
                item["item_type_id"],
                activity_id,
                amount,
                item["condition"],
                actor["id"],
            ),
        )
        InventoryService.copy_freshness(connection, item, iid)
        return iid

    @staticmethod
    def tool_requirements(spec):
        tools = list(spec.get("tools", []))
        legacy = spec.get("check_tool_type_id") if spec["kind"] == "repair" else None
        if legacy and not any(item["item_type_id"] == legacy for item in tools):
            tools.append({"item_type_id": legacy, "wear": 1})
        return tools

    @classmethod
    def missing_supplies(cls, connection, actor, spec, at=None):
        """只统计本人随身可用物品，储柜、别人的物品和已占用工具不算现货。"""
        missing = []
        from world_engine.repository import from_iso

        at = at or from_iso(connection.execute("SELECT w.current_time FROM worlds w WHERE id=?", (actor["world_id"],)).fetchone()[0])
        requirements = [
            {**tool, "quantity": 1, "minimum_condition": tool["wear"]}
            for tool in cls.tool_requirements(spec)
        ] + [{**item, "minimum_condition": 0} for item in spec["ingredients"]]
        for requirement in requirements:
            quantity = connection.execute(
                "SELECT COALESCE(SUM(quantity),0) FROM item_instances WHERE world_id=? "
                "AND container_type='character_inventory' AND container_id=? "
                "AND owner_character_id=? AND item_type_id=? AND condition>=? "
                "AND (spoils_world_time IS NULL OR spoils_world_time>?)",
                (actor["world_id"], actor["id"], actor["id"],
                 requirement["item_type_id"], requirement["minimum_condition"], to_iso(at)),
            ).fetchone()[0]
            if quantity < requirement["quantity"]:
                missing.append({**requirement, "quantity": requirement["quantity"] - quantity})
        return missing

    @classmethod
    def prepare(
        cls, connection, actor, at, recipe_id=None, target_item_id=None, map_record_id=None
    ):
        from world_engine.life import LifeSceneService

        location = LifeSceneService.ground_location(connection, actor)
        if location is None:
            raise LifeActivityError("请先走近活动地点")
        LifeActivityService.assert_available(connection, actor["id"])
        if (
            actor["health"] <= 0
            or connection.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (actor["id"],),
            ).fetchone()
        ):
            raise LifeActivityError("请先处理身体状态或结束行程，再开始活动")
        from world_engine.action_checks import CheckService

        check_plan = None
        tool = None
        tools = []
        if map_record_id:
            if recipe_id or target_item_id:
                raise LifeActivityError("地图核对与修理配方不能混为同一次活动")
            spec, check_plan = CheckService.map_plan(connection, actor, map_record_id)
        elif recipe_id:
            recipe = connection.execute(
                "SELECT * FROM activity_recipes WHERE id=? AND world_id=?",
                (recipe_id, actor["world_id"]),
            ).fetchone()
            if recipe is None:
                raise LifeActivityError("配方尚未确认登记")
            spec = json.loads(recipe["spec_json"])
            if spec["location_id"] != location["id"]:
                raise LifeActivityError("这份配方需要在登记地点进行")
        else:
            if actor["current_room_id"]:
                raise LifeActivityError("请先离开生活房间，抵达室外登记工作点")
            account=connection.execute("SELECT * FROM workplace_accounts WHERE world_id=? AND location_id=?",(actor["world_id"],location["id"])).fetchone()
            if location["kind"] != "workplace" and account is None:
                raise LifeActivityError("请先抵达工作场所")
            spec = {
                "name": "一小时工作",
                "kind": "work",
                "duration_minutes": 60,
                "energy_cost": 16,
                "satiety_cost": 8,
                "payment": 9,
                "ingredients": [],
            }
            account=connection.execute("SELECT * FROM workplace_accounts WHERE world_id=? AND location_id=?",(actor["world_id"],location["id"])).fetchone()
            if account:
                if account["balance"]<account["wage"]:raise LifeActivityError("工作场所当前没有足够工资预算")
                spec["payment"]=account["wage"]
                spec["wage_account_id"]=location["id"]
        if actor["energy"] < max(20 if spec["kind"] == "work" else 1, spec["energy_cost"]):
            raise LifeActivityError("精力不足以开始这项活动")
        if not actor["is_player"]:
            from world_engine.schedules import ScheduleService
            remaining = ScheduleService.planned_minutes(ScheduleService.assess(connection,actor,at),at)
            if remaining is not None and spec["duration_minutes"] > remaining:
                raise LifeActivityError("这项活动会占用已确认约定的赴约时间")
        target = None
        if spec["kind"] == "repair":
            target = connection.execute(
                "SELECT i.*,t.category FROM item_instances i JOIN item_types t ON "
                "t.id=i.item_type_id "
                "WHERE i.id=? AND i.world_id=? AND i.container_type='character_inventory' "
                "AND i.container_id=? AND i.owner_character_id=?",
                (target_item_id, actor["world_id"], actor["id"], actor["id"]),
            ).fetchone()
            if (
                target is None
                or target["category"] != spec["repair_category"]
                or target["quantity"] != 1
            ):
                raise LifeActivityError("请选择自己背包内符合配方类别的单件物品")
            if target["condition"] >= 100:
                raise LifeActivityError("物品已完好，无需修理")
        reserved = []
        for ingredient in spec["ingredients"]:
            rows = connection.execute(
                "SELECT * FROM item_instances WHERE world_id=? AND "
                "container_type='character_inventory' "
                "AND container_id=? AND owner_character_id=? AND item_type_id=? AND id!=? "
                "AND (spoils_world_time IS NULL OR spoils_world_time>?) "
                "ORDER BY spoils_world_time IS NULL,spoils_world_time,id",
                (
                    actor["world_id"],
                    actor["id"],
                    actor["id"],
                    ingredient["item_type_id"],
                    target_item_id or "",
                    to_iso(at),
                ),
            ).fetchall()
            remaining = ingredient["quantity"]
            for item in rows:
                count = min(remaining, item["quantity"])
                if count:
                    reserved.append((item, count))
                    remaining -= count
            if remaining:
                raise LifeActivityError("所需材料不足，尚未开始活动")
        for requirement in cls.tool_requirements(spec):
            selected = connection.execute(
                    "SELECT * FROM item_instances WHERE world_id=? AND container_type='character_inventory' "
                    "AND container_id=? AND owner_character_id=? AND item_type_id=? "
                    "AND condition>=? AND quantity>0 AND id!=? ORDER BY condition DESC,id LIMIT 1",
                    (actor["world_id"], actor["id"], actor["id"], requirement["item_type_id"],
                     requirement["wear"], target_item_id or ""),
                ).fetchone()
            if selected is None:
                raise LifeActivityError("缺少配方要求的可用工具，或工具完好度不足以完成本次活动")
            tools.append((selected, requirement["wear"]))
            if selected["item_type_id"] == spec.get("check_tool_type_id"):
                tool = selected
        if spec["kind"] == "repair":
            check_plan = CheckService.plan(
                connection,
                actor,
                "repair",
                {
                    "item_id": target["id"],
                    "type": target["item_type_id"],
                    "condition": target["condition"],
                },
                skill_name=spec.get("check_skill", "修理"),
                difficulty=spec.get("check_difficulty", 40),
                minimum=spec.get("check_min_proficiency", 0),
                tool=tool,
                method={
                    **{key: spec[key] for key in (
                        "ingredients", "duration_minutes", "energy_cost", "repair_points"
                    )},
                    "tools": cls.tool_requirements(spec),
                },
            )
            spec["check_preview"] = CheckService.public_plan(check_plan)
        return spec, target, reserved, tools, check_plan

    @classmethod
    def start(cls, connection, actor, at, recipe_id=None, target_item_id=None, map_record_id=None):
        from world_engine.action_checks import CheckService

        spec, target, reserved, tools, check_plan = cls.prepare(
            connection,
            actor,
            at,
            recipe_id,
            target_item_id,
            map_record_id,
        )
        connection.execute("SAVEPOINT start_task")
        try:
            aid = LifeActivityService.start(
                connection, actor, at, spec["kind"], spec["duration_minutes"]
            )
            before = InventoryService.snapshot(connection, actor["world_id"])
            if spec.get("wage_account_id"):
                updated=connection.execute("UPDATE workplace_accounts SET balance=balance-? WHERE location_id=? AND balance>=?",(spec["payment"],spec["wage_account_id"],spec["payment"])).rowcount
                if updated!=1:raise LifeActivityError("工资预算已经发生变化")
            spec["reserved_items"] = [
                cls._reserve(connection, actor, item, count, aid) for item, count in reserved
            ]
            if target is not None:
                spec["target_item_id"] = cls._reserve(connection, actor, target, 1, aid)
            spec["reserved_tools"] = []
            for tool, wear in tools:
                iid = cls._reserve(connection, actor, tool, 1, aid)
                spec["reserved_tools"].append({"item_id": iid, "wear": wear})
                if tool["item_type_id"] == spec.get("check_tool_type_id"):
                    spec["check_tool_item_id"] = iid
            if check_plan:
                spec["check_attempt_id"] = CheckService.begin(connection, actor, aid, check_plan)
            connection.execute(
                "INSERT INTO "
                "activity_task_details(activity_id,room_id,spec_json,before_json) VALUES "
                "(?,?,?,?)",
                (
                    aid,
                    actor["current_room_id"],
                    json.dumps(spec, ensure_ascii=False),
                    json.dumps(before, ensure_ascii=False),
                ),
            )
            connection.execute("RELEASE SAVEPOINT start_task")
            return aid, spec["name"], spec["duration_minutes"]
        except Exception:
            connection.execute("ROLLBACK TO SAVEPOINT start_task")
            connection.execute("RELEASE SAVEPOINT start_task")
            raise

    @staticmethod
    def attach_source(connection, activity_id, world_id, actor_id, event_id):
        detail = connection.execute(
            "SELECT d.* FROM activity_task_details d JOIN character_life_activities a ON "
            "a.id=d.activity_id "
            "WHERE a.id=? AND a.world_id=? AND a.character_id=?",
            (activity_id, world_id, actor_id),
        ).fetchone()
        if detail:
            spec = json.loads(detail["spec_json"])
            if spec.get("check_attempt_id"):
                connection.execute(
                    "UPDATE action_check_attempts SET source_event_id=COALESCE(source_event_id,?) "
                    "WHERE id=? AND activity_id=?",
                    (event_id, spec["check_attempt_id"], activity_id),
                )
        if detail and detail["before_json"] != "{}":
            InventoryService.audit(
                connection, world_id, event_id, json.loads(detail["before_json"])
            )
            connection.execute(
                "UPDATE activity_task_details SET before_json='{}' WHERE activity_id=?",
                (activity_id,),
            )

    @staticmethod
    def progress(connection, row, actor, effective, heartbeat_id):
        from world_engine.repository import from_iso

        detail = connection.execute(
            "SELECT * FROM activity_task_details WHERE activity_id=?", (row["id"],)
        ).fetchone()
        spec = json.loads(detail["spec_json"])
        fraction = min(
            1,
            max(
                0,
                (effective - from_iso(row["started_world_time"])).total_seconds()
                / (spec["duration_minutes"] * 60),
            ),
        )
        energy_steps, satiety_steps = (
            int(spec["energy_cost"] * fraction),
            int(spec.get("satiety_cost", 0) * fraction),
        )
        energy = max(0, actor["energy"] - max(0, energy_steps - detail["energy_paid"]))
        satiety = max(0, actor["satiety"] - max(0, satiety_steps - detail["satiety_paid"]))
        connection.execute(
            "UPDATE characters SET energy=?,satiety=?,updated_at=? WHERE id=?",
            (energy, satiety, to_iso(utc_now()), actor["id"]),
        )
        connection.execute(
            "UPDATE activity_task_details SET energy_paid=?,satiety_paid=? WHERE activity_id=?",
            (energy_steps, satiety_steps, row["id"]),
        )
        changes = {
            key: {"before": actor[key], "after": value, "delta": value - actor[key]}
            for key, value in (("energy", energy), ("satiety", satiety))
            if value != actor[key]
        }
        if changes:
            connection.execute(
                "INSERT INTO "
                "character_state_updates(id,world_id,heartbeat_id,character_id,world_time_b"
                "efore,"
                "world_time_after,changes_json,cause,created_at) VALUES "
                "(?,?,?,?,?,?,?,'activity_effort',?)",
                (
                    str(uuid4()),
                    row["world_id"],
                    heartbeat_id,
                    actor["id"],
                    row["last_processed_world_time"],
                    to_iso(effective),
                    json.dumps(changes),
                    to_iso(utc_now()),
                ),
            )
        return energy > 0 and detail["room_id"] == actor["current_room_id"]

    @staticmethod
    def finish(connection, row, status, event_id, at=None):
        detail = connection.execute(
            "SELECT * FROM activity_task_details WHERE activity_id=?", (row["id"],)
        ).fetchone()
        if detail["result_json"] != "{}":
            return json.loads(detail["result_json"])
        spec = json.loads(detail["spec_json"])
        before = InventoryService.snapshot(connection, row["world_id"])
        result = {
            "name": spec["name"],
            "completed": status == "completed",
            "energy_spent": detail["energy_paid"],
        }
        check = None
        if status == "completed" and spec.get("check_attempt_id"):
            from world_engine.action_checks import CheckService

            check = CheckService.resolve(connection, spec["check_attempt_id"], row["id"], event_id)
            result["check"] = check
            result["completed"] = check["outcome"] == "success"
        outcome = check["outcome"] if check else "success"
        if status == "completed":
            if spec["kind"] == "work":
                connection.execute("UPDATE characters SET money=money+? WHERE id=?",(spec["payment"],row["character_id"]))
                result["payment"] = spec["payment"]
            elif spec["kind"] == "map_review":
                record_id = str(uuid4())
                description = {
                    "success": "已有现场观测的整理核对通过；未观测路段仍未知。",
                    "partial": "完成部分整理，精度与对应关系仍需复核；原始观测保留。",
                    "failure": "本次未能完成可靠核对；原始观测保留，不补造地形或道路。",
                }[outcome]
                connection.execute(
                    "INSERT INTO player_activity_records(id,world_id,player_id,npc_id,source_event_id,"
                    "request_event_id,step_key,title,status,content_json,location_id,created_at) "
                    "VALUES (?,?,?,NULL,?,?,'map_review','地图核对结果',?,?,?,?)",
                    (
                        record_id,
                        row["world_id"],
                        row["character_id"],
                        event_id,
                        spec["map_source_event_id"],
                        "completed" if outcome == "success" else "partial",
                        json.dumps(
                            {
                                "result": description,
                                "observation": spec["observation"],
                                "source_record_id": spec["map_record_id"],
                                "check": check,
                            },
                            ensure_ascii=False,
                        ),
                        row["location_id"],
                        to_iso(utc_now()),
                    ),
                )
                result.update({"map_record_id": record_id, "description": description})
            else:
                # 失败最多损耗每种原料的一半（向上取整），其余材料进入领取区。
                remaining = (
                    {
                        entry["item_type_id"]: (entry["quantity"] + 1) // 2
                        for entry in spec["ingredients"]
                    }
                    if outcome == "failure"
                    else None
                )
                for iid in spec["reserved_items"]:
                    item = connection.execute(
                        "SELECT * FROM item_instances WHERE id=? AND container_type='activity_escrow' AND container_id=?",
                        (iid, row["id"]),
                    ).fetchone()
                    if item is not None:
                        count = (
                            item["quantity"]
                            if remaining is None
                            else min(item["quantity"], remaining[item["item_type_id"]])
                        )
                        if count:
                            InventoryService.consume(connection, item, count)
                            if remaining is not None:
                                remaining[item["item_type_id"]] -= count
                if spec["kind"] == "craft":
                    output_id = str(uuid4())
                    connection.execute(
                        "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,quantity,condition,owner_character_id) "
                        "VALUES (?,?,?,'activity_output',?,?,100,?)",
                        (
                            output_id,
                            row["world_id"],
                            spec["output_item_type_id"],
                            row["id"],
                            spec["output_quantity"],
                            row["character_id"],
                        ),
                    )
                    from world_engine.food import FoodService
                    from world_engine.repository import from_iso

                    FoodService.stamp(connection, output_id, spec["output_item_type_id"], at or from_iso(row["ends_world_time"]))
                else:
                    points = (
                        spec["repair_points"]
                        if outcome == "success"
                        else max(1, spec["repair_points"] // 2)
                        if outcome == "partial"
                        else 0
                    )
                    target = connection.execute(
                        "SELECT condition FROM item_instances WHERE id=? AND container_id=?",
                        (spec["target_item_id"], row["id"]),
                    ).fetchone()
                    delta = min(points, 100 - target["condition"])
                    connection.execute(
                        "UPDATE item_instances SET condition=condition+? WHERE id=? AND container_id=?",
                        (delta, spec["target_item_id"], row["id"]),
                    )
                    result["repair_delta"] = delta
                    result["description"] = f"本次完好度提升{delta}点；物品所有权不变。"
                result["collect_outputs"] = True
        elif spec["kind"] != "map_review":
            result["returned_materials"] = True
            if spec.get("wage_account_id"):
                connection.execute("UPDATE workplace_accounts SET balance=balance+? WHERE location_id=?",(spec["payment"],spec["wage_account_id"]))
        # 工具随真实投入磨损；刚开始便中止不磨损，失败也不能免费使用工具。
        from world_engine.repository import from_iso

        elapsed = max(0, ((at or from_iso(row["last_processed_world_time"]))
                          - from_iso(row["started_world_time"])).total_seconds())
        fraction = 1 if status == "completed" else min(1, elapsed / (spec["duration_minutes"] * 60))
        result["tool_wear"] = []
        for reserved in spec.get("reserved_tools", []):
            item = connection.execute(
                "SELECT * FROM item_instances WHERE id=? AND container_type='activity_escrow' "
                "AND container_id=? AND owner_character_id=?",
                (reserved["item_id"], row["id"], row["character_id"]),
            ).fetchone()
            if item is None:
                continue
            wear = min(item["condition"], math.ceil(reserved["wear"] * fraction))
            connection.execute("UPDATE item_instances SET condition=condition-? WHERE id=?", (wear, item["id"]))
            result["tool_wear"].append({"item_type_id": item["item_type_id"], "wear": wear,
                                       "condition": item["condition"] - wear})
        connection.execute(
            "UPDATE item_instances SET container_type='activity_output' WHERE "
            "container_type='activity_escrow' AND container_id=?",
            (row["id"],),
        )
        connection.execute(
            "UPDATE activity_task_details SET result_json=? WHERE activity_id=?",
            (json.dumps(result, ensure_ascii=False), row["id"]),
        )
        InventoryService.audit(connection, row["world_id"], event_id, before)
        return result

    @staticmethod
    def available(connection, actor):
        from world_engine.life import LifeSceneService

        location = LifeSceneService.ground_location(connection, actor)
        if location is None:
            return []
        recipes = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM activity_recipes WHERE world_id=?", (actor["world_id"],)
            )
        ]
        return [
            {"id": row["id"], **json.loads(row["spec_json"])}
            for row in recipes
            if json.loads(row["spec_json"])["location_id"] == location["id"]
        ]
