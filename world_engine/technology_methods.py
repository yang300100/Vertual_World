"""哥哥，科技方法仍须绑定实物，调用痕迹只在真实工单完成后形成依赖。"""

from __future__ import annotations

import json
from dataclasses import dataclass

from pydantic import Field

from world_engine import collective_resources as resources
from world_engine.collective_research import CollectiveResearchService as R
from world_engine.materials import MaterialService as M
from world_engine.materials import encoded
from world_engine.repository import to_iso
from world_engine.science_types import MethodSpec, MethodStep
from world_engine.technology_evidence import ancestors


class ExpandedMethod(MethodSpec):
    steps: list[MethodStep] = Field(min_length=1, max_length=96)


@dataclass
class Compilation:
    method: ExpandedMethod
    bindings: dict
    parameters: dict
    spans: list[dict]


def would_cycle(edges, parent, child):
    frontier, seen = [child], set()
    while frontier:
        node = frontier.pop()
        if node == parent:
            return True
        if node in seen:
            continue
        seen.add(node)
        frontier.extend(b for a, b in edges if a == node)
    return False


def verified_version(c, actor_id, technology_id, version, at):
    actor = R.actor(c, actor_id)
    row = c.execute(
        "SELECT v.*,t.status,COALESCE(s.status,'verified') version_status "
        "FROM technology_versions v JOIN technologies t ON t.id=v.technology_id "
        "LEFT JOIN science_version_states s ON s.technology_id=v.technology_id "
        "AND s.version=v.version WHERE v.technology_id=? AND v.version=? AND v.world_id=?",
        (technology_id, version, actor["world_id"]),
    ).fetchone()
    known = c.execute(
        "SELECT 1 FROM technology_knowledge WHERE actor_id=? AND technology_id=? "
        "AND version=? AND world_id=? AND learned_at<=?",
        (actor_id, technology_id, version, actor["world_id"], to_iso(at)),
    ).fetchone()
    if row is None or known is None:
        raise ValueError("本人未知或不存在同世界的科技方法版本")
    if row["status"] != "verified" or row["version_status"] != "verified":
        raise ValueError("科技方法已失效或待复核，不能作为可靠能力")
    return row


def check_bindings(c, actor_id, method, bindings):
    actor = R.actor(c, actor_id)
    if set(bindings) != set(method.inputs):
        raise ValueError("方法输入角色必须完整且无额外绑定")
    for role, kind in method.inputs.items():
        item = c.execute(
            "SELECT * FROM item_instances WHERE id=? AND world_id=?",
            (bindings[role], actor["world_id"]),
        ).fetchone()
        if item is None or item["quantity"] < 1 or item["condition"] <= 0:
            raise ValueError("方法没有实际可用实物")
        if item["owner_character_id"] != actor_id or not resources.accessible(c, item, actor):
            raise ValueError("方法实物不在场或本人没有物权")
        if actor["current_room_id"]:
            raise ValueError("人物不在实际工坊现场")
        tables = {
            "instrument": "collective_instruments",
            "apparatus": "collective_apparatus",
            "energy": "science_energy_stocks",
        }
        if kind == "material" and M.state(c, item["id"]) is None:
            raise ValueError("材料角色没有真实材料状态")
        if (
            kind in tables
            and not c.execute(
                f"SELECT 1 FROM {tables[kind]} WHERE item_id=? AND world_id=?",
                (item["id"], actor["world_id"]),
            ).fetchone()
        ):
            raise ValueError("方法绑定的实物类型不匹配")


def actual_parameters(method, parameters):
    if set(parameters) - set(method.parameters):
        raise ValueError("方法参数未声明")
    values = {}
    for key, spec in method.parameters.items():
        value = parameters.get(key, spec.default)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not spec.minimum <= value <= spec.maximum
        ):
            raise ValueError("方法参数超出认证适用范围")
        values[key] = value
    return values


