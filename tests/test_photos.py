from __future__ import annotations

import base64
from dataclasses import replace

from fastapi.testclient import TestClient

from world_engine.api import create_app
from world_engine.database import Database
from world_engine.photos import (
    GeneratedImage,
    PhotoCaptureRequest,
    PhotoService,
    PortraitUploadRequest,
)
from world_engine.repository import WorldRepository

_PNG = b"\x89PNG\r\n\x1a\nphoto-test"


class _FakeImageClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def generate(self, *, prompt: str, reference_images: list[str]) -> GeneratedImage:
        self.calls.append((prompt, reference_images))
        return GeneratedImage(content=_PNG, mime_type="image/png")


class _WriteCheckingImageClient(_FakeImageClient):
    def __init__(self, database: Database, world_id: str) -> None:
        super().__init__()
        self.database = database
        self.world_id = world_id

    def generate(self, *, prompt: str, reference_images: list[str]) -> GeneratedImage:
        # 若 API 在此时持有 BEGIN IMMEDIATE，本次写入会被阻塞；它验证生图阶段没有写锁。
        with self.database.write() as connection:
            connection.execute(
                "UPDATE worlds SET updated_at = updated_at WHERE id = ?", (self.world_id,)
            )
        return super().generate(prompt=prompt, reference_images=reference_images)


def _photo_world(database: Database) -> tuple[str, str]:
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection, name="拍照测试世界", minutes_per_tick=60, seed_demo=True
        )
        snapshot = repository.get_snapshot(connection, world_id)
        player = repository.create_player_character(
            connection,
            world_id=world_id,
            name="镜中旅人",
            identity="地图师",
            location_id=snapshot.locations[0].id,
            traits=["好奇"],
            goal="记录地貌",
        )
        connection.execute(
            """
            INSERT INTO map_features(
                id, world_id, name, feature_type, longitude, latitude, location_id,
                is_known, metadata_json, created_at, updated_at
            ) VALUES ('photo-north-tower', ?, '北境古塔', '遗迹', ?, ?, ?, 1, '{}', ?, ?)
            """,
            (
                world_id,
                player.longitude,
                player.latitude + 0.02,
                player.location_id,
                "2040-04-01T08:00:00+00:00",
                "2040-04-01T08:00:00+00:00",
            ),
        )
        return world_id, player.id


def test_photo_uses_spatial_context_and_portrait_reference(settings, tmp_path) -> None:
    configured = replace(
        settings,
        media_directory=tmp_path / "media",
        image_api_key="test-key",
        image_model="seedream5.0lite",
    )
    database = Database(configured.database_path)
    database.initialize()
    world_id, player_id = _photo_world(database)
    fake = _FakeImageClient()
    service = PhotoService(configured, image_client=fake)
    data_url = "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
    with database.write() as connection:
        portrait = service.upload_portrait(
            connection,
            world_id=world_id,
            character_id=player_id,
            payload=PortraitUploadRequest(data_url=data_url),
        )
    with database.read() as connection:
        prepared = service.prepare_capture(
            connection,
            world_id=world_id,
            request=PhotoCaptureRequest(
                photographer_character_id=player_id,
                direction="north",
                include_self=True,
                include_nearby_npcs=False,
            ),
        )
    generated = service.generate_capture(prepared)
    with database.write() as connection:
        capture = service.finalize_capture(
            connection,
            prepared=prepared,
            generated=generated,
        )
        captures = service.list_captures(connection, world_id=world_id)

    prompt, references = fake.calls[0]
    assert portrait.url.startswith("/world-media/")
    assert "镜头明确朝向北方" in prompt
    assert "初春的上午" in prompt
    assert "北境古塔" in prompt
    assert "未加载可用的高程地形数据" in prompt
    assert len(references) == 1 and references[0].startswith("data:image/png;base64,")
    assert capture.image_url.startswith("/world-media/")
    assert (configured.media_directory / capture.image_url.removeprefix("/world-media/")).is_file()
    assert captures[0].id == capture.id


def test_photo_generation_happens_outside_write_transaction(settings, tmp_path) -> None:
    configured = replace(
        settings,
        media_directory=tmp_path / "media",
        image_api_key="test-key",
    )
    database = Database(configured.database_path)
    database.initialize()
    world_id, player_id = _photo_world(database)
    client = _WriteCheckingImageClient(database, world_id)
    service = PhotoService(configured, image_client=client)
    with database.read() as connection:
        prepared = service.prepare_capture(
            connection,
            world_id=world_id,
            request=PhotoCaptureRequest(photographer_character_id=player_id),
        )
    generated = service.generate_capture(prepared)
    with database.write() as connection:
        capture = service.finalize_capture(
            connection,
            prepared=prepared,
            generated=generated,
        )

    assert capture.id
    assert client.calls


def test_photo_api_generates_without_holding_database_writer(settings, tmp_path) -> None:
    configured = replace(
        settings,
        media_directory=tmp_path / "media",
        image_api_key="test-key",
    )
    database = Database(configured.database_path)
    database.initialize()
    world_id, player_id = _photo_world(database)
    client = _WriteCheckingImageClient(database, world_id)
    service = PhotoService(configured, image_client=client)
    with TestClient(create_app(configured, photo_service_override=service)) as api_client:
        response = api_client.post(
            f"/api/worlds/{world_id}/photos",
            json={
                "photographer_character_id": player_id,
                "direction": "north",
                "include_self": True,
                "include_nearby_npcs": False,
            },
        )

    assert response.status_code == 201
    assert response.json()["context"]["time"]["day_period"]
    assert client.calls


def test_photo_prompt_uses_landform_biome_and_world_time() -> None:
    terrain = {
        "surface_type": "forest",
        "biome": "森林",
        "elevation_m": 1250,
        "slope_degrees": 2.8,
        "water_kind": None,
    }
    time_context = PhotoService._time_context("2040-04-01T18:00:00+00:00")
    context = {
        "world_name": "照片上下文世界",
        "world_time": "2040-04-01T18:00:00+00:00",
        "time": time_context,
        "camera": {
            "direction_label": "东方",
            "location_name": "雾杉岭",
            "location_kind": "荒野",
            "longitude": 10.0,
            "latitude": 20.0,
        },
        "terrain": {
            "available": True,
            **terrain,
            "landform": PhotoService._landform_from_terrain(terrain),
        },
        "landmarks": [],
        "characters": [],
    }

    prompt = PhotoService._build_prompt(context, "")

    assert context["terrain"]["landform"] == "高地丘陵"
    assert time_context["day_period"] == "黄昏"
    assert "高地丘陵地貌，森林生物群系" in prompt
    assert "初春的黄昏" in prompt


def test_portrait_upload_api_serves_media(settings, tmp_path) -> None:
    configured = replace(settings, media_directory=tmp_path / "media")
    database = Database(configured.database_path)
    database.initialize()
    world_id, player_id = _photo_world(database)
    data_url = "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
    with TestClient(create_app(configured)) as client:
        response = client.put(
            f"/api/worlds/{world_id}/characters/{player_id}/portrait",
            json={"data_url": data_url},
        )
        media = client.get(response.json()["url"])
        snapshot = client.get(f"/api/worlds/{world_id}")

    assert response.status_code == 200
    assert media.status_code == 200
    character = next(item for item in snapshot.json()["characters"] if item["id"] == player_id)
    assert character["portrait_url"] == response.json()["url"]
