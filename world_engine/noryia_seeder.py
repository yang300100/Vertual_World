"""把 Noryia 的人文地理导入为独立、可运行的新世界。"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from world_engine.database import Database
from world_engine.repository import WorldRepository, to_iso

ROOT = Path(__file__).resolve().parents[1]
BURGS_CSV = ROOT / "docs/worldbuilding/maps/map_new/data/Noryia Burgs 2026-08-29-11-38.csv"
STATES_CSV = ROOT / "docs/worldbuilding/maps/map_new/data/Noryia States 2026-08-29-11-38.csv"
WORLD_NAME = "伊瑟拉·诺里亚"


def create_noryia_world(database: Database, name: str = WORLD_NAME) -> str:
    """创建只使用 Noryia 人文地理数据的正式运行世界。"""

    repository = WorldRepository()
    with database.write() as connection:
        world_id = repository.create_world(
            connection, name=name, minutes_per_tick=60, time_scale=1.0, seed_demo=False
        )
        _seed_settlements(connection, world_id)
        _seed_capital_features(connection, world_id)
        Database._ensure_noryia_city_detail_maps(connection)
    return world_id


def _seed_settlements(connection: sqlite3.Connection, world_id: str) -> None:
    with BURGS_CSV.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    translations = json.loads(
        (ROOT / "docs/worldbuilding/maps/map_new/data/translations.json").read_text(
            encoding="utf-8"
        )
    )
    city_translations = translations.get("cities", {})

    name_counts = Counter(row["Burg"] for row in rows)
    payload = []
    used_names: set[str] = set()
    for row in rows:
        population = int(row["Population"])
        name = city_translations.get(row["Id"], {}).get("display_name", row["Burg"])
        if name in used_names:
            name = f"{name}·{row['Id']}"
        used_names.add(name)
        if name_counts[name] > 1:
            name = f"{name}（{row['Province']} #{row['Id']}）"
        radius = max(2.0, min(30.0, 2.0 + population**0.5 / 18.0))
        resources = {
            "source_id": int(row["Id"]),
            "population": population,
            "elevation_m": float(row["Elevation (m)"]),
            "capital": int(row["Capital"] == "capital"),
            "port": int(row["Port"] == "port"),
        }
        payload.append(
            (
                str(uuid5(NAMESPACE_URL, f"{world_id}:noryia:burg:{row['Id']}")),
                world_id,
                name,
                "city" if population >= 5000 else "town",
                json.dumps(resources, ensure_ascii=False),
                float(row["Longitude"]),
                float(row["Latitude"]),
                radius,
                20 if resources["capital"] else 10,
            )
        )
    connection.executemany(
        """
        INSERT INTO locations(
            id, world_id, name, kind, resources_json, longitude, latitude,
            area_radius_km, area_priority
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        payload,
    )


def _seed_capital_features(connection: sqlite3.Connection, world_id: str) -> None:
    now = to_iso(datetime.now(UTC))
    translations = json.loads(
        (ROOT / "docs/worldbuilding/maps/map_new/data/translations.json").read_text(
            encoding="utf-8"
        )
    )
    with BURGS_CSV.open(encoding="utf-8-sig", newline="") as handle:
        burgs = list(csv.DictReader(handle))
    name_counts = Counter(row["Burg"] for row in burgs)
    capital_locations = {}
    for burg in burgs:
        if burg["Capital"] != "capital":
            continue
        name = translations.get("cities", {}).get(burg["Id"], {}).get("display_name", burg["Burg"])
        if name_counts[name] > 1:
            name = f"{name}（{burg['Province']} #{burg['Id']}）"
        capital_locations[burg["State"]] = name
    with STATES_CSV.open(encoding="utf-8-sig", newline="") as handle:
        states = list(csv.DictReader(handle))
    for state in states:
        capital_name = capital_locations.get(state["State"])
        if capital_name is None:
            continue
        capital = connection.execute(
            "SELECT id, longitude, latitude FROM locations WHERE world_id = ? AND name = ?",
            (world_id, capital_name),
        ).fetchone()
        if capital is None:
            continue
        metadata = {
            "source": "Noryia States 2026-08-29-11-38.csv",
            "state_id": int(state["Id"]),
            "state": state["State"],
            "full_name": state["Full Name"],
            "form": state["Form"],
            "culture": state["Culture"],
            "population": int(state["Total Population"]),
        }
        connection.execute(
            """
            INSERT INTO map_features(
                id, world_id, name, feature_type, longitude, latitude,
                location_id, is_known, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, 'capital', ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                str(uuid5(NAMESPACE_URL, f"{world_id}:noryia:state:{state['Id']}")),
                world_id,
                translations.get("states", {})
                .get(state["State"], {})
                .get("full_name_display", state["Full Name"]),
                capital["longitude"],
                capital["latitude"],
                capital["id"],
                json.dumps(metadata, ensure_ascii=False),
                now,
                now,
            ),
        )


def _slug(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "-" for character in value).strip(
        "-"
    )
