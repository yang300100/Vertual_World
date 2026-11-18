"""哥哥，魔法科研用已有完整魔纹和有限器物供能，不从愿望、职业名或普通木料生成魔法。"""

import json
import math
from uuid import uuid4

from world_engine.event_log import record_event
from world_engine.principle_inventions import has_table
from world_engine.principle_types import CapabilityEffect
from world_engine.principle_workshop import accessible
from world_engine.repository import from_iso, to_iso

PUBLIC_RULES = [
    "当前文明以完整单纹为基础，不能仅凭愿望或普通文字生成法术。",
    "法师身体不储存施法魔力；供能来自真实储能器物或获准使用的设施。",
    "魔纹、响应介质和储能器物必须相容，容量与输出均有限。",
    "初次魔力引导需要真实传承记录，法师身份文字不等于已具施法资格。",
    "魔力转移存在损失；反射光、魔法照明和循环充能不能产生无限能源。",
]


def _confirmed(c, event_id, world_id, at):
    event = c.execute(
        "SELECT world_id,event_type,occurred_at FROM world_events WHERE id=?", (event_id,)
    ).fetchone()
    return bool(
        event
        and event["world_id"] == world_id
        and from_iso(event["occurred_at"]) <= at
        and event["event_type"]
        in {"world.registration_confirmed", "world.magic_profile_registered"}
    )


def public_context(c, actor, at):
    context = {
        "rules": PUBLIC_RULES,
        "can_project": False,
        "known_patterns": [],
        "available_devices": [],
    }
    if not has_table(c, "magic_patterns"):
        return context
    context["can_project"] = bool(
        c.execute(
            "SELECT 1 FROM magic_attunements t JOIN world_events e ON e.id=t.source_event_id "
            "WHERE t.actor_id=? AND t.world_id=? AND t.guided_at<=? AND e.world_id=t.world_id "
            "AND e.occurred_at<=t.guided_at "
            "AND e.event_type IN ('world.registration_confirmed','world.magic_profile_registered')",
            (actor["id"], actor["world_id"], to_iso(at)),
        ).fetchone()
    )
    context["known_patterns"] = [
        {k: r[k] for k in ("id", "name", "operation")}
        for r in c.execute(
            "SELECT p.id,p.name,p.operation,p.source_event_id,k.source_event_id knowledge_event "
            "FROM magic_known_patterns k JOIN magic_patterns p "
            "ON p.id=k.pattern_id AND p.world_id=k.world_id WHERE k.actor_id=? "
            "AND k.world_id=? AND k.learned_at<=?",
            (actor["id"], actor["world_id"], to_iso(at)),
        )
        if _confirmed(c, r["source_event_id"], actor["world_id"], at)
        and _confirmed(c, r["knowledge_event"], actor["world_id"], at)
    ]
    for row in c.execute(
        "SELECT i.*,t.name,d.stored,d.capacity,d.source_event_id FROM magic_energy_devices d "
        "JOIN item_instances i ON i.id=d.item_id AND i.world_id=d.world_id "
        "JOIN item_types t ON t.id=i.item_type_id WHERE d.world_id=? "
        "AND i.owner_character_id=? AND i.quantity>0 AND i.condition>0",
        (actor["world_id"], actor["id"]),
    ):
        if accessible(row, actor) and _confirmed(c, row["source_event_id"], actor["world_id"], at):
            # 哥哥，人物只感知档位；作者层精确容量和耦合分类不放入人物上下文。
            ratio = row["stored"] / row["capacity"]
            context["available_devices"].append(
                {
                    "item_id": row["id"],
                    "name": row["name"],
                    "charge": "充盈" if ratio >= 0.7 else "尚有余量" if ratio > 0 else "耗尽",
                    "can_receive": ratio < 1,
                }
            )
    return context


def _device(c, actor, item_id, key):
    row = c.execute(
        "SELECT d.*,i.id,i.owner_character_id,i.container_type,i.container_id,"
        "i.quantity,i.condition "
        "FROM magic_energy_devices d JOIN item_instances i "
        "ON i.id=d.item_id AND i.world_id=d.world_id "
        "WHERE d.item_id=? AND d.world_id=?",
        (item_id, actor["world_id"]),
    ).fetchone()
    if row is None or not accessible(row, actor) or row["quantity"] != 1 or row["condition"] <= 0:
        raise ValueError("缺少现场自有、已登记且可用的储能器物")
    if row["coupling_key"] != key:
        raise ValueError("储能器物与完整魔纹不相容")
    if any(not math.isfinite(row[k]) for k in ("capacity", "stored", "output_per_minute")):
        raise ValueError("器物容量、能量与输出必须有限")
    return row


