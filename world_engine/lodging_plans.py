"""哥哥，旅途订房先商量各自限额，再由各人向真实房主独立租住。"""

# ruff: noqa: E501

import json
import math
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.geo import great_circle_distance_km
from world_engine.life import LifeActivityService
from world_engine.repository import from_iso, to_iso, utc_now

LODGING_PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS journey_lodging_plans (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 proposer_id TEXT NOT NULL REFERENCES characters(id),recipient_id TEXT NOT NULL REFERENCES characters(id),
 journey_id TEXT NOT NULL REFERENCES joint_journeys(id),stop_index INTEGER NOT NULL,
 arrival_event_id TEXT NOT NULL REFERENCES world_events(id),pause_event_id TEXT NOT NULL REFERENCES world_events(id),
 own_room_id TEXT NOT NULL REFERENCES life_rooms(id),other_room_id TEXT NOT NULL REFERENCES life_rooms(id),
 location_id TEXT NOT NULL REFERENCES locations(id),own_budget INTEGER NOT NULL CHECK(own_budget BETWEEN 0 AND 1000),
 other_budget INTEGER NOT NULL CHECK(other_budget BETWEEN 0 AND 1000),required_until_at TEXT NOT NULL,ready_by_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('pending','accepted','completed','declined','cancelled','expired')),
 statement TEXT NOT NULL,response TEXT NOT NULL DEFAULT '',proposed_at TEXT NOT NULL,
 source_event_id TEXT NOT NULL REFERENCES world_events(id),reason TEXT NOT NULL DEFAULT '',
 CHECK(proposer_id<>recipient_id),CHECK(own_room_id<>other_room_id),
 FOREIGN KEY(journey_id,stop_index) REFERENCES journey_stops(journey_id,stop_index)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_lodging_plan_live_pair ON journey_lodging_plans(
 world_id,MIN(proposer_id,recipient_id),MAX(proposer_id,recipient_id)) WHERE status IN ('pending','accepted');
