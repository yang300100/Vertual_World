from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.database import Database
from world_engine.decisions import RuleDecisionProvider
from world_engine.engine import WorldEngine
from world_engine.orchestration import build_assembler
from world_engine.registration import (
    ConstructionProjectService,
    ElementRegistrationSubmit,
    ElementType,
    RegistrationConflict,
    WorldElementRegistry,
)
from world_engine.repository import WorldRepository, to_iso, utc_now


def _world_and_source(database: Database) -> tuple[str, str, str, str]:
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection,
            name="注册器测试世界",
            minutes_per_tick=60,
            seed_demo=True,
        )
        world = repository.get_snapshot(connection, world_id)
        actor = world.characters[0]
        target = world.characters[1]
        event_id = str(uuid4())
        now = utc_now()
        connection.execute(
            """
            INSERT INTO world_events(
                id, world_id, tick_id, occurred_at, event_type, actor_id,
                target_id, location_id, summary, importance, payload_json, created_at
            ) VALUES (?, ?, ?, ?, 'action.registration_source', ?, ?, ?, ?, 'routine', '{}', ?)
            """,
            (
                event_id,
                world_id,
                str(uuid4()),
                to_iso(world.world.current_time),
                actor.id,
                target.id,
                actor.location_id,
                "人物共同提出了一个会影响世界的计划。",
                to_iso(now),
            ),
        )
        return world_id, event_id, actor.id, target.id


def test_planned_settlement_is_idempotent_and_does_not_create_city(database: Database) -> None:
    world_id, event_id, actor_id, _target_id = _world_and_source(database)
    registry = WorldElementRegistry()
    request = ElementRegistrationSubmit.model_validate(
        {
            "requested_by_character_id": actor_id,
            "source_event_id": event_id,
            "idempotency_key": "settlement:river-haven:v1",
            "payload": {
                "element_type": "settlement",
                "name": "河湾新村",
                "settlement_kind": "village",
                "stage": "planned",
                "longitude": -72.2,
                "latitude": 34.7,
                "requirements": {"wood": 50, "food": 30},
            },
        }
    )
    with database.write() as connection:
        before_version = connection.execute(
            "SELECT version FROM worlds WHERE id = ?", (world_id,)
        ).fetchone()["version"]
        first = registry.submit(connection, world_id=world_id, request=request)
        second = registry.submit(connection, world_id=world_id, request=request)
        after_version = connection.execute(
            "SELECT version FROM worlds WHERE id = ?", (world_id,)
        ).fetchone()["version"]
        location_count = connection.execute(
            "SELECT COUNT(*) FROM locations WHERE world_id = ? AND name = '河湾新村'",
            (world_id,),
        ).fetchone()[0]
        project_count = connection.execute(
            "SELECT COUNT(*) FROM construction_projects WHERE registration_id = ?",
            (first.id,),
        ).fetchone()[0]

    assert first.status == "approved"
    assert first.id == second.id
    assert first.result_entity_id is not None
    assert after_version == before_version + 1
    assert location_count == 0
    assert project_count == 1


def test_reusing_idempotency_key_for_different_payload_is_rejected(database: Database) -> None:
    world_id, event_id, actor_id, _target_id = _world_and_source(database)
    registry = WorldElementRegistry()
    base = {
        "requested_by_character_id": actor_id,
        "source_event_id": event_id,
        "idempotency_key": "lore:same-key:v1",
        "payload": {
            "element_type": "lore",
            "title": "河畔说法",
            "content": "河岸居民相信春季第一场雨会带来好运。",
        },
    }
    with database.write() as connection:
        registry.submit(
            connection,
            world_id=world_id,
            request=ElementRegistrationSubmit.model_validate(base),
        )
        changed = json.loads(json.dumps(base, ensure_ascii=False))
        changed["payload"]["content"] = "完全不同的内容"
        try:
            registry.submit(
                connection,
                world_id=world_id,
                request=ElementRegistrationSubmit.model_validate(changed),
            )
        except RegistrationConflict:
            pass
        else:
            raise AssertionError("不同内容复用幂等键时应当产生冲突")


