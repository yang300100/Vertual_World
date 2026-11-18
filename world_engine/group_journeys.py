"""哥哥，有限多人旅行逐人同意、逐人走路，所有授权均留真实来源。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from itertools import combinations
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.actions import ActionService
from world_engine.geo import great_circle_distance_km
from world_engine.intimacy import IntimacyService
from world_engine.journeys import JourneyService, JourneyStop
from world_engine.life import LifeActivityService
from world_engine.movement import MovementService
from world_engine.repository import from_iso, to_iso

GROUP_JOURNEY_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_journeys (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 proposer_id TEXT NOT NULL REFERENCES characters(id),leader_id TEXT NOT NULL REFERENCES characters(id),
 player_id TEXT NOT NULL REFERENCES characters(id),
 status TEXT NOT NULL CHECK(status IN ('pending','accepted','active','paused','resume_pending','completed','ended','expired')),
 phase TEXT NOT NULL DEFAULT 'ready' CHECK(phase IN ('ready','moving','stopped')),
 title TEXT NOT NULL,statement TEXT NOT NULL,origin_id TEXT NOT NULL REFERENCES locations(id),
 deadline_at TEXT NOT NULL,proposed_at TEXT NOT NULL,started_at TEXT,completed_at TEXT,
 separation_policy TEXT NOT NULL CHECK(separation_policy IN ('pause','continue')),
 return_to_origin INTEGER NOT NULL CHECK(return_to_origin IN (0,1)),
 next_index INTEGER NOT NULL DEFAULT 0,next_depart_at TEXT,remaining_stop_seconds REAL NOT NULL DEFAULT 0,
 resume_epoch INTEGER NOT NULL DEFAULT 0,terms_json TEXT NOT NULL,
 proposal_event_id TEXT NOT NULL REFERENCES world_events(id),
 follow_authorized_at TEXT,follow_source_event_id TEXT REFERENCES world_events(id),follow_terms_json TEXT,
 source_event_id TEXT NOT NULL REFERENCES world_events(id),reason TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_group_journeys_live ON group_journeys(world_id,status,deadline_at);
CREATE TABLE IF NOT EXISTS group_journey_members (
 journey_id TEXT NOT NULL REFERENCES group_journeys(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id),ordinal INTEGER NOT NULL,
 decision TEXT NOT NULL DEFAULT 'pending' CHECK(decision IN ('pending','accepted','declined')),
 decision_at TEXT,decision_event_id TEXT REFERENCES world_events(id),
 left_at TEXT,left_event_id TEXT REFERENCES world_events(id),
 meeting_movement_id TEXT REFERENCES character_movements(id),
 resume_epoch INTEGER NOT NULL DEFAULT 0,resume_event_id TEXT REFERENCES world_events(id),
 PRIMARY KEY(journey_id,character_id),UNIQUE(journey_id,ordinal)
);
CREATE INDEX IF NOT EXISTS idx_group_member_character ON group_journey_members(character_id,journey_id);
CREATE TABLE IF NOT EXISTS group_journey_stops (
 journey_id TEXT NOT NULL REFERENCES group_journeys(id) ON DELETE CASCADE,stop_index INTEGER NOT NULL,
 location_id TEXT NOT NULL REFERENCES locations(id),minutes INTEGER NOT NULL CHECK(minutes BETWEEN 0 AND 120),
 is_return INTEGER NOT NULL DEFAULT 0 CHECK(is_return IN (0,1)),arrived_at TEXT,
 source_event_id TEXT REFERENCES world_events(id),PRIMARY KEY(journey_id,stop_index)
);
CREATE TABLE IF NOT EXISTS group_journey_legs (
 id TEXT PRIMARY KEY,journey_id TEXT NOT NULL REFERENCES group_journeys(id) ON DELETE CASCADE,
 stop_index INTEGER NOT NULL,status TEXT NOT NULL CHECK(status IN ('moving','arrived','cancelled')),
 started_at TEXT NOT NULL,finished_at TEXT,source_event_id TEXT NOT NULL REFERENCES world_events(id),
 arrival_event_id TEXT REFERENCES world_events(id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_group_live_leg ON group_journey_legs(journey_id) WHERE status='moving';
CREATE TABLE IF NOT EXISTS group_journey_leg_members (
 leg_id TEXT NOT NULL REFERENCES group_journey_legs(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id),
 movement_id TEXT NOT NULL UNIQUE REFERENCES character_movements(id),
 source_event_id TEXT NOT NULL REFERENCES world_events(id),PRIMARY KEY(leg_id,character_id)
);
"""


class GroupJourneyIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: Literal[
        "propose", "accept", "decline", "follow", "withdraw_follow", "depart", "pause",
        "resume", "leave", "end",
    ]
    journey_id: str | None = Field(default=None, min_length=1, max_length=100)
    member_ids: list[str] = Field(default_factory=list, max_length=8)
    leader_id: str | None = Field(default=None, min_length=1, max_length=100)
    title: str = Field(default="有限多人旅行", min_length=1, max_length=60)
    meeting_location_id: str | None = Field(default=None, min_length=1, max_length=100)
    deadline_world_time: str | None = None
    stops: list[JourneyStop] = Field(default_factory=list, max_length=6)
    return_to_origin: bool | None = Field(default=None, strict=True)
    separation_policy: Literal["pause", "continue"] | None = None
    statement: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def coherent(self):
        if self.operation == "propose":
            if (self.journey_id or not 2 <= len(self.member_ids) <= 8
                    or len(set(self.member_ids)) != len(self.member_ids)
                    or any(not cid or len(cid) > 100 for cid in self.member_ids)
                    or not self.leader_id or not self.meeting_location_id
                    or not self.deadline_world_time or not self.stops
                    or self.return_to_origin is None):
                raise ValueError("请明确2至8人完整名单、领队、集合地、期限、1至6站及是否返程")
        elif (not self.journey_id or self.member_ids or self.leader_id or self.stops
                or self.meeting_location_id or self.deadline_world_time
                or self.return_to_origin is not None or self.separation_policy is not None
                or self.title != "有限多人旅行"):
            raise ValueError("回应只引用原行程，不得悄悄修改完整名单、领队、路线及退出条款")
        return self


