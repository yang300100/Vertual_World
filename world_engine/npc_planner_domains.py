"""把领域事实实例化成同一个搜索器使用的行动算子。"""

from __future__ import annotations

import json

from world_engine.domain import ActionProposal, ActionType
from world_engine.geo import great_circle_distance_km
from world_engine.npc_operators import _local_food_options, near_location
from world_engine.npc_planning import (
    Condition,
    DomainOperation,
    Effect,
    FactValue,
    PlanningOperator,
    search,
)
from world_engine.repository import to_iso


def _proposal(actor, action, motive, **kwargs):
    return ActionProposal(actor_id=actor["id"], action=action, reason=motive, **kwargs)


def _travel(c, actor, location_id, motive, state, operators, *, requires=(), effects=()):
    location = c.execute("SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1",
                         (location_id, actor["world_id"])).fetchone()
    if not location:
        return
    if near_location(c, actor, location_id):
        state["place"] = location_id
    distance = great_circle_distance_km(actor["longitude"], actor["latitude"],
                                       location["longitude"], location["latitude"])
    operators.append(PlanningOperator(
        f"travel:{location_id}",
        (Condition("energy", 8, "ge"), Condition("place", location_id, "ne"), *requires),
        (Effect("place", location_id), Effect("energy", -1, True, 0, 100),
         Effect("satiety", -1, True, 0, 100), *effects),
        _proposal(actor, ActionType.TRAVEL, motive, destination_id=location_id),
        max(2, int(distance / actor["movement_speed_kmh"] * 2) + 2),
    ))


def _work(c, actor, motive, state, operators, location_ids=()):
    known = set(location_ids) | {actor["current_location_id"] or actor["location_id"], actor["location_id"]}
    for location_id in sorted(known):
        row = c.execute(
            "SELECT l.kind,a.balance,a.wage FROM locations l LEFT JOIN workplace_accounts a "
            "ON a.location_id=l.id AND a.world_id=l.world_id WHERE l.id=? AND l.world_id=?",
            (location_id, actor["world_id"]),
        ).fetchone()
        if not row or (row["wage"] is None and row["kind"] != "workplace"):
            continue
        wage = row["wage"] if row["wage"] is not None else 9
        if row["balance"] is not None and row["balance"] < wage:
            continue
        _travel(c, actor, location_id, motive, state, operators)
        operators.append(PlanningOperator(
            f"work:{location_id}", (Condition("energy", 20, "ge"), Condition("place", location_id)),
            (Effect("money", wage, True), Effect("energy", -16, True, 0, 100),
             Effect("satiety", -8, True, 0, 100)),
            _proposal(actor, ActionType.WORK, motive), 3,
        ))


def _food(c, actor, motive, at, state, operators):
    food = c.execute(
        "SELECT COALESCE(SUM(i.quantity),0) FROM item_instances i JOIN world_item_profiles p "
        "ON p.item_type_id=i.item_type_id AND p.world_id=i.world_id WHERE i.world_id=? "
        "AND i.container_type='character_inventory' AND i.container_id=? AND i.owner_character_id=? "
        "AND p.nutrition>0 AND (i.spoils_world_time IS NULL OR i.spoils_world_time>?)",
        (actor["world_id"], actor["id"], actor["id"], to_iso(at)),
    ).fetchone()[0]
    state["food"] = food
    operators.append(PlanningOperator(
        "eat", (Condition("food", 1, "ge"),),
        (Effect("food", -1, True), Effect("satiety", 42, True, 0, 100)),
        _proposal(actor, ActionType.EAT, motive), 1,
    ))
    price, harvestable = _local_food_options(c, actor, at)
    if price is not None:
        operators.append(PlanningOperator(
            "buy_food", (Condition("money", price, "ge"), Condition("space", 1, "ge")),
            (Effect("money", -price, True), Effect("food", 1, True)),
            DomainOperation("buy_food", ActionType.USE), 2,
        ))
    if harvestable:
        operators.append(PlanningOperator(
            "harvest_food", (Condition("energy", 3, "ge"), Condition("space", 1, "ge")),
            (Effect("energy", -3, True), Effect("food", 1, True)),
            DomainOperation("harvest_food", ActionType.GATHER), 2,
        ))


