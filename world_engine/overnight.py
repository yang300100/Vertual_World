"""哥哥，长时住宿独立确认，真实入屋与休息之后才算完成，不扩大房间或身体权限。"""

from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.geo import great_circle_distance_km
from world_engine.life import LifeActivityService
from world_engine.repository import from_iso, to_iso

OVERNIGHT_SCHEMA = """
CREATE TABLE IF NOT EXISTS journey_overnight_stays (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 proposer_id TEXT NOT NULL REFERENCES characters(id),recipient_id TEXT NOT NULL REFERENCES characters(id),
 journey_id TEXT NOT NULL REFERENCES joint_journeys(id),stop_index INTEGER NOT NULL,
 arrival_event_id TEXT NOT NULL REFERENCES world_events(id),pause_event_id TEXT NOT NULL REFERENCES world_events(id),
 room_id TEXT NOT NULL REFERENCES life_rooms(id),location_id TEXT NOT NULL REFERENCES locations(id),
 status TEXT NOT NULL CHECK(status IN ('pending','accepted','active','completed','declined','cancelled','expired','missed','interrupted')),
 arrival_starts_at TEXT NOT NULL,arrival_ends_at TEXT NOT NULL,latest_end_at TEXT NOT NULL,
 minutes INTEGER NOT NULL CHECK(minutes BETWEEN 240 AND 720),visit_host_id TEXT REFERENCES characters(id),
 statement TEXT NOT NULL,response TEXT NOT NULL DEFAULT '',proposed_at TEXT NOT NULL,started_at TEXT,ends_at TEXT,
 proposer_activity_id TEXT REFERENCES character_life_activities(id),recipient_activity_id TEXT REFERENCES character_life_activities(id),
 source_event_id TEXT NOT NULL REFERENCES world_events(id),reason TEXT NOT NULL DEFAULT '',CHECK(proposer_id<>recipient_id),
 FOREIGN KEY(journey_id,stop_index) REFERENCES journey_stops(journey_id,stop_index)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_overnight_pending_pair ON journey_overnight_stays(world_id,MIN(proposer_id,recipient_id),MAX(proposer_id,recipient_id)) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_overnight_live ON journey_overnight_stays(world_id,status,arrival_starts_at);
CREATE INDEX IF NOT EXISTS idx_overnight_trip ON journey_overnight_stays(journey_id,status);
"""


class OvernightIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: Literal["propose", "accept", "decline", "cancel", "check_in"]
    stay_id: str | None = Field(default=None, min_length=1, max_length=100)
    journey_id: str | None = Field(default=None, min_length=1, max_length=100)
    room_id: str | None = Field(default=None, min_length=1, max_length=100)
    arrival_world_time: str | None = None
    window_minutes: int = Field(default=60, ge=15, le=120, strict=True)
    minutes: int = Field(default=480, ge=240, le=720, strict=True)
    allow_visit: bool = Field(default=False, strict=True)
    statement: str = Field(min_length=1, max_length=240)

    @model_validator(mode="before")
    @classmethod
    def defaults(cls, value):
        if isinstance(value, dict):
            return {
                k: v
                for k, v in value.items()
                if not (k in {"window_minutes", "minutes", "allow_visit"} and v is None)
            }
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.operation == "propose":
            if (
                self.stay_id
                or not self.journey_id
                or not self.room_id
                or not self.arrival_world_time
            ):
                raise ValueError("过夜安排须有真实行程、房间和明确到场时间")
        elif (
            not self.stay_id
            or self.journey_id
            or self.room_id
            or self.arrival_world_time
            or self.window_minutes != 60
            or self.minutes != 480
        ):
            raise ValueError("回应住宿只引用真实编号，不能悄悄改写时长、到场窗或房间")
        if self.allow_visit and self.operation not in {"propose", "accept"}:
            raise ValueError("只有提出或接受时才能表达本次接待许可")
        return self


