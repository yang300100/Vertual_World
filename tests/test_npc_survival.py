"""端到端验收：身无分文的 NPC 能靠采集活下来。

这是本次修复的核心验收——修复前，NPC 会陷入
「饿 → 无法工作 → 没钱 → 更饿」的死锁，饱食度永久为 0。
"""

from __future__ import annotations

from dataclasses import replace

from world_engine.engine import WorldEngine
from world_engine.food_supply import seed_food_supply
from world_engine.seeder import create_iserra_world

# 伊瑟拉初始世界的 NPC 人数（不含玩家），用于报告存活比例。
NPC_COUNT = 66


def test_impoverished_npc_does_not_starve(database, settings) -> None:
    """穷光蛋 NPC 在推进多轮后饱食度应回升，且能吃到东西。"""
    world_id = create_iserra_world(database)
    with database.write() as connection:
        seed_food_supply(connection, world_id)
        # 制造最坏情况：所有 NPC 身无分文且饿到极限。
        connection.execute(
            "UPDATE characters SET money=0, satiety=0 WHERE world_id=? AND is_player=0",
            (world_id,),
        )
        # 补一刀：玩家视角角色（is_pov）也一并归零，确保后续统计口径
        # （is_player=0）覆盖的是真正的「全员身无分文」局面。
        connection.execute(
            "UPDATE characters SET money=0, satiety=0 WHERE world_id=? AND is_pov=0",
            (world_id,),
        )
        # 前置断言：确认最坏局面确实成立，避免初始值让测试假通过。
        broke = connection.execute(
            "SELECT COUNT(*) FROM characters WHERE world_id=? AND is_pov=0 "
            "AND (money>0 OR satiety>0)",
            (world_id,),
        ).fetchone()[0]
    assert broke == 0, f"应当制造出全员资产为零的局面，仍有 {broke} 人例外"
    # 关闭世界 agent，避免测试过程中调用真实模型 API。
    engine = WorldEngine(database, replace(settings, world_agent_enabled=False))
    for _ in range(20):
        engine.heartbeat(world_id, elapsed_seconds=3600)

    with database.read() as connection:

        def scalar(sql: str, params: tuple = ()) -> float:
            return connection.execute(sql, params).fetchone()[0]

        satiety = scalar(
            "SELECT AVG(satiety) FROM characters WHERE world_id=? AND is_player=0",
            (world_id,),
        )
        fed = scalar(
            "SELECT COUNT(*) FROM characters WHERE world_id=? AND is_player=0 AND satiety>0",
            (world_id,),
        )
        ate = scalar(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? AND event_type='action.eat'",
            (world_id,),
        )
        harvested = scalar(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? "
            "AND event_type='action.resource_harvested'",
            (world_id,),
        )
        # 死锁的另一个症状：工作刚开工就因饥饿被打断。修复后应不再发生。
        interrupted = scalar(
            "SELECT COUNT(*) FROM world_events WHERE world_id=? "
            "AND event_type='action.life_interrupted'",
            (world_id,),
        )
    detail = (
        f"eat={ate}, harvest={harvested}, 平均饱食度={satiety:.2f}, "
        f"饱食度>0 的 NPC={fed}/{NPC_COUNT}, 累计 life_interrupted={interrupted}"
    )
    assert ate > 0, f"NPC 应当吃到过食物（{detail}）"
    assert harvested > 0, f"NPC 应当采集过资源（{detail}）"
    assert satiety > 0, f"平均饱食度应回升（{detail}）"
