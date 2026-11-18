"""NPC 主动联系：模型决定是否开口，生成成功后原子投递。"""

from datetime import timedelta

from pydantic import BaseModel, Field, TypeAdapter

from world_engine.actions import ActionService
from world_engine.agent_llm import AgentLLMError
from world_engine.appointments import AppointmentIntent
from world_engine.appointment_followup import AppointmentFollowupService
from world_engine.companion_proposals import CompanionIntent
from world_engine.encounters import EncounterService
from world_engine.families import FamilyIntent
from world_engine.geo import great_circle_distance_km
from world_engine.rides import RideIntent, RideService
from world_engine.group_journeys import GroupJourneyIntent
from world_engine.intimacy import IntimacyService, RelationshipIntent, RelationshipReflection
from world_engine.journeys import JourneyIntent
from world_engine.lodging_plans import LodgingPlanIntent
from world_engine.overnight import OvernightIntent
from world_engine.proximity import same_room
from world_engine.relationship_continuity import RelationshipContinuityService
from world_engine.repository import WorldRepository, to_iso, utc_now
from world_engine.roleplay import npc_reply_system_prompt
from world_engine.separate_stays import SeparateStayIntent
from world_engine.shared_life import SharedLifeIntent
from world_engine.shared_plans import SharedPlanIntent
from world_engine.travel_interaction import TravelInteractionService


class OutreachText(BaseModel):
    should_contact: bool
    reply: str | None = Field(default=None, min_length=1, max_length=500)
    social_move: str = "answer"
    relationship_intent: RelationshipIntent | None = None
    relationship_reflection: RelationshipReflection | None = None
    shared_life_intent: SharedLifeIntent | None = None
    family_intent: FamilyIntent | None = None
    appointment_intent: AppointmentIntent | None = None
    shared_plan_intent: SharedPlanIntent | None = None
    journey_intent: JourneyIntent | None = None
    group_journey_intent: GroupJourneyIntent | None = None
    ride_intent: RideIntent | None = None
    overnight_intent: OvernightIntent | None = None
    separate_stay_intent: SeparateStayIntent | None = None
    lodging_plan_intent: LodgingPlanIntent | None = None
    companion_intent: CompanionIntent | None = None


def _companion_opportunity_current(c, job, at):
    """哥哥，过期同行邀请不再占一次联系，也不把旧邀请当成现在仍可接受。"""
    from json import loads

    if not job["source_event_id"]:
        return True
    event = c.execute("SELECT event_type,payload_json FROM world_events WHERE id=? AND world_id=?",
                      (job["source_event_id"],job["world_id"])).fetchone()
    if not event or event["event_type"] != "social.appointment_companion_proposed":
        return True
    try:
        proposal_id = loads(event["payload_json"])["proposal_id"]
    except (KeyError,TypeError,ValueError):
        return False
    return bool(c.execute(
        "SELECT 1 FROM companion_proposals WHERE id=? AND world_id=? AND proposer_id=? AND recipient_id=? "
        "AND status='pending' AND expires_at>?",
        (proposal_id,job["world_id"],job["player_id"],job["npc_id"],to_iso(at)),
    ).fetchone())


def _group_opportunity_current(c, job, at):
    """哥哥，固定原提议来源只为尚未本人答复的组员保留一次真实考虑机会。"""
    from json import loads

    if not job["source_event_id"]:
        return True
    event = c.execute("SELECT event_type,payload_json,actor_id FROM world_events WHERE id=? AND world_id=?",
                      (job["source_event_id"], job["world_id"])).fetchone()
    if not event or event["event_type"] != "social.group_journey_proposed":
        return True
    try:
        journey_id = loads(event["payload_json"])["journey_id"]
    except (KeyError, TypeError, ValueError):
        return False
    return bool(c.execute(
        "SELECT 1 FROM group_journeys g JOIN group_journey_members m ON m.journey_id=g.id "
        "WHERE g.id=? AND g.world_id=? AND g.player_id=? AND g.proposer_id=? "
        "AND g.proposal_event_id=? AND g.status='pending' AND g.deadline_at>? AND g.proposed_at<=? "
        "AND m.character_id=? AND m.decision='pending' AND m.left_at IS NULL",
        (journey_id, job["world_id"], job["player_id"], event["actor_id"], job["source_event_id"],
         to_iso(at), to_iso(at), job["npc_id"]),
    ).fetchone())


