"""哥哥，预检只能留下回执，不能提前花费物资或创造实验结果。"""

import json
from copy import deepcopy

from test_science_programs import craft_program
from test_science_runner import Backend, runner

from world_engine.science_programs import ScienceService

pytest_plugins = ["science_fixtures"]


def snapshot(c):
    return list(c.iterdump())


def test_preflight_rolls_back_fuel_material_tools_and_labor(science_lab):
    db, _, sid, actors, assets, at = science_lab
    program = craft_program(assets["actors"][actors[0]])
    original = deepcopy(program)
    with db.write() as c:
        before = snapshot(c)
        result = ScienceService.preview(c, sid, actors[0], program, at)
        assert snapshot(c) == before
    assert program == original
    assert result["status"] == "ready_to_start"
    assert [r["status"] for r in result["commands"]] == ["ready_to_start", "deferred"]
    assert "output_ids" not in json.dumps(result)


def test_preflight_locates_missing_energy_without_filling_it(science_lab):
    db, _, sid, actors, assets, at = science_lab
    program = craft_program(assets["actors"][actors[0]])
    program["commands"][0]["energy"] = None
    with db.write() as c:
        before = snapshot(c)
        result = ScienceService.preview(c, sid, actors[0], program, at)
        assert snapshot(c) == before
    assert result["status"] == "rejected"
    assert result["commands"][0]["status"] == "rejected"
    assert "energy" in result["commands"][0]["reason"]
    assert program["commands"][0]["energy"] is None


def test_invalid_later_schema_never_executes_a_valid_prefix(science_lab):
    db, _, sid, actors, assets, at = science_lab
    program = craft_program(assets["actors"][actors[0]])
    program["commands"].append({"op": "unlock", "name": "文字愿望"})
    with db.write() as c:
        before = snapshot(c)
        result = ScienceService.preview(c, sid, actors[0], program, at)
        assert snapshot(c) == before
    assert result["status"] == "rejected"
    assert result["errors"][0]["location"][:2] == ["commands", 2]


def test_preflight_declaration_returns_no_temporary_candidate_ids(science_lab):
    from test_science_certification import bindings, declaration

    db, _, sid, actors, assets, at = science_lab
    a = assets["actors"][actors[0]]
    program = {
        "reason": "只检查本人登记和复验程序",
        "commands": [
            {"op": "declare", **declaration(a), "result_key": "candidate"},
            {"op": "validate", "candidate_id": "$candidate", "revision": 1,
             "bindings": bindings(a), "control_bindings": bindings(a, control=True)},
        ],
    }
    with db.write() as c:
        before = snapshot(c)
        result = ScienceService.preview(c, sid, actors[0], program, at)
        assert snapshot(c) == before
    assert [r["status"] for r in result["commands"]] == ["checked", "ready_to_start"]
    assert all(set(r) <= {"command_index", "operation", "status", "reason"}
               for r in result["commands"])


def test_model_preview_receipt_is_private_deduplicated_and_not_research_evidence(science_lab):
    from world_engine.science_knowledge import science_context
    from world_engine.science_runner import has_new_research_evidence

    db, _, sid, actors, assets, at = science_lab
    program = craft_program(assets["actors"][actors[0]]) | {"phase": "preview"}
    with db.write() as c:
        before = science_context(c, actors[0], at, max_chars=80000)
        first = ScienceService.submit(c, sid, actors[0], program, at)
        second = ScienceService.submit(c, sid, actors[0], program, at)
        assert first == second
        assert not c.execute("SELECT 1 FROM science_programs").fetchone()
        after = science_context(c, actors[0], at, max_chars=80000)
        other = science_context(c, actors[1], at, max_chars=80000)
        assert any(o["id"] == first for o in after["observations"])
        assert all(o["id"] != first for o in other["observations"])
        assert not has_new_research_evidence(after, before)


def test_preflight_does_not_bypass_current_ownership(science_lab):
    db, _, sid, actors, assets, at = science_lab
    program = craft_program(assets["actors"][actors[1]])
    with db.write() as c:
        before = snapshot(c)
        result = ScienceService.preview(c, sid, actors[0], program, at)
        assert snapshot(c) == before
    assert result["status"] == "rejected"


def test_npc_revises_its_own_program_after_preview_without_auto_repair(science_lab):
    db, wid, sid, _, assets, at = science_lab
    with db.write() as c:
        c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))
    backend = Backend()
    original_complete = backend.complete

    def decide(**kwargs):
        context = kwargs["user_payload"]
        aid = context["actor_id"]
        program = craft_program(assets["actors"][aid])
        receipts = [o for o in context["observations"]
                    if o["content"].get("kind") == "science_preflight"]
        if not receipts:
            program["phase"] = "preview"
            program["commands"][0]["energy"] = None
        backend.program = program
        return original_complete(**kwargs)

    backend.complete = decide
    r = runner(db, backend, sid)
    assert r.decide(wid, at)
    with db.read() as c:
        assert not c.execute("SELECT 1 FROM science_programs").fetchone()
    # 哥哥，先让其他尚未判断的居民取得机会，再由原作者自己修改。
    for _ in range(3):
        assert r.decide(wid, at)
        with db.read() as c:
            if c.execute("SELECT 1 FROM science_programs WHERE status='active'").fetchone():
                break
    with db.read() as c:
        assert c.execute("SELECT 1 FROM science_programs WHERE status='active'").fetchone()
        assert not c.execute("SELECT 1 FROM technologies").fetchone()
