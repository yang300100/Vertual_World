from __future__ import annotations

from datetime import UTC, datetime, timedelta

from world_engine.actions import ActionService
from world_engine.domain import ActionProposal, ActionType
from world_engine.movement import MovementService
from world_engine.repository import WorldRepository


def test_world_map_is_single_noryia_global_map(database) -> None:
    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection,
            name="坐标测试世界",
            minutes_per_tick=60,
            seed_demo=True,
        )
        snapshot = repository.get_snapshot(connection, world_id)

    # 世界地图只剩一张 Noryia 全球等距圆柱总览；历史 8×8 瓦片资源已删除。
    world_maps = [item for item in snapshot.maps if item.map_role == "world"]
    assert len(world_maps) == 1
    master = world_maps[0]
    assert master.zoom_level == 0
    assert master.asset_path == "map_new/Noryia.svg"
    assert (master.min_longitude, master.max_longitude) == (-180, 180)
    assert (master.min_latitude, master.max_latitude) == (-90, 90)
    assert master.width_pixels == 10015
    assert master.height_pixels == 5008
    # 不再登记任何破损的瓦片地图。
    assert not any(item.zoom_level == 1 for item in snapshot.maps)
    # 世界地图范围必须覆盖所有地点坐标，保证标记能投影到图上。
    assert all(
        master.min_longitude <= item.longitude <= master.max_longitude
        for item in snapshot.locations
    )
    assert all(
        master.min_latitude <= item.latitude <= master.max_latitude
        for item in snapshot.locations
    )


def test_travel_progresses_character_world_coordinate_over_time(database) -> None:
    repository = WorldRepository()
    service = ActionService()
    with database.write() as connection:
        world_id = repository.create_world(
            connection,
            name="旅行坐标测试世界",
            minutes_per_tick=60,
            seed_demo=True,
        )
        before = repository.get_snapshot(connection, world_id)
        origin, destination = before.locations[:2]
        player = repository.create_player_character(
            connection,
            world_id=world_id,
            name="坐标旅人",
            identity="测绘师",
            location_id=origin.id,
            traits=[],
            goal="绘制地图",
        )
        started_at = datetime.now(UTC)
        outcome = service.execute(
            connection,
            world_id=world_id,
            tick_id="spatial-test",
            occurred_at=started_at,
            proposal=ActionProposal(
                actor_id=player.id,
                action=ActionType.TRAVEL,
                destination_id=destination.id,
                reason="验证旅行坐标同步",
            ),
        )
        after_start = repository.get_snapshot(connection, world_id)
        movement = after_start.movements[0]

        half_duration = timedelta(hours=movement.total_distance_km / movement.speed_kmh / 2)
        MovementService().advance(
            connection,
            world_id=world_id,
            previous_time=started_at,
            current_time=started_at + half_duration,
            created_at=started_at,
        )
        halfway = repository.get_snapshot(connection, world_id).character_by_id(player.id)
        MovementService().advance(
            connection,
            world_id=world_id,
            previous_time=started_at + half_duration,
            current_time=started_at + half_duration * 2 + timedelta(seconds=1),
            created_at=started_at,
        )
        arrived = repository.get_snapshot(connection, world_id).character_by_id(player.id)

    not_teleported = after_start.character_by_id(player.id)
    assert outcome.accepted is True
    assert not_teleported is not None
    assert not_teleported.location_id == origin.id
    assert not_teleported.longitude == origin.longitude
    assert not_teleported.latitude == origin.latitude
    assert halfway is not None
    assert halfway.longitude not in {origin.longitude, destination.longitude}
    assert halfway.current_location_id is None
    assert arrived is not None
    assert arrived.location_id == destination.id
    assert arrived.current_location_id == destination.id
    assert arrived.longitude == destination.longitude
    assert arrived.latitude == destination.latitude


def test_formal_world_exposes_known_map_features(database) -> None:
    from world_engine.seeder import create_iserra_world

    world_id = create_iserra_world(database)
    # 重启初始化必须幂等，不能把同一建筑/地标重复登记。
    database.initialize()
    with database.read() as connection:
        snapshot = WorldRepository().get_snapshot(connection, world_id)

    assert len(snapshot.map_features) == 4
    assert {item.feature_type for item in snapshot.map_features} >= {
        "building",
        "landmark",
        "district",
    }
    assert all(-180 <= item.longitude <= 180 for item in snapshot.map_features)
    assert all(-90 <= item.latitude <= 90 for item in snapshot.map_features)
