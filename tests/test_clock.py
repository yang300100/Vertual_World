from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from world_engine.database import Database
from world_engine.engine import WorldEngine
from world_engine.repository import WorldRepository, to_iso
from world_engine.worker import WorldWorker


def _create_world(database) -> str:
    with database.write() as connection:
        return WorldRepository().create_world(
            connection,
            name="时钟测试世界",
            minutes_per_tick=60,
            seed_demo=True,
        )


def test_one_to_one_heartbeat_advances_one_world_minute(database, settings) -> None:
    world_id = _create_world(database)
    engine = WorldEngine(database, settings)

    result = engine.heartbeat(world_id, elapsed_seconds=60)

    assert result.time_scale == 1.0
    assert result.world_delta_seconds == 60
    assert result.current_time - result.previous_time == timedelta(minutes=1)
    assert result.adjudication is None
    assert result.state_update_count == 0


def test_fractional_state_changes_accumulate_without_rounding_loss(
    database, settings
) -> None:
    world_id = _create_world(database)
    repository = WorldRepository()
    with database.read() as connection:
        before = repository.get_snapshot(connection, world_id)

    result = WorldEngine(database, settings).heartbeat(
        world_id, elapsed_seconds=20 * 60
    )

    with database.read() as connection:
        after = repository.get_snapshot(connection, world_id)
        updates = repository.list_state_updates_ascending(connection, world_id)
    assert result.current_time - result.previous_time == timedelta(minutes=20)
    assert all(
        after.character_by_id(item.id).satiety == item.satiety - 1
        for item in before.characters
    )
    assert all(
        after.character_by_id(item.id).energy == item.energy
        for item in before.characters
    )
    assert len(updates) == 3


def test_energy_recovers_gradually_with_world_time(database, settings) -> None:
    world_id = _create_world(database)
    repository = WorldRepository()
    with database.read() as connection:
        before = repository.get_snapshot(connection, world_id)

    WorldEngine(database, settings).heartbeat(world_id, elapsed_seconds=60 * 60)

    with database.read() as connection:
        after = repository.get_snapshot(connection, world_id)
        updates = repository.list_state_updates_ascending(connection, world_id)
    assert all(
        after.character_by_id(item.id).energy == min(100, item.energy + 2)
        for item in before.characters
    )
    assert all(item["cause"] == "natural_time_passage" for item in updates)


def test_time_scale_can_accelerate_and_pause_world(database, settings) -> None:
    world_id = _create_world(database)
    engine = WorldEngine(database, settings)

    accelerated = engine.set_time_scale(world_id, 60)
    heartbeat = engine.heartbeat(world_id, elapsed_seconds=60)
    paused = engine.set_time_scale(world_id, 0)
    paused_heartbeat = engine.heartbeat(world_id, elapsed_seconds=600)

    assert accelerated.old_time_scale == 1
    assert heartbeat.current_time - heartbeat.previous_time == timedelta(hours=1)
    assert heartbeat.characters_updated == 3
    assert paused.new_time_scale == 0
    assert paused_heartbeat.current_time == paused_heartbeat.previous_time


def test_worker_restart_resets_real_baseline_without_offline_catchup(
    database, settings
) -> None:
    world_id = _create_world(database)
    old_real_time = datetime(2026, 1, 1, tzinfo=UTC)
    resumed_at = datetime(2026, 8, 23, tzinfo=UTC)
    with database.write() as connection:
        connection.execute(
            "UPDATE world_clock SET last_heartbeat_real_time = ? WHERE world_id = ?",
            (to_iso(old_real_time), world_id),
        )
    engine = WorldEngine(database, settings)

    engine.reset_offline_baseline(resumed_at)
    result = engine.heartbeat(world_id, real_now=resumed_at + timedelta(minutes=1))

    assert result.real_elapsed_seconds == 60
    assert result.current_time - result.previous_time == timedelta(minutes=1)