def test_character_birth_creates_lineage_and_state(database: Database) -> None:
    world_id, event_id, actor_id, target_id = _world_and_source(database)
    registry = WorldElementRegistry()
    planned_request = ElementRegistrationSubmit.model_validate(
        {
            "requested_by_character_id": actor_id,
            "source_event_id": event_id,
            "idempotency_key": "birth:river-child:planned:v1",
            "payload": {
                "element_type": "character_birth",
                "name": "河芽",
                "parent_character_ids": [actor_id, target_id],
                "birth_state": "planned",
                "identity": "新生儿",
                "traits": ["安静"],
            },
        }
    )
    with database.write() as connection:
        planned = registry.submit(
            connection, world_id=world_id, request=planned_request
        )
        connection.execute(
            "UPDATE family_plans SET status = 'due', due_at_world = ? WHERE registration_id = ?",
            (to_iso(utc_now()), planned.id),
        )
        born_request = ElementRegistrationSubmit.model_validate(
            {
                "requested_by_character_id": actor_id,
                "source_event_id": event_id,
                "idempotency_key": "birth:river-child:born:v1",
                "payload": {
                    "element_type": "character_birth",
                    "name": "河芽",
                    "parent_character_ids": [actor_id, target_id],
                    "birth_state": "born",
                    "parent_registration_id": planned.id,
                    "identity": "新生儿",
                    "traits": ["安静"],
                },
            }
        )
        result = registry.submit(connection, world_id=world_id, request=born_request)
        child = connection.execute(
            "SELECT * FROM characters WHERE id = ?", (result.result_entity_id,)
        ).fetchone()
        lineages = connection.execute(
            "SELECT * FROM character_lineages WHERE child_character_id = ?",
            (result.result_entity_id,),
        ).fetchall()
        accumulator = connection.execute(
            "SELECT 1 FROM character_state_accumulators WHERE character_id = ?",
            (result.result_entity_id,),
        ).fetchone()

    assert result.status == "applied"
    assert child["name"] == "河芽"
    assert child["is_player"] == 0
    assert {row["parent_character_id"] for row in lineages} == {actor_id, target_id}
    assert accumulator is not None


def test_player_cannot_instantly_create_city_or_author_canon(database: Database) -> None:
    world_id, event_id, actor_id, _target_id = _world_and_source(database)
    registry = WorldElementRegistry()
    requests = [
        ElementRegistrationSubmit.model_validate(
            {
                "requested_by_character_id": actor_id,
                "source_event_id": event_id,
                "idempotency_key": "settlement:instant-city:v1",
                "payload": {
                    "element_type": "settlement",
                    "name": "瞬成之城",
                    "settlement_kind": "city",
                    "stage": "established",
                    "longitude": -70,
                    "latitude": 30,
                },
            }
        ),
        ElementRegistrationSubmit.model_validate(
            {
                "requested_by_character_id": actor_id,
                "source_event_id": event_id,
                "idempotency_key": "lore:instant-canon:v1",
                "payload": {
                    "element_type": "lore",
                    "knowledge_level": "author_canon",
                    "title": "不可直接成立的真相",
                    "content": "人物试图直接规定作者层真相。",
                },
            }
        ),
    ]
    with database.write() as connection:
        results = [
            registry.submit(connection, world_id=world_id, request=request)
            for request in requests
        ]
        city = connection.execute(
            "SELECT 1 FROM locations WHERE world_id = ? AND name = '瞬成之城'",
            (world_id,),
        ).fetchone()
        canon = connection.execute(
            "SELECT 1 FROM knowledge_entries WHERE world_id = ?",
            (world_id,),
        ).fetchone()

    assert [result.status for result in results] == ["rejected", "rejected"]
    assert all(result.rejection_reason for result in results)
    assert city is None
    assert canon is None


def test_building_structure_and_lore_handlers_write_typed_entities(database: Database) -> None:
    world_id, event_id, actor_id, _target_id = _world_and_source(database)
    registry = WorldElementRegistry()
    with database.read() as connection:
        location = connection.execute(
            "SELECT * FROM locations WHERE world_id = ? ORDER BY name LIMIT 1",
            (world_id,),
        ).fetchone()
    raw_requests = [
        {
            "requested_by_character_id": actor_id,
            "source_event_id": event_id,
            "idempotency_key": "building:riverside-workshop:v1",
            "payload": {
                "element_type": "building",
                "name": "河岸木工作坊",
                "building_type": "workshop",
                "stage": "planned",
                "location_id": location["id"],
                "requirements": {"wood": 20},
            },
        },
        {
            "requested_by_character_id": actor_id,
            "source_event_id": event_id,
            "idempotency_key": "structure:buried-door:v1",
            "payload": {
                "element_type": "structure",
                "name": "埋藏石门",
                "structure_type": "ruin",
                "origin_mode": "discovered",
                "status": "discovered",
                "location_id": location["id"],
                "longitude": location["longitude"],
                "latitude": location["latitude"],
                "provenance_claim": "发现者认为石门比城镇更古老。",
            },
        },
        {
            "requested_by_character_id": actor_id,
            "source_event_id": event_id,
            "idempotency_key": "lore:stone-door-claim:v1",
            "payload": {
                "element_type": "lore",
                "knowledge_level": "local_claim",
                "title": "石门旧说",
                "content": "附近居民把埋藏石门称作旧王留下的封印。",
                "confidence": 0.35,
                "tags": ["遗迹", "地方传闻"],
            },
        },
    ]
    with database.write() as connection:
        results = [
            registry.submit(
                connection,
                world_id=world_id,
                request=ElementRegistrationSubmit.model_validate(item),
            )
            for item in raw_requests
        ]
        building_count = connection.execute("SELECT COUNT(*) FROM buildings").fetchone()[0]
        structure = connection.execute("SELECT * FROM world_structures").fetchone()
        lore = connection.execute("SELECT * FROM knowledge_entries").fetchone()
        entity_count = connection.execute("SELECT COUNT(*) FROM world_entities").fetchone()[0]

    assert [result.status for result in results] == ["applied", "applied", "applied"]
    assert building_count == 1
    assert structure["provenance_status"] == "claimed"
    assert lore["knowledge_level"] == "local_claim"
    assert entity_count == 3


