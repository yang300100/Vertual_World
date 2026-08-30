from __future__ import annotations

import hashlib
import json
import random
from html import escape
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = (
    PROJECT_ROOT / "docs" / "worldbuilding" / "maps" / "navigation" / "settlements" / "oathflow"
)
AI_BASE = OUTPUT_ROOT / "detail-map-ai-base.png"
WIDTH = 1600
HEIGHT = 1600
BOUNDS = {
    "min_longitude": -71.21,
    "max_longitude": -70.79,
    "min_latitude": 27.82,
    "max_latitude": 28.18,
}

FEATURES = [
    {
        "id": "river-archives",
        "name": "河务档案区",
        "feature_type": "public_facility",
        "coordinates": [-71.08, 28.04],
        "status": "confirmed_name_candidate_geometry",
    },
    {
        "id": "royal-embankment",
        "name": "王室堤岸",
        "feature_type": "landmark",
        "coordinates": [-70.94, 27.98],
        "status": "confirmed_name_candidate_geometry",
    },
    {
        "id": "riverside-granaries",
        "name": "三座河畔大粮仓",
        "feature_type": "public_facility",
        "coordinates": [-71.03, 27.92],
        "status": "confirmed_reference_candidate_geometry",
    },
    {
        "id": "mage-family-quarter",
        "name": "法师家族宅区",
        "feature_type": "district",
        "coordinates": [-71.12, 28.10],
        "status": "confirmed_name_candidate_geometry",
    },
    {
        "id": "foreign-caravan-quarter",
        "name": "外来商旅区",
        "feature_type": "district",
        "coordinates": [-71.07, 27.98],
        "status": "confirmed_name_candidate_geometry",
    },
    {
        "id": "shipworkers-quarter",
        "name": "船工区",
        "feature_type": "district",
        "coordinates": [-70.98, 27.94],
        "status": "confirmed_name_candidate_geometry",
    },
    {
        "id": "inner-river-port",
        "name": "内河码头",
        "feature_type": "port",
        "coordinates": [-70.99, 27.97],
        "status": "confirmed_reference_candidate_geometry",
    },
    {
        "id": "royal-offices",
        "name": "王室官署",
        "feature_type": "public_facility",
        "coordinates": [-70.96, 28.02],
        "status": "confirmed_reference_candidate_geometry",
    },
]

ROADS = [
    {
        "id": "ancient-east-west-road",
        "name": "古老东西陆路",
        "road_type": "stone_trade_road",
        "coordinates": [[-71.21, 28.0], [-71.08, 28.01], [-71.0, 28.0], [-70.79, 28.0]],
    },
    {
        "id": "archives-road",
        "name": "档案大道",
        "road_type": "city_stone_road",
        "coordinates": [[-71.08, 28.01], [-71.08, 28.04], [-71.11, 28.1]],
    },
    {
        "id": "granary-road",
        "name": "粮仓路",
        "road_type": "service_road",
        "coordinates": [[-71.08, 28.01], [-71.05, 27.96], [-71.03, 27.92]],
    },
    {
        "id": "embankment-road",
        "name": "堤岸路",
        "road_type": "embankment_road",
        "coordinates": [[-71.0, 28.0], [-70.97, 27.99], [-70.94, 27.98]],
    },
    {
        "id": "north-ring-street",
        "name": "北环街",
        "road_type": "city_street",
        "coordinates": [[-71.17, 28.09], [-71.1, 28.12], [-71.02, 28.1], [-70.9, 28.08]],
    },
    {
        "id": "south-market-street",
        "name": "南市街",
        "road_type": "city_street",
        "coordinates": [[-71.15, 27.94], [-71.07, 27.95], [-70.99, 27.94], [-70.89, 27.94]],
    },
    {
        "id": "west-gate-street",
        "name": "西门街",
        "road_type": "city_street",
        "coordinates": [[-71.16, 28.06], [-71.14, 28.0], [-71.13, 27.91]],
    },
    {
        "id": "east-harbor-street",
        "name": "港仓街",
        "road_type": "city_street",
        "coordinates": [[-70.94, 28.08], [-70.94, 28.0], [-70.96, 27.9]],
    },
    {
        "id": "archive-lane",
        "name": "抄本巷",
        "road_type": "local_lane",
        "coordinates": [[-71.13, 28.07], [-71.08, 28.07], [-71.03, 28.07]],
    },
    {
        "id": "caravan-lane",
        "name": "商旅巷",
        "road_type": "local_lane",
        "coordinates": [[-71.12, 27.98], [-71.07, 27.98], [-71.02, 27.98]],
    },
    {
        "id": "dock-lane",
        "name": "码头巷",
        "road_type": "local_lane",
        "coordinates": [[-71.02, 27.96], [-70.98, 27.96], [-70.91, 27.96]],
    },
    {
        "id": "granary-lane",
        "name": "仓廒巷",
        "road_type": "local_lane",
        "coordinates": [[-71.09, 27.91], [-71.03, 27.91], [-70.97, 27.91]],
    },
]

