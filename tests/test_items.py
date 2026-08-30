from __future__ import annotations

from world_engine.engine import WorldEngine
from world_engine.repository import WorldRepository
from world_engine.seeder import create_iserra_world


def _setup(database, settings):
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.read() as connection:
        snap = repo.get_snapshot(connection, world_id)
        loc = next(item for item in snap.locations if item.name == "澜誓城")
    with database.write() as connection:
        repo.create_player_character(
            connection, world_id=world_id, name="旅人", identity="学者",
            location_id=loc.id, traits=["谨慎"], goal="记录伊瑟拉",
        )
    return world_id


def test_player_has_initial_kit(database, settings) -> None:
    """玩家创建后自带"剑术"技能、疗伤药背包与铁剑装备。"""
    world_id = _setup(database, settings)
    repo = WorldRepository()
    with database.read() as connection:
        pov = next(
            c for c in repo.get_snapshot(connection, world_id).characters if c.is_player
        )
    assert pov.skills == ["剑术"]
    assert any(i["name"] == "疗伤药" for i in pov.inventory)
    assert any(i["name"] == "铁剑" for i in pov.equipment)


def test_snapshot_injects_relationships(database, settings) -> None:
    """快照应注入人物关系(供前端判敌我/队友)。"""
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.read() as connection:
        snap = repo.get_snapshot(connection, world_id)
    assert len(snap.relationships) > 0


def test_use_potion_heals_and_consumes(database, settings) -> None:
    """“用疗伤药”回血并扣掉一剂。"""
    world_id = _setup(database, settings)
    repo = WorldRepository()
    with database.write() as connection:
        connection.execute(
            "UPDATE characters SET health = 50 WHERE world_id = ? AND name = ?",
            (world_id, "旅人"),
        )
    engine = WorldEngine(database, settings)
    engine.submit_player_intent(world_id, "用疗伤药")
    with database.read() as connection:
        pov = next(
            c for c in repo.get_snapshot(connection, world_id).characters if c.is_player
        )
    assert pov.health > 50
    assert sum(1 for i in pov.inventory if i["name"] == "疗伤药") == 1  # 2 -> 1


def test_gather_from_ground(database, settings) -> None:
    """清空背包后可拾取地上的物品。"""
    world_id = _setup(database, settings)
    repo = WorldRepository()
    with database.read() as connection:
        pov = next(
            c for c in repo.get_snapshot(connection, world_id).characters if c.is_player
        )
        pov_id, loc_id = pov.id, pov.location_id
    with database.write() as connection:
        connection.execute(
            "DELETE FROM item_instances WHERE world_id=? AND container_id=? "
            "AND container_type='character_inventory'",
            (world_id, pov_id),
        )
        connection.execute(
            """
            INSERT INTO item_instances(
                id, world_id, item_type_id, container_id, container_type, quantity, condition
            ) VALUES (?, ?, ?, ?, 'location_ground', 1, 100)
            """,
            ("ground_g1", world_id, "healing_potion", loc_id),
        )
    engine = WorldEngine(database, settings)
    engine.submit_player_intent(world_id, "捡起疗伤药")
    with database.read() as connection:
        pov = next(
            c for c in repo.get_snapshot(connection, world_id).characters if c.is_player
        )
    assert sum(1 for i in pov.inventory if i["name"] == "疗伤药") == 1
