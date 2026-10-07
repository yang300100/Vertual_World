"""哥哥，先用离线证据约束通用研究，测试不会调用真实 API。"""

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest


def advance_lab(c, wid, at):
    """单元实验室只推进活动时间；完整生活状态另由正常引擎集成测试验证。"""
    from world_engine.autonomous_research import ResearchService
    from world_engine.repository import from_iso, to_iso

    before = c.execute("SELECT w.current_time FROM worlds w WHERE w.id=?", (wid,)).fetchone()[0]
    seconds = (at - from_iso(before)).total_seconds()
    heartbeat = str(uuid4())
    c.execute(
        "INSERT INTO world_heartbeats(id,world_id,real_time,real_elapsed_seconds,time_scale,world_delta_seconds,"
        "world_time_before,world_time_after,created_at) VALUES (?,?,?, ?,1,?,?,?,?)",
        (heartbeat, wid, to_iso(at), seconds, seconds, before, to_iso(at), to_iso(at)),
    )
    c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(at), wid))
    ResearchService.tick(c, wid, at, heartbeat_id=heartbeat)


@pytest.fixture
def lab(database):
    from world_engine.autonomous_research import ResearchService
    from world_engine.repository import WorldRepository, to_iso

    at = datetime(2040, 4, 1, 8, tzinfo=UTC)
    with database.write() as c:
        wid = WorldRepository().create_world(
            c,
            name="通用实验室",
            minutes_per_tick=60,
            seed_demo=False,
            seed_maps=False,
            start_time=at,
        )
        net = ResearchService.create_network(c, wid, "实验装置网络", at)
        actors, devices = [], []
        for index in range(2):
            location, actor = str(uuid4()), str(uuid4())
            c.execute(
                "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) "
                "VALUES (?,?,?,'city',?,0)",
                (location, wid, f"城{index}", index * 10),
            )
            c.execute(
                "INSERT INTO characters(id,world_id,name,identity,location_id,"
                "current_location_id,longitude,latitude,energy,satiety,money,created_at,updated_at) "
                "VALUES (?,?,?,'工坊学者',?,?,?,0,100,100,100,?,?)",
                (
                    actor,
                    wid,
                    f"学者{index}",
                    location,
                    location,
                    index * 10,
                    to_iso(at),
                    to_iso(at),
                ),
            )
            device = ResearchService.add_device(
                c,
                net,
                location,
                f"polity-{index}",
                f"装置{index}",
                physical={"center": 0.1 * index, "width": 0.25},
            )
            ResearchService.enroll(c, actor, device, capability=5 + index)
            actors.append(actor)
            devices.append(device)
        ResearchService.add_contact(c, actors[0], actors[1])
        ResearchService.add_contact(c, actors[1], actors[0])
        ResearchService.disturb(c, net, forcing=2.5, at=at, description="出现陌生脉动")
    return database, wid, net, actors, devices, at


def test_research_capability_exists():
    import importlib.util

    assert importlib.util.find_spec("world_engine.autonomous_research") is not None


def test_local_payload_does_not_reveal_hidden_physics_or_remote_facts(lab):
    from world_engine.autonomous_research import ResearchService as R

    db, _, _, actors, devices, at = lab
    with db.read() as c:
        context = R.context(c, actors[0], at)
    text = json.dumps(context)
    assert "center" not in text and "width" not in text and "physical" not in text
    assert devices[1] not in text
    assert context["observations"] and context["contacts"]


def test_no_program_means_no_recovery_and_no_experiments(lab):
    from world_engine.autonomous_research import ResearchService as R

    db, wid, net, _, _, at = lab
    with db.write() as c:
        advance_lab(c, wid, at + timedelta(hours=100))
        assert R.network_view(c, net)["status"] == "active"
        assert c.execute("SELECT count(*) FROM research_steps").fetchone()[0] == 0


