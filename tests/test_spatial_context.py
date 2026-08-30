from __future__ import annotations

import json
import struct
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from world_engine.activation import NPCActivationService
from world_engine.engine import WorldEngine
from world_engine.repository import WorldRepository
from world_engine.seeder import create_iserra_world
from world_engine.spatial import SpatialContextService

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DETAIL_ROOT = (
    PROJECT_ROOT
    / "docs"
    / "worldbuilding"
    / "maps"
    / "navigation"
    / "settlements"
    / "oathflow"
)


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", data[16:24])


def test_location_context_prefers_inner_area_then_city(database) -> None:
    world_id = create_iserra_world(database)
    service = SpatialContextService()
    with database.read() as connection:
        granary = connection.execute(
            "SELECT * FROM locations WHERE world_id = ? AND name = '河畔大粮仓'",
            (world_id,),
        ).fetchone()
        city = connection.execute(
            "SELECT * FROM locations WHERE world_id = ? AND name = '澜誓城'",
            (world_id,),
        ).fetchone()
        at_granary = service.resolve_location(
            connection,
            world_id=world_id,
            longitude=granary["longitude"],
            latitude=granary["latitude"],
        )
        at_city = service.resolve_location(
            connection,
            world_id=world_id,
            longitude=city["longitude"],
            latitude=city["latitude"],
        )
        outside = service.resolve_location(
            connection,
            world_id=world_id,
            longitude=city["longitude"] + 1,
            latitude=city["latitude"],
        )

    assert at_granary is not None and at_granary["id"] == granary["id"]
    assert at_city is not None and at_city["id"] == city["id"]
    assert granary["parent_location_id"] == city["id"]
    assert outside is None


def test_oathflow_detail_map_is_bound_to_city(database) -> None:
    world_id = create_iserra_world(database)
    with database.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)
    city = next(item for item in snapshot.locations if item.name == "澜誓城")
    detail = next(item for item in snapshot.maps if item.map_role == "detail")

    assert detail.location_id == city.id
    assert detail.asset_path == "navigation/settlements/oathflow/detail-map.svg"
    assert detail.min_longitude < city.longitude < detail.max_longitude
    assert detail.min_latitude < city.latitude < detail.max_latitude
    assert detail.review_status == "candidate"


def test_background_npc_can_be_distance_and_interaction_activated(
    database, settings
) -> None:
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection,
            name="NPC激活测试世界",
            minutes_per_tick=60,
            seed_demo=True,
        )
        snapshot = repository.get_snapshot(connection, world_id)
        square = next(item for item in snapshot.locations if item.name == "晨星广场")
        player = repository.create_player_character(
            connection,
            world_id=world_id,
            name="观察者",
            identity="旅人",
            location_id=square.id,
            traits=[],
            goal="观察NPC",
        )
        background = next(
            item
            for item in snapshot.characters
            if item.activation_policy == "distance"
        )
        connection.execute(
            """
            UPDATE characters
            SET longitude = ?, latitude = ?, current_location_id = ?,
                activation_probability = 1, activation_radius_km = 20,
                activation_state = 'background'
            WHERE id = ?
            """,
            (player.longitude, player.latitude, square.id, background.id),
        )
        NPCActivationService().refresh(
            connection,
            world_id=world_id,
            world_time=datetime(2040, 4, 1, 8, 0, tzinfo=UTC),
        )
        activated = repository.get_snapshot(connection, world_id).character_by_id(
            background.id
        )
    assert activated is not None
    assert activated.activation_state == "active"
    assert activated.activation_reason == "distance"

    with database.write() as connection:
        connection.execute(
            """
            UPDATE characters
            SET longitude = longitude + 2, activation_state = 'background'
            WHERE id = ?
            """,
            (background.id,),
        )
        NPCActivationService().refresh(
            connection,
            world_id=world_id,
            world_time=datetime(2040, 4, 1, 8, 10, tzinfo=UTC),
        )
        far = repository.get_snapshot(connection, world_id).character_by_id(background.id)
    assert far is not None
    assert far.activation_state == "background"
    assert far.activation_reason == "out_of_range"

    with database.write() as connection:
        now = datetime.now(UTC).isoformat()
        connection.execute(
            """
            INSERT INTO relationships(
                world_id, source_character_id, target_character_id,
                affinity, trust, updated_at
            ) VALUES (?, ?, ?, 70, 60, ?)
            """,
            (world_id, player.id, background.id, now),
        )
        NPCActivationService().refresh(
            connection,
            world_id=world_id,
            world_time=datetime(2040, 4, 1, 8, 20, tzinfo=UTC),
        )
        relationship_active = repository.get_snapshot(
            connection, world_id
        ).character_by_id(background.id)
        connection.execute(
            "DELETE FROM relationships WHERE world_id = ? AND target_character_id = ?",
            (world_id, background.id),
        )
    assert relationship_active is not None
    assert relationship_active.activation_state == "active"
    assert relationship_active.activation_reason == "relationship"

    with database.write() as connection:
        connection.execute(
            """
            UPDATE characters
            SET longitude = ?, latitude = ?, current_location_id = ?,
                activation_state = 'background'
            WHERE id = ?
            """,
            (player.longitude, player.latitude, square.id, background.id),
        )
    WorldEngine(database, settings).submit_player_intent(
        world_id, f"与{background.name}交谈"
    )
    with database.read() as connection:
        after_dialogue = repository.get_snapshot(connection, world_id).character_by_id(
            background.id
        )
    assert after_dialogue is not None
    assert after_dialogue.activation_state == "active"
    assert after_dialogue.activation_reason == "interaction"
    assert after_dialogue.activation_until_world_time is not None


