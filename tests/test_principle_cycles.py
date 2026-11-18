"""哥哥，离线核验真实研究时间；样例不计为NPC自主研发成果。"""

import json
import sqlite3
from datetime import timedelta
from uuid import uuid4

import pytest
from test_principle_research import call, principle_lab  # noqa: F401
from test_principle_tree import foundation

from world_engine.life import LifeActivityService as Life
from world_engine.principle_cycles import projects, required_minutes, tick
from world_engine.principle_runner import PrincipleRunner
from world_engine.principle_service import PrincipleTechnologyService as T
from world_engine.principle_types import TechnologyIdea
from world_engine.repository import from_iso, to_iso


def start(c, wid, sid, actor, at):
    at = at.replace(hour=8, minute=0, second=0, microsecond=0)
    c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), wid))
    c.execute(
        "UPDATE principle_sessions SET manifest_json=? WHERE id=?",
        (json.dumps({"research_cycle_version": 1}), sid),
    )
    spec = foundation("离线通用定位工艺")
    spec.update(level="technique", domain="physical")
    spec["inventions"] = spec["inventions"][:1]
    batch = {"reason": "离线核验研究时间与独立评审", "ideas": [spec]}
    cid = call(c, sid, actor, "propose", batch, at)
    pid = T.propose(c, sid, actor, cid, batch, at)[0]
    effect = {
        "kind": "labor_time",
        "activity": "work",
        "factor": 0.8,
        "explanation": "机械定位约束减少重复动作，不凭空增加物资或能源。",
        "required_skill": "检验",
        "carrier": "knowledge",
        "materials": [],
    }
    decision = {
        "proposal_id": pid,
        "approved": True,
        "reasoning": "离线评审确认通用原理与能力边界，达成仍需实际研究周期。",
        "effect": None,
        "connections": [],
        "invention_reviews": [
            {
                "index": 0,
                "approved": True,
                "reasoning": "具体应用符合原理和当前条件，登记不代替真实研究与使用。",
                "effect": effect,
            }
        ],
    }
    rid = call(c, sid, None, "review", {"decisions": [decision]}, at)
    assert T.review(c, pid, rid, decision, at) is None
    return pid, rid, decision, at


def advance(c, wid, end):
    before = c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (wid,)).fetchone()[0]
    seconds = (end - from_iso(before)).total_seconds()
    heartbeat = str(uuid4())
    c.execute(
        "INSERT INTO world_heartbeats(id,world_id,real_time,real_elapsed_seconds,time_scale,"
        "world_delta_seconds,world_time_before,world_time_after,created_at) "
        "VALUES (?,?,?,?,1,?,?,?,?)",
        (heartbeat, wid, to_iso(end), seconds, seconds, before, to_iso(end), to_iso(end)),
    )
    c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(end), wid))
    Life.advance(c, wid, end, heartbeat)


def finish_hour(c, wid, actor, at):
    tick(c, wid, at)
    activity = Life.running(c, actor)
    assert activity and activity["kind"] == "research"
    end = from_iso(activity["ends_world_time"])
    advance(c, wid, end)
    tick(c, wid, end)
    return end, activity


def test_review_and_idle_time_do_not_unlock_technology(principle_lab):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        pid, rid, decision, at = start(c, wid, sid, actors[0], at)
        assert T.review(c, pid, rid, decision, at + timedelta(days=30)) is None
        assert not T.graph(c, wid)["nodes"]
        assert not c.execute("SELECT 1 FROM principle_knowledge").fetchone()
        row = c.execute("SELECT * FROM principle_projects").fetchone()
        assert row["required_minutes"] == 1440 and row["earned_minutes"] == 0
        assert projects(c, wid)[0]["cycle"]["progress"] == 0


def test_completed_research_segment_costs_energy_and_credits_once(principle_lab):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        _, _, _, at = start(c, wid, sid, actors[0], at)
        end, activity = finish_hour(c, wid, actors[0], at)
        Life.finish(c, activity, end, "completed", "离线重复结算检查")
        assert c.execute("SELECT earned_minutes FROM principle_projects").fetchone()[0] == 60
        assert (
            c.execute("SELECT energy FROM characters WHERE id=?", (actors[0],)).fetchone()[0] == 97
        )
        assert (
            c.execute("SELECT SUM(credited_minutes) FROM principle_research_work").fetchone()[0]
            == 60
        )
        assert not T.graph(c, wid)["nodes"]