def test_switching_off_cannot_pass_public_service_recovery(lab):
    from world_engine.autonomous_research import ResearchService as R

    db, wid, net, actors, _, at = lab
    with db.write() as c:
        for actor in actors:
            R.accept_program(
                c,
                actor,
                {
                    "reason": "关闭测试",
                    "commands": [{"op": "configure", "settings": [{"feed": 0}]}],
                },
                at,
            )
        advance_lab(c, wid, at + timedelta(hours=2))
        advance_lab(c, wid, at + timedelta(hours=100))
        view = R.network_view(c, net)
        assert view["status"] == "active"
        assert all(d["service"] == 0 for d in view["devices"])


def test_trial_consumes_resources_and_does_not_change_live_controls(lab):
    from world_engine.autonomous_research import ResearchService as R

    db, wid, _, actors, devices, at = lab
    with db.write() as c:
        before = dict(
            c.execute("SELECT * FROM research_devices WHERE id=?", (devices[0],)).fetchone()
        )
        R.accept_program(
            c,
            actors[0],
            {
                "reason": "测量候选",
                "commands": [{"op": "experiment", "settings": [{"tune": -0.8}, {"tune": 0.8}]}],
            },
            at,
        )
        advance_lab(c, wid, at + timedelta(hours=3))
        after = c.execute("SELECT * FROM research_devices WHERE id=?", (devices[0],)).fetchone()
        assert after["controls_json"] == before["controls_json"]
        assert after["reserve"] < before["reserve"]
        trials = c.execute(
            "SELECT result_json FROM research_steps WHERE operation='experiment'"
        ).fetchall()
        assert len(trials) == 1
        assert len(json.loads(trials[0][0])["trials"]) == 2


def test_message_is_not_knowledge_until_distance_delay_passes(lab):
    from world_engine.autonomous_research import ResearchService as R

    db, wid, _, actors, _, at = lab
    with db.write() as c:
        R.accept_program(
            c,
            actors[0],
            {
                "reason": "传递观测",
                "commands": [{"op": "send", "recipients": "contacts", "text": "本地出现脉动"}],
            },
            at,
        )
        advance_lab(c, wid, at + timedelta(hours=1))
        row = c.execute("SELECT * FROM research_messages").fetchone()
        assert row and row["delivered_event_id"] is None
        assert not R.context(c, actors[1], at + timedelta(hours=1))["messages"]
        advance_lab(c, wid, at + timedelta(hours=80))
        assert R.context(c, actors[1], at + timedelta(hours=80))["messages"]


def test_one_unchanged_evidence_snapshot_does_not_spend_repeated_calls(lab):
    from world_engine.agent_llm import ModelCompletion
    from world_engine.autonomous_research import ResearchRunner

    class Backend:
        calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            return ModelCompletion(
                data=kwargs["schema"].validate_python({"reason": "等待证据", "commands": []}),
                input_tokens=30,
                output_tokens=20,
            )

    db, wid, _, _, _, at = lab
    backend = Backend()
    runner = ResearchRunner(db, backend, max_calls=1, token_limit=30000)
    assert runner.decide(wid, at)
    assert not runner.decide(wid, at)
    assert backend.calls == 1
    with db.read() as c:
        assert (
            c.execute("SELECT sum(input_tokens+output_tokens) FROM research_calls").fetchone()[0]
            == 50
        )


def test_new_event_ids_with_identical_measurements_are_not_new_research_evidence():
    from world_engine.research_runner import evidence_fingerprint

    first = {
        "actor": {"id": "npc"},
        "observations": [
            {
                "id": "old",
                "kind": "research.measure",
                "text": "测量",
                "evidence": {"program_id": "a", "reading": {"hazard": 1}},
            }
        ],
    }
    second = {
        "actor": {"id": "npc"},
        "observations": [
            {
                "id": "new",
                "kind": "research.measure",
                "text": "测量",
                "evidence": {"program_id": "b", "reading": {"hazard": 1}},
            }
        ],
    }
    assert evidence_fingerprint(first) == evidence_fingerprint(second)
    second["observations"][0]["evidence"]["reading"]["hazard"] = 0.5
    assert evidence_fingerprint(first) != evidence_fingerprint(second)


