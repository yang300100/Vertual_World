"""哥哥，短时同行先各自表达意愿，规则只核对真实条件和已经作出的选择。"""

import json
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.geo import great_circle_distance_km
from world_engine.life import LifeActivityService
from world_engine.proximity import same_room
from world_engine.repository import from_iso, to_iso
from world_engine.schedules import ScheduleService

COMPANION_PROPOSAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS companion_proposals (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 proposer_id TEXT NOT NULL REFERENCES characters(id),
 recipient_id TEXT NOT NULL REFERENCES characters(id),
 leader_id TEXT NOT NULL REFERENCES characters(id),
 companion_id TEXT NOT NULL REFERENCES characters(id),
 minutes INTEGER NOT NULL CHECK(minutes BETWEEN 5 AND 240),
 status TEXT NOT NULL CHECK(status IN (
  'pending','accepted','declined','cancelled','expired','countered')),
 statement TEXT NOT NULL,response TEXT NOT NULL DEFAULT '',
 proposed_at TEXT NOT NULL,expires_at TEXT NOT NULL,
 accepted_at TEXT,session_id TEXT REFERENCES companion_sessions(id),
 parent_proposal_id TEXT REFERENCES companion_proposals(id),
 proposal_event_id TEXT NOT NULL REFERENCES world_events(id),
 source_event_id TEXT NOT NULL REFERENCES world_events(id),
 CHECK(proposer_id<>recipient_id),CHECK(leader_id<>companion_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_companion_proposal_pending_pair ON companion_proposals(
 world_id,MIN(proposer_id,recipient_id),MAX(proposer_id,recipient_id)) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_companion_proposal_due
 ON companion_proposals(world_id,status,expires_at);
CREATE INDEX IF NOT EXISTS idx_companion_proposal_session ON companion_proposals(session_id);
"""


class CompanionIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: Literal["propose", "counter", "accept", "decline", "cancel", "end"]
    proposal_id: str | None = Field(default=None, min_length=1, max_length=100)
    session_id: str | None = Field(default=None, min_length=1, max_length=100)
    minutes: int | None = Field(default=None, ge=5, le=240, strict=True)
    statement: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def coherent(self):
        if self.operation == "propose":
            if self.proposal_id or self.session_id or self.minutes is None:
                raise ValueError("新的短时同行提议需要明确时长，不能冒用已有约定")
        elif self.operation == "counter":
            if not self.proposal_id or self.session_id or self.minutes is None:
                raise ValueError("时长反提议须引用待回应的邀请并明确新的时长")
        elif self.operation == "end":
            if not self.session_id or self.proposal_id or self.minutes is not None:
                raise ValueError("结束同行只引用本人的真实同行记录")
        elif not self.proposal_id or self.session_id or self.minutes is not None:
            raise ValueError("回应同行邀请只引用已有提议，不能改写时长或同行记录")
        return self


class CompanionProposalService:
    @staticmethod
    def _people(c, actor, other):
        if (
            other is None
            or actor["world_id"] != other["world_id"]
            or bool(actor["is_player"]) == bool(other["is_player"])
        ):
            raise ValueError("请选择当前世界中与玩家商量同行的另一位人物")
        player, npc = (actor, other) if actor["is_player"] else (other, actor)
        return player, npc

    @staticmethod
    def _contact(c, actor, other):
        from world_engine.intimacy import IntimacyService

        if (
            actor["health"] <= 0
            or other["health"] <= 0
            or not same_room(actor, other)
            or great_circle_distance_km(
                actor["longitude"], actor["latitude"], other["longitude"], other["latitude"]
            )
            > 0.02
        ):
            raise ValueError("请先在同一可接触场景停下，再商量现在的短时同行")
        if IntimacyService.paused(c, actor["world_id"], actor["id"], other["id"]):
            raise ValueError("一方已表达暂停联系，暂不提出或确认新的同行")

    @staticmethod
    def _record(c, row, actor, other, at, kind, summary):
        from world_engine.appointments import AppointmentService

        payload = {
            "proposal_id": row["id"],
            "minutes": row["minutes"],
            "leader_id": row["leader_id"],
            "companion_id": row["companion_id"],
            "expires_at": row["expires_at"],
            "parent_proposal_id": row["parent_proposal_id"],
        }
        return AppointmentService._record(
            c, actor, other, at, "companion_" + kind, summary, payload
        )

    @staticmethod
    def _receipt(c, row, *, status=None):
        result = {
            "id": row["id"],
            "proposal_id": row["id"],
            "status": status or row["status"],
            "event_id": row["source_event_id"],
            "source_event_id": row["source_event_id"],
            "minutes": row["minutes"],
            "expires_at": row["expires_at"],
            "parent_proposal_id": row["parent_proposal_id"],
            "summary": "短时同行提议已记录；另一方明确接受前不会开始同行。",
        }
        if row["session_id"]:
            session = c.execute(
                "SELECT * FROM companion_sessions WHERE id=?", (row["session_id"],)
            ).fetchone()
            result.update(
                session_id=row["session_id"],
                id=row["session_id"],
                status=session["status"] if session else "accepted",
                summary="双方已经分别确认这份短时同行；当前位置和后续移动仍按真实规则处理。",
            )
        return result

    @staticmethod
    def _find(c, actor, other, pid):
        row = c.execute(
            "SELECT * FROM companion_proposals WHERE id=? AND world_id=? AND "
            "((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
            (pid, actor["world_id"], actor["id"], other["id"], other["id"], actor["id"]),
        ).fetchone()
        if not row:
            raise ValueError("这份同行提议不属于双方")
        return dict(row)

    @staticmethod
    def _execution_available(c, player, npc, at, minutes):
        from world_engine.companions import CompanionService
        from world_engine.daily_life import DailyLifeService
        from world_engine.journeys import JourneyService
        from world_engine.overnight import OvernightService
        from world_engine.group_journeys import GroupJourneyService
        for person in (player, npc):
            GroupJourneyService.assert_available(c, person)

        if JourneyService.active(c, player["world_id"], player["id"]) or JourneyService.active(
            c, npc["world_id"], npc["id"]
        ):
            raise ValueError("请先暂停或结束共同旅行，再另行确认短时同行")
        if CompanionService.unavailable(c, npc, at):
            raise ValueError(
                "一位参与者现在尚不能履行完整的短时同行，请先处理原有需要或商量其他时长"
            )
        if (
            player["health"] <= 0
            or player["current_room_id"]
            or player["current_fixture_id"]
            or player["active_vehicle_id"]
            or player["movement_type"] != "land"
        ):
            raise ValueError("请先起身、离开房间并选择徒步，再确认现在同行")
        if (
            LifeActivityService.running(c, player["id"])
            or c.execute(
                "SELECT 1 FROM character_movements WHERE character_id IN (?,?) AND status='moving'",
                (player["id"], npc["id"]),
            ).fetchone()
        ):
            raise ValueError("请先完成当前活动并停下移动，再确认现在同行")
        if c.execute(
            "SELECT 1 FROM player_steering WHERE character_id=? AND (dx!=0 OR dy!=0)",
            (player["id"],),
        ).fetchone():
            raise ValueError("请先停止方向移动，再确认现在同行")
        if c.execute(
            "SELECT 1 FROM companion_sessions WHERE companion_id=? AND status='active'",
            (npc["id"],),
        ).fetchone():
            raise ValueError("对方已经有正在执行的短时同行，请先处理现有约定")
        end = at + timedelta(minutes=minutes)
        traits = set(json.loads(npc["traits_json"] or "[]"))
        sleep = at.replace(
            hour=7 if "巡夜" in (npc["identity"] or "") or "夜行" in traits else 22,
            minute=0,
            second=0,
            microsecond=0,
        )
        if sleep <= at:
            sleep += timedelta(days=1)
        if DailyLifeService.is_sleep_time(npc, at) or end > sleep:
            raise ValueError("约定的完整同行时长与睡眠安排冲突，请明确商量更合适的时长")
        for person in (player, npc):
            plan = ScheduleService.assess(c, person, at)
            if plan and end > plan["depart_at"]:
                raise ValueError("约定的完整同行时长与已有赴约准备时间冲突，请重新商量")
            remaining = OvernightService.planned_minutes(c, person, at)
            if remaining is not None and minutes > remaining:
                raise ValueError("约定的完整同行时长与已有住宿、筹备或会合承诺冲突")
            OvernightService.assert_interval_free(
                c, person, at, end, person["current_location_id"] or person["location_id"]
            )

    @classmethod
    def _new(cls, c, actor, other, at, minutes, statement, parent=None):
        player, npc = cls._people(c, actor, other)
        cls._contact(c, actor, other)
        if c.execute(
            "SELECT 1 FROM companion_sessions WHERE companion_id=? AND status='active'",
            (npc["id"],),
        ).fetchone():
            raise ValueError("双方已有正在执行的同行，或对方已在另一份同行中")
        if (
            parent is None
            and c.execute(
                "SELECT 1 FROM companion_proposals WHERE world_id=? AND status='declined' AND "
                "((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?)) "
                "AND source_event_id IN "
                "(SELECT id FROM world_events WHERE occurred_at>?)",
                (
                    actor["world_id"],
                    actor["id"],
                    other["id"],
                    other["id"],
                    actor["id"],
                    to_iso(at - timedelta(days=1)),
                ),
            ).fetchone()
        ):
            raise ValueError("对方刚表达拒绝，请留出一个世界日再提出新的同行")
        existing = c.execute(
            "SELECT * FROM companion_proposals WHERE world_id=? AND status='pending' AND "
            "((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
            (actor["world_id"], actor["id"], other["id"], other["id"], actor["id"]),
        ).fetchone()
        if existing:
            if (
                parent is None
                and existing["proposer_id"] == actor["id"]
                and existing["minutes"] == minutes
                and existing["statement"] == statement
            ):
                return cls._receipt(c, dict(existing))
            raise ValueError("双方已有待回应的同行提议，请回应、反提时长或撤回原提议")
        row = {
            "id": str(uuid4()),
            "world_id": actor["world_id"],
            "proposer_id": actor["id"],
            "recipient_id": other["id"],
            "leader_id": player["id"],
            "companion_id": npc["id"],
            "minutes": minutes,
            "status": "pending",
            "statement": statement,
            "response": "",
            "proposed_at": to_iso(at),
            "expires_at": to_iso(at + timedelta(minutes=30)),
            "parent_proposal_id": parent["id"] if parent else None,
            "session_id": None,
        }
        event = cls._record(
            c, row, actor, other, at, "proposed", "提出短时同行，等待另一方独立决定。" + statement
        )
        row["source_event_id"] = row["proposal_event_id"] = event
        columns = tuple(row)
        c.execute(
            "INSERT INTO companion_proposals("
            + ",".join(columns)
            + ") VALUES ("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(row[k] for k in columns),
        )
        return cls._receipt(c, row)

    @classmethod
    def apply(cls, c, actor, other, at, choice):
        c.execute("SAVEPOINT companion_proposal_choice")
        try:
            cls._people(c, actor, other)
            cls.maintain(c, actor["world_id"], at)
            return cls._apply(c, actor, other, at, choice)
        except Exception:
            c.execute("ROLLBACK TO companion_proposal_choice")
            raise
        finally:
            c.execute("RELEASE companion_proposal_choice")

    @classmethod
    def _apply(cls, c, actor, other, at, choice):
        from world_engine.companions import CompanionService

        player, npc = cls._people(c, actor, other)
        if choice.operation == "end":
            row = c.execute(
                "SELECT * FROM companion_sessions WHERE id=? AND world_id=? AND "
                "((leader_id=? AND companion_id=?) OR (leader_id=? AND companion_id=?))",
                (
                    choice.session_id,
                    actor["world_id"],
                    actor["id"],
                    other["id"],
                    other["id"],
                    actor["id"],
                ),
            ).fetchone()
            if not row:
                raise ValueError("这份同行记录不属于本人和对方")
            if row["status"] == "active":
                event = CompanionService.end(
                    c, row, at, "一位参与者明确结束短时同行。" + choice.statement
                )
            elif row["status"] == "ended":
                event = row["source_event_id"]
            else:
                raise ValueError("这份记录没有已经开始的同行")
            return {
                "status": "ended",
                "session_id": row["id"],
                "id": row["id"],
                "event_id": event,
                "summary": "本人已结束短时同行，实际位置与已经走过的路程保留。",
            }
        if choice.operation == "propose":
            return cls._new(c, actor, other, at, choice.minutes, choice.statement)
        row = cls._find(c, actor, other, choice.proposal_id)
        if (
            choice.operation == "accept"
            and row["status"] == "accepted"
            and row["recipient_id"] == actor["id"]
        ):
            return cls._receipt(c, row)
        if (
            choice.operation == "decline"
            and row["status"] == "declined"
            and row["recipient_id"] == actor["id"]
        ):
            return cls._receipt(c, row)
        if (
            choice.operation == "cancel"
            and row["status"] == "cancelled"
            and row["proposer_id"] == actor["id"]
        ):
            return cls._receipt(c, row)
        if row["status"] != "pending" or at >= from_iso(row["expires_at"]):
            raise ValueError("请引用双方尚有效的待回应同行提议")
        if choice.operation == "cancel":
            if row["proposer_id"] != actor["id"]:
                raise ValueError("只有提出者可以撤回自己的同行邀请")
            return cls._finish(c, row, actor, other, at, "cancelled", choice.statement)
        if row["recipient_id"] != actor["id"]:
            raise ValueError("只有收到提议的另一方可以接受、拒绝或反提时长")
        cls._contact(c, actor, other)
        if choice.operation == "counter":
            if row["minutes"] == choice.minutes:
                raise ValueError("反提时长应当与原时长不同；同意原条件请明确接受")
            cls._finish(c, row, actor, other, at, "countered", choice.statement)
            return cls._new(c, actor, other, at, choice.minutes, choice.statement, row)
        if choice.operation == "decline":
            return cls._finish(c, row, actor, other, at, "declined", choice.statement)
        cls._execution_available(c, player, npc, at, row["minutes"])
        accepted = cls._finish(c, row, actor, other, at, "accepted", choice.statement)
        c.execute(
            "UPDATE companion_proposals SET accepted_at=? WHERE id=?", (to_iso(at), row["id"])
        )
        session = CompanionService._start_after_consent(c, row["id"], at)
        c.execute(
            "UPDATE companion_proposals SET session_id=? WHERE id=?", (session["id"], row["id"])
        )
        accepted.update(
            status="active",
            id=session["id"],
            session_id=session["id"],
            summary="双方已经分别确认短时同行；仍由各人实际行走，可随时结束。",
        )
        return accepted

    @classmethod
    def _finish(cls, c, row, actor, other, at, status, statement):
        summaries = {
            "accepted": "另一方明确接受短时同行。",
            "declined": "另一方明确婉拒短时同行。",
            "cancelled": "提出者撤回短时同行提议。",
            "countered": "另一方提出不同的同行时长，原提议没有被接受。",
        }
        event = cls._record(
            c,
            row,
            actor,
            other,
            at,
            status,
            summaries.get(status, "短时同行提议结束。") + statement,
        )
        c.execute(
            "UPDATE companion_proposals SET status=?,response=?,source_event_id=? WHERE id=? "
            "AND status='pending'",
            (status, statement, event, row["id"]),
        )
        value = {**row, "status": status, "response": statement, "source_event_id": event}
        result = cls._receipt(c, value)
        result["summary"] = summaries.get(status, "短时同行提议结束。")
        return result

    @classmethod
    def maintain(cls, c, wid, at):
        from world_engine.intimacy import IntimacyService

        for found in c.execute(
            "SELECT * FROM companion_proposals WHERE world_id=? AND status='pending'", (wid,)
        ).fetchall():
            row = dict(found)
            if at < from_iso(row["expires_at"]) and not IntimacyService.paused(
                c, wid, row["proposer_id"], row["recipient_id"]
            ):
                continue
            first = c.execute(
                "SELECT * FROM characters WHERE id=?", (row["proposer_id"],)
            ).fetchone()
            second = c.execute(
                "SELECT * FROM characters WHERE id=?", (row["recipient_id"],)
            ).fetchone()
            expired = at >= from_iso(row["expires_at"])
            when = from_iso(row["expires_at"]) if expired else at
            status = "expired" if expired else "cancelled"
            summary = (
                "同行提议未在期限内得到双方同意，未开始同行。"
                if expired
                else "一方要求留出空间，待回应的同行提议停止。"
            )
            event = cls._record(c, row, first, second, when, status, summary)
            c.execute(
                "UPDATE companion_proposals SET status=?,source_event_id=? WHERE id=? AND "
                "status='pending'",
                (status, event, row["id"]),
            )

    @staticmethod
    def rows(c, wid, cid, at, other=None):
        args = [wid, cid, cid] + ([other, other] if other else [])
        result = []
        for found in c.execute(
            "SELECT * FROM companion_proposals WHERE world_id=? AND (proposer_id=? OR "
            "recipient_id=?)"
            + (" AND (proposer_id=? OR recipient_id=?)" if other else "")
            + " ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END,rowid DESC LIMIT 16",
            args,
        ):
            row = dict(found)
            if row["status"] == "pending" and at >= from_iso(row["expires_at"]):
                row["status"] = "expired"
            row["proposal_id"] = row["id"]
            row["other_id"] = (
                row["recipient_id"] if row["proposer_id"] == cid else row["proposer_id"]
            )
            result.append(row)
        from world_engine.intimacy import IntimacyService

        IntimacyService.mask_records(c, wid, cid, result, ("statement", "response"))
        return result

    @classmethod
    def context(cls, c, wid, cid, other, at):
        keys = (
            "id",
            "proposer_id",
            "recipient_id",
            "leader_id",
            "companion_id",
            "minutes",
            "status",
            "statement",
            "expires_at",
            "parent_proposal_id",
        )
        return [
            {k: row[k] for k in keys}
            for row in cls.rows(c, wid, cid, at, other)
            if row["status"] == "pending"
        ][:4]

    @staticmethod
    def boundary(c, wid, at, result):
        found = c.execute(
            "SELECT MIN(expires_at) FROM companion_proposals WHERE world_id=? AND "
            "status='pending' AND expires_at>?",
            (wid, to_iso(at)),
        ).fetchone()
        return min(result, from_iso(found[0])) if found and found[0] else result
