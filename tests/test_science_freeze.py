"""哥哥，副本包含WAL提交，冻结后只有正常执行事务能推进状态。"""

import importlib
import json
import sqlite3
from datetime import timedelta

import pytest

pytest_plugins = ["science_fixtures"]


def freeze():
    return importlib.import_module("world_engine.science_freeze")


def test_backup_includes_wal(tmp_path):
    source, target = tmp_path / "source.db", tmp_path / "copy.db"
    with sqlite3.connect(source) as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("CREATE TABLE marker(value TEXT)")
        c.execute("INSERT INTO marker VALUES ('已提交')")
        c.commit()
        freeze().backup_world(source, target)
    with sqlite3.connect(target) as c:
        assert c.execute("SELECT value FROM marker").fetchone()[0] == "已提交"


def test_backup_path_protection(tmp_path):
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as c:
        c.execute("CREATE TABLE marker(value)")
    for destination in (source, tmp_path / "other.db"):
        if destination != source:
            destination.touch()
        with pytest.raises(ValueError):
            freeze().backup_world(source, destination)


@pytest.fixture
def frozen(science_lab, tmp_path):
    db, wid, sid, actors, assets, at = science_lab
    code, lore = tmp_path / "code.py", tmp_path / "lore.md"
    code.write_text("neutral code", encoding="utf-8")
    lore.write_text("neutral lore", encoding="utf-8")
    with db.write() as c:
        manifest = freeze().freeze_session(c, sid, source_paths=(code,), world_paths=(lore,))
        assert manifest["initial"]["technologies"] == 0
    return db, wid, sid, actors, assets, at, code, lore


@pytest.mark.parametrize("what", ["code", "lore", "inventory", "identity", "config"])
def test_external_change_detected_and_stops(frozen, what):
    db, _, sid, actors, assets, _, code, lore = frozen
    if what in {"code", "lore"}:
        (code if what == "code" else lore).write_text("external edit", encoding="utf-8")
    else:
        with db.write() as c:
            if what == "inventory":
                with pytest.raises(sqlite3.IntegrityError):
                    c.execute(
                        "UPDATE item_instances SET quantity=quantity+1 WHERE id=?",
                        (assets["actors"][actors[0]]["energy"],),
                    )
                c.execute(
                    "UPDATE item_instances SET condition=condition-1 WHERE id=?",
                    (assets["actors"][actors[0]]["energy"],),
                )
            elif what == "identity":
                c.execute(
                    "UPDATE characters SET goals_json='[\"人为安排\"]' WHERE id=?", (actors[0],)
                )
            else:
                c.execute("UPDATE world_clock SET time_scale=30")
    guarded = freeze().FrozenScienceDatabase(db.path, session_id=sid)
    with pytest.raises(RuntimeError, match="冻结|外部"):
        with guarded.write():
            pass
    with db.read() as c:
        row = c.execute("SELECT * FROM science_sessions WHERE id=?", (sid,)).fetchone()
        assert row["status"] == "stopped" and row["stop_reason"]


def test_legal_execution_consumption_and_restart_keeps_budget(frozen):
    from test_science_programs import craft_program
    from test_science_work import finish

    from world_engine.science_programs import ScienceService

    db, wid, sid, actors, assets, at, _, _ = frozen
    guarded = freeze().FrozenScienceDatabase(db.path, session_id=sid)
    with guarded.write() as c:
        c.execute("UPDATE science_sessions SET status='running' WHERE id=?", (sid,))
        pid = ScienceService.submit(
            c, sid, actors[0], craft_program(assets["actors"][actors[0]]), at
        )
    for offset in (1, 1.5):
        with guarded.write() as c:
            finish(c, wid, at + timedelta(hours=offset))
            ScienceService.tick(c, wid, at + timedelta(hours=offset))
    resumed = freeze().FrozenScienceDatabase(db.path, session_id=sid)
    with resumed.read() as c:
        assert (
            c.execute("SELECT status FROM science_programs WHERE id=?", (pid,)).fetchone()[0]
            == "completed"
        )
        assert c.execute(
            "SELECT max_calls,token_limit FROM science_sessions WHERE id=?", (sid,)
        ).fetchone()[:] == (12, 200000)


def test_refreeze_cannot_hide_change(frozen):
    db, _, sid, _, _, _, code, lore = frozen
    code.write_text("changed", encoding="utf-8")
    with db.write() as c, pytest.raises(ValueError, match="冻结"):
        freeze().freeze_session(c, sid, source_paths=(code,), world_paths=(lore,))


