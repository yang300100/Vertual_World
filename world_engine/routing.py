"""分层路线规划器。

RoutingPlanner 在 Noryia 导航格网上做 A*：先局部连接起点/终点到最近可通行格，
再从高层网格跨区域搜索，最后收敛到精确目标。返回可验证的路线折线、总距离、预计
耗时、途经地形摘要、所需桥/港/渡口、不可达原因与数据集版本。禁止退回穿模直线。

见 docs/design/08-noryia-terrain-routing-implementation.md 的"路线规划器"节。
"""

from __future__ import annotations

import heapq
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from world_engine.config import PROJECT_ROOT
from world_engine.geo import great_circle_distance_km
from world_engine.navigation import TerrainService

SLOPE_BLOCK_DEGREES = 35.0
LAND_CROSSINGS = ("bridge", "ford", "ferry", "port")


@dataclass
class RoutePlan:
    reachable: bool
    origin: tuple[float, float]
    destination: tuple[float, float]
    movement_type: str
    polyline: list[list[float]] = field(default_factory=list)
    cell_ids: list[int] = field(default_factory=list)
    distance_km: float = 0.0
    estimated_hours: float = 0.0
    segments: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    requirements: list[str] = field(default_factory=list)
    unreachable_reason: str | None = None
    dataset_status: str = "candidate"
    dataset_version: str | None = None
    dataset_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reachable": self.reachable,
            "distance_km": round(self.distance_km, 3),
            "estimated_hours": round(self.estimated_hours, 3),
            "duration_seconds": round(self.estimated_hours * 3600, 1),
            "polyline": self.polyline,
            "edges": self.edges,
            "segments": self.segments,
            "requirements": self.requirements,
            "unreachable_reason": self.unreachable_reason,
            "dataset_status": self.dataset_status,
            "dataset_version": self.dataset_version,
            "dataset_id": self.dataset_id,
            "movement_type": self.movement_type,
        }


