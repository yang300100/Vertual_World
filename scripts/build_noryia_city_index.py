"""从 Noryia 城镇导出构建本地地图资产索引。"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs/worldbuilding/maps/map_new/data/Noryia Burgs 2026-08-29-11-38.csv"
CITIES = ROOT / "docs/worldbuilding/maps/map_new/cities"
OUTPUT = CITIES / "index.json"


def slug(value: str) -> str:
    """生成稳定文件名，避免城镇同名时仍可由编号区分。"""

    return re.sub(r"(^-|-$)", "", re.sub(r"[^a-z0-9]+", "-", value.lower())) or "unnamed"


def main() -> None:
    with DATA.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))

    cities = []
    for row in rows:
        asset_name = f"{int(row['Id']):04d}-{slug(row['Burg'])}.png"
        cities.append(
            {
                "id": int(row["Id"]),
                "name": row["Burg"],
                "state": row["State"],
                "province": row["Province"],
                "population": int(row["Population"]),
                "longitude": float(row["Longitude"]),
                "latitude": float(row["Latitude"]),
                "elevation_m": float(row["Elevation (m)"]),
                "is_capital": row["Capital"] == "capital",
                "is_port": row["Port"] == "port",
                "source_url": row["Preview link"],
                "asset_path": f"map_new/cities/{asset_name}",
                "downloaded": (CITIES / asset_name).is_file(),
            }
        )

    OUTPUT.write_text(
        json.dumps(
            {"source": str(DATA.relative_to(ROOT)).replace("\\", "/"), "cities": cities},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
