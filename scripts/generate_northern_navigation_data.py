from __future__ import annotations

import argparse
import hashlib
import json
import math
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = (
    PROJECT_ROOT / "docs" / "worldbuilding" / "maps" / "northern-main-continent.png"
)
DEFAULT_OUTPUT = (
    PROJECT_ROOT
    / "docs"
    / "worldbuilding"
    / "maps"
    / "navigation"
    / "northern-main-continent"
)
BOUNDS = {
    "min_longitude": -170.0,
    "max_longitude": 0.0,
    "min_latitude": 0.0,
    "max_latitude": 72.0,
}
SURFACE_CODES = {
    "ocean": 0,
    "inland_water": 1,
    "grassland": 2,
    "forest": 3,
    "dryland": 4,
    "mountain": 5,
    "snow": 6,
    "wetland": 7,
}
SURFACE_COLORS = {
    0: (20, 78, 126),
    1: (54, 140, 190),
    2: (130, 158, 83),
    3: (43, 103, 66),
    4: (190, 158, 91),
    5: (112, 102, 96),
    6: (224, 236, 240),
    7: (83, 135, 112),
}

CITIES = {
    "forge_valley": {"name": "锻谷城", "coordinates": [-132.0, 31.0]},
    "oathflow": {"name": "澜誓城", "coordinates": [-71.0, 28.0]},
    "mirror_lake": {"name": "望镜湖庭", "coordinates": [-62.0, 50.0]},
    "east_port": {"name": "东澜港", "coordinates": [-22.0, 30.0]},
    "red_spring": {"name": "赭泉关", "coordinates": [-67.0, 12.0]},
}

WATERWAYS = [
    {
        "id": "forge-river",
        "name": "锻河",
        "coordinates": [[-132, 38], [-112, 34], [-91, 31], [-71, 28]],
        "navigable": "candidate_partial",
    },
    {
        "id": "mirror-river",
        "name": "镜河",
        "coordinates": [[-62, 56], [-64, 47], [-67, 38], [-71, 28]],
        "navigable": "candidate_partial",
    },
    {
        "id": "red-spring-river",
        "name": "赤泉河",
        "coordinates": [[-67, 12], [-68, 19], [-70, 24], [-71, 28]],
        "navigable": "candidate_no",
    },
    {
        "id": "adde-river",
        "name": "阿德河",
        "coordinates": [[-71, 28], [-55, 28.5], [-38, 29], [-22, 30]],
        "navigable": "candidate_yes",
    },
]

ROADS = [
    {
        "id": "forge-adde-east-road",
        "name": "锻河—阿德—东澜路",
        "road_type": "trade_road",
        "speed_multiplier": 1.45,
        "coordinates": [
            [-132, 31],
            [-112, 31],
            [-105, 31],
            [-91, 29],
            [-71, 28],
            [-49, 29],
            [-22, 30],
        ],
    },
    {
        "id": "mirror-oath-seasonal-road",
        "name": "镜湖—澜誓季节路",
        "road_type": "seasonal_road",
        "speed_multiplier": 1.2,
        "coordinates": [[-62, 50], [-65, 42], [-68, 35], [-71, 28]],
    },
    {
        "id": "red-pass-oath-road",
        "name": "赭泉—澜誓护粮路",
        "road_type": "guarded_track",
        "speed_multiplier": 1.25,
        "coordinates": [[-67, 12], [-68, 19], [-69, 24], [-71, 28]],
    },
]

CROSSINGS = [
    {
        "id": "forge-western-channel-crossing",
        "name": "锻谷西侧水道桥渡候选",
        "crossing_type": "bridge_and_ferry",
        "coordinates": [-126.0, 31.0],
        "status": "candidate",
    },
    {
        "id": "western-forge-pass",
        "name": "西部锻谷山口候选",
        "crossing_type": "mountain_pass",
        "coordinates": [-105.0, 31.0],
        "status": "candidate",
    },
    {
        "id": "oathflow-ancient-crossing",
        "name": "澜誓古渡与桥群",
        "crossing_type": "bridge_and_ferry",
        "coordinates": [-71.0, 28.0],
        "status": "confirmed_reference_candidate_geometry",
    },
    {
        "id": "forge-river-west-bridge-candidate",
        "name": "锻河西桥候选",
        "crossing_type": "bridge",
        "coordinates": [-96.0, 29.5],
        "status": "candidate",
    },
    {
        "id": "mirror-river-south-bridge-candidate",
        "name": "镜河南桥候选",
        "crossing_type": "bridge",
        "coordinates": [-68.0, 35.0],
        "status": "candidate",
    },
    {
        "id": "east-port-transition",
        "name": "东澜港陆海转换点",
        "crossing_type": "port",
        "coordinates": [-22.0, 30.0],
        "status": "confirmed_reference_candidate_geometry",
    },
]


