"""哥哥，编年从真实来源整理研究过程，不调用模型补写突破故事。"""

from __future__ import annotations

import json

from world_engine.event_history import EventHistoryService
from world_engine.event_log import record_event
from world_engine.materials import encoded
from world_engine.repository import from_iso, to_iso
from world_engine.technology_evidence import validation_evidence


def technology_process(c, world_id, technology_id):
    technology = c.execute(
        "SELECT * FROM technologies WHERE id=? AND world_id=?", (technology_id, world_id)
    ).fetchone()
    if technology is None:
        raise ValueError("科技不属于这个世界或不存在")
    candidate = c.execute(
        "SELECT * FROM technology_candidates WHERE id=?", (technology["candidate_id"],)
    ).fetchone()
    versions = [
        dict(r)
        for r in c.execute(
            "SELECT * FROM technology_versions WHERE technology_id=? ORDER BY version",
            (technology_id,),
        )
    ]
    revisions = [
        dict(r)
        for r in c.execute(
            "SELECT * FROM science_candidate_revisions WHERE candidate_id=? ORDER BY revision",
            (candidate["id"],),
        )
    ]
    trials, failures = [], []
    at = from_iso(
        c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (world_id,)).fetchone()[0]
    )
    for revision in revisions:
        frame = dict(candidate) | {
            "revision": revision["revision"],
            "spec_json": revision["spec_json"],
        }
        supports, _ = validation_evidence(c, frame, at)
        proven = {s["trial_id"] for s in supports}
        for row in c.execute(
            "SELECT * FROM science_claim_trials WHERE candidate_id=? AND revision=? ORDER BY rowid",
            (candidate["id"], revision["revision"]),
        ):
            trial = dict(row)
            for role, key in (("execution", "execution_id"), ("control", "control_execution_id")):
                execution = c.execute(
                    "SELECT * FROM technology_executions WHERE id=?", (trial[key],)
                ).fetchone()
                trial[role] = dict(execution)
                trial[role]["work"] = [
                    dict(w)
                    for w in c.execute(
                        (
                            "SELECT o.* FROM science_execution_work s "
                            "JOIN collective_work_orders o "
                            "ON o.id=s.order_id WHERE s.execution_id=? ORDER BY s.step_index"
                        ),
                        (execution["id"],),
                    )
                ]
                trial[role]["observations"] = [
                    dict(
                        c.execute(
                            "SELECT * FROM collective_observations WHERE id=?", (oid,)
                        ).fetchone()
                    )
                    for oid in json.loads(execution["observation_ids_json"])
                ]
                if execution["status"] in {"rejected", "unsupported", "interrupted"}:
                    failures.append(
                        {
                            "id": execution["id"],
                            "world_time": execution["completed_at"],
                            "reason": execution["error"],
                        }
                    )
            trial["validation_status"] = "proven" if trial["id"] in proven else "not_proven"
            if (
                trial["id"] not in proven
                and trial["execution"]["status"] == "completed"
                and trial["control"]["status"] == "completed"
            ):
                failures.append(
                    {
                        "id": trial["id"],
                        "world_time": trial["execution"]["completed_at"],
                        "reason": "这次实测未证明其提前登记的能力主张",
                        "claim": json.loads(trial["claim_json"]),
                    }
                )
            trials.append(trial)
    programs, calls, hypotheses = [], [], []
    for pid in {r["source_program_id"] for r in revisions if r["source_program_id"]}:
        program = c.execute(
            "SELECT * FROM science_programs WHERE id=? AND world_id=?", (pid, world_id)
        ).fetchone()
        if program is None:
            continue
        programs.append(dict(program))
        hypotheses.extend(
            {"program_id": pid, "world_time": program["created_at"], **h}
            for h in json.loads(program["spec_json"])["hypotheses"]
        )
        if program["call_id"]:
            call = c.execute(
                "SELECT * FROM science_model_calls WHERE id=? AND world_id=?",
                (program["call_id"], world_id),
            ).fetchone()
            if call:
                calls.append(dict(call))
    known_ids = {
        oid
        for t in trials
        for role in ("execution", "control")
        for oid in json.loads(t[role]["observation_ids_json"])
    }
    transfers = []
    for row in c.execute(
        "SELECT * FROM collective_messages WHERE world_id=? ORDER BY sent_at,rowid", (world_id,)
    ):
        data = json.loads(row["content_json"])
        if data.get("method_packet", {}).get("technology_id") == technology_id or known_ids & {
            e["id"] for e in data.get("evidence", [])
        }:
            transfers.append(dict(row))
    prior = [
        dict(r)
        for r in c.execute(
            (
                "SELECT * FROM technology_knowledge WHERE actor_id=? AND learned_at<=? "
                "ORDER BY learned_at,rowid"
            ),
            (candidate["actor_id"], candidate["created_at"]),
        )
    ]
    return {
        "technology": dict(technology),
        "problem": json.loads(revisions[0]["spec_json"])["problem"]
        if revisions
        else candidate["problem"],
        "current_problem": candidate["problem"],
        "prior_knowledge": prior,
        "prior_context": [json.loads(call["context_json"]) for call in calls],
        "hypotheses": hypotheses,
        "trials": trials,
        "failures": failures,
        "revisions": revisions,
        "programs": programs,
        "source_calls": calls,
        "transfers": transfers,
        "versions": versions,
        "validation": [
            dict(r)
            for r in c.execute(
                "SELECT * FROM technology_evidence WHERE technology_id=? ORDER BY created_at,rowid",
                (technology_id,),
            )
        ],
        "conditions": [json.loads(v["conditions_json"]) for v in versions],
        "dependencies": [
            dict(r)
            for r in c.execute(
                "SELECT * FROM technology_dependencies WHERE child_id=? ORDER BY created_at,rowid",
                (technology_id,),
            )
        ],
        "invalidations": [
            dict(r)
            for r in c.execute(
                (
                    "SELECT * FROM science_invalidations WHERE technology_id=? ORDER BY "
                    "created_at,rowid"
                ),
                (technology_id,),
            )
        ],
        "missing_facts": ["未保存模型调用的起始知识快照"] if not calls else [],
    }