class OvernightService:
    @staticmethod
    def has_live(c, wid):
        from world_engine.group_journeys import GroupJourneyService
        if GroupJourneyService.has_live(c, wid):
            return True
        from world_engine.lodging_plans import LodgingPlanService
        from world_engine.journey_follow import JourneyFollowService
        return JourneyFollowService.has_live(c, wid) or bool(c.execute("SELECT 1 FROM companion_proposals WHERE world_id=? AND status='pending' LIMIT 1",(wid,)).fetchone()) or LodgingPlanService.has_live(c, wid) or bool(c.execute(
            "SELECT 1 FROM journey_overnight_stays WHERE world_id=? "
            "AND status IN ('pending','accepted','active') UNION ALL "
            "SELECT 1 FROM journey_separate_stays WHERE world_id=? AND "
            "(status IN ('pending','accepted','active') OR (status='completed' AND "
            "(reunion_status='waiting' OR (reunion_status='met' AND reunion_wait_ended_at IS NULL "
            "AND reunion_ends_at>(SELECT w.current_time FROM worlds w WHERE w.id=journey_separate_stays.world_id))))) LIMIT 1",
            (wid, wid),
        ).fetchone())

    @staticmethod
    def _people(c, row):
        return [
            c.execute("SELECT * FROM characters WHERE id=?", (row[k],)).fetchone()
            for k in ("proposer_id", "recipient_id")
        ]

    @staticmethod
    def _record(c, first, second, at, state, summary, sid):
        from world_engine.appointments import AppointmentService

        return AppointmentService._record(
            c, first, second, at, "overnight_" + state, summary, {"stay_id": sid}
        )

    @staticmethod
    def _end(row):
        return from_iso(row["ends_at"] or row["latest_end_at"])

    @staticmethod
    def room_options(c, wid, cid, other, at):
        from world_engine.appointment_rooms import AppointmentRoomService
        from world_engine.interiors import InteriorError, InteriorService

        actor = c.execute(
            "SELECT * FROM characters WHERE id=? AND world_id=?", (cid, wid)
        ).fetchone()
        if not actor:
            return []
        result = {
            room["id"]: room
            for room in AppointmentRoomService.room_options(c, wid, cid, other, at)
        }
        # 哥哥，只沿本人此刻真实看见的门补选项，不枚举陌生房间或门后的内室。
        for door in InteriorService.scene(c, actor)["doors"]:
            if door["id"] in result:
                continue
            room = c.execute(
                "SELECT r.*,l.name location_name FROM life_rooms r "
                "JOIN locations l ON l.id=r.location_id "
                "WHERE r.id=? AND r.world_id=? AND l.is_active=1",
                (door["id"], wid),
            ).fetchone()
            if not room:
                continue
            try:
                end = at + timedelta(seconds=1)
                host = AppointmentRoomService.controller(c, room, at, end)
                AppointmentRoomService.validate(
                    c,
                    wid,
                    room["id"],
                    room["location_id"],
                    cid,
                    other,
                    at,
                    end,
                    awaiting_host_id=host if host in {cid, other} else None,
                )
            except InteriorError:
                continue
            result[room["id"]] = {
                "id": room["id"],
                "name": room["name"],
                "location_id": room["location_id"],
                "location_name": room["location_name"],
                "may_host": host == cid,
            }
        return list(result.values())[:32]

    @staticmethod
    def rows(c, wid, cid, at, other=None):
        args = [wid, cid, cid] + ([other, other] if other else [])
        result = []
        for row in c.execute(
            "SELECT s.*,r.name room_name,l.name location_name FROM journey_overnight_stays s JOIN life_rooms r ON r.id=s.room_id JOIN locations l ON l.id=s.location_id WHERE s.world_id=? AND (s.proposer_id=? OR s.recipient_id=?)"
            + (" AND (s.proposer_id=? OR s.recipient_id=?)" if other else "")
            + " ORDER BY CASE WHEN s.status IN ('pending','accepted','active') THEN 0 ELSE 1 END,s.rowid DESC LIMIT 12",
            args,
        ):
            value = dict(row)
            value["other_id"] = (
                row["recipient_id"] if row["proposer_id"] == cid else row["proposer_id"]
            )
            if row["status"] == "pending" and at >= from_iso(row["arrival_starts_at"]):
                value["status"] = "expired"
            result.append(value)
        from world_engine.intimacy import IntimacyService

        IntimacyService.mask_records(c, wid, cid, result, ("statement", "response", "reason"))
        return result

    @classmethod
    def context(cls, c, wid, cid, other, at):
        keys = (
            "id",
            "proposer_id",
            "recipient_id",
            "status",
            "journey_id",
            "room_id",
            "room_name",
            "arrival_starts_at",
            "arrival_ends_at",
            "minutes",
            "visit_host_id",
            "ends_at",
        )
        return [
            {k: r[k] for k in keys}
            for r in cls.rows(c, wid, cid, at, other)
            if r["status"] in {"pending", "accepted", "active"}
        ][:2]

    @classmethod
    def room_risk(cls, c, row, at):
        from world_engine.appointment_rooms import AppointmentRoomService

        end = cls._end(row)
        # 哥哥，租约恰好到醒来时结束也合法，核验最后一段真实占用，不拿零长度区间判房主。
        start = max(from_iso(row["arrival_starts_at"]), min(at, end - timedelta(microseconds=1)))
        try:
            AppointmentRoomService.validate(
                c,
                row["world_id"],
                row["room_id"],
                row["location_id"],
                row["proposer_id"],
                row["recipient_id"],
                start,
                end,
                visit_host_id=row["visit_host_id"],
            )
        except ValueError as exc:
            return str(exc)
        return ""

    @classmethod
    def visit_allowed(cls, c, room, cid, at):
        from world_engine.appointment_rooms import AppointmentRoomService

        for row in c.execute(
            "SELECT * FROM journey_overnight_stays WHERE world_id=? AND status IN ('accepted','active') AND visit_host_id IS NOT NULL AND visit_host_id<>? AND (proposer_id=? OR recipient_id=?) AND arrival_starts_at<=?",
            (room["world_id"], cid, cid, cid, to_iso(at)),
        ):
            if (
                at >= cls._end(row)
                or row["status"] == "accepted"
                and at > from_iso(row["arrival_ends_at"])
            ):
                continue
            host = c.execute(
                "SELECT * FROM characters WHERE id=?", (row["visit_host_id"],)
            ).fetchone()
            if not host or host["health"] <= 0 or host["current_room_id"] != row["room_id"]:
                continue
            try:
                chain = AppointmentRoomService.path(c, row["world_id"], row["room_id"])
            except ValueError:
                continue
            if room["id"] in {r["id"] for r in chain} and not cls.room_risk(c, row, at):
                return True
        return False

    @staticmethod
    def _travel_gap(c, person, first_location, first_room, second_location, second_room):
        from world_engine.appointment_rooms import AppointmentRoomService

        a = c.execute("SELECT * FROM locations WHERE id=?", (first_location,)).fetchone()
        b = c.execute("SELECT * FROM locations WHERE id=?", (second_location,)).fetchone()
        if not a or not b:
            raise ValueError("日程地点已不可用")
        return timedelta(
            hours=great_circle_distance_km(
                a["longitude"], a["latitude"], b["longitude"], b["latitude"]
            )
            / max(1, person["movement_speed_kmh"]),
            minutes=10
            + AppointmentRoomService.transition_minutes(
                c, person["world_id"], first_room, second_room
            ),
        )

    @classmethod
    def assert_interval_free(cls, c, person, start, end, location_id, room_id=None, exclude=None):
        from world_engine.group_journeys import GroupJourneyService
        GroupJourneyService.assert_interval_free(c, person, start, end)
        from world_engine.separate_stays import SeparateStayService
        SeparateStayService.assert_interval_free(c, person, start, end, location_id, room_id, exclude)
        for row in c.execute(
            "SELECT * FROM journey_overnight_stays WHERE world_id=? AND status IN ('accepted','active') AND (proposer_id=? OR recipient_id=?)",
            (person["world_id"], person["id"], person["id"]),
        ):
            if row["id"] == exclude:
                continue
            gap = cls._travel_gap(
                c, person, location_id, room_id, row["location_id"], row["room_id"]
            )
            if start < cls._end(row) + gap and end + gap > from_iso(row["arrival_starts_at"]):
                raise ValueError("这段时间与已确认的长时住宿及往返房间路程冲突")

    @classmethod
    def _schedule_free(cls, c, people, start, end, lid, rid, exclude=None):
        for person in people:
            cls.assert_interval_free(c, person, start, end, lid, rid, exclude)
            for date in c.execute(
                "SELECT f.location_id,f.due_world_time,w.starts_world_time,a.room_id FROM contract_fulfillments f LEFT JOIN appointment_windows w ON w.contract_id=f.request_id LEFT JOIN social_appointments a ON a.contract_id=f.request_id WHERE f.world_id=? AND f.kind='appointment' AND f.status='active' AND (f.requester_id=? OR f.recipient_id=?)",
                (person["world_id"], person["id"], person["id"]),
            ):
                begin = (
                    from_iso(date["starts_world_time"])
                    if date["starts_world_time"]
                    else from_iso(date["due_world_time"]) - timedelta(hours=1)
                )
                gap = cls._travel_gap(c, person, lid, rid, date["location_id"], date["room_id"])
                if start < from_iso(date["due_world_time"]) + gap and end + gap > begin:
                    raise ValueError("住宿与已有约会及往返房间路程冲突，请重新商量")

    @staticmethod
    def _arrival_possible(c, people, at, latest_arrival, lid, rid):
        from world_engine.schedules import ScheduleService

        destination = c.execute("SELECT * FROM locations WHERE id=?", (lid,)).fetchone()
        for person in people:
            if (
                ScheduleService.earliest_arrival(c, person, destination, at, room_id=rid)
                > latest_arrival
            ):
                raise ValueError("现有活动和实际进屋路程无法赶上到场窗，请另选时间")

    @classmethod
    def apply(cls, c, actor, other, at, choice):
        c.execute("SAVEPOINT overnight_choice")
        try:
            return cls._apply(c, actor, other, at, choice)
        except Exception:
            c.execute("ROLLBACK TO overnight_choice")
            raise
        finally:
            c.execute("RELEASE overnight_choice")

    @classmethod
    def _apply(cls, c, actor, other, at, choice):
        from world_engine.appointment_rooms import AppointmentRoomService
        from world_engine.intimacy import IntimacyService
        from world_engine.journey_breaks import JourneyBreakService
        from world_engine.journey_room_appointments import JourneyRoomService
        from world_engine.journeys import JourneyService

        if (
            other is None
            or actor["world_id"] != other["world_id"]
            or bool(actor["is_player"]) == bool(other["is_player"])
        ):
            raise ValueError("请选择当前世界中与玩家共同住宿的另一位人物")
        wid = actor["world_id"]
        cls.maintain(c, wid, at)
        if choice.operation == "propose":
            IntimacyService._reachable(c, actor, other)
            if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                raise ValueError("一方要求留出空间，暂不提出住宿")
            try:
                start = datetime.fromisoformat(choice.arrival_world_time)
            except (ValueError, TypeError) as exc:
                raise ValueError("请提供带时区的明确到场时间") from exc
            if start.tzinfo is None or not at < start <= at + timedelta(days=1):
                raise ValueError("到场须在未来一个世界日内并带时区")
            arrival_end = start + timedelta(minutes=choice.window_minutes)
            end = arrival_end + timedelta(minutes=choice.minutes)
            room = AppointmentRoomService.path(c, wid, choice.room_id)[0]
            known_rooms = {
                r["id"]
                for r in AppointmentRoomService.room_options(c, wid, actor["id"], other["id"], at)
            }
            from world_engine.interiors import InteriorService

            # 哥哥，眼前真实可见的房门可以询问接待，看到门并不等于已经获准进入。
            known_rooms.update(r["id"] for r in InteriorService.scene(c, actor)["doors"])
            if choice.room_id not in known_rooms:
                raise ValueError("只能使用本人已认识、租住或可进入的真实房间")
            trip, stop = JourneyRoomService.validate(
                c, actor, other, at, choice.journey_id, room["location_id"], end
            )
            JourneyService._assert_breaks_finished(c, trip, at)
            host = AppointmentRoomService.controller(c, room, start, end)
            if choice.allow_visit and host != actor["id"]:
                raise ValueError("只有有本次接待权的人可以表达房间访问许可")
            visit_host = actor["id"] if choice.allow_visit else None
            AppointmentRoomService.validate(
                c,
                wid,
                room["id"],
                room["location_id"],
                actor["id"],
                other["id"],
                start,
                end,
                visit_host_id=visit_host,
                awaiting_host_id=other["id"] if host == other["id"] else None,
            )
            cls._schedule_free(c, (actor, other), start, end, room["location_id"], room["id"])
            cls._arrival_possible(
                c, (actor, other), at, arrival_end, room["location_id"], room["id"]
            )
            if c.execute(
                "SELECT 1 FROM journey_overnight_stays WHERE world_id=? AND status='pending' AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
                (wid, actor["id"], other["id"], other["id"], actor["id"]),
            ).fetchone():
                raise ValueError("双方已有待回应的住宿邀请")
            link = JourneyBreakService.prepare(c, actor, other, at, choice.journey_id)
            sid = str(uuid4())
            summary = (
                "原旅行暂停；提出有限长时住宿，房租、进屋和开始休息仍分别处理：" + choice.statement
            )
            event = cls._record(c, actor, other, at, "proposed", summary, sid)
            c.execute(
                "INSERT INTO journey_overnight_stays(id,world_id,proposer_id,recipient_id,journey_id,stop_index,arrival_event_id,pause_event_id,room_id,location_id,status,arrival_starts_at,arrival_ends_at,latest_end_at,minutes,visit_host_id,statement,proposed_at,source_event_id) VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?)",
                (
                    sid,
                    wid,
                    actor["id"],
                    other["id"],
                    choice.journey_id,
                    stop["stop_index"],
                    link["arrival_event_id"],
                    link["pause_event_id"],
                    room["id"],
                    room["location_id"],
                    to_iso(start),
                    to_iso(arrival_end),
                    to_iso(end),
                    choice.minutes,
                    visit_host,
                    choice.statement,
                    to_iso(at),
                    event,
                ),
            )
            return {
                "status": "pending",
                "stay_id": sid,
                "event_id": event,
                "summary": "住宿邀请已记录，原旅行已暂停；未付房费、未入屋、未开始休息。",
            }
        row = c.execute(
            "SELECT * FROM journey_overnight_stays WHERE id=? AND world_id=? AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
            (choice.stay_id, wid, actor["id"], other["id"], other["id"], actor["id"]),
        ).fetchone()
        if not row:
            raise ValueError("这份住宿不属于双方")
        if choice.operation in {"accept", "decline"}:
            IntimacyService._reachable(c, actor, other)
            if row["status"] != "pending" or row["recipient_id"] != actor["id"]:
                raise ValueError("只有收到有效住宿邀请的人可以回应")
            host = row["visit_host_id"]
            if choice.allow_visit:
                room = AppointmentRoomService.path(c, wid, row["room_id"])[0]
                if (
                    AppointmentRoomService.controller(
                        c, room, from_iso(row["arrival_starts_at"]), from_iso(row["latest_end_at"])
                    )
                    != actor["id"]
                ):
                    raise ValueError("本人没有这段住宿的接待权")
                host = actor["id"]
            if choice.operation == "accept":
                if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                    raise ValueError("一方仍要求留出空间")
                candidate = {**dict(row), "visit_host_id": host}
                risk = cls.room_risk(c, candidate, at)
                if risk:
                    raise ValueError(risk)
                cls._schedule_free(
                    c,
                    (actor, other),
                    from_iso(row["arrival_starts_at"]),
                    from_iso(row["latest_end_at"]),
                    row["location_id"],
                    row["room_id"],
                    row["id"],
                )
                cls._arrival_possible(
                    c,
                    (actor, other),
                    at,
                    from_iso(row["arrival_ends_at"]),
                    row["location_id"],
                    row["room_id"],
                )
            status = "accepted" if choice.operation == "accept" else "declined"
            c.execute(
                "UPDATE journey_overnight_stays SET visit_host_id=? WHERE id=?", (host, row["id"])
            )
            summary = (
                "双方确认长时住宿，仍需实际分别入屋后由玩家选择开始。"
                if status == "accepted"
                else "本次住宿已婉拒，原旅行不会自动恢复。"
            )
        elif choice.operation == "check_in":
            if not actor["is_player"]:
                raise ValueError("开始住宿须玩家本人选择，NPC 不能代为入屋或开始玩家休息")
            if row["status"] != "accepted" or not from_iso(
                row["arrival_starts_at"]
            ) <= at <= from_iso(row["arrival_ends_at"]):
                raise ValueError("请在已确认的到场时间窗内入住，不能提前或重复开始")
            risk = cls.room_risk(c, row, at)
            if risk:
                raise ValueError(risk)
            from world_engine.appointments import AppointmentService

            place = c.execute(
                "SELECT * FROM locations WHERE id=? AND is_active=1", (row["location_id"],)
            ).fetchone()
            from world_engine.shared_life import SharedLifeService

            if (
                not place
                or not all(
                    AppointmentService._at_place(c, p, place, row["room_id"])
                    for p in (actor, other)
                )
                or not SharedLifeService._near(actor, other, "rest")
            ):
                raise ValueError("双方须实际逐门到达同一指定房间，不能自动移动或隔屋开始")
            if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                raise ValueError("一方仍要求留出空间")
            people = cls._people(c, row)
            from world_engine.families import FamilyService

            for person in people:
                LifeActivityService.assert_available(c, person["id"])
                if person["health"] <= 0 or person["satiety"] <= 30:
                    raise ValueError("一方需要先用餐或处理身体状态，住宿不会自动购买或消耗食物")
                if not person["is_player"] and FamilyService.hungry_dependent(c, person, at):
                    raise ValueError("对方需要先处理已承担的家庭照护")
            end = at + timedelta(minutes=row["minutes"])
            cls._schedule_free(c, people, at, end, row["location_id"], row["room_id"], row["id"])
            acts = [LifeActivityService.start(c, p, at, "rest", row["minutes"]) for p in people]
            status = "active"
            summary = "双方在真实房间开始已同意的长时休息，尚未完成；身体、照护和访问条件仍可中断。"
            event = cls._record(c, *people, at, "started", summary, row["id"])
            c.execute(
                "UPDATE journey_overnight_stays SET status='active',started_at=?,ends_at=?,proposer_activity_id=?,recipient_activity_id=?,source_event_id=? WHERE id=?",
                (to_iso(at), to_iso(end), *acts, event, row["id"]),
            )
            return {"status": status, "stay_id": row["id"], "event_id": event, "summary": summary}
        else:
            if (
                row["status"] not in {"pending", "accepted", "active"}
                or row["status"] == "pending"
                and row["proposer_id"] != actor["id"]
            ):
                raise ValueError("只能撤回本人邀请，或退出已确认住宿")
            return cls.finish(
                c,
                row,
                at,
                "interrupted" if row["status"] == "active" else "cancelled",
                choice.statement,
            )
        event = cls._record(c, actor, other, at, status, summary + choice.statement, row["id"])
        c.execute(
            "UPDATE journey_overnight_stays SET status=?,response=?,reason=?,source_event_id=? WHERE id=?",
            (status, choice.statement, summary, event, row["id"]),
        )
        if status == "accepted":
            npc = actor if not actor["is_player"] else other
            c.execute(
                "UPDATE npc_daily_states SET next_world_time=? WHERE character_id=?",
                (to_iso(at), npc["id"]),
            )
        return {"status": status, "stay_id": row["id"], "event_id": event, "summary": summary}

    @classmethod
    def finish(cls, c, row, at, status, reason):
        current = c.execute(
            "SELECT * FROM journey_overnight_stays WHERE id=?", (row["id"],)
        ).fetchone()
        if current["status"] not in {"pending", "accepted", "active"}:
            return {
                "status": current["status"],
                "stay_id": row["id"],
                "event_id": current["source_event_id"],
                "summary": current["reason"],
            }
        for key in ("proposer_activity_id", "recipient_activity_id"):
            activity = c.execute(
                "SELECT * FROM character_life_activities WHERE id=? AND status='running'",
                (current[key],),
            ).fetchone()
            if activity:
                LifeActivityService.finish(
                    c, activity, at, "completed" if status == "completed" else "interrupted", reason
                )
        people = cls._people(c, current)
        summary = (
            "双方实际完成约定的长时住宿。"
            if status == "completed"
            else "长时住宿未完整完成：" + reason
        ) + " 不自动退房、付款或恢复旅行；已发生的时间和消耗保留。"
        event = cls._record(c, *people, at, status, summary, row["id"])
        c.execute(
            "UPDATE journey_overnight_stays SET status=?,reason=?,source_event_id=? WHERE id=?",
            (status, reason, event, row["id"]),
        )
        if status == "completed":
            from world_engine.intimacy import IntimacyService

            IntimacyService.remember_event(
                c, row["world_id"], people[0]["id"], people[1]["id"], event, at, "shared", summary
            )
            for a, b in (people, people[::-1]):
                c.execute(
                    "UPDATE character_acquaintances SET shared_experiences=shared_experiences+1 WHERE observer_id=? AND subject_id=?",
                    (a["id"], b["id"]),
                )
        return {"status": status, "stay_id": row["id"], "event_id": event, "summary": summary}

    @classmethod
    def maintain(cls, c, wid, at, complete=True):
        from world_engine.lodging_plans import LodgingPlanService
        LodgingPlanService.maintain(c, wid, at)
        from world_engine.separate_stays import SeparateStayService
        SeparateStayService.maintain(c, wid, at, complete=complete)
        from world_engine.families import FamilyService
        from world_engine.intimacy import IntimacyService

        for row in c.execute(
            "SELECT * FROM journey_overnight_stays WHERE world_id=? AND status IN ('pending','accepted','active')",
            (wid,),
        ).fetchall():
            if row["status"] == "pending":
                trip = c.execute(
                    "SELECT status FROM joint_journeys WHERE id=?", (row["journey_id"],)
                ).fetchone()
                if trip["status"] not in {"paused", "resume_pending"}:
                    cls.finish(c, row, at, "cancelled", "原旅行已结束或改变，未确认的住宿撤销")
                elif at >= from_iso(row["arrival_starts_at"]):
                    cls.finish(c, row, at, "expired", "住宿未在到场前得到双方同意")
                continue
            if row["status"] == "accepted":
                if at > from_iso(row["arrival_ends_at"]):
                    cls.finish(
                        c, row, at, "missed", "到场窗已过，双方没有实际开始住宿；不推定谁故意失约"
                    )
                continue
            people = cls._people(c, row)
            reason = cls.room_risk(c, row, at)
            if not reason and IntimacyService.paused(c, wid, *[p["id"] for p in people]):
                reason = "一方表达了暂停联系的界限"
            for person in people:
                if person["health"] <= 0 or person["satiety"] <= 5:
                    reason = "一方需要先处理身体或用饭"
                elif person["current_room_id"] != row["room_id"]:
                    reason = "一方已离开约定房间"
                elif not person["is_player"] and FamilyService.hungry_dependent(c, person, at):
                    reason = "一方需要处理已承担的家庭照护"
            acts = [
                c.execute(
                    "SELECT * FROM character_life_activities WHERE id=?", (row[k],)
                ).fetchone()
                for k in ("proposer_activity_id", "recipient_activity_id")
            ]
            if any(not a or a["status"] not in {"running", "completed"} for a in acts):
                reason = reason or "一方提前结束了本次休息"
            # 哥哥，先一起核验受伤和原地条件，再让任何一人的活动结算恢复。
            for person, activity in zip(people, acts, strict=True):
                if activity and (
                    person["health"] < activity["initial_health"]
                    or great_circle_distance_km(
                        person["longitude"],
                        person["latitude"],
                        activity["longitude"],
                        activity["latitude"],
                    )
                    > 0.001
                ):
                    reason = reason or "一方的身体状况或所在位置发生变化"
            if (
                not reason
                and c.execute(
                    "SELECT 1 FROM world_events WHERE world_id=? AND event_type='action.attack' AND occurred_at>=? AND ((actor_id=? AND target_id=?) OR (actor_id=? AND target_id=?)) AND created_at>=(SELECT created_at FROM world_events WHERE id=?)",
                    (
                        wid,
                        row["started_at"],
                        people[0]["id"],
                        people[1]["id"],
                        people[1]["id"],
                        people[0]["id"],
                        row["source_event_id"],
                    ),
                ).fetchone()
            ):
                reason = "住宿中发生主动攻击"
            if reason:
                cls.finish(c, row, at, "interrupted", reason)
            elif (
                complete
                and at >= from_iso(row["ends_at"])
                and all(a["status"] == "completed" for a in acts)
            ):
                cls.finish(
                    c, row, from_iso(row["ends_at"]), "completed", "双方实际度过了完整约定时长"
                )

    @classmethod
    def _departure(cls, c, actor, row):
        from world_engine.appointment_rooms import AppointmentRoomService

        target = c.execute(
            "SELECT longitude,latitude FROM locations WHERE id=?", (row["location_id"],)
        ).fetchone()
        travel = (
            great_circle_distance_km(actor["longitude"], actor["latitude"], target[0], target[1])
            / max(1, actor["movement_speed_kmh"])
            if target
            else 0
        )
        return from_iso(row["arrival_starts_at"]) - timedelta(
            hours=travel,
            minutes=10
            + AppointmentRoomService.transition_minutes(
                c, actor["world_id"], actor["current_room_id"], row["room_id"]
            ),
        )

    @classmethod
    def planned_minutes(cls, c, actor, at):
        times = [
            cls._departure(c, actor, r)
            for r in c.execute(
                "SELECT * FROM journey_overnight_stays WHERE world_id=? AND status='accepted' AND (proposer_id=? OR recipient_id=?)",
                (actor["world_id"], actor["id"], actor["id"]),
            )
        ]
        from world_engine.separate_stays import SeparateStayService
        separate = SeparateStayService.planned_minutes(c, actor, at)
        from world_engine.lodging_plans import LodgingPlanService
        lodging = LodgingPlanService.planned_minutes(c, actor, at)
        own = max(0, int((min(times) - at).total_seconds() // 60)) if times else None
        return min(x for x in (lodging, separate, own) if x is not None) if any(x is not None for x in (lodging, separate, own)) else None

    @classmethod
    def perform(cls, c, npc, at):
        from world_engine.appointment_rooms import AppointmentRoomService

        if npc["is_player"] or npc["health"] < 40 or npc["energy"] <= 10 or npc["satiety"] <= 30:
            return None
        row = c.execute(
            "SELECT * FROM journey_overnight_stays WHERE world_id=? AND status='accepted' AND (proposer_id=? OR recipient_id=?) ORDER BY arrival_starts_at LIMIT 1",
            (npc["world_id"], npc["id"], npc["id"]),
        ).fetchone()
        if not row or at < cls._departure(c, npc, row):
            return None
        date = {**dict(row), "starts_at": row["arrival_starts_at"], "ends_at": row["latest_end_at"]}
        destination = c.execute(
            "SELECT * FROM locations WHERE id=?", (row["location_id"],)
        ).fetchone()
        return AppointmentRoomService.approach(c, npc, date, at, destination)

    @classmethod
    def boundary(cls, c, wid, at, result, satiety_rate=3):
        for row in c.execute(
            "SELECT * FROM journey_overnight_stays WHERE world_id=? AND status IN ('pending','accepted','active')",
            (wid,),
        ):
            fields = (
                ("arrival_starts_at",)
                if row["status"] == "pending"
                else ("arrival_starts_at", "arrival_ends_at")
                if row["status"] == "accepted"
                else ("ends_at",)
            )
            for key in fields:
                if row[key] and at < from_iso(row[key]) < result:
                    result = from_iso(row[key])
            if row["status"] == "accepted":
                for person in cls._people(c, row):
                    depart = cls._departure(c, person, row)
                    if at < depart < result:
                        result = depart
                for due in c.execute(
                    "SELECT next_world_time FROM npc_daily_states WHERE character_id IN (?,?)",
                    (row["proposer_id"], row["recipient_id"]),
                ):
                    if due[0] and at < from_iso(due[0]) < result:
                        result = from_iso(due[0])
                if at >= from_iso(row["arrival_ends_at"]):
                    result = min(result, at + timedelta(seconds=1))
            if row["status"] == "active":
                result = min(result, at + timedelta(minutes=15))
                for person in cls._people(c, row):
                    residual = c.execute(
                        "SELECT satiety_residual FROM character_state_accumulators WHERE character_id=?",
                        (person["id"],),
                    ).fetchone()
                    seconds = (
                        (person["satiety"] - 5 - (float(residual[0]) if residual else 0))
                        / satiety_rate
                        * 3600
                        if satiety_rate > 0
                        else 0
                    )
                    if seconds > 0:
                        result = min(result, at + timedelta(seconds=max(0.000001, seconds)))
                    if person["satiety"] <= 5:
                        result = min(result, at + timedelta(seconds=1))
        from world_engine.separate_stays import SeparateStayService
        from world_engine.lodging_plans import LodgingPlanService
        result = SeparateStayService.boundary(c, wid, at, result, satiety_rate)
        return LodgingPlanService.boundary(c, wid, at, result)
