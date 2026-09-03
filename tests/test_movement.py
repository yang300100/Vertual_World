from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.engine import WorldEngine
from world_engine.repository import WorldRepository, to_iso


def _create_player_world(database) -> tuple[str, str]:
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection,
            name="持续移动测试世界",
            minutes_per_tick=60,
            seed_demo=True,
        )
        snapshot = repository.get_snapshot(connection, world_id)
        player = repository.create_player_character(
            connection,
            world_id=world_id,
            name="行路人",
            identity="测绘员",
            location_id=snapshot.locations[0].id,
            traits=[],
            goal="验证持续移动",
        )
    return world_id, player.id


def test_map_movement_advances_and_can_be_cancelled(database, settings) -> None:
    world_id, player_id = _create_player_world(database)
    engine = WorldEngine(database, settings)
    with database.read() as connection:
        before = WorldRepository().get_snapshot(connection, world_id)
    player_before = before.character_by_id(player_id)
    assert player_before is not None

    movement = engine.start_player_movement(
        world_id,
        destination_longitude=player_before.longitude + 0.2,
        destination_latitude=player_before.latitude,
    )
    with database.read() as connection:
        started = WorldRepository().get_snapshot(connection, world_id)
    player_started = started.character_by_id(player_id)
    assert player_started is not None
    assert player_started.longitude == player_before.longitude
    assert movement.status == "moving"
    assert movement.movement_type == "land"
    assert movement.speed_kmh == 5

    engine.heartbeat(world_id, elapsed_seconds=3600)
    with database.read() as connection:
        progressed = WorldRepository().get_snapshot(connection, world_id)
    player_progressed = progressed.character_by_id(player_id)
    assert player_progressed is not None
    assert player_before.longitude < player_progressed.longitude < movement.destination_longitude

    cancelled = engine.cancel_player_movement(world_id)
    assert cancelled.status == "cancelled"
    with database.read() as connection:
        stopped = WorldRepository().get_snapshot(connection, world_id)
        logs = WorldRepository().list_events(
            connection, world_id, scope="log", limit=20
        )
        chronicle = WorldRepository().list_events(
            connection, world_id, scope="chronicle", limit=20
        )
    player_stopped = stopped.character_by_id(player_id)
    assert player_stopped is not None
    assert player_stopped.longitude == player_progressed.longitude
    assert stopped.movements == []
    assert {item["event_type"] for item in logs} >= {
        "action.route_planned",
        "action.movement_started",
        "action.movement_cancelled",
    }
    assert all(
        UUID(item["tick_id"])
        for item in logs
        if item["event_type"].startswith("action.movement_")
    )
    assert all(item["importance"] == "routine" for item in logs)
    assert all(item["importance"] == "major" for item in chronicle)


def test_vehicle_controls_movement_type_and_speed(database, settings) -> None:
    world_id, player_id = _create_player_world(database)
    vehicle_id = str(uuid4())
    now = to_iso(datetime.now(UTC))
    with database.write() as connection:
        connection.execute(
            """
            INSERT INTO vehicles(
                id, world_id, name, movement_type, speed_kmh,
                owner_character_id, metadata_json, created_at, updated_at
            ) VALUES (?, ?, '测试飞行器', 'flight', 120, ?, ?, ?, ?)
            """,
            (vehicle_id, world_id, player_id, json.dumps({"test": True}), now, now),
        )
    engine = WorldEngine(database, settings)
    selected = engine.select_player_transport(world_id, vehicle_id)
    player = selected.character_by_id(player_id)
    assert player is not None
    assert player.active_vehicle_id == vehicle_id
    assert player.movement_type == "flight"
    assert player.movement_speed_kmh == 120

    movement = engine.start_player_movement(
        world_id,
        destination_longitude=player.longitude + 1,
        destination_latitude=player.latitude,
    )
    assert movement.vehicle_id == vehicle_id
    assert movement.movement_type == "flight"
    assert movement.speed_kmh == 120


def test_movement_api_starts_and_cancels_without_teleporting(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/worlds", json={"name": "移动API世界", "seed_demo": True}
        ).json()
        world_id = created["world"]["id"]
        location = created["locations"][0]
        player_snapshot = client.post(
            f"/api/worlds/{world_id}/player",
            json={
                "name": "API旅人",
                "identity": "行者",
                "location_id": location["id"],
            },
        ).json()
        player_before = next(
            item for item in player_snapshot["characters"] if item["is_player"]
        )
        started = client.post(
            f"/api/worlds/{world_id}/player/move",
            json={
                "destination_longitude": player_before["longitude"] + 0.1,
                "destination_latitude": player_before["latitude"],
            },
        )
        during = client.get(f"/api/worlds/{world_id}").json()
        logs = client.get(
            f"/api/worlds/{world_id}/events?scope=log&limit=20"
        )
        chronicle = client.get(
            f"/api/worlds/{world_id}/events?scope=chronicle&limit=20"
        )
        cancelled = client.post(
            f"/api/worlds/{world_id}/player/move/cancel"
        )

    player_during = next(item for item in during["characters"] if item["is_player"])
    assert started.status_code == 201
    assert player_during["longitude"] == player_before["longitude"]
    assert len(during["movements"]) == 1
    assert logs.status_code == 200
    assert any(
        item["event_type"] == "action.route_planned" for item in logs.json()
    )
    assert any(
        item["event_type"] == "action.movement_started" for item in logs.json()
    )
    assert chronicle.status_code == 200
    assert cancelled.status_code == 200
