"""Noryia 导航数据加载、采样与数据库导入。

- TerrainService：从生成的候选产物(terrain-index.json / terrain.geojson /
  movement-rules.json / crossings.geojson)采样任意经纬度的地形上下文，供路线规划与前端展示。
- NavigationDatasetImporter：把 route-graph.json 导入 SQLite 的
  navigation_datasets / navigation_nodes / navigation_edges 表。

产物契约见 docs/design/08-noryia-terrain-routing-implementation.md。
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from world_engine.geo import great_circle_distance_km
from world_engine.repository import to_iso


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _bin_key(
    longitude: float, latitude: float, bin_degrees: float, lon_w: float, lat_s: float
) -> str:
    x_bucket = math.floor((longitude - lon_w) / bin_degrees)
    y_bucket = math.floor((latitude - lat_s) / bin_degrees)
    return f"{x_bucket}:{y_bucket}"


class TerrainService:
    """从导航产物读取地形栅格；只读、无副作用，不把网格写入人物表。"""

    @classmethod
    def from_memory(
        cls,
        cells: dict[int, dict[str, Any]],
        crossings: list[dict[str, Any]] | None = None,
        movement_rules: dict[str, Any] | None = None,
    ) -> TerrainService:
        """用内存中的格网构造服务，供单元测试与合成地形使用。"""
        instance = cls.__new__(cls)
        instance.asset_root = None
        instance.bin_degrees = 0.5
        instance.bounds = {
            "min_longitude": -180.0,
            "max_longitude": 180.0,
            "min_latitude": -90.0,
            "max_latitude": 90.0,
        }
        instance._lon_w = -180.0
        instance._lat_s = -90.0
        instance._id_lonlat = {
            int(cell_id): (float(cell["longitude"]), float(cell["latitude"]))
            for cell_id, cell in cells.items()
        }
        instance._bins = {}
        for cell_id, cell in cells.items():
            key = _bin_key(
                float(cell["longitude"]),
                float(cell["latitude"]),
                instance.bin_degrees,
                instance._lon_w,
                instance._lat_s,
            )
            instance._bins.setdefault(key, []).append(int(cell_id))
        instance._cells = {int(k): dict(v) for k, v in cells.items()}
        for cell in instance._cells.values():
            cell.setdefault("neighbors", [])
            cell.setdefault("road_ids", [])
            cell.setdefault("river_ids", [])
            cell.setdefault("water_kind", None)
            cell.setdefault("slope_degrees", 0.0)
            cell.setdefault("surface_type", "grassland")
            cell.setdefault("elevation_m", 0)
            cell.setdefault("elevation_code", 0)
            cell.setdefault("state_id", None)
            cell.setdefault("province_id", None)
            cell.setdefault("crossing_instances", [])
        instance._crossings = crossings or []
        instance.rules = movement_rules or _DEFAULT_RULES
        instance._crossing_by_cell = {}
        instance._build_crossing_map()
        return instance

    def __init__(self, asset_root: Path, movement_rules: dict[str, Any] | None = None) -> None:
        self.asset_root = Path(asset_root)
        index = _load_json(self.asset_root / "terrain-index.json")
        self.bin_degrees = float(index["bin_degrees"])
        self.bounds = index["bounds"]
        self._lon_w = float(self.bounds["min_longitude"])
        self._lat_s = float(self.bounds["min_latitude"])
        self._id_lonlat = {
            item["id"]: (item["longitude"], item["latitude"]) for item in index["cells"]
        }
        self._bins = index["bins"]
        features = _load_json(self.asset_root / "terrain.geojson")["features"]
        self._cells: dict[int, dict[str, Any]] = {}
        for feature in features:
            props = feature["properties"]
            props["geometry"] = feature["geometry"]
            self._cells[int(props["cell_id"])] = props
        self._crossings = _load_json(self.asset_root / "crossings.geojson")["features"]
        self.rules = movement_rules or _load_json(self.asset_root / "movement-rules.json")
        self._crossing_by_cell: dict[int, dict[str, Any]] = {}
        self._build_crossing_map()

    def _build_crossing_map(self) -> None:
        """把门户坐标吸附到最近格，记录可通行门户信息。

        世界级格网很粗，门户点常远离其格中心，因此不设距离上限，始终吸附到包含该点的格，
        避免门户被误丢。
        """
        for crossing in self._crossings:
            coordinate = crossing["geometry"]["coordinates"]
            cell = self.nearest_cell(coordinate[0], coordinate[1])
            if cell is None:
                continue
            cell_id = int(cell["cell_id"])
            prior = self._crossing_by_cell.setdefault(cell_id, crossing)
            # 同类门户去重：优先桥/渡口/港口。
            order = {"ferry": 3, "port": 2, "bridge": 1, "ford": 1, "mountain_pass": 1}
            if order.get(crossing["properties"]["crossing_type"], 0) < order.get(
                prior["properties"]["crossing_type"], 0
            ):
                self._crossing_by_cell[cell_id] = crossing
            cell.setdefault("crossing_instances", [])
            cell["crossing_instances"].append(crossing)

    def crossing_at(self, cell_id: int) -> dict[str, Any] | None:
        return self._crossing_by_cell.get(int(cell_id))

    def crossing_type_at(self, cell_id: int) -> str | None:
        crossing = self.crossing_at(int(cell_id))
        return crossing["properties"]["crossing_type"] if crossing else None

    def all_cells(self) -> dict[int, dict[str, Any]]:
        return self._cells

    def cell(self, cell_id: int) -> dict[str, Any] | None:
        return self._cells.get(int(cell_id))

    def neighbors(self, cell_id: int) -> list[int]:
        cell = self._cells.get(int(cell_id))
        if cell is None:
            return []
        return [int(item) for item in cell.get("neighbors", [])]

    def nearest_cell(
        self, longitude: float, latitude: float, max_distance_km: float | None = None
    ) -> dict[str, Any] | None:
        candidates = self._candidate_ids(longitude, latitude)
        best = None
        best_distance = float("inf")
        for cell_id in candidates:
            coord = self._id_lonlat.get(int(cell_id))
            if coord is None:
                continue
            distance = great_circle_distance_km(longitude, latitude, coord[0], coord[1])
            if distance < best_distance:
                best = cell_id
                best_distance = distance
        if best is None:
            # 回退到全部格网，保证世界边缘采样正常。
            for cell_id, coord in self._id_lonlat.items():
                distance = great_circle_distance_km(longitude, latitude, coord[0], coord[1])
                if distance < best_distance:
                    best = cell_id
                    best_distance = distance
        if best is None:
            return None
        if max_distance_km is not None and best_distance > max_distance_km:
            return None
        return self._cells.get(int(best))

    def _candidate_ids(self, longitude: float, latitude: float) -> list[int]:
        # 汇总查询格及其周围 3×3 bin 的全部候选，避免只取第一个非空 bin 而漏掉更近格。
        base_x = math.floor((longitude - self._lon_w) / self.bin_degrees)
        base_y = math.floor((latitude - self._lat_s) / self.bin_degrees)
        candidates: list[int] = []
        seen: set[int] = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                items = self._bins.get(f"{base_x + dx}:{base_y + dy}")
                if not items:
                    continue
                for item in items:
                    cell_id = int(item)
                    if cell_id not in seen:
                        seen.add(cell_id)
                        candidates.append(cell_id)
        return candidates

    def sample(self, longitude: float, latitude: float) -> dict[str, Any] | None:
        cell = self.nearest_cell(longitude, latitude)
        if cell is None:
            return None
        return self._terrain_payload(cell)

    def _terrain_payload(self, cell: dict[str, Any]) -> dict[str, Any]:
        road_ids = cell.get("road_ids") or []
        road_multiplier = 1.0
        road_type = None
        for road_id in road_ids:
            multiplier = self._road_multiplier(road_id)
            if multiplier > road_multiplier:
                road_multiplier = multiplier
                road_type = road_id.rsplit(":", 1)[-1]
        crossing = self.crossing_at(int(cell["cell_id"]))
        return {
            "cell_id": int(cell["cell_id"]),
            "longitude": cell["longitude"],
            "latitude": cell["latitude"],
            "elevation_m": cell["elevation_m"],
            "elevation_code": cell["elevation_code"],
            "surface_type": cell["surface_type"],
            "slope_degrees": cell["slope_degrees"],
            "water_kind": cell["water_kind"],
            "state_id": cell["state_id"],
            "province_id": cell["province_id"],
            "road_ids": road_ids,
            "road_type": road_type,
            "road_speed_multiplier": round(road_multiplier, 3),
            "river_ids": cell.get("river_ids") or [],
            "crossing_type": crossing["properties"]["crossing_type"] if crossing else None,
            "crossing_name": crossing["properties"].get("name") if crossing else None,
        }

    def _road_multiplier(self, road_id: str) -> float:
        group = self._route_group_from_id(road_id)
        label = _ROAD_LABEL_BY_GROUP.get(group)
        return float(self.rules.get("road_speed_multipliers", {}).get(label, 1.0)) if label else 1.0

    @staticmethod
    def _route_group_from_id(road_id: str) -> str | None:
        parts = road_id.split(":")
        if len(parts) >= 3 and parts[0] == "route" and parts[1] in ("roads", "trails", "searoutes"):
            return parts[1]
        return None

    def cell_speed_kmh(
        self,
        cell: dict[str, Any],
        base_speed_kmh: float,
        movement_type: str,
    ) -> float:
        if movement_type in ("land", "underground"):
            surface_multiplier = float(
                self.rules.get("surface_speed_multipliers", {}).get(cell.get("surface_type"), 1.0)
            )
            road_multiplier = self._terrain_payload(cell).get("road_speed_multiplier", 1.0)
            slope_multiplier = self._slope_multiplier(float(cell.get("slope_degrees", 0.0)))
            speed = base_speed_kmh * surface_multiplier * road_multiplier * slope_multiplier
        else:
            # 船舶/飞行/小艇按载具基础速度推进，不叠加陆地地表倍率。
            speed = base_speed_kmh
        return max(0.1, speed)

    def _slope_multiplier(self, slope_degrees: float) -> float:
        for rule in self.rules.get("land_slope_rules", []):
            if slope_degrees <= float(rule["max_degrees"]):
                return float(rule["multiplier"])
        return 0.1

    def max_speed_kmh(
        self, movement_type: str, base_speed_kmh: float, vehicle_metadata: dict[str, Any]
    ) -> float:
        if movement_type == "flight":
            return float(vehicle_metadata.get("speed_kmh", base_speed_kmh))
        return float(base_speed_kmh)


_ROAD_LABEL_BY_GROUP = {
    "roads": "road",
    "trails": "trail",
}

_DEFAULT_RULES: dict[str, Any] = {
    "surface_speed_multipliers": {
        "marine": 0.0,
        "ocean": 0.0,
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
    "land_slope_rules": [
        {"max_degrees": 10, "multiplier": 1.0},
        {"max_degrees": 20, "multiplier": 0.8},
        {"max_degrees": 35, "multiplier": 0.45},
        {"max_degrees": 90, "multiplier": 0.0},
    ],
}


class NavigationDatasetImporter:
    """把 route-graph.json 导入 SQLite 导航表；产物默认 candidate。"""

    def __init__(self, asset_root: Path) -> None:
        self.asset_root = Path(asset_root)

    def import_candidate(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        name: str,
        source_sha256: str,
    ) -> str:
        metadata = _load_json(self.asset_root / "metadata.json")
        graph = _load_json(self.asset_root / "route-graph.json")
        dataset_id = str(uuid4())
        now = to_iso(datetime.now(UTC))
        connection.execute(
            """
            INSERT INTO navigation_datasets(
                id, world_id, name, asset_root, source_sha256, review_status,
                bounds_json, created_at
            ) VALUES (?, ?, ?, ?, ?, 'candidate', ?, ?)
            """,
            (
                dataset_id,
                world_id,
                name,
                str(self.asset_root.relative_to(Path(__file__).resolve().parents[1])).replace(
                    "\\", "/"
                ),
                source_sha256,
                json.dumps(metadata["bounds"], ensure_ascii=False),
                now,
            ),
        )
        node_rows: list[tuple[object, ...]] = []
        for node in graph["nodes"]:
            node_id = f"{dataset_id}:{node['id']}"
            node_type = _node_type_of(node)
            node_rows.append(
                (
                    node_id,
                    dataset_id,
                    node_type,
                    node["longitude"],
                    node["latitude"],
                    json.dumps(node, ensure_ascii=False),
                )
            )
        connection.executemany(
            """
            INSERT INTO navigation_nodes(
                id, dataset_id, node_type, longitude, latitude, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            node_rows,
        )
        edge_rows: list[tuple[object, ...]] = []
        for edge in graph["edges"]:
            source = edge.get("source")
            target = edge.get("target")
            if source is None or target is None:
                continue
            edge_rows.append(
                (
                    f"{dataset_id}:{edge['id']}",
                    dataset_id,
                    f"{dataset_id}:{source}",
                    f"{dataset_id}:{target}",
                    edge.get("edge_type", "road"),
                    edge.get("distance_km", 0.0),
                    edge.get("speed_multiplier", 1.0),
                    json.dumps(edge.get("allowed_movement_types", []), ensure_ascii=False),
                    json.dumps(edge.get("polyline", []), ensure_ascii=False),
                    json.dumps(edge.get("requirements", {}), ensure_ascii=False),
                )
            )
        connection.executemany(
            """
            INSERT INTO navigation_edges(
                id, dataset_id, from_node_id, to_node_id, edge_type, distance_km,
                speed_multiplier, allowed_modes_json, polyline_json, requirements_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            edge_rows,
        )
        return dataset_id

    def mark_approved(self, connection: sqlite3.Connection, dataset_id: str) -> None:
        now = to_iso(datetime.now(UTC))
        connection.execute(
            """
            UPDATE navigation_datasets
            SET review_status = 'approved', approved_at = ?
            WHERE id = ? AND review_status = 'candidate'
            """,
            (now, dataset_id),
        )


def _node_type_of(node: dict[str, Any]) -> str:
    if node.get("kind") == "crossing":
        crossing_type = node.get("crossing_type")
        mapping = {
            "bridge": "bridge",
            "ford": "ford",
            "ferry": "ferry",
            "port": "port",
            "mountain_pass": "pass",
        }
        return mapping.get(crossing_type, "road")
    if node.get("is_port"):
        return "port"
    return "road"