def _inventory(c, actor, predicate, motive, at, state, operators):
    from world_engine.life import LifeSceneService
    from world_engine.proximity import same_room

    output_id = str(predicate["item_type_id"])
    recipe = c.execute("SELECT spec_json FROM activity_recipes WHERE id=? AND world_id=?",
                       (predicate.get("recipe_id"), actor["world_id"])).fetchone()
    spec = json.loads(recipe[0]) if recipe else None
    needed = {output_id}
    if spec:
        needed.update(item["item_type_id"] for item in spec["ingredients"] + spec["tools"])
        _travel(c, actor, spec["location_id"], motive, state, operators)
    for item_id in sorted(needed):
        key = f"item:{item_id}"
        state[key] = c.execute(
            "SELECT COALESCE(SUM(quantity),0) FROM item_instances WHERE world_id=? "
            "AND container_type='character_inventory' AND container_id=? AND owner_character_id=? "
            "AND item_type_id=? AND condition>0 AND (spoils_world_time IS NULL OR spoils_world_time>?)",
            (actor["world_id"], actor["id"], actor["id"], item_id, to_iso(at)),
        ).fetchone()[0]
        if item_id != output_id:
            supply_output = f"output:{item_id}"
            state[supply_output] = c.execute(
                "SELECT COALESCE(SUM(i.quantity),0) FROM item_instances i "
                "JOIN character_life_activities a ON a.id=i.container_id "
                "JOIN activity_task_details d ON d.activity_id=a.id "
                "WHERE i.world_id=? AND i.container_type='activity_output' AND i.owner_character_id=? "
                "AND i.item_type_id=? AND a.location_id=? AND d.room_id IS ?",
                (actor["world_id"], actor["id"], item_id,
                 actor["current_location_id"] or actor["location_id"], actor["current_room_id"]),
            ).fetchone()[0]
            operators.append(PlanningOperator(
                f"collect_supply_output:{item_id}",
                (Condition(supply_output, 1, "ge"), Condition("space", 1, "ge")),
                (Effect(supply_output, -1, True), Effect(key, 1, True)),
                DomainOperation("collect_output", ActionType.GATHER, {"item_type_id": item_id}), 1,
            ))
        for item in LifeSceneService.accessible_items(c, actor):
            if item["item_type_id"] != item_id or item["container_type"] == "character_inventory" or item["owner_character_id"] not in {None, actor["id"]}:
                continue
            if item["condition"] <= 0 or (item["spoils_world_time"] and item["spoils_world_time"] <= to_iso(at)):
                continue
            ground = f"ground:{item['id']}"
            state[ground] = True
            operators.append(PlanningOperator(
                f"gather:{item['id']}", (Condition("space", item["slot_size"], "ge"), Condition(ground, True)),
                (Effect(key, item["quantity"], True), Effect(ground, False)),
                _proposal(actor, ActionType.GATHER, motive,
                          metadata={"item": item["name"], "item_id": item["id"]}), 1,
            ))
        location_id = actor["current_location_id"] or actor["location_id"]
        location = c.execute("SELECT resources_json FROM locations WHERE id=?", (location_id,)).fetchone()
        resources = json.loads(location[0]) if location else {}
        profile = c.execute("SELECT * FROM world_item_profiles WHERE world_id=? AND item_type_id=?",
                            (actor["world_id"], item_id)).fetchone()
        if profile and profile["resource_location_id"] in {None, location_id} and profile["resource_owner_id"] in {None, actor["id"]} and resources.get(profile["resource_key"], 0) > 0:
            resource = f"resource:{item_id}"
            state[resource] = resources[profile["resource_key"]]
            operators.append(PlanningOperator(
                f"harvest:{item_id}", (Condition("energy", 3, "ge"), Condition("space", 1, "ge"), Condition(resource, 1, "ge")),
                (Effect(key, 1, True), Effect("energy", -3, True), Effect(resource, -1, True)),
                DomainOperation("harvest", ActionType.GATHER, {"item_type_id": item_id}), 2,
            ))
        for seller in c.execute("SELECT * FROM characters WHERE world_id=? AND id!=? AND health>0 AND is_player=0",
                                (actor["world_id"], actor["id"])):
            if not same_room(actor, seller) or great_circle_distance_km(actor["longitude"], actor["latitude"], seller["longitude"], seller["latitude"]) > .1:
                continue
            if not any(word in (seller["identity"] or "") for word in ("商", "店", "贩", "医", "匠", "厨")):
                continue
            supply = c.execute(
                "SELECT SUM(i.quantity) AS quantity,MIN(p.price) AS price FROM item_instances i "
                "JOIN world_item_profiles p ON p.item_type_id=i.item_type_id AND p.world_id=i.world_id "
                "WHERE i.world_id=? AND i.container_type='character_inventory' AND i.container_id=? "
                "AND i.owner_character_id=? AND i.item_type_id=? AND i.quantity>0 "
                "AND (i.spoils_world_time IS NULL OR i.spoils_world_time>?)",
                (actor["world_id"], seller["id"], seller["id"], item_id, to_iso(at)),
            ).fetchone()
            if not supply["quantity"]:
                continue
            stock = f"stock:{seller['id']}:{item_id}"
            state[stock] = supply["quantity"]
            operators.append(PlanningOperator(
                f"buy_supply:{seller['id']}:{item_id}",
                (Condition("money", supply["price"], "ge"), Condition("space", 1, "ge"), Condition(stock, 1, "ge")),
                (Effect(key, 1, True), Effect("money", -supply["price"], True), Effect(stock, -1, True)),
                DomainOperation("buy_supply", ActionType.USE, {"item_type_id": item_id}), 2,
            ))
    ready = f"output:{output_id}"
    state[ready] = c.execute(
        "SELECT COALESCE(SUM(i.quantity),0) FROM item_instances i JOIN character_life_activities a "
        "ON a.id=i.container_id JOIN activity_task_details d ON d.activity_id=a.id "
        "WHERE i.world_id=? AND i.container_type='activity_output' AND i.owner_character_id=? "
        "AND i.item_type_id=? AND a.location_id=? AND d.room_id IS ?",
        (actor["world_id"], actor["id"], output_id, actor["current_location_id"] or actor["location_id"], actor["current_room_id"]),
    ).fetchone()[0]
    operators.append(PlanningOperator(
        "collect_output", (Condition(ready, 1, "ge"), Condition("space", 1, "ge")),
        (Effect(ready, -1, True), Effect(f"item:{output_id}", 1, True)),
        DomainOperation("collect_output", ActionType.GATHER, {"item_type_id": output_id}), 1,
    ))
    if spec and spec["kind"] == "craft" and spec["output_item_type_id"] == output_id:
        operators.append(PlanningOperator(
            f"craft:{predicate['recipe_id']}",
            (Condition("place", spec["location_id"]), Condition("energy", spec["energy_cost"], "ge"),
             *(Condition(f"item:{item['item_type_id']}", item["quantity"], "ge") for item in spec["ingredients"]),
             *(Condition(f"item:{tool['item_type_id']}", 1, "ge") for tool in spec["tools"])),
            (*(Effect(f"item:{item['item_type_id']}", -item["quantity"], True) for item in spec["ingredients"]),
             Effect("energy", -spec["energy_cost"], True), Effect(ready, spec["output_quantity"], True)),
            _proposal(actor, ActionType.ACTIVITY, motive, metadata={"recipe_id": predicate["recipe_id"]}), 3,
        ))


