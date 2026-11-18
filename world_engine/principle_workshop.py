"""哥哥，宏观原型制作仍消耗真实材料，工具必须在本人能够使用的现场。"""

import json
from uuid import uuid4

from world_engine.event_log import record_event
from world_engine.inventory import InventoryService
from world_engine.materials import encoded
from world_engine.repository import to_iso

MATERIAL_NAMES = {
    "wood": "常规木料",
    "metal": "常规金属料",
    "clay": "常规黏土料",
    "fiber": "常规纤维料",
    "paper": "常规纸料",
    "water": "常规清水",
    "charcoal": "常规木炭",
    "basic_tools": "通用手工器具",
}


def accessible(item, actor):
    if item["owner_character_id"] != actor["id"]:
        return False
    if item["container_type"] == "character_inventory":
        return item["container_id"] == actor["id"]
    if actor["current_room_id"]:
        return (
            item["container_type"] == "room_ground"
            and item["container_id"] == actor["current_room_id"]
        )
    return item["container_type"] in {"ground", "location_ground"} and item["container_id"] == (
        actor["current_location_id"] or actor["location_id"]
    )


def has_skill(c, actor, skill, at):
    return (
        skill is None
        or skill in json.loads(actor["skills_json"])
        or bool(
            c.execute(
                "SELECT 1 FROM character_skill_proficiencies WHERE character_id=? AND world_id=? "
                "AND skill_name=? AND proficiency>0 AND updated_at<=?",
                (actor["id"], actor["world_id"], skill, to_iso(at)),
            ).fetchone()
        )
    )


def seed_inputs(c, world_id):
    """哥哥，只准备有限普通物资，不创建待发现科技、配方或未来依赖。"""
    for kind, name in MATERIAL_NAMES.items():
        if c.execute(
            "SELECT 1 FROM principle_material_catalogue WHERE world_id=? AND kind=?",
            (world_id, kind),
        ).fetchone():
            continue
        tid = str(uuid4())
        c.execute(
            "INSERT INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,?,?,?,1)",
            (tid, name, "tool" if kind == "basic_tools" else "material", 100),
        )
        c.execute("INSERT INTO principle_material_catalogue VALUES (?,?,?)", (world_id, kind, tid))
        for actor in c.execute(
            "SELECT * FROM characters WHERE world_id=? AND is_player=0 "
            "AND health>=40 AND json_array_length(skills_json)>0",
            (world_id,),
        ).fetchall():
            room = actor["current_room_id"]
            location = room or actor["current_location_id"] or actor["location_id"]
            c.execute(
                "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
                "owner_character_id,quantity,condition) VALUES (?,?,?,?,?,?,?,100)",
                (
                    str(uuid4()),
                    world_id,
                    tid,
                    "room_ground" if room else "location_ground",
                    location,
                    actor["id"],
                    1 if kind == "basic_tools" else 24,
                ),
            )


def prototype_accessible(c, actor_id, technology_id, invention_id=None):
    actor = c.execute("SELECT * FROM characters WHERE id=?", (actor_id,)).fetchone()
    table, column, reference = (
        ("principle_invention_prototypes", "invention_id", invention_id)
        if invention_id
        else ("principle_prototypes", "technology_id", technology_id)
    )
    row = c.execute(
        f"SELECT i.* FROM {table} p JOIN item_instances i ON i.id=p.item_id "
        f"JOIN character_life_activities l ON l.id=p.activity_id WHERE p.{column}=? "
        "AND p.actor_id=? AND l.status='completed' AND i.quantity>0 AND i.condition>0",
        (reference, actor_id),
    ).fetchone()
    return bool(row and accessible(row, actor))


