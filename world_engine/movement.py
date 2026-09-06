from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from uuid import uuid4

from world_engine.contracts import ContractService
from world_engine.domain import MovementState
from world_engine.geo import great_circle_distance_km
from world_engine.repository import from_iso, to_iso, utc_now
from world_engine.routing import RoutePlan, RoutePlanner
from world_engine.spatial import SpatialContextService

ENCOUNTER_RADIUS_KM = 3.0


def _route_or_none(row: sqlite3.Row) -> dict[str, object] | None:
    """route_json 可能是空折线(合法)，此时解析为空列表，应视为无路线。"""
    raw = row["route_json"] if "route_json" in row.keys() else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def interpolate_coordinate(
    origin_longitude: float,
    origin_latitude: float,
    destination_longitude: float,
    destination_latitude: float,
    progress: float,
) -> tuple[float, float]:
    """按最短经度方向插值；当前版本为稳定平面插值，后续可替换为测地线。"""
    ratio = max(0.0, min(1.0, progress))
    longitude_delta = destination_longitude - origin_longitude
    if longitude_delta > 180:
        longitude_delta -= 360
    elif longitude_delta < -180:
        longitude_delta += 360
    longitude = origin_longitude + longitude_delta * ratio
    if longitude > 180:
        longitude -= 360
    elif longitude < -180:
        longitude += 360
    latitude = origin_latitude + (destination_latitude - origin_latitude) * ratio
    return longitude, latitude


