from __future__ import annotations

import sqlite3

from world_engine.geo import great_circle_distance_km
from world_engine.repository import to_iso, utc_now


class SpatialContextService:
    """把精确坐标解析为城镇、遗迹、建筑等分层区域归属。"""

    @staticmethod
    def resolve_location(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        longitude: float,
        latitude: float,
    ) -> sqlite3.Row | None:
        locations = connection.execute(
            """
            SELECT * FROM locations
            WHERE world_id = ? AND is_active = 1 AND area_radius_km > 0
            """,
            (world_id,),
        ).fetchall()
        matches: list[tuple[int, float, float, sqlite3.Row]] = []
        for location in locations:
            distance = great_circle_distance_km(
                longitude,
                latitude,
                location["longitude"],
                location["latitude"],
            )
            radius = float(location["area_radius_km"])
            if distance <= radius:
                matches.append(
                    (-int(location["area_priority"]), radius, distance, location)
                )
        if not matches:
            return None
        matches.sort(key=lambda item: (item[0], item[1], item[2], item[3]["name"]))
        return matches[0][3]

    def update_character_context(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        character_id: str,
        longitude: float,
        latitude: float,
    ) -> str | None:
        location = self.resolve_location(
            connection,
            world_id=world_id,
            longitude=longitude,
            latitude=latitude,
        )
        location_id = location["id"] if location is not None else None
        connection.execute(
            """
            UPDATE characters
            SET current_location_id = ?, updated_at = ?
            WHERE id = ? AND world_id = ?
            """,
            (location_id, to_iso(utc_now()), character_id, world_id),
        )
        return location_id

    def update_world_contexts(
        self, connection: sqlite3.Connection, world_id: str
    ) -> int:
        rows = connection.execute(
            """
            SELECT id, longitude, latitude FROM characters WHERE world_id = ?
            """,
            (world_id,),
        ).fetchall()
        for row in rows:
            self.update_character_context(
                connection,
                world_id=world_id,
                character_id=row["id"],
                longitude=row["longitude"],
                latitude=row["latitude"],
            )
        return len(rows)

    @staticmethod
    def ancestor_ids(
        connection: sqlite3.Connection, location_id: str | None
    ) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        current = location_id
        while current and current not in seen:
            seen.add(current)
            result.append(current)
            row = connection.execute(
                "SELECT parent_location_id FROM locations WHERE id = ?", (current,)
            ).fetchone()
            current = row["parent_location_id"] if row else None
        return result