def test_interrupted_segment_does_not_earn_progress(principle_lab):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        _, _, _, at = start(c, wid, sid, actors[0], at)
        tick(c, wid, at)
        half = at + timedelta(minutes=30)
        advance(c, wid, half)
        Life.interrupt(c, actors[0], half, "离线中断验证")
        assert c.execute("SELECT earned_minutes FROM principle_projects").fetchone()[0] == 0
        assert c.execute("SELECT credited_minutes FROM principle_research_work").fetchone()[0] == 0
        assert (
            c.execute("SELECT energy FROM characters WHERE id=?", (actors[0],)).fetchone()[0] < 100
        )


def test_daily_limit_pauses_without_creating_extra_work(principle_lab):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        _, _, _, at = start(c, wid, sid, actors[0], at)
        for _ in range(4):
            at, _ = finish_hour(c, wid, actors[0], at)
        tick(c, wid, at)
        assert not Life.running(c, actors[0])
        row = c.execute("SELECT * FROM principle_projects").fetchone()
        assert row["earned_minutes"] == 240 and row["status"] == "paused"
        assert "今日" in row["reason"]


def test_full_cycle_unlocks_once_even_after_budget_stops(principle_lab):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        pid, _, _, at = start(c, wid, sid, actors[0], at)
        c.execute("UPDATE principle_sessions SET status='stopped' WHERE id=?", (sid,))
        for day in range(6):
            when = at + timedelta(days=day)
            for _ in range(4):
                when, _ = finish_hour(c, wid, actors[0], when)
        row = c.execute("SELECT * FROM principle_projects").fetchone()
        assert row["status"] == "completed" and row["earned_minutes"] == 1440
        assert row["technology_id"] and row["completed_at"] == to_iso(when)
        assert len(T.graph(c, wid)["nodes"]) == 1
        assert T.graph(c, wid)["nodes"][0]["research_cycle"]["progress"] == 100
        tick(c, wid, when + timedelta(days=1))
        assert c.execute("SELECT count(*) FROM principle_calls").fetchone()[0] == 2
        assert c.execute("SELECT count(*) FROM principle_knowledge").fetchone()[0] == 1
        assert (
            c.execute("SELECT status FROM principle_proposals WHERE id=?", (pid,)).fetchone()[0]
            == "researched"
        )
        assert not projects(c, wid)


def test_pending_cycle_does_not_repeat_model_calls(principle_lab):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        _, _, _, at = start(c, wid, sid, actors[0], at)
    runner = PrincipleRunner(db, None, sid)
    assert runner.step(at) and runner.step(at + timedelta(minutes=1))
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM principle_calls").fetchone()[0] == 2


@pytest.mark.parametrize("state", ["night", "hungry", "resting"])
def test_life_needs_and_other_activities_pause_research(principle_lab, state):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        _, _, _, at = start(c, wid, sid, actors[0], at)
        if state == "night":
            at = at.replace(hour=23)
        elif state == "hungry":
            c.execute("UPDATE characters SET satiety=20 WHERE id=?", (actors[0],))
        else:
            actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
            Life.start(c, actor, at, "rest", 60)
        tick(c, wid, at)
        row = c.execute("SELECT * FROM principle_projects").fetchone()
        assert row["status"] == "paused" and row["earned_minutes"] == 0
        assert not c.execute("SELECT 1 FROM principle_research_work").fetchone()


def test_project_api_omits_responsible_person_and_does_not_write(principle_lab):  # noqa: F811
    from test_science_api import client

    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        start(c, wid, sid, actors[0], at)
        before = c.execute("SELECT count(*) FROM world_events").fetchone()[0]
    response = client(db).get(f"/api/worlds/{wid}/science/projects")
    assert response.status_code == 200 and response.json()[0]["cycle"]["required_minutes"] == 1440
    assert actors[0] not in response.text and "inventor_id" not in response.text
    with db.read() as c:
        assert c.execute("SELECT count(*) FROM world_events").fetchone()[0] == before


def test_duration_policy_and_immutability(principle_lab):  # noqa: F811
    spec = TechnologyIdea.model_validate(foundation("离线基础原理"))
    assert required_minutes(spec) == 48 * 60
    assert required_minutes(spec.model_copy(update={"domain": "hybrid"})) == 72 * 60
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        start(c, wid, sid, actors[0], at)
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("UPDATE principle_projects SET required_minutes=60")