def compile_method(c, actor_id, method, bindings, parameters, at, *, root_identity=None):
    method = method if isinstance(method, MethodSpec) else MethodSpec.model_validate(method)
    actual = actual_parameters(method, parameters)
    output, spans = [], []

    def expand(current, refs, params, stack):
        local = {}
        last_product = None

        def reference(value):
            if value in refs:
                return refs[value]
            key, index = value[1:].rsplit(":", 1)
            if key not in local:
                raise ValueError("方法未取得先前真实产物引用")
            return "$" + local[key] + ":" + index

        for step in current.steps:
            raw = step.model_dump(mode="json")
            if step.op == "use_method":
                node = (step.technology_id, step.version)
                if node in stack or len(stack) >= 4:
                    raise ValueError("方法调用成环或超过4层深度")
                parent = verified_version(c, actor_id, *node, at)
                parent_method = MethodSpec.model_validate_json(parent["method_json"])
                pvalues = {
                    k: params[v[6:]] if isinstance(v, str) else v
                    for k, v in step.parameters.items()
                }
                pvalues = actual_parameters(parent_method, pvalues)
                validated = json.loads(parent["conditions_json"]).get("validated_parameters")
                if validated is not None and pvalues != validated:
                    raise ValueError("父方法参数不属于已真实复验的范围")
                mapped = {k: reference(v) for k, v in step.bindings.items()}
                if set(mapped) != set(parent_method.inputs):
                    raise ValueError("父方法输入角色绑定不完整")
                for key, kind in parent_method.inputs.items():
                    target = mapped[key]
                    if target in method.inputs and method.inputs[target] != kind:
                        raise ValueError("父方法输入角色类型不匹配")
                begin = len(output)
                product = expand(parent_method, mapped, pvalues, [*stack, node])
                spans.append(
                    {
                        "technology_id": node[0],
                        "version": node[1],
                        "start_step": begin,
                        "end_step": len(output) - 1,
                    }
                )
                if step.result_key:
                    if product is None:
                        raise ValueError("父方法没有实物产物可引用")
                    local[step.result_key] = product
                last_product = product
                continue
            for key in ("input", "instrument", "apparatus", "energy"):
                if raw.get(key):
                    raw[key] = reference(raw[key])
            if "auxiliary_inputs" in raw:
                raw["auxiliary_inputs"] = [reference(v) for v in raw["auxiliary_inputs"]]
                for key, value in raw["settings"].items():
                    if isinstance(value, str):
                        raw["settings"][key] = params[value[6:]]
            key = "compiled" + str(len(output))
            if step.result_key:
                local[step.result_key] = key
            raw["result_key"] = key
            output.append(raw)
            if len(output) > 96:
                raise ValueError("方法展开超过96步上限")
            if step.op != "assay":
                last_product = key
        return last_product

    expand(method, {k: k for k in method.inputs}, actual, [root_identity or ("__blueprint__", 0)])
    result = ExpandedMethod.model_validate({"inputs": method.inputs, "steps": output})
    return Compilation(result, bindings, {}, spans)


def instantiate_method(c, actor_id, technology_id, version, bindings, parameters, at):
    record = verified_version(c, actor_id, technology_id, version, at)
    method = MethodSpec.model_validate_json(record["method_json"])
    check_bindings(c, actor_id, method, bindings)
    actual = actual_parameters(method, parameters)
    validated = json.loads(record["conditions_json"]).get("validated_parameters")
    if validated is not None and actual != validated:
        raise ValueError("参数点尚未经过真实复验")
    compiled = compile_method(
        c, actor_id, method, bindings, actual, at, root_identity=(technology_id, version)
    )
    return [step.model_dump(mode="json") for step in compiled.method.steps]


def execute_method(
    c,
    session_id,
    actor_id,
    technology_id,
    version,
    bindings,
    parameters,
    at,
    *,
    source_program_id=None,
):
    from world_engine.science_programs import start_execution

    record = verified_version(c, actor_id, technology_id, version, at)
    instantiate_method(c, actor_id, technology_id, version, bindings, parameters, at)
    return start_execution(
        c,
        session_id,
        actor_id,
        json.loads(record["method_json"]),
        bindings,
        parameters,
        at,
        source_program_id=source_program_id,
        technology_id=technology_id,
        version=version,
    )


def record_execution_source(c, execution_id, source_execution_id, item_id, at):
    rows = [
        c.execute("SELECT * FROM technology_executions WHERE id=?", (eid,)).fetchone()
        for eid in (execution_id, source_execution_id)
    ]
    if (
        any(r is None for r in rows)
        or rows[0]["world_id"] != rows[1]["world_id"]
        or execution_id == source_execution_id
    ):
        raise ValueError("方法来源须为同世界不同真实执行")
    if rows[1]["status"] != "completed" or not item_id:
        raise ValueError("只有完成的实际产物才能成为材料来源")
    outputs = set(json.loads(rows[1]["output_ids_json"]))
    actual_inputs = {
        iid
        for r in c.execute(
            (
                "SELECT o.inputs_json FROM science_execution_work s JOIN "
                "collective_work_orders o ON o.id=s.order_id WHERE s.execution_id=?"
            ),
            (execution_id,),
        )
        for iid in json.loads(r[0])
    }
    if item_id not in actual_inputs or not ancestors(c, rows[0]["world_id"], item_id, at) & outputs:
        raise ValueError("尚无实际消耗父产物的物质轨迹")
    c.execute(
        (
            "INSERT OR IGNORE INTO "
            "science_execution_sources(world_id,execution_id,source_execution_id,item"
            "_id,created_at) VALUES (?,?,?,?,?)"
        ),
        (rows[0]["world_id"], execution_id, source_execution_id, item_id, to_iso(at)),
    )


