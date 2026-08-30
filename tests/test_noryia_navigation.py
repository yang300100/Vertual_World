from __future__ import annotations

import hashlib
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "docs" / "worldbuilding" / "maps" / "map_new" / "data"
ASSET_ROOT = PROJECT_ROOT / "docs" / "worldbuilding" / "maps" / "navigation" / "noryia"
GENERATOR = PROJECT_ROOT / "scripts" / "generate_noryia_navigation_data.py"


def _json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_noryia_navigation_assets_are_complete() -> None:
    expected = {
        "metadata.json",
        "terrain.geojson",
        "terrain-index.json",
        "surface.geojson",
        "waterways.geojson",
        "roads.geojson",
        "crossings.geojson",
        "route-graph.json",
        "movement-rules.json",
    }
    assert expected <= {path.name for path in ASSET_ROOT.iterdir() if path.is_file()}


def test_noryia_navigation_style_assets_flag() -> None:
    """--skip-svg 产物不引用 svg；元数据仍带相对高度标记。"""
    metadata = _json(ASSET_ROOT / "metadata.json")
    assert metadata["style_assets"]["elevation_is_relative"] is True
    assert metadata["style_assets"]["height_mapping_pending_review"] is True
    # 若目录中存在生成的 svg 才允许引用；当前默认不生成 svg。
    assert (
        "terrain-base.svg" in metadata["style_assets"]
        or not (ASSET_ROOT / "terrain-base.svg").exists()
    )


def test_noryia_navigation_source_hash_matches() -> None:
    metadata = _json(ASSET_ROOT / "metadata.json")
    source = PROJECT_ROOT / str(metadata["source"])
    assert source.is_file()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == metadata["source_sha256"]


def test_noryia_navigation_snapshot_counts() -> None:
    metadata = _json(ASSET_ROOT / "metadata.json")
    statistics = metadata["statistics"]
    assert statistics["total_cells"] == 5862
    assert statistics["ocean_cells"] == 2097
    assert statistics["river_cells"] == 801
    assert metadata["feature_counts"] == {
        "roads": 506,
        "rivers": 196,
        "crossings": 265,
        "route_graph_nodes": 704,
        "route_graph_edges": 506,
    }
    assert metadata["review_status"] == "candidate"


def test_noryia_generator_is_deterministic(tmp_path: Path) -> None:
    import sys

    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from generate_noryia_navigation_data import _find_csv, _find_full_json, generate

    output = tmp_path / "noryia"
    source = _find_full_json()
    burgs = _find_csv("Noryia Burgs")
    first = generate(source, output, burgs)
    output_second = tmp_path / "noryia-2"
    second = generate(source, output_second, burgs)
    assert first["source_sha256"] == second["source_sha256"]
    assert first["statistics"] == second["statistics"]
    assert (output / "metadata.json").read_bytes() == (output_second / "metadata.json").read_bytes()


def test_noryia_route_graph_edges_are_valid() -> None:
    graph = _json(ASSET_ROOT / "route-graph.json")
    assert graph["review_status"] == "candidate"
    assert len(graph["nodes"]) > 100
    assert len(graph["edges"]) > 100
    nodes = {node["id"]: node for node in graph["nodes"]}
    for edge in graph["edges"]:
        if edge["source"] and edge["target"]:
            assert edge["source"] in nodes
            assert edge["target"] in nodes
            assert edge["distance_km"] > 0
            assert edge["speed_multiplier"] >= 1.0

    rules = _json(ASSET_ROOT / "movement-rules.json")
    assert rules["surface_speed_multipliers"]["marine"] == 0
    assert rules["surface_speed_multipliers"]["grassland"] == 1.0
    assert rules["river_crossing_requires"] == ["bridge", "ford", "ferry"]


def test_noryia_water_classification() -> None:
    terrain = _json(ASSET_ROOT / "terrain.geojson")["features"]
    water_counts: dict[str, int] = {}
    for feature in terrain:
        kind = feature["properties"]["water_kind"]
        water_counts[kind] = water_counts.get(kind, 0) + 1
    assert water_counts["ocean"] == 2097
    assert water_counts["lake"] == 42
    assert water_counts["river"] == 801
    assert water_counts[None] == 2922
