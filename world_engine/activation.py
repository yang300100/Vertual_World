from __future__ import annotations

import hashlib
import math
import sqlite3
from datetime import datetime, timedelta

from world_engine.geo import great_circle_distance_km
from world_engine.repository import from_iso, to_iso, utc_now

RELATIONSHIP_ACTIVATION_SCORE = 100
INTERACTION_ACTIVE_MINUTES = 30
ACTIVATION_BUCKET_MINUTES = 10


class NPCActivationService:
    """维护持久、距离概率和交互临时三类NPC激活状态。"""

    def refresh(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        world_time: datetime,
    ) -> int:
        player = connection.execute(
            """
            SELECT * FROM characters
            WHERE world_id = ? AND is_player = 1 AND is_pov = 1
            LIMIT 1
            """,
            (world_id,),
        ).fetchone()
        if player is None:
            return 0
        relationships = self._relationship_scores(
            connection, world_id=world_id, player_id=player["id"]
        )
        characters = connection.execute(
            """
            SELECT * FROM characters
            WHERE world_id = ? AND is_player = 0 AND health > 0
            """,
            (world_id,),
        ).fetchall()
        changed = 0
        for character in characters:
            state, reason = self._resolve_state(
                character=character,
                player=player,
                relationship_score=relationships.get(character["id"], 0),
                world_time=world_time,
                world_id=world_id,
            )
            if (
                character["activation_state"] != state
                or character["activation_reason"] != reason
            ):
                changed += 1
            connection.execute(
                """
                UPDATE characters
                SET activation_state = ?, activation_reason = ?,
                    last_activation_check_world_time = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    state,
                    reason,
                    to_iso(world_time),
                    to_iso(utc_now()),
                    character["id"],
                ),
            )
        return changed

    def activate_for_interaction(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        character_id: str,
        world_time: datetime,
    ) -> None:
        character = connection.execute(
            """
            SELECT id, is_player, activation_policy FROM characters
            WHERE id = ? AND world_id = ?
            """,
            (character_id, world_id),
        ).fetchone()
        if character is None or character["is_player"]:
            return
        if character["activation_policy"] == "persistent":
            connection.execute(
                """
                UPDATE characters
                SET activation_state = 'active', activation_reason = 'persistent',
                    updated_at = ? WHERE id = ?
                """,
                (to_iso(utc_now()), character_id),
            )
            return
        until = world_time + timedelta(minutes=INTERACTION_ACTIVE_MINUTES)
        connection.execute(
            """
            UPDATE characters
            SET activation_state = 'active', activation_reason = 'interaction',
                activation_until_world_time = ?, updated_at = ?
            WHERE id = ?
            """,
            (to_iso(until), to_iso(utc_now()), character_id),
        )

    @staticmethod
    def _resolve_state(
        *,
        character: sqlite3.Row,
        player: sqlite3.Row,
        relationship_score: int,
        world_time: datetime,
        world_id: str,
    ) -> tuple[str, str]:
        if character["activation_policy"] == "persistent" or character["is_core"]:
            return "active", "persistent"
        if relationship_score >= RELATIONSHIP_ACTIVATION_SCORE:
            return "active", "relationship"
        active_until = character["activation_until_world_time"]
        if active_until and from_iso(active_until) > world_time:
            return "active", "interaction"
        radius = float(character["activation_radius_km"])
        if radius <= 0:
            return "background", "background"
        distance = great_circle_distance_km(
            player["longitude"],
            player["latitude"],
            character["longitude"],
            character["latitude"],
        )
        if distance >= radius:
            return "background", "out_of_range"
        proximity = max(0.0, 1.0 - distance / radius)
        probability = float(character["activation_probability"]) * math.pow(
            proximity, 1.5
        )
        bucket = int(world_time.timestamp() // (ACTIVATION_BUCKET_MINUTES * 60))
        digest = hashlib.sha256(
            f"{world_id}:{character['id']}:{bucket}".encode()
        ).digest()
        roll = int.from_bytes(digest[:8], "big") / (2**64 - 1)
        return (
            ("active", "distance")
            if roll < probability
            else ("background", "distance_roll")
        )

    @staticmethod
    def _relationship_scores(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        player_id: str,
    ) -> dict[str, int]:
        rows = connection.execute(
            """
            SELECT source_character_id, target_character_id, affinity, trust
            FROM relationships
            WHERE world_id = ?
              AND (source_character_id = ? OR target_character_id = ?)
            """,
            (world_id, player_id, player_id),
        ).fetchall()
        scores: dict[str, list[int]] = {}
        for row in rows:
            other = (
                row["target_character_id"]
                if row["source_character_id"] == player_id
                else row["source_character_id"]
            )
            scores.setdefault(other, []).append(int(row["affinity"]) + int(row["trust"]))
        return {
            character_id: round(sum(values) / len(values))
            for character_id, values in scores.items()
        }
