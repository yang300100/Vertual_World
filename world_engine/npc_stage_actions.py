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
from world_engine.repository import WorldRepository


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


def _record_action(
    connection, npc, at: datetime, event_type: str, summary: str, payload: dict
) -> str:
    event_id = ActionService._record_event(
        connection,
        world_id=npc["world_id"],
        tick_id=f"npc-performance:{npc['id']}:{at.isoformat()}",
        occurred_at=at,
        event_type=event_type,
        actor_id=npc["id"],
        target_id=payload.get("recipient_id"),
        location_id=npc["current_location_id"] or npc["location_id"],
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


def settle_stage_action(
    connection: sqlite3.Connection,
    *,
    npc,
    player_id: str | None,
    at: datetime,
    action: NpcActionIntent,
) -> NpcActionResolution:
    """Yuki 把动作交给世界服务；这里不凭模型文本直接改人物或物品。"""
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
