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
from world_engine.contracts import ContractService
from world_engine.conversations import (
    ConversationService,
    DialogueContextAssembler,
    DialogueSpeakerScheduler,
)
from world_engine.database import Database
from world_engine.decisions import (
    DecisionProvider,
    DecisionProviderError,
    RuleDecisionProvider,
    build_decision_provider,
)
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
from world_engine.history import HistoryExportResult, WorldHistoryLogger
from world_engine.intent_effects import IntentEffectService
from world_engine.intent_parser import IntentParserAgent, IntentPreview
from world_engine.knowledge import WorldKnowledgeBase
from world_engine.movement import MovementService
from world_engine.orchestration import (
    AgentContext,
    AgentName,
    AgentProposalRecord,
    AgentRunRecord,
    CombatTacticalAgent,
    ProposalCoordinator,
    build_assembler,
)
from world_engine.player_inputs import parse_player_input
from world_engine.registration import (
    RegistrarAgent,
    RegistrationIntentDetector,
    WorldElementRegistry,
)
from world_engine.removal import ElementRemovalSubmit, WorldElementRemover
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

    def submit_player_intent(
        self,
        world_id: str,
        intent: str,
        *,
        target_character_id: str | None = None,
        delivery: str = "normal",
    ) -> PlayerActionResult:
        """把玩家一条自然语言意图结算为一次行动，与心跳/裁判解耦。

        找到唯一的玩家角色(is_player)，用决策器(DeepSeek，失败降级规则)把
        意图转成一次 ActionProposal，执行并写事件；只增加版本号，不推进轮次。
        """
        message = parse_player_input(intent)
        from world_engine.sequences import explicit_steps
        if explicit_steps(intent):raise ValueError("这是组合输入，请通过连续行动入口执行")
        if message.kind == "action":
            from world_engine.player_action_flow import execute_explicit_action

            return execute_explicit_action(self, world_id, message.text, target_character_id)
        explicit_speech = message.kind == "speech"
        if explicit_speech and not message.text:
            raise ValueError("请在“说话：”后填写台词")
        started_at = utc_now()
        with self.database.read() as connection:
            snapshot = self.repository.get_snapshot(connection, world_id)
        player = next((item for item in snapshot.characters if item.is_player), None)
        if player is None:
            raise ValueError("当前世界还没有玩家角色，无法提交行动")
        with self.database.read() as connection:
            hinted_target = self.conversations.resolve_target(
                connection,
                snapshot=snapshot,
                player=player,
                intent=intent,
                target_character_id=target_character_id,
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

        conversation = None
        if hinted_target is not None:
            with self.database.read() as connection:
                conversation = self.conversations.load_context(
                    connection,
                    snapshot=snapshot,
                    player_id=player.id,
                    npc=hinted_target,
                )
        if explicit_speech:
            if hinted_target is None:
                raise ValueError("请先选择要说话的 NPC")
            intent = message.text
        planning_intent = conversation.planning_intent(intent) if conversation else intent

        lock_dialogue_target = (
            hinted_target is not None
            and (explicit_speech or not self.conversations.is_non_dialogue_intent(intent))
        )
        proposal: ActionProposal
        provider_name = self.decision_provider.name
        fallback_used = False
        if lock_dialogue_target:
            # 已明确人物且属于对话时，本地规则已经足够确定动作；跳过一次无意义的
            # 玩家规划模型调用，只让目标 NPC 生成一次回应。
            proposal = ActionProposal(
                actor_id=player.id,
                action=ActionType.SOCIALIZE,
                target_id=hinted_target.id,
                reason=f"我继续与{hinted_target.name}交谈。",
                dialogue=intent,
            )
        else:
            try:
                proposal = self.decision_provider.plan_player_action(
                    snapshot, player, planning_intent
                )
            except Exception:
                provider_name = self.fallback_provider.name
                fallback_used = True
                proposal = self.fallback_provider.plan_player_action(
                    snapshot, player, planning_intent
                )
        # 玩家自己的发言来自输入；NPC 可见回应只允许由模型生成，绝不使用规则台词。
        if proposal.action is ActionType.SOCIALIZE and proposal.target_id:
            target = next(
                (item for item in snapshot.characters if item.id == proposal.target_id),
                None,
            )
            if target is not None:
                from world_engine.geo import great_circle_distance_km
                from world_engine.proximity import VOICE_RADIUS_KM, same_room
                if delivery not in VOICE_RADIUS_KM:raise ValueError("说话方式无效")
                if not same_room(player,target) or great_circle_distance_km(player.longitude,player.latitude,target.longitude,target.latitude)>VOICE_RADIUS_KM[delivery]:
                    raise ValueError("对方听不见当前音量的话语，请走近或选择喊话")
                if not proposal.dialogue:
                    proposal.dialogue = intent
                # 目标 NPC 单独决定如何回应；没有模型就拒绝本次对话，不能写入预设回答。
                responder = getattr(self.decision_provider, "respond_to_player", None)
                if not callable(responder):
                    raise DecisionProviderError("当前决策器不支持 NPC 模型对话")
                reply_context: dict[str, object] = {"player_text": intent}
                try:
                    with self.database.read() as connection:
                        reply_context = self.dialogue_context.build(
                            connection,
                            snapshot=snapshot,
                            npc=target,
                            player=player,
                            player_text=intent,
                            conversation=conversation,
                            channel="in_person",
                            interaction="当面交谈；双方必须在100米可见范围内",
                            decision_details={"action": "socialize", "delivery":delivery},
                        )
                    npc_reply = responder(npc=target, player=player, context=reply_context)
                except Exception as exc:
                    LOGGER.info("NPC 独立回应失败，本次对话不写入预设台词", exc_info=True)
                    raise DecisionProviderError("NPC 对话模型暂时不可用，请稍后重试") from exc
                proposal.reply = npc_reply.reply
                proposal.metadata["npc_social_move"] = npc_reply.social_move
        proposal.metadata.setdefault("player_intent", intent)
        proposal.metadata["delivery"]=delivery
        if conversation is not None:
            proposal.metadata["conversation_target_id"] = conversation.npc_id
            proposal.metadata["conversation_turn_count"] = len(conversation.turns)

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
                target = current_snapshot.character_by_id(proposal.target_id)
                if target is not None:
                    self.conversations.get_card(
                        connection,
                        world_id=world_id,
                        npc=target,
                        persist_default=True,
                    )
            outcome = self.actions.execute(
                connection,
                world_id=world_id,
                tick_id=action_id,
                occurred_at=snapshot.world.current_time,
                proposal=proposal,
            )
            if (
                outcome.accepted
                and outcome.event_id is not None
                and proposal.action is ActionType.SOCIALIZE
                and proposal.target_id is not None
                and proposal.reply
            ):
                self.conversations.record_exchange(
                    connection,
                    world_id=world_id,
                    npc_id=proposal.target_id,
                    counterpart_id=player.id,
                    event_id=outcome.event_id,
                    world_time=snapshot.world.current_time,
                    player_text=intent,
                    npc_text=proposal.reply,
                )
            if outcome.accepted and outcome.event_id is not None:
                effects = self.intent_effects.apply(
                    connection,
                    world_id=world_id,
                    source_event_id=outcome.event_id,
                    actor_id=player.id,
                    target_id=proposal.target_id,
                    intent=intent,
                )
                applied_summaries = [effect.summary for effect in effects if effect.applied]
                if applied_summaries:
                    outcome.summary = f"{outcome.summary} {'；'.join(applied_summaries)}。"
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
        if self._tombstone_destroyed_targets(world_id, [(outcome, proposal)]):
            with self.database.read() as connection:
                new_version = int(
                    connection.execute(
                        "SELECT version FROM worlds WHERE id = ?", (world_id,)
                    ).fetchone()["version"]
                )
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

    def submit_group_dialogue(
        self,
        world_id: str,
        intent: str,
        *,
        participant_ids: list[str] | None = None,
        max_speakers: int = 2,
        delivery: str = "normal",
    ) -> dict[str, object]:
        """让本地调度器选中在场发言者，再逐人生成并原子记录多人回应。"""
        with self.database.read() as connection:
            snapshot = self.repository.get_snapshot(connection, world_id)
            player = next((item for item in snapshot.characters if item.is_player), None)
            if player is None:
                raise ValueError("当前世界还没有玩家角色，无法发起多人对话")
            scheduler = DialogueSpeakerScheduler(max_speakers=max_speakers)
            from world_engine.proximity import VOICE_RADIUS_KM
            if delivery not in VOICE_RADIUS_KM:raise ValueError("说话方式无效")
            selected = scheduler.select(
                connection,
                snapshot=snapshot,
                player=player,
                intent=intent,
                participant_ids=participant_ids,
                radius_km=VOICE_RADIUS_KM[delivery],
            )
        if not selected:
            raise ValueError("100米内没有可以参与多人对话的 NPC")
        responder = getattr(self.decision_provider, "respond_to_player", None)
        if not callable(responder):
            raise DecisionProviderError("当前决策器不支持 NPC 模型对话")

        drafted: list[dict[str, object]] = []
        prior_replies: list[dict[str, str]] = []
        participant_view = [
            {
                "id": choice.character.id,
                "name": choice.character.name,
                "score": choice.score,
                "reasons": list(choice.reasons),
            }
            for choice in selected
        ]
        for turn_order, choice in enumerate(selected, start=1):
            npc = choice.character
            try:
                with self.database.read() as connection:
                    conversation = self.conversations.load_context(
                        connection,
                        snapshot=snapshot,
                        player_id=player.id,
                        npc=npc,
                    )
                    context = self.dialogue_context.build(
                        connection,
                        snapshot=snapshot,
                        npc=npc,
                        player=player,
                        player_text=intent,
                        conversation=conversation,
                        channel="group_scene",
                        interaction="在场多人交谈；只回应自己知道和感知到的内容",
                        decision_details={
                            "turn_order": turn_order,
                            "selected_speakers": participant_view,
                            "prior_group_replies": [dict(item) for item in prior_replies],
                        },
                    )
                reply = responder(npc=npc, player=player, context=context)
            except Exception as exc:
                LOGGER.info("多人对话的 NPC 回应生成失败，本轮不写入世界", exc_info=True)
                raise DecisionProviderError("多人对话模型暂时不可用，请稍后重试") from exc
            drafted.append(
                {
                    "choice": choice,
                    "reply": reply,
                    "context_budget": context.get("budget_trace", {}),
                }
            )
            prior_replies.append({"speaker": npc.name, "reply": reply.reply})

        group_dialogue_id = str(uuid4())
        previous_version = snapshot.world.version
        replies: list[dict[str, object]] = []
        with self.database.write() as connection:
            current = self.repository.get_snapshot(connection, world_id)
            if current.world.version != previous_version:
                raise ConcurrentWorldUpdateError("多人对话生成期间世界状态已经变化，请重新提交")
            for turn_order, item in enumerate(drafted, start=1):
                choice = item["choice"]
                reply = item["reply"]
                npc = choice.character
                self.activation.activate_for_interaction(
                    connection,
                    world_id=world_id,
                    character_id=npc.id,
                    world_time=snapshot.world.current_time,
                )
                proposal = ActionProposal(
                    actor_id=player.id,
                    action=ActionType.SOCIALIZE,
                    target_id=npc.id,
                    reason=f"我向在场众人发言，{npc.name}作出回应。",
                    dialogue=intent,
                    reply=reply.reply,
                    metadata={
                        "group_dialogue_id": group_dialogue_id,
                        "delivery": delivery,
                        "group_turn_order": turn_order,
                        "npc_social_move": reply.social_move,
                    },
                )
                outcome = self.actions.execute(
                    connection,
                    world_id=world_id,
                    tick_id=f"group-dialogue:{group_dialogue_id}",
                    occurred_at=snapshot.world.current_time,
                    proposal=proposal,
                )
                if not outcome.accepted or outcome.event_id is None:
                    raise ValueError(outcome.rejection_reason or "多人对话未通过世界规则校验")
                self.conversations.get_card(
                    connection,
                    world_id=world_id,
                    npc=npc,
                    persist_default=True,
                )
                self.conversations.record_exchange(
                    connection,
                    world_id=world_id,
                    npc_id=npc.id,
                    counterpart_id=player.id,
                    event_id=outcome.event_id,
                    world_time=snapshot.world.current_time,
                    player_text=intent,
                    npc_text=reply.reply,
                )
                replies.append(
                    {
                        "character_id": npc.id,
                        "name": npc.name,
                        "reply": reply.reply,
                        "social_move": reply.social_move,
                        "event_id": outcome.event_id,
                        "turn_order": turn_order,
                        "selection_score": choice.score,
                        "selection_reasons": list(choice.reasons),
                        "context_budget": item["context_budget"],
                    }
                )
            now = to_iso(utc_now())
            connection.execute(
                "UPDATE worlds SET version = version + 1, updated_at = ? WHERE id = ?",
                (now, world_id),
            )
        self._sync_history_safely(world_id)
        return {
            "group_dialogue_id": group_dialogue_id,
            "provider": self.decision_provider.name,
            "player_text": intent,
            "previous_version": previous_version,
            "current_version": previous_version + 1,
            "replies": replies,
        }

    def preview_player_intent(
        self,
        world_id: str,
        intent: str,
        *,
        target_character_id: str | None = None,
    ) -> IntentPreview:
        """解析为待确认表单草案；本方法不写入事件、物品或人物状态。"""

        with self.database.read() as connection:
            snapshot = self.repository.get_snapshot(connection, world_id)
            player = next((item for item in snapshot.characters if item.is_player), None)
            if player is None:
                raise ValueError("当前世界还没有玩家角色，无法解析行动")
            from world_engine.sequences import explicit_steps
            steps = explicit_steps(intent)
            if steps:
                return IntentPreview(requires_form=False, operation="none", sequence_steps=steps)
            if parse_player_input(intent).kind in {"action", "speech"}:
                if not parse_player_input(intent).text:
                    raise ValueError("输入前缀后不能为空")
                return IntentPreview(requires_form=False, operation="none")
            hinted_target = self.conversations.resolve_target(
                connection,
                snapshot=snapshot,
                player=player,
                intent=intent,
                target_character_id=target_character_id,
            )
        return self.intent_parser.preview(
            intent=intent,
            player=player,
            snapshot=snapshot,
            preferred_target_id=hinted_target.id if hinted_target else None,
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
                # 待办是 NPC 自己的长期计划属性；仅作为只读决策上下文注入，
                # Agent 仍只能提出动作，不能直接改写待办状态或世界事实。
                open_todos = connection.execute(
                    """
                    SELECT character_id, title, details FROM npc_todos
                    WHERE world_id = ? AND status IN ('open', 'doing')
                    ORDER BY CASE status WHEN 'doing' THEN 0 ELSE 1 END, updated_at
                    """,
                    (world_id,),
                ).fetchall()
                recent_events = (
                    self.repository.list_events(connection, world_id, limit=40)
                    if self.settings.world_agent_enabled
                    else []
                )

            if open_todos:
                snapshot = snapshot.model_copy(deep=True)
                todo_by_character: dict[str, list[str]] = {}
                for todo in open_todos:
                    detail = str(todo["details"] or "").strip()
                    text = f"待办：{todo['title']}" + (f"（{detail[:120]}）" if detail else "")
                    todo_by_character.setdefault(str(todo["character_id"]), []).append(text)
                for character in snapshot.characters:
                    additions = todo_by_character.get(character.id, [])[:3]
                    if additions:
                        character.goals = list(dict.fromkeys([*character.goals, *additions]))[:8]

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
            # 战术偏好先在事务外生成，提交时仍使用当前事实校验战斗。
            combat_preferences = {}
            if self.settings.world_agent_combat_enabled:
                for candidate in proposal_by_actor.values():
                    if candidate.action is ActionType.ATTACK:
                        encounter = {"participants_json": json.dumps([
                            candidate.actor_id, candidate.target_id
                        ]), "location_id": snapshot.character_by_id(candidate.actor_id).location_id}
                        combat_preferences[candidate.actor_id] = self._combat_intents(snapshot, encounter)
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
                    self._create_npc_owned_plans(
                        connection, world_id=world_id, characters=active_characters
                    )
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
                                prepared_intents=combat_preferences.get(proposal.actor_id, []),
                            )
                            outcomes.append(outcome)
                        else:
                            outcome = self.actions.execute(
                                connection,
                                world_id=world_id,
                                tick_id=adjudication_id,
                                occurred_at=snapshot.world.current_time,
                                proposal=proposal,
                            )
                            if outcome.accepted and outcome.event_id is not None:
                                self.intent_effects.apply(
                                    connection,
                                    world_id=world_id,
                                    source_event_id=outcome.event_id,
                                    actor_id=proposal.actor_id,
                                    target_id=proposal.target_id,
                                    intent=proposal.dialogue or proposal.reason,
                                )
                            outcomes.append(outcome)
                    if agent_run_records:
                        self._write_agent_audit(
                            connection, world_id, agent_run_records, agent_proposal_records
                        )

                    ContractService.advance(connection, world_id, to_iso(snapshot.world.current_time))
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
                proposal_outcomes = [
                    (outcome, proposal_by_actor[outcome.actor_id])
                    for outcome in outcomes
                    if outcome.actor_id in proposal_by_actor
                ]
                if self._tombstone_destroyed_targets(world_id, proposal_outcomes):
                    with self.database.read() as connection:
                        new_version = int(
                            connection.execute(
                                "SELECT version FROM worlds WHERE id = ?", (world_id,)
                            ).fetchone()["version"]
                        )
                    result.current_version = new_version
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

    def _tombstone_destroyed_targets(
        self,
        world_id: str,
        proposal_outcomes: list[tuple[ActionOutcome, ActionProposal]],
    ) -> bool:
        """将已结算战斗中死亡的 NPC 交给删除器，避免只剩 health=0 的孤立状态。"""

        applied = False
        for outcome, proposal in proposal_outcomes:
            if (
                not outcome.accepted
                or proposal.action is not ActionType.ATTACK
                or not proposal.target_id
                or not outcome.event_id
            ):
                continue
            try:
                with self.database.read() as connection:
                    target = connection.execute(
                        """
                        SELECT health, is_player FROM characters
                        WHERE world_id = ? AND id = ?
                        """,
                        (world_id, proposal.target_id),
                    ).fetchone()
                if target is None or int(target["health"]) > 0 or bool(target["is_player"]):
                    continue
                with self.database.write() as connection:
                    removal = self.element_remover.submit(
                        connection,
                        world_id=world_id,
                        request=ElementRemovalSubmit(
                            requested_by_character_id=outcome.actor_id,
                            source_event_id=outcome.event_id,
                            idempotency_key=f"combat-death:{outcome.event_id}:{proposal.target_id}",
                            target_element_type="character",
                            target_entity_id=proposal.target_id,
                            reason="destroyed",
                            details="该人物在已结算的战斗中死亡，已退出活跃世界。",
                        ),
                    )
                applied = applied or removal.status.value == "applied"
            except Exception:
                # 行动事件已经结算，删除审计失败不得回滚行动；保留日志以便重试/诊断。
                LOGGER.exception("已结算战斗的死亡元素删除未完成")
        return applied

    def close(self) -> None:
        close = getattr(self.decision_provider, "close", None)
        if close is not None:
            close()

    @staticmethod
    def _create_npc_owned_plans(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        characters: list[CharacterState],
    ) -> None:
        """把 NPC 已有的角色目标转为其自行维护的可见计划。"""
        now = to_iso(utc_now())
        for character in characters:
            if character.is_player:
                continue
            existing_titles = {
                str(row["title"])
                for row in connection.execute(
                    """
                    SELECT title FROM npc_todos
                    WHERE world_id = ? AND character_id = ? AND status IN ('open', 'doing')
                    """,
                    (world_id, character.id),
                ).fetchall()
            }
            for goal in character.goals[:3]:
                normalized_goal = str(goal).strip()
                if not normalized_goal:
                    continue
                title = f"推进：{normalized_goal}"[:160]
                if title in existing_titles:
                    continue
                connection.execute(
                    """
                    INSERT INTO npc_todos(id, world_id, character_id, title, details, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        world_id,
                        character.id,
                        title,
                        f"{character.name}根据自身目标自行安排。",
                        now,
                        now,
                    ),
                )
                existing_titles.add(title)

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
        prepared_intents: list[object] | None = None,
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
            intents = prepared_intents or []
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
        intents, status, _ = self.coordinator._run_single(
            lambda: agent.run(ctx, participants=participants, encounter_location=location)
        )
        return intents if status == "ok" else agent._run_rules(ctx, participants)

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
        """事件插入触发器已在原事务中入队；这里不再同步调用模型。"""

    def process_memory_jobs(self, world_id: str, *, limit: int = 10) -> int:
        from world_engine.memory_queue import process_memory_batch

        return process_memory_batch(self, world_id, limit=limit)
