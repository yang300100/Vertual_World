"""哥哥，用正式抵达登记添入可邂逅的旅人；重复执行不会重复造人。"""

import argparse
import json

from world_engine.config import Settings
from world_engine.conversations import ConversationService
from world_engine.database import Database
from world_engine.geo import great_circle_distance_km
from world_engine.movement import MovementService
from world_engine.registration import ElementRegistrationSubmit, WorldElementRegistry
from world_engine.repository import WorldRepository, from_iso

TRAVELERS = (
    (
        "chiqing",
        "迟青",
        "human",
        "流动抄写员",
        ["谨慎", "直率"],
        "找到愿意认真核对旅途笔记的人",
        "短句，先确认来意；熟悉后会用干脆的玩笑表达关心",
    ),
    (
        "laimi",
        "莱弥",
        "elf",
        "旅行织补匠",
        ["好奇", "热情", "夜行"],
        "认识不同地方的普通居民，收集他们愿意分享的衣饰故事",
        "轻快亲切，喜欢问具体小事，允许自己不知道答案",
    ),
    (
        "shuosha",
        "朔砂",
        "dwarf",
        "旅行草药记录员",
        ["温和", "友善"],
        "核对旅途中常见草药的地方称呼",
        "语气柔和，耐心听完；只把亲自核实过的事情说得肯定",
    ),
    (
        "dimai",
        "荻麦",
        "goblin",
        "流动账簿校对员",
        ["审慎", "守序"],
        "完成沿途商铺委托的账簿校对并保留自己的休息时间",
        "措辞认真，不急着亲近；会明确说明自己的时间和界限",
    ),
    (
        "luolan",
        "洛岚",
        "orc",
        "旅行画稿装订师",
        ["慢热", "友善"],
        "找一处安静的落脚点，整理沿途交换来的画稿",
        "句子简短，先听再说，关心他人时偏向提出小而具体的帮助",
    ),
)


def register_traveler_routine(c, wid, npc_id, location_id, key, source_event_id):
    """哥哥，旅人有真实谋生时段，也留出闲暇；保留用户已经登记的作息。"""
    if c.execute("SELECT 1 FROM npc_routine_plans WHERE character_id=?", (npc_id,)).fetchone():
        return
    npc = c.execute("SELECT * FROM characters WHERE id=?", (npc_id,)).fetchone()
    workplaces = c.execute(
        "SELECT l.* FROM locations l WHERE l.world_id=? AND l.is_active=1 "
        "AND (l.kind='workplace' OR EXISTS (SELECT 1 FROM workplace_accounts a "
        "WHERE a.location_id=l.id))",
        (wid,),
    ).fetchall()
    workplaces = [
        p
        for p in workplaces
        if p["space_layer"] == npc["space_layer"]
        and great_circle_distance_km(
            npc["longitude"], npc["latitude"], p["longitude"], p["latitude"]
        )
        <= 10
    ]
    if not workplaces:
        raise ValueError("旅人需要附近已有的工作场所或已登记工资账户，不能凭空设置收入")
    workplace = min(
        workplaces,
        key=lambda p: great_circle_distance_km(
            npc["longitude"], npc["latitude"], p["longitude"], p["latitude"]
        ),
    )
    starts = {
        "chiqing": "09:00",
        "laimi": "17:00",
        "shuosha": "10:00",
        "dimai": "08:00",
        "luolan": "11:00",
    }
    minutes = 240 if key == "dimai" else 180
    request = ElementRegistrationSubmit.model_validate(
        {
            "source_event_id": source_event_id,
            "idempotency_key": f"encounter-life-v1:{npc_id}",
            "payload": {
                "element_type": "npc_routine",
                "name": f"{npc['name']}的旅行生计与闲暇",
                "character_id": npc_id,
                "goal": "maintain_reserve",
                "income_reserve": 30,
                "max_work_minutes_per_day": minutes,
                "allow_alternative_work": True,
                "slots": [
                    {
                        "key": "livelihood",
                        "name": "临时谋生",
                        "starts_at": starts[key],
                        "duration_minutes": minutes,
                        "target_minutes": minutes,
                        "kind": "work",
                        "location_id": workplace["id"],
                        "visibility": "public",
                    },
                    {
                        "key": "sleep",
                        "name": "日常睡眠",
                        "starts_at": "07:00" if key == "laimi" else "22:00",
                        "duration_minutes": 480,
                        "target_minutes": 480,
                        "kind": "rest",
                        "location_id": location_id,
                    },
                ],
            },
        }
    )
    registry = WorldElementRegistry()
    registered = registry.submit(c, world_id=wid, request=request)
    if registered.status == "proposed":
        registered = registry.confirm(c, world_id=wid, registration_id=registered.id)
    if registered.status != "applied":
        raise ValueError(f"旅人作息尚未应用：{registered.rejection_reason}")