RIVERS = [
    {
        "id": "forge-river-local",
        "name": "锻河",
        "coordinates": [[-71.21, 28.08], [-71.11, 28.04], [-71.0, 28.0]],
    },
    {
        "id": "mirror-river-local",
        "name": "镜河",
        "coordinates": [[-71.03, 28.18], [-71.02, 28.09], [-71.0, 28.0]],
    },
    {
        "id": "red-spring-local",
        "name": "赤泉河",
        "coordinates": [[-71.08, 27.82], [-71.04, 27.91], [-71.0, 28.0]],
    },
    {
        "id": "adde-river-local",
        "name": "阿德河",
        "coordinates": [[-71.0, 28.0], [-70.91, 27.98], [-70.79, 27.97]],
    },
]

CROSSINGS = [
    {
        "id": "three-rivers-bridge-group",
        "name": "三汇桥渡群",
        "crossing_type": "bridge_and_ferry",
        "coordinates": [-71.0, 28.0],
    }
]


def _pixel(coordinates: list[float]) -> tuple[int, int]:
    longitude, latitude = coordinates
    x = (longitude - BOUNDS["min_longitude"]) / (BOUNDS["max_longitude"] - BOUNDS["min_longitude"])
    y = (BOUNDS["max_latitude"] - latitude) / (BOUNDS["max_latitude"] - BOUNDS["min_latitude"])
    return round(x * WIDTH), round(y * HEIGHT)