def _json_dump(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _flood_border_water(mask: np.ndarray) -> np.ndarray:
    height, width = mask.shape
    ocean = np.zeros_like(mask, dtype=bool)
    queue: deque[tuple[int, int]] = deque()
    for x in range(width):
        if mask[0, x]:
            queue.append((0, x))
        if mask[height - 1, x]:
            queue.append((height - 1, x))
    for y in range(height):
        if mask[y, 0]:
            queue.append((y, 0))
        if mask[y, width - 1]:
            queue.append((y, width - 1))
    while queue:
        y, x = queue.popleft()
        if ocean[y, x] or not mask[y, x]:
            continue
        ocean[y, x] = True
        if y > 0:
            queue.append((y - 1, x))
        if y + 1 < height:
            queue.append((y + 1, x))
        if x > 0:
            queue.append((y, x - 1))
        if x + 1 < width:
            queue.append((y, x + 1))
    return ocean


def _ridge_field(size: tuple[int, int]) -> np.ndarray:
    width, height = size
    ridge = Image.new("L", size, 0)
    draw = ImageDraw.Draw(ridge)

    def points(values: list[tuple[float, float]]) -> list[tuple[int, int]]:
        return [(round(x * width), round(y * height)) for x, y in values]

    draw.line(
        points([(0.36, 0.03), (0.39, 0.18), (0.40, 0.38), (0.37, 0.60), (0.43, 0.92)]),
        fill=255,
        width=max(3, width // 180),
        joint="curve",
    )
    draw.line(
        points([(0.57, 0.48), (0.62, 0.61), (0.67, 0.75), (0.72, 0.92)]),
        fill=210,
        width=max(3, width // 210),
        joint="curve",
    )
    draw.line(
        points([(0.83, 0.24), (0.80, 0.36), (0.82, 0.47)]),
        fill=150,
        width=max(2, width // 260),
        joint="curve",
    )
    blurred = ridge.filter(ImageFilter.GaussianBlur(radius=max(6, width / 52)))
    field = np.asarray(blurred, dtype=np.float32) / 255.0
    maximum = float(field.max())
    return field / maximum if maximum > 0 else field


def _classify(source: Image.Image) -> dict[str, np.ndarray]:
    rgb = np.asarray(source, dtype=np.float32)
    red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    maximum = rgb.max(axis=2)
    minimum = rgb.min(axis=2)
    saturation = (maximum - minimum) / np.maximum(maximum, 1)
    brightness = rgb.mean(axis=2)

    water = (
        (blue > red * 1.12)
        & (blue > green * 1.035)
        & (blue > 38)
        & (saturation > 0.16)
    )
    water_image = Image.fromarray((water * 255).astype(np.uint8), mode="L")
    water_image = water_image.filter(ImageFilter.MaxFilter(3)).filter(
        ImageFilter.MinFilter(3)
    )
    water = np.asarray(water_image) > 127
    # 原图边缘有装饰性暗绿色晕染；区域图边界在海上，统一归为外海。
    border_width = max(4, source.width // 40)
    water[:border_width, :] = True
    water[-border_width:, :] = True
    water[:, :border_width] = True
    water[:, -border_width:] = True
    ocean = _flood_border_water(water)
    inland_water = water & ~ocean
    land = ~water

    land_blur = Image.fromarray((land * 255).astype(np.uint8), mode="L").filter(
        ImageFilter.GaussianBlur(radius=max(10, source.width / 32))
    )
    land_depth = np.asarray(land_blur, dtype=np.float32) / 255.0
    ridge = _ridge_field(source.size)
    pale_highlight = np.clip((brightness - 125) / 110, 0, 1) * np.clip(
        1 - saturation, 0, 1
    )
    elevation = np.where(
        land,
        35 + 560 * land_depth + 3450 * ridge + 520 * pale_highlight,
        0,
    )
    elevation = np.clip(elevation, 0, 4800).astype(np.uint16)

    mean_latitude = (BOUNDS["min_latitude"] + BOUNDS["max_latitude"]) / 2
    x_km = (
        (BOUNDS["max_longitude"] - BOUNDS["min_longitude"])
        * 111.195
        * math.cos(math.radians(mean_latitude))
        / source.width
    )
    y_km = (
        (BOUNDS["max_latitude"] - BOUNDS["min_latitude"])
        * 111.195
        / source.height
    )
    gradient_y, gradient_x = np.gradient(elevation.astype(np.float32), y_km, x_km)
    physical_slope = np.degrees(
        np.arctan(np.sqrt(gradient_x**2 + gradient_y**2) / 1000)
    )
    # 世界级候选栅格无法解析山体局部坡面，用山脊强度补充宏观通行坡度。
    slope_degrees = np.maximum(physical_slope, ridge * 42.0)
    slope = np.clip(slope_degrees / 90 * 255, 0, 255).astype(np.uint8)

    near_water = np.asarray(
        Image.fromarray((water * 255).astype(np.uint8), mode="L").filter(
            ImageFilter.GaussianBlur(radius=max(3, source.width / 180))
        ),
        dtype=np.float32,
    ) / 255.0
    surface = np.full(water.shape, SURFACE_CODES["grassland"], dtype=np.uint8)
    forest = land & (green > red * 0.88) & (green > blue * 0.82) & (brightness < 125)
    dryland = land & (red > blue * 1.35) & (green > blue * 1.18) & (brightness > 92)
    mountain = land & ((elevation > 1250) | (slope_degrees > 18))
    snow = land & (elevation > 1450) & (brightness > 160) & (saturation < 0.28)
    wetland = land & (elevation < 320) & (near_water > 0.09)
    surface[forest] = SURFACE_CODES["forest"]
    surface[dryland] = SURFACE_CODES["dryland"]
    surface[wetland] = SURFACE_CODES["wetland"]
    surface[mountain] = SURFACE_CODES["mountain"]
    surface[snow] = SURFACE_CODES["snow"]
    surface[inland_water] = SURFACE_CODES["inland_water"]
    surface[ocean] = SURFACE_CODES["ocean"]

    water_mask = np.zeros(water.shape, dtype=np.uint8)
    water_mask[inland_water] = 160
    water_mask[ocean] = 255
    return {
        "elevation": elevation,
        "slope": slope,
        "slope_degrees": slope_degrees,
        "surface": surface,
        "water_mask": water_mask,
        "land": land,
        "ocean": ocean,
        "inland_water": inland_water,
    }


def _feature_collection(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def _line_features(items: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    features = []
    for item in items:
        properties = {key: value for key, value in item.items() if key != "coordinates"}
        properties.update({"kind": kind, "review_status": "candidate"})
        features.append(
            {
                "type": "Feature",
                "id": item["id"],
                "properties": properties,
                "geometry": {"type": "LineString", "coordinates": item["coordinates"]},
            }
        )
    return _feature_collection(features)


def _point_features(items: list[dict[str, Any]], kind: str) -> dict[str, Any]:
    features = []
    for item in items:
        properties = {key: value for key, value in item.items() if key != "coordinates"}
        properties["kind"] = kind
        features.append(
            {
                "type": "Feature",
                "id": item["id"],
                "properties": properties,
                "geometry": {"type": "Point", "coordinates": item["coordinates"]},
            }
        )
    return _feature_collection(features)


def _distance_km(first: list[float], second: list[float]) -> float:
    lon1, lat1 = map(math.radians, first)
    lon2, lat2 = map(math.radians, second)
    dlon = lon2 - lon1
    dlat = lat2 - lat1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(value)))


def _route_graph() -> dict[str, Any]:
    nodes = [
        {"id": identifier, "name": item["name"], "coordinates": item["coordinates"]}
        for identifier, item in CITIES.items()
    ]
    road_by_id = {item["id"]: item for item in ROADS}
    main_road = road_by_id["forge-adde-east-road"]["coordinates"]
    pairs = [
        (
            "forge-oath",
            "forge_valley",
            "oathflow",
            "forge-adde-east-road",
            main_road[:5],
        ),
        (
            "oath-east",
            "oathflow",
            "east_port",
            "forge-adde-east-road",
            main_road[4:],
        ),
        (
            "mirror-oath",
            "mirror_lake",
            "oathflow",
            "mirror-oath-seasonal-road",
            road_by_id["mirror-oath-seasonal-road"]["coordinates"],
        ),
        (
            "red-oath",
            "red_spring",
            "oathflow",
            "red-pass-oath-road",
            road_by_id["red-pass-oath-road"]["coordinates"],
        ),
    ]
    edges = []
    for edge_id, source, target, road_id, polyline in pairs:
        road = road_by_id[road_id]
        edges.append(
            {
                "id": edge_id,
                "source": source,
                "target": target,
                "road_id": road_id,
                "distance_km": round(
                    _distance_km(CITIES[source]["coordinates"], CITIES[target]["coordinates"]),
                    3,
                ),
                "speed_multiplier": road["speed_multiplier"],
                "polyline": polyline,
                "allowed_movement_types": ["land", "flight"],
                "review_status": "candidate",
            }
        )
    return {
        "schema_version": 1,
        "review_status": "candidate",
        "nodes": nodes,
        "edges": edges,
    }


def _world_to_pixel(coordinates: list[float], size: tuple[int, int]) -> tuple[int, int]:
    longitude, latitude = coordinates
    width, height = size
    x = (longitude - BOUNDS["min_longitude"]) / (
        BOUNDS["max_longitude"] - BOUNDS["min_longitude"]
    )
    y = (BOUNDS["max_latitude"] - latitude) / (
        BOUNDS["max_latitude"] - BOUNDS["min_latitude"]
    )
    return round(x * width), round(y * height)


def _font(size: int) -> ImageFont.ImageFont:
    candidates = [
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/simhei.ttf"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _audit_overlay(source: Image.Image, surface: np.ndarray) -> Image.Image:
    base = source.convert("RGBA")
    color = np.zeros((*surface.shape, 4), dtype=np.uint8)
    for code, rgb in SURFACE_COLORS.items():
        color[surface == code, :3] = rgb
    color[..., 3] = 50
    overlay = Image.fromarray(color, mode="RGBA")
    base.alpha_composite(overlay)
    draw = ImageDraw.Draw(base)
    river_font = _font(max(11, source.width // 95))
    city_font = _font(max(12, source.width // 82))
    for waterway in WATERWAYS:
        points = [_world_to_pixel(item, source.size) for item in waterway["coordinates"]]
        draw.line(points, fill=(65, 190, 255, 235), width=max(2, source.width // 380))
    for road in ROADS:
        points = [_world_to_pixel(item, source.size) for item in road["coordinates"]]
        draw.line(points, fill=(255, 191, 77, 245), width=max(3, source.width // 300))
    for crossing in CROSSINGS:
        x, y = _world_to_pixel(crossing["coordinates"], source.size)
        radius = max(5, source.width // 150)
        draw.rectangle(
            (x - radius, y - radius, x + radius, y + radius),
            outline=(255, 245, 190, 255),
            width=max(2, source.width // 600),
        )
    for city in CITIES.values():
        x, y = _world_to_pixel(city["coordinates"], source.size)
        radius = max(5, source.width // 170)
        draw.ellipse(
            (x - radius, y - radius, x + radius, y + radius),
            fill=(235, 96, 77, 255),
            outline=(255, 247, 220, 255),
            width=2,
        )
        draw.text((x + radius + 3, y - radius), city["name"], font=city_font, fill="white")
    draw.rounded_rectangle(
        (18, 18, 410, 118), radius=10, fill=(8, 15, 24, 215), outline=(255, 255, 255, 90)
    )
    draw.text((32, 28), "候选语义地形审核图", font=city_font, fill=(255, 241, 210, 255))
    draw.line((32, 65, 82, 65), fill=(65, 190, 255, 255), width=4)
    draw.text((92, 54), "候选河流", font=river_font, fill="white")
    draw.line((210, 65, 260, 65), fill=(255, 191, 77, 255), width=4)
    draw.text((270, 54), "候选道路", font=river_font, fill="white")
    draw.text((32, 86), "红点=城市；白框=桥/港等转换门户", font=river_font, fill="white")
    return base


def generate(source_path: Path, output: Path, width: int, height: int) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=True)
    source_bytes = source_path.read_bytes()
    source = Image.open(source_path).convert("RGB").resize(
        (width, height), Image.Resampling.LANCZOS
    )
    layers = _classify(source)

    Image.fromarray(layers["elevation"]).save(output / "elevation.png")
    Image.fromarray(layers["water_mask"], mode="L").save(output / "water-mask.png")
    Image.fromarray(layers["slope"], mode="L").save(output / "slope.png")
    surface_image = Image.fromarray(layers["surface"], mode="P")
    palette: list[int] = []
    for code in range(256):
        palette.extend(SURFACE_COLORS.get(code, (0, 0, 0)))
    surface_image.putpalette(palette)
    surface_image.save(output / "surface.png")

    _json_dump(output / "waterways.geojson", _line_features(WATERWAYS, "waterway"))
    _json_dump(output / "roads.geojson", _line_features(ROADS, "road"))
    _json_dump(output / "crossings.geojson", _point_features(CROSSINGS, "crossing"))
    _json_dump(
        output / "cities.geojson",
        _point_features(
            [dict(id=identifier, **item) for identifier, item in CITIES.items()], "city"
        ),
    )
    _json_dump(output / "route-graph.json", _route_graph())
    movement_rules = {
        "schema_version": 1,
        "review_status": "candidate",
        "surface_speed_multipliers": {
            "grassland": 1.0,
            "forest": 0.65,
            "dryland": 0.8,
            "mountain": 0.45,
            "snow": 0.5,
            "wetland": 0.35,
            "ocean": 0.0,
            "inland_water": 0.0,
        },
        "road_speed_multipliers": {
            "trade_road": 1.45,
            "seasonal_road": 1.2,
            "guarded_track": 1.25,
        },
        "land_slope_rules": [
            {"max_degrees": 10, "multiplier": 1.0},
            {"max_degrees": 20, "multiplier": 0.8},
            {"max_degrees": 35, "multiplier": 0.45},
            {"max_degrees": 90, "multiplier": 0.0},
        ],
        "river_crossing_requires": ["bridge", "ferry", "ford"],
    }
    _json_dump(output / "movement-rules.json", movement_rules)
    _audit_overlay(source, layers["surface"]).save(output / "audit-overlay.png")

    surface_counts = {
        name: int(np.count_nonzero(layers["surface"] == code))
        for name, code in SURFACE_CODES.items()
    }
    metadata = {
        "schema_version": 1,
        "review_status": "candidate",
        "generator": "scripts/generate_northern_navigation_data.py",
        "source": str(source_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "width": width,
        "height": height,
        "bounds": BOUNDS,
        "projection": "logical_equirectangular",
        "encoding": {
            "elevation": "uint16 meters, sea level=0",
            "water_mask": {"land": 0, "inland_water": 160, "ocean": 255},
            "slope": "uint8, value / 255 * 90 degrees",
            "surface": SURFACE_CODES,
        },
        "statistics": {
            "land_fraction": round(float(np.mean(layers["land"])), 6),
            "ocean_fraction": round(float(np.mean(layers["ocean"])), 6),
            "inland_water_fraction": round(float(np.mean(layers["inland_water"])), 6),
            "elevation_max_m": int(layers["elevation"].max()),
            "slope_mean_degrees": round(float(layers["slope_degrees"][layers["land"]].mean()), 3),
            "surface_cell_counts": surface_counts,
        },
        "warnings": [
            "候选数据由视觉地图与已确认文字关系自动生成，不是世界正典。",
            "高程主要来自规则化山脊场，不应解释为测绘结果。",
            "河流、道路与桥梁几何必须人工审核后才能进入运行时。",
        ],
    }
    _json_dump(output / "metadata.json", metadata)
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description="生成北方主大陆候选语义地形与路网")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--width", type=int, default=768)
    parser.add_argument("--height", type=int, default=512)
    args = parser.parse_args()
    if args.width < 128 or args.height < 128:
        raise SystemExit("输出分辨率不能低于128×128")
    metadata = generate(args.source.resolve(), args.output.resolve(), args.width, args.height)
    print(json.dumps(metadata["statistics"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
