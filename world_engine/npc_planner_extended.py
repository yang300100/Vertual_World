"""疗伤、物品交付与修理复用物资和行旅算子。"""

import json

from world_engine.domain import ActionType
from world_engine.npc_planning import Condition, Effect, PlanningOperator


def extend(c, actor, predicate, motive, at, state, operators):
    from world_engine.npc_planner_domains import _inventory, _proposal, _travel

    kind = predicate["kind"]
    if kind == "health_at_least":
        for item_type in c.execute(
            "SELECT DISTINCT t.id,t.name,t.heal FROM item_types t JOIN item_instances i "
            "ON i.item_type_id=t.id WHERE i.world_id=? AND t.category='consumable' AND t.heal>0",
            (actor["world_id"],),
        ).fetchall():
            _inventory(c, actor, {"item_type_id": item_type["id"]}, motive, at, state, operators)
            key = f"item:{item_type['id']}"
            operators.append(PlanningOperator(
                f"heal:{item_type['id']}", (Condition(key, 1, "ge"), Condition("health", 99, "le")),
                (Effect(key, -1, True), Effect("health", item_type["heal"], True, 0, 100)),
                _proposal(actor, ActionType.USE, motive, metadata={"item": item_type["name"]}), 1,
            ))
        return (Condition("health", int(predicate["amount"]), "ge"),)
    if kind == "delivered_item":
        _inventory(c, actor, predicate, motive, at, state, operators)
        _travel(c, actor, predicate["location_id"], motive, state, operators)
        item_type = c.execute("SELECT name FROM item_types WHERE id=?", (predicate["item_type_id"],)).fetchone()
        if not item_type:
            return None
        amount = min(20, int(predicate["amount"]))
        metadata = {"item": item_type["name"], "item_type_id": predicate["item_type_id"], "quantity": amount}
        if "minimum_condition" in predicate:
            # Yuki 按求助中的耐久要求挑选本人真实持有的原件，避免把损坏工具送过去。
            selected = c.execute(
                "SELECT id FROM item_instances WHERE world_id=? AND container_type='character_inventory' "
                "AND container_id=? AND owner_character_id=? AND item_type_id=? AND condition>=? "
                "AND quantity>=? AND (spoils_world_time IS NULL OR spoils_world_time>?) "
                "ORDER BY condition DESC,id LIMIT 1",
                (actor["world_id"], actor["id"], actor["id"], predicate["item_type_id"],
                 predicate["minimum_condition"], amount, at.isoformat()),
            ).fetchone()
            if not selected:
                return None
            metadata["item_id"] = selected[0]
        operators.append(PlanningOperator(
            "transfer", (Condition("place", predicate["location_id"]),
                         Condition(f"item:{predicate['item_type_id']}", amount, "ge")),
            (Effect(f"item:{predicate['item_type_id']}", -amount, True), Effect("delivered", True)),
            _proposal(actor, ActionType.TRANSFER, motive, target_id=predicate["target_id"],
                      metadata=metadata), 1,
        ))
        return (Condition("delivered", True),)
    if kind == "item_condition_at_least":
        item = c.execute(
            "SELECT * FROM item_instances WHERE id=? AND world_id=? AND owner_character_id=? AND quantity>0",
            (predicate["item_id"], actor["world_id"], actor["id"]),
        ).fetchone()
        recipe = c.execute("SELECT spec_json FROM activity_recipes WHERE id=? AND world_id=?",
                           (predicate.get("recipe_id"), actor["world_id"])).fetchone()
        if not item or not recipe:
            return None
        spec = json.loads(recipe[0])
        if spec["kind"] != "repair":
            return None
        _inventory(c, actor, {**predicate, "item_type_id": item["item_type_id"]}, motive, at, state, operators)
        state["item_condition"] = item["condition"]
        state["target_carried"] = item["container_type"] == "character_inventory" and item["container_id"] == actor["id"]
        if item["container_type"] == "activity_output":
            # Yuki 领取修好的原件后，下一次规划才会确认它真正回到随身背包。
            for operator in operators:
                if operator.name == "collect_output":
                    operators.append(PlanningOperator(
                        "collect_repaired_target", operator.preconditions,
                        (*operator.effects, Effect("target_carried", True)), operator.action, 1,
                    ))
                    break
        if item["container_type"] == "character_inventory":
            operators.append(PlanningOperator(
                f"repair:{predicate['recipe_id']}",
                (Condition("place", spec["location_id"]), Condition("target_carried", True),
                 Condition("energy", spec["energy_cost"], "ge"),
                 *(Condition(f"item:{entry['item_type_id']}", entry["quantity"], "ge") for entry in spec["ingredients"]),
                 *(Condition(f"item:{entry['item_type_id']}", 1, "ge") for entry in spec["tools"])),
                (*(Effect(f"item:{entry['item_type_id']}", -entry["quantity"], True) for entry in spec["ingredients"]),
                 Effect("energy", -spec["energy_cost"], True),
                 Effect("item_condition", spec["repair_points"], True, 0, 100),
                 Effect("target_carried", False), Effect(f"output:{item['item_type_id']}", 1, True)),
                _proposal(actor, ActionType.ACTIVITY, motive,
                          metadata={"recipe_id": predicate["recipe_id"], "target_item_id": item["id"]}), 3,
            ))
            # 预测中的领取仍走现有领取算子，实际检定结果必须在活动结束后读取。
            collect = next((operator for operator in operators if operator.name == "collect_output"), None)
            if collect:
                operators.append(PlanningOperator(
                    "collect_repaired_target", collect.preconditions,
                    (*collect.effects, Effect("target_carried", True)), collect.action, 1,
                ))
        return (Condition("item_condition", int(predicate["amount"]), "ge"), Condition("target_carried", True))
    return None
