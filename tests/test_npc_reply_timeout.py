"""NPC 对话模型无响应时，玩家行动必须按超时预算尽快失败。

回归自：`/player/act` → `submit_player_intent` → `respond_to_player` 这条路径
没有 `submit_call` 的超时保护，而 `respond_to_player` 内部是
`deepseek_max_retries + 1` 次、每次 `deepseek_timeout_seconds`(默认 60s) 的重试。
最坏情况前端要等 3 × 60 + 1.5 ≈ 180 秒，「世界正在回应你的行动」一直转圈。

对照：`api_dialogue.py`（信件）与 `player_action_flow.py` 都用了
`submit_call(...).result(timeout=...)`，唯独玩家对话这条路径漏了。
"""

from __future__ import annotations

import time
from dataclasses import replace

from world_engine.engine import WorldEngine
from world_engine.repository import WorldRepository
from world_engine.seeder import create_iserra_world


class HangingProvider:
    """模拟一个永远不返回的对话模型（比超时预算长得多）。

    注意：`plan_player_action` 与 `respond_to_player` **都要**挂起。
    前者把玩家意图转成行动提案，在流程中先于后者执行；若只挂起后者，
    测试会因为前者抛 AttributeError 被降级到规则引擎而「假通过」。
    """

    name = "hanging"

    def propose(self, snapshot, characters):  # noqa: ANN001
        return []

    def plan_player_action(self, snapshot, player, intent):  # noqa: ANN001
        del snapshot, player, intent
        time.sleep(30)
        raise AssertionError("不应执行到这里：调用方应已按超时放弃")

    def respond_to_player(self, *, npc, player, context):  # noqa: ANN001
        del npc, player, context
        time.sleep(30)  # 远超测试设定的超时预算
        raise AssertionError("不应执行到这里：调用方应已按超时放弃")


def _setup(database) -> tuple[str, str, str]:
    """建世界、放一个玩家进某地点，并把一名 NPC 挪到玩家身边。

    注意：仅让两者 `location_id` 相同是不够的 —— 交谈还要过
    `same_room` 与 `VOICE_RADIUS_KM[delivery]` 两道距离校验，
    `normal` 音量只允许 20 米内。若不把坐标对齐，会在进入对话框之前
    就以「距离超出」被拒，测试也就测不到超时行为。
    """
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.read() as connection:
        snapshot = repo.get_snapshot(connection, world_id)
        location = next(item for item in snapshot.locations if item.name == "河务档案区")
    with database.write() as connection:
        repo.create_player_character(
            connection,
            world_id=world_id,
            name="旅人",
            identity="学者",
            location_id=location.id,
            traits=["谨慎"],
            goal="记录伊瑟拉",
        )
    with database.read() as connection:
        snapshot = repo.get_snapshot(connection, world_id)
        player = next(item for item in snapshot.characters if item.is_player)
        npc = next(
            item
            for item in snapshot.characters
            if not item.is_player and item.location_id == player.location_id
        )
    with database.write() as connection:
        connection.execute(
            "UPDATE characters SET longitude=?, latitude=? WHERE id=?",
            (player.longitude, player.latitude, npc.id),
        )
    return world_id, player.id, npc.id


def test_npc_reply_timeout_returns_instead_of_hanging(database, settings) -> None:
    """模型挂起时，应当按超时预算返回，而不是阻塞数分钟。"""
    world_id, _player_id, npc_id = _setup(database)
    # 把超时预算压到 0.5 秒，让测试快速失败；修复前这里会真的等满 30 秒。
    fast = replace(settings, world_agent_timeout_seconds=0.5)
    engine = WorldEngine(database, fast, decision_provider=HangingProvider())

    started = time.monotonic()
    try:
        engine.submit_player_intent(world_id, "你好呀", target_character_id=npc_id)
    except Exception:  # noqa: BLE001 - 失败方式不是本测试的关注点
        pass
    elapsed = time.monotonic() - started

    assert elapsed < 5.0, (
        f"NPC 对话模型挂起时，submit_player_intent 耗时 {elapsed:.1f} 秒才返回；"
        "应当按 world_agent_timeout_seconds 的超时预算尽快放弃。"
    )
    engine.close()
