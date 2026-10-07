"""哥哥，整体审查的七类边界都用离线真实工单约束。"""

import json
from dataclasses import replace
from datetime import timedelta

import pytest
from test_science_certification import bindings, declaration, settle, technologies
from test_science_methods import certified  # noqa: F401

pytest_plugins = ["science_fixtures"]


def drive(c, wid, at, hours=3):
    from world_engine.science_programs import ScienceService

    ScienceService.tick(c, wid, at)
    for i in range(1, int(hours * 4) + 1):
        settle(c, wid, at + timedelta(minutes=15 * i))


def available_outputs(c, execution):
    return [
        iid
        for iid in json.loads(execution["output_ids_json"])
        if c.execute(
            "SELECT quantity FROM item_instances WHERE id=? AND quantity>0", (iid,)
        ).fetchone()
    ]


def test_unrelated_processing_cannot_certify_old_improved_material(certified):  # noqa: F811
    db, wid, sid, actors, assets, at, _ = certified
    a = assets["actors"][actors[0]]
    with db.write() as c:
        old = [
            iid
            for e in c.execute("SELECT * FROM technology_executions WHERE candidate_id IS NOT NULL")
            for iid in available_outputs(c, e)
        ]
        assert len(old) >= 2
        spec = declaration(a)
        spec["method"]["inputs"]["improved"] = "material"
        spec["method"]["steps"] = [
            {"op": "store", "input": "sample", "result_key": "unrelated"},
            {"op": "assay", "input": "improved", "metric": "strength", "instrument": "gauge"},
        ]
        cid = technologies().declare(c, sid, actors[0], spec, at)
        for i, iid in enumerate(old[:2]):
            start = at + timedelta(hours=3 * i)
            technologies().validate(
                c,
                sid,
                actors[0],
                {
                    "candidate_id": cid,
                    "revision": 1,
                    "bindings": bindings(a) | {"improved": iid},
                    "control_bindings": bindings(a, control=True) | {"improved": iid},
                },
                start,
            )
            drive(c, wid, start)
        result = technologies().certify(c, cid, actors[0], at + timedelta(hours=6))
        assert result["status"] == "needs_evidence", result


def test_transformed_traditional_reference_is_not_an_original_baseline(science_lab):
    from world_engine.science_programs import start_execution
    from world_engine.technology_evidence import traditional_baseline

    db, wid, sid, actors, assets, at = science_lab
    a = assets["actors"][actors[0]]
    with db.write() as c:
        method = {
            "inputs": {"sample": "material", "kiln": "apparatus", "fuel": "energy"},
            "steps": [
                {
                    "op": "compact",
                    "input": "sample",
                    "settings": {"pressure": 1000},
                    "apparatus": "kiln",
                    "energy": "fuel",
                }
            ],
        }
        eid = start_execution(
            c,
            sid,
            actors[0],
            method,
            {"sample": a["references"]["clay"], "kiln": a["apparatus"], "fuel": a["energy"]},
            {},
            at,
        )
        drive(c, wid, at)
        row = c.execute("SELECT * FROM technology_executions WHERE id=?", (eid,)).fetchone()
        outputs = available_outputs(c, row)
        assert outputs
        assert not traditional_baseline(c, wid, outputs[0], "clay", at + timedelta(hours=3))
        assert traditional_baseline(
            c, wid, a["references"]["clay"], "clay", at + timedelta(hours=3)
        )


@pytest.mark.parametrize("case", ["invalid", "unknown"])
def test_dependency_requires_current_valid_known_parent(certified, case):  # noqa: F811
    from world_engine.technology_methods import link_dependencies

    db, wid, sid, actors, assets, at, parent = certified
    with db.write() as c:
        actor = actors[0] if case == "invalid" else actors[1]
        cid = technologies().declare(c, sid, actor, declaration(assets["actors"][actor]), at)
        # 离线边界夹具复用真实已认证记录，仅改变当前权限/效力状态来触发入口检查。
        child = "review-child"
        c.execute(
            "INSERT INTO technologies(id,session_id,world_id,candidate_id,inventor_id,"
            "name,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (child, sid, wid, cid, actor, "待核对依赖", at.isoformat()),
        )
        c.execute(
            "INSERT INTO technology_versions SELECT ?,world_id,1,method_json,claim_json,"
            "?,?,conditions_json,?,1,? FROM technology_versions "
            "WHERE technology_id=? AND version=1",
            (child, child, child, cid, at.isoformat(), parent),
        )
        e = c.execute(
            "SELECT * FROM technology_executions WHERE candidate_id IS NOT NULL LIMIT 1"
        ).fetchone()
        c.execute(
            "INSERT INTO science_method_spans VALUES (?,?,?,?,?,?,?)",
            (e["id"], wid, parent, 1, 0, 1, at.isoformat()),
        )
        if case == "invalid":
            c.execute("UPDATE technologies SET status='invalidated' WHERE id=?", (parent,))
        with pytest.raises(ValueError, match="失效|未知|复核"):
            link_dependencies(c, child, 1, [{"execution_id": e["id"]}], at)


