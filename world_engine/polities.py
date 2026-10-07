"""Yuki 从有效地图目录核对全部政体，保留设定仍有效的地方政权。"""

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def current_polities(root=ROOT):
    features = json.loads((root / "docs/world/map_features.json").read_text(encoding="utf-8"))
    locations = {p["key"]: p for p in json.loads((root / "docs/world/locations.json").read_text(encoding="utf-8"))}
    source = root / "docs/worldbuilding/maps/map_new/data/Noryia States 2026-08-29-11-38.csv"
    with source.open(encoding="utf-8-sig", newline="") as handle:
        states = {int(s["Id"]) for s in csv.DictReader(handle) if int(s["Id"]) != 0}
    capitals = [f for f in features if f["feature_type"] == "capital" and f.get("metadata", {}).get("state_id")]
    if {f["metadata"]["state_id"] for f in capitals} != states or len(capitals) != len(states):
        raise ValueError("政体目录与原始地图不一致，Yuki 不能漏掉任何国家")
    result = []
    for feature in sorted(capitals, key=lambda f: f["metadata"]["state_id"]):
        place = locations[feature["location"]]
        result.append({"key": feature["key"], "name": feature["name"], "capital_key": place["key"],
                       "capital_name": place["name"], "longitude": place["longitude"], "latitude": place["latitude"],
                       "source": "docs/world/map_features.json", "form": feature["metadata"]["form"]})
    result.append({"key": "adren-local", "name": "阿德伦议约王国（地方政权）", "capital_key": "oathflow-local",
                   "capital_name": "澜誓城", "longitude": -71.0, "latitude": 28.0, "form": "Kingdom",
                   "source": "docs/worldbuilding/29-noryia-nations-and-factions.md；world_engine/seeder.py"})
    return result