def test_summary_counter_and_manifest_changes_cannot_skip_cycle(principle_lab):  # noqa: F811
    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        pid, rid, decision, at = start(c, wid, sid, actors[0], at)
        c.execute("UPDATE principle_projects SET earned_minutes=required_minutes")
        c.execute("UPDATE principle_sessions SET manifest_json='{}' WHERE id=?", (sid,))
        with pytest.raises(ValueError, match="真实完成的研究工时"):
            T.review(c, pid, rid, decision, at + timedelta(days=30))
        assert not T.graph(c, wid)["nodes"]


def test_world_clock_settles_cycle_without_model_requests(principle_lab, settings):  # noqa: F811
    from world_engine.clock import WorldClockService

    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        _, _, _, at = start(c, wid, sid, actors[0], at)
        tick(c, wid, at)
        scale = c.execute("SELECT time_scale FROM world_clock WHERE world_id=?", (wid,)).fetchone()[
            0
        ]
    WorldClockService(db, settings).heartbeat(wid, elapsed_seconds=3600 / scale)
    with db.read() as c:
        assert c.execute("SELECT earned_minutes FROM principle_projects").fetchone()[0] == 60
        assert not T.graph(c, wid)["nodes"]
        assert c.execute("SELECT count(*) FROM principle_calls").fetchone()[0] == 2


def test_runner_starts_research_instead_of_immediate_registration(principle_lab):  # noqa: F811
    from test_principle_runner import Backend

    db, wid, sid, _, at = principle_lab
    with db.write() as c:
        c.execute(
            "UPDATE principle_sessions SET manifest_json=? WHERE id=?",
            (json.dumps({"research_cycle_version": 1}), sid),
        )
    backend = Backend()
    runner = PrincipleRunner(db, backend, sid)
    assert runner.step(at)
    with db.read() as c:
        assert not T.graph(c, wid)["nodes"]
        assert projects(c, wid)[0]["cycle"]["required_minutes"] == 2880
        assert c.execute("SELECT count(*) FROM principle_research_work").fetchone()[0] == 1
    assert runner.step(at)
    assert len(backend.calls) == 2


@pytest.mark.parametrize(
    "identity,hour,minute", [("巡夜人", 8, 0), ("巡夜人", 6, 30), ("工匠", 21, 30)]
)
def test_research_respects_sleep_and_does_not_cross_sleep_boundary(
    principle_lab,  # noqa: F811
    identity,
    hour,
    minute,
):
    from world_engine.daily_life import DailyLifeService

    db, wid, sid, actors, at = principle_lab
    with db.write() as c:
        _, _, _, at = start(c, wid, sid, actors[0], at)
        c.execute("UPDATE characters SET identity=? WHERE id=?", (identity, actors[0]))
        at = at.replace(hour=hour, minute=minute)
        tick(c, wid, at)
        assert not c.execute("SELECT 1 FROM principle_research_work").fetchone()
        DailyLifeService.tick(c, wid, at)
        activity = Life.running(c, actors[0])
        assert not activity or activity["kind"] != "research"


def test_settle_only_finishes_submitted_research_before_application_without_backend(
    principle_lab,  # noqa: F811
    settings,
    tmp_path,
    monkeypatch,
):
    import sys

    from scripts import demo_principle as demo

    db, wid, sid, _, _ = principle_lab
    with db.write() as c:
        c.execute(
            "UPDATE principle_sessions SET manifest_json=? WHERE id=?",
            (json.dumps({"source_files": {}, "world_files": {}}), sid),
        )
    output = tmp_path / "settlement"
    output.mkdir()
    (output / "run.json").write_text(
        json.dumps({"world_id": wid, "session_id": sid}), encoding="utf-8"
    )
    observed = []

    def no_backend(*args, **kwargs):
        pytest.fail("仅结算入口不得创建模型后端")

    def research(database, config, info):
        assert config.deepseek_api_key is None
        observed.append("research")
        return True

    monkeypatch.setattr(demo, "Database", lambda *args, **kwargs: db)
    monkeypatch.setattr(demo, "settings", lambda path: settings)
    monkeypatch.setattr(demo, "file_manifest", lambda paths: {})
    monkeypatch.setattr(demo, "build_agent_model_backend", no_backend)
    monkeypatch.setattr(demo, "settle_research", research)
    monkeypatch.setattr(demo, "settle_applications", lambda *args: observed.append("application"))
    monkeypatch.setattr(demo, "export_principle", lambda *args: observed.append("export"))
    monkeypatch.setattr(
        sys, "argv", ["demo_principle.py", "--output", str(output), "--settle-only"]
    )
    demo.main()
    assert observed == ["research", "application", "export"]
