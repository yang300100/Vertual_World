"""哥哥，正常心跳驱动物理结算，科研模式隔离所有付费旁路。"""

import json
from dataclasses import replace
from datetime import timedelta

import pytest

pytest_plugins = ["science_fixtures"]


def test_old_world_has_no_science_session(database):
    from world_engine.repository import WorldRepository

    with database.write() as c:
        wid = WorldRepository().create_world(
            c, name="旧世界", minutes_per_tick=60, seed_demo=False, seed_maps=False
        )
        assert not c.execute("SELECT 1 FROM science_sessions WHERE world_id=?", (wid,)).fetchone()


def test_normal_heartbeat_completes_science_work(science_lab, settings):
    from test_science_programs import craft_program

    from world_engine.engine import WorldEngine
    from world_engine.repository import to_iso
    from world_engine.science_programs import ScienceService

    db, wid, sid, actors, assets, at = science_lab
    with db.write() as c:
        # 测试lab旧模式不是产品初始化，明确换为科研模式。
        c.execute(
            "UPDATE material_modes SET policy_json=? WHERE world_id=?",
            (json.dumps({"mode": "science"}), wid),
        )
        c.execute(
            "UPDATE world_clock SET time_scale=1,next_adjudication_world_time=? WHERE world_id=?",
            (to_iso(at + timedelta(hours=12)), wid),
        )
        pid = ScienceService.submit(
            c, sid, actors[0], craft_program(assets["actors"][actors[0]]), at
        )
    engine = WorldEngine(db, settings)
    try:
        engine.heartbeat(wid, elapsed_seconds=3600)
        engine.heartbeat(wid, elapsed_seconds=1800)
        with db.read() as c:
            row = c.execute("SELECT * FROM science_programs WHERE id=?", (pid,)).fetchone()
            assert row["status"] == "completed", row["error"]
            assert c.execute(
                "SELECT 1 FROM character_state_updates WHERE character_id=?", (actors[0],)
            ).fetchone()
            assert not c.execute("SELECT 1 FROM science_model_calls").fetchone()
    finally:
        engine.close()


def test_science_enabled_disables_unbudgeted_models(science_lab, settings, monkeypatch):
    from test_science_runner import Backend

    from world_engine.engine import WorldEngine

    db, wid, sid, _, _, at = science_lab
    backends = []

    def build(config):
        if not config.deepseek_api_key:
            return None
        b = Backend()
        backends.append(b)
        return b

    monkeypatch.setattr("world_engine.engine.build_agent_model_backend", build)
    config = replace(
        settings,
        science_api_enabled=True,
        deepseek_api_key="offline",
        world_agent_enabled=True,
        research_api_enabled=True,
        collective_api_enabled=True,
    )
    engine = WorldEngine(db, config)
    try:
        assert engine.agent_model_backend is None
        assert engine.research_runner.backend is None
        assert engine.collective_runner.backend is None
        assert len(backends) == 1
        with db.write() as c:
            c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))
        engine.heartbeat(wid, elapsed_seconds=0)
        assert sum(len(b.calls) for b in backends) == 1
        with db.read() as c:
            assert c.execute("SELECT COUNT(*) FROM science_model_calls").fetchone()[0] == 1
    finally:
        engine.close()


def test_science_heartbeat_rejects_skipped_boundaries(science_lab, settings):
    from world_engine.engine import WorldEngine
    from world_engine.repository import to_iso

    db, wid, _, _, _, at = science_lab
    with db.write() as c:
        c.execute(
            "UPDATE world_clock SET time_scale=1,next_adjudication_world_time=? WHERE world_id=?",
            (to_iso(at + timedelta(hours=12)), wid),
        )
    engine = WorldEngine(db, settings)
    try:
        with pytest.raises(ValueError, match="边界"):
            engine.heartbeat(wid, elapsed_seconds=7200)
    finally:
        engine.close()


def test_stopping_finishes_existing_program_and_rejects_new_actions(science_lab):
    from science_fixtures import advance
    from test_science_programs import craft_program
    from test_science_runner import Backend

    from world_engine.science_programs import ScienceService
    from world_engine.science_runner import ScienceRunner

    db, wid, sid, actors, assets, at = science_lab
    backend = Backend()
    runner = ScienceRunner(db, backend, session_id=sid)
    with db.write() as c:
        c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))
        pid = ScienceService.submit(
            c, sid, actors[0], craft_program(assets["actors"][actors[0]]), at
        )
    runner.stop("离线模拟额度停止", at)
    assert not runner.decide(wid, at)
    assert not backend.calls
    with db.write() as c:
        assert (
            c.execute("SELECT status FROM science_sessions WHERE id=?", (sid,)).fetchone()[0]
            == "stopping"
        )
        with pytest.raises(ValueError, match="停止"):
            ScienceService.submit(c, sid, actors[1], {"reason": "新计划", "commands": []}, at)
        advance(c, wid, at + timedelta(hours=1))
        advance(c, wid, at + timedelta(hours=1.5))
        assert (
            c.execute("SELECT status FROM science_programs WHERE id=?", (pid,)).fetchone()[0]
            == "completed"
        )
        assert (
            c.execute("SELECT status FROM science_sessions WHERE id=?", (sid,)).fetchone()[0]
            == "stopped"
        )


def test_science_cannot_inject_paid_decision_provider(science_lab, settings):
    from world_engine.decisions import RuleDecisionProvider
    from world_engine.engine import WorldEngine

    db, _, _, _, _, _ = science_lab
    config = replace(settings, science_api_enabled=True)
    arbitrary_provider = object()
    engine = WorldEngine(db, config, decision_provider=arbitrary_provider)
    try:
        assert isinstance(engine.decision_provider, RuleDecisionProvider)
    finally:
        engine.close()
