"""哥哥，核对事件边界、真实生长时间和进程重启后的计时。"""

import json
from concurrent.futures import Future
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.clock import WorldClockService
from world_engine.collective_production import ProductionService
from world_engine.collective_types import CollectiveCommand
from world_engine.life import LifeActivityService
from world_engine.material_physics import MaterialState
from world_engine.materials import MaterialService
from world_engine.player_steering import PlayerSteeringService
from world_engine.repository import from_iso, to_iso
from world_engine.science_schema import create_session
from world_engine.worker import WorldWorker

REAL_BASE = datetime(2026, 10, 7, 8, tzinfo=UTC)


@pytest.fixture
def worker(lab, settings, monkeypatch):
    def completed_call(*args, **kwargs):
        # 哥哥，这里专测时钟，后台记忆和联络由各自的用例验证。
        future = Future()
        future.set_result(None)
        return future

    monkeypatch.setattr("world_engine.worker.submit_call", completed_call)
    instance = WorldWorker(settings)
    try:
        yield instance
    finally:
        instance.stop()
        instance.engine.close()


def prepare_tracking_world(lab, mode, scale=60):
    db, wid, actors, _, _, at = lab
    with db.write() as c:
        if mode == "science":
            create_session(c, wid, at)
        else:
            c.execute(
                "INSERT INTO material_modes(world_id,policy_json) VALUES (?,?) "
                "ON CONFLICT(world_id) DO UPDATE SET policy_json=excluded.policy_json",
                (wid, json.dumps({"mode": "collective"})),
            )
        c.execute(
            "UPDATE world_clock SET time_scale=?,last_heartbeat_real_time=? WHERE world_id=?",
            (scale, to_iso(REAL_BASE), wid),
        )
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        activity = LifeActivityService.start(c, actor, at, "wait", 30)
    return activity


@pytest.mark.parametrize("mode", ["science", "collective"])
@pytest.mark.parametrize("measured", [False, True])
def test_worker_consumes_elapsed_time_at_every_event_boundary(
    lab, worker, monkeypatch, mode, measured
):
    db, wid, _, _, _, at = lab
    activity = prepare_tracking_world(lab, mode)
    monkeypatch.setattr("world_engine.worker.utc_now", lambda: REAL_BASE + timedelta(seconds=120))

    assert worker.run_once(elapsed_seconds=None if measured else 120) == 1
    with db.read() as c:
        heartbeats = c.execute(
            "SELECT * FROM world_heartbeats WHERE world_id=? ORDER BY rowid", (wid,)
        ).fetchall()
        finished = c.execute(
            "SELECT status,finished_world_time FROM character_life_activities WHERE id=?",
            (activity,),
        ).fetchone()
        clock = c.execute("SELECT * FROM world_clock WHERE world_id=?", (wid,)).fetchone()
        assert from_iso(heartbeats[-1]["world_time_after"]) == at + timedelta(hours=2)
        assert [from_iso(row["world_time_after"]) - at for row in heartbeats] == [
            timedelta(minutes=30),
            timedelta(minutes=90),
            timedelta(minutes=120),
        ]
        assert sum(row["real_elapsed_seconds"] for row in heartbeats) == pytest.approx(120)
        assert finished["status"] == "completed"
        assert from_iso(finished["finished_world_time"]) == at + timedelta(minutes=30)
        assert from_iso(clock["last_heartbeat_real_time"]) == REAL_BASE + timedelta(seconds=120)

    monkeypatch.setattr("world_engine.worker.utc_now", lambda: REAL_BASE + timedelta(seconds=180))
    assert worker.run_once(elapsed_seconds=None if measured else 60) == 1
    with db.read() as c:
        current = c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()[0]
        assert from_iso(current) == at + timedelta(hours=3)


