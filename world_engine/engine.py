from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime
from uuid import uuid4

from world_engine.actions import ActionService
from world_engine.activation import NPCActivationService
from world_engine.agent_llm import build_agent_model_backend
from world_engine.clock import WorldClockService
from world_engine.combat import CombatResolver
from world_engine.config import PROJECT_ROOT, Settings
from world_engine.database import Database
from world_engine.decisions import DecisionProvider, RuleDecisionProvider, build_decision_provider
from world_engine.domain import (
    ActionOutcome,
    ActionProposal,
    ActionType,
    AgentRunView,
    CharacterState,
    ClockUpdateResult,
    EventSeed,
    HeartbeatResult,
    MovementState,
    PlayerActionResult,
    TerrainContext,
    TickResult,
    WorldSnapshot,
)
from world_engine.geo import great_circle_distance_km
from world_engine.history import HistoryExportResult, WorldHistoryLogger
from world_engine.knowledge import WorldKnowledgeBase
from world_engine.movement import MovementService
from world_engine.orchestration import (
    AgentContext,
    AgentName,
    AgentProposalRecord,
    AgentRunRecord,
    CombatTacticalAgent,
    MemoryCuratorAgent,
    ProposalCoordinator,
    build_assembler,
)
from world_engine.registration import (
    RegistrarAgent,
    RegistrationIntentDetector,
    WorldElementRegistry,
)
from world_engine.repository import WorldNotFoundError, WorldRepository, from_iso, to_iso, utc_now
from world_engine.spatial import SpatialContextService
from world_engine.time_utils import next_adjudication_boundary


class ConcurrentWorldUpdateError(RuntimeError):
    pass


LOGGER = logging.getLogger("virtual-world.engine")


