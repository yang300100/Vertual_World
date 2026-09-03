from __future__ import annotations

from uuid import uuid4

from world_engine.registration import (
    ConstructionProjectService,
    ElementRegistrationSubmit,
    WorldElementRegistry,
)
from world_engine.repository import WorldRepository, to_iso, utc_now


def _source_event(
    connection, world_id: str, actor_id: str, target_id: str, location_id: str
) -> str:
    event_id = str(uuid4())
    connection.execute(
        """
        INSERT INTO world_events(
            id, world_id, tick_id, occurred_at, event_type, actor_id, target_id,
            location_id, summary, importance, payload_json, created_at
        ) VALUES (?, ?, ?, ?, 'action.catalog_test', ?, ?, ?, ?, 'routine', '{}', ?)
        """,
        (
            event_id,
            world_id,
            str(uuid4()),
            "2040-04-01T08:00:00+00:00",
            actor_id,
            target_id,
            location_id,
            "测试元素目录是否持续同步。",
            to_iso(utc_now()),
        ),
    )
    return event_id


def test_new_world_and_player_are_registered_in_element_catalog(database, settings) -> None:
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection,
            name="元素目录新世界",
            minutes_per_tick=settings.minutes_per_tick,
            seed_demo=True,
        )
        snapshot = repository.get_snapshot(connection, world_id)
        player = repository.create_player_character(
            connection,
            world_id=world_id,
            name="目录旅人",
            identity="记录者",
            location_id=snapshot.locations[0].id,
            traits=[],
            goal="核验目录",
        )
        element_rows = connection.execute(
            """
            SELECT entity_type, entity_id FROM world_element_catalog
            WHERE world_id = ? AND lifecycle_state = 'active'
            """,
            (world_id,),
        ).fetchall()

    elements = {(row["entity_type"], row["entity_id"]) for row in element_rows}
    assert {("character", player.id), ("world_map", f"{world_id}:map:z0")} <= elements
    assert {("location", location.id) for location in snapshot.locations} <= elements


def test_cancelled_construction_project_retires_catalog_element(database, settings) -> None:
    repository = WorldRepository()
    registry = WorldElementRegistry()
    construction = ConstructionProjectService()
    with database.write() as connection:
        world_id = repository.create_world(
            connection,
            name="元素目录建设世界",
            minutes_per_tick=settings.minutes_per_tick,
            seed_demo=True,
        )
        snapshot = repository.get_snapshot(connection, world_id)
        actor, target = snapshot.characters[:2]
        event_id = _source_event(connection, world_id, actor.id, target.id, actor.location_id)
        registration = registry.submit(
            connection,
            world_id=world_id,
            request=ElementRegistrationSubmit.model_validate(
                {
                    "requested_by_character_id": actor.id,
                    "source_event_id": event_id,
                    "idempotency_key": "catalog-test:building",
                    "payload": {
                        "element_type": "building",
                        "name": "目录工坊",
                        "building_type": "工坊",
                        "stage": "planned",
                        "location_id": actor.location_id,
                    },
                }
            ),
        )
        construction.set_status(
            connection,
            world_id=world_id,
            registration_id=registration.id,
            status="cancelled",
        )
        project = connection.execute(
            "SELECT id FROM construction_projects WHERE registration_id = ?",
            (registration.id,),
        ).fetchone()
        catalog = connection.execute(
            """
            SELECT lifecycle_state FROM world_element_catalog
            WHERE world_id = ? AND entity_type = 'construction_project' AND entity_id = ?
            """,
            (world_id, project["id"]),
        ).fetchone()

    assert catalog["lifecycle_state"] == "retired"