def test_old_packet_survives_true_revision_rename(certified):  # noqa: F811
    from world_engine.collective_messages import MessageService
    from world_engine.science_knowledge import known_methods, share_method

    db, wid, sid, actors, assets, at, tid = certified
    a = assets["actors"][actors[0]]
    with db.write() as c:
        c.execute("INSERT INTO collective_contacts VALUES (?,?,12)", tuple(actors))
        mid = share_method(c, actors[0], actors[1], tid, 1, at)
        cid = c.execute("SELECT candidate_id FROM technologies WHERE id=?", (tid,)).fetchone()[0]
        spec = declaration(a, name="真正改名后的修订方法", temperature=1400)
        spec["method"]["steps"][0]["quantity"] = 2
        technologies().revise(c, cid, actors[0], spec, at)
        for i in range(2):
            start = at + timedelta(hours=2 * i)
            technologies().validate(
                c,
                sid,
                actors[0],
                {
                    "candidate_id": cid,
                    "revision": 2,
                    "bindings": bindings(a),
                    "control_bindings": bindings(a, control=True),
                },
                start,
            )
            drive(c, wid, start, hours=2)
        assert (
            technologies().certify(c, cid, actors[0], at + timedelta(hours=4))["status"]
            == "verified"
        )
        MessageService.deliver(c, wid, at + timedelta(hours=12))
        old = next(
            m for m in known_methods(c, actors[1], at + timedelta(hours=12)) if m["version"] == 1
        )
        assert old["name"] == "测试内能力"
        assert old["source_message_id"] == mid


def test_multistep_program_returns_final_live_output(science_lab):
    from world_engine.science_programs import ScienceService

    db, wid, sid, actors, assets, at = science_lab
    a = assets["actors"][actors[0]]
    with db.write() as c:
        spec = declaration(a)
        spec["method"]["steps"].insert(
            1,
            {
                "op": "compact",
                "input": "$heated:0",
                "quantity": 2,
                "settings": {"pressure": 100},
                "apparatus": "kiln",
                "energy": "fuel",
                "result_key": "dense",
            },
        )
        spec["method"]["steps"][0]["quantity"] = 2
        spec["method"]["steps"][-1]["input"] = "$dense:0"
        pid = ScienceService.submit(
            c,
            sid,
            actors[0],
            {
                "reason": "真实多步结果",
                "commands": [
                    {"op": "declare", **spec, "result_key": "candidate"},
                    {
                        "op": "validate",
                        "candidate_id": "$candidate",
                        "revision": 1,
                        "bindings": bindings(a),
                        "control_bindings": bindings(a, control=True),
                        "result_key": "final",
                    },
                    {"op": "craft", "operation": "store", "input": "$final:0"},
                ],
            },
            at,
        )
        drive(c, wid, at, hours=6)
        row = c.execute("SELECT * FROM science_programs WHERE id=?", (pid,)).fetchone()
        assert row["status"] == "completed", row["error"]


def test_runtime_config_fingerprint_includes_life_and_actual_knowledge(settings, tmp_path):
    from scripts.demo_science import runtime_config

    knowledge = tmp_path / "common.md"
    knowledge.write_text("本人可知的传统经验", encoding="utf-8")
    settings = replace(settings, knowledge_paths=(knowledge,))
    baseline = runtime_config(settings)
    for key, value in [
        ("satiety_loss_per_world_hour", 9),
        ("energy_recovery_per_world_hour", 9),
        ("knowledge_enabled", False),
    ]:
        assert runtime_config(replace(settings, **{key: value})) != baseline
    knowledge.write_text("变更后的经验", encoding="utf-8")
    assert runtime_config(settings) != baseline


def test_resume_rejects_changed_life_config_before_creating_engine(
    lab, settings, tmp_path, monkeypatch
):
    from scripts.demo_science import prepare, run
    from world_engine.database import Database

    db, wid, _, _, _, _ = lab
    output = tmp_path / "runtime-prepared"
    config = replace(settings, deepseek_api_key="offline", science_api_enabled=True)
    prepare(
        db.path, wid, output, config, {"offline_passed": True, "independent_review_passed": True}
    )

    def forbidden(*args, **kwargs):
        pytest.fail("冻结配置拒绝应发生在任何模型客户端构造之前")

    monkeypatch.setattr("scripts.demo_science.WorldEngine", forbidden)
    with pytest.raises(ValueError, match="运行配置"):
        run(output, replace(config, satiety_loss_per_world_hour=9), resume=True)
    with Database(output / "world.db", legacy_world_defaults=False).read() as c:
        assert not c.execute("SELECT 1 FROM science_model_calls").fetchone()


def test_legacy_processing_entry_rejects_unfunded_heat(science_lab):
    from world_engine.science_programs import ScienceService

    db, _, sid, actors, assets, at = science_lab
    a = assets["actors"][actors[0]]
    with db.write() as c:
        c.execute(
            "UPDATE collective_instruments SET method='moisture' WHERE item_id=?",
            (a["instruments"]["strength"],),
        )
        pid = ScienceService.submit(
            c,
            sid,
            actors[0],
            {
                "reason": "旧入口不能绕过供能",
                "commands": [
                    {
                        "op": "observe",
                        "batch_id": a["materials"]["clay"],
                        "method": "moisture",
                        "instrument_id": a["instruments"]["strength"],
                        "operation": "heat",
                        "settings": {"heat": 100},
                    }
                ],
            },
            at,
        )
        row = c.execute("SELECT * FROM science_programs WHERE id=?", (pid,)).fetchone()
        assert row["status"] == "rejected" and "craft" in row["error"]
        assert not c.execute("SELECT 1 FROM collective_programs").fetchone()