def _record(c, technology_id, candidate_id, kind, at):
    technology = c.execute(
        "SELECT * FROM technologies WHERE id=? AND candidate_id=?", (technology_id, candidate_id)
    ).fetchone()
    if technology is None:
        raise ValueError("编年需要真实已登记科技及对应候选")
    old = c.execute(
        (
            "SELECT event_id FROM science_chronicle_records WHERE technology_id=? AND "
            "version=? AND kind=?"
        ),
        (technology_id, technology["current_version"], kind),
    ).fetchone()
    if old:
        return old[0]
    process = technology_process(c, technology["world_id"], technology_id)
    actor = c.execute(
        "SELECT * FROM characters WHERE id=?", (technology["inventor_id"],)
    ).fetchone()
    event_type = {
        "breakthrough": "world.technology_breakthrough",
        "revised": "world.technology_revised",
        "invalidated": "world.technology_invalidated",
    }[kind]
    action = {
        "breakthrough": "通过实际复验取得",
        "revised": "通过复验修订",
        "invalidated": "经真实反证撤销可靠性",
    }[kind]
    event_id = record_event(
        c,
        world_id=technology["world_id"],
        tick_id="technology:"
        + technology_id
        + ":"
        + str(technology["current_version"])
        + ":"
        + kind,
        occurred_at=at,
        event_type=event_type,
        actor_id=actor["id"],
        location_id=actor["current_location_id"] or actor["location_id"],
        summary=f"{actor['name']}{action}“{technology['name']}”的方法版本{technology['current_version']}。",
        payload={
            "technology_id": technology_id,
            "candidate_id": candidate_id,
            "version": technology["current_version"],
            "kind": kind,
            "conditions": process["conditions"][-1],
            "validation_ids": [e["observation_id"] for e in process["validation"]],
        },
    )
    causes = {
        r[0]
        for r in c.execute(
            (
                "SELECT o.source_event_id FROM technology_evidence e JOIN "
                "collective_observations o ON o.id=e.observation_id WHERE "
                "e.technology_id=? AND e.version=?"
            ),
            (technology_id, technology["current_version"]),
        )
        if r[0]
    }
    for invalidation in process["invalidations"]:
        for oid in json.loads(invalidation["evidence_ids_json"]):
            original = c.execute(
                "SELECT source_event_id FROM collective_observations WHERE id=?", (oid,)
            ).fetchone()
            if original and original[0]:
                causes.add(original[0])
    for row in c.execute(
        "SELECT id FROM world_events WHERE world_id=? AND occurred_at<=? "
        "AND event_type IN ('research.candidate_declared','research.candidate_revised') "
        "AND json_extract(payload_json,'$.candidate_id')=?",
        (technology["world_id"], to_iso(at), candidate_id),
    ):
        causes.add(row[0])
    for transfer in process["transfers"]:
        for row in c.execute(
            "SELECT source_event_id FROM collective_observations WHERE world_id=? "
            "AND world_time<=? AND json_extract(content_json,'$.message_id')=?",
            (technology["world_id"], to_iso(at), transfer["id"]),
        ):
            if row[0]:
                causes.add(row[0])
    for dependency in process["dependencies"]:
        parent = c.execute(
            (
                "SELECT event_id FROM science_chronicle_records WHERE technology_id=? AND "
                "version=? AND kind IN ('breakthrough','revised')"
            ),
            (dependency["parent_id"], dependency["parent_version"]),
        ).fetchone()
        if parent:
            causes.add(parent[0])
    for cause in causes:
        EventHistoryService.link(c, event_id, cause, relation="research_evidence")
    c.execute(
        "INSERT INTO science_chronicle_records VALUES (?,?,?,?,?,?,?)",
        (
            technology_id,
            technology["world_id"],
            technology["current_version"],
            kind,
            event_id,
            encoded(process),
            to_iso(at),
        ),
    )
    return event_id


def record_breakthrough(c, technology_id, candidate_id, at):
    return _record(c, technology_id, candidate_id, "breakthrough", at)


def record_technology_revision(c, technology_id, kind, at):
    if kind not in {"revised", "invalidated"}:
        raise ValueError("科技历史修订类型不支持")
    technology = c.execute(
        "SELECT candidate_id FROM technologies WHERE id=?", (technology_id,)
    ).fetchone()
    if technology is None:
        raise ValueError("科技不存在")
    return _record(c, technology_id, technology[0], kind, at)