def register_encounter_people(c, wid, location_id):
    location = c.execute(
        "SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1", (location_id, wid)
    ).fetchone()
    if location is None:
        raise ValueError("请选择本世界中有效的落脚地点")
    world = c.execute("SELECT * FROM worlds WHERE id=?", (wid,)).fetchone()
    if world is None:
        raise ValueError("世界不存在")
    registry = WorldElementRegistry()
    result = []
    source = None
    for key, name, species, identity, traits, goal, speech in TRAVELERS:
        idempotency = f"encounter-travelers-v1:{location_id}:{key}"
        existing = c.execute(
            "SELECT * FROM element_registration_requests WHERE world_id=? AND idempotency_key=?",
            (wid, idempotency),
        ).fetchone()
        if existing:
            if existing["status"] != "applied":
                raise ValueError(f"{name}的旧登记尚未完成，请先检查登记记录")
            result.append({"name": name, "id": existing["result_entity_id"], "status": "applied"})
            register_traveler_routine(
                c, wid, existing["result_entity_id"], location_id, key, existing["source_event_id"]
            )
            continue
        if source is None:
            source = MovementService._record_log(
                c,
                world_id=wid,
                world_time=from_iso(world["current_time"]),
                event_type="world.travelers_arrival",
                actor_id=None,
                location_id=location_id,
                summary=f"几位各有谋生手艺的成年旅人抵达了{location['name']}，暂在这里落脚。",
                payload={"content_packet": "encounter-travelers-v1", "information_scope": "public"},
            )
        request = ElementRegistrationSubmit.model_validate(
            {
                "source_event_id": source,
                "idempotency_key": idempotency,
                "payload": {
                    "element_type": "character_arrival",
                    "name": name,
                    "species": species,
                    "identity": identity,
                    "location_id": location_id,
                    "traits": traits,
                    "goals": [goal],
                    "activation_policy": "distance",
                },
            }
        )
        registered = registry.submit(c, world_id=wid, request=request)
        if registered.status == "proposed":
            registered = registry.confirm(c, world_id=wid, registration_id=registered.id)
        if registered.status != "applied":
            raise ValueError(f"{name}抵达登记失败：{registered.rejection_reason}")
        npc = WorldRepository().get_snapshot(c, wid).character_by_id(registered.result_entity_id)
        card = ConversationService.default_card(npc)
        from dataclasses import replace

        card = replace(
            card,
            speech_style=speech,
            initiative_notes="按自己的作息和关系决定是否主动问候，不替玩家决定行动",
        )
        ConversationService.save_card(c, world_id=wid, npc_id=npc.id, card=card)
        register_traveler_routine(c, wid, npc.id, location_id, key, source)
        result.append({"name": name, "id": npc.id, "species": species, "status": "applied"})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--world-id", required=True)
    parser.add_argument("--location-id", required=True)
    parser.add_argument("--apply", action="store_true", help="正式登记并确认人物抵达")
    args = parser.parse_args()
    if not args.apply:
        print(
            json.dumps(
                [{"name": p[1], "species": p[2], "traits": p[4]} for p in TRAVELERS],
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    database = Database(Settings.from_env().database_path)
    database.initialize()
    with database.write() as c:
        result = register_encounter_people(c, args.world_id, args.location_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
