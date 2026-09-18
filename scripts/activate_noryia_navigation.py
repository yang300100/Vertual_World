"""将审核后的 Noryia 导航资产导入当前运行世界并重规划进行中移动。"""

from __future__ import annotations

import json
from datetime import timedelta

from world_engine.config import PROJECT_ROOT, Settings
from world_engine.database import Database
from world_engine.navigation import NavigationDatasetImporter
from world_engine.repository import from_iso, to_iso
from world_engine.routing import RoutePlanner

ASSET_ROOT = PROJECT_ROOT / "docs/worldbuilding/maps/navigation/noryia"


def activate(database: Database, world_id: str) -> dict[str, object]:
    """导入并批准数据集；返回导入与重规划统计。"""

    metadata = json.loads((ASSET_ROOT / "metadata.json").read_text(encoding="utf-8"))
    source_sha256 = str(metadata["source_sha256"])
    importer = NavigationDatasetImporter(ASSET_ROOT)
    planner = RoutePlanner()
    replanned = 0
    blocked = 0

    with database.write() as connection:
        existing = connection.execute(
            """
            SELECT id FROM navigation_datasets
            WHERE world_id = ? AND source_sha256 = ? AND review_status = 'approved'
            ORDER BY approved_at DESC LIMIT 1
            """,
            (world_id, source_sha256),
        ).fetchone()
        if existing is None:
            dataset_id = importer.import_candidate(
                connection,
                world_id=world_id,
                name="Noryia 审核导航数据集",
                source_sha256=source_sha256,
            )
            importer.mark_approved(connection, dataset_id)
        else:
            dataset_id = str(existing["id"])

        moving = connection.execute(
            """
            SELECT m.*, c.longitude AS current_longitude, c.latitude AS current_latitude,
                   c.active_vehicle_id
            FROM character_movements m
            JOIN characters c ON c.id = m.character_id
            WHERE m.world_id = ? AND m.status = 'moving'
            """,
            (world_id,),
        ).fetchall()
        for movement in moving:
            world_time = from_iso(movement["updated_at_world"])
            vehicle_metadata: dict[str, object] = {"speed_kmh": movement["speed_kmh"]}
            if movement["vehicle_id"]:
                vehicle = connection.execute(
                    "SELECT metadata_json FROM vehicles WHERE id = ?", (movement["vehicle_id"],)
                ).fetchone()
                if vehicle and vehicle["metadata_json"]:
                    vehicle_metadata.update(json.loads(vehicle["metadata_json"]))
            plan = planner.plan(
                connection,
                world_id=world_id,
                origin=(float(movement["current_longitude"]), float(movement["current_latitude"])),
                destination=(
                    float(movement["destination_longitude"]),
                    float(movement["destination_latitude"]),
                ),
                movement_type=str(movement["movement_type"]),
                vehicle_metadata=vehicle_metadata,
            )
            if not plan.reachable:
                connection.execute(
                    "UPDATE character_movements SET status = 'blocked', "
                    "replan_reason = ? WHERE id = ?",
                    (plan.unreachable_reason or "当前载具无法规划可通行路线", movement["id"]),
                )
                blocked += 1
                continue
            connection.execute(
                """
                UPDATE character_movements
                SET origin_longitude = ?, origin_latitude = ?, total_distance_km = ?,
                    distance_travelled_km = 0, route_json = ?, route_index = 0,
                    route_distance_km = ?, navigation_dataset_id = ?, replan_reason = ?,
                    started_at_world = ?, updated_at_world = ?, estimated_arrival_world = ?
                WHERE id = ?
                """,
                (
                    movement["current_longitude"],
                    movement["current_latitude"],
                    plan.distance_km,
                    json.dumps(plan.as_dict(), ensure_ascii=False),
                    plan.distance_km,
                    plan.dataset_id,
                    "导航数据集启用后从当前位置重规划",
                    to_iso(world_time),
                    to_iso(world_time),
                    to_iso(world_time + timedelta(hours=plan.estimated_hours)),
                    movement["id"],
                ),
            )
            replanned += 1

    return {"dataset_id": dataset_id, "replanned": replanned, "blocked": blocked}


if __name__ == "__main__":
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    with database.read() as connection:
        world = connection.execute("SELECT id FROM worlds WHERE status = 'running'").fetchone()
    if world is None:
        raise SystemExit("没有运行中的世界")
    print(json.dumps(activate(database, str(world["id"])), ensure_ascii=False))