def test_export_is_read_only_and_complete(frozen, tmp_path):
    from world_engine.science_exports import export_science

    db, _, sid, _, _, _, _, _ = frozen
    with db.read() as c:
        before = freeze().state_digest(c)
    output = tmp_path / "exports"
    report = export_science(db, sid, output)
    assert report["goal_achieved"] is False
    for name in (
        "处理过程.md",
        "全部人物与通信.md",
        "科技图.json",
        "科技图.svg",
        "编年.md",
        "api-usage.json",
        "freeze.json",
        "最终验收.md",
    ):
        assert (output / name).exists()
    assert json.loads((output / "api-usage.json").read_text(encoding="utf-8"))["calls"] == 0
    with db.read() as c:
        assert freeze().state_digest(c) == before


def test_frozen_database_export_keeps_read_transaction(frozen, tmp_path):
    from world_engine.science_exports import export_science

    db, _, sid, _, _, _, _, _ = frozen
    guarded = freeze().FrozenScienceDatabase(db.path, session_id=sid)
    report = export_science(guarded, sid, tmp_path / "guarded-export")
    assert report["goal_achieved"] is False
    with guarded.read() as c:
        assert freeze().verify_freeze(c, sid)


def test_run_lock_rejects_live_process_and_recovers_after_crash(tmp_path):
    import subprocess
    import sys

    from scripts.demo_science import run_lock

    script = """
import sys
import os
from pathlib import Path
from scripts.demo_science import run_lock
with run_lock(Path(sys.argv[1])):
    print('locked',flush=True)
    if sys.stdin.readline().strip() == 'crash':
        os._exit(0)
"""
    lock = tmp_path / "process.lock"
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout.readline().strip() == "locked"
        with pytest.raises(ValueError, match="运行锁"):
            with run_lock(lock):
                pass
        process.stdin.write("crash\n")
        process.stdin.flush()
        process.wait(timeout=10)
        with run_lock(lock):
            assert lock.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_prepare_only_preserves_source_and_original_npcs(lab, settings, tmp_path):
    from scripts.demo_science import prepare, preserved_identity
    from world_engine.database import Database

    db, wid, _, _, _, source_at = lab
    with db.read() as c:
        source_state = freeze().state_digest(c)
        identity = preserved_identity(c, wid)
    output = tmp_path / "prepared-run"
    prepare(db.path, wid, output, settings, {})
    with db.read() as c:
        assert freeze().state_digest(c) == source_state
        assert preserved_identity(c, wid) == identity
    copied = Database(output / "world.db", legacy_world_defaults=False)
    with copied.read() as c:
        assert preserved_identity(c, wid) == identity
        assert not c.execute("SELECT 1 FROM science_model_calls").fetchone()
        assert not c.execute("SELECT 1 FROM technologies").fetchone()
        assert not c.execute("SELECT 1 FROM science_programs").fetchone()
        s = c.execute("SELECT * FROM science_sessions").fetchone()
        assert s["created_at"] == source_at.isoformat()
        assert freeze().verify_freeze(c, s["id"])
    with pytest.raises(ValueError):
        prepare(db.path, wid, output, settings, {})


def test_existing_accepted_acquaintances_keep_real_postal_channel(science_lab):
    from scripts.demo_science import known_postal_channels

    db, wid, _, actors, assets, at = science_lab
    with db.write() as c:
        from uuid import uuid4

        far = str(uuid4())
        c.execute(
            (
                "INSERT INTO locations(id,world_id,name,kind,longitude,latitude) VALUES "
                "(?,?,'远处城镇','city',1,0)"
            ),
            (far, wid),
        )
        c.execute(
            "UPDATE characters SET location_id=?,current_location_id=?,longitude=1 WHERE id=?",
            (far, far, actors[1]),
        )
        c.execute("DELETE FROM collective_contacts")
        c.execute("DELETE FROM relationships")
        c.execute(
            (
                "INSERT INTO "
                "character_contacts(id,world_id,requester_id,recipient_id,status,created_"
                "at,updated_at) VALUES ('known',?,?,?,'accepted',?,?)"
            ),
            (wid, actors[0], actors[1], at.isoformat(), at.isoformat()),
        )
        known_postal_channels(c, wid, assets)
        route = c.execute(
            "SELECT transit_hours FROM collective_contacts WHERE sender_id=? AND recipient_id=?",
            (actors[0], actors[1]),
        ).fetchone()
        assert route is not None and route[0] > 0
