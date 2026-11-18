"""哥哥，核准折线参与真实规划，两端连接和保存的来源仍需逐段成立。"""

import json
from datetime import timedelta

import pytest

from world_engine.geo import great_circle_distance_km
from world_engine.local_routes import LocalRouteService, geometry_sha256
from world_engine.movement import MovementService
from world_engine.navigation import TerrainService
from world_engine.repository import WorldRepository, from_iso, to_iso, utc_now
from world_engine.routing import RoutePlanner


def setup_route(database, monkeypatch, *, river=True, bidirectional=True):
    repo = WorldRepository()
    with database.write() as c:
        wid = repo.create_world(c, name="局部路线隔离世界", minutes_per_tick=60, seed_demo=True)
        snapshot = repo.get_snapshot(c, wid)
        player = repo.create_player_character(c, world_id=wid, name="循路人", identity="测绘员",
                                             location_id=snapshot.locations[0].id, traits=[], goal="沿真实折线行走")
        c.execute("UPDATE characters SET longitude=0,latitude=0 WHERE id=?", (player.id,))
        c.execute("INSERT INTO navigation_datasets VALUES ('local-dataset',?,?,'unused','test','approved','{}',?,?)",
                  (wid, "隔离地图", snapshot.world.current_time.isoformat(), snapshot.world.current_time.isoformat()))
    points = [[0.0, 0.0], [0.002, 0.001], [0.004, 0.0]]
    record = {"id": "local-path", "world_id": wid, "dataset_id": "local-dataset", "name": "核实岸边通路",
              "polyline": points, "allowed_modes": ["land"], "bidirectional": bidirectional,
              "speed_limit_kmh": 5.0, "geometry_sha256": geometry_sha256(points), "revision": 1, "is_open": True}
    terrain = TerrainService.from_memory({1: {"cell_id": 1, "longitude": 0.0, "latitude": 0.0,
                                                "water_kind": "river" if river else None}})
    monkeypatch.setattr("world_engine.routing.load_terrain", lambda root: terrain)
    monkeypatch.setattr(RoutePlanner, "_approved_dataset", lambda self, c, world: {
        "id": "local-dataset", "asset_root": "unused", "review_status": "approved", "version": "test"})
    monkeypatch.setattr(LocalRouteService, "available", staticmethod(lambda c, world, dataset, mode: [record] if record["is_open"] else []))
    monkeypatch.setattr(LocalRouteService, "get", staticmethod(lambda c, world, route_id: record))
    return wid, player.id, snapshot.world.current_time, record


def test_registered_local_path_crosses_only_its_exact_geometry(database, monkeypatch):
    wid, _, _, record = setup_route(database, monkeypatch)
    with database.read() as c:
        plan = RoutePlanner().plan(c, world_id=wid, origin=(0.0, 0.0), destination=(0.004, 0.0), movement_type="land")
    assert plan.reachable and plan.polyline == record["polyline"]
    assert plan.distance_km > great_circle_distance_km(0.0, 0.0, 0.004, 0.0)
    saved = plan.as_dict()
    assert saved["local_routes"] == [{"id": record["id"], "revision": 1,
                                     "geometry_sha256": record["geometry_sha256"], "direction": "forward"}]
    assert [edge["local_route_segment_index"] for edge in plan.edges] == [0, 1]


def test_local_path_does_not_invent_an_unverified_entry_connection(database, monkeypatch):
    wid, _, _, _ = setup_route(database, monkeypatch)
    with database.read() as c:
        plan = RoutePlanner().plan(c, world_id=wid, origin=(-0.001, 0.0), destination=(0.004, 0.0), movement_type="land")
    assert not plan.reachable


@pytest.mark.parametrize("bidirectional,reachable", [(True, True), (False, False)])
def test_reverse_requires_explicit_direction_permission(database, monkeypatch, bidirectional, reachable):
    wid, _, _, record = setup_route(database, monkeypatch, bidirectional=bidirectional)
    with database.read() as c:
        plan = RoutePlanner().plan(c, world_id=wid, origin=(0.004, 0.0), destination=(0.0, 0.0), movement_type="land")
    assert plan.reachable == reachable
    if reachable:
        assert plan.polyline == list(reversed(record["polyline"]))
        assert [edge["local_route_segment_index"] for edge in plan.edges] == [1, 0]


def test_faster_valid_terrain_route_remains_preferred(database, monkeypatch):
    wid, _, _, _ = setup_route(database, monkeypatch, river=False)
    with database.read() as c:
        plan = RoutePlanner().plan(c, world_id=wid, origin=(0.0, 0.0), destination=(0.004, 0.0), movement_type="land")
    assert plan.reachable and "local_routes" not in plan.as_dict()


