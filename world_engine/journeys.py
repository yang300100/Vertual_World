"""哥哥，共同旅行先确认有限行程，每一程都走真实道路，双方保留退出权。"""

from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.geo import great_circle_distance_km
from world_engine.life import LifeActivityService
from world_engine.proximity import same_room
from world_engine.repository import from_iso, to_iso

JOURNEY_SCHEMA = """
CREATE TABLE IF NOT EXISTS joint_journeys (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 proposer_id TEXT NOT NULL REFERENCES characters(id),recipient_id TEXT NOT NULL REFERENCES characters(id),
 leader_id TEXT REFERENCES characters(id) CHECK(leader_id IS NULL OR leader_id=proposer_id OR leader_id=recipient_id),
 follow_authorized_by TEXT REFERENCES characters(id),follow_authorized_at TEXT,
 follow_source_event_id TEXT REFERENCES world_events(id),follow_terms_json TEXT,
 status TEXT NOT NULL CHECK(status IN ('pending','accepted','active','paused','resume_pending','completed','declined','ended','expired')),
 phase TEXT NOT NULL DEFAULT 'ready' CHECK(phase IN ('ready','moving','stopped')),
 title TEXT NOT NULL,statement TEXT NOT NULL,response TEXT NOT NULL DEFAULT '',
 origin_id TEXT NOT NULL REFERENCES locations(id),deadline_at TEXT NOT NULL,proposed_at TEXT NOT NULL,
 started_at TEXT,completed_at TEXT,next_index INTEGER NOT NULL DEFAULT 0,next_depart_at TEXT,wait_until TEXT,remaining_stop_seconds REAL NOT NULL DEFAULT 0,
 resume_requested_by TEXT REFERENCES characters(id),meet_wait_until TEXT,preparation_plan_id TEXT REFERENCES shared_preparation_plans(id),
 source_event_id TEXT NOT NULL REFERENCES world_events(id),reason TEXT NOT NULL DEFAULT '',CHECK(proposer_id<>recipient_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_journey_pending_pair ON joint_journeys(world_id,MIN(proposer_id,recipient_id),MAX(proposer_id,recipient_id)) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_journey_live ON joint_journeys(world_id,status,deadline_at);
CREATE TABLE IF NOT EXISTS journey_stops (
 journey_id TEXT NOT NULL REFERENCES joint_journeys(id) ON DELETE CASCADE,stop_index INTEGER NOT NULL,
 location_id TEXT NOT NULL REFERENCES locations(id),minutes INTEGER NOT NULL CHECK(minutes BETWEEN 0 AND 120),
 is_return INTEGER NOT NULL DEFAULT 0,arrived_at TEXT,source_event_id TEXT REFERENCES world_events(id),PRIMARY KEY(journey_id,stop_index)
);
CREATE TABLE IF NOT EXISTS journey_legs (
 id TEXT PRIMARY KEY,journey_id TEXT NOT NULL REFERENCES joint_journeys(id) ON DELETE CASCADE,stop_index INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('moving','arrived','cancelled')),
 player_movement_id TEXT NOT NULL REFERENCES character_movements(id),npc_movement_id TEXT NOT NULL REFERENCES character_movements(id),
 started_at TEXT NOT NULL,finished_at TEXT,source_event_id TEXT NOT NULL REFERENCES world_events(id)
);
"""


