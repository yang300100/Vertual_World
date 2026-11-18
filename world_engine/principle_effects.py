"""哥哥，科技影响作用于真实生活结算，采用记录本身不会假装已经产生收益。"""

import json
import math
from copy import deepcopy
from uuid import uuid4

from world_engine.event_log import record_event
from world_engine.materials import encoded
from world_engine.principle_service import prerequisites_known, table_exists
from world_engine.principle_types import CapabilityEffect
from world_engine.repository import to_iso


def effects(c, actor_id, kind, activity, at, technology_ids=None):
    if technology_ids is None or not table_exists(c):
        return []
    from world_engine.principle_inventions import candidates
    from world_engine.principle_workshop import has_skill, prototype_accessible

    result = []
    actor = c.execute("SELECT * FROM characters WHERE id=?", (actor_id,)).fetchone()
    for row in c.execute(
        "SELECT t.* FROM principle_adoptions a JOIN principle_technologies t "
        "ON t.id=a.technology_id AND t.world_id=a.world_id "
        "JOIN principle_knowledge k ON k.actor_id=a.actor_id AND k.technology_id=t.id "
        "WHERE a.actor_id=? AND a.adopted_at<=? AND k.learned_at<=? ORDER BY a.rowid",
        (actor_id, to_iso(at), to_iso(at)),
    ):
        if not prerequisites_known(c, actor_id, row["id"], at):
            continue
        options = candidates(c, row)
        selected = [
            o
            for o in options
            if o["selection_id"] in technology_ids
            or (len(options) == 1 and row["id"] in technology_ids)
        ]
        if len(selected) > 1:
            raise ValueError("同一项劳动须明确选择同一科技下的一项发明，不能自动叠加")
        for tech in selected:
            effect = CapabilityEffect.model_validate_json(tech["effect_json"])
            if effect.kind != kind or effect.activity != activity:
                continue
            if not tech["design"].get("adopt_if_approved", True):
                continue
            if not has_skill(c, actor, effect.required_skill, at):
                continue
            if effect.location_kinds:
                location = c.execute(
                    "SELECT kind FROM locations WHERE id=?",
                    (actor["current_location_id"] or actor["location_id"],),
                ).fetchone()
                if location is None or location[0] not in effect.location_kinds:
                    continue
            if effect.carrier == "tool" and not prototype_accessible(
                c, actor_id, row["id"], tech["invention_id"]
            ):
                continue
            result.append((tech, effect))
    return result


def adjusted(c, actor_id, kind, activity, value, at, technology_ids=None):
    current, applications, factor = value, [], 1.0
    for tech, effect in effects(c, actor_id, kind, activity, at, technology_ids):
        factor = (
            max(0.35, factor * effect.factor)
            if kind != "food_nutrition"
            else min(1.7, factor * effect.factor)
        )
        after = (
            max(1, math.ceil(value * factor)) if kind != "food_nutrition" else int(value * factor)
        )
        if after != current:
            applications.append((tech, current, after))
        current = after
    return current, applications


def record_impacts(c, actor_id, applications, kind, source_kind, source_id, at):
    actor = c.execute("SELECT * FROM characters WHERE id=?", (actor_id,)).fetchone()
    for tech, before, after in applications:
        if c.execute(
            "SELECT 1 FROM principle_impacts WHERE technology_id=? AND source_kind=? "
            "AND source_id=?",
            (tech["id"], source_kind, source_id),
        ).fetchone():
            continue
        iid = str(uuid4())
        label = {
            "labor_time": "劳动分钟",
            "labor_energy": "所需精力",
            "food_nutrition": "食物补给",
            "material_strength": "材料强度指数",
            "craft_precision": "加工精度指数",
            "magic_energy_cost": "施放魔纹所需储能",
            "magic_transfer_efficiency": "储能转移效率",
        }[kind]
        eid = record_event(
            c,
            world_id=actor["world_id"],
            tick_id="technology-use:" + iid,
            occurred_at=at,
            event_type="world.technology_impact",
            actor_id=actor_id,
            location_id=actor["current_location_id"] or actor["location_id"],
            summary=f"{actor['name']}应用“{tech['name']}”，{label}从{before}变为{after}。",
            payload={
                "technology_id": tech["id"],
                "invention_id": tech.get("invention_id"),
                "kind": kind,
                "before": before,
                "after": after,
                "source_kind": source_kind,
                "source_id": source_id,
            },
        )
        c.execute(
            "INSERT INTO principle_impacts(id,world_id,actor_id,technology_id,kind,source_kind,"
            "source_id,before_json,after_json,occurred_at,event_id,invention_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                iid,
                actor["world_id"],
                actor_id,
                tech["id"],
                kind,
                source_kind,
                source_id,
                encoded({label: before}),
                encoded({label: after}),
                to_iso(at),
                eid,
                tech.get("invention_id"),
            ),
        )


def adjust_task(c, actor, spec, at, technology_ids=None):
    result = deepcopy(spec)
    plans = []
    for kind, field in (("labor_time", "duration_minutes"), ("labor_energy", "energy_cost")):
        eligible = technology_ids
        if eligible is not None:
            from world_engine.principle_inventions import selection

            eligible = tuple(
                tid
                for tid in eligible
                if json.loads(selection(c, tid)["effect_json"])["required_skill"]
                in {None, spec.get("application_skill")}
            )

        value, applications = adjusted(
            c, actor["id"], kind, spec["kind"], spec[field], at, eligible
        )
        result[field] = value
        plans.extend(
            {
                "technology_id": tech["id"],
                "selection_id": tech["selection_id"],
                "kind": kind,
                "before": before,
                "after": after,
            }
            for tech, before, after in applications
        )
    if plans:
        result["principle_impact_plan"] = plans
    return result


def record_task(c, actor_id, spec, activity_id, at):
    for plan in spec.get("principle_impact_plan", []):
        from world_engine.principle_inventions import selection

        tech = selection(c, plan.get("selection_id", plan["technology_id"]))

        record_impacts(
            c,
            actor_id,
            [(tech, plan["before"], plan["after"])],
            plan["kind"],
            "life_activity",
            activity_id,
            at,
        )


def adjust_meal(c, actor, gain, at, technology_ids=None):
    changed, applications = adjusted(
        c, actor["id"], "food_nutrition", "food", gain, at, technology_ids
    )
    # 哥哥，饱食上限裁掉的数值不能登记成真实收益。
    room = max(0, 100 - actor["satiety"])
    actual = [
        (tech, min(room, before), min(room, after))
        for tech, before, after in applications
        if min(room, before) != min(room, after)
    ]
    return changed, actual