def test_crossing_fixed_twelve_hour_boundary_runs_one_scheduled_adjudication(
    database, settings
) -> None:
    world_id = _create_world(database)
    engine = WorldEngine(database, settings)

    result = engine.heartbeat(world_id, elapsed_seconds=4 * 3600)

    with database.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)
        runs = WorldRepository().list_adjudication_runs(connection, world_id)
    assert result.current_time.hour == 12
    assert result.adjudication is not None
    assert result.adjudication.trigger == "scheduled_12h"
    assert len(runs) == 1
    assert snapshot.world.last_adjudication_time == result.current_time
    assert snapshot.world.next_adjudication_time == datetime(
        2040, 4, 2, 0, 0, tzinfo=UTC
    )


def test_crossing_multiple_boundaries_merges_into_one_adjudication(
    database, settings
) -> None:
    world_id = _create_world(database)
    engine = WorldEngine(database, settings)

    result = engine.heartbeat(world_id, elapsed_seconds=30 * 3600)

    with database.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)
        runs = WorldRepository().list_adjudication_runs(connection, world_id)
    assert result.current_time == datetime(2040, 4, 2, 14, 0, tzinfo=UTC)
    assert len(runs) == 1
    assert snapshot.world.next_adjudication_time == datetime(
        2040, 4, 3, 0, 0, tzinfo=UTC
    )


def test_player_intervention_does_not_reset_global_schedule(database, settings) -> None:
    world_id = _create_world(database)
    repository = WorldRepository()
    with database.read() as connection:
        before = repository.get_snapshot(connection, world_id)
    actor_id = before.characters[0].id

    result = WorldEngine(database, settings).adjudicate(
        world_id,
        trigger="player_intervention",
        character_ids=[actor_id],
    )

    with database.read() as connection:
        after = repository.get_snapshot(connection, world_id)
    assert result.trigger == "player_intervention"
    assert len(result.outcomes) == 1
    assert after.world.next_adjudication_time == before.world.next_adjudication_time


def test_worker_zero_elapsed_probe_does_not_advance_world(database, settings) -> None:
    world_id = _create_world(database)
    repository = WorldRepository()
    with database.read() as connection:
        before = repository.get_snapshot(connection, world_id)
    worker = WorldWorker(settings)

    completed = worker.run_once(elapsed_seconds=0)
    worker.engine.close()

    with database.read() as connection:
        after = repository.get_snapshot(connection, world_id)
    assert completed == 1
    assert after.world.current_time == before.world.current_time


def test_worker_presence_is_exposed_and_can_be_cleared(database, settings) -> None:
    world_id = _create_world(database)
    seen_at = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
    engine = WorldEngine(database, settings)

    engine.mark_worker_seen(seen_at)
    with database.read() as connection:
        online = WorldRepository().get_snapshot(connection, world_id)
    engine.clear_worker_seen()
    with database.read() as connection:
        offline = WorldRepository().get_snapshot(connection, world_id)

    assert online.world.last_worker_seen_at == seen_at
    assert online.world.heartbeat_interval_seconds == 60
    assert offline.world.last_worker_seen_at is None


def test_time_scale_change_is_one_atomic_world_version(database, settings) -> None:
    world_id = _create_world(database)
    baseline = datetime(2026, 8, 23, 10, 0, tzinfo=UTC)
    engine = WorldEngine(database, settings)
    engine.reset_offline_baseline(baseline)
    repository = WorldRepository()
    with database.read() as connection:
        before = repository.get_snapshot(connection, world_id)

    changed = engine.set_time_scale(
        world_id,
        2.0,
        operator="test",
        real_now=baseline + timedelta(seconds=60),
    )
    with database.read() as connection:
        after_change = repository.get_snapshot(connection, world_id)
        event_count = connection.execute(
            "SELECT COUNT(*) FROM world_events WHERE world_id = ?", (world_id,)
        ).fetchone()[0]
        heartbeat_count = connection.execute(
            "SELECT COUNT(*) FROM world_heartbeats WHERE world_id = ?", (world_id,)
        ).fetchone()[0]

    unchanged = engine.set_time_scale(
        world_id,
        2.0,
        operator="test",
        real_now=baseline + timedelta(seconds=120),
    )
    with database.read() as connection:
        after_no_op = repository.get_snapshot(connection, world_id)
        final_event_count = connection.execute(
            "SELECT COUNT(*) FROM world_events WHERE world_id = ?", (world_id,)
        ).fetchone()[0]

    assert changed.world_version == before.world.version + 1
    assert after_change.world.version == before.world.version + 1
    assert after_change.world.clock_revision == before.world.clock_revision + 1
    assert changed.world_time - changed.previous_world_time == timedelta(seconds=60)
    assert heartbeat_count == 1
    assert event_count == 1
    assert unchanged.no_op is True
    assert unchanged.event_id is None
    assert after_no_op.world.version == after_change.world.version
    assert final_event_count == event_count


