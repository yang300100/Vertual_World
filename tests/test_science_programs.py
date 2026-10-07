"""哥哥，程序必须取得实物结果才继续，文字和未来编号不能变成成果。"""

import importlib
import importlib.util
import json
from datetime import timedelta

import pytest

pytest_plugins = ["science_fixtures"]


def advance(c, wid, at):
    from science_fixtures import advance as actual_advance

    actual_advance(c, wid, at)


def science():
    assert importlib.util.find_spec("world_engine.science_programs"), "尚无科研程序执行器"
    return importlib.import_module("world_engine.science_programs").ScienceService


def craft_program(a):
    return {
        "reason": "测试本人两阶段操作",
        "commands": [
            {
                "op": "craft",
                "operation": "heat",
                "input": a["materials"]["clay"],
                "quantity": 1,
                "hours": 1,
                "settings": {"temperature": 1000, "power": 4},
                "apparatus": a["apparatus"],
                "energy": a["energy"],
                "result_key": "heated",
            },
            {
                "op": "assay",
                "input": "$heated:0",
                "metric": "strength",
                "instrument": a["instruments"]["strength"],
                "result_key": "reading",
            },
        ],
    }


def test_text_alone_does_not_complete_experiment(science_lab):
    service = science()
    db, wid, sid, actors, _, at = science_lab
    with db.write() as c:
        pid = service.submit(c, sid, actors[0], {"reason": "只讨论", "commands": []}, at)
        assert pid
        assert (
            c.execute("SELECT COUNT(*) FROM technologies WHERE world_id=?", (wid,)).fetchone()[0]
            == 0
        )


def test_declaration_method_result_references_have_their_own_scope(science_lab):
    from test_science_certification import bindings, declaration

    db, _, sid, actors, assets, at = science_lab
    a = assets["actors"][actors[0]]
    with db.write() as c:
        pid = science().submit(
            c,
            sid,
            actors[0],
            {
                "reason": "登记带真实阶段引用的可执行方法",
                "commands": [
                    {"op": "declare", **declaration(a), "result_key": "candidate"},
                    {
                        "op": "validate",
                        "candidate_id": "$candidate",
                        "revision": 1,
                        "bindings": bindings(a),
                        "control_bindings": bindings(a, control=True),
                    },
                ],
            },
            at,
        )
        program = c.execute("SELECT * FROM science_programs WHERE id=?", (pid,)).fetchone()
        assert program["status"] == "active", program["error"]
        assert c.execute("SELECT COUNT(*) FROM technology_candidates").fetchone()[0] == 1
        spec = json.loads(c.execute("SELECT spec_json FROM technology_candidates").fetchone()[0])
        assert spec["method"]["steps"][1]["input"] == "$heated:0"


def test_program_waits_for_actual_output_and_assay(science_lab):
    service = science()
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        pid = service.submit(c, sid, actors[0], craft_program(assets["actors"][actors[0]]), at)
        assert (
            c.execute("SELECT status FROM science_programs WHERE id=?", (pid,)).fetchone()[0]
            == "active"
        )
        assert not c.execute(
            "SELECT 1 FROM technology_executions WHERE status='completed'"
        ).fetchone()
        advance(c, wid, at + timedelta(hours=1))
        assert (
            c.execute("SELECT status FROM science_programs WHERE id=?", (pid,)).fetchone()[0]
            == "active"
        )
        advance(c, wid, at + timedelta(hours=1.5))
        row = c.execute("SELECT * FROM science_programs WHERE id=?", (pid,)).fetchone()
        assert row["status"] == "completed", row["error"]
        reading = json.loads(row["bindings_json"])["reading"][0]
        content = json.loads(
            c.execute(
                "SELECT content_json FROM collective_observations WHERE id=?", (reading,)
            ).fetchone()[0]
        )
        assert content["measurement"]["value"] > 0
        assert (
            content["measurement"]["prediction_recorded_at"]
            <= content["measurement"]["measured_at"]
        )


