from __future__ import annotations

import pytest

from world_engine.database import Database
from world_engine.decisions import RuleDecisionProvider
from world_engine.domain import ActionProposal, ActionType
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


def test_active_npc_turns_its_own_goals_into_read_only_plans(database, settings) -> None:
    """NPC 的公开计划只从其既有目标形成，而不是由玩家接口写入。"""
    world_id = _create_demo_world(database)
    repository = WorldRepository()
    with database.write() as connection:
        snapshot = repository.get_snapshot(connection, world_id)
        npc = snapshot.characters[0]
        connection.execute(
            "UPDATE characters SET activation_state = 'active', goals_json = '[\"巡查河畔堤岸\"]' WHERE id = ?",
            (npc.id,),
        )

    WorldEngine(database, settings).adjudicate(
        world_id, trigger="manual", character_ids=[npc.id]
    )

    with database.read() as connection:
        plans = connection.execute(
            "SELECT title, details FROM npc_todos WHERE world_id = ? AND character_id = ?",
            (world_id, npc.id),
        ).fetchall()

    assert [(row["title"], row["details"]) for row in plans] == [
        ("推进：巡查河畔堤岸", f"{npc.name}根据自身目标自行安排。")
    ]


def test_sleep_restores_energy_faster_than_natural_time(database, settings) -> None:
    world_id = _create_demo_world(database)
    repository = WorldRepository()
    with database.write() as connection:
        snapshot = repository.get_snapshot(connection, world_id)
        sleeper = snapshot.characters[0]
        connection.execute(
            "UPDATE characters SET energy = 30, satiety = 80 WHERE id = ?", (sleeper.id,)
        )

    class SleepProvider:
        name = "sleep-test"

        def propose(self, snapshot, characters):  # type: ignore[no-untyped-def]
            return [
                ActionProposal(
                    actor_id=sleeper.id,
                    action=ActionType.REST,
                    reason="需要睡一觉恢复精力。",
                )
            ]

    WorldEngine(database, settings, decision_provider=SleepProvider()).adjudicate(
        world_id, trigger="manual", character_ids=[sleeper.id]
    )
    with database.read() as connection:
        after = repository.get_snapshot(connection, world_id).character_by_id(sleeper.id)
    assert after.energy == 70
    assert after.satiety == 76


def test_npc_purchase_uses_the_same_audited_effect_pipeline(database, settings) -> None:
    world_id = _create_demo_world(database)
    repository = WorldRepository()
    with database.write() as connection:
        snapshot = repository.get_snapshot(connection, world_id)
        buyer, seller = snapshot.characters[:2]
        buyer_energy = buyer.energy
        connection.execute(
            """
            UPDATE characters SET longitude = ?, latitude = ?, money = 9, identity = '药店主'
            WHERE id = ?
            """,
            (buyer.longitude, buyer.latitude, seller.id),
        )
        connection.execute("UPDATE characters SET money = 9 WHERE id = ?", (buyer.id,))
        connection.execute(
            """
            INSERT INTO item_types(id, name, category, stack_limit, usable)
            VALUES ('test-herb', '试验草药', 'consumable', 10, 1)
            """
        )
        # 商人必须具有真实库存，购买只能转移这一份现有草药。
        connection.execute(
            """INSERT INTO item_instances(id,world_id,item_type_id,container_id,container_type)
               VALUES ('merchant-herb',?,'test-herb',?,'character_inventory')""",
            (world_id, seller.id),
        )

    class NpcBuyerProvider:
        name = "npc-buyer"

        def propose(self, snapshot, characters):  # type: ignore[no-untyped-def]
            return [
                ActionProposal(
                    actor_id=buyer.id,
                    action=ActionType.SOCIALIZE,
                    target_id=seller.id,
                    reason="我需要补充药材。",
                    dialogue=f"向{seller.name}支付3铜币购买试验草药",
                    reply="给你。",
                )
            ]

    WorldEngine(database, settings, decision_provider=NpcBuyerProvider()).adjudicate(
        world_id, trigger="manual", character_ids=[buyer.id]
    )
    with database.read() as connection:
        buyer_row = connection.execute(
            "SELECT money, energy FROM characters WHERE id = ?", (buyer.id,)
        ).fetchone()
        item_count = connection.execute(
            "SELECT COUNT(*) FROM item_instances WHERE container_id = ? AND item_type_id = 'test-herb'",
            (buyer.id,),
        ).fetchone()[0]
        effect_count = connection.execute(
            "SELECT COUNT(*) FROM action_effects WHERE actor_character_id = ? AND effect_type = 'purchase' AND status = 'applied'",
            (buyer.id,),
        ).fetchone()[0]
    assert buyer_row["money"] == 6
    assert buyer_row["energy"] == buyer_energy - 2
    assert item_count == 1
    assert effect_count == 1


def test_npc_work_proposal_starts_a_real_trip_when_not_at_workplace(database, settings) -> None:
    world_id = _create_demo_world(database)
    repository = WorldRepository()
    with database.read() as connection:
        snapshot = repository.get_snapshot(connection, world_id)
        worker = next(item for item in snapshot.characters if item.name == "林澈")
        before_money = worker.money

    class WorkProvider:
        name = "work-trip"

        def propose(self, snapshot, characters):  # type: ignore[no-untyped-def]
            return [
                ActionProposal(
                    actor_id=worker.id,
                    action=ActionType.WORK,
                    reason="今天仍要靠工作维持生计。",
                )
            ]

    result = WorldEngine(database, settings, decision_provider=WorkProvider()).adjudicate(
        world_id, trigger="manual", character_ids=[worker.id]
    )
    with database.read() as connection:
        after = repository.get_snapshot(connection, world_id)
        worker_after = after.character_by_id(worker.id)

    assert result.outcomes[0].accepted is True
    assert "已动身前往" in result.outcomes[0].summary
    assert worker_after is not None and worker_after.money == before_money
    assert any(movement.character_id == worker.id and movement.status == "moving" for movement in after.movements)


def test_database_state_survives_new_database_instance(database, settings) -> None:
    world_id = _create_demo_world(database)
    WorldEngine(database, settings).tick(world_id)

    reopened = Database(settings.database_path)
    reopened.initialize()
    with reopened.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)

    assert snapshot.world.version == 1
    assert snapshot.world.tick_count == 1


def test_existing_npc_demographics_are_filled_once_without_overwrite(database) -> None:
    world_id = _create_demo_world(database)
    database.initialize()
    with database.read() as connection:
        npc = connection.execute(
            "SELECT id, gender, birth_world_time FROM characters WHERE world_id = ? AND is_player = 0 LIMIT 1",
            (world_id,),
        ).fetchone()
        assert npc is not None
        assert npc["gender"] in {"女性", "男性"}
        assert npc["birth_world_time"]
    with database.write() as connection:
        connection.execute(
            "UPDATE characters SET gender = '自定义', birth_world_time = '2000-01-02T00:00:00+00:00' WHERE id = ?",
            (npc["id"],),
        )
    database.initialize()
    with database.read() as connection:
        preserved = connection.execute(
            "SELECT gender, birth_world_time FROM characters WHERE id = ?", (npc["id"],)
        ).fetchone()
    assert preserved["gender"] == "自定义"
    assert preserved["birth_world_time"] == "2000-01-02T00:00:00+00:00"


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
