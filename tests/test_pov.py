from __future__ import annotations

from datetime import UTC, datetime

from world_engine.database import Database
from world_engine.decisions import DeepSeekDecisionProvider
from world_engine.domain import (
    ActionProposal,
    ActionType,
    CharacterState,
    LocationState,
    WorldSnapshot,
    WorldState,
)
from world_engine.engine import WorldEngine
from world_engine.repository import WorldRepository
from world_engine.seeder import create_iserra_world

BASE = datetime(2040, 4, 1, 8, 0, tzinfo=UTC)


def _char(cid, name, location_id, *, is_pov=False, identity="路人"):
    return CharacterState(
        id=cid, world_id="w", name=name, location_id=location_id, identity=identity,
        energy=80, satiety=80, money=60, traits=[], goals=[], is_core=False,
        is_pov=is_pov,
    )


def _snapshot(pov_loc: str) -> WorldSnapshot:
    world = WorldState(
        id="w", name="测试", current_time=BASE, minutes_per_tick=60, status="running",
        version=0, tick_count=3, time_scale=1.0, clock_revision=0, offline_policy="pause",
        last_adjudication_time=BASE, next_adjudication_time=BASE,
        adjudication_interval_minutes=720, heartbeat_interval_seconds=60,
    )
    locs = [
        LocationState(id=pov_loc, world_id="w", name="主控地", kind="public", resources={}),
        LocationState(id="b", world_id="w", name="他地", kind="public", resources={}),
    ]
    chars = [
        _char("pov", "主控角色", pov_loc, is_pov=True),
        _char("same", "同地角色", pov_loc),
        _char("away", "异地角色", "b"),
    ]
    return WorldSnapshot(world=world, locations=locs, characters=chars)


def test_seed_reserves_pov_for_future_player(database: Database) -> None:
    """正式世界的既有 NPC 不应占用玩家主视角。"""
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.read() as connection:
        snapshot = repo.get_snapshot(connection, world_id)
    povs = [c for c in snapshot.characters if c.is_pov]
    assert povs == []
    assert all(not character.is_player for character in snapshot.characters)


def test_pov_filter_keeps_same_location_dialogue(database: Database) -> None:
    """主控同地点的人物对话保留、异地人物对话置空。"""
    snapshot = _snapshot(pov_loc="a")
    decisions = [
        ActionProposal(actor_id="same", action=ActionType.SOCIALIZE,
                       target_id="pov", reason="打招呼", dialogue="同地台词"),
        ActionProposal(actor_id="away", action=ActionType.SOCIALIZE,
                       target_id="pov", reason="打招呼", dialogue="异地台词"),
    ]
    DeepSeekDecisionProvider._apply_pov_filter(snapshot, decisions)
    assert decisions[0].dialogue == "同地台词"
    assert decisions[1].dialogue is None


def test_pov_filter_no_pov_keeps_all(database: Database) -> None:
    """无主控人物时不过滤，对话全部保留。"""
    snapshot = _snapshot(pov_loc="a")
    for c in snapshot.characters:
        c.is_pov = False
    decisions = [ActionProposal(actor_id="away", action=ActionType.SOCIALIZE,
                                target_id="pov", reason="打招呼", dialogue="台词")]
    DeepSeekDecisionProvider._apply_pov_filter(snapshot, decisions)
    assert decisions[0].dialogue == "台词"


def test_player_intervention_preserves_created_player_pov(database: Database, settings) -> None:
    """局部介入 NPC 不得夺走已创建玩家角色的主视角。"""
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.read() as connection:
        snap = repo.get_snapshot(connection, world_id)
        by_name = {c.name: c for c in snap.characters}
        luomi_id = by_name["洛弥·陶穗"].id
        location_id = snap.locations[0].id

    with database.write() as connection:
        player = repo.create_player_character(
            connection,
            world_id=world_id,
            name="玩家旅人",
            identity="远行学者",
            location_id=location_id,
            traits=["谨慎"],
            goal="记录伊瑟拉",
        )

    engine = WorldEngine(database, settings)
    engine.adjudicate(world_id, trigger="player_intervention", character_ids=[luomi_id])

    with database.read() as connection:
        snap = repo.get_snapshot(connection, world_id)
        by_name = {c.name: c for c in snap.characters}
    assert by_name["洛弥·陶穗"].is_pov is False
    assert by_name["玩家旅人"].is_pov is True
    assert by_name["玩家旅人"].is_player is True
    assert player.is_player is True
