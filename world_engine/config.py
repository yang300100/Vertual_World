from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

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
    worker_interval_seconds: int = 60
    active_character_limit: int = 10
    decision_provider: str = "rules"
    deepseek_api_key: str | None = field(default=None, repr=False)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash-vision-exp"
    deepseek_timeout_seconds: float = 60.0
    deepseek_max_retries: int = 2
    deepseek_max_output_tokens: int = 2400
    history_logging_enabled: bool = False
    history_directory: Path | None = None
    default_time_scale: float = 1.0
    adjudication_interval_minutes: int = 720
    satiety_loss_per_world_hour: float = 3.0
    energy_loss_per_world_hour: float = 2.0

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
        return cls(
            database_path=_resolve_path(os.getenv("WORLD_DB_PATH", "data/world.db")),
            minutes_per_tick=max(1, int(os.getenv("WORLD_MINUTES_PER_TICK", "60"))),
            worker_interval_seconds=max(
                1, int(os.getenv("WORLD_WORKER_INTERVAL_SECONDS", "60"))
            ),
            active_character_limit=max(
                1, int(os.getenv("WORLD_ACTIVE_CHARACTER_LIMIT", "10"))
            ),
            decision_provider=os.getenv("WORLD_DECISION_PROVIDER", "rules").strip().lower(),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY") or None,
            deepseek_base_url=os.getenv(
                "DEEPSEEK_BASE_URL", "https://api.deepseek.com"
            ).rstrip("/"),
            deepseek_model=os.getenv(
                "DEEPSEEK_MODEL", "deepseek-v4-flash-vision-exp"
            ).strip(),
            deepseek_timeout_seconds=max(
                1.0, float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "60"))
            ),
            deepseek_max_retries=max(0, int(os.getenv("DEEPSEEK_MAX_RETRIES", "2"))),
            deepseek_max_output_tokens=max(
                256, int(os.getenv("DEEPSEEK_MAX_OUTPUT_TOKENS", "2400"))
            ),
            history_logging_enabled=os.getenv(
                "WORLD_HISTORY_LOG_ENABLED", "true"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            history_directory=_resolve_path(
                os.getenv("WORLD_HISTORY_LOG_DIR", "logs/worlds")
            ),
            default_time_scale=max(
                0.0, min(10080.0, float(os.getenv("WORLD_DEFAULT_TIME_SCALE", "1.0")))
            ),
            adjudication_interval_minutes=max(
                1, int(os.getenv("WORLD_ADJUDICATION_INTERVAL_MINUTES", "720"))
            ),
            satiety_loss_per_world_hour=max(
                0.0,
                float(
                    os.getenv(
                        "WORLD_SATIETY_LOSS_PER_WORLD_HOUR",
                        os.getenv("WORLD_HUNGER_PER_WORLD_HOUR", "3.0"),
                    )
                ),
            ),
            energy_loss_per_world_hour=max(
                0.0, float(os.getenv("WORLD_ENERGY_LOSS_PER_WORLD_HOUR", "2.0"))
            ),
        )
