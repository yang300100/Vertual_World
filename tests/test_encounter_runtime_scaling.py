"""哥哥，邻域查询和地形复用必须保持交互、空间边界与路线规则。"""

import json

import pytest
from test_lived_world import town  # noqa: F401

from world_engine.event_log import record_event
from world_engine.navigation import TerrainService, load_terrain
from world_engine.routing import RoutePlanner

# ruff: noqa: F811


@pytest.mark.parametrize(
    "origin,nearby",
    [((179.9997, 0), (-179.9997, 0)), ((0, 89.9999), (90, 89.9999)), ((80, 25), (80.0005, 25))],
)
def test_witness_prefilter_keeps_real_neighbors_across_seam_and_pole(town, origin, nearby):
    e = town
    with e.db.write() as c:
        c.execute("UPDATE characters SET longitude=?,latitude=? WHERE id=?", (*origin, e.player))
        c.execute("UPDATE characters SET longitude=?,latitude=? WHERE id=?", (*nearby, e.npc))
        event = record_event(
            c,
            world_id=e.wid,
            tick_id="witness-test",
            occurred_at=e.at,
            event_type="action.travel",
            actor_id=e.player,
            summary="经过身边",
        )
        assert c.execute(
            "SELECT 1 FROM event_observers WHERE event_id=? AND character_id=?", (event, e.npc)
        ).fetchone()
        c.execute("UPDATE characters SET space_layer='upper' WHERE id=?", (e.npc,))
        other = record_event(
            c,
            world_id=e.wid,
            tick_id="witness-test",
            occurred_at=e.at,
            event_type="action.travel",
            actor_id=e.player,
            summary="留在原空间层",
        )
        assert not c.execute(
            "SELECT 1 FROM event_observers WHERE event_id=? AND character_id=?", (other, e.npc)
        ).fetchone()


def terrain_files(root):
    cells = {
        i: {
            "cell_id": i,
            "longitude": i * 0.01,
            "latitude": 0,
            "neighbors": [n for n in (i - 1, i + 1) if 0 <= n <= 2],
            "biome": "grassland",
        }
        for i in range(3)
    }
    terrain = TerrainService.from_memory(cells)
    index = {
        "bin_degrees": terrain.bin_degrees,
        "bounds": terrain.bounds,
        "cells": [
            {"id": i, "longitude": c["longitude"], "latitude": c["latitude"]}
            for i, c in terrain._cells.items()
        ],
        "bins": terrain._bins,
    }
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [c["longitude"], c["latitude"]]},
            "properties": c,
        }
        for c in terrain._cells.values()
    ]
    for name, value in (
        ("terrain-index.json", index),
        ("terrain.geojson", {"type": "FeatureCollection", "features": features}),
        ("crossings.geojson", {"type": "FeatureCollection", "features": []}),
        ("movement-rules.json", terrain.rules),
    ):
        (root / name).write_text(json.dumps(value), encoding="utf-8")


def test_shared_terrain_preserves_each_travelers_speed_and_refreshes_changed_water(tmp_path):
    terrain_files(tmp_path)
    planner = RoutePlanner()
    plans = [
        planner.plan_on(
            load_terrain(tmp_path),
            origin=(0.001, 0),
            destination=(0.019, 0),
            movement_type="land",
            vehicle_metadata={"speed_kmh": speed},
        )
        for speed in (5, 3)
    ]
    assert all(p.reachable for p in plans)
    assert plans[0].polyline == plans[1].polyline
    assert plans[0].estimated_hours < plans[1].estimated_hours
    data = json.loads((tmp_path / "terrain.geojson").read_text(encoding="utf-8"))
    data["features"][1]["properties"]["water_kind"] = "lake"
    (tmp_path / "terrain.geojson").write_text(json.dumps(data), encoding="utf-8")
    changed = planner.plan_on(
        load_terrain(tmp_path),
        origin=(0.001, 0),
        destination=(0.019, 0),
        movement_type="land",
        vehicle_metadata={"speed_kmh": 5},
    )
    assert not changed.reachable


def test_same_land_cell_walk_does_not_detour_through_cell_center():
    from world_engine.geo import great_circle_distance_km

    terrain = TerrainService.from_memory(
        {
            1: {
                "cell_id": 1,
                "longitude": 80.7863,
                "latitude": 25.44261,
                "neighbors": [],
                "biome": "grassland",
            },
        }
    )
    origin, destination = (80.7904, 25.4397), (80.7908, 25.44)
    plan = RoutePlanner().plan_on(
        terrain,
        origin=origin,
        destination=destination,
        movement_type="land",
        vehicle_metadata={"speed_kmh": 5},
    )
    assert plan.reachable
    assert plan.polyline == [list(origin), list(destination)]
    assert plan.distance_km == pytest.approx(
        great_circle_distance_km(*origin, *destination), abs=0.001
    )
    terrain._cells[1]["water_kind"] = "lake"
    blocked = RoutePlanner().plan_on(
        terrain,
        origin=origin,
        destination=destination,
        movement_type="land",
        vehicle_metadata={"speed_kmh": 5},
    )
    assert not blocked.reachable
