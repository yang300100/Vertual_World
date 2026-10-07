"""哥哥，科技只由预登记的可执行方法和真实证据认证，不接受名字解锁。"""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from world_engine.collective_evidence import known_evidence
from world_engine.collective_research import CollectiveResearchService as R
from world_engine.materials import encoded, material_transaction
from world_engine.repository import to_iso
from world_engine.science_types import DeclareCommand, MethodSpec, ValidateCommand
from world_engine.technology_evidence import register_trial, validation_evidence


def candidate_event(c, actor_id, candidate_id, revision, spec, at, *, revised=False):
    from world_engine.event_history import EventHistoryService
    from world_engine.event_log import record_event

    actor = R.actor(c, actor_id)
    prior = c.execute(
        "SELECT id FROM world_events WHERE world_id=? "
        "AND event_type IN ('research.candidate_declared','research.candidate_revised') "
        "AND json_extract(payload_json,'$.candidate_id')=? ORDER BY rowid DESC LIMIT 1",
        (actor["world_id"], candidate_id),
    ).fetchone()
    event = record_event(
        c,
        world_id=actor["world_id"],
        tick_id=f"science-candidate:{candidate_id}:{revision}",
        occurred_at=at,
        event_type="research.candidate_revised" if revised else "research.candidate_declared",
        actor_id=actor_id,
        summary=(
            f"{actor['name']}在本人记录中{'修订' if revised else '登记'}"
            f"待验证研究主张“{spec.name}”。"
        ),
        payload={
            "candidate_id": candidate_id,
            "revision": revision,
            "problem": spec.problem,
            "status": "unverified",
        },
    )
    if prior:
        EventHistoryService.link(c, event, prior[0], relation="research_revision")
    for oid in spec.evidence_ids:
        observation = c.execute(
            "SELECT source_event_id FROM collective_observations WHERE id=? AND world_id=?",
            (oid, actor["world_id"]),
        ).fetchone()
        if observation and observation[0]:
            EventHistoryService.link(c, event, observation[0], relation="research_evidence")
    return event


def fingerprints(method, claim):
    """哥哥，规范化只去名字和局部引用标签，操作次序与物理参数保留。"""
    model = MethodSpec.model_validate(method).model_dump(mode="json")
    roles, results, params = {}, {}, {}

    def ref(value):
        if value in model["inputs"]:
            roles.setdefault(value, "input" + str(len(roles)))
            return roles[value]
        if value.startswith("$"):
            key, index = value[1:].rsplit(":", 1)
            return "$" + results[key] + ":" + index
        return value

    steps = []
    for index, step in enumerate(model["steps"]):
        step = dict(step)
        for key in ("input", "instrument", "apparatus", "energy"):
            if step.get(key):
                step[key] = ref(step[key])
        if "auxiliary_inputs" in step:
            step["auxiliary_inputs"] = [ref(x) for x in step["auxiliary_inputs"]]
        if "bindings" in step:
            step["bindings"] = {k: ref(v) for k, v in step["bindings"].items()}
        for field in ("settings", "parameters"):
            for key, value in step.get(field, {}).items():
                if isinstance(value, str) and value.startswith("param:"):
                    pname = value[6:]
                    params.setdefault(pname, "parameter" + str(len(params)))
                    step[field][key] = "param:" + params[pname]
        if step.get("result_key"):
            results[step["result_key"]] = "result" + str(index)
        step.pop("result_key", None)
        steps.append(step)
    method_data = {
        "inputs": {v: model["inputs"][k] for k, v in roles.items()},
        "parameters": {v: model["parameters"][k] for k, v in params.items()},
        "steps": steps,
    }
    claim_data = dict(claim)
    claim_data["comparator_role"] = ref(claim_data["comparator_role"])
    return tuple(hashlib.sha256(encoded(v).encode()).hexdigest() for v in (method_data, claim_data))


