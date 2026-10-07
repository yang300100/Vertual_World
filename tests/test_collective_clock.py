"""哥哥，加速只减少现实等待，不能跳过世界中的完成、送达和摄入边界。"""

import importlib.util
import json
from datetime import timedelta

import pytest
from test_collective_research import experiment, lab  # noqa: F401

from world_engine.repository import to_iso


def clock_module():
    assert importlib.util.find_spec("world_engine.collective_clock"), "尚无真实事件边界调度"
    from world_engine import collective_clock

    return collective_clock


def test_boundary_includes_single_rune_research_post(lab):
    db, wid, actors, _, _, at = lab
    from world_engine.repository import to_iso
    from world_engine.autonomous_research import ResearchService

    with db.write() as c:
        net = ResearchService.create_network(c, wid, '边界实验网络', at)
        location = c.execute('SELECT location_id FROM characters WHERE id=?', (actors[0],)).fetchone()[0]
        from uuid import uuid4
        from world_engine.event_log import record_event

        event = record_event(c, world_id=wid, tick_id='offline', occurred_at=at, event_type='research.test', summary='离线消息边界')
        due = at + timedelta(minutes=10)
        c.execute('INSERT INTO research_messages(id,network_id,sender_id,recipient_id,body,evidence_json,sent_at,due_at,destination_id,sent_event_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
                  (str(uuid4()),net,actors[0],actors[1],'本人实验结果','[]',to_iso(at),to_iso(due),location,event))
        assert clock_module().next_boundary(c, wid, at) == due


def test_boundary_splits_actual_short_journey_arrival(lab):
    db, wid, actors, _, _, at = lab
    from world_engine.movement import MovementService

    with db.write() as c:
        MovementService().start(
            c,
            world_id=wid,
            character_id=actors[0],
            destination_longitude=0.01,
            destination_latitude=0,
            world_time=at,
        )
        arrival = c.execute(
            "SELECT estimated_arrival_world FROM character_movements WHERE character_id=? AND status='moving'",
            (actors[0],),
        ).fetchone()[0]
        from world_engine.repository import from_iso

        assert at < from_iso(arrival) < at + timedelta(hours=1)
        assert clock_module().next_boundary(c, wid, at) == from_iso(arrival)


def test_inert_age_progress_is_batched_without_losing_real_material_or_chemical_changes(lab):
    module = clock_module()
    db, wid, actors, batches, _, at = lab
    from world_engine.materials import MaterialService as M

    with db.write() as c:
        module.advance_materials(c, wid, 1)
        assert M.state(c, batches[0]).age_hours == 1
        assert M.state(c, batches[0]).mobile == 20
        assert (
            c.execute(
                "SELECT COUNT(*) FROM material_clock_steps WHERE world_id=?", (wid,)
            ).fetchone()[0]
            == 1
        )
        module.advance_materials(c, wid, 1)
        assert M.state(c, batches[0]).age_hours == 2
        assert M.audit(c, wid)["balanced"]


def test_age_only_journal_cannot_hide_a_salt_conversion(lab):
    db, _, _, batches, _, _ = lab
    from dataclasses import replace

    from world_engine.materials import MaterialService as M

    with db.write() as c:
        state = M.state(c, batches[0])
        with pytest.raises(ValueError, match="年龄"):
            M.rebalance(
                c,
                {batches[0]: replace(state, mobile=state.mobile - 1, adsorbed=state.adsorbed + 1)},
                operation="natural_age",
            )


def test_boundary_splits_early_message_and_never_exceeds_one_hour(lab):
    module = clock_module()
    db, wid, actors, _, _, at = lab
    from world_engine.collective_messages import MessageService

    with db.write() as c:
        c.execute("INSERT INTO collective_contacts VALUES (?,?,.25)", (actors[0], actors[1]))
        MessageService.send(c, actors[0], actors[1], at, text="本人实际传递的信息")
        assert module.next_boundary(c, wid, at) == at + timedelta(minutes=15)
        assert module.next_boundary(c, wid, at + timedelta(hours=1)) == at + timedelta(hours=2)