def test_legacy_hunger_schema_migrates_to_positive_satiety(tmp_path: Path) -> None:
    path = tmp_path / "legacy-hunger.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys = OFF;
        CREATE TABLE worlds (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, current_time TEXT NOT NULL,
            minutes_per_tick INTEGER NOT NULL, status TEXT NOT NULL,
            version INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE world_runtime (
            world_id TEXT PRIMARY KEY, tick_count INTEGER NOT NULL,
            last_tick_started_at TEXT, last_tick_finished_at TEXT, last_tick_status TEXT
        );
        CREATE TABLE locations (
            id TEXT PRIMARY KEY, world_id TEXT NOT NULL, name TEXT NOT NULL,
            kind TEXT NOT NULL, resources_json TEXT NOT NULL
        );
        CREATE TABLE characters (
            id TEXT PRIMARY KEY, world_id TEXT NOT NULL, name TEXT NOT NULL,
            location_id TEXT NOT NULL, energy INTEGER NOT NULL,
            hunger INTEGER NOT NULL, money INTEGER NOT NULL,
            traits_json TEXT NOT NULL, goals_json TEXT NOT NULL,
            is_core INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE character_state_accumulators (
            character_id TEXT PRIMARY KEY, world_id TEXT NOT NULL,
            hunger_residual REAL NOT NULL, energy_residual REAL NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO worlds VALUES (
            'world-1', 'legacy', '2040-04-01T08:00:00+00:00', 60,
            'running', 0, '2026-08-23T00:00:00+00:00', '2026-08-23T00:00:00+00:00'
        );
        INSERT INTO world_runtime VALUES ('world-1', 0, NULL, NULL, 'never');
        INSERT INTO locations VALUES ('place-1', 'world-1', 'home', 'home', '{}');
        INSERT INTO characters VALUES (
            'npc-1', 'world-1', 'legacy npc', 'place-1', 80, 75, 10,
            '[]', '[]', 1, '2026-08-23T00:00:00+00:00', '2026-08-23T00:00:00+00:00'
        );
        INSERT INTO character_state_accumulators VALUES (
            'npc-1', 'world-1', 0.6, 0.2, '2026-08-23T00:00:00+00:00'
        );
        """
    )
    connection.commit()
    connection.close()

    database = Database(path)
    database.initialize()
    with database.read() as migrated:
        character_columns = {
            row["name"] for row in migrated.execute("PRAGMA table_info(characters)")
        }
        accumulator_columns = {
            row["name"]
            for row in migrated.execute(
                "PRAGMA table_info(character_state_accumulators)"
            )
        }
        character = migrated.execute(
            "SELECT satiety FROM characters WHERE id = 'npc-1'"
        ).fetchone()
        accumulator = migrated.execute(
            """
            SELECT satiety_residual
            FROM character_state_accumulators WHERE character_id = 'npc-1'
            """
        ).fetchone()

    assert "hunger" not in character_columns
    assert "satiety" in character_columns
    assert character["satiety"] == 25
    assert "hunger_residual" not in accumulator_columns
    assert accumulator["satiety_residual"] == 0.6