@pytest.mark.parametrize("corruption", ["root", "edge", "segment", "revision", "geometry",
                                       "root_id", "root_revision", "root_geometry_sha256", "root_direction",
                                       "global_binding_removed", "global_and_root_removed"])
def test_saved_local_binding_cannot_be_partly_removed_or_replaced(database, monkeypatch, corruption):
    wid, player, at, record = setup_route(database, monkeypatch)
    with database.write() as c:
        movement = MovementService().start(c, world_id=wid, character_id=player,
                                           destination_longitude=0.004, destination_latitude=0.0, world_time=at)
        route = json.loads(c.execute("SELECT route_json FROM character_movements WHERE id=?", (movement.id,)).fetchone()[0])
        if corruption == "root":
            route.pop("local_routes")
        elif corruption.startswith("root_"):
            route["local_routes"][0].pop(corruption.removeprefix("root_"))
        elif corruption == "edge":
            route["edges"][0].pop("local_route_id")
        elif corruption == "segment":
            route["edges"][0]["local_route_segment_index"] = 1
        elif corruption == "revision":
            record["revision"] = 2
        elif corruption in {"global_binding_removed", "global_and_root_removed"}:
            route.pop("dataset_id")
            if corruption == "global_and_root_removed":
                route.pop("local_routes")
            c.execute("UPDATE character_movements SET navigation_dataset_id=NULL,route_distance_km=0 WHERE id=?", (movement.id,))
        else:
            record["polyline"][1] = [0.002, -0.001]
        c.execute("UPDATE character_movements SET route_json=? WHERE id=?", (json.dumps(route), movement.id))
        MovementService().advance(c, world_id=wid, previous_time=at, current_time=at + timedelta(hours=1), created_at=utc_now())
        person = c.execute("SELECT longitude,latitude FROM characters WHERE id=?", (player,)).fetchone()
        assert tuple(person) == (0.0, 0.0)
        assert c.execute("SELECT status FROM character_movements WHERE id=?", (movement.id,)).fetchone()[0] == "cancelled"


@pytest.mark.parametrize("closed_seconds,expected_status", [(30, "cancelled"), (200, "cancelled"), (600, "arrived")])
def test_closure_applies_only_when_it_coincides_with_actual_path_occupancy(database, monkeypatch, closed_seconds, expected_status):
    wid, player, at, record = setup_route(database, monkeypatch)
    with database.write() as c:
        movement = MovementService().start(c, world_id=wid, character_id=player,
                                           destination_longitude=0.004, destination_latitude=0.0, world_time=at)
        record["is_open"] = False
        record["state_changed_at"] = to_iso(at + timedelta(seconds=closed_seconds))
        MovementService().advance(c, world_id=wid, previous_time=at, current_time=at + timedelta(hours=1), created_at=utc_now())
        saved = c.execute("SELECT * FROM character_movements WHERE id=?", (movement.id,)).fetchone()
        assert saved["status"] == expected_status
        if expected_status == "cancelled":
            assert saved["distance_travelled_km"] == pytest.approx(5.0 * closed_seconds / 3600)
            event = c.execute("SELECT occurred_at FROM world_events WHERE world_id=? AND actor_id=? "
                              "AND event_type='action.movement_cancelled' ORDER BY rowid DESC LIMIT 1", (wid, player)).fetchone()
            assert from_iso(event[0]) == at + timedelta(seconds=closed_seconds)
        else:
            assert saved["distance_travelled_km"] == pytest.approx(saved["total_distance_km"])


def test_ground_local_path_stops_if_actor_has_changed_space_layer(database, monkeypatch):
    wid, player, at, _ = setup_route(database, monkeypatch)
    with database.write() as c:
        movement = MovementService().start(c, world_id=wid, character_id=player,
                                           destination_longitude=0.004, destination_latitude=0.0, world_time=at)
        c.execute("UPDATE characters SET space_layer='sky' WHERE id=?", (player,))
        MovementService().advance(c, world_id=wid, previous_time=at, current_time=at + timedelta(hours=1), created_at=utc_now())
        person = c.execute("SELECT longitude,latitude FROM characters WHERE id=?", (player,)).fetchone()
        assert tuple(person) == (0.0, 0.0)
        assert c.execute("SELECT status FROM character_movements WHERE id=?", (movement.id,)).fetchone()[0] == "cancelled"
