"""哥哥，固定离线响应只验证引擎链条，不能算真实NPC自主成功。"""

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import httpx

pytest_plugins = ["science_fixtures"]


def test_no_technology_without_model_actions(science_lab):
    from world_engine.science_programs import ScienceService

    db, wid, _, _, _, at = science_lab
    with db.write() as c:
        ScienceService.tick(c, wid, at + timedelta(hours=24))
        assert not c.execute("SELECT 1 FROM technologies WHERE world_id=?", (wid,)).fetchone()


def test_frozen_normal_heartbeats_http_trials_transfer_dependency_reuse(
    science_lab, settings, monkeypatch, tmp_path
):
    from test_science_certification import bindings, declaration

    from world_engine.agent_llm import AgentModelBackend
    from world_engine.engine import WorldEngine
    from world_engine.repository import to_iso
    from world_engine.science_exports import export_science
    from world_engine.science_freeze import FrozenScienceDatabase, freeze_session, verify_freeze
    from world_engine.technologies import TechnologyService

    db, wid, sid, actors, assets, at = science_lab
    inventor, colleague = sorted(actors)
    calls, stages = [], {}
    with db.write() as c:
        c.execute(
            "UPDATE material_modes SET policy_json='{" + '"mode":"science"' + "}' WHERE world_id=?",
            (wid,),
        )
        c.execute("INSERT INTO collective_contacts VALUES (?,?,0.25)", (inventor, colleague))
        c.execute("INSERT INTO collective_contacts VALUES (?,?,0.25)", (colleague, inventor))
        c.execute(
            "UPDATE world_clock SET time_scale=1,next_adjudication_world_time=? WHERE world_id=?",
            (to_iso(at + timedelta(hours=12)), wid),
        )
        freeze_session(
            c,
            sid,
            source_paths=(Path("world_engine/science_types.py"),),
            world_paths=(Path("docs/knowledge/common-character-knowledge.md"),),
        )
    guarded = FrozenScienceDatabase(db.path, session_id=sid)
    with guarded.write() as c:
        c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))

    def validation_program(a, spec, extra=()):
        return {
            "reason": "离线固定程序验证实际成本与复验",
            "commands": [
                *extra,
                {"op": "declare", **spec, "result_key": "candidate"},
                *[
                    {
                        "op": "validate",
                        "candidate_id": "$candidate",
                        "revision": 1,
                        "bindings": bindings(a),
                        "control_bindings": bindings(a, control=True),
                    }
                    for _ in range(2)
                ],
            ],
        }

    def reply(request):
        payload = json.loads(request.content)
        context = json.loads(payload["messages"][1]["content"])
        actor = context["actor_id"]
        a = assets["actors"][actor]
        methods = context["known_methods"]
        program = {"reason": "等候本人新的实测", "commands": []}
        stage = stages.get(actor, 0)
        if actor == inventor and stage == 0:
            spec = declaration(a)
            spec["method"]["steps"][0]["quantity"] = 2
            program = validation_program(a, spec)
            stages[actor] = 1
        elif actor == inventor and stage == 1 and methods:
            parent = methods[0]["technology_id"]
            spec = declaration(a, name="离线嵌套工艺")
            spec["method"]["steps"] = [
                {
                    "op": "use_method",
                    "technology_id": parent,
                    "version": 1,
                    "bindings": {k: k for k in spec["method"]["inputs"]},
                    "result_key": "parent",
                },
                {
                    "op": "compact",
                    "input": "$parent:0",
                    "hours": 1,
                    "settings": {"pressure": 100},
                    "apparatus": "kiln",
                    "energy": "fuel",
                    "result_key": "dense",
                },
                {"op": "assay", "input": "$dense:0", "metric": "strength", "instrument": "gauge"},
            ]
            program = validation_program(
                a,
                spec,
                extra=[
                    {
                        "op": "share_method",
                        "technology_id": parent,
                        "version": 1,
                        "recipients": [colleague],
                        "text": "分享本人通过复验的方法",
                    }
                ],
            )
            stages[actor] = 2
        elif actor == inventor and stage == 2 and len(methods) >= 2:
            child = next(m for m in methods if m["name"] == "离线嵌套工艺")
            program = {
                "reason": "实际使用新方法",
                "commands": [
                    {
                        "op": "use_method",
                        "technology_id": child["technology_id"],
                        "version": 1,
                        "bindings": bindings(a),
                    }
                ],
            }
            stages[actor] = 3
        elif actor == colleague and methods and stage == 0:
            program = {
                "reason": "同行方法送达后实际复用",
                "commands": [
                    {
                        "op": "use_method",
                        "technology_id": methods[0]["technology_id"],
                        "version": 1,
                        "bindings": bindings(a),
                    }
                ],
            }
            stages[actor] = 1
        calls.append({"actor": actor, "payload": payload, "program": program})
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(program, ensure_ascii=False)},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 100},
            },
        )

    backend = AgentModelBackend(
        replace(settings, deepseek_api_key="offline", deepseek_max_retries=0),
        client=httpx.Client(
            base_url="https://science-test.invalid/", transport=httpx.MockTransport(reply)
        ),
    )
    monkeypatch.setattr(
        "world_engine.engine.build_agent_model_backend",
        lambda config: backend if config.deepseek_api_key else None,
    )
    engine = WorldEngine(
        guarded, replace(settings, science_api_enabled=True, deepseek_api_key="offline")
    )
    try:
        engine.heartbeat(wid, elapsed_seconds=0)
        from world_engine.collective_clock import next_boundary
        from world_engine.repository import from_iso

        for _ in range(40):
            with guarded.read() as c:
                current = from_iso(
                    c.execute(
                        "SELECT w.current_time FROM worlds w WHERE w.id=?", (wid,)
                    ).fetchone()[0]
                )
                following = next_boundary(c, wid, current, limit_hours=0.5)
                default_boundary = next_boundary(c, wid, current)
                assert following <= default_boundary, (current, following, default_boundary)
            try:
                engine.heartbeat(wid, elapsed_seconds=(following - current).total_seconds())
            except ValueError as error:
                raise AssertionError(
                    {
                        "read_current": current,
                        "chosen_boundary": following,
                        "default_boundary": default_boundary,
                        "error": str(error),
                    }
                ) from error
        with guarded.read() as c:
            graph = TechnologyService.graph(c, wid)
            assert len(graph["nodes"]) == 2, {
                "candidates": [dict(r) for r in c.execute("SELECT * FROM technology_candidates")],
                "calls": [
                    (r["status"], r["error"])
                    for r in c.execute("SELECT * FROM science_model_calls")
                ],
                "programs": [
                    (r["status"], r["error"]) for r in c.execute("SELECT * FROM science_programs")
                ],
            }
            assert len(graph["edges"]) == 1
            assert c.execute("SELECT COUNT(*) FROM science_claim_trials").fetchone()[0] == 4
            assert (
                c.execute(
                    "SELECT COUNT(*) FROM science_chronicle_records WHERE kind='breakthrough'"
                ).fetchone()[0]
                == 2
            )
            assert (
                c.execute(
                    "SELECT COUNT(*) FROM technology_knowledge WHERE actor_id=?", (colleague,)
                ).fetchone()[0]
                == 1
            )
            assert (
                c.execute(
                    "SELECT COUNT(*) FROM technology_executions "
                    "WHERE technology_id IS NOT NULL AND status='completed'"
                ).fetchone()[0]
                >= 2
            )
            assert verify_freeze(c, sid)
        assert engine.science_runners[sid].usage()["known_tokens"] == len(calls) * 200
        assert len(calls) <= 12
        report = export_science(db, sid, tmp_path / "report")
        assert report["api_traceable"] and report["actual_dependencies"] == 1
        assert report["completed_reuses"] >= 2 and report["freeze_intact"]
        assert report["goal_achieved"] is False
    finally:
        engine.close()
