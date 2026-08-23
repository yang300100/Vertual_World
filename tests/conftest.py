from __future__ import annotations

from pathlib import Path

import pytest

from world_engine.config import Settings
from world_engine.database import Database


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "test-world.db",
        minutes_per_tick=60,
        worker_interval_seconds=1,
        active_character_limit=5,
    )


@pytest.fixture
def database(settings: Settings) -> Database:
    database = Database(settings.database_path)
    database.initialize()
    return database

