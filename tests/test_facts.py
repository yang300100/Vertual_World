"""facts 模块的单元测试。"""

from __future__ import annotations

import pytest

from world_engine import facts
from world_engine.repository import WorldRepository, utc_now


@pytest.fixture
def world(database):
    """建一个带 demo 人物的世界。

    不要手写 INSERT INTO characters —— characters 表的 location_id、energy、
    satiety、money 都是无默认值的 NOT NULL 列，且 location_id 是外键。
    统一用 repository.create_world(seed_demo=True)，它建出林澈、瑞恩、白露三人。
    """
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection, name="测试世界", minutes_per_tick=60, seed_demo=True
        )
        snapshot = repository.get_snapshot(connection, world_id)
    return world_id, {character.name: character.id for character in snapshot.characters}


@pytest.fixture
def connection(database, world):
    with database.write() as conn:
        yield conn


def test_record_event_returns_id_and_persists_row(connection, world):
    world_id, ids = world
    event_id = facts.record_event(
        connection,
        world_id=world_id,
        tick_id="t1",
        occurred_at=utc_now(),
        event_type="action.idle",
        actor_id=ids["林澈"],
        target_id=None,
        location_id=None,
        summary="林澈发呆。",
        payload={},
    )
    row = connection.execute(
        "SELECT summary, event_type, importance FROM world_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    assert row["summary"] == "林澈发呆。"
    assert row["event_type"] == "action.idle"
    assert row["importance"] == "routine"


def test_record_event_marks_major_death_as_major(connection, world):
    world_id, ids = world
    event_id = facts.record_event(
        connection,
        world_id=world_id,
        tick_id="t1",
        occurred_at=utc_now(),
        event_type="world.major_death",
        actor_id=ids["林澈"],
        target_id=None,
        location_id=None,
        summary="重大死亡。",
        payload={},
    )
    row = connection.execute(
        "SELECT importance FROM world_events WHERE id = ?", (event_id,)
    ).fetchone()
    assert row["importance"] == "major"


def test_record_memory_persists_row(connection, world):
    world_id, ids = world
    event_id = facts.record_event(
        connection,
        world_id=world_id,
        tick_id="t1",
        occurred_at=utc_now(),
        event_type="action.idle",
        actor_id=ids["林澈"],
        target_id=None,
        location_id=None,
        summary="林澈发呆。",
        payload={},
    )
    facts.record_memory(
        connection,
        world_id=world_id,
        character_id=ids["林澈"],
        event_id=event_id,
        memory_type="experienced",
        summary="林澈记得自己发过呆。",
        importance=2,
    )
    row = connection.execute(
        "SELECT summary, confidence FROM character_memories WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    assert row["summary"] == "林澈记得自己发过呆。"
    assert row["confidence"] == 1.0


def test_update_character_applies_changes(connection, world):
    _, ids = world
    facts.update_character(connection, ids["林澈"], satiety=42)
    row = connection.execute(
        "SELECT satiety FROM characters WHERE id = ?", (ids["林澈"],)
    ).fetchone()
    assert row["satiety"] == 42


def test_update_character_without_changes_is_noop(connection, world):
    _, ids = world
    before = connection.execute(
        "SELECT satiety FROM characters WHERE id = ?", (ids["林澈"],)
    ).fetchone()["satiety"]
    facts.update_character(connection, ids["林澈"])
    after = connection.execute(
        "SELECT satiety FROM characters WHERE id = ?", (ids["林澈"],)
    ).fetchone()["satiety"]
    assert after == before


def test_change_relationship_accumulates(connection, world):
    world_id, ids = world
    facts.change_relationship(connection, world_id, ids["林澈"], ids["瑞恩"], 10, 5)
    facts.change_relationship(connection, world_id, ids["林澈"], ids["瑞恩"], 3, 2)
    row = connection.execute(
        "SELECT affinity, trust FROM relationships "
        "WHERE source_character_id = ? AND target_character_id = ?",
        (ids["林澈"], ids["瑞恩"]),
    ).fetchone()
    assert (row["affinity"], row["trust"]) == (13, 7)


def test_actor_exists_reports_presence(connection, world):
    _, ids = world
    assert facts.actor_exists(connection, ids["林澈"]) is True
    assert facts.actor_exists(connection, "nobody") is False


def test_get_actor_raises_for_missing(connection, world):
    from world_engine.actions import ActionRuleError

    world_id, _ = world
    with pytest.raises(ActionRuleError):
        facts.get_actor(connection, world_id, "nobody")


def test_importance_matches_legacy_table():
    from world_engine.domain import ActionType

    assert facts.importance(ActionType.ATTACK) == 8
    assert facts.importance(ActionType.IDLE) == 1
