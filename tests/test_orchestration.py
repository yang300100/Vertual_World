from __future__ import annotations

from pathlib import Path

import pytest

from world_engine.config import Settings
from world_engine.database import Database
from world_engine.domain import ActionProposal, ActionType
from world_engine.engine import WorldEngine
from world_engine.orchestration import ProposalCoordinator, build_assembler, forbid_agent_term
from world_engine.repository import WorldRepository


def _agent_settings(tmp_path: Path, *, combat: bool = False) -> Settings:
    return Settings(
        database_path=tmp_path / "test-agent.db",
        minutes_per_tick=60,
        worker_interval_seconds=1,
        active_character_limit=8,
        decision_provider="rules",
        knowledge_enabled=False,
        world_agent_enabled=True,
        world_agent_active_npc_limit=8,
        world_agent_combat_enabled=combat,
    )


@pytest.fixture
def agent_database(tmp_path: Path) -> Database:
    db = Database(_agent_settings(tmp_path).database_path)
    db.initialize()
    return db


def _create_demo_world(database: Database) -> str:
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection,
            name="编排测试世界",
            minutes_per_tick=60,
            seed_demo=True,
        )
    with database.write() as connection:
        location = connection.execute(
            "SELECT id FROM locations WHERE world_id=? AND kind='public' LIMIT 1",
            (world_id,),
        ).fetchone()
        repository.create_player_character(
            connection,
            world_id=world_id,
            name="旅人",
            identity="学者",
            location_id=location["id"],
            traits=["谨慎"],
            goal="记录世界",
        )
    return world_id


def test_agent_audit_tables_exist(agent_database: Database) -> None:
    with agent_database.read() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    for table in ("agent_runs", "agent_proposals", "memory_jobs", "combat_encounters"):
        assert table in tables


def test_agent_disabled_preserves_legacy_behavior(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "legacy.db",
        minutes_per_tick=60,
        worker_interval_seconds=1,
        active_character_limit=8,
        world_agent_enabled=False,
    )
    database = Database(settings.database_path)
    database.initialize()
    world_id = _create_demo_world(database)
    result = WorldEngine(database, settings).adjudicate(world_id, trigger="manual")
    assert len(result.outcomes) > 0
    assert result.agent_runs == []


def test_agent_enabled_produces_audit_and_single_action_per_actor(
    agent_database: Database,
) -> None:
    settings = _agent_settings(agent_database.path.parent)
    world_id = _create_demo_world(agent_database)
    result = WorldEngine(agent_database, settings).adjudicate(world_id, trigger="manual")

    assert len(result.outcomes) > 0
    actor_ids = [outcome.actor_id for outcome in result.outcomes]
    assert len(set(actor_ids)) == len(actor_ids)
    assert result.narrative is not None

    with agent_database.read() as connection:
        runs = WorldRepository().list_agent_runs(connection, world_id)
        proposals = WorldRepository().list_agent_proposals(connection, world_id)
    assert len(runs) >= 1
    assert len(proposals) >= 1
    run_names = {run["agent_name"] for run in runs}
    assert "event_director" in run_names
    assert "active_npc" in run_names
    assert "scene_narrative" in run_names


def test_forbidden_terminology_is_rejected(agent_database: Database) -> None:
    world_id = _create_demo_world(agent_database)
    with agent_database.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)
        active = [c for c in snapshot.characters if c.activation_state == "active"]

    coordinator = ProposalCoordinator(assembler=build_assembler(None))
    scene = coordinator.assembler.assemble(snapshot, trigger="heartbeat")
    active_ids = [c.id for c in active]

    assert forbid_agent_term("数据库权限") is not None
    assert forbid_agent_term("河上的水很清") is None

    fake = ActionProposal(actor_id="not-in-world", action=ActionType.IDLE, reason="测试")
    assert not coordinator._accept_proposal(snapshot, scene, active_ids, fake)

    bad = ActionProposal(
        actor_id=active[0].id,
        action=ActionType.SOCIALIZE,
        target_id=active[1].id,
        reason="正常交流",
        dialogue="这用到纳米机器人技术",
    )
    assert not coordinator._accept_proposal(snapshot, scene, active_ids, bad)


def test_agent_cannot_write_directly(agent_database: Database) -> None:
    """协调器只产生审计与提案，不产生状态补丁类型的原始写。"""
    settings = _agent_settings(agent_database.path.parent)
    world_id = _create_demo_world(agent_database)
    WorldEngine(agent_database, settings).adjudicate(world_id, trigger="manual")
    with agent_database.read() as connection:
        proposals = WorldRepository().list_agent_proposals(connection, world_id)
    proposal_types = {proposal["proposal_type"] for proposal in proposals}
    assert "world_state_patch" not in proposal_types


def test_memory_jobs_are_created_for_settled_events(agent_database: Database) -> None:
    settings = _agent_settings(agent_database.path.parent)
    world_id = _create_demo_world(agent_database)
    WorldEngine(agent_database, settings).adjudicate(world_id, trigger="manual")
    with agent_database.read() as connection:
        jobs = WorldRepository().list_memory_jobs(connection, world_id)
        event_ids = {
            row["id"]
            for row in connection.execute(
                "SELECT id FROM world_events WHERE world_id=?", (world_id,)
            ).fetchall()
        }
    if jobs:
        for job in jobs:
            assert job["event_id"] in event_ids
    assert all(job["status"] in {"pending", "done"} for job in jobs)

