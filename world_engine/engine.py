from __future__ import annotations

import logging
from datetime import datetime

from world_engine.actions import ActionService
from world_engine.activation import NPCActivationService
from world_engine.agent_llm import build_agent_model_backend
from world_engine.clock import WorldClockService
from world_engine.combat import CombatResolver
from world_engine.config import PROJECT_ROOT, Settings
from world_engine.conversations import (
    ConversationService,
    DialogueContextAssembler,
    DialogueSpeakerScheduler,
)
from world_engine.database import Database
from world_engine.decisions import (
    DecisionProvider,
    RuleDecisionProvider,
    build_decision_provider,
)
from world_engine.domain import (
    ClockUpdateResult,
    HeartbeatResult,
    TickResult,
)
from world_engine.engine_adjudication import AdjudicationMixin
from world_engine.engine_errors import ConcurrentWorldUpdateError
from world_engine.engine_group_dialogue import GroupDialogueMixin
from world_engine.engine_observability import ObservabilityMixin
from world_engine.engine_player_intent import PlayerIntentMixin
from world_engine.engine_player_movement import PlayerMovementMixin
from world_engine.history import HistoryExportResult, WorldHistoryLogger
from world_engine.intent_effects import IntentEffectService
from world_engine.intent_parser import IntentParserAgent
from world_engine.knowledge import WorldKnowledgeBase
from world_engine.movement import MovementService
from world_engine.orchestration import (
    ProposalCoordinator,
    build_assembler,
)
from world_engine.registration import (
    RegistrarAgent,
    RegistrationIntentDetector,
    WorldElementRegistry,
)
from world_engine.removal import WorldElementRemover
from world_engine.repository import WorldRepository, from_iso
from world_engine.spatial import SpatialContextService

# 该异常原先定义在本模块，拆分后随 Mixin 一起迁到 engine_errors；
# 这里按原路径重导出，`from world_engine.engine import ConcurrentWorldUpdateError` 保持可用。
__all__ = ["ConcurrentWorldUpdateError", "WorldEngine"]


LOGGER = logging.getLogger("virtual-world.engine")


