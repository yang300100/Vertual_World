"""一次性把旧诺里亚 CSV 转成可插拔世界观的结构化地点。"""

from __future__ import annotations

import csv
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs/worldbuilding/maps/map_new/data"
TARGET = ROOT / "docs/world"


def main() -> None:
    translations = json.loads((SOURCE / "translations.json").read_text(encoding="utf-8"))
    with (SOURCE / "Noryia Burgs 2026-08-29-11-38.csv").open(encoding="utf-8-sig", newline="") as handle:
        burgs = list(csv.DictReader(handle))
    locations = []
    used_names = set()
    for row in burgs:
        name = translations.get("cities", {}).get(row["Id"], {}).get("display_name", row["Burg"])
        if name in used_names:
            name = f"{name}·{row['Id']}"
        used_names.add(name)
        population = int(row["Population"])
        locations.append({
            "key": f"burg-{row['Id']}", "name": name,
            "kind": "city" if population >= 5000 else "town",
            "longitude": float(row["Longitude"]), "latitude": float(row["Latitude"]),
            "area_radius_km": max(2.0, min(30.0, 2.0 + population**0.5 / 18.0)),
            "area_priority": 20 if row["Capital"] == "capital" else 10,
            "resources": {"source_id": int(row["Id"]), "population": population,
                          "elevation_m": float(row["Elevation (m)"]),
                          "capital": int(row["Capital"] == "capital"),
                          "port": int(row["Port"] == "port")},
        })
    features = []
    with (SOURCE / "Noryia States 2026-08-29-11-38.csv").open(encoding="utf-8-sig", newline="") as handle:
        states = list(csv.DictReader(handle))
    for state in states:
        capital = next((row for row in burgs if row["State"] == state["State"] and row["Capital"] == "capital"), None)
        if capital is None:
            continue
        features.append({
            "key": f"state-{state['Id']}",
            "name": translations.get("states", {}).get(state["State"], {}).get("full_name_display", state["Full Name"]),
            "feature_type": "capital", "location": f"burg-{capital['Id']}",
            "longitude": float(capital["Longitude"]), "latitude": float(capital["Latitude"]),
            "metadata": {"source": "Noryia States 2026-08-29-11-38.csv", "state_id": int(state["Id"]),
                         "state": state["State"], "full_name": state["Full Name"],
                         "form": state["Form"], "culture": state["Culture"],
                         "population": int(state["Total Population"])},
        })
    maps = [{"key": "z0", "name": "诺里亚世界地图", "kind": "terrain",
             "asset_path": "map_new/Noryia.svg", "width_pixels": 10015, "height_pixels": 5008}]
    city_index = json.loads((SOURCE.parent / "cities/index.json").read_text(encoding="utf-8"))
    location_by_source = {int(place["resources"]["source_id"]): place for place in locations}
    for city in city_index["cities"]:
        asset = ROOT / "docs/worldbuilding/maps" / city["asset_path"]
        place = location_by_source.get(int(city["id"]))
        if not city.get("downloaded") or place is None or not asset.is_file() or asset.stat().st_size < 20_000:
            continue
        delta = max(0.025, place["area_radius_km"] / 111.0)
        maps.append({
            "key": f"city-{city['id']}", "name": f"{place['name']}详细地图",
            "kind": "settlement", "asset_path": city["asset_path"],
            "width_pixels": 1280, "height_pixels": 720,
            "zoom_level": 2, "map_role": "detail", "location": place["key"],
            "min_longitude": max(-180.0, place["longitude"] - delta),
            "max_longitude": min(180.0, place["longitude"] + delta),
            "min_latitude": max(-90.0, place["latitude"] - delta),
            "max_latitude": min(90.0, place["latitude"] + delta),
        })
    TARGET.mkdir(parents=True, exist_ok=True)
    for name, records in (("locations", locations), ("map_features", features), ("maps", maps)):
        (TARGET / f"{name}.json").write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