def test_registration_api_submits_lists_and_reads(settings) -> None:
    database = Database(settings.database_path)
    database.initialize()
    world_id, event_id, actor_id, _target_id = _world_and_source(database)
    app = create_app(settings)
    body = {
        "requested_by_character_id": actor_id,
        "source_event_id": event_id,
        "idempotency_key": "api:lore:river-song:v1",
        "payload": {
            "element_type": "lore",
            "knowledge_level": "character_belief",
            "title": "河歌",
            "content": "申请人物认为夜间河声会回应旅人。",
        },
    }
    with TestClient(app) as client:
        created = client.post(f"/api/worlds/{world_id}/registrations", json=body)
        listed = client.get(
            f"/api/worlds/{world_id}/registrations",
            params={"element_type": "lore", "status": "applied"},
        )
        fetched = client.get(
            f"/api/worlds/{world_id}/registrations/{created.json()['id']}"
        )

    assert created.status_code == 201
    assert created.json()["status"] == "applied"
    assert len(listed.json()) == 1
    assert fetched.json()["effects"][0]["effect_type"] == "knowledge.registered"


def test_handler_failure_rolls_back_effects_but_keeps_failed_audit(database: Database) -> None:
    world_id, event_id, actor_id, _target_id = _world_and_source(database)
    registry = WorldElementRegistry()

    class FailingBuildingHandler:
        element_type = ElementType.BUILDING

        @staticmethod
        def apply(context, _payload):
            context.connection.execute(
                """
                INSERT INTO element_registration_effects(
                    id, registration_id, world_id, effect_type, payload_json, created_at
                ) VALUES (?, ?, ?, 'should.rollback', '{}', ?)
                """,
                (
                    str(uuid4()),
                    context.registration_id,
                    context.world_id,
                    to_iso(context.now),
                ),
            )
            raise RuntimeError("模拟处理器故障")

    registry.handlers[ElementType.BUILDING] = FailingBuildingHandler()
    with database.read() as connection:
        location_id = connection.execute(
            "SELECT id FROM locations WHERE world_id = ? LIMIT 1", (world_id,)
        ).fetchone()["id"]
    request = ElementRegistrationSubmit.model_validate(
        {
            "requested_by_character_id": actor_id,
            "source_event_id": event_id,
            "idempotency_key": "building:rollback-probe:v1",
            "payload": {
                "element_type": "building",
                "name": "不应落库的建筑",
                "building_type": "probe",
                "location_id": location_id,
            },
        }
    )
    with database.write() as connection:
        before_version = connection.execute(
            "SELECT version FROM worlds WHERE id = ?", (world_id,)
        ).fetchone()["version"]
        result = registry.submit(connection, world_id=world_id, request=request)
        effect_count = connection.execute(
            "SELECT COUNT(*) FROM element_registration_effects WHERE registration_id = ?",
            (result.id,),
        ).fetchone()[0]
        after_version = connection.execute(
            "SELECT version FROM worlds WHERE id = ?", (world_id,)
        ).fetchone()["version"]

    assert result.status == "failed"
    assert result.rejection_reason == "模拟处理器故障"
    assert effect_count == 0
    assert after_version == before_version


