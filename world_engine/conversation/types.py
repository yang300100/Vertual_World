"""对话管线共用的数据载体：会话上下文、角色卡与排期发言者。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from world_engine.domain import CharacterState


@dataclass(frozen=True)
class NpcConversationContext:
    npc_id: str
    npc_name: str
    turns: tuple[str, ...]

    def planning_intent(self, player_intent: str) -> str:
        turns = "\n".join(self.turns)
        return (
            f"【连续对话对象：{self.npc_name}】\n"
            "以下为最近一小时内的真实对话，带有世界时间；本轮必须承接其话题。\n"
            f"{turns}\n"
            f"【本轮玩家原话】{player_intent}"
        )


@dataclass(frozen=True)
class NpcCharacterCard:
    """供对话使用的稳定角色底色；不是要求逐句复述的人设模板。"""

    public_role: str
    current_preoccupation: str
    private_tension: str
    social_boundary: str
    expression_notes: str
    speech_style: str
    initiative_notes: str
    preferred_address: str
    dialogue_examples: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "public_role": self.public_role,
            "current_preoccupation": self.current_preoccupation,
            "private_tension": self.private_tension,
            "social_boundary": self.social_boundary,
            "expression_notes": self.expression_notes,
            "speech_style": self.speech_style,
            "initiative_notes": self.initiative_notes,
            "preferred_address": self.preferred_address,
            "dialogue_examples": list(self.dialogue_examples),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> NpcCharacterCard:
        return cls(
            public_role=str(value.get("public_role") or "居民").strip()[:120],
            current_preoccupation=(
                str(value.get("current_preoccupation") or "处理眼前的事务").strip()[:240]
            ),
            private_tension=(
                str(value.get("private_tension") or "不愿轻易暴露自己的顾虑").strip()[:240]
            ),
            social_boundary=(
                str(value.get("social_boundary") or "会先判断谈话是否合适").strip()[:240]
            ),
            expression_notes=(
                str(value.get("expression_notes") or "按当下处境自然说话").strip()[:240]
            ),
            speech_style=(
                str(value.get("speech_style") or "使用符合身份的自然口语，避免重复套话")
                .strip()[:240]
            ),
            initiative_notes=(
                str(value.get("initiative_notes") or "必要时追问来意，并把话题带回自己关心的事务")
                .strip()[:240]
            ),
            preferred_address=(
                str(value.get("preferred_address") or "根据关系和场合自然称呼对方").strip()[:120]
            ),
            dialogue_examples=tuple(
                str(item).strip()[:300]
                for item in (value.get("dialogue_examples") or [])
                if str(item).strip()
            )[:4],
        )


@dataclass(frozen=True)
class ScheduledDialogueSpeaker:
    character: CharacterState
    score: float
    reasons: tuple[str, ...]
