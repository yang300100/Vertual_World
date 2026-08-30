from __future__ import annotations

from world_engine.database import Database
from world_engine.repository import WorldRepository
from world_engine.seeder import create_iserra_world


def test_iserra_world_lands_full_entity_set(database: Database) -> None:
    """创建伊瑟拉世界后,地点、人物、关系、初始事件与记忆都完整落库。"""
    world_id = create_iserra_world(database)

    repo = WorldRepository()
    with database.read() as connection:
        snapshot = repo.get_snapshot(connection, world_id)

        # 地点:13 个核心城市 + 澜誓城 4 个功能区
        assert len(snapshot.locations) == 17

        # 人物:政体代表(24) + 澜誓城本地(6) + 各地居民(约36)= 66
        assert len(snapshot.characters) == 66

        # 激活策略:只有 6 位核心人物 is_core=True
        core = [c for c in snapshot.characters if c.is_core]
        assert len(core) == 6
        assert {"塞芙拉·维誓", "洛弥·陶穗"} <= {c.name for c in core}

        # 背景 NPC 状态偏高(难被选中),核心状态偏低(更易作为活跃人物)
        background = [c for c in snapshot.characters if not c.is_core]
        core_satiety = [c.satiety for c in core]
        background_satiety = [c.satiety for c in background]
        assert all(value < 70 for value in core_satiety)
        assert all(value > 75 for value in background_satiety)

        # 双向关系(塞芙拉 ↔ 洛弥 的对立是张力核心)
        rel = connection.execute(
            """
            SELECT affinity, trust FROM relationships
            WHERE world_id = ? AND source_character_id = (
                SELECT id FROM characters WHERE world_id = ? AND name = '塞芙拉·维誓'
            ) AND target_character_id = (
                SELECT id FROM characters WHERE world_id = ? AND name = '洛弥·陶穗'
            )
            """,
            (world_id, world_id, world_id),
        ).fetchone()
        assert rel is not None
        assert rel["affinity"] < 0 and rel["trust"] < 0

        # 初始客观事件(世界开局)
        initial_events = connection.execute(
            """
            SELECT * FROM world_events
            WHERE world_id = ? AND event_type = 'world.initial'
            """,
            (world_id,),
        ).fetchall()
        assert len(initial_events) == 1
        assert "合流复誓" in initial_events[0]["summary"]

        # 初始人物记忆挂到核心人物
        memory_count = connection.execute(
            """
            SELECT COUNT(*) AS n FROM character_memories
            WHERE world_id = ?
            """,
            (world_id,),
        ).fetchone()["n"]
        assert memory_count >= 6