def test_worker_stop_keeps_unsettled_real_time_for_the_next_round(lab, worker, monkeypatch):
    db, wid, _, _, _, at = lab
    prepare_tracking_world(lab, "collective")
    monkeypatch.setattr("world_engine.worker.utc_now", lambda: REAL_BASE + timedelta(seconds=120))
    heartbeat = worker.engine.heartbeat

    def stop_after_first_boundary(*args, **kwargs):
        result = heartbeat(*args, **kwargs)
        worker.stop()
        return result

    monkeypatch.setattr(worker.engine, "heartbeat", stop_after_first_boundary)
    assert worker.run_once() == 1
    with db.read() as c:
        clock = c.execute("SELECT * FROM world_clock WHERE world_id=?", (wid,)).fetchone()
        assert from_iso(clock["last_heartbeat_real_time"]) == REAL_BASE + timedelta(seconds=30)

    worker.stop_event.clear()
    monkeypatch.setattr(worker.engine, "heartbeat", heartbeat)
    assert worker.run_once() == 1
    with db.read() as c:
        current = c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()[0]
        assert from_iso(current) == at + timedelta(hours=2)
        total = c.execute(
            "SELECT SUM(real_elapsed_seconds) FROM world_heartbeats WHERE world_id=?", (wid,)
        ).fetchone()[0]
        assert total == pytest.approx(120)


@pytest.mark.parametrize("scale", [2.7, 100.1])
def test_worker_fractional_time_scale_does_not_lose_boundary_time(lab, worker, monkeypatch, scale):
    db, wid, _, _, _, at = lab
    prepare_tracking_world(lab, "science", scale=scale)
    duration = timedelta(seconds=4000 / scale)
    monkeypatch.setattr("world_engine.worker.utc_now", lambda: REAL_BASE + duration)
    assert worker.run_once() == 1
    with db.read() as c:
        current = c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()[0]
        expected = at + timedelta(seconds=duration.total_seconds() * scale)
        assert abs((from_iso(current) - expected).total_seconds()) <= 0.000001
        heartbeats = c.execute(
            "SELECT * FROM world_heartbeats WHERE world_id=? ORDER BY rowid", (wid,)
        ).fetchall()
        assert len(heartbeats) == 2
        assert from_iso(heartbeats[0]["world_time_after"]) == at + timedelta(minutes=30)
        assert sum(row["real_elapsed_seconds"] for row in heartbeats) == pytest.approx(
            duration.total_seconds()
        )
        assert from_iso(heartbeats[-1]["real_time"]) == REAL_BASE + duration


def test_paused_tracking_world_records_real_time_without_advancing(lab, worker, monkeypatch):
    db, wid, _, _, _, at = lab
    activity = prepare_tracking_world(lab, "science", scale=0)
    monkeypatch.setattr("world_engine.worker.utc_now", lambda: REAL_BASE + timedelta(seconds=120))
    assert worker.run_once() == 1
    with db.read() as c:
        current = c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()[0]
        assert from_iso(current) == at
        assert (
            c.execute(
                "SELECT status FROM character_life_activities WHERE id=?", (activity,)
            ).fetchone()[0]
            == "running"
        )
        assert (
            c.execute(
                "SELECT real_elapsed_seconds FROM world_heartbeats WHERE world_id=?", (wid,)
            ).fetchone()[0]
            == 120
        )


