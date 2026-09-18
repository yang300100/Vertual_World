"""`PlayerIntentMixin`：玩家单条意图的解析、结算与战斗死亡元素的墓碑化。

从 `WorldEngine` 切出的职责切片。方法体、签名与 `self` 语义一字未改。
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4

from world_engine.bounded_calls import submit_call
from world_engine.decisions import DecisionProviderError
from world_engine.domain import (
    ActionOutcome,
    ActionProposal,
    ActionType,
    PlayerActionResult,
)
from world_engine.engine_errors import ConcurrentWorldUpdateError
from world_engine.intent_parser import IntentPreview
from world_engine.player_inputs import parse_player_input
from world_engine.removal import ElementRemovalSubmit
from world_engine.repository import to_iso, utc_now

LOGGER = logging.getLogger("virtual-world.engine")


class PlayerIntentMixin:
    """`WorldEngine` 的玩家意图职责切片。"""

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
                    # 必须施加超时预算：respond_to_player 内部是
                    # (deepseek_max_retries + 1) 次、每次 deepseek_timeout_seconds 的重试，
                    # 直接同步调用会让本请求最长阻塞约 3 分钟，前端表现为
                    # 「世界正在回应你的行动」一直转圈。submit_call 超时后丢弃结果、
                    # 不等待后台线程退出，与信件（api_dialogue）和行动反应路径一致。
                    npc_reply = submit_call(
                        responder, npc=target, player=player, context=reply_context
                    ).result(timeout=self.settings.world_agent_timeout_seconds)
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