class TechnologyService:
    @staticmethod
    def declare(c, session_id, actor_id, body, at, *, source_program_id=None):
        spec = DeclareCommand.model_validate({"op": "declare", **body})
        actor = R.actor(c, actor_id)
        session = c.execute(
            "SELECT * FROM science_sessions WHERE id=? AND world_id=?",
            (session_id, actor["world_id"]),
        ).fetchone()
        if session is None or (
            session["status"] not in {"prepared", "running"}
            and not (session["status"] == "stopping" and source_program_id)
        ):
            raise ValueError("科研会话不可提交声明")
        if set(spec.evidence_ids) - known_evidence(c, actor_id, at):
            raise ValueError("候选不能引用本人未知、私有、未来或不存在的证据")
        cid = str(uuid4())
        with material_transaction(c):
            c.execute(
                (
                    "INSERT INTO technology_candidates(id,session_id,world_id,actor_id"
                    ",name,problem,spec_json,created_at,updated_at,source_program_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)"
                ),
                (
                    cid,
                    session_id,
                    actor["world_id"],
                    actor_id,
                    spec.name,
                    spec.problem,
                    spec.model_dump_json(),
                    to_iso(at),
                    to_iso(at),
                    source_program_id,
                ),
            )
            c.execute(
                "INSERT INTO science_candidate_revisions VALUES (?,?,1,?,?,?,?)",
                (
                    cid,
                    actor["world_id"],
                    actor_id,
                    spec.model_dump_json(),
                    source_program_id,
                    to_iso(at),
                ),
            )
            candidate_event(c, actor_id, cid, 1, spec, at)
        return cid

    @staticmethod
    def revise(c, candidate_id, actor_id, body, at, *, source_program_id=None):
        row = c.execute(
            "SELECT * FROM technology_candidates WHERE id=? AND actor_id=?",
            (candidate_id, actor_id),
        ).fetchone()
        if row is None:
            raise ValueError("只能修订本人提出的候选")
        clean = {k: v for k, v in body.items() if k not in {"op", "candidate_id", "revision"}}
        spec = DeclareCommand.model_validate({"op": "declare", **clean})
        if set(spec.evidence_ids) - known_evidence(c, actor_id, at):
            raise ValueError("修订不能引用未知证据")
        revision = row["revision"] + 1
        with material_transaction(c):
            c.execute(
                "INSERT INTO science_candidate_revisions VALUES (?,?,?,?,?,?,?)",
                (
                    candidate_id,
                    row["world_id"],
                    revision,
                    actor_id,
                    spec.model_dump_json(),
                    source_program_id,
                    to_iso(at),
                ),
            )
            c.execute(
                (
                    "UPDATE technology_candidates SET name=?,problem=?,spec_json=?,rev"
                    "ision=?,status='proposed',gaps_json='[]',updated_at=?,source_prog"
                    "ram_id=? WHERE id=?"
                ),
                (
                    spec.name,
                    spec.problem,
                    spec.model_dump_json(),
                    revision,
                    to_iso(at),
                    source_program_id,
                    candidate_id,
                ),
            )
            candidate_event(c, actor_id, candidate_id, revision, spec, at, revised=True)
        return revision

    @staticmethod
    def revise_claim(c, session_id, actor_id, body, at, *, source_program_id=None):
        row = c.execute(
            "SELECT * FROM technology_candidates WHERE id=? AND session_id=? AND actor_id=?",
            (body["candidate_id"], session_id, actor_id),
        ).fetchone()
        if row is None or row["revision"] != body["revision"]:
            raise ValueError("修订必须基于本人当前候选版本")
        return TechnologyService.revise(
            c, row["id"], actor_id, body, at, source_program_id=source_program_id
        )

    @staticmethod
    def validate(c, session_id, actor_id, body, at, *, source_program_id=None):
        from world_engine.science_programs import start_execution

        cmd = ValidateCommand.model_validate({"op": "validate", **body})
        row = c.execute(
            "SELECT * FROM technology_candidates WHERE id=? AND session_id=? AND actor_id=?",
            (cmd.candidate_id, session_id, actor_id),
        ).fetchone()
        if row is None or row["revision"] != cmd.revision or row["updated_at"] > to_iso(at):
            raise ValueError("验证必须使用本人当前已提出的候选版本")
        spec = json.loads(row["spec_json"])
        method = MethodSpec.model_validate(spec["method"])
        metric = spec["claim"]["metric"]
        assays = [step for step in method.steps if step.op == "assay" and step.metric == metric]
        if not assays:
            raise ValueError("方法没有可执行的对应指标检验，不能安排验证")
        role = spec["claim"]["comparator_role"]
        gauge = assays[-1].instrument
        if (
            role not in method.inputs
            or method.inputs[role] != "material"
            or gauge not in method.inputs
        ):
            raise ValueError("验证须明确同类对照材料和量器输入角色")
        control = {
            "inputs": method.inputs,
            "steps": [{"op": "assay", "input": role, "metric": metric, "instrument": gauge}],
        }
        with material_transaction(c):
            eid = start_execution(
                c,
                session_id,
                actor_id,
                method,
                cmd.bindings,
                cmd.parameters,
                at,
                source_program_id=source_program_id,
                candidate_id=row["id"],
                candidate_revision=row["revision"],
            )
            control_id = start_execution(
                c,
                session_id,
                actor_id,
                control,
                cmd.control_bindings,
                {},
                at,
                source_program_id=source_program_id,
                candidate_id=row["id"],
                candidate_revision=row["revision"],
            )
            register_trial(c, row["id"], row["revision"], eid, control_id, at)
        return eid

    @staticmethod
    def certify(c, candidate_id, actor_id, at):
        row = c.execute(
            "SELECT * FROM technology_candidates WHERE id=? AND actor_id=?",
            (candidate_id, actor_id),
        ).fetchone()
        if row is None:
            raise ValueError("本人不能认证未知候选或他人的私有结果")
        existing = c.execute(
            (
                "SELECT technology_id FROM technology_versions WHERE source_candid"
                "ate_id=? AND source_revision=?"
            ),
            (candidate_id, row["revision"]),
        ).fetchone()
        if existing:
            return {"status": "verified", "technology_id": existing[0], "gaps": []}
        spec = json.loads(row["spec_json"])
        method_fp, claim_fp = fingerprints(spec["method"], spec["claim"])
        duplicate = c.execute(
            (
                "SELECT technology_id FROM technology_versions WHERE world_id=? AN"
                "D method_fingerprint=? AND claim_fingerprint=?"
            ),
            (row["world_id"], method_fp, claim_fp),
        ).fetchone()
        if duplicate:
            c.execute(
                "UPDATE technology_candidates SET status='duplicate',gaps_json='[]' WHERE id=?",
                (candidate_id,),
            )
            return {"status": "duplicate", "technology_id": duplicate[0], "gaps": []}
        supports, gaps = validation_evidence(c, row, at)
        if len(supports) < 2:
            c.execute(
                "UPDATE technology_candidates SET status='needs_evidence',gaps_json=? WHERE id=?",
                (encoded(gaps), candidate_id),
            )
            return {"status": "needs_evidence", "technology_id": None, "gaps": gaps}
        prior = c.execute(
            "SELECT * FROM technologies WHERE candidate_id=? AND world_id=?",
            (candidate_id, row["world_id"]),
        ).fetchone()
        tid = prior["id"] if prior else str(uuid4())
        version = prior["current_version"] + 1 if prior else 1
        conditions = {
            "declared": spec["claim"].get("conditions", {}),
            "validated_parameters": supports[0]["parameters"],
            "validated_carrier": supports[0]["carrier"],
            "validated_mass": supports[0]["mass"],
            "validated_input_mass": supports[0]["input_mass"],
        }
        with material_transaction(c):
            if prior:
                c.execute(
                    (
                        "UPDATE technologies SET current_version=?,name=?,status='verified"
                        "',invalidated_at=NULL WHERE id=?"
                    ),
                    (version, row["name"], tid),
                )
            else:
                c.execute(
                    (
                        "INSERT INTO technologies(id,session_id,world_id,candidate_id,inve"
                        "ntor_id,name,created_at) VALUES (?,?,?,?,?,?,?)"
                    ),
                    (
                        tid,
                        row["session_id"],
                        row["world_id"],
                        candidate_id,
                        actor_id,
                        row["name"],
                        to_iso(at),
                    ),
                )
            c.execute(
                "INSERT INTO technology_versions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tid,
                    row["world_id"],
                    version,
                    encoded(spec["method"]),
                    encoded(spec["claim"]),
                    method_fp,
                    claim_fp,
                    encoded(conditions),
                    candidate_id,
                    row["revision"],
                    to_iso(at),
                ),
            )
            for support in supports:
                for index, oid in enumerate(support["observation_ids"]):
                    eid = support["execution_id"] if index == 0 else support["control_execution_id"]
                    c.execute(
                        "INSERT INTO technology_evidence VALUES (?,?,?,?,?,?,?)",
                        (
                            row["world_id"],
                            tid,
                            version,
                            oid,
                            eid,
                            "validation" if index == 0 else "control",
                            to_iso(at),
                        ),
                    )
            c.execute(
                (
                    "UPDATE technology_candidates SET status='verified',gaps_json='[]'"
                    ",updated_at=? WHERE id=?"
                ),
                (to_iso(at), candidate_id),
            )
            from world_engine.technology_methods import link_dependencies

            link_dependencies(c, tid, version, supports, at)
            from world_engine.science_chronicle import (
                record_breakthrough,
                record_technology_revision,
            )

            event_id = (
                record_technology_revision(c, tid, "revised", at)
                if prior
                else record_breakthrough(c, tid, candidate_id, at)
            )
            c.execute(
                (
                    "INSERT INTO technology_knowledge(world_id,actor_id,technology_id,"
                    "version,evidence_ids_json,learned_at,source_event_id) VALUES (?,?,?,?,?,?,?)"
                ),
                (
                    row["world_id"],
                    actor_id,
                    tid,
                    version,
                    encoded([oid for s in supports for oid in s["observation_ids"]]),
                    to_iso(at),
                    event_id,
                ),
            )
        return {"status": "verified", "technology_id": tid, "gaps": []}

    @staticmethod
    def list(c, world_id):
        return [
            dict(row)
            for row in c.execute(
                "SELECT * FROM technologies WHERE world_id=? ORDER BY created_at,rowid", (world_id,)
            )
        ]

    @staticmethod
    def graph(c, world_id):
        nodes = TechnologyService.list(c, world_id)
        for node in nodes:
            state = c.execute(
                "SELECT status FROM science_version_states WHERE technology_id=? AND version=?",
                (node["id"], node["current_version"]),
            ).fetchone()
            node["version_status"] = state[0] if state else node["status"]
        return {
            "nodes": nodes,
            "edges": [
                dict(r)
                for r in c.execute(
                    (
                        "SELECT * FROM technology_dependencies WHERE world_id=? ORDER BY "
                        "created_at,rowid"
                    ),
                    (world_id,),
                )
            ],
        }

    @staticmethod
    def invalidate(c, technology_id, actor_id, evidence_ids, reason, at):
        from world_engine.technology_methods import invalidate_from_evidence

        return invalidate_from_evidence(c, technology_id, actor_id, evidence_ids, reason, at)