def validate(c, actor, effect, at):
    if not has_table(c, "magic_patterns"):
        raise ValueError("世界尚未登记魔纹与储能器物，不能把普通物品当成魔法资源")
    plan = effect.magic_plan
    pattern = c.execute(
        "SELECT p.*,k.source_event_id knowledge_event FROM magic_patterns p "
        "JOIN magic_known_patterns k "
        "ON k.pattern_id=p.id AND k.world_id=p.world_id WHERE p.id=? AND p.world_id=? "
        "AND k.actor_id=? AND k.learned_at<=?",
        (plan.pattern_id, actor["world_id"], actor["id"], to_iso(at)),
    ).fetchone()
    if pattern is None:
        raise ValueError("研究者未实际掌握所引用的完整魔纹")
    if not _confirmed(c, pattern["source_event_id"], actor["world_id"], at) or not _confirmed(
        c, pattern["knowledge_event"], actor["world_id"], at
    ):
        raise ValueError("魔纹与知识来源尚未得到本世界确认")
    operation = "cast" if effect.kind == "magic_energy_cost" else "transfer"
    if pattern["operation"] != operation:
        raise ValueError("完整魔纹的固定功能不支持该魔法应用")
    if (
        operation == "cast"
        and not c.execute(
            "SELECT 1 FROM magic_attunements t JOIN world_events e ON e.id=t.source_event_id "
            "WHERE t.actor_id=? AND t.world_id=? AND t.guided_at<=? AND e.world_id=t.world_id "
            "AND e.occurred_at<=t.guided_at "
            "AND e.event_type IN ('world.registration_confirmed','world.magic_profile_registered')",
            (actor["id"], actor["world_id"], to_iso(at)),
        ).fetchone()
    ):
        raise ValueError("缺少真实引导与激活记录，不能凭法师称谓获得施法资格")
    for name in ("base_cost", "useful_output", "base_efficiency", "max_efficiency"):
        if not math.isfinite(pattern[name]):
            raise ValueError("魔纹能源参数必须有限")
    if pattern["base_cost"] * pattern["max_efficiency"] < pattern["useful_output"]:
        raise ValueError("魔纹登记参数违反输入、输出与损耗边界")
    source = _device(c, actor, plan.source_item_id, pattern["coupling_key"])
    if not _confirmed(c, source["source_event_id"], actor["world_id"], at):
        raise ValueError("器物来源尚未得到确认")
    receiver = None
    if operation == "transfer":
        if plan.receiver_item_id == plan.source_item_id:
            raise ValueError("不能把同一器物当作自充循环")
        receiver = _device(c, actor, plan.receiver_item_id, pattern["coupling_key"])
    if receiver and not _confirmed(c, receiver["source_event_id"], actor["world_id"], at):
        raise ValueError("接收器物来源尚未得到确认")
    return pattern, source, receiver


def seal_application(c, actor, reference, at):
    """哥哥，所有真实魔法供能活动在开始时绑定规则版本，不能等试验结束才改参数。"""
    if not reference.get("invention_id") or not has_table(c, "principle_inventions"):
        return
    row = c.execute(
        "SELECT * FROM principle_inventions WHERE id=? AND world_id=? AND status='approved'",
        (reference["invention_id"], actor["world_id"]),
    ).fetchone()
    if not row:
        return
    effect = CapabilityEffect.model_validate_json(row["effect_json"])
    if not effect.magic_plan:
        return
    from world_engine.phenomena import seal_magic
    from world_engine.principle_service import known_technologies

    if reference.get("technology_id") != row["technology_id"] or row["technology_id"] not in {
        t["id"] for t in known_technologies(c, actor["id"], at)
    }:
        raise ValueError("魔法研究活动须绑定本人实际掌握的已评审发明")
    pattern, _, _ = validate(c, actor, effect, at)
    rid = seal_magic(c, actor["world_id"], pattern, at)
    if rid:
        version = c.execute("SELECT version FROM phenomenon_rules WHERE id=?", (rid,)).fetchone()[0]
        return {"rule_id": rid, "version": version}