CREATE INDEX IF NOT EXISTS idx_lodging_plan_due ON journey_lodging_plans(world_id,status,ready_by_at);
CREATE INDEX IF NOT EXISTS idx_lodging_plan_trip ON journey_lodging_plans(journey_id,status);
CREATE TABLE IF NOT EXISTS journey_lodging_roles (
 plan_id TEXT NOT NULL REFERENCES journey_lodging_plans(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id),room_id TEXT NOT NULL REFERENCES life_rooms(id),
 spending_limit INTEGER NOT NULL CHECK(spending_limit BETWEEN 0 AND 1000),
 spent INTEGER NOT NULL DEFAULT 0 CHECK(spent>=0 AND spent<=spending_limit),
 status TEXT NOT NULL DEFAULT 'awaiting' CHECK(status IN ('awaiting','offer_ready','booked','ready','blocked')),
 quote_id TEXT,request_id TEXT REFERENCES long_term_operation_requests(id),
 contract_id TEXT REFERENCES contract_fulfillments(request_id),ready_at TEXT,
 report_event_id TEXT REFERENCES world_events(id),last_attempt_at TEXT,retry_at TEXT,
 private_reason TEXT NOT NULL DEFAULT '',PRIMARY KEY(plan_id,character_id)
);
CREATE INDEX IF NOT EXISTS idx_lodging_role_person ON journey_lodging_roles(character_id,status,retry_at);
CREATE TABLE IF NOT EXISTS journey_lodging_quotes (
 id TEXT PRIMARY KEY,plan_id TEXT NOT NULL REFERENCES journey_lodging_plans(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id),owner_id TEXT NOT NULL REFERENCES characters(id),
 room_id TEXT NOT NULL REFERENCES life_rooms(id),request_id TEXT NOT NULL REFERENCES long_term_operation_requests(id),
 total INTEGER NOT NULL CHECK(total>=0),duration_days INTEGER NOT NULL CHECK(duration_days BETWEEN 1 AND 30),
 quoted_rate INTEGER NOT NULL CHECK(quoted_rate>0),terms_json TEXT NOT NULL,review_status TEXT NOT NULL,
 decision_basis TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('open','applied','superseded','expired')),
 source_event_id TEXT NOT NULL REFERENCES world_events(id)
);
CREATE INDEX IF NOT EXISTS idx_lodging_quote_role ON journey_lodging_quotes(plan_id,character_id,status);
"""


class LodgingPlanIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: Literal["propose", "accept", "decline", "cancel", "quote", "confirm", "report_ready"]
    plan_id: str | None = Field(default=None, min_length=1, max_length=100)
    journey_id: str | None = Field(default=None, min_length=1, max_length=100)
    own_room_id: str | None = Field(default=None, min_length=1, max_length=100)
    other_room_id: str | None = Field(default=None, min_length=1, max_length=100)
    own_budget: int | None = Field(default=None, ge=0, le=1000, strict=True)
    other_budget: int | None = Field(default=None, ge=0, le=1000, strict=True)
    required_until_world_time: str | None = None
    ready_by_world_time: str | None = None
    quote_id: str | None = Field(default=None, min_length=1, max_length=100)
    expected_total: int | None = Field(default=None, ge=0, le=1000, strict=True)
    statement: str = Field(min_length=1, max_length=240)

    @model_validator(mode="after")
    def coherent(self):
        proposal = (
            self.journey_id,
            self.own_room_id,
            self.other_room_id,
            self.required_until_world_time,
            self.ready_by_world_time,
        )
        if self.operation == "propose":
            if (
                self.plan_id
                or not all(proposal)
                or self.own_budget is None
                or self.other_budget is None
            ):
                raise ValueError("订房筹备须指定真实旅行、各自房间、限额、筹备期限和许可终点")
            if (
                self.own_room_id == self.other_room_id
                or self.quote_id
                or self.expected_total is not None
            ):
                raise ValueError("订房筹备需要两间不同房间，提出计划不能预先确认房费")
        elif (
            not self.plan_id
            or any(proposal)
            or self.own_budget is not None
            or self.other_budget is not None
        ):
            raise ValueError("回应订房筹备只引用已有计划，不能改写房间、预算或期限")
        if self.operation == "confirm":
            if not self.quote_id or self.expected_total is None:
                raise ValueError("付款须引用本人准确报价编号和明确总价")
        elif self.quote_id or self.expected_total is not None:
            raise ValueError("只有确认付款需要报价编号和准确总价")
        return self


class LodgingPlanService:
    @staticmethod
    def _people(c, row):
        return [
            c.execute("SELECT * FROM characters WHERE id=?", (row[k],)).fetchone()
            for k in ("proposer_id", "recipient_id")
        ]

    @staticmethod
    def _role(c, row, cid):
        role = c.execute(
            "SELECT * FROM journey_lodging_roles WHERE plan_id=? AND character_id=?",
            (row["id"], cid),
        ).fetchone()
        if not role:
            raise ValueError("这份订房筹备不属于本人")
        return role

    @staticmethod
    def _record(c, row, at, kind, summary, *, actor_id=None, owner_id=None, payload=None):
        from world_engine.actions import ActionService
        from world_engine.appointments import AppointmentService

        values = {"plan_id": row["id"], **(payload or {})}
        if actor_id is None:
            first, second = LodgingPlanService._people(c, row)
            return AppointmentService._record(
                c, first, second, at, "lodging_plan_" + kind, summary, values
            )
        event = ActionService._record_event(
            c,
            world_id=row["world_id"],
            tick_id=str(uuid4()),
            occurred_at=at,
            event_type="social.lodging_plan_" + kind,
            actor_id=actor_id,
            target_id=owner_id,
            location_id=None,
            summary=summary,
            payload=values,
        )
        for cid in {actor_id, owner_id} - {None}:
            ActionService._record_memory(
                c,
                world_id=row["world_id"],
                character_id=cid,
                event_id=event,
                memory_type="experienced",
                summary=summary,
                importance=5,
            )
        return event

    @staticmethod
    def _time(value, label):
        try:
            result = datetime.fromisoformat(value)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"{label}须使用带时区的明确世界时间") from exc
        if result.tzinfo is None:
            raise ValueError(f"{label}须使用带时区的明确世界时间")
        return result

    @staticmethod
    def _place(c, row):
        place = c.execute(
            "SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1",
            (row["location_id"], row["world_id"]),
        ).fetchone()
        if not place:
            raise ValueError("原停留站点已经不可用")
        return place

    @classmethod
    def _at_stop(cls, c, row, actor):
        from world_engine.player_steering import PlayerSteeringService

        place = cls._place(c, row)
        return bool(
            actor["health"] > 0
            and actor["current_room_id"] is None
            and not actor["current_fixture_id"]
            and not actor["active_vehicle_id"]
            and actor["movement_type"] == "land"
            and actor["space_layer"] == place["space_layer"]
            and great_circle_distance_km(
                actor["longitude"], actor["latitude"], place["longitude"], place["latitude"]
            )
            <= 0.02
            and not LifeActivityService.running(c, actor["id"])
            and not PlayerSteeringService.active(c, actor["id"])
            and not c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (actor["id"],),
            ).fetchone()
        )

    @staticmethod
    def _contact(c, renter, owner):
        from world_engine.proximity import same_room

        if not owner or owner["world_id"] != renter["world_id"] or owner["health"] <= 0:
            return False
        local = (
            same_room(renter, owner)
            and great_circle_distance_km(
                renter["longitude"], renter["latitude"], owner["longitude"], owner["latitude"]
            )
            <= 0.02
        )
        if (
            local
            and not c.execute(
                "SELECT 1 FROM character_movements WHERE character_id IN (?,?) AND status='moving'",
                (renter["id"], owner["id"]),
            ).fetchone()
            and not LifeActivityService.running(c, owner["id"])
        ):
            return True
        return bool(
            c.execute(
                "SELECT 1 FROM character_contacts WHERE world_id=? AND status='accepted' AND "
                "((requester_id=? AND recipient_id=?) OR (requester_id=? AND recipient_id=?))",
                (renter["world_id"], renter["id"], owner["id"], owner["id"], renter["id"]),
            ).fetchone()
        )

    @staticmethod
    def _reserve(c, actor):
        if actor["is_player"]:
            return 0
        from world_engine.routines import RoutinePlanSpec

        saved = c.execute(
            "SELECT spec_json FROM npc_routine_plans WHERE character_id=?", (actor["id"],)
        ).fetchone()
        return max(
            10, RoutinePlanSpec.model_validate_json(saved[0]).income_reserve if saved else 10
        )

    @classmethod
    def _paths(cls, c, row):
        from world_engine.appointment_rooms import AppointmentRoomService

        chains = [
            AppointmentRoomService.path(c, row["world_id"], row[k])
            for k in ("own_room_id", "other_room_id")
        ]
        if any(chain[0]["location_id"] != row["location_id"] for chain in chains):
            raise ValueError("订房筹备的两间房必须位于原旅行实际到达的同一站点")
        if chains[0][0]["id"] == chains[1][0]["id"] or any(
            chains[i][0]["id"] in {r["id"] for r in chains[1 - i]} for i in (0, 1)
        ):
            raise ValueError("两间房必须独立，不能让另一人的客房成为自己的内外通路")
        return chains

    @classmethod
    def _access(cls, c, row, actor, at):
        from world_engine.appointment_rooms import AppointmentRoomService
        from world_engine.interiors import InteriorService
        from world_engine.separate_stays import SeparateStayService

        role = cls._role(c, row, actor["id"])
        rooms = SeparateStayService._access(
            c,
            row["world_id"],
            actor["id"],
            role["room_id"],
            row["location_id"],
            at,
            from_iso(row["required_until_at"]),
        )
        for room in rooms:
            policy = c.execute(
                "SELECT key_item_type_id FROM life_door_policies WHERE room_id=?", (room["id"],)
            ).fetchone()
            if (room["door_locked"] or policy and policy[0]) and not InteriorService.can_unlock(
                c, room, actor["id"], at=at
            ):
                raise ValueError("本人尚缺房间或外层门路需要的真实钥匙")
            # 哥哥，未来的锁不会因为一份预算自动消失，完整许可仍由原房间规则核验。
            AppointmentRoomService.controller(c, room, at, from_iso(row["required_until_at"]))
        return rooms

    @classmethod
    def _ready_access(cls, c, row, actor, at):
        try:
            cls._access(c, row, actor, at)
            return True
        except ValueError:
            return False

    @classmethod
    def _rental(cls, c, row, actor, at):
        from world_engine.appointment_rooms import AppointmentRoomService

        cls._paths(c, row)
        role = cls._role(c, row, actor["id"])
        room = AppointmentRoomService.path(c, row["world_id"], role["room_id"])[0]
        if cls._ready_access(c, row, actor, at):
            raise ValueError("本人已有覆盖完整时段的住房许可，请直接报告准备完成，避免重复付费")
        owner = c.execute(
            "SELECT * FROM characters WHERE id=? AND world_id=?",
            (room["owner_character_id"], row["world_id"]),
        ).fetchone()
        if not owner or owner["id"] == actor["id"]:
            raise ValueError("本人自住房不能向自己付租；请先核对已有完整出入许可")
        if owner["is_player"]:
            raise ValueError("玩家所有房间须由玩家另行明确授权，不能由房主规则代为同意出租")
        if not cls._contact(c, actor, owner):
            raise ValueError("本人尚不能实际联系房主；请当面接触或先取得双方接受的联络信笺")
        rate = c.execute(
            "SELECT nightly_rate FROM room_rental_rates WHERE room_id=?", (room["id"],)
        ).fetchone()
        if not rate:
            raise ValueError("房间尚未登记有效出租条件")
        # 哥哥，只核验仍有效的既有租住，不通过伪造取消来腾出他人的房间。
        if c.execute(
            "SELECT 1 FROM contract_fulfillments WHERE kind='lodging' AND asset_id=? "
            "AND status='active'",
            (room["id"],),
        ).fetchone():
            raise ValueError("房间已有有效租约；当前筹备不会擅自续租或抢占")
        end = from_iso(row["required_until_at"])
        for outer in AppointmentRoomService.path(c, row["world_id"], room["id"])[1:]:
            if outer["access_policy"] != "public" or outer["door_locked"]:
                raise ValueError("出租房间外层通道没有可用的公共通行条件")
            if AppointmentRoomService.lease(c, outer, at, end):
                raise ValueError("出租房间外层通道存在他人的租住占用")
        for part in AppointmentRoomService.path(c, row["world_id"], room["id"]):
            policy = c.execute(
                "SELECT key_item_type_id FROM life_door_policies WHERE room_id=?", (part["id"],)
            ).fetchone()
            if (
                policy
                and policy[0]
                and not c.execute(
                    "SELECT 1 FROM item_instances WHERE world_id=? AND item_type_id=? "
                    "AND container_type='character_inventory' AND container_id=? AND owner_character_id=? "
                    "AND condition>0 AND quantity>0",
                    (row["world_id"], policy[0], actor["id"], actor["id"]),
                ).fetchone()
            ):
                raise ValueError("本人尚未实际取得客房或外层通路需要的实体钥匙")
        days = max(1, math.ceil((end - at).total_seconds() / 86400))
        if not 1 <= days <= 30:
            raise ValueError("订房筹备只办理一至三十个世界日的完整租住")
        return room, owner, int(rate[0]), days

    @classmethod
    def room_options(cls, c, wid, cid, other, at):
        from world_engine.separate_stays import SeparateStayService

        options = SeparateStayService.room_options(c, wid, cid, other, at)
        known = {o["id"] for o in options}
        for row in c.execute(
            "SELECT own_room_id,other_room_id FROM journey_lodging_plans WHERE world_id=? "
            "AND (proposer_id=? OR recipient_id=?)",
            (wid, cid, cid),
        ):
            for rid in row:
                if rid in known:
                    continue
                room = c.execute(
                    "SELECT r.id,r.name,r.location_id,l.name location_name FROM life_rooms r "
                    "JOIN locations l ON l.id=r.location_id WHERE r.id=? AND r.world_id=? AND l.is_active=1",
                    (rid, wid),
                ).fetchone()
                if room:
                    options.append({**dict(room), "own_access": False})
                    known.add(rid)
        # 哥哥，候选房间沿用本人知识集合，不将后台房主的位置和钱包带给旅伴。
        for option in options:
            rate = c.execute(
                "SELECT nightly_rate FROM room_rental_rates WHERE room_id=?", (option["id"],)
            ).fetchone()
            option["nightly_rate"] = rate[0] if rate else None
        return options

    @classmethod
    def apply(cls, c, actor, other, at, choice):
        c.execute("SAVEPOINT lodging_plan_choice")
        try:
            return cls._apply(c, actor, other, at, choice)
        except Exception:
            c.execute("ROLLBACK TO lodging_plan_choice")
            raise
        finally:
            c.execute("RELEASE lodging_plan_choice")

    @classmethod
    def _apply(cls, c, actor, other, at, choice):
        from world_engine.intimacy import IntimacyService

        actor = c.execute(
            "SELECT * FROM characters WHERE id=? AND world_id=?", (actor["id"], actor["world_id"])
        ).fetchone()
        if not actor:
            raise ValueError("本人已不在当前世界")
        if other is not None:
            other = c.execute(
                "SELECT * FROM characters WHERE id=? AND world_id=?",
                (other["id"], actor["world_id"]),
            ).fetchone()
        wid = actor["world_id"]
        cls.maintain(c, wid, at)
        if choice.operation == "propose":
            from world_engine.journey_breaks import JourneyBreakService
            from world_engine.journey_room_appointments import JourneyRoomService
            from world_engine.journeys import JourneyService

            if (
                other is None
                or wid != other["world_id"]
                or bool(actor["is_player"]) == bool(other["is_player"])
            ):
                raise ValueError("请选择当前世界与玩家共同旅行的另一位人物")
            IntimacyService._reachable(c, actor, other)
            if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                raise ValueError("一方需要留出空间，暂不提出共同订房筹备")
            end = cls._time(choice.required_until_world_time, "完整许可终点")
            ready = cls._time(choice.ready_by_world_time, "筹备期限")
            if not at < ready < end <= at + timedelta(days=30):
                raise ValueError("筹备期限须在未来且早于许可终点，完整安排不超过三十日")
            known = {r["id"] for r in cls.room_options(c, wid, actor["id"], other["id"], at)}
            if not {choice.own_room_id, choice.other_room_id} <= known:
                raise ValueError("只能商量本人已经认识或眼前可见的真实房间")
            room = c.execute(
                "SELECT * FROM life_rooms WHERE id=? AND world_id=?", (choice.own_room_id, wid)
            ).fetchone()
            row = dict(
                id=str(uuid4()),
                world_id=wid,
                proposer_id=actor["id"],
                recipient_id=other["id"],
                journey_id=choice.journey_id,
                own_room_id=choice.own_room_id,
                other_room_id=choice.other_room_id,
                own_budget=choice.own_budget,
                other_budget=choice.other_budget,
                location_id=room["location_id"],
                required_until_at=to_iso(end),
                ready_by_at=to_iso(ready),
                status="pending",
            )
            trip, stop = JourneyRoomService.validate(
                c, actor, other, at, choice.journey_id, row["location_id"], end
            )
            JourneyService._assert_breaks_finished(c, trip, at)
            cls._paths(c, row)
            if c.execute(
                "SELECT 1 FROM journey_lodging_plans WHERE world_id=? AND status IN ('pending','accepted') "
                "AND (proposer_id IN (?,?) OR recipient_id IN (?,?))",
                (wid, actor["id"], other["id"], actor["id"], other["id"]),
            ).fetchone():
                raise ValueError("一方已有尚未结束的订房筹备，请先处理原安排")
            link = JourneyBreakService.prepare(c, actor, other, at, choice.journey_id)
            row.update(
                stop_index=stop["stop_index"],
                arrival_event_id=link["arrival_event_id"],
                pause_event_id=link["pause_event_id"],
            )
            summary = (
                "原旅行暂停；提出分别以本人限额筹备指定房间，房主审议、付款和之后住宿仍独立确认。"
            )
            event = cls._record(c, row, at, "proposed", summary + choice.statement)
            columns = tuple(row)
            c.execute(
                "INSERT INTO journey_lodging_plans("
                + ",".join(columns)
                + ",statement,proposed_at,source_event_id) "
                "VALUES (" + ",".join("?" for _ in columns) + ",?,?,?)",
                tuple(row[k] for k in columns) + (choice.statement, to_iso(at), event),
            )
            for person, rid, budget in (
                (actor, choice.own_room_id, choice.own_budget),
                (other, choice.other_room_id, choice.other_budget),
            ):
                c.execute(
                    "INSERT INTO journey_lodging_roles(plan_id,character_id,room_id,spending_limit) VALUES (?,?,?,?)",
                    (row["id"], person["id"], rid, budget),
                )
            return dict(status="pending", plan_id=row["id"], event_id=event, summary=summary)
        found = c.execute(
            "SELECT * FROM journey_lodging_plans WHERE id=? AND world_id=? "
            "AND (proposer_id=? OR recipient_id=?)",
            (choice.plan_id, wid, actor["id"], actor["id"]),
        ).fetchone()
        if not found:
            raise ValueError("这份订房筹备不属于本人")
        row = dict(found)
        companion_id = (
            row["recipient_id"] if actor["id"] == row["proposer_id"] else row["proposer_id"]
        )
        if other is not None and other["id"] != companion_id:
            raise ValueError("订房筹备的旅伴与引用的计划不一致")
        other = c.execute(
            "SELECT * FROM characters WHERE id=? AND world_id=?", (companion_id, wid)
        ).fetchone()
        if choice.operation == "confirm":
            # 哥哥，完成后的同一准确付款重试仍返回原回执，不会再扣第二笔。
            prior = c.execute(
                "SELECT * FROM journey_lodging_quotes WHERE id=? AND plan_id=? AND character_id=? AND status='applied'",
                (choice.quote_id, row["id"], actor["id"]),
            ).fetchone()
            if prior:
                if prior["total"] != choice.expected_total:
                    raise ValueError("准确总价与原付款回执不同")
                return dict(
                    status="booked",
                    plan_id=row["id"],
                    quote_id=prior["id"],
                    request_id=prior["request_id"],
                    contract_id=prior["request_id"],
                    event_id=prior["source_event_id"],
                    payment=prior["total"],
                    summary="本人这份准确报价已经付款并取得租约，原回执保留。",
                )
        if choice.operation in {"accept", "decline"}:
            IntimacyService._reachable(c, actor, other)
            if row["status"] != "pending" or row["recipient_id"] != actor["id"]:
                raise ValueError("只有收到尚有效订房筹备邀请的一方可以回应")
            if choice.operation == "accept":
                if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                    raise ValueError("一方仍需要留出空间")
                known = {r["id"] for r in cls.room_options(c, wid, actor["id"], other["id"], at)}
                if not {row["own_room_id"], row["other_room_id"]} <= known:
                    raise ValueError("本人尚未认识所约定房间，不能确认未知住房安排")
                cls._paths(c, row)
                status, summary = (
                    "accepted",
                    "双方确认各自房间和支出上限；NPC仅授权在本人限额及生活储备内自行办理本人租住，玩家仍须逐笔确认。",
                )
            else:
                status, summary = "declined", "本次订房筹备已婉拒，未产生租约或付款。"
            event = cls._record(c, row, at, status, summary + choice.statement)
            c.execute(
                "UPDATE journey_lodging_plans SET status=?,response=?,source_event_id=? WHERE id=?",
                (status, choice.statement, event, row["id"]),
            )
            if status == "accepted":
                c.execute(
                    "UPDATE npc_daily_states SET next_world_time=? WHERE character_id=?",
                    (to_iso(at), actor["id"] if not actor["is_player"] else other["id"]),
                )
            return dict(status=status, plan_id=row["id"], event_id=event, summary=summary)
        if choice.operation == "cancel":
            if (
                row["status"] not in {"pending", "accepted"}
                or row["status"] == "pending"
                and row["proposer_id"] != actor["id"]
            ):
                raise ValueError("只能撤回本人邀请，或退出双方已接受的订房筹备")
            return cls._finish(
                c,
                row,
                at,
                "cancelled",
                "一方结束本次订房筹备；既有租约、实际付款与个人活动独立保留，不自动退款或恢复旅行。",
            )
        if row["status"] != "accepted" or at > from_iso(row["ready_by_at"]):
            raise ValueError("请在双方已确认的筹备期限内处理本人的订房选择")
        if IntimacyService.paused(c, wid, actor["id"], other["id"]):
            raise ValueError("一方已表达暂停联系的界限")
        LifeActivityService.assert_available(c, actor["id"])
        if (
            actor["health"] <= 0
            or c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (actor["id"],),
            ).fetchone()
        ):
            raise ValueError("请先结束本人移动或占用，再办理住房或报告准备完成")
        if choice.operation == "quote":
            return cls._quote(c, row, actor, at)
        if choice.operation == "confirm":
            return cls._confirm(c, row, actor, at, choice)
        return cls._report(c, row, actor, other, at, choice.statement)

    @classmethod
    def _quote(cls, c, row, actor, at):
        from world_engine.lodging_review import review

        role = cls._role(c, row, actor["id"])
        room, owner, rate, days = cls._rental(c, row, actor, at)
        existing = c.execute(
            "SELECT * FROM journey_lodging_quotes WHERE id=? AND status='open' AND expires_at>?",
            (role["quote_id"], to_iso(at)),
        ).fetchone()
        if (
            existing
            and existing["room_id"] == room["id"]
            and existing["owner_id"] == owner["id"]
            and existing["quoted_rate"] == rate
            and existing["duration_days"] == days
        ):
            event = cls._record(
                c, row, at, "quoted", "本人再次核对尚有效的住房报价，原审议与准确总价保持不变。",
                actor_id=actor["id"], payload={"quote_id": existing["id"], "request_id": existing["request_id"]},
            )
            return dict(
                status="offer_ready",
                plan_id=row["id"],
                quote_id=existing["id"],
                expected_total=existing["total"],
                request_id=existing["request_id"],
                event_id=event,
                summary="本人已有尚有效的准确报价，请核对后单独确认付款。",
            )
        terms = {"room_id": room["id"], "payment": rate * days, "duration_days": days}
        status, counter, basis = review(c, row["world_id"], actor, owner, terms, at)
        effective = counter if status == "npc_countered" else terms
        total = effective.get("payment", 0)
        if type(total) is not int or total < 0:
            raise ValueError("房主报价没有有效的非负整数总价")
        rid, qid = str(uuid4()), str(uuid4())
        event = cls._record(
            c,
            row,
            at,
            "reviewed",
            "本人与实际房主按明确房间和租住期限审议了住房；尚未付款或取得许可。",
            actor_id=actor["id"],
            owner_id=owner["id"],
            payload={"request_id": rid, "quote_id": qid, "review_status": status},
        )
        c.execute(
            "INSERT INTO long_term_operation_requests(id,world_id,requester_id,recipient_id,operation_type,terms_json,status,"
            "npc_response,system_notice,counter_terms_json,source_event_id,created_at,updated_at) VALUES (?,?,?,?,'住房',?,?,NULL,?,?,?,?,?)",
            (
                rid,
                row["world_id"],
                actor["id"],
                owner["id"],
                json.dumps(terms, ensure_ascii=False),
                status,
                basis,
                json.dumps(counter, ensure_ascii=False) if counter else None,
                event,
                to_iso(utc_now()),
                to_iso(utc_now()),
            ),
        )
        blocked = (
            status == "npc_rejected"
            or total > role["spending_limit"] - role["spent"]
            or total > actor["money"] - cls._reserve(c, actor)
        )
        reason = (
            basis
            if status == "npc_rejected"
            else "报价超过本人剩余支出限额或可动用资金，未付款。"
            if blocked
            else ""
        )
        c.execute(
            "UPDATE journey_lodging_quotes SET status='superseded' WHERE plan_id=? AND character_id=? AND status='open'",
            (row["id"], actor["id"]),
        )
        c.execute(
            "INSERT INTO journey_lodging_quotes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                qid,
                row["id"],
                actor["id"],
                owner["id"],
                room["id"],
                rid,
                total,
                days,
                rate,
                json.dumps(effective, ensure_ascii=False, sort_keys=True),
                status,
                basis,
                to_iso(at),
                to_iso(min(at + timedelta(minutes=30), from_iso(row["ready_by_at"]))),
                "superseded" if blocked else "open",
                event,
            ),
        )
        c.execute(
            "UPDATE journey_lodging_roles SET status=?,quote_id=?,request_id=?,last_attempt_at=?,retry_at=?,private_reason=? "
            "WHERE plan_id=? AND character_id=?",
            (
                "blocked" if blocked else "offer_ready",
                qid,
                rid,
                to_iso(at),
                to_iso(at + timedelta(minutes=15)),
                reason,
                row["id"],
                actor["id"],
            ),
        )
        return dict(
            status="blocked" if blocked else "offer_ready",
            plan_id=row["id"],
            quote_id=qid,
            expected_total=total,
            request_id=rid,
            event_id=event,
            summary=reason or "房主已独立审议本人住房请求；请核对准确总价后，再由本人确认付款。",
        )

    @classmethod
    def _confirm(cls, c, row, actor, at, choice):
        from world_engine.contracts import ContractService
        from world_engine.lodging_review import review

        role = cls._role(c, row, actor["id"])
        quote = c.execute(
            "SELECT * FROM journey_lodging_quotes WHERE id=? AND plan_id=? AND character_id=?",
            (choice.quote_id, row["id"], actor["id"]),
        ).fetchone()
        if (
            not quote
            or role["quote_id"] != quote["id"]
            or quote["status"] != "open"
            or at >= from_iso(quote["expires_at"])
        ):
            raise ValueError("本人准确报价已失效，请重新询价后确认")
        if quote["total"] != choice.expected_total:
            raise ValueError("本人确认总价与房主这份准确报价不一致")
        room, owner, rate, days = cls._rental(c, row, actor, at)
        if (
            room["id"] != quote["room_id"]
            or owner["id"] != quote["owner_id"]
            or rate != quote["quoted_rate"]
            or days != quote["duration_days"]
        ):
            raise ValueError("房主、房价或所需租期已经变化，请重新询价")
        request = c.execute(
            "SELECT * FROM long_term_operation_requests WHERE id=?", (quote["request_id"],)
        ).fetchone()
        if (
            not request
            or request["status"] not in {"npc_accepted", "npc_countered"}
            or request["requester_id"] != actor["id"]
            or request["recipient_id"] != owner["id"]
        ):
            raise ValueError("原住房审议已失效，不能按旧报价付款")
        terms = json.loads(quote["terms_json"])
        status, counter, _ = review(
            c, row["world_id"], actor, owner, json.loads(request["terms_json"]), at
        )
        effective = counter if status == "npc_countered" else json.loads(request["terms_json"])
        if status not in {"npc_accepted", "npc_countered"} or effective != terms:
            raise ValueError("房主审议条件已变化，需要重新询价并明确确认")
        if quote["total"] > role["spending_limit"] - role["spent"] or quote["total"] > actor[
            "money"
        ] - cls._reserve(c, actor):
            raise ValueError("本人的剩余预算或可动用余额不足，未产生付款")
        due = ContractService.start(c, request, terms, to_iso(at))
        if from_iso(due) < from_iso(row["required_until_at"]):
            raise ValueError("新租约没有覆盖约定的完整住宿终点，付款已撤回")
        cls._access(c, row, actor, at)
        event = cls._record(
            c,
            row,
            at,
            "booked",
            "本人明确确认准确房费，以本人钱包向真实房主付款并取得限期租约；之后入屋与住宿仍各自决定。",
            actor_id=actor["id"],
            owner_id=owner["id"],
            payload={
                "request_id": request["id"],
                "quote_id": quote["id"],
                "payment": quote["total"],
            },
        )
        c.execute(
            "UPDATE long_term_operation_requests SET status='applied',source_event_id=?,updated_at=? WHERE id=?",
            (event, to_iso(utc_now()), request["id"]),
        )
        c.execute(
            "UPDATE journey_lodging_quotes SET status='applied',source_event_id=? WHERE id=?",
            (event, quote["id"]),
        )
        c.execute(
            "UPDATE journey_lodging_roles SET status='booked',spent=spent+?,contract_id=?,last_attempt_at=?,retry_at=NULL,private_reason='' WHERE plan_id=? AND character_id=?",
            (quote["total"], request["id"], to_iso(at), row["id"], actor["id"]),
        )
        return dict(
            status="booked",
            plan_id=row["id"],
            quote_id=quote["id"],
            request_id=request["id"],
            contract_id=request["id"],
            event_id=event,
            payment=quote["total"],
            summary="本人已按准确报价实际支付房费并取得完整限期租约；尚未报告准备完成。",
        )

    @classmethod
    def _report(cls, c, row, actor, other, at, statement):
        from world_engine.appointments import AppointmentService
        from world_engine.proximity import same_room

        cls._paths(c, row)
        cls._access(c, row, actor, at)
        if (
            not cls._at_stop(c, row, actor)
            or not cls._at_stop(c, row, other)
            or not same_room(actor, other)
            or great_circle_distance_km(
                actor["longitude"], actor["latitude"], other["longitude"], other["latitude"]
            )
            > 0.02
        ):
            raise ValueError("请双方空闲、实际回到原室外站点二十米内，再当面报告本人准备完成")
        role = cls._role(c, row, actor["id"])
        if role["ready_at"]:
            return dict(
                status="ready",
                plan_id=row["id"],
                event_id=role["report_event_id"],
                summary="本人已经当面报告准备完成，原报告保留。",
            )
        event = AppointmentService._record(
            c,
            actor,
            other,
            at,
            "lodging_plan_ready",
            "本人当面报告指定房间已有完整许可；这不授权进入旅伴房间或开始住宿。" + statement,
            {"plan_id": row["id"], "character_id": actor["id"]},
        )
        c.execute(
            "UPDATE journey_lodging_roles SET status='ready',ready_at=?,report_event_id=?,private_reason='',retry_at=NULL WHERE plan_id=? AND character_id=?",
            (to_iso(at), event, row["id"], actor["id"]),
        )
        cls.maintain(c, row["world_id"], at)
        fresh = c.execute(
            "SELECT status,source_event_id FROM journey_lodging_plans WHERE id=?", (row["id"],)
        ).fetchone()
        return dict(
            status="completed" if fresh["status"] == "completed" else "ready",
            plan_id=row["id"],
            event_id=fresh["source_event_id"] if fresh["status"] == "completed" else event,
            summary="双方已实际核对各自住房筹备，之后的分房休息、会合和恢复旅行仍另行商量。"
            if fresh["status"] == "completed"
            else "本人已当面报告准备完成，等待另一人独立报告。",
        )

    @classmethod
    def _finish(cls, c, row, at, status, reason):
        event = cls._record(c, row, at, status, reason)
        c.execute(
            "UPDATE journey_lodging_plans SET status=?,reason=?,source_event_id=? WHERE id=?",
            (status, reason, event, row["id"]),
        )
        c.execute(
            "UPDATE journey_lodging_quotes SET status='expired' WHERE plan_id=? AND status='open'",
            (row["id"],),
        )
        return dict(status=status, plan_id=row["id"], event_id=event, summary=reason)

    @classmethod
    def maintain(cls, c, wid, at):
        from world_engine.intimacy import IntimacyService

        changed = 0
        for found in c.execute(
            "SELECT * FROM journey_lodging_plans WHERE world_id=? AND status IN ('pending','accepted')",
            (wid,),
        ).fetchall():
            row = dict(found)
            people = cls._people(c, row)
            trip = c.execute(
                "SELECT * FROM joint_journeys WHERE id=?", (row["journey_id"],)
            ).fetchone()
            reason = None
            if at > from_iso(row["ready_by_at"]):
                reason = "订房筹备期限已过；未完成的共同筹备结束，既有付款和租约独立保留。"
            elif not all(p and p["health"] > 0 for p in people):
                reason = "一方目前不能继续共同筹备，既有个人住房事实保留。"
            elif (
                not trip
                or trip["status"] not in {"paused", "resume_pending"}
                or trip["next_index"] != row["stop_index"] + 1
            ):
                reason = "原旅行或已到站安排已结束或变化，订房筹备停止，已付款租约不被撤回。"
            elif IntimacyService.paused(c, wid, row["proposer_id"], row["recipient_id"]):
                reason = "一方表达暂停联系的界限，共同订房筹备结束，个人租约保留。"
            if reason:
                cls._finish(
                    c,
                    row,
                    at,
                    "expired" if at > from_iso(row["ready_by_at"]) else "cancelled",
                    reason,
                )
                changed += 1
                continue
            c.execute(
                "UPDATE journey_lodging_quotes SET status='expired' WHERE plan_id=? AND status='open' AND expires_at<=?",
                (row["id"], to_iso(at)),
            )
            if row["status"] != "accepted":
                continue
            roles = [cls._role(c, row, p["id"]) for p in people]
            if any(
                r["ready_at"] and not cls._ready_access(c, row, p, at)
                for p, r in zip(people, roles, strict=True)
            ):
                # 哥哥，已经报告的变化需要公开撤回准备状态，不能继续拿旧口头承诺冒充许可。
                for p, r in zip(people, roles, strict=True):
                    if r["ready_at"] and not cls._ready_access(c, row, p, at):
                        cls._record(
                            c,
                            row,
                            at,
                            "readiness_withdrawn",
                            "一方已报告的完整住房条件发生变化，需重新核对筹备；个人细节仍属本人。",
                        )
                        c.execute(
                            "UPDATE journey_lodging_roles SET status='blocked',ready_at=NULL,report_event_id=NULL,private_reason='已报告的完整住房条件发生变化' WHERE plan_id=? AND character_id=?",
                            (row["id"], p["id"]),
                        )
                        changed += 1
                continue
            if not all(r["ready_at"] for r in roles):
                continue
            if (
                not all(cls._at_stop(c, row, p) for p in people)
                or great_circle_distance_km(
                    people[0]["longitude"],
                    people[0]["latitude"],
                    people[1]["longitude"],
                    people[1]["latitude"],
                )
                > 0.02
            ):
                continue
            cls._finish(
                c,
                row,
                at,
                "completed",
                "双方在原室外站点实际核对完成了各自住房筹备；分房休息、进入对方房间和恢复旅行仍分别确认。",
            )
            changed += 1
        return changed

    @classmethod
    def rows(cls, c, wid, cid, at, other=None):
        result = []
        args = [wid, cid, cid] + ([other, other] if other else [])
        for found in c.execute(
            "SELECT p.*,l.name location_name FROM journey_lodging_plans p JOIN locations l ON l.id=p.location_id "
            "WHERE p.world_id=? AND (p.proposer_id=? OR p.recipient_id=?)"
            + (" AND (p.proposer_id=? OR p.recipient_id=?)" if other else "")
            + " ORDER BY CASE WHEN p.status IN ('pending','accepted') THEN 0 ELSE 1 END,p.rowid DESC LIMIT 12",
            args,
        ):
            row = dict(found)
            if row["status"] in {"pending", "accepted"} and at > from_iso(row["ready_by_at"]):
                row["status"] = "expired"
            row["other_id"] = (
                row["recipient_id"] if cid == row["proposer_id"] else row["proposer_id"]
            )
            parts = []
            for found_role in c.execute(
                "SELECT r.*,m.name room_name FROM journey_lodging_roles r JOIN life_rooms m ON m.id=r.room_id WHERE r.plan_id=? ORDER BY r.character_id",
                (row["id"],),
            ):
                role = dict(found_role)
                if role["character_id"] == cid:
                    quote = c.execute(
                        "SELECT * FROM journey_lodging_quotes WHERE id=?", (role["quote_id"],)
                    ).fetchone()
                    role["quote"] = (
                        {
                            k: quote[k]
                            for k in (
                                "id",
                                "owner_id",
                                "room_id",
                                "request_id",
                                "total",
                                "duration_days",
                                "quoted_rate",
                                "review_status",
                                "decision_basis",
                                "expires_at",
                                "status",
                            )
                        }
                        if quote
                        else None
                    )
                    if (
                        role["quote"]
                        and role["quote"]["status"] == "open"
                        and at >= from_iso(role["quote"]["expires_at"])
                    ):
                        role["quote"]["status"] = "expired"
                else:
                    role = {
                        k: role[k]
                        for k in (
                            "character_id",
                            "room_id",
                            "room_name",
                            "spending_limit",
                            "ready_at",
                            "report_event_id",
                        )
                    }
                    role["status"] = "ready" if role["ready_at"] else "unreported"
                parts.append(role)
            row["roles"] = parts
            result.append(row)
        from world_engine.intimacy import IntimacyService

        IntimacyService.mask_records(c, wid, cid, result, ("statement", "response", "reason"))
        return result

    @classmethod
    def handoff_sources(cls, c, wid, cid, other, at):
        """哥哥，只给仍在原停留的共同订房事实，不带任何人的私人账务。"""
        from world_engine.separate_stays import SeparateStayService

        candidates = c.execute(
            "SELECT p.* FROM journey_lodging_plans p JOIN joint_journeys j ON j.id=p.journey_id "
            "JOIN journey_stops s ON s.journey_id=p.journey_id AND s.stop_index=p.stop_index "
            "WHERE p.world_id=? AND p.status='completed' AND p.required_until_at>? "
            "AND ((p.proposer_id=? AND p.recipient_id=?) OR (p.proposer_id=? AND p.recipient_id=?)) "
            "AND j.status='paused' AND j.phase='stopped' AND j.next_index=p.stop_index+1 "
            "AND j.deadline_at>? AND EXISTS (SELECT 1 FROM locations l WHERE l.id=p.location_id AND l.world_id=p.world_id AND l.is_active=1) "
            "AND j.source_event_id=p.pause_event_id AND s.source_event_id=p.arrival_event_id "
            "AND s.arrived_at IS NOT NULL AND s.location_id=p.location_id "
            "AND NOT EXISTS (SELECT 1 FROM journey_separate_stays a WHERE a.source_lodging_plan_id=p.id "
            "AND (a.status IN ('pending','accepted','active') OR a.status='completed' AND "
            "(a.reunion_status='waiting' OR a.reunion_status='met' AND a.reunion_wait_ended_at IS NULL AND a.reunion_ends_at>?))) "
            "ORDER BY p.rowid DESC LIMIT 4",
            (wid, to_iso(at), cid, other, other, cid, to_iso(at), to_iso(at)),
        ).fetchall()
        for row in candidates:
            try:
                source = SeparateStayService._completed_lodging(c, wid, row["id"], cid, other, at)
            except ValueError:
                continue
            value = {k: source[k] for k in (
                "id", "journey_id", "proposer_id", "recipient_id", "own_room_id",
                "other_room_id", "required_until_at",
            )}
            value["completion_event_id"] = source["source_event_id"]
            return [value]
        return []

    @classmethod
    def context(cls, c, wid, cid, other, at):
        result = []
        for row in cls.rows(c, wid, cid, at, other):
            if row["status"] not in {"pending", "accepted"}:
                continue
            value = {
                k: row[k]
                for k in (
                    "id",
                    "proposer_id",
                    "recipient_id",
                    "journey_id",
                    "location_id",
                    "status",
                    "required_until_at",
                    "ready_by_at",
                )
            }
            value["roles"] = []
            for r in row["roles"]:
                part = {
                    k: r[k]
                    for k in (
                        "character_id",
                        "room_id",
                        "room_name",
                        "spending_limit",
                        "status",
                        "ready_at",
                    )
                }
                if r["character_id"] == cid:
                    part.update({k: r[k] for k in ("spent", "private_reason")})
                    if r["quote"]:
                        part["quote"] = {
                            k: r["quote"][k]
                            for k in ("id", "total", "duration_days", "status", "expires_at")
                        }
                value["roles"].append(part)
            result.append(value)
        return result[:2]

    @classmethod
    def planned_minutes(cls, c, actor, at):
        row = c.execute(
            "SELECT p.*,r.ready_at FROM journey_lodging_plans p JOIN journey_lodging_roles r ON r.plan_id=p.id "
            "WHERE p.world_id=? AND r.character_id=? AND p.status='accepted' AND p.ready_by_at>=? ORDER BY p.ready_by_at LIMIT 1",
            (actor["world_id"], actor["id"], to_iso(at)),
        ).fetchone()
        if not row:
            return None
        if row["ready_at"]:
            return 0
        from world_engine.appointment_rooms import AppointmentRoomService
        from world_engine.schedules import ScheduleService

        place = cls._place(c, row)
        arrival = ScheduleService.earliest_arrival(c, actor, place, at)
        doors = AppointmentRoomService.transition_minutes(
            c, actor["world_id"], actor["current_room_id"], None
        )
        return max(
            0, (from_iso(row["ready_by_at"]) - max(at, arrival)).total_seconds() / 60 - doors - 5
        )

    @classmethod
    def perform(cls, c, actor, at):
        from world_engine.daily_life import DailyLifeService
        from world_engine.families import FamilyService
        from world_engine.schedules import ScheduleService

        if (
            actor["is_player"]
            or actor["health"] < 80
            or actor["satiety"] <= 30
            or actor["energy"] <= 25
            or FamilyService.hungry_dependent(c, actor, at)
        ):
            return None
        if (
            LifeActivityService.running(c, actor["id"])
            or c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (actor["id"],),
            ).fetchone()
        ):
            return None
        cls.maintain(c, actor["world_id"], at)
        found = c.execute(
            "SELECT p.* FROM journey_lodging_plans p JOIN journey_lodging_roles r ON r.plan_id=p.id "
            "WHERE p.world_id=? AND p.status='accepted' AND r.character_id=? AND (r.retry_at IS NULL OR r.retry_at<=?) ORDER BY p.ready_by_at LIMIT 1",
            (actor["world_id"], actor["id"], to_iso(at)),
        ).fetchone()
        if not found:
            return None
        row = dict(found)
        role = cls._role(c, row, actor["id"])
        if role["ready_at"]:
            if not cls._at_stop(c, row, actor):
                return DailyLifeService._travel(c, actor, at, cls._place(c, row))
            return "已在原室外站点报告住房就绪，等候旅伴共同核对"
        appointment = ScheduleService.assess(c, actor, at)
        if appointment and appointment["due"]:
            return None
        other = next(p for p in cls._people(c, row) if p["id"] != actor["id"])
        try:
            if cls._ready_access(c, row, actor, at):
                if not cls._at_stop(c, row, actor):
                    return DailyLifeService._travel(c, actor, at, cls._place(c, row))
                if not cls._at_stop(c, row, other):
                    c.execute(
                        "UPDATE journey_lodging_roles SET retry_at=? WHERE plan_id=? AND character_id=?",
                        (to_iso(at + timedelta(minutes=5)), row["id"], actor["id"]),
                    )
                    return "本人已备妥住房，在原室外站点等候旅伴实际会合后报告"
                return cls.apply(
                    c,
                    actor,
                    other,
                    at,
                    LodgingPlanIntent(
                        operation="report_ready",
                        plan_id=row["id"],
                        statement="我自己的指定房间已经具备完整许可，之后是否住宿仍另行确认。",
                    ),
                )["summary"]
            if not cls._at_stop(c, row, actor):
                # 哥哥，只回本人已到过的原站点，不读取房主真坐标追踪陌生人。
                return DailyLifeService._travel(c, actor, at, cls._place(c, row))
            quote = c.execute(
                "SELECT * FROM journey_lodging_quotes WHERE id=? AND status='open' AND expires_at>?",
                (role["quote_id"], to_iso(at)),
            ).fetchone()
            if quote:
                return cls.apply(
                    c,
                    actor,
                    other,
                    at,
                    LodgingPlanIntent(
                        operation="confirm",
                        plan_id=row["id"],
                        quote_id=quote["id"],
                        expected_total=quote["total"],
                        statement="我在本人已接受的限额和生活储备内，明确确认这份准确报价，以本人钱包租住本人房间。",
                    ),
                )["summary"]
            result = cls.apply(
                c,
                actor,
                other,
                at,
                LodgingPlanIntent(
                    operation="quote",
                    plan_id=row["id"],
                    statement="我只为本人房间询问完整租期的准确报价，尚未付款。",
                ),
            )
            # 哥哥，每次日程只结算一步；报价后下一次才依据本人限额确认付款。
            c.execute(
                "UPDATE journey_lodging_roles SET retry_at=? WHERE plan_id=? AND character_id=?",
                (
                    to_iso(at + timedelta(minutes=1 if result["status"] == "offer_ready" else 15)),
                    row["id"],
                    actor["id"],
                ),
            )
            return result["summary"]
        except ValueError as exc:
            c.execute(
                "UPDATE journey_lodging_roles SET status='blocked',last_attempt_at=?,retry_at=?,private_reason=? WHERE plan_id=? AND character_id=?",
                (to_iso(at), to_iso(at + timedelta(minutes=15)), str(exc), row["id"], actor["id"]),
            )
            return None

    @staticmethod
    def blocks_journey(c, journey_id, at):
        return bool(
            c.execute(
                "SELECT 1 FROM journey_lodging_plans WHERE journey_id=? AND status IN ('pending','accepted') AND ready_by_at>=?",
                (journey_id, to_iso(at)),
            ).fetchone()
        )

    @staticmethod
    def has_live(c, wid):
        return bool(
            c.execute(
                "SELECT 1 FROM journey_lodging_plans WHERE world_id=? AND status IN ('pending','accepted')",
                (wid,),
            ).fetchone()
        )

    @staticmethod
    def boundary(c, wid, at, result):
        for row in c.execute(
            "SELECT p.status,p.ready_by_at,r.retry_at,q.expires_at FROM journey_lodging_plans p "
            "LEFT JOIN journey_lodging_roles r ON r.plan_id=p.id LEFT JOIN journey_lodging_quotes q ON q.id=r.quote_id AND q.status='open' "
            "WHERE p.world_id=? AND p.status IN ('pending','accepted')",
            (wid,),
        ):
            values = [from_iso(row["ready_by_at"]) + timedelta(microseconds=1)]
            if row["status"] == "accepted":
                values.append(at + timedelta(minutes=5))
            values += [from_iso(row[k]) for k in ("retry_at", "expires_at") if row[k]]
            result = min([result] + [v for v in values if v > at])
        return result
