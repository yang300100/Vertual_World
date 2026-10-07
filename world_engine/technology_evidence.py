"""哥哥，能力判据必须早于真实劳动；认证只读实际量器和不可变来源账。"""

import json
import math
from uuid import uuid4

from world_engine.collective_evidence import visible_evidence
from world_engine.materials import encoded
from world_engine.repository import to_iso


def register_trial(c, candidate_id, revision, execution_id, control_execution_id, at):
    claim = c.execute(
        "SELECT * FROM science_candidate_revisions WHERE candidate_id=? AND revision=?",
        (candidate_id, revision),
    ).fetchone()
    executions = [
        c.execute("SELECT * FROM technology_executions WHERE id=?", (eid,)).fetchone()
        for eid in (execution_id, control_execution_id)
    ]
    if claim is None or any(e is None for e in executions) or execution_id == control_execution_id:
        raise ValueError("预登记需要存在的候选版本及两次实际执行")
    if any(
        e["world_id"] != claim["world_id"]
        or e["candidate_id"] != candidate_id
        or e["candidate_revision"] != revision
        or e["actor_id"] != claim["actor_id"]
        or e["status"] != "pending"
        or e["started_at"] is not None
        for e in executions
    ):
        raise ValueError("验证必须在同世界、同版本的实际工作开始前登记")
    if claim["created_at"] > to_iso(at) or any(e["created_at"] > to_iso(at) for e in executions):
        raise ValueError("不能倒填或引用未来验证登记")
    if c.execute(
        "SELECT 1 FROM science_claim_trials WHERE execution_id=?", (execution_id,)
    ).fetchone():
        raise ValueError("同一实际执行不能重复登记为不同复验")
    spec = json.loads(claim["spec_json"])
    c.execute(
        "INSERT INTO science_claim_trials VALUES (?,?,?,?,?,?,?,?)",
        (
            str(uuid4()),
            claim["world_id"],
            candidate_id,
            revision,
            execution_id,
            control_execution_id,
            encoded(spec["claim"]),
            to_iso(at),
        ),
    )


def ancestors(c, world_id, item_id, at):
    """哥哥，拆批、混合和转化沿实际物质账追踪，不靠物品名称猜来源。"""
    rows = c.execute(
        "SELECT * FROM material_ledger WHERE world_id=? AND world_time<=? ORDER BY rowid DESC",
        (world_id, to_iso(at)),
    ).fetchall()
    seen = {item_id}
    # 逆时间遍历保留混合前的来源，不会把目标后来的新来料倒灌到旧样品。
    for row in rows:
        ids = json.loads(row["item_ids_json"])
        if row["operation"] == "split" and len(ids) == 2 and ids[1] in seen:
            seen.add(ids[0])
        elif row["operation"] == "transform":
            count = len(json.loads(row["before_json"]))
            if seen & set(ids[count:]):
                seen.update(ids[:count])
        elif row["operation"] == "merge" and len(ids) == 2 and ids[1] in seen:
            seen.add(ids[0])
    return seen


def proves_improvement(value, baseline, direction, delta, precision):
    if value is None or baseline is None:
        return False
    signed = value - baseline if direction == "increase" else baseline - value
    return signed >= delta and signed > precision


def traditional_baseline(c, world_id, item_id, family, at):
    """哥哥，只有原传统实物及其纯拆分/运输样品能代表传统能力。"""
    seen = set()
    while item_id not in seen:
        seen.add(item_id)
        if c.execute(
            "SELECT 1 FROM science_reference_samples WHERE item_id=? AND world_id=? AND family=?",
            (item_id, world_id, family),
        ).fetchone():
            return True
        parent = None
        for row in c.execute(
            "SELECT * FROM material_ledger WHERE world_id=? AND world_time<=? ORDER BY rowid DESC",
            (world_id, to_iso(at)),
        ):
            ids = json.loads(row["item_ids_json"])
            if row["operation"] == "split" and len(ids) == 2 and ids[1] == item_id:
                parent = ids[0]
                break
            if row["operation"] == "transform":
                count = len(json.loads(row["before_json"]))
                if item_id in ids[count:]:
                    return False
            if row["operation"] == "merge" and ids[-1:] == [item_id]:
                return False
        if parent is None:
            return False
        item_id = parent
    return False


def conditions_hold(conditions, observed):
    for key, threshold in conditions.items():
        direction, field = key.split("_", 1) if key.startswith(("min_", "max_")) else ("equal", key)
        if field not in {"carrier", "mass", "water", "temperature"} or field not in observed:
            return False
        value = observed[field]
        if direction == "equal":
            if value != threshold:
                return False
        elif not isinstance(value, (int, float)) or not isinstance(threshold, (int, float)):
            return False
        elif direction == "min" and value < threshold or direction == "max" and value > threshold:
            return False
    return True