def _letter_channel_current(c, job):
    """哥哥，异地联系只能使用这两人当前共同同意的信笺渠道。"""
    return bool(c.execute(
        "SELECT 1 FROM character_contacts WHERE id=? AND world_id=? AND status='accepted' "
        "AND ((requester_id=? AND recipient_id=?) OR (requester_id=? AND recipient_id=?))",
        (job["contact_id"], job["world_id"], job["npc_id"], job["player_id"], job["player_id"], job["npc_id"]),
    ).fetchone())


def process_outreach(engine, wid):
    backend = engine.agent_model_backend
    if backend is None:
        return 0
    now = utc_now()
    with engine.database.write() as c:
        snapshot = engine.repository.get_snapshot(c, wid)
        day = snapshot.world.current_time.date().isoformat()
        c.execute(
            "UPDATE npc_outreach_jobs SET status='cancelled',error='联系时机已过' WHERE world_id=? AND world_day<? AND status IN ('pending','failed','processing')",
            (wid, day),
        )
        if (
            c.execute(
                "SELECT count(*) FROM npc_outreach_jobs WHERE world_id=? AND world_day=? AND status='done'",
                (wid, day),
            ).fetchone()[0]
            >= 4
        ):
            return 0
        job = c.execute(
            "SELECT * FROM npc_outreach_jobs WHERE world_id=? AND attempts<3 AND "
            "(status='pending' OR (status='failed' AND claim_time<?) OR (status='processing' AND claim_time<?)) "
            "ORDER BY CASE WHEN reason LIKE '有临近%' THEN 0 ELSE 1 END,CASE WHEN EXISTS(SELECT 1 FROM relationship_continuity_followups f WHERE f.job_id=npc_outreach_jobs.id) THEN 0 ELSE 1 END,world_day,id LIMIT 1",
            (wid, to_iso(now - timedelta(minutes=2)), to_iso(now - timedelta(minutes=5))),
        ).fetchone()
        if job is None:
            return 0
        if not _companion_opportunity_current(c,job,snapshot.world.current_time):
            c.execute("UPDATE npc_outreach_jobs SET status='cancelled',error='原同行提议已结束，旧联系时机停止' WHERE id=?",(job["id"],))
            return 0
        if not _group_opportunity_current(c, job, snapshot.world.current_time):
            c.execute("UPDATE npc_outreach_jobs SET status='cancelled',error='原多人提议已结束或本人已答复，旧联系时机停止' WHERE id=?", (job["id"],))
            return 0
        if not AppointmentFollowupService.current_job(c, job, snapshot.world.current_time):
            c.execute("UPDATE npc_outreach_jobs SET status='cancelled',error='原约定已变化，旧补约考虑停止' WHERE id=?", (job["id"],))
            return 0
        if not TravelInteractionService.current_job(c, job, snapshot.world.current_time):
            c.execute("UPDATE npc_outreach_jobs SET status='cancelled',error='原共同移动已结束或变化，旧交谈机会停止' WHERE id=?", (job["id"],))
            return 0
        motion_at_claim = TravelInteractionService.for_job(c, job, snapshot.world.current_time)
        if not EncounterService.current_job(c, job, snapshot.world.current_time):
            c.execute(
                "UPDATE npc_outreach_jobs SET status='cancelled',error='本次见面已结束或交谈时机已过' "
                "WHERE id=?", (job["id"],),
            )
            return 0
        encounter_at_claim = EncounterService.for_job(c, job, snapshot.world.current_time)
        if not RelationshipContinuityService.current_job(c,job["id"]):
            c.execute("UPDATE npc_outreach_jobs SET status='cancelled',error='本人持续事项已发生变化，旧联系时机不再适用' WHERE id=?",(job["id"],))
            return 0
        c.execute(
            "UPDATE npc_outreach_jobs SET status='processing',claim_time=?,attempts=attempts+1 WHERE id=?",
            (to_iso(now), job["id"]),
        )
        job = dict(job)
        npc = snapshot.character_by_id(job["npc_id"])
        player = snapshot.character_by_id(job["player_id"])
        if npc is None or player is None or npc.health <= 0 or player.health <= 0:
            c.execute("UPDATE npc_outreach_jobs SET status='cancelled' WHERE id=?", (job["id"],))
            return 0
        if IntimacyService.paused(c, wid, npc.id, player.id):
            c.execute("UPDATE npc_outreach_jobs SET status='cancelled',error='一方要求暂停主动联系' WHERE id=?", (job["id"],))
            return 0
        if job["channel"] == "in_person" and (
            not same_room(npc, player)
            or great_circle_distance_km(
                npc.longitude, npc.latitude, player.longitude, player.latitude
            )
            > 0.02
        ):
            c.execute("UPDATE npc_outreach_jobs SET status='cancelled' WHERE id=?", (job["id"],))
            return 0
        if job["channel"] == "letter" and not _letter_channel_current(c, job):
            c.execute("UPDATE npc_outreach_jobs SET status='cancelled',error='原信笺联系已失效' WHERE id=?", (job["id"],))
            return 0
        if (
            c.execute(
                "SELECT 1 FROM character_life_activities WHERE character_id IN (?,?) AND status='running' AND kind!='wait'",
                (npc.id, player.id),
            ).fetchone()
            or (not motion_at_claim and c.execute(
                "SELECT 1 FROM character_movements WHERE character_id IN (?,?) AND status='moving'",
                (npc.id, player.id),
            ).fetchone())
        ):
            c.execute(
                "UPDATE npc_outreach_jobs SET status='pending',attempts=MAX(0,attempts-1),error='人物当前忙碌，稍后再考虑' WHERE id=?",
                (job["id"],),
            )
            return 0
        context = engine.dialogue_context.build(
            c,
            snapshot=snapshot,
            npc=npc,
            player=player,
            player_text="",
            conversation=None,
            channel=job["channel"],
            interaction="这是你主动考虑联系对方，而不是对方刚说了话",
            decision_details={"opportunity": job["reason"], "may_decline": True},
        )
        if motion_at_claim:
            context["shared_travel"] = TravelInteractionService.context(motion_at_claim)
        if encounter_at_claim:
            context["current_encounter"] = EncounterService.context(c, encounter_at_claim, npc.id)
        focus = c.execute("SELECT thread_id FROM relationship_continuity_followups WHERE job_id=?", (job["id"],)).fetchone()
        if focus:
            context["intimacy"]["ongoing_considerations"] = RelationshipContinuityService.context(c,wid,npc.id,player.id,snapshot.world.current_time,focus_id=focus[0])
        version = snapshot.world.version
    try:
        completion = backend.complete(
            label="npc_outreach",
            roleplay=True,
            system_prompt=npc_reply_system_prompt()
            + '\n本轮你自主决定是否联系对方。若不合适，输出{"should_contact":false,"reply":null}；'
            '若决定联系，只输出{"should_contact":true,"reply":"本人的中文发言","social_move":"answer","relationship_intent":null,"shared_life_intent":null,"family_intent":null,"appointment_intent":null,"shared_plan_intent":null,"journey_intent":null,"group_journey_intent":null,"overnight_intent":null,"separate_stay_intent":null,"lodging_plan_intent":null,"companion_intent":null,"relationship_reflection":null}。'
            '可按上述规则提出或回应本人的关系与住宿选择；同屋住宿引用intimacy.overnights，分房住宿引用intimacy.separate_stays与separate_stay_rooms，住宿筹备用intimacy.lodging_plans和lodging_room_options，只承诺本人预算，不代动玩家钱；不能代替玩家同意、付款或check_in。不得虚构对方刚说过的话。',
            user_payload=context,
            schema=TypeAdapter(OutreachText),
        )
        result = completion.data
        if result.should_contact and (
            not result.reply
            or not result.reply.strip()
            or any(
                word in result.reply
                for word in ("纳米机器人", "人工智能", "系统权限", "RAG", "数据库", "prompt")
            )
        ):
            raise AgentLLMError("主动联系文本未通过校验")
        with engine.database.write() as c:
            current = engine.repository.get_snapshot(c, wid)
            if not _companion_opportunity_current(c,job,current.world.current_time):
                c.execute("UPDATE npc_outreach_jobs SET status='cancelled',error='原同行提议已结束，旧联系时机停止' WHERE id=?",(job["id"],))
                return 0
            if not _group_opportunity_current(c, job, current.world.current_time):
                c.execute("UPDATE npc_outreach_jobs SET status='cancelled',error='原多人提议已结束或本人已答复，旧联系时机停止' WHERE id=?", (job["id"],))
                return 0
            if not AppointmentFollowupService.current_job(c, job, current.world.current_time):
                c.execute("UPDATE npc_outreach_jobs SET status='cancelled',error='原约定已变化，旧补约考虑停止' WHERE id=?", (job["id"],))
                return 0
            latest = c.execute(
                "SELECT status,claim_time FROM npc_outreach_jobs WHERE id=?", (job["id"],)
            ).fetchone()
            if latest["status"] != "processing" or latest["claim_time"] != to_iso(now):
                return 0
            motion_at_delivery = TravelInteractionService.for_job(c, job, current.world.current_time)
            if not TravelInteractionService.current_job(c, job, current.world.current_time) or (
                motion_at_claim and any(
                    motion_at_delivery[key] != motion_at_claim[key]
                    for key in ("npc_movement_id", "player_movement_id")
                )
            ):
                c.execute("UPDATE npc_outreach_jobs SET status='cancelled',error='原共同移动已结束或变化，旧交谈机会停止' WHERE id=?", (job["id"],))
                return 0
            encounter_at_delivery = EncounterService.for_job(c, job, current.world.current_time)
            if not EncounterService.current_job(c, job, current.world.current_time) or (
                encounter_at_claim and (
                    encounter_at_delivery["episode_id"] != encounter_at_claim["episode_id"]
                    or encounter_at_delivery["source_event_id"] != encounter_at_claim["source_event_id"]
                )
            ):
                c.execute(
                    "UPDATE npc_outreach_jobs SET status='cancelled',error='本次见面已结束或交谈时机已过' "
                    "WHERE id=?", (job["id"],),
                )
                return 0
            if current.world.version != version:
                c.execute(
                    "UPDATE npc_outreach_jobs SET status='failed',error='世界状态变化，等待重新考虑' WHERE id=?",
                    (job["id"],),
                )
                return 0
            if (
                current.world.current_time.date().isoformat() != job["world_day"]
                or c.execute(
                    "SELECT count(*) FROM npc_outreach_jobs WHERE world_id=? AND world_day=? AND status='done'",
                    (wid, day),
                ).fetchone()[0]
                >= 4
            ):
                c.execute(
                    "UPDATE npc_outreach_jobs SET status='cancelled',error='联系时机或当日预算已变化' WHERE id=?",
                    (job["id"],),
                )
                return 0
            if (
                c.execute(
                    "SELECT 1 FROM character_life_activities WHERE character_id IN (?,?) AND status='running' AND kind!='wait'",
                    (npc.id, player.id),
                ).fetchone()
                or (not motion_at_delivery and c.execute(
                    "SELECT 1 FROM character_movements WHERE character_id IN (?,?) AND status='moving'",
                    (npc.id, player.id),
                ).fetchone())
            ):
                c.execute(
                    "UPDATE npc_outreach_jobs SET status='pending',attempts=MAX(0,attempts-1) WHERE id=?",
                    (job["id"],),
                )
                return 0
            if not result.should_contact:
                RelationshipContinuityService.considered(c,job["id"],current.world.current_time)
                c.execute("UPDATE npc_outreach_jobs SET status='declined' WHERE id=?", (job["id"],))
                return 0
            if IntimacyService.paused(c, wid, npc.id, player.id):
                c.execute("UPDATE npc_outreach_jobs SET status='cancelled' WHERE id=?", (job["id"],))
                return 0
            if job["channel"] == "letter":
                if not _letter_channel_current(c, job):
                    c.execute("UPDATE npc_outreach_jobs SET status='cancelled',error='原信笺联系已失效' WHERE id=?", (job["id"],))
                    return 0
                contact = c.execute(
                    "SELECT * FROM character_contacts WHERE id=? AND world_id=? AND status='accepted'",
                    (job["contact_id"], wid),
                ).fetchone()
                if contact is None:
                    c.execute(
                        "UPDATE npc_outreach_jobs SET status='cancelled' WHERE id=?", (job["id"],)
                    )
                    return 0
                c.execute(
                    "INSERT OR IGNORE INTO character_messages VALUES (?,?,?,?,?,?,?,?)",
                    (
                        job["id"],
                        wid,
                        job["contact_id"],
                        npc.id,
                        player.id,
                        result.reply,
                        to_iso(current.world.current_time),
                        to_iso(utc_now()),
                    ),
                )
                summary = f"你收到了{npc.name}主动寄来的一封信。"
                event = ActionService._record_event(
                    c,
                    world_id=wid,
                    tick_id=job["id"],
                    occurred_at=current.world.current_time,
                    event_type="social.letter_received",
                    actor_id=npc.id,
                    target_id=player.id,
                    location_id=None,
                    summary=summary,
                    payload={"message_id": job["id"], "channel": "letter"},
                )
            else:
                from world_engine.domain import ActionProposal, ActionType

                outcome = ActionService().execute(
                    c,
                    world_id=wid,
                    tick_id=job["id"],
                    occurred_at=current.world.current_time,
                    proposal=ActionProposal(
                        actor_id=npc.id,
                        target_id=player.id,
                        action=ActionType.SOCIALIZE,
                        reason="根据自身处境主动交流",
                        dialogue=result.reply,
                        metadata={"delivery": "normal", "npc_initiated": True},
                    ),
                )
                if not outcome.accepted:
                    c.execute(
                        "UPDATE npc_outreach_jobs SET status='failed',error=? WHERE id=?",
                        (outcome.summary, job["id"]),
                    )
                    return 0
                event = outcome.event_id
                summary = f"{npc.name}主动与你说话。"
            # 哥哥，先确保实际投递成功，再落定关系与本人的主观看法。
            from world_engine.decisions import NpcReply
            from world_engine.npc_performance import settle_npc_reply

            npc_row = c.execute("SELECT * FROM characters WHERE id=?", (npc.id,)).fetchone()
            settled = settle_npc_reply(
                c, npc=npc_row, player_id=player.id, at=current.world.current_time,
                channel=job["channel"], reply=NpcReply(reply=result.reply,
                    relationship_intent=result.relationship_intent, relationship_reflection=result.relationship_reflection,
                    shared_life_intent=result.shared_life_intent,family_intent=result.family_intent,
                    appointment_intent=result.appointment_intent,shared_plan_intent=result.shared_plan_intent,journey_intent=result.journey_intent,group_journey_intent=result.group_journey_intent,ride_intent=result.ride_intent,overnight_intent=result.overnight_intent,separate_stay_intent=result.separate_stay_intent,lodging_plan_intent=result.lodging_plan_intent,companion_intent=result.companion_intent),
                source_event_id=event,
            )
            result.reply = settled.reply
            if job["channel"] == "letter":
                c.execute("UPDATE character_messages SET content=? WHERE id=?", (result.reply, job["id"]))
            else:
                import json

                row = c.execute("SELECT payload_json FROM world_events WHERE id=?", (event,)).fetchone()
                payload = json.loads(row[0])
                payload["dialogue"] = result.reply
                payload.setdefault("metadata", {})["npc_action_event_id"] = settled.action_event_id
                c.execute("UPDATE world_events SET summary=?,payload_json=? WHERE id=?",
                          (f"{npc.name}主动与你说：{result.reply}", json.dumps(payload, ensure_ascii=False), event))
                engine.conversations.record_exchange(
                    c,
                    world_id=wid,
                    npc_id=npc.id,
                    counterpart_id=player.id,
                    event_id=event,
                    world_time=current.world.current_time,
                    player_text="",
                    npc_text=result.reply,
                )
            for cid in (npc.id, player.id):
                # 当面交谈已由 ActionService 保存亲历记忆，不能重复插入导致整次投递回滚。
                if c.execute("SELECT 1 FROM character_memories WHERE character_id=? AND event_id=? AND memory_type='experienced'",(cid,event)).fetchone():
                    continue
                ActionService._record_memory(
                    c,
                    world_id=wid,
                    character_id=cid,
                    event_id=event,
                    memory_type="experienced",
                    summary=summary,
                    importance=5,
                )
            c.execute(
                "INSERT OR IGNORE INTO player_notifications VALUES (?,?,?,?,?,NULL,?)",
                (job["id"], wid, player.id, event, summary, to_iso(current.world.current_time)),
            )
            c.execute(
                "UPDATE npc_outreach_jobs SET status='done',result_event_id=?,error=NULL WHERE id=?",
                (event, job["id"]),
            )
            RelationshipContinuityService.considered(c,job["id"],current.world.current_time)
            WorldRepository().bump_version(c, wid)
        return 1
    except Exception as exc:
        import logging

        logging.getLogger("virtual-world.engine").exception("主动联系投递失败 job_id=%s",job["id"])
        with engine.database.write() as c:
            c.execute(
                "UPDATE npc_outreach_jobs SET status='failed',error=? WHERE id=? AND status='processing' AND claim_time=?",
                (type(exc).__name__, job["id"], to_iso(now)),
            )
        return 0