def consume_inputs(c, actor, technology_id, requirements, purpose, at):
    inputs = []
    for requirement in requirements:
        entry = c.execute(
            "SELECT item_type_id FROM principle_material_catalogue WHERE world_id=? AND kind=?",
            (actor["world_id"], requirement.kind),
        ).fetchone()
        if entry is None:
            raise ValueError("尚无该类现实物资：" + requirement.kind)
        item = next(
            (
                row
                for row in c.execute(
                    "SELECT * FROM item_instances WHERE world_id=? AND owner_character_id=? "
                    "AND item_type_id=? AND quantity>=? ORDER BY id",
                    (actor["world_id"], actor["id"], entry[0], requirement.quantity),
                )
                if accessible(row, actor)
            ),
            None,
        )
        if item is None:
            raise ValueError("本人现场材料不足：" + requirement.kind)
        before = item["quantity"]
        InventoryService.consume(
            c, item, requirement.quantity, destination="principle:" + technology_id
        )
        inputs.append(
            {
                "item_id": item["id"],
                "kind": requirement.kind,
                "before": before,
                "consumed": requirement.quantity,
                "after": before - requirement.quantity,
            }
        )
    if inputs:
        c.execute(
            "INSERT INTO principle_material_uses VALUES (?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                actor["world_id"],
                actor["id"],
                technology_id,
                purpose,
                encoded(inputs),
                to_iso(at),
            ),
        )
    return inputs


def produce_prototype(c, actor, technology, activity_id, inputs, at):
    active = c.execute(
        "SELECT * FROM character_life_activities WHERE id=? AND character_id=?",
        (activity_id, actor["id"]),
    ).fetchone()
    if active is None or active["status"] != "completed":
        raise ValueError("原型制作劳动尚未完成")
    invention_id = technology.get("invention_id")
    table, column, reference = (
        ("principle_invention_prototypes", "invention_id", invention_id)
        if invention_id
        else ("principle_prototypes", "technology_id", technology["id"])
    )
    if c.execute(f"SELECT 1 FROM {table} WHERE {column}=?", (reference,)).fetchone():
        raise ValueError("这项科技已有原型记录，不能重复制造并覆盖原来源")
    tid, iid = str(uuid4()), str(uuid4())
    c.execute(
        "INSERT INTO item_types(id,name,category,stack_limit,slot_size) VALUES (?,?,'tool',1,1)",
        (tid, technology["name"] + "·原型"),
    )
    room = actor["current_room_id"]
    location = room or actor["current_location_id"] or actor["location_id"]
    c.execute(
        "INSERT INTO item_instances(id,world_id,item_type_id,container_type,container_id,"
        "owner_character_id,quantity,condition) VALUES (?,?,?,?,?,?,1,100)",
        (
            iid,
            actor["world_id"],
            tid,
            "room_ground" if room else "location_ground",
            location,
            actor["id"],
        ),
    )
    c.execute(
        f"INSERT INTO {table} VALUES (?,?,?,?,?,?)",
        (reference, actor["id"], activity_id, iid, encoded(inputs), to_iso(at)),
    )
    if invention_id:
        from world_engine.principle_effects import record_impacts
        from world_engine.principle_types import CapabilityEffect

        effect = CapabilityEffect.model_validate_json(technology["effect_json"])
        strength = effect.factor if effect.kind == "material_strength" else 1.0
        precision = effect.factor if effect.kind == "craft_precision" else 1.0
        c.execute(
            "INSERT INTO principle_product_properties VALUES (?,?,?,0,?)",
            (iid, strength, precision, invention_id),
        )
        if effect.kind in {"material_strength", "craft_precision"}:
            record_impacts(
                c,
                actor["id"],
                [(technology, 1.0, effect.factor)],
                effect.kind,
                "life_activity",
                activity_id,
                at,
            )
    record_event(
        c,
        world_id=actor["world_id"],
        tick_id="prototype:" + iid,
        occurred_at=at,
        event_type="world.technology_prototype_built",
        actor_id=actor["id"],
        location_id=actor["current_location_id"] or actor["location_id"],
        summary=f"{actor['name']}完成“{technology['name']}”原型制作，材料已经实际消耗。",
        payload={
            "technology_id": technology["id"],
            "item_id": iid,
            "activity_id": activity_id,
            "inputs": inputs,
        },
    )
    return iid
