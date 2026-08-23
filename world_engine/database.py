from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from world_engine.time_utils import next_adjudication_boundary

SCHEMA = """
CREATE TABLE IF NOT EXISTS worlds (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    current_time TEXT NOT NULL,
    minutes_per_tick INTEGER NOT NULL CHECK (minutes_per_tick > 0),
    status TEXT NOT NULL DEFAULT 'running',
    version INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_runtime (
    world_id TEXT PRIMARY KEY REFERENCES worlds(id) ON DELETE CASCADE,
    tick_count INTEGER NOT NULL DEFAULT 0,
    last_tick_started_at TEXT,
    last_tick_finished_at TEXT,
    last_tick_status TEXT
);

CREATE TABLE IF NOT EXISTS locations (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    resources_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(world_id, name)
);

CREATE TABLE IF NOT EXISTS characters (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    location_id TEXT NOT NULL REFERENCES locations(id),
    energy INTEGER NOT NULL CHECK (energy BETWEEN 0 AND 100),
    hunger INTEGER NOT NULL CHECK (hunger BETWEEN 0 AND 100),
    money INTEGER NOT NULL CHECK (money >= 0),
    traits_json TEXT NOT NULL DEFAULT '[]',
    goals_json TEXT NOT NULL DEFAULT '[]',
    is_core INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(world_id, name)
);

CREATE TABLE IF NOT EXISTS relationships (
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    source_character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    target_character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    affinity INTEGER NOT NULL DEFAULT 0 CHECK (affinity BETWEEN -100 AND 100),
    trust INTEGER NOT NULL DEFAULT 0 CHECK (trust BETWEEN -100 AND 100),
    updated_at TEXT NOT NULL,
    PRIMARY KEY(world_id, source_character_id, target_character_id),
    CHECK (source_character_id <> target_character_id)
);

CREATE TABLE IF NOT EXISTS world_events (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    tick_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    target_id TEXT REFERENCES characters(id) ON DELETE SET NULL,
    location_id TEXT REFERENCES locations(id) ON DELETE SET NULL,
    summary TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_world_events_world_time
ON world_events(world_id, occurred_at DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS character_memories (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES world_events(id) ON DELETE CASCADE,
    memory_type TEXT NOT NULL,
    summary TEXT NOT NULL,
    importance INTEGER NOT NULL CHECK (importance BETWEEN 1 AND 10),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    created_at TEXT NOT NULL,
    UNIQUE(character_id, event_id, memory_type)
);

CREATE INDEX IF NOT EXISTS idx_character_memories_character_time
ON character_memories(character_id, created_at DESC);

CREATE TABLE IF NOT EXISTS world_clock (
    world_id TEXT PRIMARY KEY REFERENCES worlds(id) ON DELETE CASCADE,
    time_scale REAL NOT NULL DEFAULT 1.0 CHECK (time_scale BETWEEN 0 AND 10080),
    heartbeat_interval_seconds INTEGER NOT NULL DEFAULT 60 CHECK (heartbeat_interval_seconds > 0),
    last_heartbeat_real_time TEXT,
    clock_revision INTEGER NOT NULL DEFAULT 0,
    offline_policy TEXT NOT NULL DEFAULT 'pause' CHECK (offline_policy = 'pause'),
    last_adjudication_world_time TEXT NOT NULL,
    next_adjudication_world_time TEXT NOT NULL,
    adjudication_interval_minutes INTEGER NOT NULL DEFAULT 720,
    last_player_intervention_world_time TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS world_heartbeats (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    real_time TEXT NOT NULL,
    real_elapsed_seconds REAL NOT NULL CHECK (real_elapsed_seconds >= 0),
    time_scale REAL NOT NULL CHECK (time_scale >= 0),
    world_delta_seconds REAL NOT NULL CHECK (world_delta_seconds >= 0),
    world_time_before TEXT NOT NULL,
    world_time_after TEXT NOT NULL,
    characters_updated INTEGER NOT NULL DEFAULT 0,
    adjudication_due INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_world_heartbeats_world_time
ON world_heartbeats(world_id, created_at ASC);

CREATE TABLE IF NOT EXISTS character_state_accumulators (
    character_id TEXT PRIMARY KEY REFERENCES characters(id) ON DELETE CASCADE,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    hunger_residual REAL NOT NULL DEFAULT 0,
    energy_residual REAL NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS character_state_updates (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    heartbeat_id TEXT NOT NULL REFERENCES world_heartbeats(id) ON DELETE CASCADE,
    character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
    world_time_before TEXT NOT NULL,
    world_time_after TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    cause TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_character_state_updates_character_time
ON character_state_updates(character_id, created_at ASC);

CREATE TABLE IF NOT EXISTS adjudication_runs (
    id TEXT PRIMARY KEY,
    world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
    trigger_type TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    provider TEXT NOT NULL,
    selected_character_ids_json TEXT NOT NULL,
    proposals_json TEXT NOT NULL,
    rule_rejections_json TEXT NOT NULL,
    final_event_ids_json TEXT NOT NULL,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    error_text TEXT
);

CREATE INDEX IF NOT EXISTS idx_adjudication_runs_world_time
ON adjudication_runs(world_id, completed_at ASC);
"""


class Database:
    """负责SQLite连接、WAL设置与显式写事务。"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._ensure_clock_and_accumulator_rows(connection)

    @staticmethod
    def _ensure_clock_and_accumulator_rows(connection: sqlite3.Connection) -> None:
        world_rows = connection.execute(
            """
            SELECT worlds.id,
                   worlds."current_time" AS current_time,
                   worlds.updated_at
            FROM worlds
            """
        ).fetchall()
        for row in world_rows:
            current_time = datetime.fromisoformat(row["current_time"])
            next_boundary = next_adjudication_boundary(current_time)
            connection.execute(
                """
                INSERT OR IGNORE INTO world_clock(
                    world_id, time_scale, heartbeat_interval_seconds,
                    last_heartbeat_real_time, clock_revision, offline_policy,
                    last_adjudication_world_time, next_adjudication_world_time,
                    adjudication_interval_minutes, updated_at
                ) VALUES (?, 1.0, 60, NULL, 0, 'pause', ?, ?, 720, ?)
                """,
                (
                    row["id"],
                    row["current_time"],
                    next_boundary.isoformat(),
                    row["updated_at"],
                ),
            )
        connection.execute(
            """
            INSERT OR IGNORE INTO character_state_accumulators(
                character_id, world_id, hunger_residual, energy_residual, updated_at
            )
            SELECT id, world_id, 0, 0, updated_at FROM characters
            """
        )

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
