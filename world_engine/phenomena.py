"""哥哥，现象只从真实结算取得；内部状态留作审计，人物只取得受限观察。"""

import json
from uuid import uuid4

from world_engine.materials import MaterialService, material_transaction
from world_engine.phenomena_rules import REGISTRY, canonical, fingerprint, rule_definition
from world_engine.principle_inventions import has_table
from world_engine.repository import from_iso, to_iso


def ensure_rule(c, world_id, outcome, scope_key, at):
    if not has_table(c, "phenomenon_rules"):
        return None
    if outcome.status != "computed":
        raise ValueError("未支持的机制不能登记为世界规则或实验证据")
    definition = rule_definition(outcome)
    digest = fingerprint(definition)
    row = c.execute(
        "SELECT * FROM phenomenon_rules WHERE world_id=? AND rule_key=? "
        "AND version=? AND scope_key=?",
        (world_id, outcome.rule_key, outcome.version, scope_key),
    ).fetchone()
    if row:
        if row["fingerprint"] != digest:
            raise ValueError("机制或材料参数已改变，不能沿用同一规则版本结算")
        return row["id"]
    rid = str(uuid4())
    c.execute(
        "INSERT INTO phenomenon_rules VALUES (?,?,?,?,?,?,?,?)",
        (
            rid,
            world_id,
            outcome.rule_key,
            outcome.version,
            scope_key,
            canonical(definition),
            digest,
            to_iso(at),
        ),
    )
    return rid


def _record(c, world_id, source_kind, source_id, operation, outcome, scope, observers, at, status):
    if not has_table(c, "phenomenon_events"):
        return None
    if outcome.status != "computed":
        raise ValueError("未计算的结果不能形成现象")
    audit = {
        "settlement_status": status,
        "source_kind": source_kind,
        "source_id": source_id,
        "calculation": outcome.audit,
    }
    facts = list(outcome.public_facts)
    if status != "completed":
        facts = [
            {
                "channel": "action",
                "code": "interrupted",
                "text": "工序中断，只记录实际发生的部分变化，不作为完成试验。",
            }
        ]
    digest = fingerprint(audit)
    with material_transaction(c):
        rule_id = ensure_rule(c, world_id, outcome, scope, at)
        previous = c.execute(
            "SELECT * FROM phenomenon_events WHERE world_id=? AND source_kind=? AND source_id=?",
            (world_id, source_kind, source_id),
        ).fetchone()
        if previous:
            if previous["audit_hash"] != digest or previous["public_facts_json"] != canonical(
                facts
            ):
                raise ValueError("同一结算来源不能改写现象或观察结果")
            return previous["id"]
        eid = str(uuid4())
        c.execute(
            "INSERT INTO phenomenon_events VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                eid,
                world_id,
                rule_id,
                source_kind,
                source_id,
                operation,
                to_iso(at),
                canonical(audit),
                digest,
                canonical(facts),
            ),
        )
        for observer in set(observers):
            if not c.execute(
                "SELECT 1 FROM characters WHERE id=? AND world_id=?", (observer, world_id)
            ).fetchone():
                raise ValueError("观察者必须属于现象发生的世界")
            c.execute(
                "INSERT INTO phenomenon_observations VALUES (?,?,?,?,?,?)",
                (str(uuid4()), eid, observer, world_id, canonical(facts), to_iso(at)),
            )
        return eid


def record_work(c, order_id, state, auxiliaries, outputs, hours, at):
    if not has_table(c, "phenomenon_events"):
        return None
    order = c.execute(
        "SELECT w.*,p.world_id FROM collective_work_orders w JOIN collective_proposals p "
        "ON p.id=w.proposal_id WHERE w.id=?",
        (order_id,),
    ).fetchone()
    if not order or order["status"] not in {"completed", "interrupted"}:
        raise ValueError("现象必须来自已经实际结算的工单")
    if order["status"] == "interrupted" and hours <= 0:
        # 哥哥，刚开始就中断仍应正常释放物资，只是不产生新的观察证据。
        return None
    task = json.loads(order["spec_json"])
    if task["operation"] != "assay" and not c.execute(
        "SELECT 1 FROM science_workshops WHERE world_id=?", (order["world_id"],)
    ).fetchone():
        # 哥哥，未接入本机制的旧工单沿原规则结算，不补造新框架的观察。
        return None
    people = c.execute(
        "SELECT r.actor_id,a.* FROM collective_work_people r JOIN character_life_activities a "
        "ON a.id=r.activity_id WHERE r.order_id=?",
        (order_id,),
    ).fetchall()
    observers = [
        p["actor_id"]
        for p in people
        if p["world_id"] == order["world_id"]
        and p["status"] in {"completed", "interrupted"}
        and from_iso(p["last_processed_world_time"]) > from_iso(p["started_world_time"])
    ]
    if not observers and order["status"] == "interrupted":
        return None
    if not observers or from_iso(order["due_at"]) > at and order["status"] == "completed":
        raise ValueError("尚无实际完成或参与劳动的观察者，不能提前生成现象")
    from world_engine.science_workshop import profile_for

    profile = profile_for(c, order["world_id"], state)
    if task["operation"] == "assay":
        if order["status"] != "completed":
            return None
        tool = dict(
            c.execute(
                "SELECT * FROM collective_instruments WHERE item_id=?", (task["instrument_id"],)
            ).fetchone()
        )
        outcome = REGISTRY.evaluate(
            "material_measurement",
            "assay",
            version=task.get("_phenomenon_version"),
            state=state,
            metric=task["method"],
            instrument=tool,
            profile=profile,
        )
        measured = json.loads(order["result_json"])["measurement"]
        if measured["value"] != outcome.outputs[0]["value"]:
            raise ValueError("现象读数与实际已结算量器记录不一致")
    else:
        outcome = REGISTRY.evaluate(
            "material_processing",
            task["operation"],
            version=task.get("_phenomenon_version"),
            state=state,
            settings=task["settings"],
            auxiliaries=auxiliaries,
            hours=hours,
            supplied_energy=task.get("_supplied_energy", 0),
            profile=profile,
        )
        actual_ids = json.loads(order["outputs_json"])
        actual = tuple(MaterialService.state(c, iid) for iid in actual_ids)
        if actual != tuple(outputs) or actual != outcome.outputs:
            raise ValueError("现象状态必须等于已入物质账的真实产物")
    return _record(
        c,
        order["world_id"],
        "work_order",
        order_id,
        task["operation"],
        outcome,
        state.carrier,
        observers,
        at,
        order["status"],
    )