@pytest.mark.parametrize("change", ["absent", "remote", "foreign_owner", "fuel", "future"])
def test_invalid_resources_do_not_create_results(science_lab, change):
    service = science()
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        a = assets["actors"][actors[0]]
        p = craft_program(a)
        if change == "absent":
            p["commands"][0]["input"] = "耗尽的编号"
        elif change == "remote":
            c.execute(
                "UPDATE item_instances SET container_type='message_cargo' WHERE id=?",
                (a["materials"]["clay"],),
            )
        elif change == "foreign_owner":
            p["commands"][0]["input"] = assets["actors"][actors[1]]["materials"]["clay"]
        elif change == "fuel":
            p["commands"][0]["energy"] = None
        else:
            p["commands"][0]["input"] = "$heated:0"
        pid = service.submit(c, sid, actors[0], p, at)
        row = c.execute("SELECT * FROM science_programs WHERE id=?", (pid,)).fetchone()
        assert row["status"] == "rejected"
        assert row["error"]
        assert c.execute("SELECT COUNT(*) FROM technologies").fetchone()[0] == 0


def test_no_second_program_for_same_body(science_lab):
    service = science()
    db, _, sid, actors, assets, at = science_lab
    with db.write() as c:
        p = craft_program(assets["actors"][actors[0]])
        service.submit(c, sid, actors[0], p, at)
        with pytest.raises(ValueError, match="程序|身体|工作"):
            service.submit(c, sid, actors[0], p, at)


def test_native_program_uses_existing_collective_execution(science_lab):
    service = science()
    db, wid, sid, actors, _, at = science_lab
    with db.write() as c:
        pid = service.submit(
            c,
            sid,
            actors[0],
            {"reason": "休息后继续", "commands": [{"op": "rest", "hours": 1}]},
            at,
        )
        assert c.execute("SELECT COUNT(*) FROM collective_programs").fetchone()[0] == 1
        advance(c, wid, at + timedelta(hours=1))
        assert (
            c.execute("SELECT status FROM science_programs WHERE id=?", (pid,)).fetchone()[0]
            == "completed"
        )


@pytest.mark.parametrize("reserve", [0, 10])
def test_resonator_uses_existing_energy_and_work_without_disaster(science_lab, reserve):
    service = science()
    from world_engine.autonomous_research import ResearchService

    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        net = ResearchService.create_network(c, wid, "普通试台", at)
        device = ResearchService.add_device(
            c,
            net,
            assets["actors"][actors[0]]["site_id"],
            "test",
            "已有装置",
            physical={"center": 0.1, "width": 0.3},
        )
        ResearchService.enroll(c, actors[0], device, capability=5)
        c.execute("UPDATE research_devices SET reserve=? WHERE id=?", (reserve, device))
        pid = service.submit(
            c,
            sid,
            actors[0],
            {
                "reason": "没有灾害时自选试验",
                "commands": [
                    {
                        "op": "resonance",
                        "program": {
                            "reason": "测试已知装置",
                            "commands": [{"op": "experiment", "settings": [{"feed": 0.5}]}],
                        },
                    }
                ],
            },
            at,
        )
        if reserve == 0:
            assert (
                c.execute("SELECT status FROM science_programs WHERE id=?", (pid,)).fetchone()[0]
                == "rejected"
            )
        else:
            advance(c, wid, at + timedelta(hours=1))
            state = c.execute("SELECT * FROM science_programs WHERE id=?", (pid,)).fetchone()
            assert state["status"] == "completed", state["error"]
            assert (
                c.execute("SELECT reserve FROM research_devices WHERE id=?", (device,)).fetchone()[
                    0
                ]
                < reserve
            )
        assert (
            c.execute("SELECT status FROM research_networks WHERE id=?", (net,)).fetchone()[0]
            == "quiet"
        )
