"""NPC 在对话里提出的动作，只经既有世界规则结算。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from world_engine.actions import ActionService
from world_engine.decisions import NpcActionIntent
from world_engine.geo import great_circle_distance_km
from world_engine.interiors import InteriorService
from world_engine.inventory import InventoryService
from world_engine.life import LifeActivityService
from world_engine.movement import MovementService
from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM, same_room
from world_engine.repository import WorldRepository, from_iso, to_iso


SELFCARE_RECEIPT_SCHEMA = """
CREATE TABLE IF NOT EXISTS npc_selfcare_receipts (
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    source_event_id TEXT NOT NULL,
    npc_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK(kind IN ('self_care','self_rest')),
    target_id TEXT NOT NULL, minutes INTEGER,
    action_event_id TEXT NOT NULL,
    PRIMARY KEY(world_id,source_event_id,npc_id),
    CHECK((kind='self_rest' AND minutes BETWEEN 1 AND 720) OR (kind='self_care' AND minutes IS NULL))
);
"""


def install_selfcare_receipts(connection):
    """哥哥，逐条建表，不能替调用方提交已有事务。"""
    for statement in SELFCARE_RECEIPT_SCHEMA.split(";"):
        if statement.strip():
            connection.execute(statement)


def _actor_id(npc):
    return npc["id"] if hasattr(npc, "keys") else npc.id


def _blocked_aboard_actor(connection, npc, at=None):
    """只认仍在真实挂起载具里的本人；不把模型快照当作当前状态。"""
    from world_engine.ride_recovery import RideRecoveryService

    actor = connection.execute("SELECT * FROM characters WHERE id=?", (_actor_id(npc),)).fetchone()
    if actor is None or actor["is_player"] or actor["health"] <= 0:
        raise ValueError("只有仍然存活的NPC本人能够选择照护")
    current = connection.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (actor["world_id"],)).fetchone()
    if current is None:
        raise ValueError("人物所在世界不存在")
    world_time = from_iso(current[0])
    if at is not None and at != world_time:
        raise ValueError("世界时间已变化，请重新核对本人的实际状态")
    binding = LifeActivityService._aboard_stationary(connection, actor, world_time)
    if binding is None:
        raise ValueError("本人目前不在被困的真实载具上")
    ride, _ = binding
    movement = connection.execute("SELECT status FROM character_movements WHERE id=? AND world_id=?", (ride["movement_id"], actor["world_id"])).fetchone() if ride["movement_id"] else None
    if movement and movement["status"] != "suspended":
        raise ValueError("原载具路线并未真实停下")
    if ride["movement_id"] and movement is None:
        raise ValueError("原载具路线不存在")
    if connection.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (actor["id"],)).fetchone():
        raise ValueError("本人仍有正在执行的独立行程")
    from world_engine.player_steering import PlayerSteeringService
    if PlayerSteeringService.active(connection, actor["id"]):
        raise ValueError("本人仍有方向移动输入")
    LifeActivityService.assert_available(connection, actor["id"])
    blocked = RideRecoveryService._block_event(connection, ride, world_time)
    if not connection.execute(
        "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=? AND world_id=? AND learned_world_time<=?",
        (actor["id"], blocked["id"], actor["world_id"], to_iso(world_time)),
    ).fetchone():
        raise ValueError("本人尚不知道当前真实挂起事件")
    return actor, ride, world_time


def self_rest_available(connection, npc):
    try:
        _blocked_aboard_actor(connection, npc)
        return True
    except (ValueError, LookupError, TypeError):
        return False


def _self_care_item(connection, actor, item_id, at):
    """哥哥，食物必须有本世界登记或真实物质批次，药物必须真能使用。"""
    from world_engine.food import FoodService
    from world_engine.materials import MaterialService

    item = connection.execute(
        "SELECT i.*,t.name,t.category,t.usable,t.heal,COALESCE(p.nutrition,0) AS nutrition "
        "FROM item_instances i JOIN item_types t ON t.id=i.item_type_id "
        "LEFT JOIN world_item_profiles p ON p.item_type_id=i.item_type_id AND p.world_id=i.world_id "
        "WHERE i.id=? AND i.world_id=? AND i.container_type='character_inventory' "
        "AND i.container_id=? AND i.owner_character_id=? AND i.quantity>0",
        (item_id, actor["world_id"], actor["id"], actor["id"]),
    ).fetchone()
    if item is None:
        raise ValueError("本人没有这件可使用的随身物品")
    if item["category"] == "consumable" and item["usable"] and item["heal"] > 0:
        if actor["health"] >= 100:
            raise ValueError("本人身体状态已满，不消耗治疗物品")
        return item, "medicine", None
    if item["category"] != "food" or actor["satiety"] >= 100:
        raise ValueError("这件物品不是当前可食用的本人食物，或本人已吃饱")
    material = MaterialService.state(connection, item["id"])
    if MaterialService.enabled(connection, actor["world_id"]):
        if material is None or material.nutrition <= 0:
            raise ValueError("食物缺少真实可食用的物质批次")
    elif item["nutrition"] <= 0:
        raise ValueError("食物没有本世界已登记的营养定义")
    freshness = FoodService.view(item, at)["state"]
    if freshness == "spoiled":
        raise ValueError("食物已经变质，本人照护不默认接受风险")
    decision = connection.execute("SELECT policy FROM collective_food_decisions WHERE actor_id=? AND batch_id=?", (actor["id"], item["id"])).fetchone()
    if decision and decision[0] in {"avoid", "reserve"}:
        raise ValueError("本人已安排避免或留存这份食物")
    return item, "food", freshness


def self_care_options(connection, npc, at):
    try:
        actor, _, current = _blocked_aboard_actor(connection, npc, at)
    except (ValueError, LookupError, TypeError):
        return []
    options = []
    for row in connection.execute(
        "SELECT id FROM item_instances WHERE world_id=? AND container_type='character_inventory' "
        "AND container_id=? AND owner_character_id=? AND quantity>0 ORDER BY id",
        (actor["world_id"], actor["id"], actor["id"]),
    ).fetchall():
        try:
            item, kind, freshness = _self_care_item(connection, actor, row["id"], current)
        except (ValueError, LookupError, TypeError):
            continue
        value = {"id": item["id"], "name": item["name"], "kind": kind}
        if freshness is not None:
            value["freshness"] = freshness
        options.append(value)
    return options


@dataclass(frozen=True)
class NpcActionResolution:
    status: str
    narration: str
    event_id: str | None = None


def carried_items(connection: sqlite3.Connection, npc) -> list[dict[str, object]]:
    """只把 NPC 自己持有且拥有的物品交给模型作为候选。"""
    rows = connection.execute(
        "SELECT i.id,t.name,i.quantity FROM item_instances i "
        "JOIN item_types t ON t.id=i.item_type_id "
        "WHERE i.world_id=? AND i.container_type='character_inventory' "
        "AND i.container_id=? AND i.owner_character_id=? AND i.quantity>0 "
        "ORDER BY t.name,i.id LIMIT 12",
        (npc["world_id"], npc["id"], npc["id"]),
    ).fetchall()
    return [dict(row) for row in rows]


def travel_destinations(connection: sqlite3.Connection, npc) -> list[dict[str, object]]:
    """Yuki 只给出同一世界的实际地点，避免模型编造目的地。"""
    rows = connection.execute(
        "SELECT id,name,longitude,latitude FROM locations "
        "WHERE world_id=? AND is_active=1 AND id!=? AND space_layer=?",
        (npc["world_id"], npc["current_location_id"] or npc["location_id"],npc["space_layer"]),
    ).fetchall()
    ranked = sorted(
        rows,
        key=lambda row: great_circle_distance_km(
            npc["longitude"], npc["latitude"], row["longitude"], row["latitude"]
        ),
    )[:8]
    return [{"id": row["id"], "name": row["name"]} for row in ranked]


_DEFAULT_LOCATION = object()


def _record_action(
    connection, npc, at: datetime, event_type: str, summary: str, payload: dict,
    *, location_id=_DEFAULT_LOCATION,
) -> str:
    event_id = ActionService._record_event(
        connection,
        world_id=npc["world_id"],
        tick_id=f"npc-performance:{npc['id']}:{at.isoformat()}",
        occurred_at=at,
        event_type=event_type,
        actor_id=npc["id"],
        target_id=payload.get("recipient_id"),
        location_id=(npc["current_location_id"] or npc["location_id"]) if location_id is _DEFAULT_LOCATION else location_id,
        summary=summary,
        payload=payload,
    )
    ActionService._record_memory(
        connection,
        world_id=npc["world_id"],
        character_id=npc["id"],
        event_id=event_id,
        memory_type="experienced",
        summary=summary,
        importance=3,
    )
    return event_id


def _self_care_action(connection, npc, at, action, source_event_id):
    """本人明确选择、真实消耗与幂等回执在同一保存点中提交。"""
    from world_engine.domain import ActionProposal, ActionType
    from world_engine.food import FoodService

    actor_id = _actor_id(npc)
    actor = connection.execute("SELECT * FROM characters WHERE id=?", (actor_id,)).fetchone()
    if actor is None or actor["is_player"]:
        raise ValueError("不能替玩家或其他人物选择本人照护")
    if not source_event_id:
        raise ValueError("本人选择缺少可核验的对话来源")
    source = connection.execute("SELECT * FROM world_events WHERE id=? AND world_id=?", (source_event_id, actor["world_id"])).fetchone()
    if source is None or from_iso(source["occurred_at"]) > at:
        raise ValueError("本人选择的来源不存在、跨世界或尚未发生")
    if (actor_id not in {source["actor_id"], source["target_id"]} and not connection.execute(
        "SELECT 1 FROM event_observers o JOIN character_event_knowledge k ON k.event_id=o.event_id "
        "AND k.character_id=o.character_id WHERE o.event_id=? AND o.character_id=? AND k.world_id=? "
        "AND k.learned_world_time<=?", (source_event_id, actor_id, actor["world_id"], to_iso(at)),
    ).fetchone()):
        raise ValueError("本人没有该来源事件的参与或观察记录")
    connection.execute("SAVEPOINT npc_aboard_selfcare")
    try:
        receipt = connection.execute(
            "SELECT * FROM npc_selfcare_receipts WHERE world_id=? AND source_event_id=? AND npc_id=?",
            (actor["world_id"], source_event_id, actor_id),
        ).fetchone()
        if receipt:
            if (receipt["kind"], receipt["target_id"], receipt["minutes"]) != (action.kind, action.target_id, action.minutes):
                raise ValueError("同一来源不能改成另一份本人照护选择")
            recorded = connection.execute("SELECT summary FROM world_events WHERE id=? AND world_id=?", (receipt["action_event_id"], actor["world_id"])).fetchone()
            if recorded is None:
                raise ValueError("原本人照护回执不存在")
            connection.execute("RELEASE npc_aboard_selfcare")
            return NpcActionResolution("already", recorded["summary"], receipt["action_event_id"])

        actor, ride, current = _blocked_aboard_actor(connection, actor, at)
        if action.kind == "self_rest":
            if action.target_id != actor["id"]:
                raise ValueError("本人休息只能引用自己的真实身份")
            proposal = ActionProposal(actor_id=actor["id"], action=ActionType.REST,
                reason="本人明确选择在真实挂起的载具内休息。",
                metadata={"duration_minutes": action.minutes, "cause_event_ids": [source_event_id]})
            outcome = ActionService().execute(connection, world_id=actor["world_id"], tick_id=f"npc-selfcare:{source_event_id}:{actor['id']}", occurred_at=current, proposal=proposal)
            if not outcome.accepted:
                raise ValueError(outcome.rejection_reason or "本人休息未能开始")
            event_id, summary = outcome.event_id, outcome.summary
        else:
            item, kind, _ = _self_care_item(connection, actor, action.target_id, current)
            proposal = ActionProposal(actor_id=actor["id"], action=ActionType.USE if kind == "medicine" else ActionType.EAT,
                reason="本人明确选择使用自己实际携带的物品。",
                metadata={"item": item["name"], "item_id": item["id"], "cause_event_ids": [source_event_id],
                          "_npc_aboard_selfcare": True})
            if kind == "medicine":
                outcome = ActionService().execute(connection, world_id=actor["world_id"], tick_id=f"npc-selfcare:{source_event_id}:{actor['id']}", occurred_at=current, proposal=proposal)
                if not outcome.accepted:
                    raise ValueError(outcome.rejection_reason or "本人用药未能成立")
                event_id, summary = outcome.event_id, outcome.summary
            else:
                before = ActionService._observed_state(connection, actor["id"])
                inventory_before = InventoryService.snapshot(connection, actor["world_id"])
                effect = FoodService.eat(connection, actor, item, current)
                summary = f"{actor['name']}吃了一份自己携带的{item['name']}，补充{effect['nutrition']}点饱食。"
                from world_engine.rides import RideService
                state = RideService.state(connection, actor["world_id"], ride["vehicle_id"])
                event_id = _record_action(connection, actor, current, "action.eat", summary,
                    {"item_id": item["id"], "quantity": 1, "source_event_id": source_event_id,
                     "freshness": effect["freshness"], "aboard_selfcare": {"ride_id": ride["id"], "vehicle_id": ride["vehicle_id"]}},
                    location_id=state["current_location_id"])
                InventoryService.audit(connection, actor["world_id"], event_id, inventory_before)
                ActionService._record_effect(connection, actor["world_id"], event_id, proposal, current,
                    before, ActionService._observed_state(connection, actor["id"]), None, None)
        if not event_id:
            raise ValueError("本人照护缺少实际动作回执")
        connection.execute("INSERT INTO npc_selfcare_receipts VALUES (?,?,?,?,?,?,?)",
            (actor["world_id"], source_event_id, actor["id"], action.kind, action.target_id, action.minutes, event_id))
        WorldRepository().bump_version(connection, actor["world_id"])
        connection.execute("RELEASE npc_aboard_selfcare")
        return NpcActionResolution("applied", summary, event_id)
    except Exception:
        connection.execute("ROLLBACK TO npc_aboard_selfcare")
        connection.execute("RELEASE npc_aboard_selfcare")
        raise


def settle_stage_action(
    connection: sqlite3.Connection,
    *,
    npc,
    player_id: str | None,
    at: datetime,
    action: NpcActionIntent,
    source_event_id: str | None = None,
) -> NpcActionResolution:
    """Yuki 把动作交给世界服务；这里不凭模型文本直接改人物或物品。"""
    if action.kind in {"self_care", "self_rest"}:
        return _self_care_action(connection, npc, at, action, source_event_id)
    if action.kind == "cross_layer":
        from world_engine.cross_layer import CrossLayerPassageService
        result = CrossLayerPassageService.npc_decide(connection,npc['world_id'],npc['id'],action.target_id,action.operation,at,source_event_id)
        if action.operation == 'approach':
            return NpcActionResolution(result.get('approach_status', 'applied'), '对方自主开始向通道节点行走，尚未通过。', result['approach_event_id'])
        return NpcActionResolution('applied', '对方已自行婉拒通道邀请。' if result['status']=='declined' else '对方自行通过了公共升降通道。', result['npc_event_id'])
    # 哥哥，模型请求返回后本人状态可能变化，不能让旧快照替失能 NPC 行动。
    npc = connection.execute("SELECT * FROM characters WHERE id=? AND world_id=?", (_actor_id(npc), npc["world_id"])).fetchone()
    if npc is None or npc["is_player"] or npc["health"] <= 0:
        raise ValueError("当前 NPC 身体状态无法自行行动")
    if action.kind in {"door", "room"}:
        visible = {door["id"] for door in InteriorService.scene(connection, npc)["doors"]}
        if action.target_id not in visible:
            raise ValueError("目标门不在眼前")
        outcome = InteriorService.door(
            connection, npc, action.target_id, action.operation, at
        )
        return NpcActionResolution(
            "applied" if outcome.get("event_id") else "already",
            outcome["summary"],
            outcome.get("event_id"),
        )

    if action.kind == "give_item":
        if not player_id:
            raise ValueError("没有当面接收物品的玩家")
        player = connection.execute(
            "SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=1",
            (player_id, npc["world_id"]),
        ).fetchone()
        if player is None or not same_room(npc, player) or great_circle_distance_km(
            npc["longitude"], npc["latitude"], player["longitude"], player["latitude"]
        ) > VISIBLE_PERSON_RADIUS_KM:
            raise ValueError("玩家不在 NPC 身边")
        LifeActivityService.assert_available(connection, npc["id"])
        item = connection.execute(
            "SELECT i.*,t.name,t.stack_limit,t.slot_size FROM item_instances i "
            "JOIN item_types t ON t.id=i.item_type_id "
            "WHERE i.id=? AND i.world_id=? AND i.container_type='character_inventory' "
            "AND i.container_id=? AND i.owner_character_id=? AND i.quantity>=?",
            (action.target_id, npc["world_id"], npc["id"], npc["id"], action.quantity),
        ).fetchone()
        if item is None:
            raise ValueError("NPC 没有可转交的这件物品")
        before = InventoryService.snapshot(connection, npc["world_id"])
        InventoryService.transfer(connection, item, player_id, action.quantity)
        summary = f"{npc['name']}把{action.quantity}件{item['name']}交给了{player['name']}。"
        event_id = _record_action(
            connection, npc, at, "action.npc_give_item", summary,
            {"item_id": action.target_id, "quantity": action.quantity, "recipient_id": player_id},
        )
        InventoryService.audit(connection, npc["world_id"], event_id, before)
        ActionService._record_memory(
            connection,
            world_id=npc["world_id"],
            character_id=player_id,
            event_id=event_id,
            memory_type="experienced",
            summary=summary,
            importance=3,
        )
        WorldRepository().bump_version(connection, npc["world_id"])
        return NpcActionResolution("applied", summary, event_id)

    if action.kind == "travel":
        destinations = {item["id"]: item for item in travel_destinations(connection, npc)}
        destination = destinations.get(action.target_id)
        if destination is None:
            raise ValueError("目的地不在已知的地点列表中")
        row = connection.execute(
            "SELECT longitude,latitude FROM locations WHERE id=? AND world_id=?",
            (action.target_id, npc["world_id"]),
        ).fetchone()
        movement = MovementService().start(
            connection,
            world_id=npc["world_id"],
            character_id=npc["id"],
            destination_longitude=row["longitude"],
            destination_latitude=row["latitude"],
            world_time=at,
            record_log=False,
        )
        summary = f"{npc['name']}动身前往{destination['name']}，尚未到达。"
        event_id = _record_action(
            connection, npc, at, "action.movement_started", summary,
            {"movement_id": movement.id, "destination_id": action.target_id},
        )
        WorldRepository().bump_version(connection, npc["world_id"])
        return NpcActionResolution("applied", summary, event_id)

    raise ValueError("尚不支持这种 NPC 行动")
