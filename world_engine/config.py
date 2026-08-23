from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path.resolve()


@dataclass(frozen=True, slots=True)
class Settings:
    """运行配置；所有路径都显式解析，便于测试隔离和服务器迁移。"""

    database_path: Path
    minutes_per_tick: int = 60
    worker_interval_seconds: int = 300
    active_character_limit: int = 5

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            database_path=_resolve_path(os.getenv("WORLD_DB_PATH", "data/world.db")),
            minutes_per_tick=max(1, int(os.getenv("WORLD_MINUTES_PER_TICK", "60"))),
            worker_interval_seconds=max(
                1, int(os.getenv("WORLD_WORKER_INTERVAL_SECONDS", "300"))
            ),
            active_character_limit=max(
                1, int(os.getenv("WORLD_ACTIVE_CHARACTER_LIMIT", "5"))
            ),
        )

