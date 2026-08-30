from __future__ import annotations

from world_engine.navigation import TerrainService
from world_engine.routing import RoutePlanner


def _cell(
    cell_id: int,
    longitude: float,
    latitude: float,
    *,
    surface: str = "grassland",
    water: str | None = None,
    slope: float = 0.0,
    neighbors: list[int] | None = None,
    road_ids: list[str] | None = None,
) -> dict[str, object]:
    return {
        "cell_id": cell_id,
        "longitude": longitude,
        "latitude": latitude,
        "surface_type": surface,
        "water_kind": water,
        "slope_degrees": slope,
        "elevation_m": 100,
        "elevation_code": 30,
        "state_id": 1,
        "province_id": 1,
        "neighbors": neighbors or [],
        "road_ids": road_ids or [],
        "river_ids": [],
    }


def _crossing(
    identifier: str, name: str, crossing_type: str, longitude: float, latitude: float
) -> dict[str, object]:
    return {
        "type": "Feature",
        "id": identifier,
        "properties": {"name": name, "crossing_type": crossing_type},
        "geometry": {"type": "Point", "coordinates": [longitude, latitude]},
    }


def test_land_cannot_cross_ocean_but_flight_and_ship_can() -> None:
    cells = {
        0: _cell(0, 0.0, 0.0, surface="marine", water="ocean", neighbors=[1]),
        1: _cell(1, 0.5, 0.0, surface="marine", water="ocean", neighbors=[0, 2]),
        2: _cell(2, 1.0, 0.0, surface="marine", water="ocean", neighbors=[1]),
    }
    terrain = TerrainService.from_memory(cells, [])
    planner = RoutePlanner()

    land = planner.plan_on(terrain, origin=(0.0, 0.0), destination=(1.0, 0.0), movement_type="land")
    assert not land.reachable
    assert "外海" in (land.unreachable_reason or "")

    flight = planner.plan_on(
        terrain,
        origin=(0.0, 0.0),
        destination=(1.0, 0.0),
        movement_type="flight",
        vehicle_metadata={"speed_kmh": 100.0},
    )
    assert flight.reachable
    assert flight.distance_km > 0

    ship = planner.plan_on(
        terrain,
        origin=(0.0, 0.0),
        destination=(1.0, 0.0),
        movement_type="ship",
        vehicle_metadata={"speed_kmh": 40.0},
    )
    assert ship.reachable is True


def test_ship_cannot_start_on_land() -> None:
    cells = {0: _cell(0, 0.0, 0.0), 1: _cell(1, 0.3, 0.0, neighbors=[0])}
    terrain = TerrainService.from_memory(cells, [])
    plan = RoutePlanner().plan_on(
        terrain, origin=(0.0, 0.0), destination=(0.3, 0.0), movement_type="ship"
    )
    assert not plan.reachable
    assert "陆地" in (plan.unreachable_reason or "")


def test_land_cannot_cross_river_without_crossing() -> None:
    cells = {
        0: _cell(0, 0.0, 0.0, neighbors=[1]),
        1: _cell(1, 0.3, 0.0, water="river", neighbors=[0, 2]),
        2: _cell(2, 0.6, 0.0, neighbors=[1]),
    }
    planner = RoutePlanner()

    without = TerrainService.from_memory(cells, [])
    plan = planner.plan_on(without, origin=(0.0, 0.0), destination=(0.6, 0.0), movement_type="land")
    assert not plan.reachable

    with_crossing = TerrainService.from_memory(
        cells, [_crossing("bridge1", "北岸桥", "bridge", 0.3, 0.0)]
    )
    plan_bridge = planner.plan_on(
        with_crossing, origin=(0.0, 0.0), destination=(0.6, 0.0), movement_type="land"
    )
    assert plan_bridge.reachable
    assert plan_bridge.cell_ids == [0, 1, 2]
    assert any("北岸桥" in requirement for requirement in plan_bridge.requirements)


def test_steep_slope_requires_mountain_pass() -> None:
    cells = {
        0: _cell(0, 0.0, 0.0, neighbors=[1]),
        1: _cell(1, 0.3, 0.0, slope=40.0, neighbors=[0, 2]),
        2: _cell(2, 0.6, 0.0, neighbors=[1]),
    }
    planner = RoutePlanner()

    without = TerrainService.from_memory(cells, [])
    plan = planner.plan_on(without, origin=(0.0, 0.0), destination=(0.6, 0.0), movement_type="land")
    assert not plan.reachable

    with_pass = TerrainService.from_memory(
        cells, [_crossing("pass1", "高山山口", "mountain_pass", 0.3, 0.0)]
    )
    plan_pass = planner.plan_on(
        with_pass, origin=(0.0, 0.0), destination=(0.6, 0.0), movement_type="land"
    )
    assert plan_pass.reachable
    assert any("高山山口" in requirement for requirement in plan_pass.requirements)


def _diamond(with_road: bool) -> TerrainService:
    cells = {
        0: _cell(0, 0.0, 0.0, neighbors=[1, 3]),
        1: _cell(
            1,
            0.3,
            0.0,
            neighbors=[0, 2],
            road_ids=["route:roads:1"] if with_road else [],
        ),
        2: _cell(
            2,
            0.6,
            0.0,
            neighbors=[1, 4],
            road_ids=["route:roads:1"] if with_road else [],
        ),
        3: _cell(
            3,
            0.3,
            0.2,
            neighbors=[0, 4],
            road_ids=["route:trails:2"] if with_road else [],
        ),
        4: _cell(4, 0.9, 0.0, neighbors=[2, 3]),
    }
    return TerrainService.from_memory(cells, [])


def test_road_route_is_faster_than_wilderness() -> None:
    planner = RoutePlanner()
    with_road = planner.plan_on(
        _diamond(True),
        origin=(0.0, 0.0),
        destination=(0.9, 0.0),
        movement_type="land",
        vehicle_metadata={"speed_kmh": 5.0},
    )
    without_road = planner.plan_on(
        _diamond(False),
        origin=(0.0, 0.0),
        destination=(0.9, 0.0),
        movement_type="land",
        vehicle_metadata={"speed_kmh": 5.0},
    )
    assert with_road.reachable and without_road.reachable
    assert with_road.estimated_hours < without_road.estimated_hours
    assert any(segment["speed_kmh"] > 5.0 for segment in with_road.segments)
