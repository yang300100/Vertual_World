"""对话判定与目标解析：区分说话与行动，锁定本轮对话对象。"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

from world_engine.domain import CharacterState, WorldSnapshot
from world_engine.geo import great_circle_distance_km
from world_engine.player_inputs import parse_player_input
from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM, same_room
from world_engine.repository import to_iso


class ConversationRoutingMixin:
    """`ConversationService` 的对话判定与目标解析职责切片。"""

    @staticmethod
    def is_non_dialogue_intent(intent: str) -> bool:
        message = parse_player_input(intent)
        if message.kind != "auto":
            return message.kind == "action"
        return any(
            marker in intent
            for marker in (
                "攻击", "袭击", "砍", "杀", "打", "前往", "赶路", "动身", "休息",
                "睡", "吃", "喝", "捡", "拾", "使用", "装备", "开始工作", "去工作",
                "干活", "建造",
            )
        )

    @staticmethod
    def looks_like_follow_up(intent: str) -> bool:
        if parse_player_input(intent).kind == "speech":
            return True
        if ConversationRoutingMixin.is_non_dialogue_intent(intent):
            return False
        markers = ("是", "对", "嗯", "好", "那", "所以", "需要", "吗", "？", "?")
        return any(marker in intent for marker in markers)

    @staticmethod
    def looks_like_directed_dialogue(intent: str) -> bool:
        """区分“向人物说话”和“只是在陈述中提到人物”，避免误锁对话目标。"""
        markers = (
            "交谈",
            "对话",
            "聊",
            "问",
            "询问",
            "打听",
            "告诉",
            "跟他说",
            "向他说",
            "请教",
            "请求",
            "你好",
        )
        return any(marker in intent for marker in markers)

    def resolve_target(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot: WorldSnapshot,
        player: CharacterState,
        intent: str,
        target_character_id: str | None,
    ) -> CharacterState | None:
        by_id = {item.id: item for item in snapshot.characters}
        if target_character_id:
            target = by_id.get(target_character_id)
            if target is None or target.is_player:
                raise ValueError("指定的对话对象不存在于当前世界")
            if not same_room(player, target) or great_circle_distance_km(
                player.longitude, player.latitude, target.longitude, target.latitude
            ) > VISIBLE_PERSON_RADIUS_KM:
                raise ValueError("指定的对话对象不在同一空间或距离超过100米")
            return target
        named = next(
            (
                item
                for item in sorted(snapshot.characters, key=lambda value: -len(value.name))
                if not item.is_player and item.name in intent
            ),
            None,
        )
        if named is not None:
            # 这里只负责锁定“对话”对象。攻击、旅行等非对话意图仍由各自动作规则
            # 决定有效距离，不能因为对话限制而误拦截既有行动解析。
            if self.is_non_dialogue_intent(intent):
                return named
            if not self.looks_like_directed_dialogue(intent):
                return None
            if not same_room(player, named) or great_circle_distance_km(
                player.longitude, player.latitude, named.longitude, named.latitude
            ) > VISIBLE_PERSON_RADIUS_KM:
                raise ValueError(f"{named.name}不在你100米视线范围内")
            return named
        if not self.looks_like_follow_up(intent):
            return None
        cutoff = to_iso(snapshot.world.current_time - timedelta(hours=1))
        row = connection.execute(
            """
            SELECT npc_character_id FROM npc_conversation_sessions
            WHERE world_id = ? AND counterpart_character_id = ?
              AND last_world_time >= ? AND status = 'active'
            ORDER BY last_world_time DESC, updated_at DESC LIMIT 1
            """,
            (snapshot.world.id, player.id, cutoff),
        ).fetchone()
        if row is None:
            return None
        target = by_id.get(row["npc_character_id"])
        if target is None or not same_room(player, target) or great_circle_distance_km(
            player.longitude, player.latitude, target.longitude, target.latitude
        ) > VISIBLE_PERSON_RADIUS_KM:
            return None
        return target
