from __future__ import annotations

import pytest

from world_engine.database import Database
from world_engine.decisions import RuleDecisionProvider
from world_engine.engine import ConcurrentWorldUpdateError, WorldEngine
from world_engine.repository import WorldRepository, to_iso, utc_now


def _create_demo_world(database: Database, minutes_per_tick: int = 60) -> str:
    repository = WorldRepository()
    with database.write() as connection:
        return repository.create_world(
            connection,
            name="测试世界",
            minutes_per_tick=minutes_per_tick,
            seed_demo=True,
        )


def test_tick_adjudicates_without_advancing_time_and_persists_events(
    database, settings
) -> None:
    world_id = _create_demo_world(database)
    repository = WorldRepository()
    with database.read() as connection:
        before = repository.get_snapshot(connection, world_id)

    result = WorldEngine(database, settings).tick(world_id)

    with database.read() as connection:
        after = repository.get_snapshot(connection, world_id)
        events = repository.list_events(connection, world_id)

    assert result.current_time == before.world.current_time
    assert after.world.version == 1
    assert after.world.tick_count == 1
    assert len(result.outcomes) == 2
    assert all(item.event_id for item in result.outcomes)
    assert result.trigger == "manual"
    assert any(item["event_type"] == "world.adjudication" for item in events)


def test_actions_change_character_state_and_create_subjective_memory(database, settings) -> None:
    world_id = _create_demo_world(database)
    repository = WorldRepository()
    with database.read() as connection:
        before = repository.get_snapshot(connection, world_id)
        lin_before = next(item for item in before.characters if item.name == "林澈")

    WorldEngine(database, settings).tick(world_id)

    with database.read() as connection:
        after = repository.get_snapshot(connection, world_id)
        lin_after = next(item for item in after.characters if item.name == "林澈")
        memories = repository.list_memories(connection, world_id, lin_after.id)

    assert lin_after.money == lin_before.money - 3
    assert lin_after.satiety > lin_before.satiety
    assert memories
    assert memories[0]["memory_type"] == "experienced"


def test_database_state_survives_new_database_instance(database, settings) -> None:
    world_id = _create_demo_world(database)
    WorldEngine(database, settings).tick(world_id)

    reopened = Database(settings.database_path)
    reopened.initialize()
    with reopened.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)

    assert snapshot.world.version == 1
    assert snapshot.world.tick_count == 1


def test_concurrent_version_change_is_not_recorded_as_world_failure(database, settings) -> None:
    world_id = _create_demo_world(database)

    class VersionMutatingProvider:
        name = "test-version-mutator"

        def propose(self, snapshot, characters):
            with database.write() as connection:
                connection.execute(
                    "UPDATE worlds SET version = version + 1, updated_at = ? WHERE id = ?",
                    (to_iso(utc_now()), world_id),
                )
            return RuleDecisionProvider().propose(snapshot, characters)

    engine = WorldEngine(database, settings, decision_provider=VersionMutatingProvider())
    with pytest.raises(ConcurrentWorldUpdateError):
        engine.tick(world_id)

    with database.read() as connection:
        runtime = connection.execute(
            "SELECT last_tick_status FROM world_runtime WHERE world_id = ?", (world_id,)
        ).fetchone()
    assert runtime["last_tick_status"] == "never"