def validation_evidence(c, candidate, at):
    spec = json.loads(candidate["spec_json"])
    claim = spec["claim"]
    known = visible_evidence(c, candidate["actor_id"], at)
    supports, gaps, samples = [], [], set()
    rows = c.execute(
        "SELECT * FROM science_claim_trials WHERE candidate_id=? AND revision=? ORDER BY rowid",
        (candidate["id"], candidate["revision"]),
    ).fetchall()
    for trial in rows:
        executions = [
            c.execute(
                "SELECT * FROM technology_executions WHERE id=? AND world_id=?",
                (trial[key], candidate["world_id"]),
            ).fetchone()
            for key in ("execution_id", "control_execution_id")
        ]
        if any(
            e is None
            or e["status"] != "completed"
            or not e["completed_at"]
            or e["completed_at"] > to_iso(at)
            for e in executions
        ):
            gaps.append("验证或对照尚未实际完成")
            continue
        if json.loads(trial["claim_json"]) != claim or any(
            not e["started_at"] or trial["registered_at"] > e["started_at"] for e in executions
        ):
            gaps.append("主张不一致或验证判据没有提前登记")
            continue
        packets = []
        for e in executions:
            matches = []
            for oid in json.loads(e["observation_ids_json"]):
                packet = known.get(oid)
                if packet and packet["world_time"] <= to_iso(at):
                    content = packet["content"]
                    if (
                        content.get("completed")
                        and content.get("measurement", {}).get("metric") == claim["metric"]
                    ):
                        matches.append(packet)
            packets.append(matches[-1] if matches else None)
        if any(p is None for p in packets):
            gaps.append("缺少本人实际已知的同指标真实检验记录")
            continue
        measurement, control = [p["content"]["measurement"] for p in packets]
        a, b = measurement.get("sample_conditions", {}), control.get("sample_conditions", {})
        if not conditions_hold(claim.get("conditions", {}), a):
            gaps.append("主张适用条件尚未支持或实际测量不满足条件")
            continue
        if (
            not a
            or not b
            or a.get("carrier") != b.get("carrier")
            or not math.isclose(a.get("mass", -1), b.get("mass", -2), rel_tol=1e-6)
            or measurement.get("instrument_id") != control.get("instrument_id")
            or measurement.get("unit") != control.get("unit")
            or measurement["precision"] != control["precision"]
        ):
            gaps.append("材料载体、样品量或量器比较条件不匹配")
            continue
        traditional = traditional_baseline(
            c, candidate["world_id"], control["batch_id"], a["carrier"], at
        )
        if not traditional:
            gaps.append("对照没有可追溯的传统能力基线，不能以原料证明新科技")
            continue
        if not proves_improvement(
            measurement["value"],
            control["value"],
            claim["direction"],
            claim["minimum_delta"],
            measurement["precision"] + control["precision"],
        ):
            gaps.append("实测改进不足或差异未超过量器不确定性")
            continue
        orders = c.execute(
            (
                "SELECT o.* FROM science_execution_work s JOIN collective_work_ord"
                "ers o ON o.id=s.order_id WHERE s.execution_id=? ORDER BY s.step_i"
                "ndex"
            ),
            (executions[0]["id"],),
        ).fetchall()
        measured_sources = ancestors(c, candidate["world_id"], measurement["batch_id"], at)
        processed = [
            o
            for o in orders
            if json.loads(o["spec_json"])["operation"] != "assay"
            and measured_sources & set(json.loads(o["outputs_json"]))
        ]
        if not processed:
            gaps.append("受测样品不是本次处理产物；无关处理或只做观察不能证明能力")
            continue
        first = processed[0]
        physical = json.loads(first["inputs_json"])[0]
        entry = c.execute(
            "SELECT before_json FROM material_ledger WHERE world_id=? AND operation='transform' "
            "AND json_extract(item_ids_json,'$[0]')=? ORDER BY rowid LIMIT 1",
            (candidate["world_id"], physical),
        ).fetchone()
        input_mass = json.loads(entry[0])[0]["mass"] if entry else a["mass"]
        if physical in samples:
            gaps.append("同一实物不能通过重新引用冒充第二次复验")
            continue
        params = json.loads(executions[0]["parameters_json"])
        if supports and params != supports[0]["parameters"]:
            gaps.append("不同参数不是同一方法条件下的重复验证")
            continue
        samples.add(physical)
        supports.append(
            {
                "trial_id": trial["id"],
                "execution_id": executions[0]["id"],
                "control_execution_id": executions[1]["id"],
                "observation_ids": [p["id"] for p in packets],
                "sample_id": physical,
                "parameters": params,
                "carrier": a["carrier"],
                "mass": a["mass"],
                "input_mass": input_mass,
            }
        )
    if len(supports) < 2:
        gaps.append("至少需要两次独立实物处理与两份不同真实样品的重复验证")
    return supports, list(dict.fromkeys(gaps))
