"""角色卡管理：读取、确定性补全与持久化 NPC 的对话底色。"""

from __future__ import annotations

import json
import sqlite3

from world_engine.conversation.types import NpcCharacterCard
from world_engine.domain import CharacterState
from world_engine.repository import to_iso, utc_now


class ConversationCardMixin:
    """`ConversationService` 的角色卡职责切片。"""

    @staticmethod
    def default_card(npc: CharacterState) -> NpcCharacterCard:
        """从已有角色资料确定性补全首张卡，不凭空添加具体世界事实。"""
        traits = set(npc.traits)
        role = (npc.identity or "居民").strip()
        concern = next((item.strip() for item in npc.goals if item.strip()), f"把{role}的事务办妥")
        if traits & {"警觉", "谨慎", "守序", "戒备", "孤僻"}:
            boundary = "陌生人问得太深时，会先追问来意或暂缓回答"
            expression = "先问清对象和缘由，再给有限而具体的答复"
        elif traits & {"热情", "温和", "仁慈", "健谈"}:
            boundary = "愿意帮忙，但不会替别人作出无法兑现的承诺"
            expression = "会接住对方的话，也常把话题带回自己关心的人或事"
        elif traits & {"直率", "务实", "专注", "坚韧"}:
            boundary = "讨厌空话，若被耽误会直接结束谈话"
            expression = "偏向先说可做与不可做的事，不绕远弯"
        else:
            boundary = "会先判断场合、关系与手头事务，再决定说多少"
            expression = "不急着下结论，会留下尚未说完的余地"
        if traits & {"热情", "健谈"}:
            speech_style = "语气亲切，句子稍长，会自然补充与话题有关的见闻"
            initiative = "愿意主动追问，也会提起自己正在关心的人或事"
        elif traits & {"警觉", "谨慎", "戒备", "孤僻"}:
            speech_style = "措辞克制，先确认来意，未知和不愿说的部分会明确保留"
            initiative = "发现问题涉及隐私或风险时，会反问来源或结束话题"
        elif traits & {"直率", "务实", "专注"}:
            speech_style = "表达直接，偏好具体的人、物、时间与可执行事项"
            initiative = "会把空泛问题收束成可以回答或办理的具体问题"
        else:
            speech_style = "语气自然含蓄，会随关系和场合调整回答长短"
            initiative = "有相关牵挂时可以主动提及；话题已经说清时也可以自然结束"
        trait_text = "、".join(npc.traits[:2]) or "职责"
        return NpcCharacterCard(
            public_role=role,
            current_preoccupation=concern,
            private_tension=f"在{trait_text}与眼前责任之间寻找不失分寸的做法",
            social_boundary=boundary,
            expression_notes=expression,
            speech_style=speech_style,
            initiative_notes=initiative,
            preferred_address="根据关系与场合自然称呼对方，不固定使用尊称",
            dialogue_examples=(),
        )

    def get_card(
        self,
        connection: sqlite3.Connection,
        *,
        world_id: str,
        npc: CharacterState,
        persist_default: bool = False,
    ) -> NpcCharacterCard:
        row = connection.execute(
            "SELECT card_json FROM npc_character_cards WHERE world_id = ? AND character_id = ?",
            (world_id, npc.id),
        ).fetchone()
        if row is not None:
            try:
                raw = json.loads(row["card_json"])
                if isinstance(raw, dict):
                    return NpcCharacterCard.from_dict(raw)
            except (json.JSONDecodeError, TypeError):
                pass
        card = self.default_card(npc)
        if persist_default:
            self.save_card(connection, world_id=world_id, npc_id=npc.id, card=card)
        return card

    @staticmethod
    def save_card(
        connection: sqlite3.Connection,
        *,
        world_id: str,
        npc_id: str,
        card: NpcCharacterCard,
    ) -> None:
        now = to_iso(utc_now())
        connection.execute(
            """
            INSERT INTO npc_character_cards(
                character_id, world_id, card_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(character_id) DO UPDATE SET
                card_json = excluded.card_json, updated_at = excluded.updated_at
            """,
            (npc_id, world_id, json.dumps(card.to_dict(), ensure_ascii=False), now, now),
        )
