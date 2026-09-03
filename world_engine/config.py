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


def _resolve_paths(raw_paths: str) -> tuple[Path, ...]:
    """使用分号分隔多个路径，避免与Windows盘符中的冒号冲突。"""

    return tuple(
        _resolve_path(item.strip()) for item in raw_paths.split(";") if item.strip()
    )


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
    deepseek_max_output_tokens: int = 8192
    knowledge_enabled: bool = True
    knowledge_paths: tuple[Path, ...] = field(default_factory=tuple)
    knowledge_top_k: int = 6
    knowledge_max_context_chars: int = 8000
    dialogue_context_max_chars: int = 12000
    dialogue_context_max_tokens: int = 3600
    dialogue_memory_top_k: int = 6
    dialogue_knowledge_top_k: int = 4
    dialogue_episode_turn_threshold: int = 6
    dialogue_episode_top_k: int = 4
    history_logging_enabled: bool = False
    history_directory: Path | None = None
    default_time_scale: float = 1.0
    adjudication_interval_minutes: int = 180
    satiety_loss_per_world_hour: float = 3.0
    energy_recovery_per_world_hour: float = 2.0
    world_agent_enabled: bool = False
    world_agent_budget_per_heartbeat: int = 100
    world_agent_max_concurrency: int = 4
    world_agent_timeout_seconds: float = 8.0
    world_agent_active_npc_limit: int = 8
    world_agent_combat_enabled: bool = False
    image_generation_provider: str = "seedream"
    image_api_key: str | None = field(default=None, repr=False)
    image_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    image_model: str = "seedream5.0lite"
    image_timeout_seconds: float = 120.0
    image_size: str = "2048x2048"
    image_response_format: str = "url"
    media_directory: Path | None = None

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
                256, int(os.getenv("DEEPSEEK_MAX_OUTPUT_TOKENS", "8192"))
            ),
            knowledge_enabled=os.getenv("WORLD_KNOWLEDGE_ENABLED", "true").strip().lower()
            in {"1", "true", "yes", "on"},
            knowledge_paths=_resolve_paths(
                os.getenv(
                    "WORLD_KNOWLEDGE_PATHS",
                    "docs/worldbuilding;docs/knowledge",
                )
            ),
            knowledge_top_k=max(1, int(os.getenv("WORLD_KNOWLEDGE_TOP_K", "6"))),
            knowledge_max_context_chars=max(
                1000,
                int(os.getenv("WORLD_KNOWLEDGE_MAX_CONTEXT_CHARS", "8000")),
            ),
            dialogue_context_max_chars=max(
                1000,
                int(os.getenv("WORLD_DIALOGUE_CONTEXT_MAX_CHARS", "12000")),
            ),
            dialogue_context_max_tokens=max(
                800,
                int(os.getenv("WORLD_DIALOGUE_CONTEXT_MAX_TOKENS", "3600")),
            ),
            dialogue_memory_top_k=max(
                1,
                int(os.getenv("WORLD_DIALOGUE_MEMORY_TOP_K", "6")),
            ),
            dialogue_knowledge_top_k=max(
                1,
                int(os.getenv("WORLD_DIALOGUE_KNOWLEDGE_TOP_K", "4")),
            ),
            dialogue_episode_turn_threshold=max(
                4,
                int(os.getenv("WORLD_DIALOGUE_EPISODE_TURN_THRESHOLD", "6")),
            ),
            dialogue_episode_top_k=max(
                1,
                int(os.getenv("WORLD_DIALOGUE_EPISODE_TOP_K", "4")),
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
                1, int(os.getenv("WORLD_ADJUDICATION_INTERVAL_MINUTES", "180"))
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
            energy_recovery_per_world_hour=max(
                0.0,
                float(os.getenv("WORLD_ENERGY_RECOVERY_PER_WORLD_HOUR", "2.0")),
            ),
            world_agent_enabled=os.getenv("WORLD_AGENT_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"},
            world_agent_budget_per_heartbeat=max(
                1, int(os.getenv("WORLD_AGENT_BUDGET_PER_HEARTBEAT", "100"))
            ),
            world_agent_max_concurrency=max(
                1, int(os.getenv("WORLD_AGENT_MAX_CONCURRENCY", "4"))
            ),
            world_agent_timeout_seconds=max(
                1.0, float(os.getenv("WORLD_AGENT_TIMEOUT_SECONDS", "8"))
            ),
            world_agent_active_npc_limit=max(
                1, int(os.getenv("WORLD_AGENT_ACTIVE_NPC_LIMIT", "8"))
            ),
            world_agent_combat_enabled=os.getenv(
                "WORLD_AGENT_COMBAT_ENABLED", "false"
            ).strip().lower()
            in {"1", "true", "yes", "on"},
            image_generation_provider=os.getenv(
                "IMAGE_GENERATION_PROVIDER", "seedream"
            ).strip().lower(),
            image_api_key=os.getenv("IMAGE_API_KEY") or None,
            image_base_url=os.getenv(
                "IMAGE_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
            ).rstrip("/"),
            image_model=os.getenv("IMAGE_MODEL", "seedream5.0lite").strip(),
            image_timeout_seconds=max(
                5.0, float(os.getenv("IMAGE_TIMEOUT_SECONDS", "120"))
            ),
            image_size=os.getenv("IMAGE_SIZE", "2048x2048").strip(),
            image_response_format=os.getenv("IMAGE_RESPONSE_FORMAT", "url").strip(),
            media_directory=_resolve_path(
                os.getenv("WORLD_MEDIA_DIR", "data/world-media")
            ),
        )
