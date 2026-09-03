"""为已登记的抵达 NPC 补齐稳定且不同的城镇内坐标。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from world_engine.database import Database
from world_engine.npc_generation import GeneratedNpcProfile, generate_npc_positions
from world_engine.repository import to_iso, utc_now


def main() -> None:
    parser = argparse.ArgumentParser(description="重新分布已登记抵达 NPC 的位置")
    parser.add_argument("--world-id", help="目标世界，默认最近运行中的世界")
    parser.add_argument("--seed", default="arrival-position-v1", help="固定位置种子")
    args = parser.parse_args()
    database = Database(Path("data/world.db"))
    database.initialize()

    with database.write() as connection:
        world = connection.execute(
            """
            SELECT id, current_time FROM worlds
            WHERE id = COALESCE(?, id) AND status = 'running'
            ORDER BY updated_at DESC LIMIT 1
            """,
            (args.world_id,),
        ).fetchone()
        if world is None:
            raise SystemExit("没有可运行的目标世界")
        rows = connection.execute(
            """
            SELECT c.id, c.name, c.identity, c.traits_json, c.goals_json, c.location_id,
                   l.longitude AS location_longitude, l.latitude AS location_latitude
            FROM characters c
            JOIN element_registration_requests r ON r.result_entity_id = c.id
            JOIN locations l ON l.id = c.location_id
            WHERE c.world_id = ? AND r.element_type = 'character_arrival'
              AND r.status = 'applied' AND c.is_player = 0
            ORDER BY c.location_id, c.created_at, c.id
            """,
            (world["id"],),
        ).fetchall()
        groups: dict[str, list[object]] = {}
        for row in rows:
            groups.setdefault(row["location_id"], []).append(row)
        updated: list[dict[str, object]] = []
        now = utc_now()
        for location_id, group in groups.items():
            profiles = [
                GeneratedNpcProfile(
                    name=row["name"],
                    identity=row["identity"] or "居民",
                    traits=tuple(json.loads(row["traits_json"])),
                    goals=tuple(json.loads(row["goals_json"])),
                )
                for row in group
            ]
            positions = generate_npc_positions(
                seed=f"{args.seed}:{location_id}",
                profiles=profiles,
                center_longitude=float(group[0]["location_longitude"]),
                center_latitude=float(group[0]["location_latitude"]),
            )
            for row in group:
                longitude, latitude = positions[row["name"]]
                connection.execute(
                    """
                    UPDATE characters SET longitude = ?, latitude = ?, current_location_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (longitude, latitude, location_id, to_iso(now), row["id"]),
                )
                updated.append({"id": row["id"], "name": row["name"], "longitude": longitude, "latitude": latitude})
        if updated:
            event_id = str(uuid4())
            connection.execute(
                """
                INSERT INTO world_events(
                    id, world_id, tick_id, occurred_at, event_type, summary,
                    importance, payload_json, created_at
                ) VALUES (?, ?, ?, ?, 'world.npc_positions_seeded', ?, 'routine', ?, ?)
                """,
                (
                    event_id, world["id"], event_id, world["current_time"],
                    f"系统为{len(updated)}名抵达 NPC 写入了独立的城镇内坐标。",
                    json.dumps({"seed": args.seed, "character_ids": [item["id"] for item in updated]}, ensure_ascii=False),
                    to_iso(now),
                ),
            )
            connection.execute(
                "UPDATE worlds SET version = version + 1, updated_at = ? WHERE id = ?",
                (to_iso(now), world["id"]),
            )
    print(json.dumps({"world_id": world["id"], "updated": updated}, ensure_ascii=False))


if __name__ == "__main__":
    main()
