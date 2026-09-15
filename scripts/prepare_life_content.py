"""导出低成本模型填写所需的目录和真实结构；只读数据库，不导入内容。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from world_engine.activity_tasks import ActivityRecipeSpec
from world_engine.economy import CommoditySpec, WorkplaceBudgetSpec
from world_engine.interiors import InteriorRoomSpec
from world_engine.routines import RoutinePlanSpec

MODELS = {
    "commodity": CommoditySpec,
    "workplace_budget": WorkplaceBudgetSpec,
    "interior_room": InteriorRoomSpec,
    "activity_recipe": ActivityRecipeSpec,
    "npc_routine": RoutinePlanSpec,
}


def export_packet(database: Path, world_id: str, location_ids: list[str], output: Path):
    if not location_ids:
        raise ValueError("需要明确选择至少一个地点，不能默认导出整个世界")
    selected = sorted(set(location_ids))
    marks = ",".join("?" for _ in selected)
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as c:
        c.row_factory = sqlite3.Row
        world = c.execute(
            "SELECT w.id,w.name,w.current_time,w.version FROM worlds w WHERE id=?", (world_id,)
        ).fetchone()
        if world is None:
            raise ValueError("世界不存在")
        locations = [
            dict(row)
            for row in c.execute(
                f"SELECT id,name,kind,longitude,latitude,resources_json FROM locations WHERE world_id=? AND is_active=1 AND id IN ({marks})",
                (world_id, *selected),
            )
        ]
        if len(locations) != len(selected):
            raise ValueError("所选地点不存在、不属于当前世界或已停用")
        for row in locations:
            row["resources"] = json.loads(row.pop("resources_json"))
        people = [
            dict(row)
            for row in c.execute(
                f"SELECT id,name,identity,skills_json,location_id FROM characters WHERE world_id=? AND is_player=0 AND (location_id IN ({marks}) OR current_location_id IN ({marks})) ORDER BY name,id",
                (world_id, *selected, *selected),
            )
        ]
        for row in people:
            row["skills"] = json.loads(row.pop("skills_json"))
        items = [
            dict(row)
            for row in c.execute(
                "SELECT id,name,category,stack_limit,slot_size FROM item_types WHERE id IN "
                "(SELECT item_type_id FROM item_instances WHERE world_id=? UNION SELECT item_type_id FROM world_item_profiles WHERE world_id=?) ORDER BY name,id",
                (world_id, world_id),
            )
        ]
        rooms = [
            dict(row)
            for row in c.execute(
                f"SELECT r.*,COALESCE(p.nightly_rate,0) AS nightly_rate FROM life_rooms r LEFT JOIN room_rental_rates p ON p.room_id=r.id WHERE r.world_id=? AND r.location_id IN ({marks})",
                (world_id, *selected),
            )
        ]
        has_door_policies = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_door_policies'"
        ).fetchone()
        for room in rooms:
            policy = c.execute(
                "SELECT key_item_type_id,visitor_policy FROM life_door_policies WHERE room_id=?",
                (room["id"],),
            ).fetchone() if has_door_policies else None
            room.update(dict(policy) if policy else {"key_item_type_id": None, "visitor_policy": "authorized_only"})
        recipes = [
            {"id":row["id"],**json.loads(row["spec_json"])}
            for row in c.execute(
                "SELECT id,spec_json FROM activity_recipes WHERE world_id=?", (world_id,)
            )
            if json.loads(row["spec_json"])["location_id"] in selected
        ]
        profiles = [
            dict(row)
            for row in c.execute(
                "SELECT item_type_id,resource_location_id,resource_key,resource_owner_id,price,nutrition FROM world_item_profiles WHERE world_id=?",
                (world_id,),
            )
        ]
        has_food_rules = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='food_storage_rules'").fetchone()
        for profile in profiles:
            rule = c.execute("SELECT shelf_life_hours FROM food_storage_rules WHERE item_type_id=?", (profile["item_type_id"],)).fetchone() if has_food_rules else None
            profile["shelf_life_hours"] = rule[0] if rule else None
        budgets = [
            dict(row)
            for row in c.execute(
                f"SELECT location_id,wage,balance FROM workplace_accounts WHERE world_id=? AND location_id IN ({marks})",
                (world_id, *selected),
            )
        ]
        has_routines=c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='npc_routine_plans'").fetchone()
        people_ids={person["id"] for person in people}
        routines=[{"id":row["id"],"revision":row["revision"],"spec":json.loads(row["spec_json"])} for row in c.execute("SELECT * FROM npc_routine_plans WHERE world_id=?",(world_id,)) if row["character_id"] in people_ids] if has_routines else []
    catalogue = {
        "format_version": 1,
        "world": dict(world),
        "locations": locations,
        "characters": people,
        "item_types": items,
        "rooms": rooms,
        "recipes": recipes,
        "commodity_profiles": profiles,
        "workplace_accounts": budgets,
        "npc_routines":routines,
    }
    output.mkdir(parents=True, exist_ok=True)
    write_json(output / "catalogue.json", catalogue)
    for kind, model in MODELS.items():
        write_json(output / f"schema-{kind}.json", model.model_json_schema())
    template = output / "content-draft.template.json"
    write_json(template, {"format_version": 1, "world_id": world_id, "entries": []})
    guide = Path(__file__).resolve().parents[1] / "docs/content/life-content-authoring.md"
    (output / "填写说明.md").write_text(guide.read_text(encoding="utf-8"), encoding="utf-8")
    return catalogue


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def validate_draft(draft, catalogue):
    """验证草案结构、引用及明显矛盾；最终授权仍由注册器审核。"""
    if not isinstance(draft, dict) or set(draft) != {"format_version", "world_id", "entries"}:
        raise ValueError("草案根对象只能包含 format_version、world_id、entries")
    if (
        type(draft["format_version"]) is not int
        or draft["format_version"] != 1
        or draft["world_id"] != catalogue["world"]["id"]
    ):
        raise ValueError("草案格式版本或世界标识不匹配")
    if not isinstance(draft["entries"], list) or len(draft["entries"]) > 100:
        raise ValueError("每批 entries 必须是最多 100 项的列表")
    locations = {row["id"]: row for row in catalogue["locations"]}
    people = {row["id"] for row in catalogue["characters"]}
    items = {row["id"]: row for row in catalogue["item_types"]}
    rooms = {row["id"]: row for row in catalogue["rooms"]}
    used_keys = set()
    names = set()
    sources = {
        (row["resource_location_id"], row["resource_key"])
        for row in catalogue["commodity_profiles"]
        if row["resource_location_id"]
    }
    budgets = {row["location_id"] for row in catalogue["workplace_accounts"]}
    registered_budgets=set(budgets)
    existing_items = {row["name"] for row in catalogue["item_types"]}
    existing_recipes = {row["name"] for row in catalogue["recipes"]}
    routine_people=set()
    for index, entry in enumerate(draft["entries"], 1):
        if not isinstance(entry, dict) or set(entry) != {"key", "source_note", "payload"}:
            raise ValueError(f"第 {index} 项只能包含 key、source_note、payload")
        key = entry["key"]
        if not isinstance(key, str) or not key.strip() or len(key) > 80 or key in used_keys:
            raise ValueError(f"第 {index} 项的 key 无效或重复")
        used_keys.add(key)
        if not isinstance(entry["source_note"], str) or not entry["source_note"].strip():
            raise ValueError(f"第 {index} 项缺少设定依据或推断说明")
        raw = entry["payload"]
        model = MODELS.get(raw.get("element_type")) if isinstance(raw, dict) else None
        if model is None:
            raise ValueError(f"第 {index} 项的元素类型不支持")
        spec = model.model_validate(raw)
        kind = spec.element_type
        if (kind, spec.name) in names:
            raise ValueError(f"第 {index} 项与本批同类条目重名")
        names.add((kind, spec.name))
        loc = getattr(spec, "location_id", None) or getattr(spec, "resource_location_id", None)
        if loc and loc not in locations:
            raise ValueError(f"第 {index} 项引用了目录外的地点")
        if isinstance(spec, CommoditySpec):
            if spec.shelf_life_hours is not None and spec.category != "food":
                raise ValueError(f"第 {index} 项只有食物能登记保鲜期限")
            if spec.name in existing_items:
                raise ValueError(f"第 {index} 项物品类型已存在，应复用目录 ID")
            if bool(spec.resource_location_id) != bool(spec.resource_key):
                raise ValueError(f"第 {index} 项资源地点和资源名必须同时填写")
            if spec.resource_owner_id and spec.resource_owner_id not in people:
                raise ValueError(f"第 {index} 项引用了目录外的资源所有者")
            if spec.resource_owner_id and not spec.resource_location_id:
                raise ValueError(f"第 {index} 项没有资源来源却指定所有者")
            if spec.initial_resource > spec.resource_capacity:
                raise ValueError(f"第 {index} 项初始储量超过容量")
            if spec.resource_location_id:
                source = (spec.resource_location_id, spec.resource_key)
                if source in sources:
                    raise ValueError(f"第 {index} 项重复定义已有资源来源")
                if locations[loc]["resources"].get(spec.resource_key, 0) > spec.resource_capacity:
                    raise ValueError(f"第 {index} 项容量低于既有储量")
                sources.add(source)
        elif isinstance(spec, WorkplaceBudgetSpec):
            if loc in budgets:
                raise ValueError(f"第 {index} 项重复初始化工资账户")
            budgets.add(loc)
        elif isinstance(spec, InteriorRoomSpec):
            if spec.key_item_type_id:
                key = items.get(spec.key_item_type_id)
                profile = next((p for p in catalogue["commodity_profiles"] if p["item_type_id"] == spec.key_item_type_id), None)
                if not key or not profile or key["category"] != "tool" or key["stack_limit"] != 1 or not spec.owner_character_id:
                    raise ValueError(f"第 {index} 项实体钥匙须为本世界已登记的不可堆叠工具，且房间有明确所有者")
            if spec.owner_character_id and spec.owner_character_id not in people:
                raise ValueError(f"第 {index} 项房间所有者不在目录内")
            if (
                spec.access_policy == "private"
                or spec.nightly_rate
                or any(f.kind == "container" for f in spec.fixtures)
            ) and not spec.owner_character_id:
                raise ValueError(f"第 {index} 项私人房间、储物柜或出租房必须明确所有者")
            parent = rooms.get(spec.parent_room_id) if spec.parent_room_id else None
            if spec.parent_room_id and (not parent or parent["location_id"] != loc):
                raise ValueError(f"第 {index} 项外层房间不存在或地点不一致")
        elif isinstance(spec,RoutinePlanSpec):
            if spec.character_id not in people or spec.character_id in routine_people:
                raise ValueError(f"第 {index} 项的 NPC 不在目录内，或本批重复安排同一人物")
            routine_people.add(spec.character_id)
            current=next((item for item in catalogue.get("npc_routines",[]) if item["spec"]["character_id"]==spec.character_id),None)
            if current and spec.expected_revision!=current["revision"] or not current and spec.expected_revision is not None:
                raise ValueError(f"第 {index} 项的作息修订号与目录不符")
            for slot in spec.slots:
                if current and not spec.enabled:
                    continue
                if slot.location_id not in locations:
                    raise ValueError(f"第 {index} 项的作息引用了目录外的地点")
                if slot.kind=="work" and locations[slot.location_id]["kind"]!="workplace" and slot.location_id not in registered_budgets:
                    raise ValueError(f"第 {index} 项工作地点没有已登记的工作或工资规则")
                if slot.kind=="craft":
                    recipe=next((item for item in catalogue["recipes"] if item.get("id")==slot.recipe_id),None)
                    if not recipe or recipe["kind"]!="craft" or recipe["location_id"]!=slot.location_id or recipe["duration_minutes"]>slot.duration_minutes:
                        raise ValueError(f"第 {index} 项配方尚未登记，或与作息地点/时长不符")
                    if slot.target_minutes and slot.target_minutes%recipe["duration_minutes"]:
                        raise ValueError(f"第 {index} 项的制作目标不是完整工序时长的倍数")
            if spec.enabled:
                from world_engine.repository import from_iso

                for goal in spec.ambitions:
                    if goal.deadline_world_time and goal.deadline_world_time <= from_iso(catalogue["world"]["current_time"]):
                        raise ValueError(f"第 {index} 项目标期限已经过去")
                    work_ids=set(goal.work_location_ids)|{s.location_id for s in spec.slots if s.kind=="work"}
                    if goal.kind=="reserve_money" and not work_ids:
                        raise ValueError(f"第 {index} 项储蓄目标需要已知工作地点")
                    if any(lid not in locations or locations[lid]["kind"]!="workplace" and lid not in registered_budgets for lid in work_ids):
                        raise ValueError(f"第 {index} 项目标工作地点不可用")
                    if goal.kind=="craft_stock" and not any(r.get("id")==goal.recipe_id and r["kind"]=="craft" for r in catalogue["recipes"]):
                        raise ValueError(f"第 {index} 项目标配方不在已登记目录内")
        else:
            if spec.name in existing_recipes:
                raise ValueError(f"第 {index} 项配方名称已存在")
            ids = [item.item_type_id for item in spec.ingredients]
            if len(ids) != len(set(ids)):
                raise ValueError(f"第 {index} 项重复原料应合并数量")
            if spec.check_tool_type_id in ids:
                raise ValueError(f"第 {index} 项工具不能同时作为消耗原料")
            tool_ids = [tool.item_type_id for tool in spec.tools]
            if len(tool_ids) != len(set(tool_ids)) or set(tool_ids) & set(ids):
                raise ValueError(f"第 {index} 项工具不能重复，也不能同时作为消耗原料")
            refs = ids + tool_ids + [v for v in (spec.output_item_type_id, spec.check_tool_type_id) if v]
            if any(ref not in items for ref in refs):
                raise ValueError(f"第 {index} 项引用了目录外的物品类型；先登记物品并重新导出目录")
            if spec.kind == "craft" and not spec.output_item_type_id:
                raise ValueError(f"第 {index} 项制作配方缺少产物")
            if spec.kind == "repair" and (not spec.repair_category or spec.output_item_type_id):
                raise ValueError(f"第 {index} 项修理配方必须指定类别且不能额外产出物品")
    return len(draft["entries"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--world")
    parser.add_argument("--location", action="append", default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate", type=Path)
    parser.add_argument("--catalogue", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.validate:
            if not args.catalogue:
                raise ValueError("校验时需要 --catalogue")
            count = validate_draft(
                json.loads(args.validate.read_text(encoding="utf-8-sig")),
                json.loads(args.catalogue.read_text(encoding="utf-8-sig")),
            )
            print(f"草案 {count} 项结构与目录引用检查通过；尚未提交或写入世界。")
        else:
            if not args.world or not args.location or not args.output:
                raise ValueError("导出需要 --world、至少一个 --location 和 --output")
            from world_engine.config import Settings

            export_packet(
                args.database or Settings.from_env().database_path,
                args.world,
                args.location,
                args.output,
            )
            print(f"只读材料已导出到 {args.output.resolve()}；世界内容未改变。")
    except (ValueError, ValidationError, sqlite3.Error, OSError) as exc:
        parser.exit(2, f"{exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
