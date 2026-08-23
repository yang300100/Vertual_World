from __future__ import annotations

from datetime import UTC, datetime, timedelta

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
        after.character_by_id(item.id).hunger == item.hunger + 1
        for item in before.characters
    )
    assert all(
        after.character_by_id(item.id).energy == item.energy
        for item in before.characters
    )
    assert len(updates) == 3


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
