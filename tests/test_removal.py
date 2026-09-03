from __future__ import annotations

import json
from uuid import uuid4

from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.database import Database
from world_engine.engine import WorldEngine
from world_engine.registration import ElementRegistrationSubmit, WorldElementRegistry
from world_engine.removal import ElementRemovalSubmit, WorldElementRemover
from world_engine.repository import WorldRepository, to_iso, utc_now


def _world_source_and_building(database: Database) -> tuple[str, str, str, str]:
    repository = WorldRepository()
    registry = WorldElementRegistry()
    with database.write() as connection:
        world_id = repository.create_world(
            connection, name="删除器测试世界", minutes_per_tick=60, seed_demo=True
        )
        snapshot = repository.get_snapshot(connection, world_id)
        actor, target = snapshot.characters[:2]
        source_event_id = str(uuid4())
        now = utc_now()
        connection.execute(
            """
            INSERT INTO world_events(
                id, world_id, tick_id, occurred_at, event_type, actor_id, target_id,
                location_id, summary, importance, payload_json, created_at
            ) VALUES (?, ?, ?, ?, 'action.test', ?, ?, ?, ?, 'routine', '{}', ?)
            """,
            (
                source_event_id,
                world_id,
                str(uuid4()),
                to_iso(snapshot.world.current_time),
                actor.id,
                target.id,
                actor.location_id,
                "两人决定建造一座会被测试摧毁的工坊。",
                to_iso(now),
            ),
        )
        registration = registry.submit(
            connection,
            world_id=world_id,
            request=ElementRegistrationSubmit.model_validate(
                {
                    "requested_by_character_id": actor.id,
                    "source_event_id": source_event_id,
                    "idempotency_key": "removal-test:building-register",
                    "payload": {
                        "element_type": "building",
                        "name": "潮痕工坊",
                        "building_type": "工坊",
                        "stage": "planned",
                        "location_id": actor.location_id,
                    },
                }
            ),
        )
        assert registration.result_entity_id is not None
        destroy_event_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO world_events(
                id, world_id, tick_id, occurred_at, event_type, actor_id, target_id,
                location_id, summary, importance, payload_json, created_at
            ) VALUES (?, ?, ?, ?, 'action.test_destruction', ?, ?, ?, ?, 'routine', '{}', ?)
            """,
            (
                destroy_event_id,
                world_id,
                str(uuid4()),
                to_iso(snapshot.world.current_time),
                actor.id,
                target.id,
                actor.location_id,
                "一场失控的火焰毁坏了工坊。",
                to_iso(now),
            ),
        )
        return world_id, destroy_event_id, actor.id, registration.result_entity_id


def test_remover_tombstones_building_and_preserves_audit(database: Database) -> None:
    world_id, event_id, actor_id, building_id = _world_source_and_building(database)
    remover = WorldElementRemover()
    request = ElementRemovalSubmit.model_validate(
        {
            "requested_by_character_id": actor_id,
            "source_event_id": event_id,
            "idempotency_key": "removal-test:building-fire",
            "target_element_type": "building",
            "target_entity_id": building_id,
            "reason": "destroyed",
            "details": "火焰烧毁了承重结构，不能继续施工。",
        }
    )
    with database.write() as connection:
        result = remover.submit(connection, world_id=world_id, request=request)
        repeated = remover.submit(connection, world_id=world_id, request=request)
        building = connection.execute(
            "SELECT status FROM buildings WHERE id = ?", (building_id,)
        ).fetchone()
        catalog = connection.execute(
            """
            SELECT lifecycle_state, destroyed_by_event_id FROM world_element_catalog
            WHERE world_id = ? AND entity_type = 'building' AND entity_id = ?
            """,
            (world_id, building_id),
        ).fetchone()
        project = connection.execute(
            "SELECT status FROM construction_projects WHERE target_entity_id = ?", (building_id,)
        ).fetchone()
        removal_event = connection.execute(
            "SELECT event_type FROM world_events WHERE tick_id = ?", (result.id,)
        ).fetchone()

    assert result.status == "applied"
    assert repeated.id == result.id
    assert building["status"] == "ruined"
    assert project["status"] == "cancelled"
    assert catalog["lifecycle_state"] == "destroyed"
    assert catalog["destroyed_by_event_id"]
    assert removal_event["event_type"] == "world.element_removed"
    assert {effect.effect_type for effect in result.effects} >= {
        "building.ruined",
        "construction.destroyed",
    }


def test_removal_api_returns_tombstone_view(settings) -> None:
    database = Database(settings.database_path)
    database.initialize()
    world_id, event_id, actor_id, building_id = _world_source_and_building(database)
    with TestClient(create_app(settings)) as client:
        response = client.post(
            f"/api/worlds/{world_id}/removals",
            json={
                "requested_by_character_id": actor_id,
                "source_event_id": event_id,
                "idempotency_key": "removal-test:api-building-fire",
                "target_element_type": "building",
                "target_entity_id": building_id,
                "reason": "destroyed",
                "details": "被可验证的灾害事件摧毁。",
            },
        )
        listed = client.get(f"/api/worlds/{world_id}/removals")

    assert response.status_code == 201
    assert response.json()["status"] == "applied"
    assert listed.status_code == 200
    assert listed.json()[0]["target_entity_id"] == building_id


def test_destroyed_location_leaves_active_world_snapshot(database: Database) -> None:
    world_id, event_id, actor_id, _building_id = _world_source_and_building(database)
    repository = WorldRepository()
    with database.write() as connection:
        connection.execute(
            """
            INSERT INTO locations(
                id, world_id, name, kind, resources_json, longitude, latitude,
                area_radius_km, area_priority
            ) VALUES ('abandoned-removal-site', ?, '待拆观测站', 'ruin', '{}', 15, 15, 1, 0)
            """,
            (world_id,),
        )
        result = WorldElementRemover().submit(
            connection,
            world_id=world_id,
            request=ElementRemovalSubmit(
                requested_by_character_id=actor_id,
                source_event_id=event_id,
                idempotency_key="removal-test:empty-location",
                target_element_type="location",
                target_entity_id="abandoned-removal-site",
                reason="destroyed",
                details="观测站在此前的冲突中已经坍塌。",
            ),
        )
        snapshot = repository.get_snapshot(connection, world_id)

    assert result.status == "applied"
    assert all(location.id != "abandoned-removal-site" for location in snapshot.locations)


def test_destroyed_npc_leaves_active_world_snapshot(database: Database) -> None:
    world_id, event_id, actor_id, _building_id = _world_source_and_building(database)
    repository = WorldRepository()
    with database.write() as connection:
        target_id = connection.execute(
            "SELECT id FROM characters WHERE world_id = ? AND id <> ? LIMIT 1",
            (world_id, actor_id),
        ).fetchone()["id"]
        result = WorldElementRemover().submit(
            connection,
            world_id=world_id,
            request=ElementRemovalSubmit(
                requested_by_character_id=actor_id,
                source_event_id=event_id,
                idempotency_key="removal-test:npc-death",
                target_element_type="character",
                target_entity_id=target_id,
                reason="destroyed",
                details="该 NPC 在已结算冲突中死亡。",
            ),
        )
        snapshot = repository.get_snapshot(connection, world_id)

    assert result.status == "applied"
    assert all(character.id != target_id for character in snapshot.characters)


def test_destroyed_vehicle_detaches_owner_and_cancels_its_journey(
    database: Database, settings
) -> None:
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection, name="毁坏载具世界", minutes_per_tick=60, seed_demo=True
        )
        snapshot = repository.get_snapshot(connection, world_id)
        player = repository.create_player_character(
            connection,
            world_id=world_id,
            name="载具旅人",
            identity="试航员",
            location_id=snapshot.locations[0].id,
            traits=[],
            goal="测试事故处理",
        )
        npc = next(character for character in snapshot.characters if character.id != player.id)
        vehicle_id = str(uuid4())
        now = to_iso(utc_now())
        connection.execute(
            """
            INSERT INTO vehicles(
                id, world_id, name, movement_type, speed_kmh, owner_character_id,
                is_available, metadata_json, created_at, updated_at
            ) VALUES (?, ?, '曙光艇', 'flight', 120, ?, 1, ?, ?, ?)
            """,
            (vehicle_id, world_id, player.id, json.dumps({}), now, now),
        )
        source_event_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO world_events(
                id, world_id, tick_id, occurred_at, event_type, actor_id, target_id,
                location_id, summary, importance, payload_json, created_at
            ) VALUES (?, ?, ?, ?, 'action.vehicle_accident', ?, ?, ?, ?, 'routine', '{}', ?)
            """,
            (
                source_event_id,
                world_id,
                str(uuid4()),
                to_iso(snapshot.world.current_time),
                player.id,
                npc.id,
                player.location_id,
                "曙光艇在出发后被毁。",
                now,
            ),
        )

    engine = WorldEngine(database, settings)
    engine.select_player_transport(world_id, vehicle_id)
    movement = engine.start_player_movement(
        world_id,
        destination_longitude=snapshot.locations[1].longitude,
        destination_latitude=snapshot.locations[1].latitude,
    )
    with database.write() as connection:
        result = WorldElementRemover().submit(
            connection,
            world_id=world_id,
            request=ElementRemovalSubmit(
                requested_by_character_id=player.id,
                source_event_id=source_event_id,
                idempotency_key="removal-test:vehicle-destroyed",
                target_element_type="vehicle",
                target_entity_id=vehicle_id,
                reason="destroyed",
                details="载具在行程中无法继续使用。",
            ),
        )
        character = connection.execute(
            """
            SELECT active_vehicle_id, movement_type, movement_speed_kmh
            FROM characters WHERE id = ?
            """,
            (player.id,),
        ).fetchone()
        movement_status = connection.execute(
            "SELECT status FROM character_movements WHERE id = ?", (movement.id,)
        ).fetchone()

    assert result.status == "applied"
    assert character["active_vehicle_id"] is None
    assert character["movement_type"] == "land"
    assert character["movement_speed_kmh"] == 5
    assert movement_status["status"] == "cancelled"
    assert result.effects[0].payload["cancelled_movements"] == 1
