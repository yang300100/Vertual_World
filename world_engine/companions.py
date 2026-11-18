"""哥哥，同行是一段有期限、可结束的共同经历，人物仍保留自己的需要。"""

import json
from datetime import timedelta
from uuid import uuid4

from world_engine.geo import great_circle_distance_km
from world_engine.life import LifeActivityService
from world_engine.proximity import VISIBLE_PERSON_RADIUS_KM, same_room
from world_engine.repository import from_iso, to_iso, utc_now
from world_engine.schedules import ScheduleService

COMPANION_SCHEMA = """
CREATE TABLE IF NOT EXISTS companion_sessions (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id),
 leader_id TEXT NOT NULL REFERENCES characters(id),
 companion_id TEXT NOT NULL REFERENCES characters(id),
 status TEXT NOT NULL CHECK(status IN ('active','declined','ended')),
 reason TEXT NOT NULL, started_world_time TEXT NOT NULL, until_world_time TEXT NOT NULL,
 ended_world_time TEXT, source_event_id TEXT REFERENCES world_events(id),
 proposal_id TEXT REFERENCES companion_proposals(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companion_active
 ON companion_sessions(companion_id) WHERE status='active';
CREATE INDEX IF NOT EXISTS idx_companion_world ON companion_sessions(world_id,leader_id,status);
CREATE TABLE IF NOT EXISTS companion_movement_links (
 session_id TEXT NOT NULL REFERENCES companion_sessions(id) ON DELETE CASCADE,
 leader_movement_id TEXT NOT NULL REFERENCES character_movements(id) ON DELETE CASCADE,
 companion_movement_id TEXT NOT NULL REFERENCES character_movements(id) ON DELETE CASCADE,
 PRIMARY KEY(session_id,companion_movement_id),
 CHECK(leader_movement_id<>companion_movement_id)
);
CREATE INDEX IF NOT EXISTS idx_companion_leader_movement
 ON companion_movement_links(leader_movement_id);
"""