def _opportunity(c, actor, predicate, motive, state, operators):
    from world_engine.opportunities import OpportunityService

    row = OpportunityService._row(c, str(predicate["opportunity_id"]))
    if row["world_id"] != actor["world_id"] or row["status"] not in {"open", "accepted", "completed"}:
        return
    if row["accepted_by"] and row["accepted_by"] != actor["id"]:
        return
    state["accepted"] = row["status"] in {"accepted", "completed"}
    state["condition_met"] = bool(OpportunityService.accepted_condition(c, row)) if state["accepted"] else False
    state["paid"] = row["status"] == "completed"
    condition = json.loads(row["condition_json"])
    _travel(c, actor, row["issuer_location_id"], motive, state, operators)
    objective = condition["location_id"] if condition["kind"] == "at_location" else row["objective_location_id"]
    _travel(c, actor, objective, motive, state, operators,
            requires=(Condition("accepted", True),),
            effects=(Effect("condition_met", True),) if condition["kind"] == "at_location" else ())
    operators.extend([
        PlanningOperator("accept_opportunity", (Condition("accepted", False), Condition("place", row["issuer_location_id"])),
                         (Effect("accepted", True),), DomainOperation("accept_opportunity", ActionType.USE, {"opportunity_id": row["id"]}), 1),
        PlanningOperator("claim_opportunity", (Condition("accepted", True), Condition("condition_met", True), Condition("place", row["issuer_location_id"])),
                         (Effect("paid", True), Effect("money", row["reward_money"], True)), DomainOperation("claim_opportunity", ActionType.USE, {"opportunity_id": row["id"]}), 1),
    ])
    if condition["kind"] == "target_health_at_most":
        operators.append(PlanningOperator(
            "attack", (Condition("accepted", True), Condition("place", row["objective_location_id"]), Condition("condition_met", False), Condition("energy", 8, "ge")),
            (Effect("condition_met", True), Effect("energy", -8, True, 0, 100)),
            _proposal(actor, ActionType.ATTACK, motive, target_id=condition["target_id"]), 5,
        ))


