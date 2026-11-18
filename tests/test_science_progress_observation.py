"""哥哥，轻量进度只供观察，不能替代动作与费用的冻结核验。"""

import sqlite3

import pytest

from world_engine.science_freeze import FrozenScienceDatabase, freeze_session, state_digest
from world_engine.science_runner import ScienceRunner

pytest_plugins = ["science_fixtures"]


def test_progress_snapshot_is_read_only_and_matches_guarded_usage(science_lab, monkeypatch):
    from scripts.demo_science import observe_progress

    db, wid, sid, _, _, at = science_lab
    runner = ScienceRunner(db, None, session_id=sid)
    with db.write() as c:
        freeze_session(c, sid, source_paths=(), world_paths=())
        before = state_digest(c)
    guarded = FrozenScienceDatabase(db.path, session_id=sid)
    runner.database = guarded
    expected = runner.usage()

    def forbid_guarded_read(*args, **kwargs):
        pytest.fail("普通进度不应发起整库冻结读取")

    monkeypatch.setattr(guarded, "read", forbid_guarded_read)
    original_usage = runner._usage

    def check_transaction(c):
        assert c.in_transaction
        assert c.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))
        return original_usage(c)

    monkeypatch.setattr(runner, "_usage", check_transaction)
    progress = observe_progress(guarded.path, runner, wid)
    assert progress["world_time"] == at
    assert progress["usage"] == expected
    assert progress["status"] == "prepared"
    assert progress["next_boundary"] > at
    assert "freeze_intact" not in progress
    with db.read() as c:
        assert state_digest(c) == before


def test_progress_cannot_hide_tampering_before_next_action(science_lab):
    from scripts.demo_science import observe_progress

    db, wid, sid, actors, _, _ = science_lab
    runner = ScienceRunner(db, None, session_id=sid)
    with db.write() as c:
        freeze_session(c, sid, source_paths=(), world_paths=())
    guarded = FrozenScienceDatabase(db.path, session_id=sid)
    with db.write() as c:
        c.execute("UPDATE characters SET goals_json='[\"外部修改目标\"]' WHERE id=?", (actors[0],))
    observe_progress(guarded.path, runner, wid)
    with pytest.raises(RuntimeError, match="冻结执行"):
        with guarded.write():
            pytest.fail("外部修改后不能进入动作或费用事务")


def test_progress_missing_database_does_not_create_file(tmp_path):
    from scripts.demo_science import observe_progress

    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        observe_progress(path, None, "missing")
    assert not path.exists()


def test_progress_rejects_world_mismatch(science_lab):
    from scripts.demo_science import observe_progress

    db, _, sid, _, _, _ = science_lab
    runner = ScienceRunner(db, None, session_id=sid)
    with pytest.raises(ValueError, match="跨世界"):
        observe_progress(db.path, runner, "other-world")