class CompanionService:
    @staticmethod
    def active(c, leader_id):
        return c.execute(
            "SELECT * FROM companion_sessions WHERE leader_id=? AND status='active'", (leader_id,)
        ).fetchall()

    @staticmethod
    def record(c, session, at, event_type, summary):
        from world_engine.movement import MovementService

        event = MovementService._record_log(
            c,
            world_id=session["world_id"],
            world_time=at,
            event_type=event_type,
            actor_id=session["leader_id"],
            target_id=session["companion_id"],
            summary=summary,
            location_id=c.execute(
                "SELECT current_location_id FROM characters WHERE id=?", (session["leader_id"],)
            ).fetchone()[0],
            payload={
                "session_id": session["id"],
                "reason": session["reason"],
                "until_world_time": session["until_world_time"],
                "proposal_id": session["proposal_id"] if "proposal_id" in session.keys() else None,
            },
        )
        for cid in (session["leader_id"], session["companion_id"]):
            c.execute(
                "INSERT INTO character_memories(id,world_id,character_id,event_id,memory_type,"
                "summary,importance,confidence,created_at) VALUES (?,?,?,?,'experienced',?,5,1,?)",
                (str(uuid4()), session["world_id"], cid, event, summary, to_iso(utc_now())),
            )
        return event

    @staticmethod
    def unavailable(c, npc, at):
        from world_engine.daily_life import DailyLifeService

        if npc["health"] < 40 or npc["energy"] <= 20 or npc["satiety"] <= 20:
            return "对方需要先处理身体或用饭需要"
        if npc["current_room_id"] or npc["current_fixture_id"]:
            return "对方还在室内或座位上，需要先完成眼前的安排"
        if npc["active_vehicle_id"] or npc["movement_type"] != "land":
            return "当前同行只支持各自徒步，交通方式不匹配"
        if LifeActivityService.running(c, npc["id"]):
            return "对方正在进行已有活动"
        if DailyLifeService.is_sleep_time(npc, at):
            return "对方现在需要睡眠"
        plan = ScheduleService.assess(c, npc, at)
        if plan and plan["due"]:
            return "对方需要准备赴约"
        return None

    @classmethod
    def invite(cls, c, wid, player, npc, at, minutes, statement=None):
        """哥哥，旧邀请入口也只记录本人的提议，不按性格数值替对方答应。"""
        from world_engine.companion_proposals import CompanionIntent, CompanionProposalService

        if player["world_id"] != wid or npc["world_id"] != wid:
            raise ValueError("人物不属于当前世界")
        return CompanionProposalService.apply(
            c,
            player,
            npc,
            at,
            CompanionIntent(
                operation="propose",
                minutes=minutes,
                statement=statement
                or f"我邀请你现在在附近一起徒步{minutes}个世界分钟，你可以独立决定或提出其他时长。",
            ),
        )

    @classmethod
    def _start_after_consent(cls, c, proposal_id, at):
        """哥哥，执行中的同行只从双方已经明确确认的真实提议建立。"""
        proposal = c.execute(
            "SELECT * FROM companion_proposals WHERE id=? AND status='accepted'", (proposal_id,)
        ).fetchone()
        if not proposal or proposal["accepted_at"] != to_iso(at):
            raise ValueError("同行尚没有本次双方分别确认的有效同意")
        event = c.execute(
            "SELECT * FROM world_events WHERE id=? AND world_id=?",
            (proposal["source_event_id"], proposal["world_id"]),
        ).fetchone()
        try:
            valid = (
                event
                and event["event_type"] == "social.appointment_companion_accepted"
                and event["actor_id"] == proposal["recipient_id"]
                and event["target_id"] == proposal["proposer_id"]
                and json.loads(event["payload_json"])["proposal_id"] == proposal_id
            )
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("同行缺少另一方明确接受的真实事件")
        if proposal["session_id"]:
            return dict(
                c.execute(
                    "SELECT * FROM companion_sessions WHERE id=?", (proposal["session_id"],)
                ).fetchone()
            )
        from world_engine.group_journeys import GroupJourneyService
        for cid in (proposal["leader_id"], proposal["companion_id"]):
            person = c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
            GroupJourneyService.assert_available(c, person)
        session = dict(
            id=str(uuid4()),
            world_id=proposal["world_id"],
            leader_id=proposal["leader_id"],
            companion_id=proposal["companion_id"],
            status="active",
            reason="双方分别明确同意短时徒步同行，随时可以结束",
            started_world_time=to_iso(at),
            until_world_time=to_iso(at + timedelta(minutes=proposal["minutes"])),
            proposal_id=proposal_id,
        )
        c.execute(
            "INSERT INTO companion_sessions(id,world_id,leader_id,companion_id,status,reason,"
            "started_world_time,until_world_time,proposal_id) VALUES (?,?,?,?,?,?,?,?,?)",
            tuple(
                session[key]
                for key in (
                    "id",
                    "world_id",
                    "leader_id",
                    "companion_id",
                    "status",
                    "reason",
                    "started_world_time",
                    "until_world_time",
                    "proposal_id",
                )
            ),
        )
        source = cls.record(
            c,
            session,
            at,
            "social.companion_started",
            "双方已经分别明确同意短时同行；仍按各自真实路线移动，可随时结束。",
        )
        c.execute(
            "UPDATE companion_sessions SET source_event_id=? WHERE id=?", (source, session["id"])
        )
        session["source_event_id"] = source
        return session

    @classmethod
    def bind_movements(cls, c, leader_id, movements):
        """哥哥，只绑定本次真正共同出发的移动，不猜测旧存档里的行程。"""
        by_person = {movement.character_id: movement.id for movement in movements}
        for session in cls.active(c, leader_id):
            c.execute(
                "INSERT INTO companion_movement_links(session_id,leader_movement_id,"
                "companion_movement_id) VALUES (?,?,?)",
                (session["id"], by_person[leader_id], by_person[session["companion_id"]]),
            )

    @classmethod
    def end(cls, c, session, at, reason):
        if session["status"] != "active":
            return
        from world_engine.movement import MovementService

        updated = c.execute(
            "UPDATE companion_sessions SET status='ended',reason=?,ended_world_time=? "
            "WHERE id=? AND status='active'",
            (reason, to_iso(at), session["id"]),
        )
        if not updated.rowcount:
            return
        # 哥哥，退出只取消本人绑定的同行路程；领队和其他人仍保留自己的选择。
        bound = c.execute(
            "SELECT 1 FROM companion_movement_links l "
            "JOIN character_movements m ON m.id=l.companion_movement_id "
            "WHERE l.session_id=? AND m.character_id=? AND m.status='moving' LIMIT 1",
            (session["id"], session["companion_id"]),
        ).fetchone()
        if bound:
            MovementService().cancel(
                c, world_id=session["world_id"], character_id=session["companion_id"],
                world_time=at, cascade_journey=False,
            )
        event = cls.record(
            c,
            {**dict(session), "reason": reason},
            at,
            "social.companion_ended",
            f"同行结束：{reason}。",
        )
        c.execute(
            "UPDATE companion_sessions SET source_event_id=? WHERE id=?", (event, session["id"])
        )
        return event

    @classmethod
    def maintain(cls, c, wid, at):
        from world_engine.companion_proposals import CompanionProposalService
        from world_engine.intimacy import IntimacyService

        CompanionProposalService.maintain(c, wid, at)
        for session in c.execute(
            "SELECT * FROM companion_sessions WHERE world_id=? AND status='active'", (wid,)
        ).fetchall():
            leader = c.execute(
                "SELECT * FROM characters WHERE id=?", (session["leader_id"],)
            ).fetchone()
            npc = c.execute(
                "SELECT * FROM characters WHERE id=?", (session["companion_id"],)
            ).fetchone()
            leader_unavailable = (
                leader["health"] <= 0 or leader["current_room_id"] or leader["current_fixture_id"]
            )
            if leader_unavailable:
                from world_engine.movement import MovementService
                from world_engine.player_steering import PlayerSteeringService

                # 哥哥，领队无法继续带路才停止本程全队，不能与某位同伴自行退出混为一谈。
                if c.execute(
                    "SELECT 1 FROM companion_movement_links l "
                    "JOIN character_movements m ON m.id=l.leader_movement_id "
                    "WHERE l.session_id=? AND m.character_id=? AND m.status='moving'",
                    (session["id"], leader["id"]),
                ).fetchone():
                    MovementService().cancel(
                        c, world_id=wid, character_id=leader["id"], world_time=at,
                    )
                if PlayerSteeringService.active(c, leader["id"]):
                    PlayerSteeringService.stop(c, wid, at, "领路人的身体或场景状态已经改变。")
            reason = cls.unavailable(c, npc, at)
            if IntimacyService.paused(c, wid, leader["id"], npc["id"]):
                reason = "一方明确要求暂停联系，本次同行结束"
            if at >= from_iso(session["until_world_time"]):
                reason = "约定的同行时间已经结束"
            elif leader_unavailable:
                reason = "领路人的身体或场景状态已经改变"
            elif (
                not same_room(leader, npc)
                or great_circle_distance_km(
                    leader["longitude"], leader["latitude"], npc["longitude"], npc["latitude"]
                )
                > VISIBLE_PERSON_RADIUS_KM
            ):
                reason = "双方已经离开同一可见场景"
            if reason:
                cls.end(c, session, at, reason)

    @classmethod
    def members(cls, c, leader):
        return [
            c.execute("SELECT * FROM characters WHERE id=?", (s["companion_id"],)).fetchone()
            for s in cls.active(c, leader["id"])
        ]

    @classmethod
    def speed_cap(cls, c, actor, speed):
        members = cls.members(c, actor) if actor["is_player"] else []
        if members:
            if actor["active_vehicle_id"] or actor["movement_type"] != "land":
                raise ValueError("同行中请保持徒步，或先结束同行再更换交通方式")
            return min(speed, *(float(n["movement_speed_kmh"]) for n in members))
        return speed