class GroupJourneyService:
    LIVE = ("pending", "accepted", "active", "paused", "resume_pending")

    @staticmethod
    def has_live(c, wid):
        return bool(c.execute(
            "SELECT 1 FROM group_journeys WHERE world_id=? AND status IN "
            "('pending','accepted','active','paused','resume_pending') LIMIT 1", (wid,),
        ).fetchone())

    @classmethod
    def assert_available(cls, c, person):
        from world_engine.rides import RideService
        RideService.assert_available(c, person)
        if cls.active(c, person["world_id"], person["id"]):
            raise ValueError("本人已有未结束的多人有限旅行，请先处理原约定")

    @classmethod
    def assert_interval_free(cls, c, person, start, end):
        row = cls.active(c, person["world_id"], person["id"])
        if row and start < from_iso(row["deadline_at"]) and end > from_iso(row["proposed_at"]):
            raise ValueError("这段时间与本人已接受的多人有限旅行冲突，请先处理原约定")

    @staticmethod
    def install(c):
        # 哥哥，用逐条 DDL 保留调用方事务；executescript 会隐式提交旧事务。
        for statement in GROUP_JOURNEY_SCHEMA.split(";"):
            if statement.strip():
                c.execute(statement)

    @staticmethod
    def _json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _row(c, jid):
        return c.execute("SELECT * FROM group_journeys WHERE id=?", (jid,)).fetchone()

    @staticmethod
    def _person(c, wid, cid):
        person = c.execute("SELECT * FROM characters WHERE id=? AND world_id=?", (cid, wid)).fetchone()
        if person is None:
            raise ValueError("人物不属于本世界")
        return person

    @staticmethod
    def _members(c, row, *, remaining=True):
        return c.execute(
            "SELECT m.*,p.is_player FROM group_journey_members m JOIN characters p "
            "ON p.id=m.character_id AND p.world_id=? WHERE m.journey_id=?"
            + (" AND m.left_at IS NULL" if remaining else "") + " ORDER BY m.ordinal",
            (row["world_id"], row["id"]),
        ).fetchall()

    @classmethod
    def _people(cls, c, row):
        return [cls._person(c, row["world_id"], m["character_id"]) for m in cls._members(c, row)]

    @classmethod
    def active(cls, c, wid, cid):
        return c.execute(
            "SELECT g.* FROM group_journeys g JOIN group_journey_members m ON m.journey_id=g.id "
            "WHERE g.world_id=? AND m.character_id=? AND m.left_at IS NULL "
            "AND g.status IN ('accepted','active','paused','resume_pending') ORDER BY g.rowid LIMIT 1",
            (wid, cid),
        ).fetchone()

    @classmethod
    def terms(cls, c, row):
        return {
            "journey_id": row["id"], "world_id": row["world_id"],
            "proposer_id": row["proposer_id"], "leader_id": row["leader_id"],
            "player_id": row["player_id"], "title": row["title"], "statement": row["statement"],
            "origin_id": row["origin_id"], "deadline_at": row["deadline_at"],
            "return_to_origin": bool(row["return_to_origin"]),
            "separation_policy": row["separation_policy"],
            "end_policy": "提议者可结束整份有限约定，每人均可单独退出，不擅自换领队或路线",
            "member_ids": [m["character_id"] for m in cls._members(c, row, remaining=False)],
            "stops": [dict(s) for s in c.execute(
                "SELECT stop_index,location_id,minutes,is_return FROM group_journey_stops "
                "WHERE journey_id=? ORDER BY stop_index", (row["id"],),
            )],
        }

    @classmethod
    def _event(cls, c, row, actor, at, operation, summary, payload=None, *, audience=None):
        data = {"journey_id": row["id"], **(payload or {})}
        event = ActionService._record_event(
            c, world_id=row["world_id"], tick_id=str(uuid4()), occurred_at=at,
            event_type="social.group_journey_" + operation, actor_id=actor["id"],
            target_id=None,
            location_id=None, summary=summary, payload=data,
        )
        ids = audience if audience is not None else [
            m["character_id"] for m in cls._members(c, row)
        ]
        for cid in dict.fromkeys(ids):
            c.execute("INSERT OR IGNORE INTO event_observers(event_id,character_id,channel) VALUES (?,?,'participant')", (event, cid))
            c.execute(
                "INSERT INTO character_event_knowledge(character_id,event_id,world_id,source_kind,confidence,hops,learned_world_time) "
                "VALUES (?,?,?,'experienced',1,0,?) ON CONFLICT(character_id,event_id) DO UPDATE "
                "SET source_kind='experienced',source_character_id=NULL,confidence=1,hops=0,learned_world_time=excluded.learned_world_time",
                (cid, event, row["world_id"], to_iso(at)),
            )
            ActionService._record_memory(
                c, world_id=row["world_id"], character_id=cid, event_id=event,
                memory_type="experienced", summary=summary, importance=6,
            )
        return event

    @classmethod
    def _source(cls, c, row, event_id, operation, actor_id, at, *, terms=False, epoch=False):
        event = c.execute("SELECT * FROM world_events WHERE id=? AND world_id=?", (event_id, row["world_id"])).fetchone()
        try:
            payload = json.loads(event["payload_json"]) if event else {}
            valid = bool(
                event and event["event_type"] == "social.group_journey_" + operation
                and event["actor_id"] == actor_id
                and event["target_id"] is None
                and from_iso(row["proposed_at"]) <= from_iso(event["occurred_at"]) <= at
                and from_iso(event["occurred_at"]) < from_iso(row["deadline_at"])
                and payload.get("journey_id") == row["id"]
                and (not terms or payload.get("terms") == cls.terms(c, row))
                and (not epoch or payload.get("resume_epoch") == row["resume_epoch"])
                and c.execute(
                    "SELECT 1 FROM character_event_knowledge WHERE character_id=? AND event_id=? "
                    "AND world_id=? AND source_kind='experienced' AND learned_world_time<=?",
                    (actor_id, event_id, row["world_id"], to_iso(at)),
                ).fetchone()
            )
            return valid
        except (ValueError, TypeError, KeyError):
            return False

    @classmethod
    def _terms_valid(cls, c, row, at):
        try:
            return (json.loads(row["terms_json"]) == cls.terms(c, row)
                    and cls._source(c, row, row["proposal_event_id"], "proposed", row["proposer_id"], at, terms=True))
        except (ValueError, TypeError):
            return False

    @classmethod
    def _accepted(cls, c, row, at):
        members = cls._members(c, row, remaining=False)
        if not cls._terms_valid(c, row, at) or len(members) < 2:
            return False
        return all(
            m["decision"] == "accepted" and m["decision_at"]
            and cls._source(c, row, m["decision_event_id"], "proposed" if m["character_id"] == row["proposer_id"] else "accepted", m["character_id"], at, terms=True)
            and c.execute("SELECT occurred_at FROM world_events WHERE id=?", (m["decision_event_id"],)).fetchone()[0] == m["decision_at"]
            for m in members
        )

    @classmethod
    def _resumed(cls, c, row, at):
        return row["resume_epoch"] == 0 or all(
            m["resume_epoch"] == row["resume_epoch"]
            and cls._source(c, row, m["resume_event_id"], "resumed", m["character_id"], at, terms=True, epoch=True)
            for m in cls._members(c, row)
        )

    @classmethod
    def _follow_required(cls, c, row):
        return row["leader_id"] != row["player_id"] and any(
            m["character_id"] == row["player_id"] for m in cls._members(c, row)
        )

    @classmethod
    def following(cls, c, row, at):
        if not cls._follow_required(c, row) or row["status"] not in {"accepted", "active"} or at >= from_iso(row["deadline_at"]):
            return False
        try:
            return bool(
                cls._accepted(c, row, at) and cls._resumed(c, row, at)
                and json.loads(row["follow_terms_json"]) == cls.terms(c, row)
                and cls._source(c, row, row["follow_source_event_id"], "follow", row["player_id"], at, terms=True, epoch=True)
                and c.execute("SELECT occurred_at FROM world_events WHERE id=?", (row["follow_source_event_id"],)).fetchone()[0] == row["follow_authorized_at"]
            )
        except (ValueError, TypeError):
            return False

    @staticmethod
    def _clear_follow(c, jid):
        c.execute("UPDATE group_journeys SET follow_authorized_at=NULL,follow_source_event_id=NULL,follow_terms_json=NULL WHERE id=?", (jid,))

    @classmethod
    def _knows(cls, c, person, lids, at):
        for lid in lids:
            place = JourneyService._place(c, person["world_id"], lid)
            if place["space_layer"] != person["space_layer"] or (
                lid != (person["current_location_id"] or person["location_id"])
                and not c.execute(
                    "SELECT 1 FROM world_events e JOIN event_observers o ON o.event_id=e.id "
                    "JOIN character_event_knowledge k ON k.event_id=e.id AND k.character_id=o.character_id "
                    "WHERE e.world_id=? AND k.world_id=e.world_id AND e.location_id=? AND o.character_id=? "
                    "AND o.channel='participant' AND e.occurred_at<=? AND k.learned_world_time<=? LIMIT 1",
                    (person["world_id"], lid, person["id"], to_iso(at), to_iso(at)),
                ).fetchone()
            ):
                raise ValueError("本人不能确认这份完整路线，请拒绝或另提可带路的行程")

    @classmethod
    def _leader_knows(cls, c, row, at):
        leader = cls._person(c, row["world_id"], row["leader_id"])
        if not leader["is_player"]:
            cls._knows(c, leader, [row["origin_id"]] + [s[0] for s in c.execute(
                "SELECT location_id FROM group_journey_stops WHERE journey_id=? ORDER BY stop_index", (row["id"],),
            )], at)

    @classmethod
    def _conflict(cls, c, person, row, at):
        from world_engine.rides import RideService
        RideService.assert_available(c, person)
        cid, wid = person["id"], person["world_id"]
        if c.execute("SELECT 1 FROM companion_sessions WHERE world_id=? AND status='active' AND (leader_id=? OR companion_id=?)", (wid, cid, cid)).fetchone():
            raise ValueError("本人已有短时同行，请先处理原约定")
        if c.execute("SELECT 1 FROM joint_journeys WHERE world_id=? AND status IN ('accepted','active','paused','resume_pending') AND (proposer_id=? OR recipient_id=?)", (wid, cid, cid)).fetchone():
            raise ValueError("本人已有双人有限旅行，请先处理原约定")
        for table in ("shared_preparation_plans", "shared_life_moments"):
            if c.execute(
                f"SELECT 1 FROM {table} WHERE world_id=? AND status='active' "
                "AND (proposer_id=? OR recipient_id=?)", (wid, cid, cid),
            ).fetchone():
                raise ValueError("本人已有正在执行的共同活动或筹备，请先处理原约定")
        if c.execute(
            "SELECT 1 FROM group_journeys g JOIN group_journey_members m ON m.journey_id=g.id "
            "WHERE g.world_id=? AND g.id<>? AND m.character_id=? AND m.left_at IS NULL "
            "AND g.status IN ('accepted','active','paused','resume_pending') LIMIT 1", (wid, row["id"], cid),
        ).fetchone():
            raise ValueError("本人已有另一份多人有限旅行，请先处理原约定")

    @classmethod
    def _estimate(cls, c, people, stops, origins):
        if not people:
            raise ValueError("没有仍参加的成员")
        cap = min(float(p["movement_speed_kmh"]) for p in people)
        if cap <= 0:
            raise ValueError("当前没有可用徒步速度")
        total = 0
        for stop in stops:
            place = JourneyService._place(c, people[0]["world_id"], stop["location_id"])
            durations = []
            for person, origin in zip(people, origins, strict=True):
                if person["space_layer"] != place["space_layer"]:
                    raise ValueError("本份有限旅行需要同层各自徒步")
                shadow = {**dict(person), "longitude": origin[0], "latitude": origin[1], "movement_speed_kmh": cap}
                route = MovementService()._plan_route(
                    c, world_id=person["world_id"], actor=shadow,
                    destination_longitude=place["longitude"], destination_latitude=place["latitude"],
                    movement_type="land", speed_kmh=cap, selected_vehicle_id=None,
                )
                durations.append(route.estimated_hours * 3600 if route else great_circle_distance_km(
                    *origin, place["longitude"], place["latitude"],
                ) / cap * 3600)
            total += max(durations) + stop["minutes"] * 60
            origins = [(place["longitude"], place["latitude"])] * len(people)
        return total

    @classmethod
    def _commitments(cls, c, person, at, duration, final):
        from world_engine.appointment_rooms import AppointmentRoomService
        from world_engine.schedules import ScheduleService
        from world_engine.separate_stays import SeparateStayService

        plan = ScheduleService.assess(c, person, at)
        if plan and plan["depart_at"] <= at + timedelta(seconds=duration):
            raise ValueError("本人的既有赴约安排与剩余旅行冲突")
        if SeparateStayService.new_journey_conflict(c, person, at, at + timedelta(seconds=duration), final["id"]):
            raise ValueError("本人的既有休息及会合安排与剩余旅行冲突")
        for table, deadline in (("social_appointments", "starts_at"), ("journey_overnight_stays", "arrival_starts_at")):
            for date in c.execute(
                f"SELECT * FROM {table} WHERE world_id=? AND status IN ('accepted','active') AND (proposer_id=? OR recipient_id=?)",
                (person["world_id"], person["id"], person["id"]),
            ):
                onward = cls._estimate(c, [person], [{"location_id": date["location_id"], "minutes": 0}], [(final["longitude"], final["latitude"])])
                extra = 0
                if table == "journey_overnight_stays":
                    extra = 60 * (10 + AppointmentRoomService.transition_minutes(c, person["world_id"], None, date["room_id"]))
                if at + timedelta(seconds=duration + onward + extra) >= from_iso(date[deadline]):
                    raise ValueError("本人的既有承诺无法在本次有限旅行后及时履行")

    @classmethod
    def _ready(cls, c, row, at):
        from world_engine.daily_life import DailyLifeService
        from world_engine.families import FamilyService

        people = cls._people(c, row)
        if len(people) < 2 or row["leader_id"] not in {p["id"] for p in people}:
            raise ValueError("原领队已退出或人数不足，请结束旧约另提")
        cls._leader_knows(c, row, at)
        for a, b in combinations(people, 2):
            if IntimacyService.paused(c, row["world_id"], a["id"], b["id"]):
                raise ValueError("一方要求留出空间，需要另行商量")
            if a["current_room_id"] != b["current_room_id"] or great_circle_distance_km(
                a["longitude"], a["latitude"], b["longitude"], b["latitude"],
            ) > 0.02:
                raise ValueError("所有仍参加者需要先实际会合到同一室外场景20米内")
        for person in people:
            cls._conflict(c, person, row, at)
            if (person["current_room_id"] or person["current_fixture_id"] or person["active_vehicle_id"]
                    or person["movement_type"] != "land"):
                raise ValueError("请本人先起身离开室内并选择各自徒步")
            if person["health"] < 40 or person["energy"] <= 20 or person["satiety"] <= 30:
                raise ValueError("当前有成员不能出发，请各自处理已有需要")
            LifeActivityService.assert_available(c, person["id"])
            if c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (person["id"],)).fetchone():
                raise ValueError("请先结束本人当前移动")
            if c.execute("SELECT 1 FROM player_steering WHERE character_id=? AND (dx<>0 OR dy<>0)", (person["id"],)).fetchone():
                raise ValueError("请先停止本人方向移动")
            if not person["is_player"] and (DailyLifeService.is_sleep_time(person, at) or FamilyService.hungry_dependent(c, person, at)):
                raise ValueError("当前有成员不能出发，请各自处理已有安排")
        if row["started_at"] is None:
            site = JourneyService._place(c, row["world_id"], row["origin_id"])
        elif row["phase"] == "stopped":
            previous = c.execute("SELECT location_id FROM group_journey_stops WHERE journey_id=? AND stop_index=?", (row["id"], row["next_index"] - 1)).fetchone()
            site = JourneyService._place(c, row["world_id"], previous[0])
        else:
            site = None
        if site and any(great_circle_distance_km(p["longitude"], p["latitude"], site["longitude"], site["latitude"]) > 0.025 for p in people):
            raise ValueError("请所有仍参加者先实际回到约定集合或停留地点")
        stops = c.execute("SELECT * FROM group_journey_stops WHERE journey_id=? AND stop_index>=? ORDER BY stop_index", (row["id"], row["next_index"])).fetchall()
        seconds = cls._estimate(c, people, stops, [(p["longitude"], p["latitude"]) for p in people]) if stops else 0
        seconds += row["remaining_stop_seconds"] if row["status"] in {"paused", "resume_pending"} else max(0, (from_iso(row["next_depart_at"]) - at).total_seconds()) if row["next_depart_at"] else 0
        if at + timedelta(seconds=seconds) > from_iso(row["deadline_at"]):
            raise ValueError("剩余真实路程和停留已无法在原期限内完成")
        final_id = c.execute("SELECT location_id FROM group_journey_stops WHERE journey_id=? ORDER BY stop_index DESC LIMIT 1", (row["id"],)).fetchone()[0]
        final = JourneyService._place(c, row["world_id"], final_id)
        for person in people:
            cls._commitments(c, person, at, seconds, final)
        return people

    @classmethod
    def apply(cls, c, actor, at, choice):
        if not isinstance(choice, GroupJourneyIntent):
            choice = GroupJourneyIntent.model_validate(choice)
        name = "group_choice_" + uuid4().hex
        c.execute("SAVEPOINT " + name)
        try:
            return cls._apply(c, actor, at, choice)
        except Exception:
            c.execute("ROLLBACK TO " + name)
            raise
        finally:
            c.execute("RELEASE " + name)

    @classmethod
    def _apply(cls, c, actor, at, choice):
        actor = cls._person(c, actor["world_id"], actor["id"])
        if at.tzinfo is None:
            raise ValueError("旅行选择需要带时区的真实世界时间")
        if choice.operation == "propose":
            return cls._propose(c, actor, at, choice)
        row = cls._row(c, choice.journey_id)
        if not row or row["world_id"] != actor["world_id"]:
            raise ValueError("行程不属于本世界")
        member = c.execute("SELECT * FROM group_journey_members WHERE journey_id=? AND character_id=?", (row["id"], actor["id"])).fetchone()
        if not member:
            raise ValueError("只有原名单中的本人可以表达选择")
        cls.maintain(c, row["world_id"], at)
        row = cls._row(c, row["id"])
        if row["status"] not in cls.LIVE:
            raise ValueError("原行程已经结束或到期，不能再授予同意")
        if choice.operation == "end":
            if actor["id"] != row["proposer_id"]:
                raise ValueError("只有提议者可结束整份原约定；其他成员可本人 leave")
            return cls._finish(c, row, actor, at, "ended", "提议者明确结束整份有限约定。" + choice.statement)
        if member["left_at"]:
            raise ValueError("本人已退出原约定，不能被恢复或重新带走")
        if choice.operation == "leave":
            return cls._leave(c, row, actor, at, choice.statement)
        if choice.operation in {"pause", "withdraw_follow"}:
            if choice.operation == "withdraw_follow" and (actor["id"] != row["player_id"] or not row["follow_source_event_id"]):
                raise ValueError("只有玩家本人可撤回当前跟随")
            if row["status"] == "pending":
                raise ValueError("待回应提议尚无可暂停的执行")
            return cls.pause(c, row, at, "本人要求暂停有限旅行。" + choice.statement, actor=actor)
        if not cls._terms_valid(c, row, at):
            raise ValueError("原条款或提议来源已变化，请结束旧约另提")
        if actor["health"] <= 0:
            raise ValueError("本人当前无法表达有效选择")
        proposer = cls._person(c, row["world_id"], row["proposer_id"])
        if choice.operation in {"accept", "decline", "resume"}:
            # 哥哥，恢复只与原领队沟通；已退出的提议者不能成为留下者恢复的隐含门槛。
            recipient = cls._person(c, row["world_id"], row["leader_id"]) if choice.operation == "resume" else proposer
            if actor["id"] != recipient["id"]:
                IntimacyService._reachable(c, actor, recipient)
        if choice.operation in {"accept", "decline"}:
            if row["status"] != "pending" or member["decision"] != "pending":
                if member["decision"] == ("accepted" if choice.operation == "accept" else "declined"):
                    op = ("proposed" if actor["id"] == row["proposer_id"] else "accepted") if choice.operation == "accept" else "declined"
                    if cls._source(c, row, member["decision_event_id"], op, actor["id"], at, terms=True):
                        return cls._result(row, member["decision_event_id"], "本人已留下这份有效答复，未代其他成员选择。")
                raise ValueError("本人只能回应尚未答复的原提议；变更须结束旧约另提")
            if choice.operation == "accept":
                cls._conflict(c, actor, row, at)
                for other in cls._people(c, row):
                    if other["id"] != actor["id"] and IntimacyService.paused(c, row["world_id"], actor["id"], other["id"]):
                        raise ValueError("一方要求留出空间，不能接受共同旅行")
                if actor["id"] == row["leader_id"]:
                    cls._leader_knows(c, row, at)
            decision = "accepted" if choice.operation == "accept" else "declined"
            event = cls._event(c, row, actor, at, decision, "一名成员本人明确" + ("接受" if decision == "accepted" else "婉拒") + "原完整行程，其他人的选择分别保留。" + choice.statement, {"terms": cls.terms(c, row)})
            c.execute("UPDATE group_journey_members SET decision=?,decision_at=?,decision_event_id=? WHERE journey_id=? AND character_id=?", (decision, to_iso(at), event, row["id"], actor["id"]))
            status = "accepted" if cls._accepted(c, row, at) else "pending"
            if status == "accepted":
                for person in cls._people(c, row):
                    cls._conflict(c, person, row, at)
                    if not person["is_player"]:
                        c.execute("INSERT INTO npc_daily_states(character_id,world_id,next_world_time) VALUES (?,?,?) ON CONFLICT(character_id) DO UPDATE SET next_world_time=excluded.next_world_time", (person["id"], row["world_id"], to_iso(at)))
            c.execute("UPDATE group_journeys SET status=?,source_event_id=? WHERE id=?", (status, event, row["id"]))
            return cls._result(cls._row(c, row["id"]), event, "仅本人答复已记录；全体真实接受之前不能出发。")
        if choice.operation == "resume":
            if row["status"] not in {"paused", "resume_pending"}:
                raise ValueError("请先暂停再分别提出恢复")
            if not cls._accepted(c, row, at):
                raise ValueError("原逐人接受来源不完整，不能恢复")
            if member["resume_epoch"] == row["resume_epoch"] and cls._source(c, row, member["resume_event_id"], "resumed", actor["id"], at, terms=True, epoch=True):
                return cls._result(row, member["resume_event_id"], "本人已明确恢复，仍等待余者分别决定。")
            if row["leader_id"] not in {m["character_id"] for m in cls._members(c, row)}:
                raise ValueError("原领队已退出，不得擅自换领队；请结束旧约另提")
            event = cls._event(c, row, actor, at, "resumed", "一名成员本人明确同意恢复原有限路线，仍须其他留下者各自同意。" + choice.statement, {"terms": cls.terms(c, row), "resume_epoch": row["resume_epoch"]})
            c.execute("UPDATE group_journey_members SET resume_epoch=?,resume_event_id=? WHERE journey_id=? AND character_id=?", (row["resume_epoch"], event, row["id"], actor["id"]))
            if cls._resumed(c, row, at):
                cls._ready(c, row, at)
                status = "active" if row["started_at"] else "accepted"
                depart = to_iso(at + timedelta(seconds=row["remaining_stop_seconds"])) if row["phase"] == "stopped" else None
                c.execute("UPDATE group_journeys SET status=?,next_depart_at=?,source_event_id=?,reason='' WHERE id=?", (status, depart, event, row["id"]))
            else:
                c.execute("UPDATE group_journeys SET status='resume_pending',source_event_id=? WHERE id=?", (event, row["id"]))
            return cls._result(cls._row(c, row["id"]), event, "本人恢复已记录；NPC 带路仍需玩家重新选择跟随。")
        if not cls._accepted(c, row, at) or not cls._resumed(c, row, at):
            raise ValueError("必须全体有效接受或留下者分别恢复，不能替任何成员决定")
        if choice.operation == "follow":
            if actor["id"] != row["player_id"] or not cls._follow_required(c, row):
                raise ValueError("只有仍参加的玩家本人可以选择跟随已约定的 NPC 领队")
            if row["status"] not in {"accepted", "active"} or row["phase"] == "moving":
                raise ValueError("请先共同确认或恢复，并实际会合再跟随")
            if cls.following(c, row, at):
                return cls._result(row, row["follow_source_event_id"], "本人已有这份有限授权，未重复移动。")
            cls._ready(c, row, at)
            terms = cls.terms(c, row)
            event = cls._event(c, row, actor, at, "follow", "玩家本人明确选择跟随已共同接受的有限行程；暂停后需重新选择。" + choice.statement, {"terms": terms, "resume_epoch": row["resume_epoch"]})
            c.execute("UPDATE group_journeys SET follow_authorized_at=?,follow_source_event_id=?,follow_terms_json=?,source_event_id=? WHERE id=?", (to_iso(at), event, cls._json(terms), event, row["id"]))
            c.execute("UPDATE npc_daily_states SET next_world_time=? WHERE character_id=?", (to_iso(at), row["leader_id"]))
            return cls._result(cls._row(c, row["id"]), event, "有限跟随已授权，尚未移动。")
        if choice.operation == "depart":
            if actor["id"] != row["leader_id"]:
                raise ValueError("只有原领队本人可开始本程")
            if cls._follow_required(c, row) and not cls.following(c, row, at):
                raise ValueError("尚无玩家本人对原完整条款的有效跟随授权")
            return cls._depart(c, row, actor, at)
        raise ValueError("这项选择不适用于当前行程")

    @classmethod
    def _propose(cls, c, actor, at, choice):
        wid = actor["world_id"]
        if actor["id"] not in choice.member_ids or choice.leader_id not in choice.member_ids:
            raise ValueError("提议者和领队本人必须在完整名单中")
        people = [cls._person(c, wid, cid) for cid in choice.member_ids]
        players = c.execute("SELECT id FROM characters WHERE world_id=? AND is_player=1", (wid,)).fetchall()
        if len(players) != 1 or sum(bool(p["is_player"]) for p in people) != 1:
            raise ValueError("本世界和完整名单均须有且只有一位玩家")
        if any(p["health"] <= 0 for p in people):
            raise ValueError("当前有成员无法参与旅行")
        for other in people:
            if other["id"] != actor["id"]:
                IntimacyService._reachable(c, actor, other)
                if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                    raise ValueError("一方要求留出空间，暂不提出共同旅行")
        try:
            end = datetime.fromisoformat(choice.deadline_world_time)
        except (ValueError, TypeError) as exc:
            raise ValueError("请明确带时区的期限") from exc
        if end.tzinfo is None or not at + timedelta(minutes=30) <= end <= at + timedelta(days=7):
            raise ValueError("期限须在半小时至七个世界日内且带时区")
        stops = [s.model_dump() | {"is_return": 0} for s in choice.stops]
        if choice.return_to_origin and stops[-1]["location_id"] != choice.meeting_location_id:
            stops.append({"location_id": choice.meeting_location_id, "minutes": 0, "is_return": 1})
        if choice.return_to_origin:
            stops[-1]["is_return"] = 1
        lids = [choice.meeting_location_id] + [s["location_id"] for s in stops]
        # 哥哥，只核验提议者本人的见闻；不在提议入口探测 NPC 领队的私下到访史。
        cls._knows(c, actor, lids, at)
        for a, b in zip(lids, lids[1:], strict=False):
            first, second = JourneyService._place(c, wid, a), JourneyService._place(c, wid, b)
            if a == b or great_circle_distance_km(first["longitude"], first["latitude"], second["longitude"], second["latitude"]) < 0.05:
                raise ValueError("相邻站点需要真实且足够的路程")
        origin = JourneyService._place(c, wid, choice.meeting_location_id)
        duration = cls._estimate(c, people, stops, [(origin["longitude"], origin["latitude"])] * len(people))
        if at + timedelta(seconds=duration) > end:
            raise ValueError("真实路线与停留已超过期限")
        jid = str(uuid4())
        seed = {"id": jid, "world_id": wid, "proposer_id": actor["id"]}
        cls._conflict(c, actor, seed, at)
        event = cls._event(c, seed, actor, at, "proposed", "本人提出完整有限多人行程，等待名单中每人分别回应。" + choice.statement, audience=choice.member_ids)
        c.execute(
            "INSERT INTO group_journeys(id,world_id,proposer_id,leader_id,player_id,status,title,statement,origin_id,deadline_at,proposed_at,separation_policy,return_to_origin,terms_json,proposal_event_id,source_event_id) "
            "VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?,?,'{}',?,?)",
            (jid, wid, actor["id"], choice.leader_id, players[0][0], choice.title, choice.statement, origin["id"], to_iso(end), to_iso(at), choice.separation_policy or "pause", int(choice.return_to_origin), event, event),
        )
        for ordinal, person in enumerate(people):
            c.execute("INSERT INTO group_journey_members(journey_id,character_id,ordinal) VALUES (?,?,?)", (jid, person["id"], ordinal))
        for index, stop in enumerate(stops):
            c.execute("INSERT INTO group_journey_stops(journey_id,stop_index,location_id,minutes,is_return) VALUES (?,?,?,?,?)", (jid, index, stop["location_id"], stop["minutes"], stop["is_return"]))
        row = cls._row(c, jid)
        terms = cls.terms(c, row)
        c.execute("UPDATE group_journeys SET terms_json=? WHERE id=?", (cls._json(terms), jid))
        c.execute("UPDATE world_events SET payload_json=? WHERE id=?", (cls._json({"journey_id": jid, "terms": terms}), event))
        c.execute("UPDATE group_journey_members SET decision='accepted',decision_at=?,decision_event_id=? WHERE journey_id=? AND character_id=?", (to_iso(at), event, jid, actor["id"]))
        # 哥哥，提议者只能用本人明确提议作为本人的承诺，不代任何受邀者接受。
        if actor["id"] == choice.leader_id and not actor["is_player"]:
            cls._leader_knows(c, cls._row(c, jid), at)
        return cls._result(row, event, "原完整名单与条款已冻结；提议是本人承诺，其他成员仍须各自接受。")

    @staticmethod
    def _result(row, event, summary):
        return {"status": row["status"], "journey_id": row["id"], "event_id": event, "summary": summary}

    @classmethod
    def _depart(cls, c, row, actor, at):
        if row["status"] not in {"accepted", "active"} or row["phase"] == "moving":
            raise ValueError("请先确认或恢复行程，不能重复出发")
        if row["next_depart_at"] and at < from_iso(row["next_depart_at"]):
            raise ValueError("约定停留尚未真实完成")
        people = cls._ready(c, row, at)
        target = c.execute("SELECT s.*,l.longitude,l.latitude FROM group_journey_stops s JOIN locations l ON l.id=s.location_id WHERE s.journey_id=? AND s.stop_index=?", (row["id"], row["next_index"])).fetchone()
        if not target:
            raise ValueError("已走完路线，请实际完成最后停留")
        if all(great_circle_distance_km(p["longitude"], p["latitude"], target["longitude"], target["latitude"]) <= 0.001 for p in people):
            cls._mark_stop(c, row, at, None)
            latest = cls._row(c, row["id"])
            return cls._result(latest, latest["source_event_id"], "已实际在站点，保留已走路程并开始停留。")
        cap = min(float(p["movement_speed_kmh"]) for p in people)
        movements = [MovementService()._start_solo(
            c, world_id=row["world_id"], character_id=p["id"],
            destination_longitude=target["longitude"], destination_latitude=target["latitude"],
            world_time=at, speed_cap=cap, minimum_distance_km=0,
        ) for p in people]
        MovementService.synchronize(c, movements, at, from_iso(row["deadline_at"]))
        leg = str(uuid4())
        event = cls._event(c, row, actor, at, "departed", "全体留下者从各自真实起点按各自路线出发，以最慢者同步，尚未抵达。", {"leg_id": leg, "stop_index": row["next_index"], "member_ids": [p["id"] for p in people], "movement_ids": [m.id for m in movements], "terms": cls.terms(c, row), "resume_epoch": row["resume_epoch"]})
        c.execute("INSERT INTO group_journey_legs(id,journey_id,stop_index,status,started_at,source_event_id) VALUES (?,?,?,'moving',?,?)", (leg, row["id"], row["next_index"], to_iso(at), event))
        for movement in movements:
            source = c.execute("SELECT id FROM world_events WHERE world_id=? AND actor_id=? AND event_type='action.movement_started' AND json_extract(payload_json,'$.movement_id')=? ORDER BY rowid DESC LIMIT 1", (row["world_id"], movement.character_id, movement.id)).fetchone()
            if not source:
                raise ValueError("本程缺少本人真实出发来源")
            c.execute("INSERT INTO group_journey_leg_members VALUES (?,?,?,?)", (leg, movement.character_id, movement.id, source[0]))
        c.execute("UPDATE group_journeys SET status='active',phase='moving',started_at=COALESCE(started_at,?),next_depart_at=NULL,remaining_stop_seconds=0,source_event_id=?,reason='' WHERE id=?", (to_iso(at), event, row["id"]))
        return cls._result(cls._row(c, row["id"]), event, "本程逐人真实出发，抵达后才计算停留。")

    @classmethod
    def _stop(cls, c, row, at, cid=None):
        for meeting in c.execute(
            "SELECT m.character_id,m.id FROM group_journey_members gm JOIN character_movements m "
            "ON m.id=gm.meeting_movement_id AND m.character_id=gm.character_id "
            "WHERE gm.journey_id=? AND m.world_id=? AND m.status='moving'"
            + (" AND gm.character_id=?" if cid else ""),
            (row["id"], row["world_id"], cid) if cid else (row["id"], row["world_id"]),
        ).fetchall():
            MovementService().cancel(c, world_id=row["world_id"], character_id=meeting["character_id"], world_time=at, cascade_journey=False)
        for link in c.execute(
            "SELECT lm.*,m.status,m.world_id FROM group_journey_legs l JOIN group_journey_leg_members lm "
            "ON lm.leg_id=l.id JOIN character_movements m ON m.id=lm.movement_id AND m.character_id=lm.character_id "
            "WHERE l.journey_id=? AND l.status='moving'" + (" AND lm.character_id=?" if cid else ""),
            (row["id"], cid) if cid else (row["id"],),
        ).fetchall():
            if link["status"] == "moving" and link["world_id"] == row["world_id"]:
                MovementService().cancel(c, world_id=row["world_id"], character_id=link["character_id"], world_time=at, cascade_journey=False)
        if cid is None:
            c.execute("UPDATE group_journey_legs SET status='cancelled',finished_at=? WHERE journey_id=? AND status='moving'", (to_iso(at), row["id"]))

    @classmethod
    def pause(cls, c, row, at, reason, *, actor=None, force=False):
        row = cls._row(c, row["id"])
        if row["status"] == "paused" and not force:
            return cls._result(row, row["source_event_id"], "已经暂停，未重复取消或改写恢复答复。")
        cls._stop(c, row, at)
        cls._clear_follow(c, row["id"])
        remaining = max(0, (from_iso(row["next_depart_at"]) - at).total_seconds()) if row["phase"] == "stopped" and row["next_depart_at"] else row["remaining_stop_seconds"] if row["phase"] == "stopped" else 0
        speaker = actor or cls._person(c, row["world_id"], row["leader_id"])
        event = cls._event(c, row, speaker, at, "paused", reason, {"resume_epoch": row["resume_epoch"] + 1})
        c.execute("UPDATE group_journeys SET status='paused',phase=CASE phase WHEN 'moving' THEN 'ready' ELSE phase END,next_depart_at=NULL,remaining_stop_seconds=?,resume_epoch=resume_epoch+1,reason=?,source_event_id=? WHERE id=?", (remaining, reason, event, row["id"]))
        return cls._result(cls._row(c, row["id"]), event, "暂停保留真实位置和未完成停留，恢复须留下者分别同意。")

    @classmethod
    def _finish(cls, c, row, actor, at, status, reason):
        cls._stop(c, row, at)
        cls._clear_follow(c, row["id"])
        event = cls._event(c, row, actor, at, status, reason)
        c.execute("UPDATE group_journeys SET status=?,completed_at=?,reason=?,source_event_id=? WHERE id=?", (status, to_iso(at), reason, event, row["id"]))
        return cls._result(cls._row(c, row["id"]), event, reason)

    @classmethod
    def _leave(cls, c, row, actor, at, statement):
        if row["status"] == "pending":
            raise ValueError("提议尚未共同确认，请本人婉拒或由提议者结束旧约")
        cls._stop(c, row, at, actor["id"])
        event = cls._event(c, row, actor, at, "left", "一名成员本人退出原有限行程，保留每人真实选择和已经发生的路程。" + statement)
        c.execute("UPDATE group_journey_members SET left_at=?,left_event_id=? WHERE journey_id=? AND character_id=?", (to_iso(at), event, row["id"], actor["id"]))
        c.execute("UPDATE group_journeys SET source_event_id=? WHERE id=?", (event, row["id"]))
        if len(cls._members(c, row)) < 2:
            return cls._finish(c, row, actor, at, "ended", "仍参加人数不足两人，原共同旅行结束。")
        if actor["id"] == row["leader_id"] or row["separation_policy"] == "pause":
            return cls.pause(c, row, at, "原领队退出或原共同条款要求分离后暂停，留下者须分别商量。", actor=actor, force=True)
        if actor["id"] == row["player_id"]:
            cls._clear_follow(c, row["id"])
        return cls._result(cls._row(c, row["id"]), event, "本人已退出；余者可依全体原先接受的继续条款沿原有限路线继续。")

    @classmethod
    def _leg_valid(cls, c, row, leg, at):
        try:
            event = c.execute("SELECT * FROM world_events WHERE id=? AND world_id=?", (leg["source_event_id"], row["world_id"])).fetchone()
            links = c.execute("SELECT * FROM group_journey_leg_members WHERE leg_id=? ORDER BY rowid", (leg["id"],)).fetchall()
            payload = json.loads(event["payload_json"]) if event else {}
            if not (cls._source(c, row, leg["source_event_id"], "departed", row["leader_id"], at, terms=True, epoch=True)
                    and event["occurred_at"] == leg["started_at"]
                    and payload.get("leg_id") == leg["id"] and payload.get("stop_index") == leg["stop_index"]
                    and payload.get("member_ids") == [l["character_id"] for l in links]
                    and payload.get("movement_ids") == [l["movement_id"] for l in links]
                    and set(m["character_id"] for m in cls._members(c, row)).issubset({l["character_id"] for l in links})):
                return False
            target = c.execute("SELECT l.* FROM group_journey_stops s JOIN locations l ON l.id=s.location_id WHERE s.journey_id=? AND s.stop_index=?", (row["id"], leg["stop_index"])).fetchone()
            for link in links:
                movement = c.execute("SELECT * FROM character_movements WHERE id=? AND character_id=? AND world_id=?", (link["movement_id"], link["character_id"], row["world_id"])).fetchone()
                source = c.execute("SELECT * FROM world_events WHERE id=? AND world_id=?", (link["source_event_id"], row["world_id"])).fetchone()
                data = json.loads(source["payload_json"]) if source else {}
                if not (movement and source and source["actor_id"] == link["character_id"]
                        and source["event_type"] == "action.movement_started"
                        and source["occurred_at"] == leg["started_at"] == movement["started_at_world"]
                        and data.get("movement_id") == movement["id"]
                        and movement["destination_longitude"] == target["longitude"]
                        and movement["destination_latitude"] == target["latitude"]):
                    return False
            return True
        except (ValueError, TypeError, KeyError):
            return False

    @classmethod
    def _mark_stop(cls, c, row, at, leg):
        stop = c.execute("SELECT * FROM group_journey_stops WHERE journey_id=? AND stop_index=?", (row["id"], row["next_index"])).fetchone()
        actor = cls._person(c, row["world_id"], row["leader_id"])
        event = cls._event(c, row, actor, at, "arrived", "所有仍参加者实际抵达原站点，现在开始约定停留。", {"stop_index": row["next_index"], "leg_id": leg["id"] if leg else None})
        if leg:
            c.execute("UPDATE group_journey_legs SET status='arrived',finished_at=?,arrival_event_id=? WHERE id=?", (to_iso(at), event, leg["id"]))
        c.execute("UPDATE group_journey_stops SET arrived_at=?,source_event_id=? WHERE journey_id=? AND stop_index=?", (to_iso(at), event, row["id"], row["next_index"]))
        c.execute("UPDATE group_journeys SET phase='stopped',next_index=next_index+1,next_depart_at=?,remaining_stop_seconds=?,source_event_id=? WHERE id=?", (to_iso(at + timedelta(minutes=stop["minutes"])), stop["minutes"] * 60, event, row["id"]))

    @classmethod
    def _arrive(cls, c, row, at):
        leg = c.execute("SELECT * FROM group_journey_legs WHERE journey_id=? AND status='moving'", (row["id"],)).fetchone()
        if not leg or not cls._leg_valid(c, row, leg, at):
            cls.pause(c, row, at, "本程真实移动或来源记录不再完整，需要留下者重新商量。")
            return
        moves = c.execute(
            "SELECT m.* FROM group_journey_leg_members lm JOIN character_movements m ON m.id=lm.movement_id "
            "JOIN group_journey_members gm ON gm.character_id=lm.character_id AND gm.journey_id=? "
            "WHERE lm.leg_id=? AND gm.left_at IS NULL", (row["id"], leg["id"]),
        ).fetchall()
        if any(m["status"] not in {"moving", "arrived"} for m in moves):
            cls.pause(c, row, at, "一名留下者已改变本人移动，本程暂停并撤回跟随。")
            return
        if not all(m["status"] == "arrived" for m in moves):
            return
        target = c.execute("SELECT l.* FROM group_journey_stops s JOIN locations l ON l.id=s.location_id WHERE s.journey_id=? AND s.stop_index=?", (row["id"], row["next_index"])).fetchone()
        if any(great_circle_distance_km(p["longitude"], p["latitude"], target["longitude"], target["latitude"]) > 0.025 for p in cls._people(c, row)):
            cls.pause(c, row, at, "真实抵达位置未与原约定一致，需要实际会合。")
            return
        arrival = max(from_iso(m["updated_at_world"]) for m in moves)
        if arrival > at or arrival > from_iso(row["deadline_at"]):
            cls.pause(c, row, at, "真实到站回执晚于核验时间或原期限，不冒充按时完成。")
            return
        cls._mark_stop(c, row, arrival, leg)

    @classmethod
    def maintain(cls, c, wid, at):
        from world_engine.daily_life import DailyLifeService
        from world_engine.families import FamilyService
        from world_engine.schedules import ScheduleService

        rows = c.execute("SELECT * FROM group_journeys WHERE world_id=? AND status IN ('pending','accepted','active','paused','resume_pending') ORDER BY rowid", (wid,)).fetchall()
        for row in rows:
            remaining_people = cls._people(c, row)
            leader = next((p for p in remaining_people if p["id"] == row["leader_id"]), remaining_people[0] if remaining_people else cls._person(c, wid, row["proposer_id"]))
            if at >= from_iso(row["deadline_at"]):
                valid = cls._accepted(c, row, at) and cls._resumed(c, row, at)
                if cls._follow_required(c, row):
                    valid = valid and cls.following(c, row, from_iso(row["deadline_at"]) - timedelta(microseconds=1))
                if valid and row["status"] == "active" and row["phase"] == "moving":
                    cls._arrive(c, row, min(at, from_iso(row["deadline_at"])))
                    row = cls._row(c, row["id"])
                final = not c.execute("SELECT 1 FROM group_journey_stops WHERE journey_id=? AND stop_index=?", (row["id"], row["next_index"])).fetchone()
                if valid and row["status"] == "active" and row["phase"] == "stopped" and final and row["next_depart_at"] and from_iso(row["next_depart_at"]) <= from_iso(row["deadline_at"]):
                    cls._finish(c, row, leader, from_iso(row["next_depart_at"]), "completed", "留下者以真实路程和完整停留完成原有限行程。")
                else:
                    cls._finish(c, row, leader, at, "expired", "原旅行期限已到，保留实际路程及未完成结果。")
                continue
            if row["status"] in {"pending", "paused", "resume_pending"}:
                continue
            if not cls._accepted(c, row, at) or not cls._resumed(c, row, at) or (row["follow_source_event_id"] and not cls.following(c, row, at)):
                cls.pause(c, row, at, "原逐人同意或跟随来源已失效，需要本人重新商量。")
                continue
            if row["phase"] == "moving" and cls._follow_required(c, row) and not cls.following(c, row, at):
                cls.pause(c, row, at, "玩家的有限跟随授权已失效，本程暂停。")
                continue
            try:
                for lid in [row["origin_id"]] + [s[0] for s in c.execute("SELECT location_id FROM group_journey_stops WHERE journey_id=?", (row["id"],))]:
                    JourneyService._place(c, wid, lid)
            except ValueError:
                cls._finish(c, row, leader, at, "ended", "原约定地点已不可用，请另提完整新行程。")
                continue
            reason = ""
            people = cls._people(c, row)
            if len(people) < 2 or row["leader_id"] not in {p["id"] for p in people}:
                reason = "原领队已退出或当前人数不足，需要另提有限行程。"
            for person in people:
                activity = LifeActivityService.running(c, person["id"])
                plan = ScheduleService.assess(c, person, at)
                try:
                    cls._conflict(c, person, row, at)
                except ValueError as exc:
                    reason = str(exc)
                gathering = row["status"] == "accepted" and row["started_at"] is None
                if (person["health"] < 40 or person["energy"] <= 20 or person["satiety"] <= 30
                        or ((person["current_room_id"] or person["current_fixture_id"]) and not gathering) or person["active_vehicle_id"]
                        or person["movement_type"] != "land" or (plan and plan["depart_at"] <= at)
                        or (activity and (row["phase"] == "moving" or activity["kind"] not in {"wait", "rest"}))
                        or (not person["is_player"] and (DailyLifeService.is_sleep_time(person, at) or FamilyService.hungry_dependent(c, person, at)))):
                    reason = "一名成员当前无法继续共同旅行，请各自处理已有需要并重新商量。"
                if row["phase"] != "moving":
                    current_move = c.execute("SELECT id FROM character_movements WHERE character_id=? AND status='moving'", (person["id"],)).fetchone()
                    gathering = row["started_at"] is None and current_move and c.execute(
                        "SELECT 1 FROM group_journey_members gm JOIN character_movements m ON m.id=gm.meeting_movement_id "
                        "JOIN locations l ON l.id=? WHERE gm.journey_id=? AND gm.character_id=? "
                        "AND m.id=? AND m.world_id=? AND m.character_id=gm.character_id "
                        "AND m.destination_longitude=l.longitude AND m.destination_latitude=l.latitude",
                        (row["origin_id"], row["id"], person["id"], current_move[0], wid),
                    ).fetchone()
                    if current_move and not gathering:
                        reason = "一名成员选择了独立移动，原共同出发授权暂停。"
            for a, b in combinations(people, 2):
                if IntimacyService.paused(c, wid, a["id"], b["id"]):
                    reason = "一方要求留出空间，原共同旅行暂停。"
                if row["status"] == "active" and (a["current_room_id"] != b["current_room_id"] or great_circle_distance_km(a["longitude"], a["latitude"], b["longitude"], b["latitude"]) > 0.1):
                    reason = "留下者已经分离，需要实际重新会合并分别恢复。"
            if reason:
                cls.pause(c, row, at, reason)
                continue
            if row["phase"] == "moving":
                cls._arrive(c, row, at)
                row = cls._row(c, row["id"])
            if row["status"] == "active" and row["phase"] == "stopped" and at >= from_iso(row["next_depart_at"]) and not c.execute("SELECT 1 FROM group_journey_stops WHERE journey_id=? AND stop_index=?", (row["id"], row["next_index"])).fetchone():
                cls._finish(c, row, leader, from_iso(row["next_depart_at"]), "completed", "留下者以真实移动和完整停留完成原约定有限行程。")

    @classmethod
    def perform(cls, c, npc, at):
        cls.maintain(c, npc["world_id"], at)
        row = cls.active(c, npc["world_id"], npc["id"])
        if not row or row["status"] not in {"accepted", "active"}:
            return None
        if row["phase"] == "moving":
            return "正在履行本人已接受的有限共同旅行"
        if row["started_at"] is None:
            origin = JourneyService._place(c, row["world_id"], row["origin_id"])
            current_move = c.execute("SELECT id FROM character_movements WHERE character_id=? AND status='moving'", (npc["id"],)).fetchone()
            if current_move:
                return "正在以本人真实路线前往原约定集合处"
            if npc["current_room_id"] or npc["current_fixture_id"]:
                from world_engine.daily_life import DailyLifeService
                return DailyLifeService._travel(c, npc, at, origin)
            if great_circle_distance_km(npc["longitude"], npc["latitude"], origin["longitude"], origin["latitude"]) > 0.025:
                name = "group_gather_" + uuid4().hex
                c.execute("SAVEPOINT " + name)
                try:
                    movement = MovementService()._start_solo(
                        c, world_id=row["world_id"], character_id=npc["id"],
                        destination_longitude=origin["longitude"], destination_latitude=origin["latitude"],
                        world_time=at, minimum_distance_km=0,
                    )
                except ValueError:
                    c.execute("ROLLBACK TO " + name)
                    cls.pause(c, row, at, "本人实际赴集合的路线当前不可通行，请留下者重新商量。")
                    return "真实集合路线不可用，有限旅行已暂停"
                finally:
                    c.execute("RELEASE " + name)
                c.execute("UPDATE group_journey_members SET meeting_movement_id=? WHERE journey_id=? AND character_id=?", (movement.id, row["id"], npc["id"]))
                return "本人已按自己接受的有限约定，实际启程前往集合处"
            if any(p["current_room_id"] or great_circle_distance_km(p["longitude"], p["latitude"], origin["longitude"], origin["latitude"]) > 0.025 for p in cls._people(c, row)):
                return "本人已到集合处，等候其他人各自真实到达"
        if row["leader_id"] != npc["id"] or (cls._follow_required(c, row) and not cls.following(c, row, at)):
            return "等待原领队及玩家本人选择有限行程的下一程"
        if row["next_depart_at"] and at < from_iso(row["next_depart_at"]):
            return "正在真实完成原约定停留"
        try:
            result = cls.apply(c, npc, at, GroupJourneyIntent(operation="depart", journey_id=row["id"], statement="我本人按全体分别接受的原有限路线开始下一程。"))
        except ValueError:
            cls.pause(c, row, at, "原本程当前不能实际出发，须留下者重新商量。")
            return "本程真实出发条件已改变，已暂停并清除跟随"
        return result["summary"]

    @classmethod
    def boundary(cls, c, wid, at, result, satiety_rate=3):
        from world_engine.schedules import ScheduleService

        for row in c.execute("SELECT * FROM group_journeys WHERE world_id=? AND status IN ('pending','accepted','active','paused','resume_pending')", (wid,)).fetchall():
            deadline = from_iso(row["deadline_at"])
            if deadline > at:
                result = min(result, deadline)
            if row["status"] != "active" and row["status"] != "accepted":
                continue
            if row["started_at"] is None:
                try:
                    origin = JourneyService._place(c, wid, row["origin_id"])
                except ValueError:
                    # 哥哥，失效地点由维护正常结束约定，不能让边界查询卡住真实时钟。
                    continue
                if any(not p["is_player"] and great_circle_distance_km(p["longitude"], p["latitude"], origin["longitude"], origin["latitude"]) > 0.025 and not c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (p["id"],)).fetchone() for p in cls._people(c, row)):
                    result = min(result, at + timedelta(seconds=1))
            for person in cls._people(c, row):
                if not person["is_player"]:
                    hour = 7 if "巡夜" in (person["identity"] or "") or "夜行" in json.loads(person["traits_json"] or "[]") else 22
                    sleep = at.replace(hour=hour, minute=0, second=0, microsecond=0)
                    if sleep <= at:
                        sleep += timedelta(days=1)
                    result = min(result, sleep)
                plan = ScheduleService.assess(c, person, at)
                if plan and plan["depart_at"] > at:
                    result = min(result, plan["depart_at"])
                residual = c.execute("SELECT satiety_residual FROM character_state_accumulators WHERE character_id=?", (person["id"],)).fetchone()
                if satiety_rate > 0 and person["satiety"] > 30:
                    hours = (person["satiety"] - 30 - (residual[0] if residual else 0)) / satiety_rate
                    if hours > 0:
                        result = min(result, at + timedelta(hours=hours))
            for movement in c.execute("SELECT m.estimated_arrival_world FROM group_journey_members gm JOIN character_movements m ON m.id=gm.meeting_movement_id WHERE gm.journey_id=? AND gm.left_at IS NULL AND m.status='moving'", (row["id"],)):
                if from_iso(movement[0]) > at:
                    result = min(result, from_iso(movement[0]))
            if row["phase"] == "moving":
                for movement in c.execute("SELECT m.estimated_arrival_world FROM group_journey_legs l JOIN group_journey_leg_members lm ON lm.leg_id=l.id JOIN character_movements m ON m.id=lm.movement_id WHERE l.journey_id=? AND l.status='moving' AND m.status='moving'", (row["id"],)):
                    due = from_iso(movement[0])
                    if due > at:
                        result = min(result, due)
            elif row["next_depart_at"] and from_iso(row["next_depart_at"]) > at:
                result = min(result, from_iso(row["next_depart_at"]))
            elif row["leader_id"] != row["player_id"] and (not cls._follow_required(c, row) or cls.following(c, row, at)):
                people = cls._people(c, row)
                remains = c.execute("SELECT 1 FROM group_journey_stops WHERE journey_id=? AND stop_index=?", (row["id"], row["next_index"])).fetchone()
                if remains and len(people) >= 2 and not any(c.execute("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'", (p["id"],)).fetchone() for p in people) and all(great_circle_distance_km(a["longitude"], a["latitude"], b["longitude"], b["latitude"]) <= 0.02 for a, b in combinations(people, 2)):
                    result = min(result, at + timedelta(seconds=1))
        return result

    @classmethod
    def start_independent(cls, c, *, starter=None, **kwargs):
        """哥哥，接入普通移动入口时用此事务，失败不会偷偷撤回旧授权。"""
        name = "group_independent_" + uuid4().hex
        c.execute("SAVEPOINT " + name)
        try:
            starter = starter or MovementService()._start_existing
            row = cls.active(c, kwargs["world_id"], kwargs["character_id"])
            if row and row["status"] == "accepted" and row["started_at"] is None and not row["follow_source_event_id"]:
                origin = JourneyService._place(c, row["world_id"], row["origin_id"])
                if (kwargs["destination_longitude"] == origin["longitude"] and kwargs["destination_latitude"] == origin["latitude"]):
                    result = starter(c, **kwargs)
                    c.execute("UPDATE group_journey_members SET meeting_movement_id=? WHERE journey_id=? AND character_id=?", (result.id, row["id"], kwargs["character_id"]))
                    return result
            if row and row["status"] in {"accepted", "active", "resume_pending"}:
                cls.pause(c, row, kwargs["world_time"], "本人选择独立路线，原有限共同授权暂停。", actor=cls._person(c, row["world_id"], kwargs["character_id"]), force=row["status"] == "resume_pending")
            return starter(c, **kwargs)
        except Exception:
            c.execute("ROLLBACK TO " + name)
            raise
        finally:
            c.execute("RELEASE " + name)

    @classmethod
    def rows(cls, c, wid, cid, at, other=None):
        from world_engine.society import SocietyService

        result = []
        for row in c.execute("SELECT g.* FROM group_journeys g JOIN group_journey_members m ON m.journey_id=g.id WHERE g.world_id=? AND m.character_id=? AND g.proposed_at<=? ORDER BY g.rowid DESC LIMIT 12", (wid, cid, to_iso(at))).fetchall():
            members = cls._members(c, row, remaining=False)
            if other and other not in {m["character_id"] for m in members}:
                continue
            value = {key: row[key] for key in ("id", "proposer_id", "leader_id", "player_id", "status", "phase", "title", "statement", "origin_id", "deadline_at", "proposed_at", "next_index", "next_depart_at", "separation_policy", "return_to_origin", "resume_epoch", "reason", "source_event_id")}
            if row["status"] in cls.LIVE and at >= from_iso(row["deadline_at"]):
                value["status"] = "expired"
            value["leader_name"] = "你" if row["leader_id"] == cid else SocietyService.label(c, cid, row["leader_id"])
            origin = c.execute("SELECT name FROM locations WHERE id=? AND world_id=?", (row["origin_id"], wid)).fetchone()
            value["origin_name"] = origin[0] if origin else "原集合地点"
            value["following"] = cls.following(c, row, at)
            value["can_follow"] = bool(cid == row["player_id"] and cls._follow_required(c, row) and row["status"] in {"accepted", "active"} and row["phase"] != "moving" and at < from_iso(row["deadline_at"]) and not value["following"])
            value["members"] = [{"character_id": m["character_id"], "name": "你" if m["character_id"] == cid else SocietyService.label(c, cid, m["character_id"]), "decision": m["decision"], "decision_at": m["decision_at"], "left_at": m["left_at"], "resumed": m["resume_epoch"] == row["resume_epoch"] and bool(m["resume_event_id"])} for m in members]
            value["stops"] = [dict(s) for s in c.execute("SELECT s.stop_index,s.location_id,s.minutes,s.is_return,s.arrived_at,l.name FROM group_journey_stops s JOIN locations l ON l.id=s.location_id WHERE s.journey_id=? ORDER BY s.stop_index", (row["id"],))]
            own = next(m for m in members if m["character_id"] == cid)
            cutoff = min(at, from_iso(own["left_at"])) if own["left_at"] else at
            for member in value["members"]:
                if member["decision_at"] and from_iso(member["decision_at"]) > cutoff:
                    member.update(decision="pending", decision_at=None)
                if member["left_at"] and from_iso(member["left_at"]) > cutoff:
                    member["left_at"] = None
            for stop in value["stops"]:
                if stop["arrived_at"] and from_iso(stop["arrived_at"]) > cutoff:
                    stop["arrived_at"] = None
            latest = c.execute("SELECT occurred_at FROM world_events WHERE id=? AND world_id=?", (row["source_event_id"], wid)).fetchone()
            if latest and from_iso(latest[0]) > cutoff:
                # 哥哥，旧时间的读取只重放本人当时真正已知的共享事件，不倒灌后来进展。
                progress = {"status": "pending", "phase": "ready", "next_index": 0, "next_depart_at": None, "resume_epoch": 0, "following": False, "reason": "", "source_event_id": row["proposal_event_id"]}
                consent, restored, left, started = set(), set(), set(), False
                for known in c.execute(
                    "SELECT e.* FROM world_events e JOIN character_event_knowledge k ON k.event_id=e.id "
                    "AND k.world_id=e.world_id WHERE k.character_id=? AND e.world_id=? AND e.occurred_at<=? "
                    "AND k.learned_world_time<=? AND json_extract(e.payload_json,'$.journey_id')=? "
                    "ORDER BY e.occurred_at,e.rowid", (cid, wid, to_iso(cutoff), to_iso(cutoff), row["id"]),
                ):
                    operation = known["event_type"].removeprefix("social.group_journey_")
                    payload = json.loads(known["payload_json"])
                    progress["source_event_id"] = known["id"]
                    if operation in {"proposed", "accepted"}:
                        consent.add(known["actor_id"])
                        if consent == {m["character_id"] for m in members}:
                            progress["status"] = "accepted"
                    elif operation == "follow":
                        progress["following"] = True
                    elif operation == "departed":
                        started = True
                        progress.update(status="active", phase="moving", next_index=payload["stop_index"], next_depart_at=None)
                    elif operation == "arrived":
                        index = payload["stop_index"]
                        progress.update(status="active", phase="stopped", next_index=index + 1,
                                        next_depart_at=to_iso(from_iso(known["occurred_at"]) + timedelta(minutes=value["stops"][index]["minutes"])))
                    elif operation == "paused":
                        progress.update(status="paused", phase="ready" if progress["phase"] == "moving" else progress["phase"], following=False, next_depart_at=None, resume_epoch=payload["resume_epoch"])
                        restored.clear()
                    elif operation == "resumed":
                        restored.add(known["actor_id"])
                        progress["status"] = ("active" if started else "accepted") if restored == {m["character_id"] for m in members} - left else "resume_pending"
                    elif operation == "left":
                        left.add(known["actor_id"])
                    elif operation in {"ended", "expired", "completed"}:
                        progress.update(status=operation, following=False)
                value.update(progress)
                value["can_follow"] = False
                for member in value["members"]:
                    member["resumed"] = member["character_id"] in restored
            if own["left_at"] and from_iso(own["left_at"]) <= at:
                for member in value["members"]:
                    member["resumed"] = None
                value.update(status="left", phase=None, resume_epoch=None, following=False, can_follow=False, next_depart_at=None,
                             next_index=sum(bool(s["arrived_at"]) for s in value["stops"]),
                             reason="本人已经退出，后续进展需通过新的真实交流获知。", source_event_id=own["left_event_id"])
            result.append(value)
        IntimacyService.mask_records(c, wid, cid, result, ("title", "statement", "reason"))
        return result

    @classmethod
    def context(cls, c, wid, cid, other, at):
        return [r for r in cls.rows(c, wid, cid, at, other) if r["status"] in cls.LIVE][:2]
