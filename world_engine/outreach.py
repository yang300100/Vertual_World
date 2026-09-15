"""NPC 主动联系：模型决定是否开口，生成成功后原子投递。"""

from datetime import timedelta

from pydantic import BaseModel, Field, TypeAdapter

from world_engine.actions import ActionService
from world_engine.agent_llm import AgentLLMError
from world_engine.geo import great_circle_distance_km
from world_engine.proximity import same_room
from world_engine.repository import to_iso, utc_now
from world_engine.roleplay import npc_reply_system_prompt


class OutreachText(BaseModel):
    should_contact: bool
    reply: str | None = Field(default=None, min_length=1, max_length=500)
    social_move: str = "answer"


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
            "ORDER BY CASE WHEN reason LIKE '有临近%' THEN 0 ELSE 1 END,world_day,id LIMIT 1",
            (wid, to_iso(now - timedelta(minutes=2)), to_iso(now - timedelta(minutes=5))),
        ).fetchone()
        if job is None:
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
        if job["channel"] == "in_person" and (
            not same_room(npc, player)
            or great_circle_distance_km(
                npc.longitude, npc.latitude, player.longitude, player.latitude
            )
            > 0.02
        ):
            c.execute("UPDATE npc_outreach_jobs SET status='cancelled' WHERE id=?", (job["id"],))
            return 0
        if (
            c.execute(
                "SELECT 1 FROM character_life_activities WHERE character_id IN (?,?) AND status='running' AND kind!='wait'",
                (npc.id, player.id),
            ).fetchone()
            or c.execute(
                "SELECT 1 FROM character_movements WHERE character_id IN (?,?) AND status='moving'",
                (npc.id, player.id),
            ).fetchone()
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
        version = snapshot.world.version
    try:
        completion = backend.complete(
            label="npc_outreach",
            system_prompt=npc_reply_system_prompt()
            + '\n本轮你自主决定是否联系对方。若不合适，输出{"should_contact":false,"reply":null}；'
            '若决定联系，只输出{"should_contact":true,"reply":"本人的中文发言","social_move":"answer"}。不得虚构对方刚说过的话。',
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
            latest = c.execute(
                "SELECT status,claim_time FROM npc_outreach_jobs WHERE id=?", (job["id"],)
            ).fetchone()
            if latest["status"] != "processing" or latest["claim_time"] != to_iso(now):
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
                or c.execute(
                    "SELECT 1 FROM character_movements WHERE character_id IN (?,?) AND status='moving'",
                    (npc.id, player.id),
                ).fetchone()
            ):
                c.execute(
                    "UPDATE npc_outreach_jobs SET status='pending',attempts=MAX(0,attempts-1) WHERE id=?",
                    (job["id"],),
                )
                return 0
            if not result.should_contact:
                c.execute("UPDATE npc_outreach_jobs SET status='declined' WHERE id=?", (job["id"],))
                return 0
            if job["channel"] == "letter":
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
            c.execute("UPDATE worlds SET version=version+1 WHERE id=?", (wid,))
        return 1
    except Exception as exc:
        with engine.database.write() as c:
            c.execute(
                "UPDATE npc_outreach_jobs SET status='failed',error=? WHERE id=? AND status='processing' AND claim_time=?",
                (type(exc).__name__, job["id"], to_iso(now)),
            )
        return 0
