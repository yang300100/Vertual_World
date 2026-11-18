"""NPC 回复的事实门禁：先结算行动，再交付台词与表演。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

from world_engine.decisions import NpcReply
from world_engine.diagnostic_logging import diagnostic_json
from world_engine.intimacy import IntimacyService
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
_SELFCARE_CLAIM = re.compile(
    r"(?:已|已经|刚|刚刚|现已).{0,10}(?:吃|服|喝|吞|用药|入睡|睡着|休息)"
    r"|(?:吃了|服下|喝下|吞下|用过药|睡着了|休息完了)"
)
_SPEECH_CLAIM = re.compile(
    "|".join((_DOOR_CLAIM.pattern, _ITEM_CLAIM.pattern, _MOVEMENT_CLAIM.pattern, _SELFCARE_CLAIM.pattern))
)
_WORLD_EFFECT = re.compile(
    _SPEECH_CLAIM.pattern
    + r"|拿起|拿走|交给|递给|交到|递到|送给|送到|进入|离开|走进|走出"
    + r"|走入|踏入|到达|抵达|坐下|站起|拥抱|触碰|触摸|吃|服药|用药|喝|吞|休息|入睡|睡着"
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
    source_event_id: str | None = None,
) -> SettledNpcReply:
    """Yuki 先请世界结算候选动作，再给哥哥最终能看到的回复。"""
    speech = safe_npc_speech(reply.reply)
    action = reply.action_intent
    if action is not None and action.kind in {"self_care", "self_rest", "cross_layer"}:
        competing = any(getattr(reply, field) is not None for field in (
            "ride_intent", "relationship_intent", "shared_life_intent", "family_intent",
            "appointment_intent", "shared_plan_intent", "journey_intent", "group_journey_intent",
            "overnight_intent", "separate_stay_intent", "lodging_plan_intent", "companion_intent",
        ))
        if competing:
            return SettledNpcReply(
                safe_npc_speech(reply.fallback_reply) or "这次我先不做这个决定。",
                "本人行动须单独选择，不能同时夹带其他行动。", "rejected",
            )
    if reply.ride_intent is not None:
        from world_engine.rides import RideService

        choice = reply.ride_intent
        connection.execute("SAVEPOINT npc_ride_choice")
        try:
            if any(getattr(reply, field) is not None for field in (
                "action_intent", "relationship_intent", "shared_life_intent", "family_intent",
                "appointment_intent", "shared_plan_intent", "journey_intent", "group_journey_intent",
                "overnight_intent", "separate_stay_intent", "lodging_plan_intent", "companion_intent",
            )):
                raise ValueError("共乘选择需要单独表达，不能同时夹带其他行动")
            if channel not in {"in_person", "group_scene", "action_observation"} and choice.operation not in {"decline", "request_stop", "end", "decline_recovery", "withdraw_recovery", "decline_handoff", "withdraw_handoff", "revoke_handoff"}:
                raise ValueError("远程只能婉拒或撤回本人原共乘安排，不能代本人登车和驾驶")
            result = RideService.apply(connection, npc, at, choice)
        except ValueError as exc:
            connection.execute("ROLLBACK TO npc_ride_choice")
            speech = safe_npc_speech(reply.fallback_reply) or "这次共乘选择还没有成立，我们先核对原安排。"
            result = {"summary": str(exc), "status": "rejected", "event_id": None}
        finally:
            connection.execute("RELEASE npc_ride_choice")
        if reply.relationship_reflection is not None and source_event_id:
            IntimacyService.reflect(connection, npc, player_id, at, source_event_id, reply.relationship_reflection)
        return SettledNpcReply(speech or "……", result["summary"], result["status"], result["event_id"])
    if reply.group_journey_intent is not None:
        from world_engine.group_journeys import GroupJourneyService

        choice = reply.group_journey_intent
        # 哥哥，本人结构化选择独立结算；裸台词不能补同意，也不夹带其他世界效果。
        competing = any(getattr(reply, field) is not None for field in (
            "action_intent", "relationship_intent", "shared_life_intent", "family_intent",
            "appointment_intent", "shared_plan_intent", "journey_intent", "overnight_intent",
            "separate_stay_intent", "lodging_plan_intent", "companion_intent",
        ))
        connection.execute("SAVEPOINT npc_group_journey")
        try:
            if competing:
                raise ValueError("多人旅行选择须单独表达，不能夹带另一份效果候选")
            if channel not in {"in_person", "group_scene", "action_observation"} and choice.operation not in {"leave", "pause", "end", "withdraw_follow"}:
                raise ValueError("远处只能操作本人已有多人行程的暂停或退出")
            result = GroupJourneyService.apply(connection, npc, at, choice)
        except ValueError as exc:
            connection.execute("ROLLBACK TO npc_group_journey")
            speech = safe_npc_speech(reply.fallback_reply) or "这次多人行程的选择尚未成立，请先核对原完整条款。"
            result = {"summary": str(exc), "status": "rejected", "event_id": None}
        finally:
            connection.execute("RELEASE npc_group_journey")
        if reply.relationship_reflection is not None and source_event_id:
            IntimacyService.reflect(connection, npc, player_id, at, source_event_id, reply.relationship_reflection)
        return SettledNpcReply(speech or "……", result["summary"], result["status"], result["event_id"])
    if reply.relationship_reflection is not None and source_event_id:
        IntimacyService.reflect(connection, npc, player_id, at, source_event_id, reply.relationship_reflection)
    local = channel in {"in_person", "group_scene", "action_observation"}
    beat = _safe_clauses(reply.stage_direction, _WORLD_EFFECT) if local else ""
    action = reply.action_intent
    relationship = reply.relationship_intent
    relationship_result = None
    if reply.lodging_plan_intent is not None:
        from world_engine.lodging_plans import LodgingPlanService
        private_booking = reply.lodging_plan_intent.operation in {"quote", "confirm"}
        try:
            other=connection.execute("SELECT * FROM characters WHERE id=? AND world_id=?",(player_id,npc["world_id"])).fetchone()
            relationship_result=LodgingPlanService.apply(connection,npc,other,at,reply.lodging_plan_intent)
            if private_booking:
                # 哥哥，本人房价和办理障碍只记本人回执，不让旁观的旅伴读到私下细节。
                relationship_result={"status":"recorded","event_id":None,"summary":""}
        except ValueError as exc:
            speech=safe_npc_speech(reply.fallback_reply) or ("我会自己处理这边的准备，准备好后再回站商量。" if private_booking else "这份订房筹备还不能这样落实，我们先核对各自的预算、房间和期限。")
            relationship_result={"status":"recorded" if private_booking else "rejected","event_id":None,"summary":"" if private_booking else str(exc)}
        beat=" ".join(part for part in (beat,relationship_result["summary"]) if part)
    if reply.separate_stay_intent is not None:
        from world_engine.separate_stays import SeparateStayService
        try:
            other=connection.execute("SELECT * FROM characters WHERE id=? AND world_id=?",(player_id,npc["world_id"])).fetchone()
            relationship_result=SeparateStayService.apply(connection,npc,other,at,reply.separate_stay_intent)
        except ValueError as exc:
            speech=safe_npc_speech(reply.fallback_reply) or "这份分房安排还不能这样落实，我们先核对各自的房间和时间。"
            relationship_result={"status":"rejected","event_id":None,"summary":str(exc)}
        beat=" ".join(part for part in (beat,relationship_result["summary"]) if part)
    if reply.companion_intent is not None:
        from world_engine.companion_proposals import CompanionProposalService
        try:
            other=connection.execute("SELECT * FROM characters WHERE id=? AND world_id=?",(player_id,npc["world_id"])).fetchone()
            relationship_result=CompanionProposalService.apply(connection,npc,other,at,reply.companion_intent)
        except ValueError as exc:
            speech=safe_npc_speech(reply.fallback_reply) or "这段同行还不能这样落实，我们先商量各自的时间和安排。"
            relationship_result={"status":"rejected","event_id":None,"summary":str(exc)}
        beat=" ".join(part for part in (beat,relationship_result["summary"]) if part)
    if reply.overnight_intent is not None:
        from world_engine.overnight import OvernightService
        try:
            other=connection.execute("SELECT * FROM characters WHERE id=? AND world_id=?",(player_id,npc["world_id"])).fetchone()
            relationship_result=OvernightService.apply(connection,npc,other,at,reply.overnight_intent)
        except ValueError as exc:
            speech=safe_npc_speech(reply.fallback_reply) or "这份住宿还不能这样开始，我们先核对眼前的条件。"
            relationship_result={"status":"rejected","event_id":None,"summary":str(exc)}
        beat=" ".join(part for part in (beat,relationship_result["summary"]) if part)
    if reply.journey_intent is not None:
        from world_engine.journeys import JourneyService
        try:
            other=connection.execute("SELECT * FROM characters WHERE id=? AND world_id=?",(player_id,npc["world_id"])).fetchone()
            relationship_result=JourneyService.apply(connection,npc,other,at,reply.journey_intent)
        except ValueError as exc:
            speech=safe_npc_speech(reply.fallback_reply) or "这份行程暂时不能这样继续，我们先把眼前条件说清楚。"
            relationship_result={"status":"rejected","event_id":None,"summary":str(exc)}
        beat=" ".join(part for part in (beat,relationship_result["summary"]) if part)
    if reply.shared_plan_intent is not None:
        from world_engine.shared_plans import SharedPlanService
        connection.execute("SAVEPOINT npc_shared_plan")
        try:
            other=connection.execute("SELECT * FROM characters WHERE id=? AND world_id=?",(player_id,npc["world_id"])).fetchone()
            relationship_result=SharedPlanService.apply(connection,npc,other,at,reply.shared_plan_intent)
        except ValueError as exc:
            connection.execute("ROLLBACK TO npc_shared_plan")
            relationship_result={"status":"rejected","event_id":None,"summary":str(exc)}
            speech=safe_npc_speech(reply.fallback_reply) or "这份分工还没能落实，我们再商量一下。"
        finally:connection.execute("RELEASE npc_shared_plan")
        beat=" ".join(part for part in (beat,relationship_result["summary"]) if part)
    if reply.appointment_intent is not None:
        from world_engine.appointments import AppointmentService

        connection.execute("SAVEPOINT npc_appointment")
        try:
            other=connection.execute("SELECT * FROM characters WHERE id=? AND world_id=?",(player_id,npc["world_id"])).fetchone()
            relationship_result=AppointmentService.apply(connection,npc,other,at,reply.appointment_intent)
        except ValueError as exc:
            connection.execute("ROLLBACK TO npc_appointment")
            relationship_result={"status":"rejected","event_id":None,"summary":str(exc)}
            speech=safe_npc_speech(reply.fallback_reply) or "这次安排还没能定下来，我们再核对一下时间和地点。"
        finally:
            connection.execute("RELEASE npc_appointment")
        beat=" ".join(part for part in (beat,relationship_result["summary"]) if part)
    if reply.family_intent is not None:
        from world_engine.families import FamilyService

        connection.execute("SAVEPOINT npc_family")
        try:
            other=connection.execute("SELECT * FROM characters WHERE id=? AND world_id=?",(player_id,npc["world_id"])).fetchone()
            relationship_result=FamilyService.apply(connection,npc,other,at,reply.family_intent)
        except ValueError as exc:
            connection.execute("ROLLBACK TO npc_family")
            relationship_result={"status":"rejected","event_id":None,"summary":str(exc)}
            speech=safe_npc_speech(reply.fallback_reply) or speech
        finally:
            connection.execute("RELEASE npc_family")
        beat=" ".join(part for part in (beat,relationship_result["summary"]) if part)
    if reply.shared_life_intent is not None:
        from world_engine.shared_life import SharedLifeService

        connection.execute("SAVEPOINT npc_shared_life")
        try:
            other = connection.execute("SELECT * FROM characters WHERE id=? AND world_id=?", (player_id, npc["world_id"])).fetchone()
            relationship_result = SharedLifeService.apply(connection, npc, other, at, reply.shared_life_intent)
        except ValueError as exc:
            connection.execute("ROLLBACK TO npc_shared_life")
            speech = safe_npc_speech(reply.fallback_reply) or "眼前还没法开始这件事，我们可以再商量。"
            relationship_result = {"status": "rejected", "event_id": None, "summary": str(exc)}
        finally:
            connection.execute("RELEASE npc_shared_life")
        beat = " ".join(part for part in (beat, relationship_result["summary"]) if part)
    if relationship is not None:
        # 哥哥，两类选择各自结算；失败的关系选择不能留下半份约定或事件。
        connection.execute("SAVEPOINT npc_relationship")
        try:
            other = connection.execute(
                "SELECT * FROM characters WHERE id=? AND world_id=?",
                (player_id, npc["world_id"]),
            ).fetchone()
            relationship_result = IntimacyService.apply(connection, npc, other, at, relationship)
        except ValueError as exc:
            connection.execute("ROLLBACK TO npc_relationship")
            speech = safe_npc_speech(reply.fallback_reply) or "这份关系选择尚未成立。"
            relationship_result = {"status": "rejected", "event_id": None, "summary": str(exc)}
        finally:
            connection.execute("RELEASE npc_relationship")
        beat = " ".join(part for part in (beat, relationship_result["summary"]) if part)
    if action is None:
        LOGGER.info(
            "NPC回复结算 npc_id=%s channel=%s action_status=no_action",
            npc["id"],
            channel,
        )
        return SettledNpcReply(
            speech or "……", beat or None,
            relationship_result["status"] if relationship_result else None,
            relationship_result["event_id"] if relationship_result else None,
        )
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
            connection, npc=npc, player_id=player_id, at=at, action=action,
            source_event_id=source_event_id,
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
