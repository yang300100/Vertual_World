from __future__ import annotations

from pathlib import Path

import pytest

from world_engine.combat import CombatResolver
from world_engine.config import Settings
from world_engine.database import Database
from world_engine.domain import ActionProposal, ActionType, CombatIntent
from world_engine.orchestration import AgentContext, CombatTacticalAgent
from world_engine.repository import WorldRepository


def _combat_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "test-combat.db",
        minutes_per_tick=60,
        worker_interval_seconds=1,
        active_character_limit=8,
        decision_provider="rules",
        knowledge_enabled=False,
        world_agent_enabled=True,
        world_agent_active_npc_limit=8,
        world_agent_combat_enabled=True,
    )


@pytest.fixture
def combat_database(tmp_path: Path) -> Database:
    db = Database(_combat_settings(tmp_path).database_path)
    db.initialize()
    return db


def _setup_world(database: Database) -> tuple[str, str, str]:
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection, name="战斗测试世界", minutes_per_tick=60, seed_demo=True
        )
    with database.write() as connection:
        snap = repository.get_snapshot(connection, world_id)
        location = snap.locations[0]
        chars = [c for c in snap.characters if not c.is_player][:2]
        for character in chars:
            connection.execute(
                "UPDATE characters SET longitude=?, latitude=?, current_location_id=?,"
                " location_id=? WHERE id=?",
                (location.longitude, location.latitude, location.id, location.id, character.id),
            )
    attacker, target = chars[0].id, chars[1].id
    return world_id, attacker, target


def test_combat_seed_is_deterministic(combat_database: Database) -> None:
    world_id, attacker, target = _setup_world(combat_database)
    resolver = CombatResolver()
    assert resolver.generate_seed(world_id, attacker, target) == resolver.generate_seed(
        world_id, attacker, target
    )


def test_combat_rejects_out_of_range_target(combat_database: Database) -> None:
    world_id, attacker, target = _setup_world(combat_database)
    repository = WorldRepository()
    resolver = CombatResolver()
    proposal = ActionProposal(
        actor_id=attacker,
        action=ActionType.ATTACK,
        target_id=target,
        reason="测试攻击",
        dialogue="动手！",
        reply="休想！",
    )
    with combat_database.read() as connection:
        snapshot = repository.get_snapshot(connection, world_id)
    with combat_database.write() as connection:
        # 把 target 移到远处
        connection.execute(
            "UPDATE characters SET longitude=?, latitude=? WHERE id=?",
            (80.0, 80.0, target),
        )
        result = resolver.resolve_proposal(
            connection,
            world_id=world_id,
            tick_id="t1",
            occurred_at=snapshot.world.current_time,
            proposal=proposal,
        )
    assert not result.outcome.accepted


def test_combat_resolves_deterministically_with_seeded_encounter(
    combat_database: Database,
) -> None:
    world_id, attacker, target = _setup_world(combat_database)
    repository = WorldRepository()
    resolver = CombatResolver()
    proposal = ActionProposal(
        actor_id=attacker,
        action=ActionType.ATTACK,
        target_id=target,
        reason="测试攻击",
    )
    with combat_database.read() as connection:
        snapshot = repository.get_snapshot(connection, world_id)
    with combat_database.write() as connection:
        result = resolver.resolve_proposal(
            connection,
            world_id=world_id,
            tick_id="t2",
            occurred_at=snapshot.world.current_time,
            proposal=proposal,
        )
    assert result.outcome.accepted
    assert result.round_seed != 0

    with combat_database.read() as connection:
        encounters = repository.list_combat_encounters(connection, world_id)
    assert encounters
    assert encounters[0]["random_seed"] == resolver.generate_seed(world_id, attacker, target)

    with combat_database.read() as connection:
        events = repository.list_events(connection, world_id, limit=30)
    assert any(e["event_type"] == "action.attack" for e in events)


def test_combat_agent_intent_does_not_change_seed_or_skip_range(
    combat_database: Database,
) -> None:
    world_id, attacker, target = _setup_world(combat_database)
    repository = WorldRepository()
    resolver = CombatResolver()
    proposal = ActionProposal(
        actor_id=attacker,
        action=ActionType.ATTACK,
        target_id=target,
        reason="测试攻击",
    )
    with combat_database.read() as connection:
        snapshot = repository.get_snapshot(connection, world_id)
    seed_before = resolver.generate_seed(world_id, attacker, target)

    combatant_states = [
        snapshot.character_by_id(attacker),
        snapshot.character_by_id(target),
    ]
    agent = CombatTacticalAgent()
    ctx = AgentContext(
        name="combat_tactical",
        scene=None,  # type: ignore[arg-type]
        snapshot=snapshot,
        model_backend=None,
    )
    intents = agent.run(
        ctx,
        participants=[c for c in combatant_states if c],
        encounter_location=None,
    )
    assert intents  # 战术偏好已生成
    assert resolver.generate_seed(world_id, attacker, target) == seed_before

    # 超出范围时即使战术Agent给出攻击偏好，仍然被拒绝（验证不被跳过）。
    with combat_database.write() as connection:
        connection.execute(
            "UPDATE characters SET longitude=?, latitude=? WHERE id=?",
            (81.0, 81.0, target),
        )
        out = resolver.resolve_proposal(
            connection,
            world_id=world_id,
            tick_id="t3",
            occurred_at=snapshot.world.current_time,
            proposal=proposal,
        )
    assert not out.outcome.accepted


def test_combat_track_initiative_and_action_points(combat_database: Database) -> None:
    world_id, attacker, target = _setup_world(combat_database)
    repository = WorldRepository()
    resolver = CombatResolver()
    proposal = ActionProposal(
        actor_id=attacker, action=ActionType.ATTACK, target_id=target, reason="测试攻击"
    )
    with combat_database.read() as connection:
        snapshot = repository.get_snapshot(connection, world_id)
    with combat_database.write() as connection:
        result = resolver.resolve_proposal(
            connection,
            world_id=world_id,
            tick_id="t4",
            occurred_at=snapshot.world.current_time,
            proposal=proposal,
        )
    assert result.action_points == 2
    assert {attacker, target} == set(result.initiative_order)


def test_combat_withdraw_intent_ends_encounter_without_damage(
    combat_database: Database,
) -> None:
    world_id, attacker, target = _setup_world(combat_database)
    repository = WorldRepository()
    resolver = CombatResolver()
    proposal = ActionProposal(
        actor_id=attacker, action=ActionType.ATTACK, target_id=target, reason="测试攻击"
    )
    with combat_database.read() as connection:
        snapshot = repository.get_snapshot(connection, world_id)
    with combat_database.write() as connection:
        result = resolver.resolve_proposal(
            connection,
            world_id=world_id,
            tick_id="t5",
            occurred_at=snapshot.world.current_time,
            proposal=proposal,
            intents=[
                CombatIntent(
                    actor_id=target,
                    intent="withdraw",
                    target_id=attacker,
                    reason="后撤",
                )
            ],
        )
    assert result.withdrawn
    assert result.encounter_status == "withdrawn"
    assert result.action_points == 0
    with combat_database.read() as connection:
        events = repository.list_events(connection, world_id, limit=30)
    assert any(e["event_type"] == "combat.withdraw" for e in events)
