from __future__ import annotations

from world_engine.domain import ActionType
from world_engine.engine import WorldEngine
from world_engine.repository import WorldRepository
from world_engine.seeder import create_iserra_world


def _setup(database, settings):
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.read() as connection:
        snap = repo.get_snapshot(connection, world_id)
        loc = next(item for item in snap.locations if item.name == "河务档案区")
    with database.write() as connection:
        repo.create_player_character(
            connection, world_id=world_id, name="旅人", identity="学者",
            location_id=loc.id, traits=["谨慎"], goal="记录伊瑟拉",
        )
    return world_id


def test_attack_is_recognized_and_settles(database, settings) -> None:
    """“打北潭·漱泉”应转成 attack 动作并落一次 action.attack 事件。"""
    world_id = _setup(database, settings)
    engine = WorldEngine(database, settings)
    result = engine.submit_player_intent(world_id, "打北潭·漱泉")
    assert result.outcome.action is ActionType.ATTACK
    with database.read() as connection:
        events = WorldRepository().list_events(connection, world_id, limit=20)
    assert any(e["event_type"] == "action.attack" for e in events)


def test_attack_important_death_is_major_event(database, settings) -> None:
    """重要 NPC 被打倒应记入重大历史事件(world.major_death)。"""
    world_id = _setup(database, settings)
    repo = WorldRepository()
    with database.write() as connection:
        connection.execute(
            "UPDATE characters SET health = 12 WHERE world_id = ? AND name = ?",
            (world_id, "北潭·漱泉"),
        )
    engine = WorldEngine(database, settings)
    engine.submit_player_intent(world_id, "打北潭·漱泉")
    with database.read() as connection:
        events = repo.list_events(connection, world_id, limit=20)
        health = connection.execute(
            "SELECT health FROM characters WHERE world_id=? AND name=?",
            (world_id, "北潭·漱泉"),
        ).fetchone()["health"]
    assert health <= 0
    assert any(e["event_type"] == "world.major_death" for e in events)


def test_attack_in_public_triggers_warden(database, settings) -> None:
    """在公共地点公开攻击应触发守卫/河务介入事件。"""
    world_id = _setup(database, settings)  # 河务档案区是 public
    engine = WorldEngine(database, settings)
    engine.submit_player_intent(world_id, "打北潭·漱泉")
    with database.read() as connection:
        events = WorldRepository().list_events(connection, world_id, limit=20)
    assert any(e["event_type"] == "world.warden_intervention" for e in events)
