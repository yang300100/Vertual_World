from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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