class JourneyStop(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location_id: str = Field(min_length=1, max_length=100)
    minutes: int = Field(default=15, ge=0, le=120, strict=True)


class JourneyIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: Literal["propose", "accept", "decline", "depart", "pause", "resume", "end", "follow", "withdraw_follow"]
    journey_id: str | None = Field(default=None, min_length=1, max_length=100)
    leader_id: str | None = Field(default=None, min_length=1, max_length=100)
    title: str = Field(default="共同旅行", min_length=1, max_length=60)
    meeting_location_id: str | None = Field(default=None, min_length=1, max_length=100)
    deadline_world_time: str | None = None
    stops: list[JourneyStop] = Field(default_factory=list, max_length=6)
    return_to_origin: bool = Field(default=False, strict=True)
    preparation_plan_id: str | None = Field(default=None, min_length=1, max_length=100)
    statement: str = Field(min_length=1, max_length=240)

    @model_validator(mode="before")
    @classmethod
    def defaults(cls, value):
        if isinstance(value, dict):
            value = {
                k: v
                for k, v in value.items()
                if not (k in {"title", "stops", "return_to_origin"} and v is None)
            }
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.operation == "propose":
            if (
                self.journey_id
                or not self.meeting_location_id
                or not self.deadline_world_time
                or not self.stops
            ):
                raise ValueError("旅行需要集合地点、明确期限和有限站点")
        elif (
            not self.journey_id
            or self.stops
            or self.meeting_location_id
            or self.deadline_world_time
            or self.return_to_origin
            or self.preparation_plan_id
            or self.leader_id
        ):
            raise ValueError("回应行程只引用真实编号，不能悄悄变更路线或返程承诺")
        return self


class JourneyService:
    @staticmethod
    def active(c, wid, cid):
        return c.execute(
            "SELECT * FROM joint_journeys WHERE world_id=? AND status='active' AND (proposer_id=? OR recipient_id=?)",
            (wid, cid, cid),
        ).fetchone()

    @staticmethod
    def places(c, wid, cid):
        return [
            dict(r)
            for r in c.execute(
                "SELECT l.id,l.name FROM locations l JOIN characters a ON a.id=? WHERE l.world_id=? AND l.is_active=1 AND l.space_layer=a.space_layer AND (l.id=COALESCE(a.current_location_id,a.location_id) OR l.id IN (SELECT e.location_id FROM world_events e JOIN event_observers o ON o.event_id=e.id WHERE e.world_id=? AND o.character_id=? AND o.channel='participant' AND e.occurred_at<=(SELECT w.current_time FROM worlds w WHERE w.id=?))) ORDER BY (l.id=COALESCE(a.current_location_id,a.location_id)) DESC,l.name LIMIT 40",
                (cid, wid, wid, cid, wid),
            )
        ]

    @staticmethod
    def rows(c, wid, cid, at, other=None):
        args = [wid, cid, cid] + ([other, other] if other else [])
        result = []
        for row in c.execute(
            "SELECT j.*,l.name origin_name FROM joint_journeys j JOIN locations l ON l.id=j.origin_id WHERE j.world_id=? AND (j.proposer_id=? OR j.recipient_id=?)"
            + (" AND (j.proposer_id=? OR j.recipient_id=?)" if other else "")
            + " ORDER BY CASE WHEN j.status IN ('pending','accepted','active','paused','resume_pending') THEN 0 ELSE 1 END,j.rowid DESC LIMIT 12",
            args,
        ):
            value = dict(row)
            from world_engine.journey_follow import JourneyFollowService

            value.update(JourneyFollowService.shared(c, row, cid, at))
            value.pop("follow_terms_json", None)
            value.pop("meet_wait_until", None)
            value["other_id"] = (
                row["recipient_id"] if row["proposer_id"] == cid else row["proposer_id"]
            )
            if (
                row["status"] not in {"completed", "declined", "ended", "expired"}
                and from_iso(row["deadline_at"]) <= at
            ):
                value["status"] = "expired"
            value["stops"] = [
                dict(s)
                for s in c.execute(
                    "SELECT s.*,l.name FROM journey_stops s JOIN locations l ON l.id=s.location_id WHERE s.journey_id=? ORDER BY s.stop_index",
                    (row["id"],),
                )
            ]
            value["breaks"] = [dict(b) for b in c.execute(
                "SELECT b.moment_id,b.stop_index,m.kind,CASE WHEN m.status='pending' AND m.expires_at<=? THEN 'cancelled' ELSE m.status END status,m.minutes FROM journey_breaks b "
                "JOIN shared_life_moments m ON m.id=b.moment_id WHERE b.journey_id=? ORDER BY m.rowid DESC LIMIT 8",
                (to_iso(at),row["id"]),
            )]
            result.append(value)
        from world_engine.intimacy import IntimacyService

        IntimacyService.mask_records(
            c, wid, cid, result, ("title", "statement", "response", "reason")
        )
        return result

    @classmethod
    def context(cls, c, wid, cid, other, at):
        return [
            {
                k: p[k]
                for k in (
                    "id",
                    "proposer_id",
                    "recipient_id",
                    "status",
                    "phase",
                    "title",
                    "origin_id",
                    "origin_name",
                    "deadline_at",
                    "next_index",
                    "resume_requested_by",
                    "leader_id",
                    "leader_name",
                    "following",
                    "can_follow",
                    "follow_authorized_by",
                    "follow_authorized_at",
                    "follow_source_event_id",
                )
            }
            | {"route": " → ".join(f"{s['name']}(停留{s['minutes']}分)" for s in p["stops"]),
               "breaks": [b for b in p["breaks"] if b["status"] in {"pending","active"}]}
            for p in cls.rows(c, wid, cid, at, other)
            if p["status"] in {"pending", "accepted", "active", "paused", "resume_pending"}
        ][:2]

    @staticmethod
    def _people(c, row):
        pair = [
            c.execute("SELECT * FROM characters WHERE id=?", (row[k],)).fetchone()
            for k in ("proposer_id", "recipient_id")
        ]
        return sorted(pair, key=lambda a: not bool(a["is_player"]))

    @classmethod
    def perform(cls, c, npc, at):
        from world_engine.daily_life import DailyLifeService
        from world_engine.journey_follow import JourneyFollowService
        from world_engine.schedules import ScheduleService

        row = c.execute(
            "SELECT * FROM joint_journeys WHERE world_id=? AND status IN ('accepted','active') "
            "AND (proposer_id=? OR recipient_id=?) ORDER BY rowid LIMIT 1",
            (npc["world_id"], npc["id"], npc["id"]),
        ).fetchone()
        if not row:
            return None
        leads = JourneyFollowService.leader_id(c, row) == npc["id"]
        authorized = leads and JourneyFollowService.valid(c, row, at)
        if (
            npc["health"] < 40
            or npc["satiety"] <= 30
            or npc["energy"] <= 25
            or DailyLifeService.is_sleep_time(npc, at)
        ):
            if authorized:
                cls.pause(c, row, at, "领队需要先处理身体、用饭或作息，跟随暂停后须本人重新选择。")
            return None
        plan = ScheduleService.assess(c, npc, at)
        if plan and plan["due"]:
            if authorized:
                cls.pause(c, row, at, "领队需要准备已有约会，跟随暂停后须本人重新选择。")
            return None
        if row["status"] == "active":
            if not authorized:
                return None
            if row["phase"] == "moving":
                return "正在按玩家本人明确选择的有限路线带路"
            if row["next_depart_at"] and at < from_iso(row["next_depart_at"]):
                return "按约定停留，完整停留后再继续带路"
            if not c.execute(
                "SELECT 1 FROM journey_stops WHERE journey_id=? AND stop_index=?",
                (row["id"], row["next_index"]),
            ).fetchone():
                return None
            return cls._lead_depart(c, row, npc, at)
        origin = cls._place(c, npc["world_id"], row["origin_id"])
        if (
            npc["current_room_id"]
            or great_circle_distance_km(
                npc["longitude"], npc["latitude"], origin["longitude"], origin["latitude"]
            )
            > 0.025
        ):
            return DailyLifeService._travel(c, npc, at, origin)
        if authorized:
            return cls._lead_depart(c, row, npc, at)
        return "已到集合处，等候玩家本人明确选择跟随本次有限路线" if leads else "已到共同旅行的集合处，暂候对方本人选择出发"

    @classmethod
    def _lead_depart(cls, c, row, npc, at):
        player = next(person for person in cls._people(c, row) if person["is_player"])
        try:
            cls.apply(c, npc, player, at, JourneyIntent(
                operation="depart", journey_id=row["id"],
                statement="我按双方已确认、且玩家本人仍选择跟随的有限路线，开始下一程带路。",
            ))
        except ValueError as exc:
            latest = c.execute("SELECT * FROM joint_journeys WHERE id=?", (row["id"],)).fetchone()
            cls.pause(c, latest, at, "本程真实出发条件已变化，需要重新商量。" + str(exc))
            return "本程暂不能实际带路，已暂停并释放后续跟随"
        return "领队已按共同确认的路线开始实际带路，尚未抵达"

    @staticmethod
    def _record(c, first, second, at, state, summary, payload):
        from world_engine.appointments import AppointmentService

        return AppointmentService._record(
            c, first, second, at, "journey_" + state, summary, payload
        )

    @staticmethod
    def _place(c, wid, lid):
        row = c.execute(
            "SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1", (lid, wid)
        ).fetchone()
        if not row:
            raise ValueError("行程地点不存在或已不可用")
        return row

    @classmethod
    def _estimate(cls, c, people, stops, origins):
        from world_engine.movement import MovementService

        mover = MovementService()
        speed = min(float(p["movement_speed_kmh"]) for p in people)
        if speed <= 0:
            raise ValueError("当前没有可用徒步速度")
        total = 0
        for stop in stops:
            place = cls._place(c, people[0]["world_id"], stop["location_id"])
            costs = []
            for person, origin in zip(people, origins, strict=True):
                shadow = {**dict(person), "longitude": origin[0], "latitude": origin[1]}
                if place["space_layer"] != person["space_layer"]:
                    raise ValueError("本次徒步行程须在同一空间层，跨层需要另行安排")
                route = mover._plan_route(
                    c,
                    world_id=person["world_id"],
                    actor=shadow,
                    destination_longitude=place["longitude"],
                    destination_latitude=place["latitude"],
                    movement_type="land",
                    speed_kmh=speed,
                    selected_vehicle_id=None,
                )
                costs.append(
                    route.estimated_hours
                    if route
                    else great_circle_distance_km(*origin, place["longitude"], place["latitude"])
                    / speed
                )
            total += max(costs) * 3600 + stop["minutes"] * 60
            origins = [(place["longitude"], place["latitude"])] * 2
        return total

    @classmethod
    def _assert_breaks_finished(cls,c,row,at):
        from world_engine.lodging_plans import LodgingPlanService
        if LodgingPlanService.blocks_journey(c, row["id"], at):
            raise ValueError("请先处理本人已同意的订房筹备，或明确退出该筹备，再商量恢复旅行")
        from world_engine.separate_stays import SeparateStayService
        if SeparateStayService.blocks_journey(c, row["id"], at):
            raise ValueError("请先完成分房休息和约定的实际会合，或明确退出该安排，再商量恢复旅行")
        if c.execute("SELECT 1 FROM journey_overnight_stays WHERE journey_id=? AND (status IN ('accepted','active') OR (status='pending' AND arrival_starts_at>?))",(row["id"],to_iso(at))).fetchone():
            raise ValueError("请先处理关联的长时住宿并实际回到室外站点，再商量恢复旅行")
        from world_engine.journey_room_appointments import JourneyRoomService
        JourneyRoomService.assert_finished(c,row["id"],at)
        if c.execute(
            "SELECT 1 FROM journey_breaks b JOIN shared_life_moments m ON m.id=b.moment_id "
            "WHERE b.journey_id=? AND (m.status='active' OR (m.status='pending' AND m.expires_at>?))",
            (row["id"],to_iso(at)),
        ).fetchone():
            raise ValueError("请先处理旅行停留中待回应或进行中的用餐休息，再商量恢复和出发")

    @classmethod
    def _ready(cls, c, row, at, *, depart=False):
        from world_engine.daily_life import DailyLifeService
        from world_engine.families import FamilyService
        from world_engine.schedules import ScheduleService
        from world_engine.shared_life import SharedLifeService

        people = cls._people(c, row)
        cls._assert_breaks_finished(c,row,at)
        if not SharedLifeService._near(*people, "talk"):
            raise ValueError("请双方先实际会合到同一室外场景20米内")
        for person in people:
            from world_engine.group_journeys import GroupJourneyService
            GroupJourneyService.assert_available(c, person)
            if (
                person["current_room_id"]
                or person["current_fixture_id"]
                or person["active_vehicle_id"]
                or person["movement_type"] != "land"
            ):
                raise ValueError("本次行程只支持各自徒步，请先起身并实际离开室内")
            if person["health"] < 40 or person["energy"] <= 20 or person["satiety"] <= 30:
                raise ValueError("一方需要先处理身体、用饭或休息，不能强行出发")
            LifeActivityService.assert_available(c, person["id"])
            if c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (person["id"],),
            ).fetchone():
                raise ValueError("请先结束当前移动，再商量共同出发")
            if c.execute(
                "SELECT 1 FROM player_steering WHERE character_id=? AND (dx<>0 OR dy<>0)",
                (person["id"],),
            ).fetchone():
                raise ValueError("请先停止方向移动")
            if c.execute(
                "SELECT 1 FROM companion_sessions WHERE status='active' AND (leader_id=? OR companion_id=?)",
                (person["id"], person["id"]),
            ).fetchone():
                raise ValueError("请先处理已有的短时同行")
            if not person["is_player"] and (
                DailyLifeService.is_sleep_time(person, at)
                or FamilyService.hungry_dependent(c, person, at)
            ):
                raise ValueError("对方需要先处理作息或已承担的照护")
        if row["started_at"] is None:
            origin = cls._place(c, row["world_id"], row["origin_id"])
            if any(
                great_circle_distance_km(
                    p["longitude"], p["latitude"], origin["longitude"], origin["latitude"]
                )
                > 0.025
                for p in people
            ):
                raise ValueError("请双方实际到约定集合地点，再出发")
        elif row["phase"] == "stopped":
            previous = c.execute(
                "SELECT location_id FROM journey_stops WHERE journey_id=? AND stop_index=?",
                (row["id"], row["next_index"] - 1),
            ).fetchone()
            site = cls._place(c, row["world_id"], previous[0])
            if any(
                great_circle_distance_km(
                    p["longitude"], p["latitude"], site["longitude"], site["latitude"]
                )
                > 0.025
                for p in people
            ):
                raise ValueError("请先实际回到上次停留站点，再恢复这段行程")
        stops = c.execute(
            "SELECT * FROM journey_stops WHERE journey_id=? AND stop_index>=? ORDER BY stop_index",
            (row["id"], row["next_index"]),
        ).fetchall()
        seconds = cls._estimate(c, people, stops, [(p["longitude"], p["latitude"]) for p in people])
        if row["status"] == "resume_pending" and row["phase"] == "stopped":
            seconds += row["remaining_stop_seconds"]
        if at + timedelta(seconds=seconds) > from_iso(row["deadline_at"]):
            raise ValueError("按当前路程与停留时长，已无法在约定期限内完成")
        final = cls._place(
            c,
            row["world_id"],
            stops[-1]["location_id"]
            if stops
            else c.execute(
                "SELECT location_id FROM journey_stops WHERE journey_id=? ORDER BY stop_index DESC LIMIT 1",
                (row["id"],),
            ).fetchone()[0],
        )
        for person in people:
            plan = ScheduleService.assess(c, person, at)
            if plan and plan["depart_at"] <= at + timedelta(seconds=seconds):
                raise ValueError("剩余路线与已经确认的赴约安排冲突，需要另行商量")
            from world_engine.separate_stays import SeparateStayService
            if SeparateStayService.new_journey_conflict(c, person, at, at + timedelta(seconds=seconds), final["id"]):
                raise ValueError("剩余路线与已同意的分房住宿或休息后会合冲突，请先重新商量")
            for date in c.execute(
                "SELECT * FROM social_appointments WHERE world_id=? AND status IN ('accepted','active') AND (proposer_id=? OR recipient_id=?)",
                (row["world_id"], person["id"], person["id"]),
            ):
                onward = cls._estimate(
                    c,
                    (person, person),
                    [{"location_id": date["location_id"], "minutes": 0}],
                    [(final["longitude"], final["latitude"])] * 2,
                )
                if at + timedelta(seconds=seconds + onward) >= from_iso(date["starts_at"]):
                    raise ValueError("一方无法在本次旅行后及时赶赴已有约定，请先重新商量")
            from world_engine.appointment_rooms import AppointmentRoomService

            for stay in c.execute(
                "SELECT * FROM journey_overnight_stays WHERE world_id=? AND status IN ('accepted','active') "
                "AND (proposer_id=? OR recipient_id=?)",
                (row["world_id"], person["id"], person["id"]),
            ):
                # 哥哥，住宿承诺独立于原旅行；新路线结束后，还要留出真实赶路、逐门入屋和准备的时间。
                onward = cls._estimate(
                    c,
                    (person, person),
                    [{"location_id": stay["location_id"], "minutes": 0}],
                    [(final["longitude"], final["latitude"])] * 2,
                )
                doorway = AppointmentRoomService.transition_minutes(
                    c, row["world_id"], None, stay["room_id"]
                )
                ready = at + timedelta(seconds=seconds + onward, minutes=10 + doorway)
                if ready > from_iso(stay["arrival_starts_at"]):
                    raise ValueError("本次旅行及返程入屋会占用已确认的长时住宿安排，请先重新商量")
        return people

    @classmethod
    def apply(cls, c, actor, other, at, choice):
        c.execute("SAVEPOINT joint_journey_choice")
        try:
            return cls._apply(c, actor, other, at, choice)
        except Exception:
            c.execute("ROLLBACK TO joint_journey_choice")
            raise
        finally:
            c.execute("RELEASE joint_journey_choice")

    @classmethod
    def _apply(cls, c, actor, other, at, choice):
        from world_engine.intimacy import IntimacyService

        if (
            other is None
            or actor["world_id"] != other["world_id"]
            or bool(actor["is_player"]) == bool(other["is_player"])
        ):
            raise ValueError("请选择本世界与玩家同行的另一位人物")
        wid = actor["world_id"]
        cls.maintain(c, wid, at)
        if choice.operation == "propose":
            if actor["health"] <= 0 or other["health"] <= 0:
                raise ValueError("当前身体状态无法参与共同旅行")
            IntimacyService._reachable(c, actor, other)
            if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                raise ValueError("一方仍要求留出空间，暂不提出共同旅行")
            try:
                end = datetime.fromisoformat(choice.deadline_world_time)
            except (ValueError, TypeError) as exc:
                raise ValueError("请给出带时区的旅行期限") from exc
            if end.tzinfo is None or not at + timedelta(minutes=30) <= end <= at + timedelta(
                days=7
            ):
                raise ValueError("旅行期限须在半小时至七个世界日内并带时区")
            ids = [choice.meeting_location_id] + [s.location_id for s in choice.stops]
            player = actor if actor["is_player"] else other
            leader_id = choice.leader_id or player["id"]
            if leader_id not in {actor["id"], other["id"]}:
                raise ValueError("本次领队必须是共同参与这份有限行程的本人或另一方")
            for lid in ids:
                place = cls._place(c, wid, lid)
                if place["space_layer"] != actor["space_layer"] or (
                    lid != (actor["current_location_id"] or actor["location_id"])
                    and not c.execute(
                        "SELECT 1 FROM world_events e JOIN event_observers o ON o.event_id=e.id WHERE e.world_id=? AND e.location_id=? AND o.character_id=? AND o.channel='participant' AND e.occurred_at<=? LIMIT 1",
                        (wid, lid, actor["id"], to_iso(at)),
                    ).fetchone()
                ):
                    raise ValueError("提议只能使用本人当前所在或确实到访过的地点")
            if any(a == b for a, b in zip(ids, ids[1:], strict=False)):
                raise ValueError("相邻站点不能相同，请明确真正要走的路线")
            for first, second in zip(ids, ids[1:], strict=False):
                a, b = cls._place(c, wid, first), cls._place(c, wid, second)
                if (
                    great_circle_distance_km(
                        a["longitude"], a["latitude"], b["longitude"], b["latitude"]
                    )
                    < 0.05
                ):
                    raise ValueError("相邻站点没有足够实际路程，可以改为共同活动或选择更远地点")
            if c.execute(
                "SELECT 1 FROM joint_journeys WHERE world_id=? AND status='pending' AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
                (wid, actor["id"], other["id"], other["id"], actor["id"]),
            ).fetchone():
                raise ValueError("双方已有待回应的旅行提议")
            stops = [s.model_dump() for s in choice.stops]
            if choice.return_to_origin and stops[-1]["location_id"] != choice.meeting_location_id:
                stops.append(
                    {"location_id": choice.meeting_location_id, "minutes": 0, "is_return": 1}
                )
            if choice.preparation_plan_id:
                plan = c.execute(
                    "SELECT * FROM shared_preparation_plans WHERE id=? AND world_id=? AND status='completed' AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
                    (
                        choice.preparation_plan_id,
                        wid,
                        actor["id"],
                        other["id"],
                        other["id"],
                        actor["id"],
                    ),
                ).fetchone()
                if not plan:
                    raise ValueError("只能衔接双方已核对完成的筹备，不能借筹备代替旅行同意")
                if c.execute(
                    "SELECT 1 FROM joint_journeys WHERE preparation_plan_id=? AND (started_at IS NOT NULL OR status IN ('pending','accepted','active','paused','resume_pending'))",
                    (plan["id"],),
                ).fetchone():
                    raise ValueError("这份筹备已有后续旅行，不能重复兑现")
            origin = cls._place(c, wid, choice.meeting_location_id)
            estimate = cls._estimate(
                c, (actor, other), stops, [(origin["longitude"], origin["latitude"])] * 2
            )
            if at + timedelta(seconds=estimate) > end:
                raise ValueError("真实路线与停留时间超过旅行期限")
            jid = str(uuid4())
            event = cls._record(
                c,
                actor,
                other,
                at,
                "proposed",
                "提出有限徒步行程，等待对方独立同意：" + choice.statement,
                {
                    "journey_id": jid,
                    "stops": stops,
                    "deadline_at": to_iso(end),
                    "preparation_plan_id": choice.preparation_plan_id,
                    "leader_id": leader_id,
                },
            )
            c.execute(
                "INSERT INTO joint_journeys(id,world_id,proposer_id,recipient_id,leader_id,status,title,statement,origin_id,deadline_at,proposed_at,preparation_plan_id,source_event_id) VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?,?)",
                (
                    jid,
                    wid,
                    actor["id"],
                    other["id"],
                    leader_id,
                    choice.title,
                    choice.statement,
                    origin["id"],
                    to_iso(end),
                    to_iso(at),
                    choice.preparation_plan_id,
                    event,
                ),
            )
            for index, stop in enumerate(stops):
                c.execute(
                    "INSERT INTO journey_stops(journey_id,stop_index,location_id,minutes,is_return) VALUES (?,?,?,?,?)",
                    (jid, index, stop["location_id"], stop["minutes"], stop.get("is_return", 0)),
                )
            return {
                "status": "pending",
                "journey_id": jid,
                "event_id": event,
                "summary": "行程已提出，尚未取得对方同意，也没有移动或花费。",
            }
        row = c.execute(
            "SELECT * FROM joint_journeys WHERE id=? AND world_id=? AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
            (choice.journey_id, wid, actor["id"], other["id"], other["id"], actor["id"]),
        ).fetchone()
        if not row:
            raise ValueError("这份行程不属于双方")
        if choice.operation in {"follow", "withdraw_follow"}:
            from world_engine.journey_follow import JourneyFollowService

            return JourneyFollowService.apply(c, row, actor, other, at, choice)
        if choice.operation in {"accept", "decline"}:
            IntimacyService._reachable(c, actor, other)
            if row["status"] != "pending" or row["recipient_id"] != actor["id"]:
                raise ValueError("只有收到有效旅行提议的人可以回应")
            if choice.operation == "accept":
                from world_engine.journey_follow import JourneyFollowService
                from world_engine.group_journeys import GroupJourneyService
                for person in (actor, other):
                    GroupJourneyService.assert_available(c, person)

                # 哥哥，玩家提出领路请求时不窥探 NPC 私下见闻，领队本人答应时再核验。
                JourneyFollowService.assert_leader_knows(c, row, at)
                if actor["health"] <= 0 or other["health"] <= 0:
                    raise ValueError("当前身体状态无法确认共同旅行")
                if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                    raise ValueError("一方仍要求暂停联系")
                if c.execute(
                    "SELECT 1 FROM joint_journeys WHERE world_id=? AND status IN ('accepted','active','paused','resume_pending') AND (proposer_id IN (?,?) OR recipient_id IN (?,?))",
                    (wid, actor["id"], other["id"], actor["id"], other["id"]),
                ).fetchone():
                    raise ValueError("一方已有未结束的旅行安排")
            status = "accepted" if choice.operation == "accept" else "declined"
            summary = (
                "双方确认了有限行程，仍需实际会合；NPC 领队需玩家另行选择跟随，玩家领队仍本人逐程选择出发。"
                if status == "accepted"
                else "旅行提议已被婉拒。"
            )
        elif choice.operation == "depart":
            from world_engine.journey_follow import JourneyFollowService

            JourneyFollowService.require_departure(c, row, actor, at)
            if row["status"] not in {"accepted", "active"} or row["phase"] == "moving":
                raise ValueError("请先确认或恢复行程，不能重复开始正在走的一程")
            if row["next_depart_at"] and at < from_iso(row["next_depart_at"]):
                raise ValueError("请实际度过约定的停留时间，再开始下一程")
            if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                raise ValueError("一方仍要求暂停联系，不能出发")
            people = cls._ready(c, row, at)
            from world_engine.movement import MovementService

            target = c.execute(
                "SELECT s.*,l.longitude,l.latitude FROM journey_stops s JOIN locations l ON l.id=s.location_id WHERE s.journey_id=? AND s.stop_index=?",
                (row["id"], row["next_index"]),
            ).fetchone()
            if target is None:
                raise ValueError("路线已经走完，请实际完成最后的停留时间")
            mover = MovementService()
            cap = min(float(p["movement_speed_kmh"]) for p in people)
            if all(
                great_circle_distance_km(
                    p["longitude"], p["latitude"], target["longitude"], target["latitude"]
                )
                <= 0.001
                for p in people
            ):
                cls._mark_stop(c, row, people, at, None)
                updated = c.execute(
                    "SELECT * FROM joint_journeys WHERE id=?", (row["id"],)
                ).fetchone()
                return {
                    "status": updated["status"],
                    "journey_id": row["id"],
                    "event_id": updated["source_event_id"],
                    "summary": "双方已实际会合在本程站点，核对到站并记录停留，不重复走已经走过的路。",
                }
            movements = [
                mover._start_solo(
                    c,
                    world_id=wid,
                    character_id=p["id"],
                    destination_longitude=target["longitude"],
                    destination_latitude=target["latitude"],
                    world_time=at,
                    speed_cap=cap,
                    minimum_distance_km=0,
                )
                for p in people
            ]
            mover.synchronize(c, movements, at, from_iso(row["deadline_at"]))
            leg = str(uuid4())
            event = cls._record(
                c,
                actor,
                other,
                at,
                "departed",
                "双方按各自真实路线开始共同旅行的一程，尚未抵达。",
                {
                    "journey_id": row["id"],
                    "leg_id": leg,
                    "stop_index": row["next_index"],
                    "movement_ids": [m.id for m in movements],
                },
            )
            c.execute(
                "INSERT INTO journey_legs VALUES (?,?,?,'moving',?,?,?,NULL,?)",
                (
                    leg,
                    row["id"],
                    row["next_index"],
                    movements[0].id,
                    movements[1].id,
                    to_iso(at),
                    event,
                ),
            )
            c.execute(
                "UPDATE joint_journeys SET status='active',phase='moving',started_at=COALESCE(started_at,?),next_depart_at=NULL,wait_until=NULL,remaining_stop_seconds=0,source_event_id=?,reason='' WHERE id=?",
                (to_iso(at), event, row["id"]),
            )
            return {
                "status": "active",
                "journey_id": row["id"],
                "event_id": event,
                "summary": "双方已实际出发，沿各自路线按较慢速度前进，抵达后才能记为到站。",
            }
        elif choice.operation == "pause":
            if row["status"] not in {"active", "resume_pending"} and not (
                row["status"] == "accepted" and dict(row).get("follow_authorized_by")
            ):
                raise ValueError("当前没有进行中的旅行可以暂停")
            return cls.pause(c, row, at, choice.statement)
        elif choice.operation == "resume":
            IntimacyService._reachable(c, actor, other)
            cls._assert_breaks_finished(c,row,at)
            if row["status"] == "paused":
                c.execute(
                    "UPDATE joint_journeys SET resume_requested_by=? WHERE id=?",
                    (actor["id"], row["id"]),
                )
                status, summary = "resume_pending", "本人提出恢复行程，仍等待另一方明确同意。"
            elif row["status"] == "resume_pending" and row["resume_requested_by"] != actor["id"]:
                if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                    raise ValueError("一方仍要求留出空间")
                cls._ready(c, row, at)
                depart = at + timedelta(seconds=row["remaining_stop_seconds"])
                c.execute(
                    "UPDATE joint_journeys SET next_depart_at=?,wait_until=?,resume_requested_by=NULL WHERE id=?",
                    (
                        to_iso(depart) if row["phase"] == "stopped" else None,
                        to_iso(depart + timedelta(hours=1)),
                        row["id"],
                    ),
                )
                status, summary = "active", "双方明确恢复行程；NPC 领队仍需玩家重新选择跟随，玩家领队仍本人逐程选择出发。"
            else:
                raise ValueError("只能提出恢复已暂停行程，或回应另一方的恢复请求")
        else:
            if (
                row["status"] not in {"pending", "accepted", "active", "paused", "resume_pending"}
                or row["status"] == "pending"
                and row["proposer_id"] != actor["id"]
            ):
                raise ValueError("只能撤回本人提议，或退出已确认的旅行")
            cls._stop(c, row, at)
            status, summary = "ended", "一方结束共同旅行，保留各自真实位置、物品、支出与已走路程。"
        event = cls._record(
            c, actor, other, at, status, summary + choice.statement, {"journey_id": row["id"]}
        )
        c.execute(
            "UPDATE joint_journeys SET status=?,response=?,reason=?,source_event_id=? WHERE id=?",
            (status, choice.statement, summary, event, row["id"]),
        )
        from world_engine.journey_breaks import JourneyBreakService
        JourneyBreakService.expire_pending(c,wid,at)
        from world_engine.journey_room_appointments import JourneyRoomService
        JourneyRoomService.expire_pending(c,wid,at)
        from world_engine.overnight import OvernightService
        OvernightService.maintain(c,wid,at)
        return {"status": status, "journey_id": row["id"], "event_id": event, "summary": summary}

    @staticmethod
    def _stop(c, row, at):
        from world_engine.movement import MovementService
        from world_engine.journey_follow import JourneyFollowService

        JourneyFollowService.clear(c, row["id"])

        legs = c.execute(
            "SELECT * FROM journey_legs WHERE journey_id=? AND status='moving'", (row["id"],)
        ).fetchall()
        for leg in legs:
            for key in ("player_movement_id", "npc_movement_id"):
                movement = c.execute(
                    "SELECT character_id FROM character_movements WHERE id=? AND status='moving'",
                    (leg[key],),
                ).fetchone()
                if movement:
                    MovementService().cancel(
                        c,
                        world_id=row["world_id"],
                        character_id=movement[0],
                        world_time=at,
                        cascade_journey=False,
                    )
        c.execute(
            "UPDATE journey_legs SET status='cancelled',finished_at=? WHERE journey_id=? AND status='moving'",
            (to_iso(at), row["id"]),
        )

    @classmethod
    def pause(cls, c, row, at, reason):
        cls._stop(c, row, at)
        people = cls._people(c, row)
        event = cls._record(
            c, *people, at, "paused", "共同旅行已暂停：" + reason, {"journey_id": row["id"]}
        )
        remaining = (
            max(0, (from_iso(row["next_depart_at"]) - at).total_seconds())
            if row["phase"] == "stopped" and row["next_depart_at"]
            else 0
        )
        c.execute(
            "UPDATE joint_journeys SET status='paused',phase=CASE phase WHEN 'moving' THEN 'ready' ELSE phase END,remaining_stop_seconds=?,reason=?,source_event_id=? WHERE id=?",
            (remaining, reason, event, row["id"]),
        )
        return {
            "status": "paused",
            "journey_id": row["id"],
            "event_id": event,
            "summary": "行程已暂停，保留当前位置；恢复须双方重新同意。",
        }

    @classmethod
    def maintain(cls, c, wid, at):
        from world_engine.journey_follow import JourneyFollowService
        from world_engine.journey_room_appointments import JourneyRoomService
        from world_engine.daily_life import DailyLifeService
        from world_engine.families import FamilyService
        from world_engine.intimacy import IntimacyService
        from world_engine.schedules import ScheduleService
        from world_engine.journey_breaks import JourneyBreakService

        for row in c.execute(
            "SELECT * FROM joint_journeys WHERE world_id=? AND status IN ('pending','accepted','active','paused','resume_pending')",
            (wid,),
        ).fetchall():
            people = cls._people(c, row)
            npc_led = JourneyFollowService.leader_id(c, row) == people[1]["id"]
            has_follow = bool(dict(row).get("follow_authorized_by"))
            following = npc_led and JourneyFollowService.valid(c, row, at)
            if row["status"] in {"accepted", "active"} and (
                has_follow and not following
                or npc_led and row["phase"] == "moving" and not following
            ) and at < from_iso(row["deadline_at"]):
                cls.pause(c, row, at, "原跟随授权已不再对应当前有限路线，后续出发须本人重新选择。")
                continue
            invalid = c.execute(
                "SELECT 1 FROM locations WHERE id=? AND is_active=0 UNION ALL SELECT 1 FROM journey_stops s JOIN locations l ON l.id=s.location_id WHERE s.journey_id=? AND l.is_active=0 LIMIT 1",
                (row["origin_id"], row["id"]),
            ).fetchone()
            if invalid:
                cls._stop(c, row, at)
                event = cls._record(
                    c,
                    *people,
                    at,
                    "ended",
                    "原行程地点已不可用，需要重新商量；实际位置与已走路程保留。",
                    {"journey_id": row["id"]},
                )
                c.execute(
                    "UPDATE joint_journeys SET status='ended',reason='原行程地点已不可用',source_event_id=? WHERE id=?",
                    (event, row["id"]),
                )
                continue
            if (
                row["status"] == "active"
                and row["started_at"]
                and c.execute(
                    "SELECT 1 FROM world_events e WHERE e.world_id=? AND e.event_type='action.attack' AND ((e.actor_id=? AND e.target_id=?) OR (e.actor_id=? AND e.target_id=?)) AND e.occurred_at>=? AND e.created_at>=(SELECT MIN(m.created_at) FROM journey_legs g JOIN character_movements m ON m.id=g.player_movement_id WHERE g.journey_id=?) LIMIT 1",
                    (
                        wid,
                        people[0]["id"],
                        people[1]["id"],
                        people[1]["id"],
                        people[0]["id"],
                        row["started_at"],
                        row["id"],
                    ),
                ).fetchone()
            ):
                cls._stop(c, row, at)
                event = cls._record(
                    c,
                    *people,
                    at,
                    "ended",
                    "共同旅行中发生主动攻击，本次行程结束，保留真实位置与后果。",
                    {"journey_id": row["id"]},
                )
                c.execute(
                    "UPDATE joint_journeys SET status='ended',reason='共同旅行中发生攻击',source_event_id=? WHERE id=?",
                    (event, row["id"]),
                )
                continue
            if (
                row["status"] == "active"
                and row["phase"] == "moving"
                and at <= from_iso(row["deadline_at"])
            ):
                cls._arrive(c, row, people, at)
                row = c.execute("SELECT * FROM joint_journeys WHERE id=?", (row["id"],)).fetchone()
                if row["status"] != "active":
                    continue
            final_stop = (
                row["status"] == "active"
                and row["phase"] == "stopped"
                and not c.execute(
                    "SELECT 1 FROM journey_stops WHERE journey_id=? AND stop_index=?",
                    (row["id"], row["next_index"]),
                ).fetchone()
            )
            if at >= from_iso(row["deadline_at"]) and not (
                final_stop
                and row["next_depart_at"]
                and from_iso(row["next_depart_at"]) <= from_iso(row["deadline_at"])
            ):
                cls._stop(c, row, at)
                event = cls._record(
                    c,
                    *people,
                    at,
                    "expired",
                    "旅行期限已到，保留实际结果，不冒充全部完成。",
                    {"journey_id": row["id"]},
                )
                c.execute(
                    "UPDATE joint_journeys SET status='expired',reason='旅行期限已到',source_event_id=? WHERE id=?",
                    (event, row["id"]),
                )
                continue
            if row["status"] == "accepted":
                npc = people[1]
                origin = cls._place(c, wid, row["origin_id"])
                if (
                    not npc["current_room_id"]
                    and not npc["current_fixture_id"]
                    and not LifeActivityService.running(c, npc["id"])
                    and not c.execute(
                        "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                        (npc["id"],),
                    ).fetchone()
                    and great_circle_distance_km(
                        npc["longitude"], npc["latitude"], origin["longitude"], origin["latitude"]
                    )
                    <= 0.025
                ):
                    if row["meet_wait_until"] is None:
                        c.execute(
                            "UPDATE joint_journeys SET meet_wait_until=? WHERE id=?",
                            (to_iso(at + timedelta(hours=1)), row["id"]),
                        )
                    elif at >= from_iso(row["meet_wait_until"]):
                        cls.pause(c, row, at, "集合尚未及时开始，需要双方重新确认出发")
            if row["status"] != "active" and not (row["status"] == "accepted" and following):
                continue
            reason = ""
            if IntimacyService.paused(c, wid, people[0]["id"], people[1]["id"]):
                reason = "一方要求留出空间"
            for person in people:
                activity = LifeActivityService.running(c, person["id"])
                plan = ScheduleService.assess(c, person, at)
                if person["health"] < 40 or person["energy"] <= 20 or person["satiety"] <= 30:
                    reason = "一方需要先处理身体、用饭或休息"
                elif (
                    person["current_room_id"]
                    or person["current_fixture_id"]
                    or person["active_vehicle_id"]
                    or person["movement_type"] != "land"
                ):
                    reason = "一方的场景或交通方式已经改变"
                elif activity and (
                    row["phase"] == "moving" or activity["kind"] not in {"wait", "rest"}
                ):
                    reason = "一方已有其他需要处理的活动"
                elif plan and plan["depart_at"] <= at:
                    reason = "一方需要准备已经确认的赴约安排"
                elif not person["is_player"] and (
                    DailyLifeService.is_sleep_time(person, at)
                    or FamilyService.hungry_dependent(c, person, at)
                ):
                    reason = "一方需要处理作息或家庭照护"
            if (
                not same_room(*people)
                or great_circle_distance_km(
                    people[0]["longitude"],
                    people[0]["latitude"],
                    people[1]["longitude"],
                    people[1]["latitude"],
                )
                > 0.1
            ):
                reason = "双方已离开同一可见场景，需要实际重新会合"
            if reason:
                cls.pause(c, row, at, reason)
                continue
            if row["status"] != "active":
                continue
            if final_stop and at >= from_iso(row["next_depart_at"]):
                cls._complete(c, row, people, from_iso(row["next_depart_at"]))
                continue
            if row["phase"] != "moving" and row["wait_until"] and at >= from_iso(row["wait_until"]):
                cls.pause(c, row, at, "约定停留后已等待一小时，先释放彼此的日常安排")
        JourneyBreakService.expire_pending(c,wid,at)
        JourneyRoomService.expire_pending(c,wid,at)
        from world_engine.overnight import OvernightService
        OvernightService.maintain(c,wid,at)

    @classmethod
    def _arrive(cls, c, row, people, at):
        leg = c.execute(
            "SELECT * FROM journey_legs WHERE journey_id=? AND status='moving' ORDER BY rowid DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if not leg:
            cls.pause(c, row, at, "当前这一程缺少有效移动回执")
            return
        moves = [
            c.execute("SELECT status FROM character_movements WHERE id=?", (leg[k],)).fetchone()[0]
            for k in ("player_movement_id", "npc_movement_id")
        ]
        if any(s not in {"moving", "arrived"} for s in moves):
            cls.pause(c, row, at, "一方提前取消或改变了这一程的移动")
            return
        if not all(s == "arrived" for s in moves):
            return
        stop = c.execute(
            "SELECT * FROM journey_stops WHERE journey_id=? AND stop_index=?",
            (row["id"], row["next_index"]),
        ).fetchone()
        place = cls._place(c, row["world_id"], stop["location_id"])
        if any(
            great_circle_distance_km(
                p["longitude"], p["latitude"], place["longitude"], place["latitude"]
            )
            > 0.025
            for p in people
        ):
            cls.pause(c, row, at, "实际抵达位置与约定站点不一致")
            return
        cls._mark_stop(c, row, people, at, leg)

    @classmethod
    def _mark_stop(cls, c, row, people, at, leg):
        stop = c.execute(
            "SELECT * FROM journey_stops WHERE journey_id=? AND stop_index=?",
            (row["id"], row["next_index"]),
        ).fetchone()
        next_index = row["next_index"] + 1
        done = not c.execute(
            "SELECT 1 FROM journey_stops WHERE journey_id=? AND stop_index=?",
            (row["id"], next_index),
        ).fetchone()
        event = cls._record(
            c,
            *people,
            at,
            "arrived",
            "双方实际抵达约定站点，开始记录停留时间。",
            {
                "journey_id": row["id"],
                "leg_id": leg["id"] if leg else None,
                "stop_index": row["next_index"],
            },
        )
        if leg:
            c.execute(
                "UPDATE journey_legs SET status='arrived',finished_at=?,source_event_id=? WHERE id=?",
                (to_iso(at), event, leg["id"]),
            )
        c.execute(
            "UPDATE journey_stops SET arrived_at=?,source_event_id=? WHERE journey_id=? AND stop_index=?",
            (to_iso(at), event, row["id"], row["next_index"]),
        )
        depart = at + timedelta(minutes=stop["minutes"])
        c.execute(
            "UPDATE joint_journeys SET status='active',started_at=COALESCE(started_at,?),phase='stopped',next_index=?,next_depart_at=?,wait_until=?,remaining_stop_seconds=?,source_event_id=? WHERE id=?",
            (
                to_iso(at),
                next_index,
                to_iso(depart),
                to_iso(depart + timedelta(hours=1)),
                stop["minutes"] * 60,
                event,
                row["id"],
            ),
        )
        if done and stop["minutes"] == 0:
            cls._complete(c, row, people, at)

    @classmethod
    def _complete(cls, c, row, people, at):
        from world_engine.intimacy import IntimacyService
        from world_engine.journey_follow import JourneyFollowService

        JourneyFollowService.clear(c, row["id"])

        event = cls._record(
            c,
            *people,
            at,
            "completed",
            "双方用实际移动和停留完成了约定的共同旅行。",
            {"journey_id": row["id"]},
        )
        c.execute(
            "UPDATE joint_journeys SET status='completed',completed_at=?,source_event_id=?,reason='全部约定路线与停留已经实际完成' WHERE id=?",
            (to_iso(at), event, row["id"]),
        )
        IntimacyService.remember_event(
            c,
            row["world_id"],
            people[0]["id"],
            people[1]["id"],
            event,
            at,
            "shared",
            "双方用实际移动和停留完成了约定的共同旅行。",
        )
        for first, second in (people, people[::-1]):
            c.execute(
                "UPDATE character_acquaintances SET shared_experiences=shared_experiences+1 WHERE observer_id=? AND subject_id=?",
                (first["id"], second["id"]),
            )
