"""对话判定与目标解析：区分说话与行动，锁定本轮对话对象。"""

from __future__ import annotations

import re
import sqlite3
from datetime import timedelta

from world_engine.domain import CharacterState, WorldSnapshot
from world_engine.geo import great_circle_distance_km
from world_engine.player_inputs import parse_player_input, unprefixed_action_text
from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM, same_room
from world_engine.repository import to_iso


class ConversationRoutingMixin:
    """`ConversationService` 的对话判定与目标解析职责切片。"""

    @staticmethod
    def is_addressed_question(intent: str) -> bool:
        return (
            any(pronoun in intent for pronoun in ("你", "您"))
            and any(
                mark in intent
                for mark in ("吗", "么", "呢", "？", "?", "什么", "怎么", "如何")
            )
        )

    @staticmethod
    def is_non_dialogue_intent(intent: str) -> bool:
        message = parse_player_input(intent)
        if message.kind != "auto":
            return message.kind == "action"
        if ConversationRoutingMixin.is_addressed_question(intent):
            return False
        if unprefixed_action_text(intent) is not None:
            return True
        if re.search(r"想知道|想问|请问|询问|打听|如何|怎么|怎样|为什么|什么|是否|能否|可不可以", intent):
            return False
        if any(
            marker in intent
            for marker in ("告诉", "询问", "打听", "请教", "聊天", "交谈", "谈谈", "打招呼")
        ):
            return False
        if re.search(r"打(?!算|听|招呼|工)", intent):
            return True
        if re.match(r"^(?:我)?(?:(?:现在|先|想|要|打算|准备)\s*)*(?:去(?!过)|到|赶往)\S+", intent.strip()):
            return True
        return any(
            marker in intent
            for marker in (
                "攻击", "袭击", "砍", "杀", "前往", "赶路", "动身", "休息",
                "睡", "吃", "喝", "捡", "拾", "使用", "装备", "开始工作", "去工作",
                "干活", "打工", "建造",
            )
        )

    @staticmethod
    def looks_like_follow_up(intent: str) -> bool:
        if parse_player_input(intent).kind == "speech":
            return True
        if ConversationRoutingMixin.is_non_dialogue_intent(intent):
            return False
        markers = ("是", "对", "嗯", "好", "那", "所以", "需要", "吗", "？", "?", "怎么", "如何", "想知道")
        return any(marker in intent for marker in markers)

    @staticmethod
    def looks_like_directed_dialogue(intent: str) -> bool:
        """区分“向人物说话”和“只是在陈述中提到人物”，避免误锁对话目标。"""
        if ConversationRoutingMixin.is_addressed_question(intent):
            return True
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
            "打招呼",
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
