from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSET_ROOT = (
    PROJECT_ROOT
    / "docs"
    / "worldbuilding"
    / "maps"
    / "navigation"
    / "northern-main-continent"
)


def _json(name: str) -> dict[str, object]:
    return json.loads((ASSET_ROOT / name).read_text(encoding="utf-8"))


def _png_size(name: str) -> tuple[int, int]:
    data = (ASSET_ROOT / name).read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_candidate_navigation_assets_are_complete_and_aligned() -> None:
    expected = {
        "metadata.json",
        "elevation.png",
        "water-mask.png",
        "slope.png",
        "surface.png",
        "waterways.geojson",
        "roads.geojson",
        "crossings.geojson",
        "cities.geojson",
        "route-graph.json",
        "movement-rules.json",
        "audit-overlay.png",
    }
    assert expected <= {path.name for path in ASSET_ROOT.iterdir() if path.is_file()}

    metadata = _json("metadata.json")
    assert metadata["review_status"] == "candidate"
    assert metadata["bounds"] == {
        "min_longitude": -170.0,
        "max_longitude": 0.0,
        "min_latitude": 0.0,
        "max_latitude": 72.0,
    }
    dimensions = (metadata["width"], metadata["height"])
    for name in (
        "elevation.png",
        "water-mask.png",
        "slope.png",
        "surface.png",
        "audit-overlay.png",
    ):
        assert _png_size(name) == dimensions

    source = PROJECT_ROOT / metadata["source"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == metadata["source_sha256"]
    statistics = metadata["statistics"]
    assert 0.35 < statistics["land_fraction"] < 0.7
    assert 0.25 < statistics["ocean_fraction"] < 0.65
    assert statistics["elevation_max_m"] >= 4000
    assert statistics["surface_cell_counts"]["mountain"] > 10_000


def test_candidate_route_graph_has_real_crossing_portals() -> None:
    waterways = _json("waterways.geojson")
    roads = _json("roads.geojson")
    crossings = _json("crossings.geojson")
    graph = _json("route-graph.json")
    rules = _json("movement-rules.json")

    assert len(waterways["features"]) == 4
    assert len(roads["features"]) == 3
    crossing_types = {
        item["properties"]["crossing_type"] for item in crossings["features"]
    }
    assert {"bridge", "bridge_and_ferry", "port", "mountain_pass"} <= crossing_types
    assert graph["review_status"] == "candidate"
    assert len(graph["nodes"]) == 5
    assert len(graph["edges"]) == 4

    nodes = {item["id"]: item for item in graph["nodes"]}
    for edge in graph["edges"]:
        assert edge["polyline"][0] == nodes[edge["source"]]["coordinates"]
        assert edge["polyline"][-1] == nodes[edge["target"]]["coordinates"]
        assert edge["distance_km"] > 0
        assert edge["speed_multiplier"] > 1

    assert rules["surface_speed_multipliers"]["ocean"] == 0
    assert rules["surface_speed_multipliers"]["grassland"] == 1
    assert rules["river_crossing_requires"] == ["bridge", "ferry", "ford"]
