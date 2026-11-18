"""哥哥，页面必须展示实际试验，等待和中断不能变成应用收益。"""

import json
from datetime import timedelta

from test_science_api import client
from test_science_programs import advance, craft_program, science

from world_engine.collective_research import CollectiveResearchService as R
from world_engine.life import LifeActivityService
from world_engine.repository import to_iso

pytest_plugins = ["science_fixtures", "test_science_methods"]


def test_queued_physical_program_is_visible_without_fictitious_time(science_lab):
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        LifeActivityService.start(c, R.actor(c, actors[0]), at, "rest", 60)
        pid = science().submit(c, sid, actors[0], craft_program(assets["actors"][actors[0]]), at)
        c.execute(
            "UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at + timedelta(minutes=30)), wid)
        )
    response = client(db).get(f"/api/worlds/{wid}/science/projects")
    assert response.status_code == 200
    project = next(p for p in response.json() if p["id"] == pid)
    assert project["mode"] == "physical_experiment"
    assert project["cycle"]["status"] == "paused"
    assert project["cycle"]["earned_minutes"] == 0
    assert project["cycle"]["progress"] is None
    assert project["cycle"]["required_minutes"] is None
    assert project["cycle"]["current_stage"]["worked_minutes"] == 0
    assert "inventor_name" not in project
    with db.read() as c:
        assert (
            c.execute("SELECT count(*) FROM technologies WHERE world_id=?", (wid,)).fetchone()[0]
            == 0
        )


def test_completed_work_is_counted_but_not_an_unlocked_technology(science_lab):
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        pid = science().submit(c, sid, actors[0], craft_program(assets["actors"][actors[0]]), at)
        advance(c, wid, at)
        advance(c, wid, at + timedelta(hours=1))
    api = client(db)
    project = next(
        p for p in api.get(f"/api/worlds/{wid}/science/projects").json() if p["id"] == pid
    )
    assert project["cycle"]["settled_steps"] == 1
    assert project["cycle"]["earned_minutes"] == 60
    assert project["cycle"]["current_stage"]["operation"] == "assay"
    assert api.get(f"/api/worlds/{wid}/science").json()["technology_count"] == 0
    assert api.get(f"/api/worlds/{wid}/science/impacts").json() == []


def test_completed_method_reuse_has_real_outputs_and_work_not_pending_income(certified):
    from test_science_certification import bindings, settle
    from test_science_methods import methods

    from world_engine.science_programs import ScienceService

    db, wid, sid, actors, assets, at, tid = certified
    with db.write() as c:
        eid = methods().execute_method(
            c, sid, actors[0], tid, 1, bindings(assets["actors"][actors[0]]), {}, at
        )
        ScienceService.tick(c, wid, at)
    api = client(db)
    assert api.get(f"/api/worlds/{wid}/science/impacts").json() == []
    with db.write() as c:
        settle(c, wid, at + timedelta(hours=1))
        settle(c, wid, at + timedelta(hours=1.5))
        execution = c.execute("SELECT * FROM technology_executions WHERE id=?", (eid,)).fetchone()
        assert execution["status"] == "completed"
        outputs = json.loads(execution["output_ids_json"])
    response = api.get(f"/api/worlds/{wid}/science/impacts")
    impact = next(i for i in response.json() if i["source_id"] == eid)
    assert impact["source_status"] == "completed" and impact["technology_id"] == tid
    result = json.loads(impact["after_json"])
    assert result["本次记录的产物批次"] == len(outputs)
    assert result["已结算劳动小时"] == 1.5
    assert "strength_scale" not in response.text
    assert api.get("/api/worlds/missing/science/impacts").status_code == 404


def test_interrupted_method_reuse_is_not_reported_as_completed_impact(certified):
    from test_science_certification import bindings, settle
    from test_science_methods import methods

    from world_engine.science_programs import ScienceService

    db, wid, sid, actors, assets, at, tid = certified
    with db.write() as c:
        methods().execute_method(
            c, sid, actors[0], tid, 1, bindings(assets["actors"][actors[0]]), {}, at
        )
        ScienceService.tick(c, wid, at)
        c.execute("UPDATE characters SET health=health-1 WHERE id=?", (actors[0],))
        settle(c, wid, at + timedelta(minutes=30))
    assert client(db).get(f"/api/worlds/{wid}/science/impacts").json() == []


def test_parent_label_cannot_turn_working_orders_into_completed_impact(certified):
    from test_science_certification import bindings
    from test_science_methods import methods

    from world_engine.science_programs import ScienceService

    db, wid, sid, actors, assets, at, tid = certified
    with db.write() as c:
        eid = methods().execute_method(
            c, sid, actors[0], tid, 1, bindings(assets["actors"][actors[0]]), {}, at
        )
        ScienceService.tick(c, wid, at)
        # 哥哥，离线模拟上层标签错误；页面必须核对实际工单，而不是相信标签。
        c.execute(
            "UPDATE technology_executions SET status='completed',completed_at=? WHERE id=?",
            (to_iso(at), eid),
        )
    assert client(db).get(f"/api/worlds/{wid}/science/impacts").json() == []


def test_reading_physical_projects_and_impacts_does_not_change_world(science_lab):
    from world_engine.science_freeze import state_digest

    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        science().submit(c, sid, actors[0], craft_program(assets["actors"][actors[0]]), at)
        advance(c, wid, at)
    with db.read() as c:
        before = state_digest(c)
    api = client(db)
    assert api.get(f"/api/worlds/{wid}/science/projects").status_code == 200
    assert api.get(f"/api/worlds/{wid}/science/impacts").status_code == 200
    with db.read() as c:
        assert state_digest(c) == before


