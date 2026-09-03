from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.database import Database
from world_engine.navigation import NavigationDatasetImporter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = PROJECT_ROOT / "docs" / "worldbuilding" / "maps" / "navigation" / "noryia"


def _import_approved_dataset(database, world_id: str) -> None:
    with database.write() as connection:
        source_sha256 = json.loads((ASSET_ROOT / "metadata.json").read_text(encoding="utf-8"))[
            "source_sha256"
        ]
        importer = NavigationDatasetImporter(ASSET_ROOT)
        dataset_id = importer.import_candidate(
            connection, world_id=world_id, name="Noryia", source_sha256=source_sha256
        )
        importer.mark_approved(connection, dataset_id)


def _style_world(settings) -> tuple[str, str]:
    """创建带玩家并导入审核通过数据集的测试世界；返回 (world_id, location_id)。"""
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post(
            "/api/worlds", json={"name": "地图样式世界", "seed_demo": True}
        ).json()
        world_id = created["world"]["id"]
        location = created["locations"][0]
        client.post(
            f"/api/worlds/{world_id}/player",
            json={"name": "旅人", "identity": "测绘员", "location_id": location["id"]},
        )
        Database(settings.database_path).initialize()
        _import_approved_dataset(Database(settings.database_path), world_id)
        return world_id, location["id"]


def test_map_styles_defaults_to_political_and_persists(settings) -> None:
    app = create_app(settings)
    with TestClient(app) as client:
        created = client.post("/api/worlds", json={"name": "样式", "seed_demo": True}).json()
        world_id = created["world"]["id"]
        base = client.get(f"/api/worlds/{world_id}/map-styles").json()
        assert base["style"] == "political"
        assert "passability" in base["available_styles"]
        saved = client.put(f"/api/worlds/{world_id}/map-styles", json={"style": "elevation"})
        assert saved.status_code == 200
        assert client.get(f"/api/worlds/{world_id}/map-styles").json()["style"] == "elevation"
        missing = client.get("/api/worlds/does-not-exist/map-styles")
        assert missing.status_code == 404


def test_world_map_layers_only_expose_navigation_noryia_images(settings) -> None:
    """图层下拉框只暴露指定导航目录中的可显示图片。"""
    app = create_app(settings)
    with TestClient(app) as client:
        payload = client.get("/api/world-map-layers").json()

    layers = payload["layers"]
    by_path = {item["asset_path"]: item for item in layers}
    assert by_path["navigation/noryia/Noryia卫星图.png"]["format"] == "png"
    assert by_path["navigation/noryia/Noryia高程图.svg"]["format"] == "svg"
    assert all(item["asset_path"].startswith("navigation/noryia/") for item in layers)


def test_map_styles_reflects_approved_dataset(settings) -> None:
    world_id, _location_id = _style_world(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        payload = client.get(f"/api/worlds/{world_id}/map-styles").json()
        assert payload["dataset_status"] == "approved"
        assert payload["passability_available"] is True


def test_navigation_dataset_endpoint(settings) -> None:
    world_id, _location_id = _style_world(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        payload = client.get(f"/api/worlds/{world_id}/navigation-dataset").json()
        assert payload["review_status"] == "approved"
        assert payload["azgaar_version"]
        assert payload["feature_counts"]["roads"] > 0
        assert payload["bounds"]["min_longitude"] == -180.0


def test_terrain_returns_passability_summary(settings) -> None:
    world_id, location_id = _style_world(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        location = client.get(f"/api/worlds/{world_id}").json()["locations"][0]
        response = client.get(
            f"/api/worlds/{world_id}/terrain",
            params={
                "longitude": location["longitude"],
                "latitude": location["latitude"],
                "movement_type": "land",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert {"passable", "reason", "speed_kmh", "surface_multiplier"} <= set(
            payload["passability"]
        )
        assert isinstance(payload["passability"]["passable"], bool)


def test_passability_grid_endpoint(settings) -> None:
    world_id, _location_id = _style_world(settings)
    app = create_app(settings)
    with TestClient(app) as client:
        response = client.get(
            f"/api/worlds/{world_id}/passability",
            params={
                "min_longitude": -180,
                "max_longitude": 180,
                "min_latitude": -90,
                "max_latitude": 90,
                "columns": 6,
                "rows": 3,
                "movement_type": "land",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert len(payload["cells"]) == 18
        assert all("passable" in cell for cell in payload["cells"])
        invalid = client.get(
            f"/api/worlds/{world_id}/passability",
            params={"min_longitude": 10, "max_longitude": 5, "min_latitude": 0, "max_latitude": 1},
        )
        assert invalid.status_code == 400


def test_terrain_sample_matches_route_surface_consistently(settings) -> None:
    """悬停地形与移动路线使用同一份真实格网数据（设计一致性验收）。"""
    world_id, location_id = _style_world(settings)
    from world_engine.engine import WorldEngine

    engine = WorldEngine(Database(settings.database_path), settings)
    app = create_app(settings)
    with TestClient(app) as client:
        player = next(
            item
            for item in client.get(f"/api/worlds/{world_id}").json()["characters"]
            if item["is_player"]
        )
        movement = engine.start_player_movement(
            world_id,
            destination_longitude=player["longitude"] + 0.5,
            destination_latitude=player["latitude"] + 0.4,
        )
        route = movement.route
    assert route["reachable"] is True
    # 路线途经格与地形采样一致：取路线首格，地形采样返回地表类型与路段一致。
    first = route["polyline"][1]
    with TestClient(app) as client:
        sample = client.get(
            f"/api/worlds/{world_id}/terrain",
            params={"longitude": first[0], "latitude": first[1], "movement_type": "land"},
        ).json()
    route_surfaces = {segment["surface"] for segment in route["segments"]}
    assert sample["surface_type"] in route_surfaces or sample["surface_type"] == "marine"
