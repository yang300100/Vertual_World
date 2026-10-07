"""NPC 回复的事实门禁：先结算行动，再交付台词与表演。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from world_engine.decisions import NpcReply
from world_engine.diagnostic_logging import diagnostic_json
from world_engine.npc_stage_actions import settle_stage_action

LOGGER = logging.getLogger("virtual-world.engine")

_DOOR_CLAIM = re.compile(
    r"(?:门|房门|大门|木门).{0,10}(?:打开|推开|拉开|关上|掩上|锁上|解锁|开启|合上)"
    r"|(?:打开|推开|拉开|关上|掩上|锁上|解锁|开启|合上).{0,10}(?:门|房门|大门|木门)"
)
_ITEM_CLAIM = re.compile(
    r"(?:已|已经|刚|刚刚|现已).{0,12}(?:交到|递到|交给|递给|送到|送给|给了|拿走|取走)"
    r"|(?:交到|递到|送到).{0,8}(?:手|怀|背包)"
    r"|把.{0,12}(?:交给|递给|给了).{0,3}了"
)
_MOVEMENT_CLAIM = re.compile(
    r"(?:已|已经|刚|刚刚|现已).{0,10}(?:进入|走进|走入|踏入|离开|走出|到达|抵达)"
    r"|(?<!想)(?<!要)(?<!会)(?<!准备)(?<!打算)"
    r"(?:走进|走入|踏入|进入|离开|走出|到达|抵达).{0,8}(?:了|屋|房|门|目的地)"
)
_SPEECH_CLAIM = re.compile(
    "|".join((_DOOR_CLAIM.pattern, _ITEM_CLAIM.pattern, _MOVEMENT_CLAIM.pattern))
)
_WORLD_EFFECT = re.compile(
    _SPEECH_CLAIM.pattern
    + r"|拿起|拿走|交给|递给|交到|递到|送给|送到|进入|离开|走进|走出"
    + r"|走入|踏入|到达|抵达|坐下|站起|拥抱|触碰|触摸"
)
_CLAUSES = re.compile(r"(?<=[。！？；])")


@dataclass(frozen=True)
class SettledNpcReply:
    reply: str
    stage_direction: str | None
    action_status: str | None = None
    action_event_id: str | None = None


def finalize_social_event(
    connection,
    *,
    event_id: str,
    summary: str,
    reply: SettledNpcReply,
) -> None:
    """Yuki 在事务提交前把临时台词换成世界已裁定的最终版本。"""
    row = connection.execute(
        "SELECT payload_json FROM world_events WHERE id=?", (event_id,)
    ).fetchone()
    if row is None:
        raise ValueError("对话事件已不存在")
    payload = json.loads(row[0])
    metadata = payload.setdefault("metadata", {})
    payload["reply"] = reply.reply
    metadata["npc_stage_direction"] = reply.stage_direction
    metadata["npc_action_status"] = reply.action_status
    metadata["npc_action_event_id"] = reply.action_event_id
    connection.execute(
        "UPDATE world_events SET summary=?,payload_json=? WHERE id=?",
        (summary, json.dumps(payload, ensure_ascii=False), event_id),
    )
    connection.execute(
        "UPDATE character_memories SET summary=? WHERE event_id=?",
        (summary, event_id),
    )
    from world_engine.epistemics import KnowledgeService

    KnowledgeService.capture_final_reply_name(connection, event_id, reply.reply)


def _safe_clauses(value: str | None, forbidden: re.Pattern[str]) -> str:
    """Yuki 只留下不声称世界效果的短句，危险片段由裁定结果替换。"""
    if not value:
        return ""
    clean = re.sub(
        r"\*([^*]+)\*",
        lambda match: "" if forbidden.search(match.group(1)) else match.group(1),
        value,
    ).replace("*", "").strip()
    return "".join(
        clause
        for clause in _CLAUSES.split(clean)
        if clause.strip() and not forbidden.search(clause)
    ).strip()


def safe_npc_speech(value: str | None) -> str:
    """台词可表达愿望；Yuki 删掉直接宣告未裁定物品、门和位置变化的片段。"""
    return _safe_clauses(value, _SPEECH_CLAIM)


def settle_npc_reply(
    connection,
    *,
    npc,
    player_id: str | None = None,
    at: datetime,
    channel: str,
    reply: NpcReply,
) -> SettledNpcReply:
    """Yuki 先请世界结算候选动作，再给哥哥最终能看到的回复。"""
    speech = safe_npc_speech(reply.reply)
    local = channel in {"in_person", "group_scene", "action_observation"}
    beat = _safe_clauses(reply.stage_direction, _WORLD_EFFECT) if local else ""
    action = reply.action_intent
    if action is None:
        LOGGER.info(
            "NPC回复结算 npc_id=%s channel=%s action_status=no_action",
            npc["id"],
            channel,
        )
        return SettledNpcReply(speech or "……", beat or None)
    if not local:
        LOGGER.info(
            "NPC行动结算 npc_id=%s channel=%s status=rejected reason=remote_channel",
            npc["id"],
            channel,
        )
        return SettledNpcReply(
            safe_npc_speech(reply.fallback_reply) or "等见面再说。",
            None,
            "rejected",
        )

    try:
        resolved = settle_stage_action(
            connection, npc=npc, player_id=player_id, at=at, action=action
        )
    except ValueError as exc:
        # Yuki 不让模型在失败后偷偷改写场景，失败只影响本次呈现。
        LOGGER.info(
            "NPC行动结算 npc_id=%s channel=%s status=rejected reason=%s",
            npc["id"],
            channel,
            diagnostic_json(str(exc)),
        )
        speech = safe_npc_speech(reply.fallback_reply) or "等一下，我没能做到。"
        return SettledNpcReply(
            speech,
            " ".join(part for part in (beat, f"{npc['name']}没能完成刚才的动作。") if part),
            "rejected",
        )
    LOGGER.info(
        "NPC行动结算 npc_id=%s channel=%s status=%s event_id=%s",
        npc["id"],
        channel,
        resolved.status,
        resolved.event_id,
    )
    return SettledNpcReply(
        speech or "……",
        " ".join(part for part in (beat, resolved.narration) if part),
        resolved.status,
        resolved.event_id,
    )