class WorldEngine(
    AdjudicationMixin,
    PlayerIntentMixin,
    GroupDialogueMixin,
    PlayerMovementMixin,
    ObservabilityMixin,
):
    """协调状态心跳、人物决策、本地裁判、事件、记忆和历史日志。

    各职责切片以 Mixin 组合，`self` 语义与事务边界与拆分前完全一致。
    """

    def __init__(
        self,
        database: Database,
        settings: Settings,
        decision_provider: DecisionProvider | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.repository = WorldRepository()
        self.decision_provider = decision_provider or build_decision_provider(settings)
        self.fallback_provider = RuleDecisionProvider()
        self.actions = ActionService()
        self.activation = NPCActivationService()
        self.movement = MovementService()
        self.spatial = SpatialContextService()
        self.clock = WorldClockService(database, settings)
        self.combat = CombatResolver()
        self.element_registry = WorldElementRegistry()
        self.element_remover = WorldElementRemover()
        self.registration_detector = RegistrationIntentDetector()
        self.knowledge_base = self._build_knowledge_base()
        self.conversations = ConversationService(
            settings.dialogue_context_max_chars,
            max_context_tokens=settings.dialogue_context_max_tokens,
            memory_top_k=settings.dialogue_memory_top_k,
            knowledge_top_k=settings.dialogue_knowledge_top_k,
            episode_turn_threshold=settings.dialogue_episode_turn_threshold,
            episode_top_k=settings.dialogue_episode_top_k,
            knowledge_base=self.knowledge_base,
        )
        self.dialogue_context = DialogueContextAssembler(self.conversations)
        self.dialogue_speakers = DialogueSpeakerScheduler(max_speakers=2)
        self.intent_effects = IntentEffectService()
        self.agent_model_backend = build_agent_model_backend(settings)
        self.intent_parser = IntentParserAgent(self.agent_model_backend)
        self.registrar_agent = RegistrarAgent(
            self.agent_model_backend if settings.world_agent_enabled else None
        )
        self.assembler = build_assembler(self.knowledge_base, database=self.database)
        self.coordinator = ProposalCoordinator(
            assembler=self.assembler,
            agent_enabled=settings.world_agent_enabled,
            active_npc_enabled=settings.world_agent_enabled,
            event_director_enabled=settings.world_agent_enabled,
            narrative_enabled=settings.world_agent_enabled,
            active_npc_limit=settings.world_agent_active_npc_limit,
            token_budget=settings.world_agent_budget_per_heartbeat * 40,
            max_concurrency=settings.world_agent_max_concurrency,
            timeout_seconds=settings.world_agent_timeout_seconds,
        )
        self.history_logger = (
            WorldHistoryLogger(database, settings.history_directory)
            if settings.history_logging_enabled and settings.history_directory is not None
            else None
        )

    def _build_knowledge_base(self) -> WorldKnowledgeBase | None:
        if not self.settings.knowledge_enabled:
            return None
        try:
            return WorldKnowledgeBase.from_paths(
                self.settings.knowledge_paths,
                project_root=PROJECT_ROOT,
            )
        except Exception:
            LOGGER.warning("知识库初始化失败，Agent将无RAG上下文", exc_info=True)
            return None

    def reset_offline_baseline(self, real_now: datetime | None = None) -> int:
        count = self.clock.reset_offline_baseline(real_now)
        with self.database.write() as connection:
            worlds = connection.execute(
                "SELECT id, \"current_time\" AS current_time FROM worlds"
            ).fetchall()
            for world in worlds:
                self.spatial.update_world_contexts(connection, world["id"])
                self.activation.refresh(
                    connection,
                    world_id=world["id"],
                    world_time=from_iso(world["current_time"]),
                )
        return count

    def mark_worker_seen(self, real_now: datetime | None = None) -> int:
        return self.clock.mark_worker_seen(real_now)

    def clear_worker_seen(self) -> int:
        return self.clock.clear_worker_seen()

    def heartbeat(
        self,
        world_id: str,
        *,
        real_now: datetime | None = None,
        elapsed_seconds: float | None = None,
        activity_skip: dict[str, object] | None = None,
    ) -> HeartbeatResult:
        result = self.clock.heartbeat(
            world_id,
            real_now=real_now,
            elapsed_seconds=elapsed_seconds,
            activity_skip=activity_skip,
        )
        if result.adjudication_due and not result.time_skip_replayed:
            try:
                with self.database.read() as connection:
                    snapshot = self.repository.get_snapshot(connection, world_id)
                result.adjudication = self.adjudicate(
                    world_id,
                    trigger="scheduled_12h",
                    window_start=snapshot.world.last_adjudication_time,
                    window_end=result.current_time,
                )
            except Exception as exc:
                result.adjudication_error = str(exc)
                LOGGER.exception("世界%s到达裁判点，但自主裁判未完成", world_id)
        self._sync_state_logs_safely(world_id)
        return result

    def set_time_scale(
        self,
        world_id: str,
        new_time_scale: float,
        *,
        operator: str = "main_view",
        real_now: datetime | None = None,
    ) -> ClockUpdateResult:
        result = self.clock.set_time_scale(
            world_id,
            new_time_scale,
            operator=operator,
            real_now=real_now,
        )
        if result.no_op:
            return result
        if result.adjudication_due and not result.no_op:
            try:
                with self.database.read() as connection:
                    snapshot = self.repository.get_snapshot(connection, world_id)
                self.adjudicate(
                    world_id,
                    trigger="scheduled_12h",
                    window_start=snapshot.world.last_adjudication_time,
                    window_end=result.world_time,
                )
                result.adjudication_triggered = True
            except Exception:
                LOGGER.exception("世界%s调速结算跨过裁判点，但自主裁判未完成", world_id)
        self._sync_state_logs_safely(world_id)
        self._sync_history_safely(world_id)
        return result

    def tick(self, world_id: str) -> TickResult:
        """兼容旧入口：现在只强制裁判，不再推进世界时间。"""

        return self.adjudicate(world_id, trigger="manual")

    def close(self) -> None:
        close = getattr(self.decision_provider, "close", None)
        if close is not None:
            close()

    def sync_history(self, world_id: str) -> HistoryExportResult:
        if self.history_logger is None:
            raise RuntimeError("世界历史日志未启用")
        return self.history_logger.sync_world(world_id)

    def process_memory_jobs(self, world_id: str, *, limit: int = 10) -> int:
        from world_engine.memory_queue import process_memory_batch

        return process_memory_batch(self, world_id, limit=limit)