class WorldEngine:
    """协调状态心跳、人物决策、本地裁判、事件、记忆和历史日志。"""

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
        self.registration_detector = RegistrationIntentDetector()
        self.knowledge_base = self._build_knowledge_base()
        self.agent_model_backend = build_agent_model_backend(settings)
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
    ) -> HeartbeatResult:
        result = self.clock.heartbeat(
            world_id,
            real_now=real_now,
            elapsed_seconds=elapsed_seconds,
        )
        if result.adjudication_due:
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

    def start_player_movement(
        self,
        world_id: str,
        *,
        destination_longitude: float,
        destination_latitude: float,
        vehicle_id: str | None = None,
    ) -> MovementState:
        """按玩家当前能力创建持续移动，不直接改写为目标坐标。"""
        now = utc_now()
        with self.database.write() as connection:
            snapshot = self.repository.get_snapshot(connection, world_id)
            player = next(
                (
                    character
                    for character in snapshot.characters
                    if character.is_player and character.is_pov
                ),
                None,
            )
            if player is None:
                raise ValueError("当前世界还没有玩家角色")
            movement = self.movement.start(
                connection,
                world_id=world_id,
                character_id=player.id,
                destination_longitude=destination_longitude,
                destination_latitude=destination_latitude,
                vehicle_id=vehicle_id,
                world_time=snapshot.world.current_time,
                created_at=now,
            )
            connection.execute(
                """
                UPDATE worlds SET version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (to_iso(now), world_id),
            )
        return movement

    def cancel_player_movement(self, world_id: str) -> MovementState:
        """取消玩家当前行程，保留已经走过的真实坐标。"""
        now = utc_now()
        with self.database.write() as connection:
            snapshot = self.repository.get_snapshot(connection, world_id)
            player = next(
                (
                    character
                    for character in snapshot.characters
                    if character.is_player and character.is_pov
                ),
                None,
            )
            if player is None:
                raise ValueError("当前世界还没有玩家角色")
            movement = self.movement.cancel(
                connection,
                world_id=world_id,
                character_id=player.id,
                world_time=snapshot.world.current_time,
                created_at=now,
            )
            connection.execute(
                """
                UPDATE worlds SET version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (to_iso(now), world_id),
            )
        return movement

    def select_player_transport(
        self, world_id: str, vehicle_id: str | None
    ) -> WorldSnapshot:
        """切换玩家当前载具；移动中禁止切换。"""
        now = utc_now()
        with self.database.write() as connection:
            snapshot = self.repository.get_snapshot(connection, world_id)
            player = next(
                (
                    character
                    for character in snapshot.characters
                    if character.is_player and character.is_pov
                ),
                None,
            )
            if player is None:
                raise ValueError("当前世界还没有玩家角色")
            self.movement.select_transport(
                connection,
                world_id=world_id,
                character_id=player.id,
                vehicle_id=vehicle_id,
            )
            connection.execute(
                """
                UPDATE worlds SET version = version + 1, updated_at = ?
                WHERE id = ?
                """,
                (to_iso(now), world_id),
            )
            return self.repository.get_snapshot(connection, world_id)

    def tick(self, world_id: str) -> TickResult:
        """兼容旧入口：现在只强制裁判，不再推进世界时间。"""

        return self.adjudicate(world_id, trigger="manual")

    def submit_player_intent(self, world_id: str, intent: str) -> PlayerActionResult:
        """把玩家一条自然语言意图结算为一次行动，与心跳/裁判解耦。

        找到唯一的玩家角色(is_player)，用决策器(DeepSeek，失败降级规则)把
        意图转成一次 ActionProposal，执行并写事件；只增加版本号，不推进轮次。
        """
        started_at = utc_now()
        with self.database.read() as connection:
            snapshot = self.repository.get_snapshot(connection, world_id)
        player = next((item for item in snapshot.characters if item.is_player), None)
        if player is None:
            raise ValueError("当前世界还没有玩家角色，无法提交行动")
        hinted_target = next(
            (
                item
                for item in sorted(snapshot.characters, key=lambda value: -len(value.name))
                if not item.is_player
                and item.name in intent
                and great_circle_distance_km(
                    player.longitude,
                    player.latitude,
                    item.longitude,
                    item.latitude,
                )
                <= 5.0
            ),
            None,
        )
        if hinted_target is not None:
            with self.database.write() as connection:
                self.activation.activate_for_interaction(
                    connection,
                    world_id=world_id,
                    character_id=hinted_target.id,
                    world_time=snapshot.world.current_time,
                )
                snapshot = self.repository.get_snapshot(connection, world_id)
                player = next(item for item in snapshot.characters if item.is_player)

        proposal: ActionProposal
        provider_name = self.decision_provider.name
        fallback_used = False
        try:
            proposal = self.decision_provider.plan_player_action(snapshot, player, intent)
        except Exception:
            provider_name = self.fallback_provider.name
            fallback_used = True
            proposal = self.fallback_provider.plan_player_action(snapshot, player, intent)
        # 兜底：玩家发起的社交若缺"自己台词/对方回应"，用规则补齐，保证双方内容都可见。
        if proposal.action is ActionType.SOCIALIZE and proposal.target_id:
            target = next(
                (item for item in snapshot.characters if item.id == proposal.target_id),
                None,
            )
            if target is not None:
                if not proposal.dialogue:
                    proposal.dialogue = self.fallback_provider._socialize_dialogue(
                        snapshot, player, target
                    )
                if not proposal.reply:
                    proposal.reply = self.fallback_provider._socialize_reply(
                        snapshot, target, player
                    )
        proposal.metadata.setdefault("player_intent", intent)

        action_id = str(uuid4())
        completed_at = utc_now()
        new_version = snapshot.world.version + 1
        with self.database.write() as connection:
            current_snapshot = self.repository.get_snapshot(connection, world_id)
            if current_snapshot.world.version != snapshot.world.version:
                raise ConcurrentWorldUpdateError(
                    f"状态修订号已经从{snapshot.world.version}变化为"
                    f"{current_snapshot.world.version}，本轮必须重新决策"
                )
            if proposal.action is ActionType.SOCIALIZE and proposal.target_id:
                self.activation.activate_for_interaction(
                    connection,
                    world_id=world_id,
                    character_id=proposal.target_id,
                    world_time=snapshot.world.current_time,
                )
            outcome = self.actions.execute(
                connection,
                world_id=world_id,
                tick_id=action_id,
                occurred_at=snapshot.world.current_time,
                proposal=proposal,
            )
            # 玩家败北：玩家被打倒则原地苏醒并记败北事件(真实失败但可继续)。
            player_health = connection.execute(
                "SELECT health FROM characters WHERE id = ?", (player.id,)
            ).fetchone()["health"]
            if player_health <= 0:
                connection.execute(
                    "UPDATE characters SET health = 60 WHERE id = ?", (player.id,)
                )
                connection.execute(
                    """
                    INSERT INTO world_events(
                        id, world_id, tick_id, occurred_at, event_type,
                        summary, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, 'world.player_defeat', ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        world_id,
                        action_id,
                        to_iso(snapshot.world.current_time),
                        "你被击倒，昏倒在地，许久后才在原地苏醒。",
                        json.dumps({"intent": intent}, ensure_ascii=False),
                        to_iso(completed_at),
                    ),
                )
            connection.execute(
                """
                UPDATE worlds
                SET version = ?, updated_at = ?
                WHERE id = ?
                """,
                (new_version, to_iso(completed_at), world_id),
            )
        registration_ids: list[str] = []
        if outcome.accepted and outcome.event_id is not None:
            try:
                requests = self.registration_detector.detect(
                    intent=intent,
                    player=player,
                    snapshot=snapshot,
                    source_event_id=outcome.event_id,
                )
                if not requests and self.registrar_agent.should_consult(intent):
                    try:
                        requests = self.registrar_agent.detect(
                            intent=intent,
                            player=player,
                            snapshot=snapshot,
                            source_event_id=outcome.event_id,
                        )
                    except Exception:
                        LOGGER.exception("RegistrarAgent未生成可用候选，保留已结算玩家行动")
                for request in requests:
                    with self.database.write() as connection:
                        registration = self.element_registry.submit(
                            connection,
                            world_id=world_id,
                            request=request,
                            auto_apply=False,
                        )
                    registration_ids.append(registration.id)
                if requests:
                    with self.database.read() as connection:
                        new_version = int(
                            connection.execute(
                                "SELECT version FROM worlds WHERE id = ?", (world_id,)
                            ).fetchone()["version"]
                        )
            except Exception:
                LOGGER.exception("玩家行动已结算，但自动元素注册未完成")
        self._sync_history_safely(world_id)
        return PlayerActionResult(
            world_id=world_id,
            action_id=action_id,
            started_at=started_at,
            completed_at=completed_at,
            previous_version=snapshot.world.version,
            current_version=new_version,
            outcome=outcome,
            provider=provider_name,
            fallback_used=fallback_used,
            registration_ids=registration_ids,
        )

    def adjudicate(
        self,
        world_id: str,
        *,
        trigger: str,
        character_ids: list[str] | None = None,
        window_start: datetime | None = None,
        window_end: datetime | None = None,
    ) -> TickResult:
        started_at = utc_now()
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT \"current_time\" AS current_time FROM worlds WHERE id = ?",
                (world_id,),
            ).fetchone()
            if row is None:
                raise WorldNotFoundError(world_id)
            self.activation.refresh(
                connection,
                world_id=world_id,
                world_time=from_iso(row["current_time"]),
            )
        adjudication_id = str(uuid4())

        # 版本冲突时最多重试一次：重新取快照并重新决策，随后仍冲突则回退规则失败。
        for attempt in range(2):
            with self.database.read() as connection:
                snapshot = self.repository.get_snapshot(connection, world_id)
                recent_events = (
                    self.repository.list_events(connection, world_id, limit=40)
                    if self.settings.world_agent_enabled
                    else []
                )

            active_characters = self._select_active_characters(snapshot, character_ids)

            director_seeds: list[EventSeed] = []
            narrative: str | None = None
            agent_run_records: list[AgentRunRecord] = []
            agent_proposal_records: list[AgentProposalRecord] = []
            if self.settings.world_agent_enabled:
                orchestration = self.coordinator.orchestrate(
                    snapshot=snapshot,
                    trigger=trigger,
                    active_characters=active_characters,
                    recent_events=recent_events,
                    model_backend=self.agent_model_backend,
                    provider=self.decision_provider,
                    terrain=self._build_terrain_context(world_id, snapshot),
                )
                proposal_by_actor = orchestration.proposal_by_actor
                provider_name = orchestration.provider_name
                fallback_used = orchestration.fallback_used
                provider_error = orchestration.provider_error
                director_seeds = orchestration.seeds
                narrative = orchestration.narrative
                agent_run_records = orchestration.run_records
                agent_proposal_records = orchestration.proposal_records
            else:
                proposals, provider_name, fallback_used, provider_error = self._propose(
                    snapshot, active_characters
                )
                proposal_by_actor = self._normalize_proposals(
                    snapshot, active_characters, proposals
                )
            resolved_window_start = window_start or snapshot.world.current_time
            resolved_window_end = window_end or snapshot.world.current_time

            try:
                with self.database.write() as connection:
                    current_snapshot = self.repository.get_snapshot(connection, world_id)
                    if current_snapshot.world.version != snapshot.world.version:
                        if attempt < 1:
                            # 版本变化：重新取快照并重新决策，最多一次。
                            continue
                        raise ConcurrentWorldUpdateError(
                            f"状态修订号已经从{snapshot.world.version}变化为"
                            f"{current_snapshot.world.version}，本轮必须重新决策"
                        )

                    connection.execute(
                        """
                        UPDATE world_runtime
                        SET last_tick_started_at = ?, last_tick_status = 'running'
                        WHERE world_id = ?
                        """,
                        (to_iso(started_at), world_id),
                    )
                    outcomes = []
                    for character in active_characters:
                        proposal = proposal_by_actor[character.id]
                        if (
                            proposal.action is ActionType.ATTACK
                            and self.settings.world_agent_combat_enabled
                        ):
                            outcome = self._resolve_combat_proposal(
                                connection,
                                world_id=world_id,
                                tick_id=adjudication_id,
                                occurred_at=snapshot.world.current_time,
                                proposal=proposal,
                                snapshot=snapshot,
                            )
                            outcomes.append(outcome)
                        else:
                            outcomes.append(
                                self.actions.execute(
                                    connection,
                                    world_id=world_id,
                                    tick_id=adjudication_id,
                                    occurred_at=snapshot.world.current_time,
                                    proposal=proposal,
                                )
                            )
                    if agent_run_records:
                        self._write_agent_audit(
                            connection, world_id, agent_run_records, agent_proposal_records
                        )

                    new_version = snapshot.world.version + 1
                    completed_at = utc_now()
                    connection.execute(
                        """
                        UPDATE worlds
                        SET version = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (new_version, to_iso(completed_at), world_id),
                    )
                    connection.execute(
                        """
                        UPDATE world_runtime
                        SET tick_count = tick_count + 1,
                            last_tick_finished_at = ?,
                            last_tick_status = 'completed'
                        WHERE world_id = ?
                        """,
                        (to_iso(completed_at), world_id),
                    )
                    if trigger == "scheduled_12h":
                        next_boundary = next_adjudication_boundary(
                            resolved_window_end,
                            snapshot.world.adjudication_interval_minutes,
                        )
                        connection.execute(
                            """
                            UPDATE world_clock
                            SET last_adjudication_world_time = ?,
                                next_adjudication_world_time = ?,
                                clock_revision = clock_revision + 1,
                                updated_at = ?
                            WHERE world_id = ?
                            """,
                            (
                                to_iso(resolved_window_end),
                                to_iso(next_boundary),
                                to_iso(completed_at),
                                world_id,
                            ),
                        )
                    elif trigger == "player_intervention":
                        connection.execute(
                            """
                            UPDATE world_clock
                            SET last_player_intervention_world_time = ?, updated_at = ?
                            WHERE world_id = ?
                            """,
                            (
                                to_iso(snapshot.world.current_time),
                                to_iso(completed_at),
                                world_id,
                            ),
                        )

                    adjudication_event_id = str(uuid4())
                    connection.execute(
                        """
                        INSERT INTO world_events(
                            id, world_id, tick_id, occurred_at, event_type,
                            summary, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, 'world.adjudication', ?, ?, ?)
                        """,
                        (
                            adjudication_event_id,
                            world_id,
                            adjudication_id,
                            to_iso(snapshot.world.current_time),
                            "世界在当前时间完成了一次人物与事件裁判。",
                            json.dumps(
                                {
                                    "trigger": trigger,
                                    "provider": provider_name,
                                    "fallback_used": fallback_used,
                                    "provider_error": provider_error,
                                    "active_character_count": len(active_characters),
                                    "accepted_action_count": sum(
                                        1 for item in outcomes if item.accepted
                                    ),
                                    "previous_version": snapshot.world.version,
                                    "current_version": new_version,
                                    "window_start": to_iso(resolved_window_start),
                                    "window_end": to_iso(resolved_window_end),
                                },
                                ensure_ascii=False,
                            ),
                            to_iso(completed_at),
                        ),
                    )
                    proposal_records = [
                        proposal_by_actor[item.id].model_dump(mode="json")
                        for item in active_characters
                    ]
                    rejections = [
                        item.model_dump(mode="json")
                        for item in outcomes
                        if not item.accepted
                    ]
                    final_event_ids = [
                        item.event_id for item in outcomes if item.event_id is not None
                    ] + [adjudication_event_id]
                    connection.execute(
                        """
                        INSERT INTO adjudication_runs(
                            id, world_id, trigger_type, window_start, window_end,
                            provider, selected_character_ids_json, proposals_json,
                            rule_rejections_json, final_event_ids_json,
                            fallback_used, status, started_at, completed_at, error_text
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?, ?)
                        """,
                        (
                            adjudication_id,
                            world_id,
                            trigger,
                            to_iso(resolved_window_start),
                            to_iso(resolved_window_end),
                            provider_name,
                            json.dumps(
                                [item.id for item in active_characters], ensure_ascii=False
                            ),
                            json.dumps(proposal_records, ensure_ascii=False),
                            json.dumps(rejections, ensure_ascii=False),
                            json.dumps(final_event_ids, ensure_ascii=False),
                            int(fallback_used),
                            to_iso(started_at),
                            to_iso(completed_at),
                            provider_error,
                        ),
                    )

                result = TickResult(
                    world_id=world_id,
                    tick_id=adjudication_id,
                    started_at=started_at,
                    completed_at=completed_at,
                    previous_time=snapshot.world.current_time,
                    current_time=snapshot.world.current_time,
                    previous_version=snapshot.world.version,
                    current_version=new_version,
                    outcomes=outcomes,
                    trigger=trigger,
                    provider=provider_name,
                    fallback_used=fallback_used,
                    narrative=narrative,
                    director_seeds=director_seeds,
                    agent_runs=self._run_records_to_views(agent_run_records),
                )
                self._sync_history_safely(world_id)
                if self.settings.world_agent_enabled:
                    self._sync_memory_candidates_safely(world_id)
                return result
            except ConcurrentWorldUpdateError:
                if attempt >= 1:
                    raise
                # 否则进入下一次循环重新取快照。
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc).lower():
                    raise
                self._mark_failed(world_id)
                raise
            except Exception:
                self._mark_failed(world_id)
                raise
        raise ConcurrentWorldUpdateError("状态修订号在重试后仍不一致，已交由规则引擎降级")

    def close(self) -> None:
        close = getattr(self.decision_provider, "close", None)
        if close is not None:
            close()

    def sync_history(self, world_id: str) -> HistoryExportResult:
        if self.history_logger is None:
            raise RuntimeError("世界历史日志未启用")
        return self.history_logger.sync_world(world_id)

    def _select_active_characters(
        self,
        snapshot: WorldSnapshot,
        character_ids: list[str] | None = None,
    ) -> list[CharacterState]:
        moving_ids = {
            movement.character_id
            for movement in snapshot.movements
            if movement.status == "moving"
        }
        if character_ids is not None:
            allowed = set(character_ids)
            selected = [
                item
                for item in snapshot.characters
                if item.id in allowed
                and item.id not in moving_ids
                and not item.is_player
                and item.health > 0
                and item.activation_state == "active"
            ]
            return selected[: self.settings.active_character_limit]

        def priority(character: CharacterState) -> tuple[int, str]:
            urgency = (100 - character.satiety) + (100 - character.energy)
            if character.money < 10:
                urgency += 15
            if character.is_core:
                urgency += 40
            if character.goals:
                urgency += 10  # 有明确目标的人物更主动、更常行动
            return (-urgency, character.name)

        autonomous_characters = [
            item
            for item in snapshot.characters
            if not item.is_player
            and item.id not in moving_ids
            and item.health > 0
            and item.activation_state == "active"
        ]
        return sorted(autonomous_characters, key=priority)[
            : self.settings.active_character_limit
        ]

    def _propose(
        self, snapshot: WorldSnapshot, characters: list[CharacterState]
    ) -> tuple[list[ActionProposal], str, bool, str | None]:
        try:
            return (
                self.decision_provider.propose(snapshot, characters),
                self.decision_provider.name,
                False,
                None,
            )
        except Exception as exc:
            LOGGER.warning(
                "决策器%s调用失败，当前裁判降级为规则引擎：%s",
                self.decision_provider.name,
                exc,
            )
            return (
                self.fallback_provider.propose(snapshot, characters),
                self.fallback_provider.name,
                True,
                str(exc),
            )

    def _normalize_proposals(
        self,
        snapshot: WorldSnapshot,
        characters: list[CharacterState],
        proposals: list[ActionProposal],
    ) -> dict[str, ActionProposal]:
        allowed_ids = {item.id for item in characters}
        normalized: dict[str, ActionProposal] = {}
        for proposal in proposals:
            if proposal.actor_id in allowed_ids and proposal.actor_id not in normalized:
                normalized[proposal.actor_id] = proposal

        missing = [item for item in characters if item.id not in normalized]
        for fallback in self.fallback_provider.propose(snapshot, missing):
            normalized[fallback.actor_id] = fallback
        return normalized

    def _mark_failed(self, world_id: str) -> None:
        try:
            with self.database.write() as connection:
                connection.execute(
                    """
                    UPDATE world_runtime
                    SET last_tick_finished_at = ?, last_tick_status = 'failed'
                    WHERE world_id = ?
                    """,
                    (to_iso(utc_now()), world_id),
                )
        except Exception:
            # 原始异常必须优先返回，状态记录失败不能掩盖真正原因。
            return

    def _sync_history_safely(self, world_id: str) -> None:
        if self.history_logger is None:
            return
        try:
            self.history_logger.sync_world(world_id)
        except Exception:
            # 日志是客观事件表的派生视图，导出失败不能回滚已完成事务。
            LOGGER.exception("世界%s事务已完成，但历史日志同步失败", world_id)

    def _sync_state_logs_safely(self, world_id: str) -> None:
        if self.history_logger is None:
            return
        try:
            self.history_logger.sync_state_logs(world_id)
        except Exception:
            # 状态日志同样可以从SQLite心跳与状态差值表重新生成。
            LOGGER.exception("世界%s心跳已完成，但人物状态日志同步失败", world_id)

    def _write_agent_audit(
        self,
        connection: sqlite3.Connection,
        world_id: str,
        run_records: list[AgentRunRecord],
        proposal_records: list[AgentProposalRecord],
    ) -> None:
        for run in run_records:
            connection.execute(
                """
                INSERT INTO agent_runs(
                    id, world_id, trigger, agent_name, input_snapshot_version, status,
                    model_name, input_tokens, output_tokens, latency_ms, error_text,
                    created_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.id,
                    run.world_id,
                    run.trigger,
                    run.agent_name,
                    run.input_snapshot_version,
                    run.status,
                    run.model_name,
                    run.input_tokens,
                    run.output_tokens,
                    run.latency_ms,
                    run.error_text,
                    to_iso(run.created_at),
                    to_iso(run.completed_at) if run.completed_at else None,
                ),
            )
        for record in proposal_records:
            connection.execute(
                """
                INSERT INTO agent_proposals(
                    id, run_id, world_id, actor_id, proposal_type, payload_json,
                    validation_status, rejection_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.run_id,
                    record.world_id,
                    record.actor_id,
                    record.proposal_type,
                    json.dumps(record.payload, ensure_ascii=False),
                    record.validation_status,
                    record.rejection_reason,
                    to_iso(record.created_at),
                ),
            )

    def _resolve_combat_proposal(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        tick_id: str,
        occurred_at: datetime,
        proposal: ActionProposal,
        snapshot: WorldSnapshot,
    ) -> ActionOutcome:
        encounter = self.combat.ensure_encounter(
            connection,
            world_id=world_id,
            actor_id=proposal.actor_id,
            target_id=proposal.target_id or "",
            occurred_at=occurred_at,
        )
        intents: list[object] = []
        if encounter:
            intents = self._combat_intents(snapshot, encounter)
            self.combat.apply_combat_intent_preference(
                connection,
                world_id=world_id,
                encounter_id=encounter["id"],
                intents=intents,
                occurred_at=occurred_at,
            )
        return self.combat.resolve_proposal(
            connection,
            world_id=world_id,
            tick_id=tick_id,
            occurred_at=occurred_at,
            proposal=proposal,
            intents=intents,
        ).outcome

    def _combat_intents(
        self, snapshot: WorldSnapshot, encounter: dict[str, object]
    ) -> list[object]:
        try:
            participants = [
                snapshot.character_by_id(cid)
                for cid in json.loads(encounter["participants_json"])
            ]
        except (json.JSONDecodeError, TypeError, KeyError):
            return []
        participants = [item for item in participants if item is not None]
        if not participants:
            return []
        location = (
            snapshot.location_by_id(encounter["location_id"])
            if encounter.get("location_id")
            else None
        )
        scene = self.assembler.assemble(snapshot, trigger="combat")
        ctx = AgentContext(
            name=AgentName.COMBAT_TACTICAL,
            scene=scene,
            snapshot=snapshot,
            model_backend=self.agent_model_backend,
        )
        agent = CombatTacticalAgent()
        return agent.run(ctx, participants=participants, encounter_location=location)

    def _build_terrain_context(
        self, world_id: str, snapshot: WorldSnapshot
    ) -> TerrainContext | None:
        if not self.settings.world_agent_enabled:
            return None
        pov = next((item for item in snapshot.characters if item.is_pov), None)
        if pov is None:
            return None
        try:
            with self.database.read() as connection:
                row = connection.execute(
                    """
                    SELECT asset_root FROM navigation_datasets
                    WHERE world_id = ? AND review_status = 'approved'
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (world_id,),
                ).fetchone()
            if row is None:
                return None
            from world_engine.navigation import TerrainService

            service = TerrainService(PROJECT_ROOT / row["asset_root"])
            sample = service.sample(pov.longitude, pov.latitude)
            if sample is None:
                return None
            return TerrainContext(
                dataset_id=row["asset_root"],
                location_id=pov.location_id,
                surface=sample.get("surface_type"),
                speed_multiplier=sample.get("road_speed_multiplier", 1.0),
                terrain_features=[sample.get("surface_type")] if sample.get("surface_type") else [],
                reachable=True,
            )
        except Exception:
            return None

    def _run_records_to_views(self, run_records: list[AgentRunRecord]) -> list[AgentRunView]:
        return [
            AgentRunView(
                id=run.id,
                world_id=run.world_id,
                trigger=run.trigger,
                agent_name=run.agent_name,
                input_snapshot_version=run.input_snapshot_version,
                status=run.status,
                model_name=run.model_name,
                input_tokens=run.input_tokens,
                output_tokens=run.output_tokens,
                latency_ms=run.latency_ms,
                error_text=run.error_text,
                created_at=run.created_at,
                completed_at=run.completed_at,
            )
            for run in run_records
        ]

    def _sync_memory_candidates_safely(self, world_id: str) -> None:
        try:
            with self.database.read() as connection:
                snapshot = self.repository.get_snapshot(connection, world_id)
                events = self.repository.list_events(connection, world_id, limit=30)
            ctx = AgentContext(
                name=AgentName.MEMORY_CURATOR,
                scene=self.assembler.assemble(snapshot, trigger="event_followup"),
                snapshot=snapshot,
                model_backend=self.agent_model_backend,
            )
            curator = MemoryCuratorAgent()
            raw_candidates = curator.run(ctx, events=events)
        except Exception:
            LOGGER.exception("世界%s记忆整理失败，保留原始事件待后续重试", world_id)
            return
        try:
            with self.database.write() as connection:
                for candidate in raw_candidates:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO character_memories(
                            id, world_id, character_id, event_id, memory_type,
                            summary, importance, confidence, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(uuid4()),
                            world_id,
                            candidate.character_id,
                            candidate.event_id,
                            candidate.memory_type,
                            candidate.summary,
                            candidate.importance,
                            candidate.confidence,
                            to_iso(utc_now()),
                        ),
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO memory_jobs(
                            id, world_id, event_id, status, attempt_count,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, 'done', 1, ?, ?)
                        """,
                        (
                            "memjob:" + candidate.event_id,
                            world_id,
                            candidate.event_id,
                            to_iso(utc_now()),
                            to_iso(utc_now()),
                        ),
                    )
        except Exception:
            LOGGER.exception("世界%s记忆候选写入失败", world_id)
