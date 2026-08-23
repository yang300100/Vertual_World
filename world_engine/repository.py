from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from uuid import uuid4

from world_engine.domain import CharacterState, LocationState, WorldSnapshot, WorldState
from world_engine.time_utils import next_adjudication_boundary


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def from_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class WorldNotFoundError(LookupError):
    pass


class WorldRepository:
    """所有世界事实的持久化入口。"""

    def create_world(
        self,
        connection: sqlite3.Connection,
        *,
        name: str,
        minutes_per_tick: int,
        time_scale: float = 1.0,
        adjudication_interval_minutes: int = 720,
        seed_demo: bool = True,
    ) -> str:
        world_id = str(uuid4())
        now = utc_now()
        world_time = datetime(2040, 4, 1, 8, 0, tzinfo=UTC)
        connection.execute(
            """
            INSERT INTO worlds(
                id, name, current_time, minutes_per_tick, status, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'running', 0, ?, ?)
            """,
            (
                world_id,
                name.strip(),
                to_iso(world_time),
                minutes_per_tick,
                to_iso(now),
                to_iso(now),
            ),
        )
        connection.execute(
            """
            INSERT INTO world_runtime(world_id, tick_count, last_tick_status)
            VALUES (?, 0, 'never')
            """,
            (world_id,),
        )
        connection.execute(
            """
            INSERT INTO world_clock(
                world_id, time_scale, heartbeat_interval_seconds,
                last_heartbeat_real_time, clock_revision, offline_policy,
                last_adjudication_world_time, next_adjudication_world_time,
                adjudication_interval_minutes, updated_at
            ) VALUES (?, ?, 60, NULL, 0, 'pause', ?, ?, ?, ?)
            """,
            (
                world_id,
                time_scale,
                to_iso(world_time),
                to_iso(
                    next_adjudication_boundary(
                        world_time, adjudication_interval_minutes
                    )
                ),
                adjudication_interval_minutes,
                to_iso(now),
            ),
        )
        if seed_demo:
            self._seed_demo(connection, world_id, now)
        connection.execute(
            """
            INSERT OR IGNORE INTO character_state_accumulators(
                character_id, world_id, hunger_residual, energy_residual, updated_at
            )
            SELECT id, world_id, 0, 0, updated_at
            FROM characters WHERE world_id = ?
            """,
            (world_id,),
        )
        return world_id

    def _seed_demo(
        self, connection: sqlite3.Connection, world_id: str, created_at: datetime
    ) -> None:
        square_id = str(uuid4())
        home_id = str(uuid4())
        workshop_id = str(uuid4())
        locations = [
            (square_id, "晨星广场", "public", {"food": 20}),
            (home_id, "河畔住宅", "home", {"food": 8}),
            (workshop_id, "旧钟表工坊", "workplace", {"materials": 15}),
        ]
        connection.executemany(
            """
            INSERT INTO locations(id, world_id, name, kind, resources_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (location_id, world_id, name, kind, json.dumps(resources, ensure_ascii=False))
                for location_id, name, kind, resources in locations
            ],
        )

        characters = [
            (
                str(uuid4()),
                "林澈",
                square_id,
                65,
                78,
                12,
                ["谨慎", "善良"],
                ["照顾家人", "在城镇站稳脚跟"],
                1,
            ),
            (
                str(uuid4()),
                "白露",
                home_id,
                24,
                42,
                18,
                ["敏感", "好奇"],
                ["寻找失踪的旧友"],
                1,
            ),
            (
                str(uuid4()),
                "瑞恩",
                workshop_id,
                76,
                35,
                5,
                ["勤奋", "固执"],
                ["修复工坊的大钟"],
                0,
            ),
        ]
        connection.executemany(
            """
            INSERT INTO characters(
                id, world_id, name, location_id, energy, hunger, money,
                traits_json, goals_json, is_core, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    character_id,
                    world_id,
                    name,
                    location_id,
                    energy,
                    hunger,
                    money,
                    json.dumps(traits, ensure_ascii=False),
                    json.dumps(goals, ensure_ascii=False),
                    is_core,
                    to_iso(created_at),
                    to_iso(created_at),
                )
                for (
                    character_id,
                    name,
                    location_id,
                    energy,
                    hunger,
                    money,
                    traits,
                    goals,
                    is_core,
                ) in characters
            ],
        )

    def list_worlds(self, connection: sqlite3.Connection) -> list[WorldState]:
        rows = connection.execute(
            """
            SELECT w.*, r.tick_count, r.last_worker_seen_at,
                   c.time_scale, c.clock_revision, c.offline_policy,
                   c.last_adjudication_world_time, c.next_adjudication_world_time,
                   c.adjudication_interval_minutes, c.heartbeat_interval_seconds,
                   c.last_heartbeat_real_time
            FROM worlds w
            JOIN world_runtime r ON r.world_id = w.id
            JOIN world_clock c ON c.world_id = w.id
            ORDER BY w.created_at ASC
            """
        ).fetchall()
        return [self._world_from_row(row) for row in rows]

    def get_snapshot(self, connection: sqlite3.Connection, world_id: str) -> WorldSnapshot:
        world_row = connection.execute(
            """
            SELECT w.*, r.tick_count, r.last_worker_seen_at,
                   c.time_scale, c.clock_revision, c.offline_policy,
                   c.last_adjudication_world_time, c.next_adjudication_world_time,
                   c.adjudication_interval_minutes, c.heartbeat_interval_seconds,
                   c.last_heartbeat_real_time
            FROM worlds w
            JOIN world_runtime r ON r.world_id = w.id
            JOIN world_clock c ON c.world_id = w.id
            WHERE w.id = ?
            """,
            (world_id,),
        ).fetchone()
        if world_row is None:
            raise WorldNotFoundError(world_id)

        location_rows = connection.execute(
            "SELECT * FROM locations WHERE world_id = ? ORDER BY name",
            (world_id,),
        ).fetchall()
        character_rows = connection.execute(
            "SELECT * FROM characters WHERE world_id = ? ORDER BY is_core DESC, name",
            (world_id,),
        ).fetchall()
        return WorldSnapshot(
            world=self._world_from_row(world_row),
            locations=[self._location_from_row(row) for row in location_rows],
            characters=[self._character_from_row(row) for row in character_rows],
        )

    def list_events(
        self, connection: sqlite3.Connection, world_id: str, limit: int = 100
    ) -> list[dict[str, object]]:
        self._ensure_world(connection, world_id)
        rows = connection.execute(
            """
            SELECT * FROM world_events
            WHERE world_id = ?
            ORDER BY occurred_at DESC, created_at DESC
            LIMIT ?
            """,
            (world_id, max(1, min(limit, 500))),
        ).fetchall()
        return [self._event_dict(row) for row in rows]

    def list_all_events_ascending(
        self, connection: sqlite3.Connection, world_id: str
    ) -> list[dict[str, object]]:
        """按实际写入顺序读取完整客观历史，用于生成可重复的日志。"""

        self._ensure_world(connection, world_id)
        rows = connection.execute(
            """
            SELECT * FROM world_events
            WHERE world_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (world_id,),
        ).fetchall()
        return [self._event_dict(row) for row in rows]

    def list_heartbeats_ascending(
        self, connection: sqlite3.Connection, world_id: str
    ) -> list[dict[str, object]]:
        self._ensure_world(connection, world_id)
        rows = connection.execute(
            """
            SELECT * FROM world_heartbeats
            WHERE world_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (world_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def list_state_updates_ascending(
        self, connection: sqlite3.Connection, world_id: str
    ) -> list[dict[str, object]]:
        self._ensure_world(connection, world_id)
        rows = connection.execute(
            """
            SELECT * FROM character_state_updates
            WHERE world_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (world_id,),
        ).fetchall()
        items: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            item["changes"] = json.loads(item.pop("changes_json"))
            items.append(item)
        return items

    def list_adjudication_runs(
        self, connection: sqlite3.Connection, world_id: str, limit: int = 100
    ) -> list[dict[str, object]]:
        self._ensure_world(connection, world_id)
        rows = connection.execute(
            """
            SELECT * FROM adjudication_runs
            WHERE world_id = ?
            ORDER BY completed_at DESC, id DESC
            LIMIT ?
            """,
            (world_id, max(1, min(limit, 500))),
        ).fetchall()
        json_fields = {
            "selected_character_ids_json": "selected_character_ids",
            "proposals_json": "proposals",
            "rule_rejections_json": "rule_rejections",
            "final_event_ids_json": "final_event_ids",
        }
        items: list[dict[str, object]] = []
        for row in rows:
            item = dict(row)
            for source, target in json_fields.items():
                item[target] = json.loads(item.pop(source))
            item["fallback_used"] = bool(item["fallback_used"])
            items.append(item)
        return items

    def list_memories(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        character_id: str,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        character = connection.execute(
            "SELECT id FROM characters WHERE id = ? AND world_id = ?",
            (character_id, world_id),
        ).fetchone()
        if character is None:
            raise LookupError(character_id)
        rows = connection.execute(
            """
            SELECT * FROM character_memories
            WHERE world_id = ? AND character_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (world_id, character_id, max(1, min(limit, 500))),
        ).fetchall()
        return [dict(row) for row in rows]

    def _ensure_world(self, connection: sqlite3.Connection, world_id: str) -> None:
        if connection.execute("SELECT 1 FROM worlds WHERE id = ?", (world_id,)).fetchone() is None:
            raise WorldNotFoundError(world_id)

    @staticmethod
    def _world_from_row(row: sqlite3.Row) -> WorldState:
        return WorldState(
            id=row["id"],
            name=row["name"],
            current_time=from_iso(row["current_time"]),
            minutes_per_tick=row["minutes_per_tick"],
            status=row["status"],
            version=row["version"],
            tick_count=row["tick_count"],
            time_scale=row["time_scale"],
            clock_revision=row["clock_revision"],
            offline_policy=row["offline_policy"],
            last_adjudication_time=from_iso(row["last_adjudication_world_time"]),
            next_adjudication_time=from_iso(row["next_adjudication_world_time"]),
            adjudication_interval_minutes=row["adjudication_interval_minutes"],
            heartbeat_interval_seconds=row["heartbeat_interval_seconds"],
            last_heartbeat_real_time=(
                from_iso(row["last_heartbeat_real_time"])
                if row["last_heartbeat_real_time"]
                else None
            ),
            last_worker_seen_at=(
                from_iso(row["last_worker_seen_at"])
                if row["last_worker_seen_at"]
                else None
            ),
        )

    @staticmethod
    def _location_from_row(row: sqlite3.Row) -> LocationState:
        return LocationState(
            id=row["id"],
            world_id=row["world_id"],
            name=row["name"],
            kind=row["kind"],
            resources=json.loads(row["resources_json"]),
        )

    @staticmethod
    def _character_from_row(row: sqlite3.Row) -> CharacterState:
        return CharacterState(
            id=row["id"],
            world_id=row["world_id"],
            name=row["name"],
            location_id=row["location_id"],
            energy=row["energy"],
            hunger=row["hunger"],
            money=row["money"],
            traits=json.loads(row["traits_json"]),
            goals=json.loads(row["goals_json"]),
            is_core=bool(row["is_core"]),
        )

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, object]:
        item = dict(row)
        item["payload"] = json.loads(item.pop("payload_json"))
        return item