def choose_action(c, actor, predicate, motive, at):
    carried = c.execute("SELECT i.quantity,t.stack_limit,t.slot_size FROM item_instances i JOIN item_types t "
                        "ON t.id=i.item_type_id WHERE i.world_id=? AND i.container_type='character_inventory' "
                        "AND i.container_id=? AND i.owner_character_id=? AND i.quantity>0",
                        (actor["world_id"], actor["id"], actor["id"])).fetchall()
    used = sum(((row["quantity"] + row["stack_limit"] - 1) // row["stack_limit"]) * row["slot_size"] for row in carried)
    state: dict[str, FactValue] = {"money": actor["money"], "energy": actor["energy"], "satiety": actor["satiety"], "health": actor["health"],
                                  "place": "", "space": max(0, actor["inventory_capacity"] - used)}
    operators = [PlanningOperator(
        "rest", (Condition("energy", 99, "le"),),
        (Effect("energy", 20, True, 0, 100),),
        _proposal(actor, ActionType.REST, motive, metadata={"duration_minutes": 240}), 3,
    )]
    kind = predicate["kind"]
    if kind in {"money_at_least", "satiety_at_least", "inventory_at_least", "health_at_least", "delivered_item", "item_condition_at_least"}:
        _work(c, actor, motive, state, operators, predicate.get("work_location_ids", ()))
    if kind == "satiety_at_least":
        _food(c, actor, motive, at, state, operators)
        goals = (Condition("satiety", int(predicate["amount"]), "ge"),)
    elif kind in {"money_at_least", "energy_at_least"}:
        goals = (Condition("money" if kind == "money_at_least" else "energy", int(predicate["amount"]), "ge"),)
    elif kind == "inventory_at_least":
        _inventory(c, actor, predicate, motive, at, state, operators)
        goals = (Condition(f"item:{predicate['item_type_id']}", int(predicate["amount"]), "ge"),)
    elif kind == "opportunity_rewarded":
        _opportunity(c, actor, predicate, motive, state, operators)
        goals = (Condition("paid", True),)
    elif kind in {"at_location", "spoke_about_event"}:
        _travel(c, actor, predicate["location_id"], motive, state, operators)
        if kind == "at_location":
            goals = (Condition("place", predicate["location_id"]),)
        else:
            operators.append(PlanningOperator(
                "socialize", (Condition("place", predicate["location_id"]),), (Effect("spoken", True),),
                _proposal(actor, ActionType.SOCIALIZE, motive, target_id=predicate["target_id"],
                          dialogue=predicate.get("dialogue", "我听闻此事，想了解现场发生了什么。"),
                          metadata={"share_event_id": predicate["event_id"]}), 1,
            ))
            goals = (Condition("spoken", True),)
    elif kind == "cooperation_operation":
        from world_engine.npc_cooperation import extend as extend_cooperation

        goals = extend_cooperation(c, actor, predicate, motive, at, state, operators)
    else:
        from world_engine.npc_planner_extended import extend

        goals = extend(c, actor, predicate, motive, at, state, operators)
        if goals is None:
            return None
    plan = search(state, goals, operators)
    if not plan:
        return None
    selected = plan[0].action
    if isinstance(selected, ActionProposal):
        selected.metadata["predicted_plan"] = [operator.name for operator in plan]
    return selected