def settle(c, tech, actor, activity_id, at):
    effect = CapabilityEffect.model_validate_json(tech["effect_json"])
    pattern, source, receiver = validate(c, actor, effect, at)
    if effect.carrier == "tool":
        from world_engine.principle_workshop import prototype_accessible

        if not prototype_accessible(c, actor["id"], tech["id"], tech["invention_id"]):
            raise ValueError("魔法辅助工具的真实原型已不可用，不能仅凭曾制造记录获得效果")
    activity = c.execute(
        "SELECT * FROM character_life_activities WHERE id=? AND character_id=? "
        "AND world_id=? AND status='completed'",
        (activity_id, actor["id"], actor["world_id"]),
    ).fetchone()
    detail = c.execute(
        "SELECT spec_json FROM activity_task_details WHERE activity_id=?", (activity_id,)
    ).fetchone()
    reference = {"technology_id": tech["id"], "invention_id": tech["invention_id"]}
    work_spec = json.loads(detail[0]) if detail else {}
    declared = work_spec.get("principle_application")
    if (
        activity is None
        or detail is None
        or not declared
        or {k: declared.get(k) for k in reference} != reference
    ):
        raise ValueError("魔法结算须绑定已完成且明确选用该发明的活动")
    if from_iso(activity["ends_world_time"]) > at:
        raise ValueError("不能在真实供能活动结束之前登记魔法现象")
    previous = c.execute(
        "SELECT * FROM magic_applications WHERE invention_id=? AND activity_id=?",
        (tech["invention_id"], activity_id),
    ).fetchone()
    if previous:
        applied = c.execute(
            "SELECT 1 FROM principle_impacts WHERE invention_id=? AND source_id=?",
            (tech["invention_id"], activity_id),
        ).fetchone()
        return dict(previous) | {"status": "applied" if applied else "no_applicable_effect"}
    auxiliary_use_id = declared.get("auxiliary_use_id")
    if effect.carrier != "tool" and effect.materials:
        material_use = c.execute(
            "SELECT * FROM principle_material_uses WHERE id=? "
            "AND actor_id=? AND world_id=? AND technology_id=? AND purpose='magic_application'",
            (auxiliary_use_id, actor["id"], actor["world_id"], tech["id"]),
        ).fetchone()
        if (
            not material_use
            or c.execute(
                "SELECT 1 FROM magic_applications WHERE auxiliary_use_id=?", (auxiliary_use_id,)
            ).fetchone()
        ):
            raise ValueError("魔法耗材须绑定本次实际消耗记录，不能跳过或复用旧凭证")
        from collections import Counter

        expected, consumed = Counter(), Counter()
        for required in effect.materials:
            expected[required.kind] += required.quantity
        for used in json.loads(material_use["inputs_json"]):
            consumed[used["kind"]] += used["consumed"]
        if expected != consumed:
            raise ValueError("本次耗材与已评审魔法契约不一致")
    minutes = (
        from_iso(activity["ends_world_time"]) - from_iso(activity["started_world_time"])
    ).total_seconds() / 60
    from world_engine.phenomena import ensure_rule, record_magic
    from world_engine.phenomena_rules import REGISTRY

    outcome = REGISTRY.evaluate(
        "magic_energy",
        pattern["operation"],
        version=work_spec.get("phenomenon_rule_version"),
        source=source,
        pattern=pattern,
        minutes=minutes,
        factor=effect.factor,
        receiver=receiver,
    )
    if outcome.status != "computed":
        raise ValueError(outcome.reason)
    energy = outcome.outputs[0]
    spent, output = energy["spent"], energy["output"]
    before, after = energy["baseline"], energy["actual"]
    receiver_before = receiver["stored"] if receiver else None
    receiver_after = energy["receiver_after"]
    rid = ensure_rule(c, actor["world_id"], outcome, pattern["id"], at)
    if rid and work_spec.get("phenomenon_rule_id") != rid:
        raise ValueError("魔法活动没有绑定此规则来源，不能按其他规则补结算")
    receipt_id = str(uuid4())
    c.execute(
        "UPDATE magic_energy_devices SET stored=? WHERE item_id=?",
        (max(0, source["stored"] - spent), source["item_id"]),
    )
    if receiver:
        c.execute(
            "UPDATE magic_energy_devices SET stored=? WHERE item_id=?",
            (min(receiver["capacity"], receiver_after), receiver["item_id"]),
        )
    c.execute(
        "INSERT INTO magic_applications VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            receipt_id,
            actor["world_id"],
            actor["id"],
            tech["id"],
            tech["invention_id"],
            pattern["id"],
            source["item_id"],
            receiver["item_id"] if receiver else None,
            activity_id,
            source["stored"],
            source["stored"] - spent,
            receiver_before,
            receiver_after,
            output,
            spent - output,
            to_iso(at),
            auxiliary_use_id,
        ),
    )
    record_magic(c, receipt_id, outcome, at)
    record_event(
        c,
        world_id=actor["world_id"],
        tick_id="magic-application:" + receipt_id,
        occurred_at=at,
        event_type="world.magic_research_application",
        actor_id=actor["id"],
        location_id=actor["current_location_id"] or actor["location_id"],
        summary=f"{actor['name']}使用已知完整魔纹与真实储能器物完成“{tech['name']}”应用。",
        payload={
            "receipt_id": receipt_id,
            "pattern_id": pattern["id"],
            "source_item_id": source["item_id"],
        },
    )
    if before != after:
        from world_engine.principle_effects import record_impacts
        from world_engine.principle_inventions import record_reuse

        record_impacts(
            c, actor["id"], [(tech, before, after)], effect.kind, "life_activity", activity_id, at
        )
        record_reuse(c, tech, actor["id"], activity_id, at)
    return {
        "status": "applied" if before != after else "no_applicable_effect",
        "receipt_id": receipt_id,
        "activity_id": activity_id,
        "invention_id": tech["invention_id"],
        "technology_id": tech["id"],
        "source_consumed": spent,
        "output": output,
        "loss": spent - output,
    }