def record_magic(c, receipt_id, outcome, at):
    row = c.execute(
        "SELECT r.*,a.status,a.ends_world_time,a.last_processed_world_time "
        "FROM magic_applications r "
        "JOIN character_life_activities a ON a.id=r.activity_id AND a.world_id=r.world_id "
        "AND a.character_id=r.actor_id WHERE r.id=?",
        (receipt_id,),
    ).fetchone()
    if not row or row["status"] != "completed" or from_iso(row["ends_world_time"]) > at:
        raise ValueError("魔法现象须绑定真实完成的供能活动与能源凭证")
    expected = outcome.outputs[0]
    if row["source_after"] != expected["source_after"] or row["output"] != expected["output"]:
        raise ValueError("魔法现象不等于实际能源账结果")
    source = c.execute(
        "SELECT stored FROM magic_energy_devices WHERE item_id=?", (row["source_item_id"],)
    ).fetchone()
    if source[0] != row["source_after"]:
        raise ValueError("魔法现象必须在实际能源更新之后登记")
    return _record(
        c,
        row["world_id"],
        "magic_application",
        receipt_id,
        outcome.audit["pattern"]["operation"],
        outcome,
        row["pattern_id"],
        [row["actor_id"]],
        at,
        "completed",
    )


def seal_magic(c, world_id, pattern, at):
    """哥哥，劳动开始前只冻结已验证魔纹参数，不产生现象、产物或观察。"""
    from world_engine.phenomena_rules import RuleOutcome

    entry = REGISTRY.specification("magic_energy")
    declaration = RuleOutcome(
        "computed", "magic_energy", entry[0].version, (), (), {"pattern": dict(pattern)}
    )
    return ensure_rule(c, world_id, declaration, pattern["id"], at)


def observations(c, actor_id, at):
    if not has_table(c, "phenomenon_observations"):
        return []
    return [
        {
            "id": row["id"],
            "event_id": row["event_id"],
            "source_kind": row["source_kind"],
            "source_id": row["source_id"],
            "operation": row["operation"],
            "rule_key": row["rule_key"],
            "rule_version": row["version"],
            "rule_fingerprint": row["fingerprint"],
            "facts": json.loads(row["facts_json"]),
            "observed_at": row["observed_at"],
        }
        for row in c.execute(
            "SELECT o.*,e.source_kind,e.source_id,e.operation,r.rule_key,r.version,r.fingerprint "
            "FROM phenomenon_observations o JOIN phenomenon_events e ON e.id=o.event_id "
            "AND e.world_id=o.world_id JOIN phenomenon_rules r ON r.id=e.rule_id "
            "JOIN characters a ON a.id=o.observer_id AND a.world_id=o.world_id "
            "WHERE o.observer_id=? AND o.observed_at<=? AND e.occurred_at<=? "
            "ORDER BY o.observed_at,o.rowid",
            (actor_id, to_iso(at), to_iso(at)),
        )
    ]


def actor_packet(value):
    """哥哥，保留量器实际读数，旧记录附带的内部样品数值不进入人物输入。"""
    if isinstance(value, list):
        return [actor_packet(v) for v in value]
    if isinstance(value, dict):
        return {
            key: ({"carrier": v["carrier"]} if isinstance(v, dict) and "carrier" in v else {})
            if key == "sample_conditions"
            else actor_packet(v)
            for key, v in value.items()
        }
    return value


def audit_world(c, world_id):
    """哥哥，完整审计供作者导出，绝不作为人物观察或公开读数。"""
    if not has_table(c, "phenomenon_events"):
        return {"rules": [], "events": [], "observations": [], "hashes_valid": True}
    rows = {
        "rules": [
            dict(r)
            for r in c.execute("SELECT * FROM phenomenon_rules WHERE world_id=?", (world_id,))
        ],
        "events": [
            dict(r)
            for r in c.execute("SELECT * FROM phenomenon_events WHERE world_id=?", (world_id,))
        ],
        "observations": [
            dict(r)
            for r in c.execute(
                "SELECT * FROM phenomenon_observations WHERE world_id=?", (world_id,)
            )
        ],
    }
    rows["hashes_valid"] = all(
        fingerprint(json.loads(r["definition_json"])) == r["fingerprint"] for r in rows["rules"]
    ) and all(fingerprint(json.loads(r["audit_json"])) == r["audit_hash"] for r in rows["events"])
    return rows