class RoutePlanner:
    """按移动类型在世界格网上规划一条可通行路线，不做直线穿越。"""

    def plan(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        origin: tuple[float, float],
        destination: tuple[float, float],
        movement_type: str,
        vehicle_metadata: dict[str, Any] | None = None,
    ) -> RoutePlan:
        dataset = self._approved_dataset(connection, world_id)
        if dataset is None:
            return RoutePlan(
                reachable=False,
                origin=origin,
                destination=destination,
                movement_type=movement_type,
                unreachable_reason="当前世界没有审核通过的导航数据集",
            )
        asset_root = self._resolve_asset_root(dataset["asset_root"])
        terrain = TerrainService(asset_root)
        return self.plan_on(
            terrain,
            origin=origin,
            destination=destination,
            movement_type=movement_type,
            vehicle_metadata=vehicle_metadata,
            dataset_status=dataset["review_status"],
            dataset_version=dataset.get("version"),
            dataset_id=dataset["id"],
        )

    def plan_on(
        self,
        terrain: TerrainService,
        *,
        origin: tuple[float, float],
        destination: tuple[float, float],
        movement_type: str,
        vehicle_metadata: dict[str, Any] | None = None,
        dataset_status: str = "candidate",
        dataset_version: str | None = None,
        dataset_id: str | None = None,
    ) -> RoutePlan:
        vehicle_metadata = vehicle_metadata or {}
        base_speed = float(vehicle_metadata.get("speed_kmh", 5.0))
        start_cell = terrain.nearest_cell(origin[0], origin[1])
        goal_cell = terrain.nearest_cell(destination[0], destination[1])

        if start_cell is None:
            return self._unreachable(
                terrain,
                origin,
                destination,
                movement_type,
                "起点没有可用的地形数据",
                dataset_status,
                dataset_version,
                dataset_id,
            )
        if goal_cell is None:
            return self._unreachable(
                terrain,
                origin,
                destination,
                movement_type,
                "目的地没有可用的地形数据",
                dataset_status,
                dataset_version,
                dataset_id,
            )

        start_ok, start_reason = self._passable(
            terrain, start_cell, movement_type, vehicle_metadata
        )
        if not start_ok:
            return self._unreachable(
                terrain,
                origin,
                destination,
                movement_type,
                start_reason,
                dataset_status,
                dataset_version,
                dataset_id,
            )
        goal_ok, goal_reason = self._passable(terrain, goal_cell, movement_type, vehicle_metadata)
        if not goal_ok:
            return self._unreachable(
                terrain,
                origin,
                destination,
                movement_type,
                goal_reason,
                dataset_status,
                dataset_version,
                dataset_id,
            )

        path = self._astar(
            terrain, start_cell, goal_cell, movement_type, vehicle_metadata, base_speed
        )
        if path is None:
            return self._unreachable(
                terrain,
                origin,
                destination,
                movement_type,
                self._unreachable_reason(movement_type),
                dataset_status,
                dataset_version,
                dataset_id,
            )

        return self._build_plan(
            terrain,
            path,
            origin,
            destination,
            movement_type,
            vehicle_metadata,
            base_speed,
            dataset_status,
            dataset_version,
            dataset_id,
        )

    def describe(
        self,
        terrain: TerrainService,
        longitude: float,
        latitude: float,
        movement_type: str = "land",
        vehicle_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """对任意经纬度做只读地形/通行描述，供地形图悬停与通行图使用。"""
        vehicle_metadata = vehicle_metadata or {}
        base_speed = float(vehicle_metadata.get("speed_kmh", 5.0))
        cell = terrain.nearest_cell(longitude, latitude)
        if cell is None:
            return {"passable": False, "reason": "当前位置没有地形数据"}
        cell_id = int(cell["cell_id"])
        ok, reason = self._passable(terrain, cell, movement_type, vehicle_metadata)
        speed = terrain.cell_speed_kmh(cell, base_speed, movement_type) if ok else 0.0
        surface_multiplier = float(
            terrain.rules.get("surface_speed_multipliers", {}).get(cell.get("surface_type"), 1.0)
        )
        payload = terrain._terrain_payload(cell)
        return {
            "cell_id": cell_id,
            "longitude": round(cell.get("longitude", longitude), 5),
            "latitude": round(cell.get("latitude", latitude), 5),
            "surface_type": cell.get("surface_type"),
            "elevation_code": cell.get("elevation_code"),
            "elevation_m": cell.get("elevation_m"),
            "slope_degrees": cell.get("slope_degrees"),
            "water_kind": cell.get("water_kind"),
            "state_id": cell.get("state_id"),
            "province_id": cell.get("province_id"),
            "road_ids": payload["road_ids"],
            "road_type": payload["road_type"],
            "river_ids": payload["river_ids"],
            "crossing_type": terrain.crossing_type_at(cell_id),
            "crossing_name": payload["crossing_name"],
            "passable": ok,
            "reason": reason,
            "speed_kmh": round(speed, 2),
            "surface_multiplier": round(surface_multiplier, 3),
        }

    def _approved_dataset(
        self, connection: sqlite3.Connection, world_id: str
    ) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT id, name, asset_root, source_sha256, review_status, bounds_json
            FROM navigation_datasets
            WHERE world_id = ? AND review_status = 'approved'
            ORDER BY created_at DESC LIMIT 1
            """,
            (world_id,),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["version"] = None
        try:
            metadata = json.loads(
                (self._resolve_asset_root(item["asset_root"]) / "metadata.json").read_text(
                    encoding="utf-8"
                )
            )
            item["version"] = metadata.get("azgaar_version")
        except (OSError, json.JSONDecodeError, TypeError):
            item["version"] = None
        return item

    def _resolve_asset_root(self, asset_root: str) -> Path:
        return self._projects_root() / asset_root

    @staticmethod
    def _projects_root() -> Path:
        return PROJECT_ROOT

    def _passable(
        self,
        terrain: TerrainService,
        cell: dict[str, Any],
        movement_type: str,
        vehicle_metadata: dict[str, Any],
    ) -> tuple[bool, str | None]:
        water_kind = cell.get("water_kind")
        crossing_type = terrain.crossing_type_at(int(cell["cell_id"]))
        slope = float(cell.get("slope_degrees", 0.0))

        if movement_type == "flight":
            return True, None

        if movement_type == "ship":
            if water_kind in ("ocean", "lake", "river"):
                return True, None
            if crossing_type == "port":
                return True, None
            return False, "船只只能在可航行水体和港口停靠，不能在陆地发起或抵达移动"

        if movement_type == "water":
            if water_kind in ("lake", "river"):
                return True, None
            if water_kind == "ocean" and vehicle_metadata.get("ocean_capable"):
                return True, None
            if crossing_type in ("bridge", "ford"):
                return True, None
            return False, "小艇不能进入外海或陆地，需要更大的船只"

        if movement_type == "underground":
            if crossing_type == "tunnel":
                return True, None
            return False, "没有已登记的矿道/隧道边，不能任意直线钻地"

        # land
        if water_kind in ("ocean", "lake"):
            return False, "徒步无法越过外海或湖面，需要船只或飞行载具"
        if water_kind == "river" and crossing_type not in LAND_CROSSINGS:
            return False, "徒步无法直接跨河，需要桥梁、渡口或浅滩"
        if water_kind == "river" and crossing_type in LAND_CROSSINGS:
            return True, None
        if slope > SLOPE_BLOCK_DEGREES and crossing_type != "mountain_pass":
            return False, "坡度超过35°，必须寻找山口或绕行"
        return True, None

    @staticmethod
    def _fastest_cell_speed(
        terrain: TerrainService, base_speed: float, movement_type: str
    ) -> float:
        """启发式用的可达最快格速，确保启发式是下界（可采纳）。"""
        if movement_type not in ("land", "underground"):
            return max(0.1, base_speed)
        surface_values = [
            float(value)
            for value in terrain.rules.get("surface_speed_multipliers", {}).values()
            if value > 0
        ]
        road_values = [
            float(value)
            for value in terrain.rules.get("road_speed_multipliers", {}).values()
            if value > 0
        ]
        max_surface = max(surface_values, default=1.0)
        max_road = max(road_values, default=1.0)
        return max(0.1, base_speed * max_surface * max_road)

    def _astar(
        self,
        terrain: TerrainService,
        start_cell: dict[str, Any],
        goal_cell: dict[str, Any],
        movement_type: str,
        vehicle_metadata: dict[str, Any],
        base_speed: float,
    ) -> list[int] | None:
        start_id = int(start_cell["cell_id"])
        goal_id = int(goal_cell["cell_id"])
        if start_id == goal_id:
            return [start_id]

        cells = terrain.all_cells()
        # 启发式必须可采纳(下界)：除以"可达最快格速"而非载具基础速度。
        # 陆地上道路/地表倍率可让格速 > 基础速度，若用较慢速度除会高估代价，破坏最优性。
        heuristic_speed = self._fastest_cell_speed(terrain, base_speed, movement_type)
        goal_lonlat = (goal_cell["longitude"], goal_cell["latitude"])

        open_heap: list[tuple[float, int, int]] = []  # f, g, cell_id
        g_score: dict[int, float] = {start_id: 0.0}
        came_from: dict[int, int] = {}
        closed: set[int] = set()

        def heuristic(cell_id: int) -> float:
            coord = cells[cell_id]
            return (
                great_circle_distance_km(
                    coord["longitude"], coord["latitude"], goal_lonlat[0], goal_lonlat[1]
                )
                / heuristic_speed
            )

        heapq.heappush(open_heap, (heuristic(start_id), 0.0, start_id))
        while open_heap:
            f, current_g, current = heapq.heappop(open_heap)
            if current in closed:
                # 以更低 g 再次扩张。
                if current_g > g_score.get(current, float("inf")):
                    continue
            closed.add(current)
            if current == goal_id:
                return self._reconstruct(came_from, current)
            for neighbor in terrain.neighbors(current):
                neighbor_cell = cells.get(int(neighbor))
                if neighbor_cell is None:
                    continue
                ok, _reason = self._passable(
                    terrain, neighbor_cell, movement_type, vehicle_metadata
                )
                if not ok:
                    continue
                distance = great_circle_distance_km(
                    cells[current]["longitude"],
                    cells[current]["latitude"],
                    neighbor_cell["longitude"],
                    neighbor_cell["latitude"],
                )
                if distance <= 0.001:
                    continue
                speed = terrain.cell_speed_kmh(neighbor_cell, base_speed, movement_type)
                step_cost = distance / max(0.1, speed)
                tentative_g = g_score.get(current, float("inf")) + step_cost
                if tentative_g < g_score.get(int(neighbor), float("inf")):
                    g_score[int(neighbor)] = tentative_g
                    came_from[int(neighbor)] = current
                    heapq.heappush(
                        open_heap,
                        (tentative_g + heuristic(int(neighbor)), tentative_g, int(neighbor)),
                    )
        return None

    @staticmethod
    def _reconstruct(came_from: dict[int, int], current: int) -> list[int]:
        path = [current]
        while current in came_from:
            current = came_from[current]
            path.append(current)
        path.reverse()
        return path

    def _build_plan(
        self,
        terrain: TerrainService,
        path: list[int],
        origin: tuple[float, float],
        destination: tuple[float, float],
        movement_type: str,
        vehicle_metadata: dict[str, Any],
        base_speed: float,
        dataset_status: str,
        dataset_version: str | None,
        dataset_id: str | None,
    ) -> RoutePlan:
        cells = terrain.all_cells()
        # 折线 = 真实起点 + 途经格中心 + 真实终点，边与折线严格对齐，供逐段推进。
        points: list[list[float]] = [[origin[0], origin[1]]]
        cell_ids: list[int] = []
        for cell_id in path:
            cell = cells[cell_id]
            points.append([cell["longitude"], cell["latitude"]])
            cell_ids.append(cell_id)
        points.append([destination[0], destination[1]])

        distance = 0.0
        hours = 0.0
        edges: list[dict[str, Any]] = []
        segments_by_surface: dict[tuple[str, float, str | None], dict[str, Any]] = {}
        requirements: list[str] = []
        seen_requirements: set[str] = set()

        for index in range(1, len(points)):
            # 每段的速度取该段终点所在的途经格；末段并入最后一个途经格。
            cell_index = min(index - 1, len(path) - 1)
            cell = cells[path[cell_index]]
            speed = terrain.cell_speed_kmh(cell, base_speed, movement_type)
            step_distance = great_circle_distance_km(
                points[index - 1][0], points[index - 1][1], points[index][0], points[index][1]
            )
            surface = cell.get("surface_type")
            crossing_type = terrain.crossing_type_at(path[cell_index])
            distance += step_distance
            hours += step_distance / max(0.1, speed)
            edges.append(
                {
                    "length_km": round(step_distance, 4),
                    "speed_kmh": round(speed, 2),
                    "surface": surface,
                    "crossing_type": crossing_type,
                }
            )
            key = (surface, round(speed, 2), crossing_type)
            bucket = segments_by_surface.setdefault(
                key,
                {
                    "surface": surface,
                    "speed_kmh": round(speed, 2),
                    "crossing_type": crossing_type,
                    "count": 0,
                    "distance_km": 0.0,
                },
            )
            bucket["count"] += 1
            bucket["distance_km"] += step_distance
            crossing = terrain.crossing_at(path[cell_index])
            if crossing:
                crossing_name = (
                    crossing["properties"].get("name") or crossing["properties"]["crossing_type"]
                )
                if crossing_name not in seen_requirements:
                    seen_requirements.add(crossing_name)
                    requirements.append(
                        f"{crossing['properties']['crossing_type']}：{crossing_name}"
                    )

        segments = list(segments_by_surface.values())
        segments.sort(key=lambda item: item["distance_km"], reverse=True)

        return RoutePlan(
            reachable=True,
            origin=origin,
            destination=destination,
            movement_type=movement_type,
            polyline=points,
            cell_ids=cell_ids,
            distance_km=round(distance, 3),
            estimated_hours=round(hours, 3),
            segments=segments,
            edges=edges,
            requirements=requirements,
            unreachable_reason=None,
            dataset_status=dataset_status,
            dataset_version=dataset_version,
            dataset_id=dataset_id,
        )

    @staticmethod
    def _unreachable_reason(movement_type: str) -> str:
        if movement_type == "ship":
            return "没有找到可航行的连通水域到达目的地"
        if movement_type == "water":
            return "没有找到可通行的小艇水道"
        if movement_type == "flight":
            return "飞行路线受限（载具升限、能源或禁飞区）"
        if movement_type == "underground":
            return "没有连通的已登记矿道"
        return "该地形上没有可通行的陆地路线到达目的地，可能需要船只、飞行载具或绕行更远路线"

    def _unreachable(
        self,
        terrain,
        origin,
        destination,
        movement_type,
        reason,
        dataset_status: str = "candidate",
        dataset_version: str | None = None,
        dataset_id: str | None = None,
    ) -> RoutePlan:
        return RoutePlan(
            reachable=False,
            origin=origin,
            destination=destination,
            movement_type=movement_type,
            polyline=[],
            unreachable_reason=reason,
            dataset_status=dataset_status,
            dataset_version=dataset_version,
            dataset_id=dataset_id,
        )