def test_persistent_npc_stays_active_outside_distance(database) -> None:
    world_id = create_iserra_world(database)
    repository = WorldRepository()
    with database.write() as connection:
        snapshot = repository.get_snapshot(connection, world_id)
        repository.create_player_character(
            connection,
            world_id=world_id,
            name="持久激活观察者",
            identity="旅人",
            location_id=snapshot.locations[0].id,
            traits=[],
            goal="观察特殊人物",
        )
        persistent = next(
            item
            for item in snapshot.characters
            if item.activation_policy == "persistent" and not item.is_player
        )
        connection.execute(
            "UPDATE characters SET longitude = 170, latitude = -70 WHERE id = ?",
            (persistent.id,),
        )
        NPCActivationService().refresh(
            connection,
            world_id=world_id,
            world_time=snapshot.world.current_time,
        )
        refreshed = repository.get_snapshot(connection, world_id).character_by_id(
            persistent.id
        )
    assert refreshed is not None
    assert refreshed.activation_state == "active"
    assert refreshed.activation_reason == "persistent"


def test_background_npc_remains_visible_but_does_not_act(database, settings) -> None:
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection,
            name="背景NPC测试世界",
            minutes_per_tick=60,
            seed_demo=True,
        )
        before = repository.get_snapshot(connection, world_id)
    background_ids = {
        item.id for item in before.characters if item.activation_state == "background"
    }
    result = WorldEngine(database, settings).tick(world_id)
    acted_ids = {item.actor_id for item in result.outcomes}

    assert background_ids
    assert background_ids.isdisjoint(acted_ids)
    with database.read() as connection:
        visible_ids = {
            item.id
            for item in repository.get_snapshot(connection, world_id).characters
        }
    assert background_ids <= visible_ids


def test_oathflow_detail_assets_are_complete() -> None:
    metadata = json.loads((DETAIL_ROOT / "metadata.json").read_text(encoding="utf-8"))
    features = json.loads((DETAIL_ROOT / "features.geojson").read_text(encoding="utf-8"))
    roads = json.loads((DETAIL_ROOT / "roads.geojson").read_text(encoding="utf-8"))
    rivers = json.loads((DETAIL_ROOT / "rivers.geojson").read_text(encoding="utf-8"))

    assert metadata["review_status"] == "candidate"
    assert _png_size(DETAIL_ROOT / "detail-map.png") == (1600, 1600)
    svg = (DETAIL_ROOT / "detail-map.svg").read_text(encoding="utf-8")
    ET.fromstring(svg)
    assert '<image href="detail-map-ai-base.png"' in svg
    assert '<g id="pois"' in svg
    assert len(features["features"]) == 8
    assert len(roads["features"]) == 12
    assert len(rivers["features"]) == 4
    assert metadata["building_count"] >= 150
    assert metadata["visual_source"] == "image_generation_plus_vector_overlay"
    assert len(metadata["ai_base_sha256"]) == 64
    names = {item["properties"]["name"] for item in features["features"]}
    assert {"河务档案区", "王室堤岸", "内河码头", "外来商旅区"} <= names
