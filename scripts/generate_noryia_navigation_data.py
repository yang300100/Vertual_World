"""把 Noryia Full JSON 派生为导航数据产物。

只读取 `docs/worldbuilding/maps/map_new/data/Noryia Full *.json`（唯一地理输入），
生成 `docs/worldbuilding/maps/navigation/noryia/` 下的候选导航产物。所有自动派生
结果一律标记为 `candidate`，审核通过后才写入运行时。生成可重复：相同源文件哈希产生
完全相同的产物。

产物契约见 docs/design/08-noryia-terrain-routing-implementation.md。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "docs" / "worldbuilding" / "maps" / "map_new" / "data"
OUTPUT_DIR = PROJECT_ROOT / "docs" / "worldbuilding" / "maps" / "navigation" / "noryia"

# 世界等距圆柱参数（来自 info.width/height 与 mapCoordinates）。
WORLD_WIDTH = 10015
WORLD_HEIGHT = 5008
LON_W = -180.0
LON_E = 180.0
LAT_N = 90.0
LAT_S = -90.0
LON_SPAN = LON_E - LON_W
LAT_SPAN = LAT_N - LAT_S

# 高度码 -> 米：海平面阈值与最大海拔在 metadata.json 中留痕，供审核调整。
SEA_LEVEL_CODE = 20
MAX_ELEVATION_M = 4800
ELEVATION_MAX_CODE = 78
ELEVATION_EXPONENT = 1.6

# 生物群系 ID -> 受控地表类型。0 为海洋（水体）。
BIOME_SURFACE = {
    0: "marine",
    1: "desert",
    2: "desert",
    3: "savanna",
    4: "grassland",
    5: "forest",
    6: "forest",
    7: "rainforest",
    8: "rainforest",
    9: "taiga",
    10: "tundra",
    11: "glacier",
    12: "wetland",
}

# 路线分组 -> 道路等级元信息。缓冲必须限于路线点附近，不得覆盖整块领地。
ROAD_RULES = {
    "roads": {
        "label": "road",
        "speed_multiplier": 1.45,
        "buffer_degrees": 0.09,
        "crossing_type": "bridge",
    },
    "trails": {
        "label": "trail",
        "speed_multiplier": 1.2,
        "buffer_degrees": 0.05,
        "crossing_type": "ford",
    },
}

SEAROUTE_LABEL = "searoute"
SEAROUTE_SPEED_MULTIPLIER = 1.0

# 生物群系地表 -> 地形底图配色（与 _audit_overlay_svg 一致，避免视觉漂移）。
SURFACE_FILL = {
    "marine": "#466eab",
    "ocean": "#466eab",
    "lake": "#5aa7c7",
    "river": "#7fd0e8",
    "grassland": "#9ecf88",
    "forest": "#4f9a62",
    "rainforest": "#2f7d5a",
    "savanna": "#c5c06a",
    "desert": "#cfae72",
    "taiga": "#557a63",
    "tundra": "#b9c6bd",
    "glacier": "#e8f0f6",
    "wetland": "#7aa692",
}

# 相对高度色带（水=深蓝；陆地由低到高 绿->黄->橙->红->白）。在高度映射审核前只作相对展示。
ELEVATION_STOPS: list[tuple[float, tuple[int, int, int]]] = [
    (0.0, (12, 42, 92)),
    (0.15, (60, 120, 160)),
    (0.3, (96, 158, 96)),
    (0.5, (176, 182, 96)),
    (0.7, (216, 168, 92)),
    (0.85, (206, 118, 84)),
    (1.0, (240, 238, 236)),
]


def _elevation_hex(ratio: float) -> str:
    ratio = max(0.0, min(1.0, ratio))
    for index in range(len(ELEVATION_STOPS) - 1):
        low_ratio, low_rgb = ELEVATION_STOPS[index]
        high_ratio, high_rgb = ELEVATION_STOPS[index + 1]
        if ratio <= high_ratio:
            span = high_ratio - low_ratio
            mix = 0.0 if span <= 0 else (ratio - low_ratio) / span
            rgb = tuple(round(low_rgb[i] + (high_rgb[i] - low_rgb[i]) * mix) for i in range(3))
            return "#{:02x}{:02x}{:02x}".format(*rgb)
    return "#{:02x}{:02x}{:02x}".format(*ELEVATION_STOPS[-1][1])


def _find_full_json() -> Path:
    candidates = sorted(DATA_DIR.glob("Noryia Full *.json"))
    if not candidates:
        raise FileNotFoundError(f"在 {DATA_DIR} 中找不到 'Noryia Full *.json'")
    return candidates[-1]


def _find_csv(prefix: str) -> Path:
    candidates = sorted(DATA_DIR.glob(f"{prefix} *.csv"))
    if not candidates:
        raise FileNotFoundError(f"在 {DATA_DIR} 中找不到 '{prefix} *.csv'")
    return candidates[-1]


def _json_dump(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _pixel_to_lonlat(x: float, y: float) -> tuple[float, float]:
    longitude = LON_W + (x / WORLD_WIDTH) * LON_SPAN
    latitude = LAT_N + (y / WORLD_HEIGHT) * (LAT_SPAN * -1.0)
    return round(longitude, 5), round(latitude, 5)


def _distance_km(first: tuple[float, float], second: tuple[float, float]) -> float:
    lon1, lat1 = map(math.radians, first)
    lon2, lat2 = map(math.radians, second)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(value)))


def _surface_type(cell: dict[str, Any]) -> str:
    return BIOME_SURFACE.get(int(cell.get("biome", 0)), "grassland")


def _feature_type_map(pack: dict[str, Any]) -> dict[int, str]:
    """cell.f 指向 pack.features 下标，据此判定洋/湖/岛/大陆。"""
    result: dict[int, str] = {}
    for feature in pack.get("features", []):
        if isinstance(feature, int):
            continue
        feature_id = int(feature.get("i"))
        result[feature_id] = feature.get("type")
    return result


def _elevation_m(cell: dict[str, Any], is_water: bool) -> int:
    if is_water:
        return 0
    code = float(cell.get("h", 0))
    if code <= SEA_LEVEL_CODE:
        return 0
    ratio = (code - SEA_LEVEL_CODE) / (ELEVATION_MAX_CODE - SEA_LEVEL_CODE)
    return int(round((max(0.0, ratio) ** ELEVATION_EXPONENT) * MAX_ELEVATION_M))


def _water_basins(cells: list[dict[str, Any]], feature_type_by_f: dict[int, str]) -> dict[int, str]:
    """按 cell.f 指向的 pack.features 类型划分外海(ocean)、内陆湖(lake)与河流格(river)。"""
    kind: dict[int, str] = {}
    for cell in cells:
        feature_type = feature_type_by_f.get(int(cell.get("f", 0)))
        if int(cell.get("r", 0)) != 0:
            kind[cell["i"]] = "river"
        elif feature_type == "ocean":
            kind[cell["i"]] = "ocean"
        elif feature_type == "lake":
            kind[cell["i"]] = "lake"
        else:
            kind[cell["i"]] = None
    return kind


def _slope_for_cell(
    cell: dict[str, Any],
    cell_by_id: dict[int, dict[str, Any]],
    water_kind: dict[int, str],
) -> float:
    if water_kind.get(cell["i"]) in ("ocean", "lake", "river"):
        return 0.0
    origin = tuple(_pixel_to_lonlat(*cell["p"]))
    origin_elevation = float(_elevation_m(cell, False))
    max_slope = 0.0
    for neighbor in cell["c"]:
        neighbor_cell = cell_by_id.get(neighbor)
        if neighbor_cell is None:
            continue
        neighbor_elevation = float(_elevation_m(neighbor_cell, False))
        distance = _distance_km(origin, tuple(_pixel_to_lonlat(*neighbor_cell["p"])))
        if distance <= 0.001:
            continue
        rise = abs(neighbor_elevation - origin_elevation)
        slope = math.degrees(math.atan2(rise, distance * 1000.0))
        max_slope = max(max_slope, slope)
    return round(max_slope, 2)


def _terrain_mesh(
    cells: list[dict[str, Any]], feature_type_by_f: dict[int, str]
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]], dict[int, str]]:
    """派生每个 pack 单元的完整地形记录并建索引。"""
    cell_by_id = {cell["i"]: cell for cell in cells}
    water_kind = _water_basins(cells, feature_type_by_f)
    records: list[dict[str, Any]] = []
    for cell in cells:
        cell_id = cell["i"]
        longitude, latitude = _pixel_to_lonlat(*cell["p"])
        is_water = water_kind[cell_id] in ("ocean", "lake")
        records.append(
            {
                "cell_id": cell_id,
                "longitude": longitude,
                "latitude": latitude,
                "elevation_code": int(cell["h"]),
                "elevation_m": _elevation_m(cell, is_water),
                "surface_type": _surface_type(cell),
                "slope_degrees": 0.0,  # 占位，稍后按邻接填充
                "water_kind": water_kind[cell_id],
                "state_id": int(cell.get("state", 0)) or None,
                "province_id": int(cell.get("province", 0)) or None,
                "road_ids": [],
                "river_ids": [int(cell["r"])] if int(cell.get("r", 0)) else [],
                "port_id": None,
                "neighbors": list(cell["c"]),
                "cell_vertices": list(cell["v"]),
            }
        )
    for record in records:
        record["slope_degrees"] = _slope_for_cell(
            cell_by_id[record["cell_id"]], cell_by_id, water_kind
        )
    return records, cell_by_id, water_kind


def _river_geometries(
    pack: dict[str, Any], cell_by_id: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """按 pack.rivers 重建河流中心线(lon/lat 折线)并附可航行等级。"""
    rivers = pack["rivers"]
    features: list[dict[str, Any]] = []
    for river in sorted(rivers, key=lambda item: int(item["i"])):
        path: list[list[float]] = []
        for cell_id in river.get("cells", []):
            cell = cell_by_id.get(int(cell_id))
            if cell is None:
                continue
            path.append(list(_pixel_to_lonlat(*cell["p"])))
        if not path:
            continue
        width_factor = float(river.get("widthFactor", 1.0))
        discharge = float(river.get("discharge", 0))
        if width_factor <= 0.6:
            navigable = "no"
        elif discharge >= 600:
            navigable = "yes"
        else:
            navigable = "partial"
        properties = {
            "id": f"river:{int(river['i'])}",
            "name": river.get("name"),
            "river_id": int(river["i"]),
            "length_km": round(float(river.get("length", 0.0)), 3),
            "discharge": int(discharge),
            "width_factor": round(width_factor, 3),
            "navigable": navigable,
            "kind": "waterway",
            "review_status": "candidate",
        }
        features.append(
            {
                "type": "Feature",
                "id": properties["id"],
                "properties": properties,
                "geometry": {"type": "LineString", "coordinates": path},
            }
        )
    return features


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()


def _route_geometries(pack: dict[str, Any]) -> dict[str, Any]:
    routes = pack["routes"]
    features: list[dict[str, Any]] = []
    for route in sorted(routes, key=lambda item: (item.get("group") or "", int(item["i"]))):
        group = route.get("group") or "trails"
        coords: list[list[float]] = []
        for point in route.get("points", []):
            x, y = point[0], point[1]
            coords.append(list(_pixel_to_lonlat(x, y)))
        if len(coords) < 2:
            continue
        properties = {
            "id": f"route:{route.get('group')}:{int(route['i'])}",
            "group": group,
            "feature": route.get("feature"),
            "route_type": ROAD_RULES.get(group, {}).get("label", SEAROUTE_LABEL),
            "speed_multiplier": ROAD_RULES.get(group, {}).get(
                "speed_multiplier", SEAROUTE_SPEED_MULTIPLIER
            ),
            "allowed_movement_types": (
                ["land", "flight"] if group in ROAD_RULES else ["ship", "flight"]
            ),
            "kind": "road" if group in ROAD_RULES else "searoute",
            "review_status": "candidate",
        }
        features.append(
            {
                "type": "Feature",
                "id": properties["id"],
                "properties": properties,
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _crossing_modes(crossing_type: str) -> list[str]:
    if crossing_type in ("bridge", "ford", "ferry"):
        return ["land", "water"]
    if crossing_type == "port":
        return ["ship", "water", "land"]
    if crossing_type == "mountain_pass":
        return ["land"]
    return ["land", "ship", "water", "flight"]


def _crossing_geometries(
    roads: dict[str, Any],
    rivers: list[dict[str, Any]],
    terrain_records: list[dict[str, Any]],
    port_coords: list[dict[str, Any]],
) -> dict[str, Any]:
    """由道路/山路几何与河流关系推出桥、浅滩、渡口、港口与山口门户。"""
    crossings: list[dict[str, Any]] = []
    river_lines = [
        (feature["geometry"]["coordinates"], feature["properties"]) for feature in rivers
    ]

    def add_crossing(
        identifier: str, name: str, crossing_type: str, coordinate: list[float]
    ) -> None:
        if any(item["id"] == identifier for item in crossings):
            return
        crossings.append(
            {
                "type": "Feature",
                "id": identifier,
                "properties": {
                    "name": name,
                    "crossing_type": crossing_type,
                    "allowed_movement_types": _crossing_modes(crossing_type),
                    "review_status": "candidate",
                    "kind": "crossing",
                },
                "geometry": {"type": "Point", "coordinates": coordinate},
            }
        )

    for road in roads["features"]:
        if road["properties"]["group"] not in ROAD_RULES:
            continue
        coords = road["geometry"]["coordinates"]
        group = road["properties"]["group"]
        crossing_type = ROAD_RULES[group]["crossing_type"]
        for index, point in enumerate(coords):
            for river_coords, river_props in river_lines:
                if not river_coords:
                    continue
                best_idx = min(
                    range(len(river_coords)),
                    key=lambda idx: _distance_km((point[0], point[1]), tuple(river_coords[idx])),
                )
                near_river = _distance_km((point[0], point[1]), tuple(river_coords[best_idx]))
                if near_river <= 0.12:
                    identifier = f"crossing:{_slug(road['id'])}:{index}:{crossing_type}"
                    add_crossing(
                        identifier,
                        f"{river_props.get('name') or river_props.get('river_id')}跨口",
                        crossing_type,
                        list(point),
                    )
                    break

    for port in port_coords:
        add_crossing(
            f"crossing:port:{_slug(port['name'])}",
            f"{port['name']}港",
            "port",
            [port["longitude"], port["latitude"]],
        )

    # 山口：陆地路线点处于高坡格(>35°)视为山口。
    for road in roads["features"]:
        if road["properties"]["group"] not in ROAD_RULES:
            continue
        road_id = road["id"]
        for index, coordinate in enumerate(road["geometry"]["coordinates"]):
            cell = _nearest_cell(coordinate[0], coordinate[1], terrain_records)
            if cell and cell["slope_degrees"] > 35.0:
                add_crossing(
                    f"crossing:pass:{_slug(road_id)}:{index}",
                    "高山山口",
                    "mountain_pass",
                    coordinate,
                )

    return {"type": "FeatureCollection", "features": crossings}


def _nearest_cell(
    longitude: float, latitude: float, cells: list[dict[str, Any]]
) -> dict[str, Any] | None:
    if not cells:
        return None
    best = min(
        cells,
        key=lambda item: _distance_km((longitude, latitude), (item["longitude"], item["latitude"])),
    )
    return (
        best
        if _distance_km((longitude, latitude), (best["longitude"], best["latitude"])) <= 0.6
        else None
    )


def _mark_road_cells(roads: dict[str, Any], cells: list[dict[str, Any]]) -> None:
    """道路只在路线点缓冲区内生效，标记受影响格。"""
    for road in roads["features"]:
        route_type = road["properties"]["group"]
        buffer = ROAD_RULES.get(route_type, {}).get("buffer_degrees", 0.05)
        route_id = road["id"]
        for x, y in road["geometry"]["coordinates"]:
            for cell in cells:
                if abs(cell["longitude"] - x) > buffer or abs(cell["latitude"] - y) > buffer:
                    continue
                if route_id not in cell["road_ids"]:
                    cell["road_ids"].append(route_id)
    for cell in cells:
        cell["road_ids"] = sorted(set(cell["road_ids"]))


def _burg_nodes(burg_rows: list[dict[str, Any]]) -> dict[str, Any]:
    nodes: list[dict[str, Any]] = []
    for row in burg_rows:
        population = int(row["Population"])
        if population < 2000:
            continue
        nodes.append(
            {
                "id": f"burg:{_slug(row['Burg'])}:{row['Id']}",
                "name": row["Burg"],
                "source_id": int(row["Id"]),
                "kind": "city" if population >= 5000 else "town",
                "longitude": float(row["Longitude"]),
                "latitude": float(row["Latitude"]),
                "population": population,
                "is_port": int(row["Port"] == "port"),
                "is_capital": int(row["Capital"] == "capital"),
                "province": row["Province"],
                "state": row["State"],
            }
        )
    return {"nodes": nodes}


def _nearest_node(
    coordinate: list[float], nodes: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    best = None
    best_distance = float("inf")
    for node in nodes.values():
        distance = _distance_km(
            (coordinate[0], coordinate[1]), (node["longitude"], node["latitude"])
        )
        if distance < best_distance:
            best = node
            best_distance = distance
    return best if best_distance <= 0.6 else None


def _route_graph(
    burg_rows: list[dict[str, Any]],
    roads: dict[str, Any],
    crossings: dict[str, Any],
) -> dict[str, Any]:
    nodes_by_id: dict[str, dict[str, Any]] = {}
    for node in _burg_nodes(burg_rows)["nodes"]:
        nodes_by_id[node["id"]] = node
    for crossing in crossings["features"]:
        node = {
            "id": crossing["id"],
            "name": crossing["properties"]["name"],
            "kind": "crossing",
            "longitude": crossing["geometry"]["coordinates"][0],
            "latitude": crossing["geometry"]["coordinates"][1],
            "crossing_type": crossing["properties"]["crossing_type"],
        }
        nodes_by_id[node["id"]] = node

    edges: list[dict[str, Any]] = []
    for road in roads["features"]:
        coords = road["geometry"]["coordinates"]
        if len(coords) < 2:
            continue
        start = _nearest_node(coords[0], nodes_by_id)
        end = _nearest_node(coords[-1], nodes_by_id)
        polyline = [list(point) for point in coords]
        distance = sum(
            _distance_km(tuple(polyline[i]), tuple(polyline[i + 1]))
            for i in range(len(polyline) - 1)
        )
        edges.append(
            {
                "id": f"edge:{_slug(road['id'])}",
                "route_id": road["id"],
                "group": road["properties"]["group"],
                "source": start["id"] if start else None,
                "target": end["id"] if end else None,
                "distance_km": round(distance, 3),
                "speed_multiplier": road["properties"]["speed_multiplier"],
                "edge_type": road["properties"]["route_type"],
                "polyline": polyline,
                "allowed_movement_types": road["properties"]["allowed_movement_types"],
                "requirements": [],
                "review_status": "candidate",
            }
        )
    return {
        "schema_version": 1,
        "review_status": "candidate",
        "nodes": list(nodes_by_id.values()),
        "edges": edges,
    }


def _terrain_index(cells: list[dict[str, Any]], bin_degrees: float = 0.5) -> dict[str, Any]:
    bins: dict[str, list[int]] = {}
    compact: list[dict[str, Any]] = []
    for cell in cells:
        compact.append(
            {"id": cell["cell_id"], "longitude": cell["longitude"], "latitude": cell["latitude"]}
        )
        x_bucket = math.floor((cell["longitude"] - LON_W) / bin_degrees)
        y_bucket = math.floor((cell["latitude"] - LAT_S) / bin_degrees)
        bins.setdefault(f"{x_bucket}:{y_bucket}", []).append(cell["cell_id"])
    return {
        "schema_version": 1,
        "bounds": {
            "min_longitude": LON_W,
            "max_longitude": LON_E,
            "min_latitude": LAT_S,
            "max_latitude": LAT_N,
        },
        "bin_degrees": bin_degrees,
        "cells": compact,
        "bins": bins,
    }


def _svg_polygon_layer(
    records: list[dict[str, Any]], fill_func, stroke: str = "#223344"
) -> list[str]:
    def lonlat_to_svg(longitude: float, latitude: float, size: int = 900) -> tuple[float, float]:
        return (longitude - LON_W) / LON_SPAN * size, (LAT_N - latitude) / LAT_SPAN * size

    polygons: list[str] = []
    for record in records:
        points = " ".join(
            f"{lonlat_to_svg(x, y)[0]:.1f},{lonlat_to_svg(x, y)[1]:.1f}" for x, y in record["ring"]
        )
        polygons.append(
            f'<polygon points="{points}" fill="{fill_func(record)}" '
            f'stroke="{stroke}" stroke-width="0.4"/>'
        )
    return polygons


def _svg_header(title: str, legend: str = "") -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="450" viewBox="0 0 900 450" '
        'style="background:#0b1620">\n'
        f'<text x="16" y="22" font-size="14" fill="#fff1d2">{title}</text>'
        f'<text x="16" y="62" font-size="12" fill="#ffffff">{legend}</text>'
    )


def _svg_footer() -> str:
    return "</svg>\n"


def _terrain_base_svg(
    records: list[dict[str, Any]],
    rivers: list[dict[str, Any]],
    roads_geojson: dict[str, Any],
) -> str:
    """生物群系与海陆的干净矢量地形底图（无城镇/审核标注）。"""

    def lonlat_to_svg(longitude: float, latitude: float, size: int = 900) -> tuple[float, float]:
        return (longitude - LON_W) / LON_SPAN * size, (LAT_N - latitude) / LAT_SPAN * size

    polygons = _svg_polygon_layer(
        records, lambda item: SURFACE_FILL.get(item["surface_type"], "#cccccc")
    )
    water_overlays = [
        '<polyline points="'
        + " ".join(
            f"{lonlat_to_svg(x, y)[0]:.1f},{lonlat_to_svg(x, y)[1]:.1f}"
            for x, y in feature["geometry"]["coordinates"]
        )
        + '" fill="none" stroke="#41beff" stroke-width="2.2"/>'
        for feature in rivers
    ]
    road_overlays: list[str] = []
    for feature in roads_geojson["features"]:
        coords = feature["geometry"]["coordinates"]
        points = " ".join(
            f"{lonlat_to_svg(x, y)[0]:.1f},{lonlat_to_svg(x, y)[1]:.1f}" for x, y in coords
        )
        color = "#ffbf4d" if feature["properties"]["group"] in ROAD_RULES else "#ff8a4d"
        road_overlays.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="3.0"/>'
        )
    return (
        _svg_header("Noryia 地形底图（生物群系与海陆）", "青线=河流；黄线=道路；橙线=海路")
        + "".join(polygons)
        + "".join(water_overlays)
        + "".join(road_overlays)
        + _svg_footer()
    )


def _elevation_svg(records: list[dict[str, Any]]) -> str:
    """相对高度色带地形图（映射审核前只作相对展示，不宣称精确米数）。"""
    codes = [float(item["elevation_code"]) for item in records]
    code_min = min(codes)
    code_max = max(codes)
    span = max(1.0, code_max - code_min)

    def fill(record: dict[str, Any]) -> str:
        if record["water_kind"] in ("ocean", "lake", "river"):
            return "#12304f"
        ratio = (float(record["elevation_code"]) - code_min) / span
        return _elevation_hex(ratio)

    polygons = _svg_polygon_layer(records, fill, stroke="#25384a")
    return (
        _svg_header("Noryia 相对高度色带", "深蓝=水体；绿->黄->橙->白=相对高度（非精确海拔米）")
        + "".join(polygons)
        + _svg_footer()
    )


def _audit_overlay_svg(
    records: list[dict[str, Any]],
    rivers: list[dict[str, Any]],
    roads_geojson: dict[str, Any],
    county_nodes: list[dict[str, Any]],
    crossings: dict[str, Any],
) -> str:
    """生成人工审核叠加图 SVG（供人审，不供引擎读取）。"""

    def lonlat_to_svg(longitude: float, latitude: float, size: int = 900) -> tuple[float, float]:
        return (longitude - LON_W) / LON_SPAN * size, (LAT_N - latitude) / LAT_SPAN * size

    surface_fill = {
        "marine": "#466eab",
        "ocean": "#466eab",
        "lake": "#5aa7c7",
        "river": "#7fd0e8",
        "grassland": "#9ecf88",
        "forest": "#4f9a62",
        "rainforest": "#2f7d5a",
        "savanna": "#c5c06a",
        "desert": "#cfae72",
        "taiga": "#557a63",
        "tundra": "#b9c6bd",
        "glacier": "#e8f0f6",
        "wetland": "#7aa692",
    }

    polygons: list[str] = []
    for record in records:
        fill = surface_fill.get(record["surface_type"], "#cccccc")
        points = " ".join(
            f"{lonlat_to_svg(x, y)[0]:.1f},{lonlat_to_svg(x, y)[1]:.1f}" for x, y in record["ring"]
        )
        polygons.append(
            f'<polygon points="{points}" fill="{fill}" stroke="#223344" stroke-width="0.4"/>'
        )

    water_overlays = [
        '<polyline points="'
        + " ".join(
            f"{lonlat_to_svg(x, y)[0]:.1f},{lonlat_to_svg(x, y)[1]:.1f}"
            for x, y in feature["geometry"]["coordinates"]
        )
        + '" fill="none" stroke="#41beff" stroke-width="2.4"/>'
        for feature in rivers
    ]

    road_overlays: list[str] = []
    for feature in roads_geojson["features"]:
        coords = feature["geometry"]["coordinates"]
        points = " ".join(
            f"{lonlat_to_svg(x, y)[0]:.1f},{lonlat_to_svg(x, y)[1]:.1f}" for x, y in coords
        )
        color = "#ffbf4d" if feature["properties"]["group"] in ROAD_RULES else "#ff8a4d"
        road_overlays.append(
            f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="3.2"/>'
        )

    crossing_overlays = [
        '<rect x="{:.1f}" y="{:.1f}" width="14" height="14" fill="none" '
        'stroke="#fff5be" stroke-width="2.4"/>'.format(
            *lonlat_to_svg(*feature["geometry"]["coordinates"])
        )
        for feature in crossings["features"]
    ]

    city_overlays: list[str] = []
    for node in county_nodes:
        x, y = lonlat_to_svg(node["longitude"], node["latitude"])
        city_overlays.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="7" fill="#eb604d" '
            f'stroke="#fff7dc" stroke-width="2"/>'
            f'<text x="{x + 9:.1f}" y="{y + 4:.1f}" font-size="12" '
            f'fill="#ffffff">{node["name"]}</text>'
        )

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="450" viewBox="0 0 900 450" '
        'style="background:#0b1620">\n'
        + "".join(polygons)
        + "".join(water_overlays)
        + "".join(road_overlays)
        + "".join(crossing_overlays)
        + "".join(city_overlays)
        + '<g font-family="sans-serif" fill="#ffffff">'
        '<text x="16" y="22" font-size="14" fill="#fff1d2">Noryia 候选语义地形审核图</text>'
        '<line x1="16" y1="40" x2="66" y2="40" stroke="#41beff" stroke-width="4"/>'
        '<text x="76" y="44" font-size="12">河流</text>'
        '<line x1="150" y1="40" x2="200" y2="40" stroke="#ffbf4d" stroke-width="4"/>'
        '<text x="210" y="44" font-size="12">道路</text>'
        '<text x="16" y="62" font-size="12">白框=桥/港等门户；红点=城镇</text>'
        "</g></svg>\n"
    )


def generate(
    source_path: Path,
    output: Path,
    burg_csv: Path,
    *,
    include_svg: bool = True,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    data = json.loads(source_path.read_text(encoding="utf-8"))
    pack = data["pack"]

    records, _cells_by_id, water_kind = _terrain_mesh(pack["cells"], _feature_type_map(pack))
    rivers = _river_geometries(pack, _cells_by_id)
    route_geojson = _route_geometries(pack)

    # 每个 record 附带顶点多边形环（供 surface 与 audit overlay 使用）。
    vertex_index = {int(v["i"]): v for v in pack["vertices"]}
    for record in records:
        record["ring"] = [
            list(_pixel_to_lonlat(*vertex_index[int(vertex_id)]["p"]))
            for vertex_id in record["cell_vertices"]
        ]

    with burg_csv.open(encoding="utf-8-sig", newline="") as handle:
        burg_rows = list(csv.DictReader(handle))
    port_coords = [
        {
            "name": row["Burg"],
            "longitude": float(row["Longitude"]),
            "latitude": float(row["Latitude"]),
        }
        for row in burg_rows
        if row["Port"] == "port"
    ]

    crossings = _crossing_geometries(route_geojson, rivers, records, port_coords)
    _mark_road_cells(route_geojson, records)
    graph = _route_graph(burg_rows, route_geojson, crossings)
    index = _terrain_index(records)

    # 地表区域（每单元多边形，含地表类型；用于开发审计）。
    surface_features = [
        {
            "type": "Feature",
            "id": f"cell:{record['cell_id']}",
            "properties": {
                "cell_id": record["cell_id"],
                "surface_type": record["surface_type"],
                "elevation_m": record["elevation_m"],
                "slope_degrees": record["slope_degrees"],
                "water_kind": record["water_kind"],
                "state_id": record["state_id"],
                "province_id": record["province_id"],
                "road_ids": record["road_ids"],
                "river_ids": record["river_ids"],
                "review_status": "candidate",
            },
            "geometry": {"type": "Polygon", "coordinates": [record["ring"]]},
        }
        for record in records
    ]

    terrain_features = [
        {
            "type": "Feature",
            "id": f"cell:{record['cell_id']}",
            "properties": {
                "cell_id": record["cell_id"],
                "longitude": record["longitude"],
                "latitude": record["latitude"],
                "elevation_code": record["elevation_code"],
                "elevation_m": record["elevation_m"],
                "surface_type": record["surface_type"],
                "slope_degrees": record["slope_degrees"],
                "water_kind": record["water_kind"],
                "state_id": record["state_id"],
                "province_id": record["province_id"],
                "road_ids": record["road_ids"],
                "river_ids": record["river_ids"],
                "port_id": record["port_id"],
                "neighbors": record["neighbors"],
                "review_status": "candidate",
            },
            "geometry": {"type": "Point", "coordinates": [record["longitude"], record["latitude"]]},
        }
        for record in records
    ]

    _json_dump(
        output / "metadata.json",
        _metadata(source_path, records, water_kind, data, route_geojson, rivers, crossings, graph),
    )
    _json_dump(
        output / "terrain.geojson", {"type": "FeatureCollection", "features": terrain_features}
    )
    _json_dump(output / "terrain-index.json", index)
    _json_dump(
        output / "surface.geojson", {"type": "FeatureCollection", "features": surface_features}
    )
    _json_dump(output / "waterways.geojson", {"type": "FeatureCollection", "features": rivers})
    _json_dump(output / "roads.geojson", route_geojson)
    _json_dump(output / "crossings.geojson", crossings)
    _json_dump(output / "route-graph.json", graph)
    _json_dump(output / "movement-rules.json", _movement_rules())

    if include_svg:
        svg = _audit_overlay_svg(records, rivers, route_geojson, graph["nodes"], crossings)
        (output / "audit-overlay.svg").write_text(svg, encoding="utf-8")
        (output / "terrain-base.svg").write_text(
            _terrain_base_svg(records, rivers, route_geojson), encoding="utf-8"
        )
        (output / "elevation.svg").write_text(_elevation_svg(records), encoding="utf-8")

    return _metadata(
        source_path,
        records,
        water_kind,
        data,
        route_geojson,
        rivers,
        crossings,
        graph,
        include_svg=include_svg,
    )


def _style_assets(include_svg: bool) -> dict[str, Any]:
    """记录生成的样式资产；跳过 SVG 时不再引用 svg 图，避免元数据指向不存在的文件。"""
    assets: dict[str, Any] = {
        "elevation_is_relative": True,
        "height_mapping_pending_review": True,
    }
    if include_svg:
        assets["terrain-base.svg"] = "biome & land/sea vector base (no labels)"
        assets["elevation.svg"] = (
            "relative-height color ramp; NOT accurate elevation metres until the "
            "height mapping is approved"
        )
    return assets


def _metadata(
    source_path: Path,
    records: list[dict[str, Any]],
    water_kind: dict[int, str],
    data: dict[str, Any],
    route_geojson: dict[str, Any],
    rivers: list[dict[str, Any]],
    crossings: dict[str, Any],
    graph: dict[str, Any],
    *,
    include_svg: bool = True,
) -> dict[str, Any]:
    surface_counts: dict[str, int] = {}
    for record in records:
        surface_counts[record["surface_type"]] = surface_counts.get(record["surface_type"], 0) + 1
    return {
        "schema_version": 1,
        "review_status": "candidate",
        "generator": "scripts/generate_noryia_navigation_data.py",
        "source": str(source_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "azgaar_version": str(data["info"].get("version", "")),
        "bounds": {
            "min_longitude": LON_W,
            "max_longitude": LON_E,
            "min_latitude": LAT_S,
            "max_latitude": LAT_N,
        },
        "width": WORLD_WIDTH,
        "height": WORLD_HEIGHT,
        "projection": "equirectangular",
        "encoding": {
            "elevation_code": "pack cell h (1..78); sea-level threshold=20",
            "elevation_m_formula": (
                f"round(((h-{SEA_LEVEL_CODE})/{ELEVATION_MAX_CODE - SEA_LEVEL_CODE})"
                f"**{ELEVATION_EXPONENT}*{MAX_ELEVATION_M}) for land, 0 for water"
            ),
            "sea_level_code": SEA_LEVEL_CODE,
            "surface_type": BIOME_SURFACE,
            "water_kind": {
                "ocean": "border-connected sea",
                "lake": "inland water",
                "river": "waterway cell",
            },
            "slope_degrees": "max angle from neighbouring cell elevation / great-circle distance",
        },
        "style_assets": _style_assets(include_svg),
        "statistics": {
            "total_cells": len(records),
            "land_cells": sum(1 for record in records if record["water_kind"] is None),
            "water_cells": sum(1 for record in records if record["water_kind"] is not None),
            "ocean_cells": sum(1 for value in water_kind.values() if value == "ocean"),
            "lake_cells": sum(1 for value in water_kind.values() if value == "lake"),
            "river_cells": sum(1 for value in water_kind.values() if value == "river"),
            "elevation_max_m": max(record["elevation_m"] for record in records),
            "slope_max_degrees": max(record["slope_degrees"] for record in records),
            "surface_cell_counts": surface_counts,
        },
        "feature_counts": {
            "roads": len(route_geojson["features"]),
            "rivers": len(rivers),
            "crossings": len(crossings["features"]),
            "route_graph_nodes": len(graph["nodes"]),
            "route_graph_edges": len(graph["edges"]),
        },
        "warnings": [
            "候选数据由 Noryia Full JSON 自动生成，不是世界正典。",
            "高程为单调映射的高度码，不应解释为测绘结果。",
            "河流、道路与桥梁几何必须人工审核后才能进入运行时。",
        ],
    }


def _movement_rules() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "review_status": "candidate",
        "surface_speed_multipliers": {
            "marine": 0.0,
            "desert": 0.55,
            "savanna": 0.9,
            "grassland": 1.0,
            "forest": 0.72,
            "rainforest": 0.6,
            "taiga": 0.5,
            "tundra": 0.5,
            "glacier": 0.2,
            "wetland": 0.45,
        },
        "road_speed_multipliers": {"road": 1.45, "trail": 1.2},
        "searoute_speed_multiplier": SEAROUTE_SPEED_MULTIPLIER,
        "land_slope_rules": [
            {"max_degrees": 10, "multiplier": 1.0},
            {"max_degrees": 20, "multiplier": 0.8},
            {"max_degrees": 35, "multiplier": 0.45},
            {"max_degrees": 90, "multiplier": 0.0},
        ],
        "river_crossing_requires": ["bridge", "ford", "ferry"],
        "modes": {
            "land": {
                "passable": "land cells, roads, bridges/fords/ferries",
                "forbidden": "ocean/lake; major river without a crossing portal; slope>35 degrees",
            },
            "ship": {
                "passable": "navigable water, navigable rivers, port edges",
                "forbidden": "land; disembark outside a port; closed waterways",
            },
            "water": {
                "passable": "small-boat rivers, lakes, near-shore water",
                "forbidden": "open ocean unless ocean_capable",
            },
            "flight": {
                "passable": "land, water, high mountains",
                "forbidden": "vehicle ceiling, energy, no-fly and weather limits",
            },
            "underground": {
                "passable": "registered mine/tunnel edges",
                "forbidden": "arbitrary straight digging; collapsed/closed tunnels",
            },
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 Noryia 候选导航数据")
    parser.add_argument("--source", type=Path, default=_find_full_json())
    parser.add_argument("--burgs", type=Path, default=_find_csv("Noryia Burgs"))
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR)
    parser.add_argument(
        "--skip-svg",
        action="store_true",
        help="不生成 terrain-base/elevation/audit-overlay.svg（只输出运行所需的 json/geojson）",
    )
    args = parser.parse_args()
    metadata = generate(
        args.source.resolve(),
        args.output.resolve(),
        args.burgs.resolve(),
        include_svg=not args.skip_svg,
    )
    print(json.dumps(metadata["statistics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