def test_native_inspection_is_researching_and_retains_completed_labor(science_lab):
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        pid = science().submit(
            c,
            sid,
            actors[0],
            {
                "reason": "先观察现场再进行材料试验",
                "commands": [
                    {"op": "inspect", "hours": 1},
                    craft_program(assets["actors"][actors[0]])["commands"][0],
                ],
            },
            at,
        )
        advance(c, wid, at + timedelta(minutes=30))
    api = client(db)
    project = next(
        p for p in api.get(f"/api/worlds/{wid}/science/projects").json() if p["id"] == pid
    )
    assert project["cycle"]["status"] == "researching"
    assert project["cycle"]["current_stage"]["operation"] == "inspect"
    assert project["cycle"]["current_stage"]["worked_minutes"] == 30
    with db.write() as c:
        advance(c, wid, at + timedelta(hours=1))
    project = next(
        p for p in api.get(f"/api/worlds/{wid}/science/projects").json() if p["id"] == pid
    )
    assert project["cycle"]["earned_minutes"] == 60
    assert project["cycle"]["current_stage"]["operation"] == "heat"


def test_native_waiting_time_is_not_research_labor(science_lab):
    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        pid = science().submit(
            c,
            sid,
            actors[0],
            {
                "reason": "等待后再实际加工",
                "commands": [
                    {"op": "wait", "hours": 1},
                    craft_program(assets["actors"][actors[0]])["commands"][0],
                ],
            },
            at,
        )
        advance(c, wid, at + timedelta(minutes=30))
    api = client(db)
    project = next(
        p for p in api.get(f"/api/worlds/{wid}/science/projects").json() if p["id"] == pid
    )
    assert project["cycle"]["earned_minutes"] == 0
    assert project["cycle"]["current_stage"]["is_research"] is False
    with db.write() as c:
        advance(c, wid, at + timedelta(hours=1))
    project = next(
        p for p in api.get(f"/api/worlds/{wid}/science/projects").json() if p["id"] == pid
    )
    assert project["cycle"]["earned_minutes"] == 0


def test_existing_resonator_tracks_real_activity_without_claiming_magic(science_lab):
    from world_engine.autonomous_research import ResearchService

    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        net = ResearchService.create_network(c, wid, "离线普通试台", at)
        device = ResearchService.add_device(
            c,
            net,
            assets["actors"][actors[0]]["site_id"],
            "test",
            "已有装置",
            physical={"center": 0.1, "width": 0.3},
        )
        ResearchService.enroll(c, actors[0], device, capability=5)
        pid = science().submit(
            c,
            sid,
            actors[0],
            {
                "reason": "测试已有装置",
                "commands": [
                    {
                        "op": "resonance",
                        "program": {
                            "reason": "测试",
                            "commands": [
                                {"op": "experiment", "hours": 6, "settings": [{"feed": 0.5}] * 6}
                            ],
                        },
                    }
                ],
            },
            at,
        )
        advance(c, wid, at + timedelta(hours=1))
    api = client(db)
    project = next(
        p for p in api.get(f"/api/worlds/{wid}/science/projects").json() if p["id"] == pid
    )
    assert project["domain"] == "physical"
    assert project["cycle"]["status"] == "researching"
    assert project["cycle"]["current_stage"]["worked_minutes"] == 60
    with db.write() as c:
        advance(c, wid, at + timedelta(hours=4))
    project = next(
        p for p in api.get(f"/api/worlds/{wid}/science/projects").json() if p["id"] == pid
    )
    assert project["cycle"]["earned_minutes"] == 240


def test_missing_native_source_is_unknown_instead_of_paused(science_lab):
    db, wid, sid, actors, _, at = science_lab
    with db.write() as c:
        pid = science().submit(
            c,
            sid,
            actors[0],
            {
                "reason": "现场观察",
                "commands": [{"op": "inspect", "hours": 1}],
            },
            at,
        )
        # 哥哥，离线模拟缺失来源记录，不能把缺失信息包装成已知暂停。
        c.execute("DELETE FROM collective_jobs")
    response = client(db).get(f"/api/worlds/{wid}/science/projects")
    project = next(p for p in response.json() if p["id"] == pid)
    assert project["cycle"]["status"] == "unmapped"
    assert project["cycle"]["earned_minutes"] == 0
    assert project["cycle"]["progress"] is None


def test_idempotent_resonance_source_does_not_double_count_work(science_lab):
    from world_engine.autonomous_research import ResearchService

    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        net = ResearchService.create_network(c, wid, "同一试台", at)
        device = ResearchService.add_device(
            c,
            net,
            assets["actors"][actors[0]]["site_id"],
            "test",
            "已有装置",
            physical={"center": 0.1, "width": 0.3},
        )
        ResearchService.enroll(c, actors[0], device, capability=5)
        command = {
            "op": "resonance",
            "program": {
                "reason": "相同的程序",
                "commands": [{"op": "experiment", "settings": [{"feed": 0.5}]}],
            },
        }
        pid = science().submit(
            c,
            sid,
            actors[0],
            {"reason": "核对幂等来源", "commands": [command, command, {"op": "wait", "hours": 2}]},
            at,
        )
        advance(c, wid, at + timedelta(hours=1))
        science().tick(c, wid, at + timedelta(hours=1))
    response = client(db).get(f"/api/worlds/{wid}/science/projects")
    project = next(p for p in response.json() if p["id"] == pid)
    assert project["cycle"]["earned_minutes"] == 60
    assert project["cycle"]["settled_steps"] == 1