def _font(size: int) -> ImageFont.ImageFont:
    for path in (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/simhei.ttf")):
        if path.exists():
            return ImageFont.truetype(str(path), size=size)
    return ImageFont.load_default()


def _geojson(items: list[dict[str, Any]], geometry_type: str) -> dict[str, Any]:
    features = []
    for item in items:
        properties = {key: value for key, value in item.items() if key != "coordinates"}
        features.append(
            {
                "type": "Feature",
                "id": item["id"],
                "properties": properties,
                "geometry": {"type": geometry_type, "coordinates": item["coordinates"]},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def _dump(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _distance_to_segment(
    point: tuple[float, float], first: tuple[int, int], second: tuple[int, int]
) -> float:
    px, py = point
    x1, y1 = first
    x2, y2 = second
    dx = x2 - x1
    dy = y2 - y1
    if dx == 0 and dy == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    ratio = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    x = x1 + ratio * dx
    y = y1 + ratio * dy
    return ((px - x) ** 2 + (py - y) ** 2) ** 0.5


def _line_segments(items: list[dict[str, Any]]) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    result = []
    for item in items:
        points = [_pixel(value) for value in item["coordinates"]]
        result.extend(zip(points, points[1:], strict=False))
    return result


def _building_footprints() -> list[tuple[int, int, int, int]]:
    rng = random.Random(24816)
    blocked = [(segment, 52) for segment in _line_segments(RIVERS)]
    blocked.extend((segment, 28) for segment in _line_segments(ROADS))
    feature_points = [_pixel(feature["coordinates"]) for feature in FEATURES]
    buildings: list[tuple[int, int, int, int]] = []
    attempts = 0
    while len(buildings) < 165 and attempts < 6000:
        attempts += 1
        x = rng.randint(130, WIDTH - 130)
        y = rng.randint(190, HEIGHT - 130)
        width = rng.randint(22, 62)
        height = rng.randint(18, 48)
        center = (x, y)
        if any(
            _distance_to_segment(center, *segment) < clearance for segment, clearance in blocked
        ):
            continue
        if any(((x - fx) ** 2 + (y - fy) ** 2) ** 0.5 < 65 for fx, fy in feature_points):
            continue
        if any(
            abs(x - bx) < (width + bw) / 2 + 10 and abs(y - by) < (height + bh) / 2 + 10
            for bx, by, bw, bh in buildings
        ):
            continue
        buildings.append((x, y, width, height))
    return buildings


def _road_width(road_type: str) -> tuple[int, int, tuple[int, int, int], tuple[int, int, int]]:
    if road_type == "stone_trade_road":
        return 30, 20, (206, 174, 102), (255, 244, 205)
    if road_type in {"city_stone_road", "embankment_road"}:
        return 23, 15, (197, 193, 183), (255, 255, 252)
    if road_type in {"service_road", "city_street"}:
        return 18, 11, (205, 202, 194), (252, 252, 249)
    return 12, 7, (210, 207, 198), (248, 248, 244)


def _draw_map(buildings: list[tuple[int, int, int, int]]) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), (235, 233, 225))
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rounded_rectangle((95, 190, 490, 570), radius=35, fill=(205, 226, 197, 255))
    draw.rounded_rectangle((1130, 1020, 1510, 1450), radius=35, fill=(207, 228, 201, 255))

    for river in RIVERS:
        points = [_pixel(value) for value in river["coordinates"]]
        draw.line(points, fill=(126, 185, 215, 255), width=64, joint="curve")
        draw.line(points, fill=(166, 213, 234, 255), width=50, joint="curve")

    for x, y, width, height in buildings:
        draw.rectangle(
            (x - width // 2, y - height // 2, x + width // 2, y + height // 2),
            fill=(210, 207, 198, 255),
            outline=(188, 184, 174, 255),
            width=2,
        )

    for road in ROADS:
        points = [_pixel(value) for value in road["coordinates"]]
        casing, inner, casing_color, inner_color = _road_width(road["road_type"])
        draw.line(points, fill=(*casing_color, 255), width=casing, joint="curve")
        draw.line(points, fill=(*inner_color, 255), width=inner, joint="curve")

    label_font = _font(22)
    poi_font = _font(24)
    title_font = _font(30)
    small_font = _font(18)
    for road in ROADS[:4]:
        points = [_pixel(value) for value in road["coordinates"]]
        x, y = points[len(points) // 2]
        draw.text((x, y - 18), road["name"], font=label_font, fill=(104, 102, 96), anchor="ms")

    for feature in FEATURES:
        x, y = _pixel(feature["coordinates"])
        color = (51, 119, 179, 255) if feature["feature_type"] != "port" else (31, 143, 160, 255)
        draw.ellipse((x - 13, y - 13, x + 13, y + 13), fill=color, outline="white", width=4)
        draw.text(
            (x + 18, y),
            feature["name"],
            font=poi_font,
            fill=(58, 58, 55),
            anchor="lm",
            stroke_width=3,
            stroke_fill=(246, 245, 240),
        )

    for crossing in CROSSINGS:
        x, y = _pixel(crossing["coordinates"])
        draw.rectangle(
            (x - 30, y - 9, x + 30, y + 9), fill=(250, 250, 247), outline=(121, 118, 108), width=3
        )

    draw.rounded_rectangle(
        (28, 28, 360, 104),
        radius=16,
        fill=(255, 255, 253, 235),
        outline=(190, 188, 181, 255),
        width=2,
    )
    draw.text((48, 43), "澜誓城", font=title_font, fill=(45, 48, 50))
    draw.text((195, 56), "详细导航图 · 候选", font=small_font, fill=(115, 116, 116))
    draw.polygon([(1510, 70), (1530, 112), (1510, 101), (1490, 112)], fill=(45, 54, 61))
    draw.text((1510, 120), "N", font=small_font, fill=(45, 54, 61), anchor="ma")
    draw.line((60, 1510, 255, 1510), fill=(47, 49, 49), width=5)
    draw.line((60, 1499, 60, 1521), fill=(47, 49, 49), width=4)
    draw.line((255, 1499, 255, 1521), fill=(47, 49, 49), width=4)
    draw.text((157, 1477), "约 5 km", font=small_font, fill=(47, 49, 49), anchor="ma")
    return image


def _draw_ai_preview() -> Image.Image:
    image = Image.open(AI_BASE).convert("RGB").resize((WIDTH, HEIGHT), Image.Resampling.LANCZOS)
    draw = ImageDraw.Draw(image, "RGBA")
    label_font = _font(22)
    poi_font = _font(24)
    title_font = _font(30)
    small_font = _font(18)
    for road in ROADS[:4]:
        points = [_pixel(value) for value in road["coordinates"]]
        x, y = points[len(points) // 2]
        draw.text(
            (x, y - 18),
            road["name"],
            font=label_font,
            fill=(104, 102, 96),
            anchor="ms",
            stroke_width=4,
            stroke_fill=(250, 249, 245),
        )
    for feature in FEATURES:
        x, y = _pixel(feature["coordinates"])
        color = (51, 119, 179, 255) if feature["feature_type"] != "port" else (31, 143, 160, 255)
        draw.ellipse((x - 13, y - 13, x + 13, y + 13), fill=color, outline="white", width=4)
        draw.text(
            (x + 18, y),
            feature["name"],
            font=poi_font,
            fill=(58, 58, 55),
            anchor="lm",
            stroke_width=3,
            stroke_fill=(246, 245, 240),
        )
    for crossing in CROSSINGS:
        x, y = _pixel(crossing["coordinates"])
        draw.rectangle(
            (x - 30, y - 9, x + 30, y + 9),
            fill=(250, 250, 247),
            outline=(121, 118, 108),
            width=3,
        )
    draw.rounded_rectangle(
        (28, 28, 360, 104),
        radius=16,
        fill=(255, 255, 253, 235),
        outline=(190, 188, 181, 255),
        width=2,
    )
    draw.text((48, 43), "澜誓城", font=title_font, fill=(45, 48, 50))
    draw.text(
        (195, 56),
        "AI视觉底图 · 候选",
        font=small_font,
        fill=(115, 116, 116),
    )
    draw.polygon([(1510, 70), (1530, 112), (1510, 101), (1490, 112)], fill=(45, 54, 61))
    draw.text((1510, 120), "N", font=small_font, fill=(45, 54, 61), anchor="ma")
    draw.line((60, 1510, 255, 1510), fill=(47, 49, 49), width=5)
    draw.line((60, 1499, 60, 1521), fill=(47, 49, 49), width=4)
    draw.line((255, 1499, 255, 1521), fill=(47, 49, 49), width=4)
    draw.text((157, 1477), "约 5 km", font=small_font, fill=(47, 49, 49), anchor="ma")
    return image


def _svg_ai_map() -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        '<image href="detail-map-ai-base.png" x="0" y="0" '
        'width="1600" height="1600" preserveAspectRatio="none"/>',
        '<g id="road-labels" font-family="Microsoft YaHei, sans-serif" '
        'font-size="22" fill="#686661" text-anchor="middle" '
        'paint-order="stroke" stroke="#f8f7f2" stroke-width="5">',
    ]
    for road in ROADS[:4]:
        points = [_pixel(value) for value in road["coordinates"]]
        x, y = points[len(points) // 2]
        parts.append(f'<text x="{x}" y="{y - 18}">{escape(road["name"])}</text>')
    parts.append("</g>")
    parts.append('<g id="crossings" fill="#fafaf7" stroke="#79766d" stroke-width="3">')
    for crossing in CROSSINGS:
        x, y = _pixel(crossing["coordinates"])
        parts.append(f'<rect x="{x - 30}" y="{y - 9}" width="60" height="18" rx="2"/>')
    parts.append("</g>")
    parts.append(
        '<g id="pois" font-family="Microsoft YaHei, sans-serif" font-size="24" '
        'fill="#3a3a37" paint-order="stroke" stroke="#f6f5f0" stroke-width="6">'
    )
    for feature in FEATURES:
        x, y = _pixel(feature["coordinates"])
        color = "#3377b3" if feature["feature_type"] != "port" else "#1f8fa0"
        parts.append(
            f'<circle cx="{x}" cy="{y}" r="13" fill="{color}" '
            f'stroke="white" stroke-width="4"/><text x="{x + 18}" '
            f'y="{y + 8}">{escape(feature["name"])}</text>'
        )
    parts.extend(
        [
            "</g>",
            '<g id="ui" font-family="Microsoft YaHei, sans-serif">'
            '<rect x="28" y="28" width="355" height="76" rx="16" fill="white" '
            'fill-opacity=".94" stroke="#bebcb5" stroke-width="2"/>'
            '<text x="48" y="77" font-size="30" fill="#2d3032">澜誓城</text>'
            '<text x="195" y="73" font-size="18" fill="#737474">'
            "AI视觉底图 · 候选</text>",
            '<path d="M1510 70 L1530 112 L1510 101 L1490 112 Z" fill="#2d363d"/>'
            '<text x="1510" y="140" text-anchor="middle" font-size="18" '
            'fill="#2d363d">N</text>',
            '<path d="M60 1510 H255 M60 1499 V1521 M255 1499 V1521" '
            'stroke="#2f3131" stroke-width="5"/>'
            '<text x="157" y="1480" text-anchor="middle" font-size="18" '
            'fill="#2f3131">约 5 km</text></g>',
            "</svg>",
        ]
    )
    return "".join(parts)


def _svg_map(buildings: list[tuple[int, int, int, int]]) -> str:
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">',
        '<rect width="1600" height="1600" fill="#ebe9e1"/>',
        '<g id="parks" fill="#cde2c5">'
        '<rect x="95" y="190" width="395" height="380" rx="35"/>'
        '<rect x="1130" y="1020" width="380" height="430" rx="35"/></g>',
    ]
    parts.append('<g id="water" fill="none" stroke-linecap="round" stroke-linejoin="round">')
    for river in RIVERS:
        points = " ".join(f"{x},{y}" for x, y in (_pixel(value) for value in river["coordinates"]))
        parts.append(
            f'<polyline points="{points}" stroke="#7eb9d7" stroke-width="64"/>'
            f'<polyline points="{points}" stroke="#a6d5ea" stroke-width="50"/>'
        )
    parts.append("</g>")
    parts.append('<g id="buildings" fill="#d2cfc6" stroke="#bbb7ad" stroke-width="2">')
    for x, y, width, height in buildings:
        parts.append(
            f'<rect x="{x - width / 2:.1f}" y="{y - height / 2:.1f}" '
            f'width="{width}" height="{height}" rx="3"/>'
        )
    parts.append("</g>")
    parts.append('<g id="roads" fill="none" stroke-linecap="round" stroke-linejoin="round">')
    for road in ROADS:
        points = " ".join(f"{x},{y}" for x, y in (_pixel(value) for value in road["coordinates"]))
        casing, inner, casing_color, inner_color = _road_width(road["road_type"])
        parts.append(
            f'<polyline points="{points}" stroke="rgb{casing_color}" '
            f'stroke-width="{casing}"/><polyline points="{points}" '
            f'stroke="rgb{inner_color}" stroke-width="{inner}"/>'
        )
    parts.append("</g>")
    parts.append(
        '<g id="road-labels" font-family="Microsoft YaHei, sans-serif" '
        'font-size="22" fill="#686661" text-anchor="middle" '
        'paint-order="stroke" stroke="#f5f4ef" stroke-width="5">'
    )
    for road in ROADS[:4]:
        points = [_pixel(value) for value in road["coordinates"]]
        x, y = points[len(points) // 2]
        parts.append(f'<text x="{x}" y="{y - 18}">{escape(road["name"])}</text>')
    parts.append("</g>")
    parts.append('<g id="crossings" fill="#fafaf7" stroke="#79766d" stroke-width="3">')
    for crossing in CROSSINGS:
        x, y = _pixel(crossing["coordinates"])
        parts.append(f'<rect x="{x - 30}" y="{y - 9}" width="60" height="18" rx="2"/>')
    parts.append("</g>")
    parts.append(
        '<g id="pois" font-family="Microsoft YaHei, sans-serif" font-size="24" '
        'fill="#3a3a37" paint-order="stroke" stroke="#f6f5f0" stroke-width="6">'
    )
    for feature in FEATURES:
        x, y = _pixel(feature["coordinates"])
        color = "#3377b3" if feature["feature_type"] != "port" else "#1f8fa0"
        parts.append(
            f'<circle cx="{x}" cy="{y}" r="13" fill="{color}" '
            f'stroke="white" stroke-width="4"/><text x="{x + 18}" '
            f'y="{y + 8}">{escape(feature["name"])}</text>'
        )
    parts.append("</g>")
    parts.extend(
        [
            '<g id="ui" font-family="Microsoft YaHei, sans-serif">'
            '<rect x="28" y="28" width="332" height="76" rx="16" fill="white" '
            'fill-opacity=".94" stroke="#bebcb5" stroke-width="2"/>'
            '<text x="48" y="77" font-size="30" fill="#2d3032">澜誓城</text>'
            '<text x="195" y="73" font-size="18" fill="#737474">'
            "详细导航图 · 候选</text>",
            '<path d="M1510 70 L1530 112 L1510 101 L1490 112 Z" fill="#2d363d"/>'
            '<text x="1510" y="140" text-anchor="middle" font-size="18" '
            'fill="#2d363d">N</text>',
            '<path d="M60 1510 H255 M60 1499 V1521 M255 1499 V1521" '
            'stroke="#2f3131" stroke-width="5"/>'
            '<text x="157" y="1480" text-anchor="middle" font-size="18" '
            'fill="#2f3131">约 5 km</text></g>',
            "</svg>",
        ]
    )
    return "".join(parts)


def generate() -> dict[str, Any]:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    buildings = _building_footprints()
    use_ai_base = AI_BASE.exists()
    image = _draw_ai_preview() if use_ai_base else _draw_map(buildings)
    image.save(OUTPUT_ROOT / "detail-map.png", optimize=True)
    svg = _svg_ai_map() if use_ai_base else _svg_map(buildings)
    (OUTPUT_ROOT / "detail-map.svg").write_text(svg, encoding="utf-8")
    _dump(OUTPUT_ROOT / "features.geojson", _geojson(FEATURES, "Point"))
    _dump(OUTPUT_ROOT / "roads.geojson", _geojson(ROADS, "LineString"))
    _dump(OUTPUT_ROOT / "rivers.geojson", _geojson(RIVERS, "LineString"))
    _dump(OUTPUT_ROOT / "crossings.geojson", _geojson(CROSSINGS, "Point"))
    metadata = {
        "schema_version": 1,
        "id": "oathflow",
        "name": "澜誓城详细地图",
        "review_status": "candidate",
        "width": WIDTH,
        "height": HEIGHT,
        "bounds": BOUNDS,
        "area_radius_km": 20,
        "asset_path": "navigation/settlements/oathflow/detail-map.svg",
        "feature_count": len(FEATURES),
        "road_count": len(ROADS),
        "river_count": len(RIVERS),
        "crossing_count": len(CROSSINGS),
        "building_count": len(buildings),
        "visual_source": "image_generation_plus_vector_overlay"
        if use_ai_base
        else "procedural_vector",
        "ai_base_sha256": (
            hashlib.sha256(AI_BASE.read_bytes()).hexdigest() if use_ai_base else None
        ),
        "content_sha256": hashlib.sha256(svg.encode("utf-8")).hexdigest(),
        "warning": "设施名称来自已确认设定，街区和建筑几何仍为候选。",
    }
    _dump(OUTPUT_ROOT / "metadata.json", metadata)
    (OUTPUT_ROOT / "README.md").write_text(
        "# 澜誓城详细地图\n\n"
        "状态：`candidate`。视觉底图由图像生成接口根据程序化导航参考图生成；"
        "运行资源为生成式PNG底图加SVG准确标签/POI覆盖层，普通PNG作同款预览。"
        "坐标、设施名称、区域边界和路线数据仍由GeoJSON及本地代码掌控。\n\n"
        "运行 `scripts/generate_settlement_detail_maps.py` 可确定性重建全部文件。\n",
        encoding="utf-8",
    )
    return metadata


def main() -> None:
    print(json.dumps(generate(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
