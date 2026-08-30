from __future__ import annotations

from datetime import UTC, datetime

from world_engine.database import Database
from world_engine.decisions import RuleDecisionProvider
from world_engine.domain import (
    ActionType,
    CharacterState,
    LocationState,
    WorldSnapshot,
    WorldState,
)
from world_engine.repository import WorldRepository
from world_engine.seeder import create_iserra_world

BASE = datetime(2040, 4, 1, 8, 0, tzinfo=UTC)


def test_characters_table_has_identity_column(database: Database) -> None:
    """迁移后 characters 表应含 identity 列。"""
    with database.read() as connection:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(characters)").fetchall()
        }
    assert "identity" in columns


def test_iserra_characters_have_identity(database: Database) -> None:
    """正式世界人物应写入身份：塞芙拉=议约女王，洛弥含'河务六席'。"""
    world_id = create_iserra_world(database)
    repo = WorldRepository()
    with database.read() as connection:
        snapshot = repo.get_snapshot(connection, world_id)
        by_name = {ch.name: ch for ch in snapshot.characters}
    assert by_name["塞芙拉·维誓"].identity == "议约女王"
    assert "河务六席" in by_name["洛弥·陶穗"].identity
    assert by_name["北潭·漱泉"].identity == "河务六席·书记官"
    # 所有人物都应带身份,不能为空
    assert all(ch.identity for ch in snapshot.characters)


def _snapshot_with_two_at_same_place(tick_count: int = 3) -> WorldSnapshot:
    world = WorldState(
        id="w",
        name="测试世界",
        current_time=BASE,
        minutes_per_tick=60,
        status="running",
        version=0,
        tick_count=tick_count,
        time_scale=1.0,
        clock_revision=0,
        offline_policy="pause",
        last_adjudication_time=BASE,
        next_adjudication_time=BASE,
        adjudication_interval_minutes=720,
        heartbeat_interval_seconds=60,
    )
    loc = LocationState(id="a", world_id="w", name="甲地", kind="public", resources={})
    char1 = CharacterState(
        id="c1", world_id="w", name="林一", location_id="a",
        energy=80, satiety=80, money=50, traits=[], goals=[], is_core=False,
    )
    char2 = CharacterState(
        id="c2", world_id="w", name="林二", location_id="a",
        energy=80, satiety=80, money=50, traits=[], goals=[], is_core=False,
    )
    return WorldSnapshot(world=world, locations=[loc], characters=[char1, char2])


def test_rule_socialize_generates_dialogue() -> None:
    """规则决策器在 SOCIALIZE 时给出一句台词(dialogue)。"""
    snapshot = _snapshot_with_two_at_same_place(tick_count=3)
    provider = RuleDecisionProvider()
    proposals = provider.propose(snapshot, [snapshot.characters[0]])
    assert proposals and proposals[0].action is ActionType.SOCIALIZE
    assert proposals[0].dialogue and "林二" in proposals[0].dialogue


def test_non_socialize_actions_have_no_dialogue() -> None:
    """非社交动作(如 idle)应默认无对话。"""
    snapshot = _snapshot_with_two_at_same_place(tick_count=0)  # 非 %3,走到 idle/travel
    provider = RuleDecisionProvider()
    proposals = provider.propose(snapshot, [snapshot.characters[0]])
    # tick_count=0 → socialize 条件 %3==0 成立,此处换 tick 值走 idle 分支
    snapshot2 = _snapshot_with_two_at_same_place(tick_count=1)
    proposals2 = provider.propose(snapshot2, [snapshot2.characters[0]])
    non_social = [p for p in ([*proposals, *proposals2]) if p.action is not ActionType.SOCIALIZE]
    assert all(p.dialogue is None for p in non_social)