def link_dependencies(c, technology_id, version, supports, at):
    child = c.execute(
        "SELECT world_id,inventor_id FROM technologies WHERE id=?", (technology_id,)
    ).fetchone()
    world_id = child["world_id"]
    edges = [
        tuple(r)
        for r in c.execute(
            "SELECT parent_id,child_id FROM technology_dependencies WHERE world_id=?", (world_id,)
        )
    ]
    for support in supports:
        eid = support["execution_id"]
        parents = [
            (r["technology_id"], r["version"], eid)
            for r in c.execute("SELECT * FROM science_method_spans WHERE execution_id=?", (eid,))
        ]
        for row in c.execute(
            (
                "SELECT p.technology_id,p.version,s.source_execution_id FROM "
                "science_execution_sources s JOIN technology_executions p ON "
                "p.id=s.source_execution_id WHERE s.execution_id=? AND p.technology_id IS "
                "NOT NULL"
            ),
            (eid,),
        ):
            parents.append(tuple(row))
        for parent, parent_version, source_id in parents:
            verified_version(c, child["inventor_id"], parent, parent_version, at)
            if parent == technology_id or would_cycle(edges, parent, technology_id):
                raise ValueError("实际方法依赖将产生科技循环")
            c.execute(
                "INSERT OR IGNORE INTO technology_dependencies VALUES (?,?,?,?,?,?,?)",
                (world_id, parent, parent_version, technology_id, version, source_id, to_iso(at)),
            )
            edges.append((parent, technology_id))


def check_work_span(c, order, worker, state, at, *, material_ids=()):
    row = c.execute(
        "SELECT execution_id,step_index FROM science_execution_work WHERE order_id=?",
        (order["id"],),
    ).fetchone()
    if row is None:
        return
    for iid in material_ids:
        source_ids = ancestors(c, worker["world_id"], iid, at)
        for parent in c.execute(
            "SELECT * FROM technology_executions WHERE world_id=? AND technology_id IS NOT NULL "
            "AND status='completed' AND completed_at<=?",
            (worker["world_id"], to_iso(at)),
        ):
            if source_ids & set(json.loads(parent["output_ids_json"])):
                verified_version(c, worker["id"], parent["technology_id"], parent["version"], at)
    spans = c.execute(
        "SELECT * FROM science_method_spans WHERE execution_id=? AND start_step=?",
        (row["execution_id"], row["step_index"]),
    ).fetchall()
    execution = c.execute(
        "SELECT * FROM technology_executions WHERE id=?", (row["execution_id"],)
    ).fetchone()
    versions = [(s["technology_id"], s["version"]) for s in spans]
    if execution["technology_id"] and row["step_index"] == 0:
        versions.append((execution["technology_id"], execution["version"]))
    for technology_id, version in versions:
        record = verified_version(c, worker["id"], technology_id, version, at)
        conditions = json.loads(record["conditions_json"])
        if conditions.get("validated_carrier", state.carrier) != state.carrier:
            raise ValueError("父方法实物载体不属于已验证条件")
        if (
            "validated_input_mass" in conditions
            and abs(conditions["validated_input_mass"] - state.mass) > 1e-6
        ):
            raise ValueError("父方法实际处理样品量不属于已验证条件")


def track_material_sources(c, execution_id, at):
    execution = c.execute(
        "SELECT * FROM technology_executions WHERE id=?", (execution_id,)
    ).fetchone()
    inputs = {
        iid
        for r in c.execute(
            (
                "SELECT o.inputs_json FROM science_execution_work s JOIN "
                "collective_work_orders o ON o.id=s.order_id WHERE s.execution_id=?"
            ),
            (execution_id,),
        )
        for iid in json.loads(r[0])
    }
    parents = c.execute(
        (
            "SELECT * FROM technology_executions WHERE world_id=? AND id!=? AND "
            "status='completed' AND completed_at<=? AND technology_id IS NOT NULL"
        ),
        (execution["world_id"], execution_id, to_iso(at)),
    ).fetchall()
    for iid in inputs:
        source_ids = ancestors(c, execution["world_id"], iid, at)
        for parent in parents:
            if source_ids & set(json.loads(parent["output_ids_json"])):
                record_execution_source(c, execution_id, parent["id"], iid, at)


