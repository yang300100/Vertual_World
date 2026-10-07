"""NPC 可复用的目标谓词与行动候选。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from world_engine.domain import ActionProposal
from world_engine.geo import great_circle_distance_km
from world_engine.npc_planning import DomainOperation
from world_engine.repository import to_iso


def _local_food_options(connection: sqlite3.Connection, actor: sqlite3.Row,
                        at: datetime) -> tuple[int | None, bool]:
    from world_engine.proximity import same_room

    price = None
    for seller in connection.execute(
        "SELECT * FROM characters WHERE world_id=? AND id!=? AND is_player=0 AND health>0 "
        "AND COALESCE(current_location_id,location_id)=?",
        (actor["world_id"], actor["id"],
         actor["current_location_id"] or actor["location_id"]),
    ):
        if not any(word in (seller["identity"] or "") for word in
                   ("商", "店", "贩", "医", "匠", "厨")):
            continue
        if not same_room(actor, seller) or great_circle_distance_km(
            actor["longitude"], actor["latitude"], seller["longitude"], seller["latitude"],
        ) > .1:
            continue
        if (actor["current_location_id"] or actor["location_id"]) != (
            seller["current_location_id"] or seller["location_id"]
        ):
            continue
        found = connection.execute(
            "SELECT MIN(p.price) FROM item_instances i JOIN world_item_profiles p "
            "ON p.item_type_id=i.item_type_id AND p.world_id=i.world_id "
            "WHERE i.world_id=? AND i.container_type='character_inventory' "
            "AND i.container_id=? AND i.owner_character_id=? AND i.quantity>0 "
            "AND p.nutrition>0 AND (i.spoils_world_time IS NULL OR i.spoils_world_time>?)",
            (actor["world_id"], seller["id"], seller["id"], to_iso(at)),
        ).fetchone()[0]
        if found is not None:
            price = found if price is None else min(price, found)
    location = connection.execute("SELECT resources_json FROM locations WHERE id=?",
                                  (actor["current_location_id"],)).fetchone()
    resources = json.loads(location[0] or "{}") if location else {}
    harvestable = any(
        profile["resource_key"] and resources.get(profile["resource_key"], 0) > 0
        for profile in connection.execute(
            "SELECT resource_key FROM world_item_profiles WHERE world_id=? AND nutrition>0 "
            "AND (resource_location_id=? OR resource_location_id IS NULL) "
            "AND (resource_owner_id IS NULL OR resource_owner_id=?)",
            (actor["world_id"], actor["current_location_id"], actor["id"]),
        )
    )
    return price, harvestable


def near_location(connection: sqlite3.Connection, actor: sqlite3.Row,
                  location_id: str) -> bool:
    place = connection.execute(
        "SELECT longitude,latitude FROM locations WHERE id=? AND world_id=? AND is_active=1",
        (location_id, actor["world_id"]),
    ).fetchone()
    return bool(place and great_circle_distance_km(
        actor["longitude"], actor["latitude"], place["longitude"], place["latitude"],
    ) <= .1)


def satisfied(connection: sqlite3.Connection, actor: sqlite3.Row,
              predicate: dict[str, object], goal_id: str | None = None, *, at: datetime | None = None) -> bool:
    kind = predicate.get("kind")
    if kind == "cooperation_operation":
        from world_engine.npc_cooperation import satisfied as cooperation_satisfied

        return cooperation_satisfied(connection, actor, predicate, at)
    if kind == "money_at_least":
        return actor["money"] >= int(predicate["amount"])
    if kind == "satiety_at_least":
        return actor["satiety"] >= int(predicate["amount"])
    if kind == "energy_at_least":
        return actor["energy"] >= int(predicate["amount"])
    if kind == "health_at_least":
        return actor["health"] >= int(predicate["amount"])
    if kind == "item_condition_at_least":
        item = connection.execute(
            "SELECT condition FROM item_instances WHERE id=? AND world_id=? AND owner_character_id=? "
            "AND container_type='character_inventory' AND container_id=? AND quantity>0",
            (predicate["item_id"], actor["world_id"], actor["id"], actor["id"]),
        ).fetchone()
        return bool(item and item["condition"] >= int(predicate["amount"]))
    if kind == "delivered_item":
        if not goal_id:
            return False
        delivered = connection.execute(
            "SELECT COALESCE(SUM(json_extract(e.payload_json,'$.metadata.quantity')),0) "
            "FROM npc_agency_steps s JOIN world_events e ON e.id=s.event_id "
            "WHERE s.goal_id=? AND s.action_type='transfer' AND s.status='accepted' "
            "AND e.actor_id=? AND e.target_id=? AND json_extract(e.payload_json,'$.metadata.item_type_id')=?",
            (goal_id, actor["id"], predicate["target_id"], predicate["item_type_id"]),
        ).fetchone()[0]
        return delivered >= int(predicate["amount"])
    if kind == "inventory_at_least":
        world_time = to_iso(at) if at else connection.execute(
            "SELECT current_time FROM worlds WHERE id=?", (actor["world_id"],),
        ).fetchone()[0]
        amount = connection.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM item_instances WHERE world_id=? "
            "AND container_type='character_inventory' AND container_id=? AND owner_character_id=? "
            "AND item_type_id=? AND condition>0 AND (spoils_world_time IS NULL OR spoils_world_time>?)",
            (actor["world_id"], actor["id"], actor["id"], predicate["item_type_id"], world_time),
        ).fetchone()[0]
        return amount >= int(predicate["amount"])
    if kind == "at_location":
        return near_location(connection, actor, str(predicate["location_id"]))
    if kind == "spoke_about_event":
        if not goal_id:
            return False
        return connection.execute(
            "SELECT 1 FROM npc_agency_steps s JOIN world_events e ON e.id=s.event_id "
            "WHERE s.goal_id=? AND s.action_type='socialize' AND s.status='accepted' "
            "AND e.target_id=? LIMIT 1", (goal_id, predicate["target_id"]),
        ).fetchone() is not None
    if kind == "opportunity_rewarded":
        return connection.execute(
            "SELECT 1 FROM npc_opportunities WHERE id=? AND status='completed' "
            "AND accepted_by=? AND payout_event_id IS NOT NULL",
            (predicate["opportunity_id"], actor["id"]),
        ).fetchone() is not None
    raise ValueError(f"不支持的目标谓词：{kind}")


def next_action(
    connection: sqlite3.Connection, actor: sqlite3.Row,
    predicate: dict[str, object], motive: str, at: datetime,
) -> ActionProposal | DomainOperation | None:
    """Yuki 用同一个有界搜索器组合领域行动，每步都重新核对真实状态。"""
    from world_engine.npc_planner_domains import choose_action

    if satisfied(connection, actor, predicate, at=at):
        return None
    return choose_action(connection, actor, predicate, motive, at)


def predicate_from_json(value: str) -> dict[str, object]:
    result = json.loads(value)
    if not isinstance(result, dict):
        raise ValueError("目标谓词必须是对象")
    return result
