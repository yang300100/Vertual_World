from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.database import Database
from world_engine.engine import WorldEngine
from world_engine.navigation import NavigationDatasetImporter
from world_engine.repository import WorldRepository

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = PROJECT_ROOT / "docs" / "worldbuilding" / "maps" / "navigation" / "noryia"


def _import_approved_dataset(database, world_id: str) -> str:
    importer = NavigationDatasetImporter(ASSET_ROOT)
    with database.write() as connection:
        source_sha256 = json.loads((ASSET_ROOT / "metadata.json").read_text(encoding="utf-8"))[
            "source_sha256"
        ]
        dataset_id = importer.import_candidate(
            connection, world_id=world_id, name="Noryia 导航数据集", source_sha256=source_sha256
        )
        importer.mark_approved(connection, dataset_id)
    return dataset_id


def _create_world_with_player(database) -> tuple[str, str]:
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection, name="路线移动测试世界", minutes_per_tick=60, seed_demo=True
        )
        snapshot = repository.get_snapshot(connection, world_id)
        player = repository.create_player_character(
            connection,
            world_id=world_id,
            name="循路人",
            identity="测绘员",
            location_id=snapshot.locations[0].id,
            traits=[],
            goal="验证路线移动",
        )
    return world_id, player.id


def _player_start(database, world_id: str, player_id: str) -> tuple[float, float]:
    with database.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)
    player = snapshot.character_by_id(player_id)
    return float(player.longitude), float(player.latitude)


def test_movement_uses_route_when_dataset_approved(database, settings) -> None:
    world_id, player_id = _create_world_with_player(database)
    dataset_id = _import_approved_dataset(database, world_id)
    engine = WorldEngine(database, settings)
    start_lon, start_lat = _player_start(database, world_id, player_id)

    movement = engine.start_player_movement(
        world_id, destination_longitude=start_lon + 0.5, destination_latitude=start_lat + 0.4
    )
    assert movement.status == "moving"
    assert movement.route is not None
    assert movement.route["reachable"] is True
    assert movement.navigation_dataset_id == dataset_id
    assert movement.route_distance_km > 0
    assert movement.route["polyline"][0] == [movement.origin_longitude, movement.origin_latitude]
    assert movement.route["polyline"][-1] == [
        movement.destination_longitude,
        movement.destination_latitude,
    ]


def test_movement_advances_along_route_segmentwise(database, settings) -> None:
    world_id, player_id = _create_world_with_player(database)
    _import_approved_dataset(database, world_id)
    engine = WorldEngine(database, settings)
    start_lon, start_lat = _player_start(database, world_id, player_id)
    destination = (start_lon + 0.5, start_lat + 0.4)

    movement = engine.start_player_movement(
        world_id, destination_longitude=destination[0], destination_latitude=destination[1]
    )
    previous_travelled = movement.distance_travelled_km
    longitude_steps: list[float] = []
    for seconds in (3600, 3600, 7200, 14400, 28800, 43200, 43200, 43200):
        engine.heartbeat(world_id, elapsed_seconds=seconds)
        with database.read() as connection:
            snapshot = WorldRepository().get_snapshot(connection, world_id)
            row = connection.execute(
                "SELECT * FROM character_movements WHERE id = ?", (movement.id,)
            ).fetchone()
        player = snapshot.character_by_id(player_id)
        if row is None:
            break
        movement_now = WorldRepository._movement_from_row(row)
        assert movement_now.distance_travelled_km >= previous_travelled - 1e-9
        previous_travelled = movement_now.distance_travelled_km
        longitude_steps.append(round(player.longitude, 5))
    with database.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)
    player = snapshot.character_by_id(player_id)
    assert abs(player.longitude - destination[0]) < 1e-4  # 已到达目标经度
    assert len(set(longitude_steps)) >= 3  # 逐步推进，不是一次跳跃


def test_unreachable_move_reports_reason(database, settings) -> None:
    world_id, player_id = _create_world_with_player(database)
    _import_approved_dataset(database, world_id)
    engine = WorldEngine(database, settings)
    try:
        engine.start_player_movement(
            world_id, destination_longitude=100.0, destination_latitude=-32.0
        )
        raise AssertionError("应因不可达而拒绝")
    except ValueError as error:
        assert "船只" in str(error) or "外海" in str(error) or "陆地" in str(error)


def test_terrain_sample_endpoint(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/worlds", json={"name": "地形采样世界", "seed_demo": True}
        ).json()
        world_id = created["world"]["id"]
        Database(settings.database_path).initialize()
        _import_approved_dataset(Database(settings.database_path), world_id)
        location = created["locations"][0]
        response = client.get(
            f"/api/worlds/{world_id}/terrain",
            params={"longitude": location["longitude"], "latitude": location["latitude"]},
        )
        assert response.status_code == 200
        payload = response.json()
        assert {"elevation_m", "surface_type", "slope_degrees", "water_kind"} <= set(payload)
        assert payload["dataset_status"] == "approved"