def invalidate_from_evidence(c, technology_id, actor_id, evidence_ids, reason, at):
    from world_engine.collective_evidence import visible_evidence
    from world_engine.materials import material_transaction
    from world_engine.technology_evidence import proves_improvement

    actor = R.actor(c, actor_id)
    technology = c.execute(
        "SELECT * FROM technologies WHERE id=? AND world_id=?", (technology_id, actor["world_id"])
    ).fetchone()
    known = visible_evidence(c, actor_id, at)
    if (
        technology is None
        or not evidence_ids
        or set(evidence_ids) - set(known)
        or not str(reason).strip()
    ):
        raise ValueError("失效必须有本人实际取得的真实反证")
    version = technology["current_version"]
    record = c.execute(
        "SELECT * FROM technology_versions WHERE technology_id=? AND version=?",
        (technology_id, version),
    ).fetchone()
    claim = json.loads(record["claim_json"])
    controls = [
        json.loads(r[0]).get("measurement")
        for r in c.execute(
            (
                "SELECT o.content_json FROM technology_evidence e JOIN "
                "collective_observations o ON o.id=e.observation_id WHERE "
                "e.technology_id=? AND e.version=? AND e.role='control'"
            ),
            (technology_id, version),
        )
    ]
    found = False
    for oid in evidence_ids:
        measured = known[oid]["content"].get("measurement")
        execution = c.execute(
            (
                "SELECT * FROM technology_executions WHERE technology_id=? AND version=? "
                "AND world_id=? AND status='completed' AND completed_at<=? AND "
                "EXISTS(SELECT 1 FROM json_each(observation_ids_json) WHERE value=?)"
            ),
            (technology_id, version, actor["world_id"], to_iso(at), oid),
        ).fetchone()
        if (
            not measured
            or not execution
            or measured["metric"] != claim["metric"]
            or measured["value"] is None
        ):
            continue
        for control in controls:
            if (
                control
                and control["value"] is not None
                and measured.get("unit") == control.get("unit")
                and measured.get("sample_conditions", {}).get("carrier")
                == control.get("sample_conditions", {}).get("carrier")
            ):
                precision = measured["precision"] + control["precision"]
                if not proves_improvement(
                    measured["value"],
                    control["value"],
                    claim["direction"],
                    claim["minimum_delta"],
                    precision,
                ):
                    # 哥哥，低精度或未检出无法成为反证，须明确越过失败侧误差范围。
                    signed = (
                        measured["value"] - control["value"]
                        if claim["direction"] == "increase"
                        else control["value"] - measured["value"]
                    )
                    found |= signed + precision < claim["minimum_delta"]
    if not found:
        raise ValueError("反证没有同方法实际失败且可分辨的测量")
    with material_transaction(c):
        from uuid import uuid4

        c.execute(
            "INSERT INTO science_invalidations VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                actor["world_id"],
                technology_id,
                version,
                actor_id,
                encoded(list(evidence_ids)),
                reason,
                to_iso(at),
            ),
        )
        c.execute(
            "UPDATE technologies SET status='invalidated',invalidated_at=? WHERE id=?",
            (to_iso(at), technology_id),
        )
        c.execute(
            (
                "INSERT INTO science_version_states VALUES (?,?,?,'invalidated',?,?) ON "
                "CONFLICT(technology_id,version) DO UPDATE SET "
                "status=excluded.status,reason=excluded.reason,updated_at=excluded.update"
                "d_at"
            ),
            (technology_id, actor["world_id"], version, reason, to_iso(at)),
        )
        frontier, seen = [technology_id], {technology_id}
        while frontier:
            parent = frontier.pop()
            for child in c.execute(
                (
                    "SELECT child_id,child_version FROM technology_dependencies WHERE "
                    "world_id=? AND parent_id=?"
                ),
                (actor["world_id"], parent),
            ).fetchall():
                c.execute(
                    (
                        "INSERT INTO science_version_states VALUES (?,?,?,'needs_review',?,?) ON "
                        "CONFLICT(technology_id,version) DO UPDATE SET "
                        "status=excluded.status,reason=excluded.reason,updated_at=excluded.update"
                        "d_at"
                    ),
                    (
                        child[0],
                        actor["world_id"],
                        child[1],
                        "父方法实际被证伪，需要复核",
                        to_iso(at),
                    ),
                )
                if child[0] not in seen:
                    seen.add(child[0])
                    frontier.append(child[0])
        from world_engine.science_chronicle import record_technology_revision

        record_technology_revision(c, technology_id, "invalidated", at)