def test_player_action_automatically_registers_explicit_lore(database: Database, settings) -> None:
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection, name="自动注册世界", minutes_per_tick=60, seed_demo=True
        )
        location_id = connection.execute(
            "SELECT id FROM locations WHERE world_id = ? LIMIT 1", (world_id,)
        ).fetchone()["id"]
        player = repository.create_player_character(
            connection,
            world_id=world_id,
            name="记录者",
            identity="旅人",
            location_id=location_id,
            traits=["好奇"],
            goal="记录地方说法",
        )
    engine = WorldEngine(database, settings, decision_provider=RuleDecisionProvider())
    result = engine.submit_player_intent(world_id, "我认为河心的微光会回应歌声")
    with database.read() as connection:
        entry_before = connection.execute(
            "SELECT * FROM knowledge_entries WHERE world_id = ?", (world_id,)
        ).fetchone()
        proposed = WorldElementRegistry().get(
            connection,
            world_id=world_id,
            registration_id=result.registration_ids[0],
        )
    with database.write() as connection:
        confirmed = WorldElementRegistry().confirm(
            connection,
            world_id=world_id,
            registration_id=result.registration_ids[0],
        )
        entry_after = connection.execute(
            "SELECT * FROM knowledge_entries WHERE world_id = ?", (world_id,)
        ).fetchone()

    assert result.outcome.accepted is True
    assert len(result.registration_ids) == 1
    assert result.current_version == result.previous_version + 1
    assert proposed.status == "proposed"
    assert entry_before is None
    assert confirmed.status == "applied"
    assert entry_after["author_character_id"] == player.id
    assert entry_after["knowledge_level"] == "character_belief"


def test_dynamic_lore_respects_character_visibility(database: Database) -> None:
    world_id, event_id, actor_id, target_id = _world_and_source(database)
    registry = WorldElementRegistry()
    with database.write() as connection:
        private = registry.submit(
            connection,
            world_id=world_id,
            request=ElementRegistrationSubmit.model_validate(
                {
                    "requested_by_character_id": actor_id,
                    "source_event_id": event_id,
                    "idempotency_key": "lore:private-river-light:v1",
                    "payload": {
                        "element_type": "lore",
                        "knowledge_level": "character_belief",
                        "title": "河心微光",
                        "content": "河心微光会回应只有我知道的短歌。",
                    },
                }
            ),
        )
        assert private.status == "applied"
        snapshot = WorldRepository().get_snapshot(connection, world_id)
    actor = snapshot.character_by_id(actor_id)
    target = snapshot.character_by_id(target_id)
    assembler = build_assembler(None, database=database)

    actor_hits = assembler.retrieve_knowledge(snapshot, [actor])
    target_hits = assembler.retrieve_knowledge(snapshot, [target])

    assert any(hit.chunk.id.startswith("dynamic:") for hit in actor_hits)
    assert all(not hit.chunk.id.startswith("dynamic:") for hit in target_hits)


def test_settlement_construction_advances_and_creates_location(database: Database) -> None:
    world_id, event_id, actor_id, _target_id = _world_and_source(database)
    registry = WorldElementRegistry()
    construction = ConstructionProjectService()
    request = ElementRegistrationSubmit.model_validate(
        {
            "requested_by_character_id": actor_id,
            "source_event_id": event_id,
            "idempotency_key": "settlement:slow-harbor:v1",
            "payload": {
                "element_type": "settlement",
                "name": "缓潮港",
                "settlement_kind": "village",
                "stage": "planned",
                "longitude": -70.5,
                "latitude": 33.5,
                "requirements": {"food": 5, "money": 2},
            },
        }
    )
    with database.write() as connection:
        before_money = connection.execute(
            "SELECT money FROM characters WHERE id = ?", (actor_id,)
        ).fetchone()["money"]
        source_location_id = connection.execute(
            "SELECT location_id FROM world_events WHERE id = ?", (event_id,)
        ).fetchone()["location_id"]
        before_food = json.loads(
            connection.execute(
                "SELECT resources_json FROM locations WHERE id = ?", (source_location_id,)
            ).fetchone()["resources_json"]
        )["food"]
        registration = registry.submit(connection, world_id=world_id, request=request)
        construction.set_status(
            connection,
            world_id=world_id,
            registration_id=registration.id,
            status="constructing",
        )
        world_time = WorldRepository().get_snapshot(connection, world_id).world.current_time
        updates = construction.advance(
            connection,
            world_id=world_id,
            world_delta_seconds=90 * 24 * 3600,
            world_time=world_time,
            created_at=utc_now(),
        )
        project = construction.get(
            connection, world_id=world_id, registration_id=registration.id
        )
        location = connection.execute(
            "SELECT * FROM locations WHERE world_id = ? AND name = '缓潮港'",
            (world_id,),
        ).fetchone()
        after_money = connection.execute(
            "SELECT money FROM characters WHERE id = ?", (actor_id,)
        ).fetchone()["money"]
        after_food = json.loads(
            connection.execute(
                "SELECT resources_json FROM locations WHERE id = ?", (source_location_id,)
            ).fetchone()["resources_json"]
        )["food"]

    assert updates == 1
    assert project["status"] == "completed"
    assert project["progress"] == 1
    assert location is not None
    assert after_money == before_money - 2
    assert after_food == before_food - 5