def test_crop_growth_ends_at_current_world_time_and_preserves_actual_materials(lab, settings):
    from test_collective_regions import material, region

    db, wid, actors, _, _, at = lab
    with db.write() as c:
        rid, soil, source, _ = region(c, wid, actors[0], at)
        seed = material(
            c, wid, actors[0], MaterialState(carrier="seed", mass=10, nutrition=200), 10
        )
        c.execute(
            "INSERT INTO character_skill_proficiencies"
            "(world_id,character_id,skill_name,proficiency,updated_at) VALUES (?,?,'农作',80,?)",
            (wid, actors[0], to_iso(at)),
        )
        ProductionService.start(
            c,
            actors[0],
            CollectiveCommand(op="plant", region_id=rid, batch_id=seed, quantity=10, hours=1),
            at,
        )
        planted_at = at + timedelta(hours=1)
        LifeActivityService.finish(
            c, LifeActivityService.running(c, actors[0]), planted_at, "completed", "播种完成"
        )
        ProductionService.tick(c, wid, planted_at)
        crop = c.execute("SELECT * FROM collective_crops WHERE world_id=?", (wid,)).fetchone()
        plant = MaterialService.state(c, crop["crop_item_id"])
        soil_state = MaterialService.state(c, soil)
        source_state = MaterialService.state(c, source)
        (expected, _, _), _ = ProductionService.growth_states(
            plant, soil_state, source_state, crop["target_growth_mass"], 1, accessible=True
        )
        c.execute("UPDATE worlds SET current_time=? WHERE id=?", (to_iso(planted_at), wid))
        create_session(c, wid, planted_at)

    clock = WorldClockService(db, settings)
    clock.heartbeat(wid, elapsed_seconds=3600)
    with db.read() as c:
        crop = c.execute("SELECT * FROM collective_crops WHERE world_id=?", (wid,)).fetchone()
        assert from_iso(crop["last_grown_at"]) == planted_at + timedelta(hours=1)
        assert MaterialService.state(c, crop["crop_item_id"]) == expected
        assert MaterialService.audit(c, wid)["balanced"]
    clock.heartbeat(wid, elapsed_seconds=3600)
    with db.read() as c:
        crop = c.execute("SELECT * FROM collective_crops WHERE world_id=?", (wid,)).fetchone()
        assert from_iso(crop["last_grown_at"]) == planted_at + timedelta(hours=2)
        assert MaterialService.audit(c, wid)["balanced"]


@pytest.mark.parametrize("presence", ["online", "missing", "stale"])
def test_api_restart_preserves_only_online_world_clock_and_direction(
    lab, settings, monkeypatch, presence
):
    db, wid, actors, _, _, at = lab
    restarted_at = REAL_BASE + timedelta(seconds=59)
    monkeypatch.setattr("world_engine.clock.utc_now", lambda: restarted_at)
    with db.write() as c:
        c.execute(
            "UPDATE world_clock SET last_heartbeat_real_time=? WHERE world_id=?",
            (to_iso(REAL_BASE), wid),
        )
        if presence != "missing":
            seen = restarted_at if presence == "online" else restarted_at - timedelta(minutes=4)
            c.execute(
                "UPDATE world_runtime SET last_worker_seen_at=? WHERE world_id=?",
                (to_iso(seen), wid),
            )
        actor = c.execute("SELECT * FROM characters WHERE id=?", (actors[0],)).fetchone()
        PlayerSteeringService.set_direction(c, actor, (1.0, 0.0), 1, at, restarted_at)

    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").status_code == 200
        with db.read() as c:
            clock = c.execute("SELECT * FROM world_clock WHERE world_id=?", (wid,)).fetchone()
            steering = c.execute(
                "SELECT dx FROM player_steering WHERE world_id=?", (wid,)
            ).fetchone()
            online = presence == "online"
            assert from_iso(clock["last_heartbeat_real_time"]) == (
                REAL_BASE if online else restarted_at
            )
            assert steering["dx"] == (1.0 if online else 0.0)

    result = WorldClockService(db, settings).heartbeat(
        wid, real_now=REAL_BASE + timedelta(seconds=60)
    )
    assert result.real_elapsed_seconds == (60 if presence == "online" else 1)


def test_worker_restart_resets_even_a_fresh_presence_marker(lab, settings):
    db, wid, _, _, _, _ = lab
    resumed_at = REAL_BASE + timedelta(seconds=59)
    clock = WorldClockService(db, settings)
    with db.write() as c:
        c.execute(
            "UPDATE world_clock SET last_heartbeat_real_time=? WHERE world_id=?",
            (to_iso(REAL_BASE), wid),
        )
    clock.mark_worker_seen(resumed_at)
    assert clock.reset_offline_baseline(resumed_at) == 1
    result = clock.heartbeat(wid, real_now=REAL_BASE + timedelta(seconds=60))
    assert result.real_elapsed_seconds == 1