def test_collective_clock_rejects_skipping_a_prior_event(lab):
    clock_module()
    db, wid, actors, _, _, at = lab
    from world_engine.clock import WorldClockService
    from world_engine.collective_messages import MessageService
    from world_engine.config import Settings

    with db.write() as c:
        c.execute("INSERT INTO material_modes VALUES (?,?)", (wid, '{"mode":"collective"}'))
        c.execute("INSERT INTO collective_contacts VALUES (?,?,.5)", (actors[0], actors[1]))
        MessageService.send(c, actors[0], actors[1], at, text="确实会先到达")
    with pytest.raises(ValueError, match="边界|心跳"):
        WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=3600)
    with db.read() as c:
        assert c.execute("SELECT w.current_time FROM worlds w WHERE id=?", (wid,)).fetchone()[
            0
        ] == to_iso(at)


def test_real_heartbeat_completes_experiment_without_manual_settlement(lab):
    clock_module()
    db, wid, actors, batches, tool, at = lab
    from world_engine.clock import WorldClockService
    from world_engine.collective_research import CollectiveResearchService as R
    from world_engine.config import Settings

    with db.write() as c:
        c.execute("INSERT INTO material_modes VALUES (?,?)", (wid, '{"mode":"collective"}'))
        R.submit(c, actors[0], experiment(batches[0], tool), at)
    WorldClockService(db, Settings(database_path=db.path)).heartbeat(wid, elapsed_seconds=3600)
    with db.read() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM collective_observations WHERE kind='measurement'"
            ).fetchone()[0]
            == 1
        )
        assert c.execute("SELECT status FROM collective_programs").fetchone()[0] == "completed"


def test_fine_and_accelerated_steps_preserve_delayed_delivery_and_material(lab):
    module = clock_module()
    db, wid, actors, batches, _, at = lab
    import sqlite3

    from world_engine.clock import WorldClockService
    from world_engine.collective_messages import MessageService
    from world_engine.config import Settings
    from world_engine.database import Database
    from world_engine.material_physics import MaterialState
    from world_engine.materials import MaterialService as M

    with db.write() as c:
        c.execute("INSERT INTO material_modes VALUES (?,?)", (wid, '{"mode":"collective"}'))
        M.rebalance(
            c,
            {
                batches[0]: MaterialState(
                    mass=100, nutrition=2000, mobile=10, adsorbed=10, particles=1, water=0
                )
            },
            operation="salt_equilibrium",
        )
        c.execute("INSERT INTO collective_contacts VALUES (?,?,.5)", (actors[0], actors[1]))
        MessageService.send(c, actors[0], actors[1], at, text="实际报告")
    paths = [db.path.parent / "fine.db", db.path.parent / "accelerated.db"]
    for path in paths:
        source = sqlite3.connect(db.path)
        target = sqlite3.connect(path)
        source.backup(target)
        target.close()
        source.close()
    results = []
    for path, step in zip(paths, (0.25, 1)):
        replica = Database(path)
        clock = WorldClockService(replica, Settings(database_path=path))
        current = at
        while current < at + timedelta(hours=2):
            with replica.read() as c:
                boundary = min(
                    module.next_boundary(c, wid, current, limit_hours=step), at + timedelta(hours=2)
                )
            clock.heartbeat(wid, elapsed_seconds=(boundary - current).total_seconds())
            current = boundary
        with replica.read() as c:
            results.append(
                (
                    M.state(c, batches[0]),
                    c.execute("SELECT delivered_at FROM collective_messages").fetchone()[0],
                    c.execute(
                        "SELECT satiety,energy FROM characters WHERE id=?", (actors[0],)
                    ).fetchone()[:],
                )
            )
    assert results[0] == results[1]


def test_material_ledger_and_ingestion_use_world_time_not_sql_clock(lab):
    db, wid, actors, batches, _, at = lab
    from world_engine.materials import MaterialService as M

    with db.write() as c:
        timestamp = c.execute(
            "SELECT world_time FROM material_ledger WHERE world_id=?", (wid,)
        ).fetchone()[0]
        assert timestamp == to_iso(at)
        item = c.execute("SELECT * FROM item_instances WHERE id=?", (batches[0],)).fetchone()
        M.consume(c, item, 1, "body:" + actors[0])
        assert c.execute(
            "SELECT world_time FROM material_sinks WHERE world_id=?", (wid,)
        ).fetchone()[0] == to_iso(at)
