"""`GroupDialogueMixin`：多人对话的在场发言者调度与整批原子记录。

从 `WorldEngine` 切出的职责切片。方法体、签名与 `self` 语义一字未改；
全部回应仍在同一个 `with self.database.write()` 事务里写入。
"""

from __future__ import annotations

import logging
from uuid import uuid4

from world_engine.conversations import DialogueSpeakerScheduler
from world_engine.decisions import DecisionProviderError
from world_engine.domain import ActionProposal, ActionType
from world_engine.engine_errors import ConcurrentWorldUpdateError
from world_engine.repository import to_iso, utc_now

LOGGER = logging.getLogger("virtual-world.engine")


class GroupDialogueMixin:
    """`WorldEngine` 的多人对话职责切片。"""

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