class MovementService:
    """创建、推进和取消持续移动；模型与前端都不能直接瞬移人物。"""

    def __init__(self) -> None:
        self.spatial = SpatialContextService()
        self.planner = RoutePlanner()

    def start(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        character_id: str,
        destination_longitude: float,
        destination_latitude: float,
        vehicle_id: str | None = None,
        world_time: datetime,
        created_at: datetime | None = None,
        record_log: bool = True,
    ) -> MovementState:
        now = created_at or utc_now()
        actor = connection.execute(
            "SELECT * FROM characters WHERE id = ? AND world_id = ?",
            (character_id, world_id),
        ).fetchone()
        if actor is None:
            raise LookupError(character_id)
        active = connection.execute(
            """
            SELECT id FROM character_movements
            WHERE character_id = ? AND status = 'moving'
            """,
            (character_id,),
        ).fetchone()
        if active is not None:
            raise ValueError("人物已经在移动中，请先取消当前行程")

        longitude = float(destination_longitude)
        latitude = float(destination_latitude)
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise ValueError("目标经纬度超出世界范围")
        if (
            great_circle_distance_km(
                float(actor["longitude"]), float(actor["latitude"]), longitude, latitude
            )
            < 0.05
        ):
            raise ValueError("目标位置与当前位置过近")

        selected_vehicle_id = vehicle_id or actor["active_vehicle_id"]
        movement_type = actor["movement_type"]
        speed_kmh = float(actor["movement_speed_kmh"])
        vehicle_name = "徒步"
        if selected_vehicle_id:
            vehicle = ContractService.vehicle_for(connection, world_id, character_id, selected_vehicle_id)
            if vehicle is None:
                raise ValueError("所选交通工具不可用或不属于当前人物")
            movement_type = vehicle["movement_type"]
            speed_kmh = float(vehicle["speed_kmh"])
            vehicle_name = vehicle["name"]

        route = self._plan_route(
            connection,
            world_id=world_id,
            actor=actor,
            destination_longitude=longitude,
            destination_latitude=latitude,
            movement_type=movement_type,
            speed_kmh=speed_kmh,
            selected_vehicle_id=selected_vehicle_id,
        )
        route_json = "[]"
        route_distance_km = 0.0
        navigation_dataset_id = None
        replan_reason = None
        if route is not None:
            route_json = json.dumps(route.as_dict(), ensure_ascii=False)
            total_distance = route.distance_km
            estimated_arrival = world_time + timedelta(hours=route.estimated_hours)
            route_distance_km = route.distance_km
            navigation_dataset_id = route.dataset_id
        else:
            total_distance = great_circle_distance_km(
                actor["longitude"], actor["latitude"], longitude, latitude
            )
            if total_distance < 0.05:
                raise ValueError("目标位置与当前位置过近")
            estimated_arrival = world_time + timedelta(hours=total_distance / speed_kmh)

        destination_location = self._nearest_location(connection, world_id, longitude, latitude)
        initial_encounters = self._nearby_character_ids(
            connection,
            world_id=world_id,
            character_id=character_id,
            longitude=float(actor["longitude"]),
            latitude=float(actor["latitude"]),
        )
        movement_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO character_movements(
                id, world_id, character_id, status, movement_type, vehicle_id,
                speed_kmh, origin_longitude, origin_latitude,
                destination_longitude, destination_latitude,
                total_distance_km, distance_travelled_km,
                destination_location_id, route_json, route_index, route_distance_km,
                navigation_dataset_id, replan_reason,
                started_at_world, updated_at_world,
                estimated_arrival_world, encountered_character_ids_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'moving', ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?)
            """,
            (
                movement_id,
                world_id,
                character_id,
                movement_type,
                selected_vehicle_id,
                speed_kmh,
                actor["longitude"],
                actor["latitude"],
                longitude,
                latitude,
                total_distance,
                destination_location["id"] if destination_location else None,
                route_json,
                route_distance_km,
                navigation_dataset_id,
                replan_reason,
                to_iso(world_time),
                to_iso(world_time),
                to_iso(estimated_arrival),
                json.dumps(initial_encounters, ensure_ascii=False),
                to_iso(now),
                to_iso(now),
            ),
        )
        if record_log:
            route_distance = route.distance_km if route is not None else total_distance
            route_hours = route.estimated_hours if route is not None else total_distance / speed_kmh
            self._record_log(
                connection,
                world_id=world_id,
                world_time=world_time,
                event_type="action.route_planned",
                actor_id=character_id,
                location_id=actor["current_location_id"] or actor["location_id"],
                summary=(
                    f"已为{actor['name']}规划{'地形' if route is not None else '直线'}路径："
                    f"{route_distance:.1f}公里，预计{route_hours:.1f}小时。"
                ),
                payload={
                    "movement_id": movement_id,
                    "navigation_dataset_id": route.dataset_id if route is not None else None,
                    "distance_km": route_distance,
                    "estimated_hours": route_hours,
                    "requirements": route.requirements if route is not None else [],
                    "route_available": route is not None,
                },
            )
            self._record_log(
                connection,
                world_id=world_id,
                world_time=world_time,
                event_type="action.movement_started",
                actor_id=character_id,
                location_id=actor["current_location_id"] or actor["location_id"],
                summary=(
                    f"{actor['name']}以{vehicle_name}开始前往"
                    f"{longitude:.3f}°, {latitude:.3f}°，"
                    f"预计行程{total_distance:.1f}公里。"
                ),
                payload={
                    "movement_id": movement_id,
                    "movement_type": movement_type,
                    "vehicle_id": selected_vehicle_id,
                    "speed_kmh": speed_kmh,
                    "destination_longitude": longitude,
                    "destination_latitude": latitude,
                },
            )
        return self._movement_from_row(
            connection.execute(
                "SELECT * FROM character_movements WHERE id = ?", (movement_id,)
            ).fetchone()
        )

    def _plan_route(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        actor: sqlite3.Row,
        destination_longitude: float,
        destination_latitude: float,
        movement_type: str,
        speed_kmh: float,
        selected_vehicle_id: str | None,
    ) -> RoutePlan | None:
        """有审核通过数据集则规划可通行路线；否则返回 None 退回直线移动。"""
        has_dataset = connection.execute(
            """
            SELECT 1 FROM navigation_datasets
            WHERE world_id = ? AND review_status = 'approved' LIMIT 1
            """,
            (world_id,),
        ).fetchone()
        if not has_dataset:
            return None
        vehicle_metadata: dict[str, object] = {"speed_kmh": speed_kmh}
        if selected_vehicle_id:
            vehicle = connection.execute(
                "SELECT * FROM vehicles WHERE id = ?", (selected_vehicle_id,)
            ).fetchone()
            if vehicle is not None and vehicle["metadata_json"]:
                try:
                    vehicle_metadata.update(json.loads(vehicle["metadata_json"]))
                except (TypeError, json.JSONDecodeError):
                    pass
        plan = self.planner.plan(
            connection,
            world_id=world_id,
            origin=(float(actor["longitude"]), float(actor["latitude"])),
            destination=(destination_longitude, destination_latitude),
            movement_type=movement_type,
            vehicle_metadata=vehicle_metadata,
        )
        if not plan.reachable:
            raise ValueError(plan.unreachable_reason or "该移动无法规划出可通行路线")
        return plan

    def cancel(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        character_id: str,
        world_time: datetime,
        created_at: datetime | None = None,
    ) -> MovementState:
        now = created_at or utc_now()
        row = connection.execute(
            """
            SELECT m.*, c.name, c.location_id, c.current_location_id
            FROM character_movements m
            JOIN characters c ON c.id = m.character_id
            WHERE m.world_id = ? AND m.character_id = ? AND m.status = 'moving'
            """,
            (world_id, character_id),
        ).fetchone()
        if row is None:
            raise ValueError("人物当前没有进行中的移动")
        connection.execute(
            """
            UPDATE character_movements
            SET status = 'cancelled', updated_at_world = ?, updated_at = ?
            WHERE id = ?
            """,
            (to_iso(world_time), to_iso(now), row["id"]),
        )
        self._record_log(
            connection,
            world_id=world_id,
            world_time=world_time,
            event_type="action.movement_cancelled",
            actor_id=character_id,
            location_id=row["current_location_id"] or row["location_id"],
            summary=f"{row['name']}取消了当前移动，并停留在途中。",
            payload={"movement_id": row["id"]},
        )
        return self._movement_from_row(
            connection.execute(
                "SELECT * FROM character_movements WHERE id = ?", (row["id"],)
            ).fetchone()
        )

    def select_transport(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        character_id: str,
        vehicle_id: str | None,
    ) -> None:
        moving = connection.execute(
            """
            SELECT 1 FROM character_movements
            WHERE character_id = ? AND status = 'moving'
            """,
            (character_id,),
        ).fetchone()
        if moving is not None:
            raise ValueError("移动过程中不能更换交通工具")
        if vehicle_id is None:
            connection.execute(
                """
                UPDATE characters
                SET active_vehicle_id = NULL, movement_type = 'land',
                    movement_speed_kmh = 5, updated_at = ?
                WHERE id = ? AND world_id = ?
                """,
                (to_iso(utc_now()), character_id, world_id),
            )
            return
        vehicle = ContractService.vehicle_for(connection, world_id, character_id, vehicle_id)
        if vehicle is None:
            raise ValueError("交通工具不可用或不属于当前人物")
        connection.execute(
            """
            UPDATE characters
            SET active_vehicle_id = ?, movement_type = ?, movement_speed_kmh = ?,
                updated_at = ?
            WHERE id = ? AND world_id = ?
            """,
            (
                vehicle_id,
                vehicle["movement_type"],
                vehicle["speed_kmh"],
                to_iso(utc_now()),
                character_id,
                world_id,
            ),
        )

    def advance(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        previous_time: datetime,
        current_time: datetime,
        created_at: datetime,
    ) -> int:
        elapsed_hours = max(0.0, (current_time - previous_time).total_seconds() / 3600)
        if elapsed_hours <= 0:
            return 0
        rows = connection.execute(
            """
            SELECT m.*, c.name, c.location_id, c.current_location_id
            FROM character_movements m
            JOIN characters c ON c.id = m.character_id
            WHERE m.world_id = ? AND m.status = 'moving'
            ORDER BY m.started_at_world, m.id
            """,
            (world_id,),
        ).fetchall()
        updated = 0
        for row in rows:
            effective_time = current_time
            if row["vehicle_id"]:
                lease = connection.execute(
                    "SELECT due_world_time FROM contract_fulfillments WHERE asset_id=? AND status='active'",
                    (row["vehicle_id"],),
                ).fetchone()
                if lease:
                    effective_time = min(current_time, from_iso(lease["due_world_time"]))
            allowed_hours = max(0.0, (effective_time - previous_time).total_seconds() / 3600)
            travelled, longitude, latitude, arrived, route_index = self._advance_one(
                row, min(elapsed_hours, allowed_hours)
            )
            # 心跳延迟或浮点累计误差不能让进度已满的行程继续停在 moving。
            if effective_time >= from_iso(row["estimated_arrival_world"]):
                travelled = float(row["total_distance_km"])
                longitude = float(row["destination_longitude"])
                latitude = float(row["destination_latitude"])
                arrived = True
            resolved_location = self.spatial.resolve_location(
                connection,
                world_id=world_id,
                longitude=longitude,
                latitude=latitude,
            )
            current_location_id = resolved_location["id"] if resolved_location is not None else None
            connection.execute(
                """
                UPDATE characters
                SET longitude = ?, latitude = ?,
                    location_id = CASE WHEN ? THEN COALESCE(?, location_id) ELSE location_id END,
                    current_location_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    longitude,
                    latitude,
                    int(arrived),
                    row["destination_location_id"],
                    current_location_id,
                    to_iso(created_at),
                    row["character_id"],
                ),
            )
            encountered = set(json.loads(row["encountered_character_ids_json"]))
            nearby = set(
                self._nearby_character_ids(
                    connection,
                    world_id=world_id,
                    character_id=row["character_id"],
                    longitude=longitude,
                    latitude=latitude,
                )
            )
            new_encounters = sorted(nearby - encountered)
            encountered.update(new_encounters)
            status = "arrived" if arrived else "moving"
            connection.execute(
                """
                UPDATE character_movements
                SET status = ?, distance_travelled_km = ?, route_index = ?,
                    updated_at_world = ?,
                    encountered_character_ids_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    travelled,
                    route_index,
                    to_iso(current_time),
                    json.dumps(sorted(encountered), ensure_ascii=False),
                    to_iso(created_at),
                    row["id"],
                ),
            )
            for encountered_id in new_encounters:
                encountered_character = connection.execute(
                    "SELECT name FROM characters WHERE id = ?", (encountered_id,)
                ).fetchone()
                if encountered_character:
                    self._record_log(
                        connection,
                        world_id=world_id,
                        world_time=current_time,
                        event_type="action.movement_encounter",
                        actor_id=row["character_id"],
                        target_id=encountered_id,
                        location_id=current_location_id,
                        summary=(f"{row['name']}在移动途中遇见了{encountered_character['name']}。"),
                        payload={"movement_id": row["id"]},
                    )
            if arrived:
                self._record_log(
                    connection,
                    world_id=world_id,
                    world_time=current_time,
                    event_type="action.movement_arrived",
                    actor_id=row["character_id"],
                    location_id=current_location_id,
                    summary=f"{row['name']}抵达了本次移动的目标坐标。",
                    payload={
                        "movement_id": row["id"],
                        "destination_longitude": row["destination_longitude"],
                        "destination_latitude": row["destination_latitude"],
                    },
                )
            updated += 1
        return updated

    @staticmethod
    def _nearest_location(
        connection: sqlite3.Connection,
        world_id: str,
        longitude: float,
        latitude: float,
    ) -> sqlite3.Row | None:
        locations = connection.execute(
            "SELECT id, longitude, latitude FROM locations WHERE world_id = ? AND is_active = 1",
            (world_id,),
        ).fetchall()
        if not locations:
            return None
        nearest = min(
            locations,
            key=lambda item: great_circle_distance_km(
                longitude, latitude, item["longitude"], item["latitude"]
            ),
        )
        distance = great_circle_distance_km(
            longitude,
            latitude,
            nearest["longitude"],
            nearest["latitude"],
        )
        return nearest if distance <= 20.0 else None

    def _advance_one(
        self,
        row: sqlite3.Row,
        elapsed_hours: float,
    ) -> tuple[float, float, float, bool, int]:
        """推进单个移动；有路线则沿折线逐段推进，否则退回直线插值。"""
        raw = row["route_json"]
        if not isinstance(raw, str) or not raw:
            return self._advance_legacy(row, elapsed_hours)
        try:
            route = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return self._advance_legacy(row, elapsed_hours)
        if not isinstance(route, dict) or not route.get("polyline") or not route.get("edges"):
            return self._advance_legacy(row, elapsed_hours)
        return self._walk_route(row, route, elapsed_hours)

    def _walk_route(
        self,
        row: sqlite3.Row,
        route: dict[str, object],
        elapsed_hours: float,
    ) -> tuple[float, float, float, bool, int]:
        polyline = route.get("polyline") or []
        edges = route.get("edges") or []
        if len(polyline) < 2 or not edges:
            return self._advance_legacy(row, elapsed_hours)
        points = [(float(p[0]), float(p[1])) for p in polyline]
        edge_lengths = [float(item["length_km"]) for item in edges]
        edge_speeds = [float(item["speed_kmh"]) for item in edges]
        cumulative: list[float] = []
        running = 0.0
        for length in edge_lengths:
            running += length
            cumulative.append(running)
        total = cumulative[-1]
        if total <= 0:
            return self._advance_legacy(row, elapsed_hours)

        current_dist = min(float(row["distance_travelled_km"]), total)
        index = min(int(row["route_index"]), len(edges))
        hours_left = max(0.0, elapsed_hours)
        while hours_left > 1e-9 and index < len(edges):
            seg_end = cumulative[index]
            remaining = seg_end - current_dist
            speed = edge_speeds[index]
            if speed <= 0:
                break
            time_to_finish = remaining / speed
            if hours_left >= time_to_finish:
                hours_left -= time_to_finish
                current_dist = seg_end
                index += 1
            else:
                current_dist += hours_left * speed
                hours_left = 0.0
        travelled = min(current_dist, total)
        longitude, latitude = self._interpolate_along(
            points, cumulative, edge_lengths, travelled, total
        )
        arrived = travelled >= total - 1e-9
        new_index = len(edges) if arrived else index
        return travelled, longitude, latitude, arrived, new_index

    @staticmethod
    def _interpolate_along(
        points: list[tuple[float, float]],
        cumulative: list[float],
        edge_lengths: list[float],
        distance: float,
        total: float,
    ) -> tuple[float, float]:
        if distance <= 0 or not points:
            return points[0] if points else (0.0, 0.0)
        if distance >= total - 1e-9:
            return points[-1]
        target = min(distance, total)
        for index, edge_length in enumerate(edge_lengths):
            seg_start = cumulative[index] - edge_length
            if target <= cumulative[index] + 1e-9:
                ratio = 0.0 if edge_length <= 0 else (target - seg_start) / edge_length
                longitude, latitude = MovementService._interpolate_point(
                    points[index], points[index + 1], ratio
                )
                return longitude, latitude
        return points[-1]

    @staticmethod
    def _interpolate_point(
        first: tuple[float, float], second: tuple[float, float], ratio: float
    ) -> tuple[float, float]:
        """沿两点线性插值，经度跨 ±180 时按最短方向绕行，避免穿越到对侧半球。"""
        longitude_delta = second[0] - first[0]
        if longitude_delta > 180:
            longitude_delta -= 360
        elif longitude_delta < -180:
            longitude_delta += 360
        longitude = first[0] + longitude_delta * ratio
        if longitude > 180:
            longitude -= 360
        elif longitude < -180:
            longitude += 360
        latitude = first[1] + (second[1] - first[1]) * ratio
        return longitude, latitude

    def _advance_legacy(
        self, row: sqlite3.Row, elapsed_hours: float
    ) -> tuple[float, float, float, bool, int]:
        travelled = min(
            float(row["total_distance_km"]),
            float(row["distance_travelled_km"]) + float(row["speed_kmh"]) * elapsed_hours,
        )
        total = float(row["total_distance_km"])
        progress = 1.0 if total <= 0 else travelled / total
        longitude, latitude = interpolate_coordinate(
            row["origin_longitude"],
            row["origin_latitude"],
            row["destination_longitude"],
            row["destination_latitude"],
            progress,
        )
        return travelled, longitude, latitude, progress >= 1.0 - 1e-9, int(row["route_index"])

    @staticmethod
    def _nearby_character_ids(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        character_id: str,
        longitude: float,
        latitude: float,
    ) -> list[str]:
        rows = connection.execute(
            """
            SELECT id, longitude, latitude
            FROM characters
            WHERE world_id = ? AND id <> ? AND health > 0
            """,
            (world_id, character_id),
        ).fetchall()
        return [
            row["id"]
            for row in rows
            if great_circle_distance_km(longitude, latitude, row["longitude"], row["latitude"])
            <= ENCOUNTER_RADIUS_KM
        ]

    @staticmethod
    def _record_log(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        world_time: datetime,
        event_type: str,
        actor_id: str | None,
        location_id: str | None,
        summary: str,
        payload: dict[str, object],
        target_id: str | None = None,
    ) -> str:
        event_id = str(uuid4())
        connection.execute(
            """
            INSERT INTO world_events(
                id, world_id, tick_id, occurred_at, event_type, actor_id,
                target_id, location_id, summary, importance, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'routine', ?, ?)
            """,
            (
                event_id,
                world_id,
                event_id,
                to_iso(world_time),
                event_type,
                actor_id,
                target_id,
                location_id,
                summary,
                json.dumps(payload, ensure_ascii=False),
                to_iso(utc_now()),
            ),
        )
        return event_id

    @staticmethod
    def _movement_from_row(row: sqlite3.Row) -> MovementState:
        return MovementState(
            id=row["id"],
            world_id=row["world_id"],
            character_id=row["character_id"],
            status=row["status"],
            movement_type=row["movement_type"],
            vehicle_id=row["vehicle_id"],
            speed_kmh=row["speed_kmh"],
            origin_longitude=row["origin_longitude"],
            origin_latitude=row["origin_latitude"],
            destination_longitude=row["destination_longitude"],
            destination_latitude=row["destination_latitude"],
            total_distance_km=row["total_distance_km"],
            distance_travelled_km=row["distance_travelled_km"],
            destination_location_id=row["destination_location_id"],
            route=_route_or_none(row),
            route_index=(row["route_index"] if "route_index" in row.keys() else 0),
            route_distance_km=(
                row["route_distance_km"] if "route_distance_km" in row.keys() else 0
            ),
            navigation_dataset_id=(
                row["navigation_dataset_id"] if "navigation_dataset_id" in row.keys() else None
            ),
            replan_reason=(row["replan_reason"] if "replan_reason" in row.keys() else None),
            started_at_world=from_iso(row["started_at_world"]),
            updated_at_world=from_iso(row["updated_at_world"]),
            estimated_arrival_world=from_iso(row["estimated_arrival_world"]),
            encountered_character_ids=json.loads(row["encountered_character_ids_json"]),
        )