def test_new_global_scenario_has_only_initial_disaster_and_no_prescribed_program(database):
    from scripts.demo_autonomous_disaster import build_scenario
    from world_engine.autonomous_research import ResearchService as R
    from world_engine.polities import current_polities

    wid, net, _ = build_scenario(database)
    with database.read() as c:
        view = R.network_view(c, net)
        assert {d["polity_key"] for d in view["devices"]} == {p["key"] for p in current_polities()}
        assert all(d["hazard"] > view["hazard_limit"] for d in view["devices"])
        assert c.execute("SELECT count(*) FROM research_programs").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM research_calls").fetchone()[0] == 0
        assert (
            c.execute(
                "SELECT count(*) FROM cooperative_crises WHERE world_id=?", (wid,)
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute("SELECT count(*) FROM activity_recipes WHERE world_id=?", (wid,)).fetchone()[
                0
            ]
            == 0
        )


def test_program_cannot_forge_a_trial_or_global_success(lab):
    from world_engine.autonomous_research import ResearchService as R

    db, _, net, actors, _, at = lab
    with db.write() as c:
        with pytest.raises(ValueError):
            R.accept_program(
                c,
                actors[0],
                {"reason": "假结论", "commands": [{"op": "resolve", "status": "resolved"}]},
                at,
            )
        assert R.network_view(c, net)["status"] == "active"


def test_research_occupies_real_activity_time_and_unfit_people_cannot_experiment(lab):
    from world_engine.autonomous_research import ResearchService as R
    from world_engine.life import LifeActivityService

    db, wid, _, actors, _, at = lab
    with db.write() as c:
        R.accept_program(
            c,
            actors[0],
            {
                "reason": "持续观察",
                "commands": [{"op": "experiment", "settings": [{"tune": 0.2}, {"tune": 0.4}]}],
            },
            at,
        )
        active = LifeActivityService.running(c, actors[0])
        assert active and active["kind"] == "research"
        with pytest.raises(ValueError):
            actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
            LifeActivityService.start(c, actor, at, "rest", 60)
        c.execute("UPDATE characters SET satiety=0,energy=0 WHERE id=?", (actors[1],))
        R.accept_program(
            c,
            actors[1],
            {
                "reason": "先登记但身体不能执行",
                "commands": [{"op": "experiment", "settings": [{"tune": 0.5}]}],
            },
            at,
        )
        advance_lab(c, wid, at + timedelta(hours=3))
        assert (
            c.execute(
                "SELECT count(*) FROM research_steps s JOIN research_programs p ON p.id=s.program_id "
                "WHERE p.character_id=?",
                (actors[1],),
            ).fetchone()[0]
            == 0
        )
        assert (
            c.execute("SELECT energy FROM characters WHERE id=?", (actors[0],)).fetchone()[0] < 100
        )


def test_disabled_researcher_does_not_hold_the_entire_model_scheduler(lab):
    from world_engine.agent_llm import ModelCompletion
    from world_engine.autonomous_research import ResearchRunner
    from world_engine.autonomous_research import ResearchService as R

    class Backend:
        def complete(self, **kwargs):
            return ModelCompletion(
                data=kwargs["schema"].validate_python({"reason": "需要观察", "commands": []}),
                input_tokens=10,
                output_tokens=10,
            )

    db, wid, _, actors, _, at = lab
    with db.write() as c:
        R.accept_program(
            c, actors[0], {"reason": "原有研究", "commands": [{"op": "wait", "hours": 40}]}, at
        )
        c.execute("UPDATE research_people SET enabled=0 WHERE character_id=?", (actors[0],))
    assert ResearchRunner(db, Backend()).decide(wid, at)


def test_frozen_database_allows_engine_transactions_but_detects_external_writes(lab):
    from scripts.demo_autonomous_disaster import FrozenDatabase

    db, _, _, actors, _, _ = lab
    guarded = FrozenDatabase(db.path, legacy_world_defaults=False)
    guarded.arm()
    with guarded.write() as c:
        c.execute("UPDATE characters SET money=money+1 WHERE id=?", (actors[0],))
    with guarded.read() as c:
        assert (
            c.execute("SELECT money FROM characters WHERE id=?", (actors[0],)).fetchone()[0] == 101
        )
    with db.write() as c:
        c.execute("UPDATE characters SET money=money+1 WHERE id=?", (actors[0],))
    with pytest.raises(RuntimeError, match="外部"):
        with guarded.read():
            pass


def test_absent_researcher_does_not_hold_model_scheduler(lab):
    from world_engine.agent_llm import ModelCompletion
    from world_engine.autonomous_research import ResearchRunner
    from world_engine.autonomous_research import ResearchService as R

    class Backend:
        calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            return ModelCompletion(
                data=kwargs["schema"].validate_python({"reason": "新观察", "commands": []}),
                input_tokens=10,
                output_tokens=10,
            )

    db, wid, _, actors, devices, at = lab
    with db.write() as c:
        destination = R._device(c, devices[1])["location_id"]
        c.execute(
            "UPDATE characters SET current_location_id=? WHERE id=?", (destination, actors[0])
        )
        pid = R.accept_program(
            c, actors[0], {"reason": "尚未到现场", "commands": [{"op": "measure"}]}, at
        )
        assert (
            c.execute("SELECT status FROM research_programs WHERE id=?", (pid,)).fetchone()[0]
            == "blocked"
        )
    backend = Backend()
    assert ResearchRunner(db, backend).decide(wid, at)
    assert backend.calls == 1
    with db.read() as c:
        assert c.execute("SELECT character_id FROM research_calls").fetchone()[0] == actors[1]


def test_cancelled_program_releases_activity_and_does_not_charge_energy(lab):
    from world_engine.autonomous_research import ResearchService as R
    from world_engine.life import LifeActivityService

    db, wid, _, actors, _, at = lab
    with db.write() as c:
        pid = R.accept_program(
            c,
            actors[0],
            {
                "reason": "持续测量",
                "commands": [{"op": "experiment", "settings": [{"tune": 0.2}, {"tune": 0.4}]}],
            },
            at,
        )
        activity = LifeActivityService.running(c, actors[0])
        assert activity and activity["kind"] == "research"
        c.execute("UPDATE research_people SET enabled=0 WHERE character_id=?", (actors[0],))
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        assert not R.advance_work(c, activity, actor, at + timedelta(hours=1), "unused-heartbeat")
        assert (
            c.execute("SELECT energy FROM characters WHERE id=?", (actors[0],)).fetchone()[0] == 100
        )
        advance_lab(c, wid, at + timedelta(hours=3))
        assert (
            c.execute("SELECT status FROM research_programs WHERE id=?", (pid,)).fetchone()[0]
            == "cancelled"
        )
        assert LifeActivityService.running(c, actors[0]) is None
        assert c.execute("SELECT count(*) FROM research_steps").fetchone()[0] == 0
        assert (
            c.execute("SELECT energy FROM characters WHERE id=?", (actors[0],)).fetchone()[0] == 100
        )


def test_all_21_polities_can_execute_received_research_under_normal_heartbeats(database):
    from dataclasses import replace

    from scripts.demo_autonomous_disaster import build_scenario
    from world_engine.agent_llm import ModelCompletion
    from world_engine.autonomous_research import ResearchService as R
    from world_engine.config import Settings
    from world_engine.engine import WorldEngine
    from world_engine.research_physics import DEFAULT_CONTROLS

    class OfflineBackend:
        calls = 0

        def complete(self, **kwargs):
            self.calls += 1
            # 仅验证执行层；这些假模型步骤绝不会进入正式场景初始化。
            program = {
                "reason": "离线测试中的人物自拟程序",
                "commands": [
                    {"op": "experiment", "settings": [{"tune": -0.8}, {"tune": 0.8}]},
                    {"op": "configure", "select": "lowest_noise", "minimum_service": 0.65},
                    {
                        "op": "send",
                        "recipients": "contacts",
                        "text": "同行请在本地复验",
                        "share_program": True,
                    },
                ],
            }
            return ModelCompletion(
                data=kwargs["schema"].validate_python(program), input_tokens=100, output_tokens=100
            )

    wid, net, _ = build_scenario(database)
    settings = replace(
        Settings(database_path=database.path),
        research_api_enabled=True,
        knowledge_enabled=False,
        adjudication_interval_minutes=180000,
    )
    engine = WorldEngine(database, settings)
    backend = OfflineBackend()
    engine.research_runner.backend = backend
    try:
        for _ in range(1300):
            engine.heartbeat(wid, elapsed_seconds=3600)
            with database.read() as c:
                view = R.network_view(c, net)
            if view["status"] == "resolved":
                break
        assert view["status"] == "resolved"
        assert len(view["devices"]) == 21 and backend.calls == 1
        with database.write() as c:
            for device in view["devices"]:
                assert device["reserve"] < 200 and device["service"] >= view["service_min"]
                current = c.execute(
                    "SELECT controls_json FROM research_devices WHERE id=?", (device["id"],)
                ).fetchone()[0]
                c.execute(
                    "UPDATE research_devices SET controls_json=? WHERE id=?",
                    (json.dumps(DEFAULT_CONTROLS), device["id"]),
                )
                without = R.network_view(c, net)
                assert any(d["hazard"] > view["hazard_limit"] for d in without["devices"])
                c.execute(
                    "UPDATE research_devices SET controls_json=? WHERE id=?",
                    (current, device["id"]),
                )
            assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert not c.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        engine.close()


@pytest.mark.parametrize("holdout", [False, True])
def test_shared_research_is_repeated_locally_and_a_holdout_keeps_the_world_unsafe(lab, holdout):
    from world_engine.autonomous_research import ResearchService as R

    db, wid, net, actors, _, at = lab
    # 这是离线测试中的假模型产物，生产场景不会登记该程序。
    spec = {
        "reason": "以本地实测检验我的假说",
        "commands": [
            {"op": "experiment", "settings": [{"tune": -0.8}, {"tune": 0.8}]},
            {"op": "configure", "select": "lowest_noise", "minimum_service": 0.65},
            {
                "op": "send",
                "recipients": "contacts",
                "share_program": True,
                "text": "附上实测，请自行检验",
            },
        ],
    }
    with db.write() as c:
        if holdout:
            c.execute("UPDATE research_people SET enabled=0 WHERE character_id=?", (actors[1],))
        R.accept_program(c, actors[0], spec, at)
        for hour in range(1, 150):
            advance_lab(c, wid, at + timedelta(hours=hour))
        view = R.network_view(c, net)
        assert view["status"] == ("active" if holdout else "resolved")
        if not holdout:
            rows = c.execute(
                "SELECT character_id,author_id,source_message_id FROM research_programs"
            ).fetchall()
            received = next(r for r in rows if r["character_id"] == actors[1])
            assert received["author_id"] == actors[0] and received["source_message_id"]
            assert all(d["reserve"] < 200 and d["service"] >= 0.65 for d in view["devices"])


def test_normal_engine_heartbeat_runs_research_and_program_selects_api_actor(lab):
    from dataclasses import replace

    from world_engine.agent_llm import ModelCompletion
    from world_engine.config import Settings
    from world_engine.engine import WorldEngine

    class Backend:
        seen_actor = None

        def complete(self, **kwargs):
            self.seen_actor = kwargs["user_payload"]["actor"]["id"]
            return ModelCompletion(
                data=kwargs["schema"].validate_python(
                    {"reason": "先记录当前表现", "commands": [{"op": "measure"}]}
                ),
                input_tokens=40,
                output_tokens=30,
            )

    db, wid, _, actors, _, _ = lab
    settings = replace(
        Settings(database_path=db.path), research_api_enabled=True, research_max_calls=1
    )
    engine = WorldEngine(db, settings)
    backend = Backend()
    engine.research_runner.backend = backend
    for _ in range(10):
        engine.heartbeat(wid, elapsed_seconds=3600)
    assert backend.seen_actor == actors[1]
    engine.heartbeat(wid, elapsed_seconds=3600)
    with db.read() as c:
        assert (
            c.execute("SELECT count(*) FROM research_steps WHERE operation='measure'").fetchone()[0]
            == 1
        )
    engine.close()
