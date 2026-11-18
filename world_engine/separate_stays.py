"""哥哥，分房住宿各自入屋和休息，约好的会合也必须真实发生。"""

import json
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.geo import great_circle_distance_km
from world_engine.life import LifeActivityService
from world_engine.repository import from_iso, to_iso

SEPARATE_STAY_SCHEMA = """
CREATE TABLE IF NOT EXISTS journey_separate_stays (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 proposer_id TEXT NOT NULL REFERENCES characters(id),
 recipient_id TEXT NOT NULL REFERENCES characters(id),
 journey_id TEXT NOT NULL REFERENCES joint_journeys(id),stop_index INTEGER NOT NULL,
 source_lodging_plan_id TEXT REFERENCES journey_lodging_plans(id),
 arrival_event_id TEXT NOT NULL REFERENCES world_events(id),
 pause_event_id TEXT NOT NULL REFERENCES world_events(id),
 own_room_id TEXT NOT NULL REFERENCES life_rooms(id),
 other_room_id TEXT NOT NULL REFERENCES life_rooms(id),
 location_id TEXT NOT NULL REFERENCES locations(id),
 status TEXT NOT NULL CHECK(status IN (
  'pending','accepted','active','completed','declined','cancelled','expired','missed','interrupted')),
 arrival_starts_at TEXT NOT NULL,arrival_ends_at TEXT NOT NULL,latest_end_at TEXT NOT NULL,
 minutes INTEGER NOT NULL CHECK(minutes BETWEEN 240 AND 720),
 reunion_starts_at TEXT NOT NULL,reunion_ends_at TEXT NOT NULL,
 reunion_status TEXT NOT NULL DEFAULT 'waiting'
  CHECK(reunion_status IN ('waiting','met','missed','cancelled')),
 reunited_at TEXT,reunion_event_id TEXT REFERENCES world_events(id),reunion_wait_ended_at TEXT,
 statement TEXT NOT NULL,response TEXT NOT NULL DEFAULT '',proposed_at TEXT NOT NULL,
 source_event_id TEXT NOT NULL REFERENCES world_events(id),reason TEXT NOT NULL DEFAULT '',
 CHECK(proposer_id<>recipient_id),CHECK(own_room_id<>other_room_id),
 FOREIGN KEY(journey_id,stop_index) REFERENCES journey_stops(journey_id,stop_index)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_separate_stay_pending_pair
 ON journey_separate_stays(world_id,MIN(proposer_id,recipient_id),MAX(proposer_id,recipient_id))
 WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_separate_stay_live
 ON journey_separate_stays(world_id,status,reunion_status,arrival_starts_at);
CREATE INDEX IF NOT EXISTS idx_separate_stay_journey ON journey_separate_stays(journey_id,status);
CREATE TABLE IF NOT EXISTS journey_separate_stay_participants (
 stay_id TEXT NOT NULL REFERENCES journey_separate_stays(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id),
 room_id TEXT NOT NULL REFERENCES life_rooms(id),
 status TEXT NOT NULL DEFAULT 'waiting'
  CHECK(status IN ('waiting','active','completed','missed','interrupted','cancelled')),
 activity_id TEXT REFERENCES character_life_activities(id),
 checked_in_at TEXT,ends_at TEXT,finished_at TEXT,
 source_event_id TEXT REFERENCES world_events(id),reason TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(stay_id,character_id)
);
CREATE INDEX IF NOT EXISTS idx_separate_stay_person
 ON journey_separate_stay_participants(character_id,status);
"""


class SeparateStayIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: Literal[
        "propose",
        "accept",
        "decline",
        "cancel",
        "check_in",
        "reschedule",
        "accept_change",
        "decline_change",
        "withdraw_change",
    ]
    stay_id: str | None = Field(default=None, min_length=1, max_length=100)
    change_id: str | None = Field(default=None, min_length=1, max_length=100)
    journey_id: str | None = Field(default=None, min_length=1, max_length=100)
    source_lodging_plan_id: str | None = Field(default=None, min_length=1, max_length=100)
    own_room_id: str | None = Field(default=None, min_length=1, max_length=100)
    other_room_id: str | None = Field(default=None, min_length=1, max_length=100)
    arrival_world_time: str | None = None
    window_minutes: int = Field(default=60, ge=15, le=120, strict=True)
    minutes: int = Field(default=480, ge=240, le=720, strict=True)
    reunion_window_minutes: int = Field(default=60, ge=15, le=120, strict=True)
    statement: str = Field(min_length=1, max_length=240)

    @model_validator(mode="before")
    @classmethod
    def defaults(cls, value):
        if isinstance(value, dict):
            return {
                k: v
                for k, v in value.items()
                if not (k in {"window_minutes", "minutes", "reunion_window_minutes"} and v is None)
            }
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.operation == "propose":
            if (
                self.stay_id
                or self.change_id
                or not all(
                    (self.journey_id, self.own_room_id, self.other_room_id, self.arrival_world_time)
                )
            ):
                raise ValueError("分房住宿须指定真实旅行、各自房间和明确到场时间")
            if self.own_room_id == self.other_room_id:
                raise ValueError("分房住宿需要两间不同的真实房间")
        elif self.operation == "reschedule":
            if (
                not self.stay_id
                or self.change_id
                or not self.arrival_world_time
                or any(
                    (
                        self.journey_id,
                        self.source_lodging_plan_id,
                        self.own_room_id,
                        self.other_room_id,
                    )
                )
                or self.window_minutes != 60
                or self.minutes != 480
                or self.reunion_window_minutes != 60
            ):
                raise ValueError("分房改期只引用原安排和新的入住开始时间，不改人、房间、时长或来源")
        elif (
            not self.stay_id
            or any(
                (
                    self.journey_id,
                    self.source_lodging_plan_id,
                    self.own_room_id,
                    self.other_room_id,
                    self.arrival_world_time,
                )
            )
            or self.window_minutes != 60
            or self.minutes != 480
            or self.reunion_window_minutes != 60
            or (self.operation in {"accept_change", "decline_change", "withdraw_change"})
            != bool(self.change_id)
        ):
            raise ValueError("回应分房住宿只引用真实编号，不能改写房间、时间或会合约定")
        return self


class SeparateStayService:
    @staticmethod
    def _people(c, row):
        return [
            c.execute("SELECT * FROM characters WHERE id=?", (row[k],)).fetchone()
            for k in ("proposer_id", "recipient_id")
        ]

    @staticmethod
    def _parts(c, sid):
        return c.execute(
            (
                "SELECT * FROM journey_separate_stay_participants WHERE "
                "stay_id=? ORDER BY character_id"
            ),
            (sid,),
        ).fetchall()

    @staticmethod
    def _record(c, row, at, state, summary, cid=None):
        from world_engine.appointments import AppointmentService

        payload = {"stay_id": row["id"]}
        if row.get("source_lodging_plan_id"):
            payload["source_lodging_plan_id"] = row["source_lodging_plan_id"]
            source = c.execute(
                "SELECT source_event_id FROM journey_lodging_plans WHERE id=?",
                (row["source_lodging_plan_id"],),
            ).fetchone()
            if source:
                payload["source_lodging_plan_completed_event_id"] = source[0]
        if cid:
            from world_engine.actions import ActionService

            actor = c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
            event = ActionService._record_event(
                c,
                world_id=row["world_id"],
                tick_id=str(uuid4()),
                occurred_at=at,
                event_type="social.appointment_separate_stay_" + state,
                actor_id=cid,
                target_id=None,
                location_id=actor["current_location_id"] or actor["location_id"],
                summary=summary,
                payload={**payload, "character_id": cid},
            )
            ActionService._record_memory(
                c,
                world_id=row["world_id"],
                character_id=cid,
                event_id=event,
                memory_type="experienced",
                summary=summary,
                importance=3,
            )
            return event
        first, second = SeparateStayService._people(c, row)
        return AppointmentService._record(
            c,
            first,
            second,
            at,
            "separate_stay_" + state,
            summary,
            payload,
        )

    @staticmethod
    def _completed_lodging(c, wid, pid, first, second, at):
        source = c.execute(
            "SELECT * FROM journey_lodging_plans WHERE id=? AND world_id=? AND status='completed'",
            (pid, wid),
        ).fetchone()
        if not source or {source["proposer_id"], source["recipient_id"]} != {first, second}:
            raise ValueError("请引用双方真实完成并共同核对的订房筹备")
        event = c.execute(
            "SELECT * FROM world_events WHERE id=? AND world_id=?",
            (source["source_event_id"], wid),
        ).fetchone()
        try:
            valid = bool(
                event
                and event["event_type"] == "social.appointment_lodging_plan_completed"
                and event["actor_id"] == source["proposer_id"]
                and event["target_id"] == source["recipient_id"]
                and json.loads(event["payload_json"])["plan_id"] == pid
                and from_iso(event["occurred_at"]) <= at
            )
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("订房筹备缺少双方真实完成的共同回执")
        roles = c.execute("SELECT * FROM journey_lodging_roles WHERE plan_id=?", (pid,)).fetchall()
        if {r["character_id"] for r in roles} != {first, second}:
            raise ValueError("订房筹备缺少双方各自报告的真实回执")
        for role in roles:
            report = c.execute(
                "SELECT * FROM world_events WHERE id=? AND world_id=?",
                (role["report_event_id"], wid),
            ).fetchone()
            try:
                report_payload = json.loads(report["payload_json"]) if report else {}
                valid = bool(
                    role["status"] == "ready"
                    and role["room_id"]
                    == (
                        source["own_room_id"]
                        if role["character_id"] == source["proposer_id"]
                        else source["other_room_id"]
                    )
                    and role["ready_at"]
                    and report
                    and isinstance(report_payload, dict)
                    and report["event_type"] == "social.appointment_lodging_plan_ready"
                    and report["actor_id"] == role["character_id"]
                    and report["target_id"] == (second if role["character_id"] == first else first)
                    and report_payload.get("plan_id") == pid
                    and report_payload.get("character_id") == role["character_id"]
                    and from_iso(report["occurred_at"]) == from_iso(role["ready_at"])
                    and from_iso(role["ready_at"]) <= from_iso(event["occurred_at"])
                )
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise ValueError("订房筹备缺少双方各自报告的真实回执")
        return dict(source)

    @classmethod
    def _lodging_source(cls, c, row, at):
        if not row.get("source_lodging_plan_id"):
            return
        source = cls._completed_lodging(
            c,
            row["world_id"],
            row["source_lodging_plan_id"],
            row["proposer_id"],
            row["recipient_id"],
            at,
        )
        source_own = (
            source["own_room_id"]
            if source["proposer_id"] == row["proposer_id"]
            else source["other_room_id"]
        )
        source_other = (
            source["other_room_id"]
            if source["proposer_id"] == row["proposer_id"]
            else source["own_room_id"]
        )
        if (row["own_room_id"], row["other_room_id"]) != (source_own, source_other):
            raise ValueError("引用订房筹备须保留各自原房间，不能交换本人和对方的房间")
        trip = c.execute("SELECT * FROM joint_journeys WHERE id=?", (row["journey_id"],)).fetchone()
        stop = c.execute(
            "SELECT * FROM journey_stops WHERE journey_id=? AND stop_index=?",
            (row["journey_id"], row["stop_index"]),
        ).fetchone()
        if (
            source["journey_id"] != row["journey_id"]
            or source["location_id"] != row["location_id"]
            or any(
                source[k] != row[k] for k in ("stop_index", "arrival_event_id", "pause_event_id")
            )
            or not trip
            or trip["status"] != "paused"
            or trip["phase"] != "stopped"
            or trip["next_index"] != source["stop_index"] + 1
            or trip["source_event_id"] != source["pause_event_id"]
            or not stop
            or not stop["arrived_at"]
            or stop["source_event_id"] != source["arrival_event_id"]
            or stop["location_id"] != source["location_id"]
        ):
            raise ValueError("原旅行、到站或暂停记录已变化，不能把旧订房筹备套到新的停留")
        if from_iso(row["latest_end_at"]) > from_iso(source["required_until_at"]):
            raise ValueError("最晚入住后的完整休息超过原订房筹备的许可终点，请重新商量")
        from world_engine.interiors import InteriorService

        # 哥哥，共同报告是历史事实，手中的钥匙和今天的门路仍要重新核对。
        for cid in (row["proposer_id"], row["recipient_id"]):
            rooms = cls._access(
                c,
                row["world_id"],
                cid,
                cls._room_for(row, cid),
                row["location_id"],
                max(at, from_iso(row["arrival_starts_at"])),
                from_iso(row["latest_end_at"]),
            )
            for room in rooms:
                policy = c.execute(
                    "SELECT key_item_type_id FROM life_door_policies WHERE room_id=?", (room["id"],)
                ).fetchone()
                if (room["door_locked"] or policy and policy[0]) and not InteriorService.can_unlock(
                    c, room, cid, at=at
                ):
                    raise ValueError("本人尚缺原订房房间或外层门路需要的真实钥匙")

    @staticmethod
    def _source_summary(c, row):
        if not row.get("source_lodging_plan_id"):
            return None
        source = c.execute(
            "SELECT id,required_until_at,source_event_id completion_event_id "
            "FROM journey_lodging_plans WHERE id=? AND world_id=?",
            (row["source_lodging_plan_id"], row["world_id"]),
        ).fetchone()
        return dict(source) if source else None

    @staticmethod
    def _room_for(row, cid):
        return row["own_room_id"] if row["proposer_id"] == cid else row["other_room_id"]

    @staticmethod
    def _trip_resumed(c, row):
        if not row["reunited_at"]:
            return False
        trip = c.execute(
            (
                "SELECT j.status,e.occurred_at FROM joint_journeys j LEFT JOIN "
                "world_events e ON e.id=j.source_event_id WHERE j.id=?"
            ),
            (row["journey_id"],),
        ).fetchone()
        return bool(
            trip
            and trip["status"] in {"active", "completed"}
            and trip["occurred_at"]
            and from_iso(trip["occurred_at"]) >= from_iso(row["reunited_at"])
        )

    @classmethod
    def _end_reunion_wait(cls, c, row, at, summary):
        event = cls._record(c, row, at, "reunion_wait_ended", summary)
        c.execute(
            (
                "UPDATE journey_separate_stays SET reunion_wait_ended_at=? WHERE "
                "id=? AND reunion_wait_ended_at IS NULL"
            ),
            (to_iso(at), row["id"]),
        )
        return {"status": "completed", "stay_id": row["id"], "event_id": event, "summary": summary}

    @staticmethod
    def _access(c, wid, cid, rid, lid, start, end):
        from world_engine.appointment_rooms import AppointmentRoomService

        rooms = AppointmentRoomService.path(c, wid, rid)
        if rooms[0]["location_id"] != lid:
            raise ValueError("两间住宿房间必须属于原旅行实际到达的同一站点")
        for room in rooms:
            controller = AppointmentRoomService.controller(c, room, start, end)
            if controller != cid and not AppointmentRoomService.base_access(
                c, room, cid, start, end
            ):
                raise ValueError(
                    "本人缺少所分房间或外层门路的完整进入许可；请先分别办理租住或居住安排"
                )
        return rooms

    @classmethod
    def _rooms_valid(cls, c, row, at=None):
        from world_engine.appointment_rooms import AppointmentRoomService

        chains = []
        for cid in (row["proposer_id"], row["recipient_id"]):
            part = (
                c.execute(
                    (
                        "SELECT * FROM journey_separate_stay_participants WHERE "
                        "stay_id=? AND character_id=?"
                    ),
                    (row["id"], cid),
                ).fetchone()
                if row.get("id")
                else None
            )
            end = from_iso(part["ends_at"] if part and part["ends_at"] else row["latest_end_at"])
            start = from_iso(row["arrival_starts_at"])
            if at is not None:
                start = max(start, min(at, end - timedelta(microseconds=1)))
            chains.append(
                cls._access(
                    c, row["world_id"], cid, cls._room_for(row, cid), row["location_id"], start, end
                )
            )
        if chains[0][0]["id"] == chains[1][0]["id"] or any(
            chains[i][0]["id"] in {r["id"] for r in chains[1 - i]} for i in (0, 1)
        ):
            raise ValueError("分房不能使用同一房间，也不能让一人的房间成为另一人的内外通路")
        for rooms in chains:
            if (
                AppointmentRoomService.transition_minutes(c, row["world_id"], rooms[0]["id"], None)
                > (
                    from_iso(row["reunion_ends_at"]) - from_iso(row["reunion_starts_at"])
                ).total_seconds()
                / 60
            ):
                raise ValueError("会合时间窗不足以实际逐门离开房间，请重新商量")

    @classmethod
    def room_options(cls, c, wid, cid, other, at):
        from world_engine.interiors import InteriorService

        actor = c.execute(
            "SELECT * FROM characters WHERE id=? AND world_id=?", (cid, wid)
        ).fetchone()
        if not actor:
            return []
        known = {
            r[0]
            for r in c.execute(
                (
                    "SELECT id FROM life_rooms WHERE world_id=? AND "
                    "owner_character_id=? UNION SELECT room_id FROM life_room_access "
                    "WHERE character_id=? UNION SELECT room_id FROM "
                    "home_residency_permissions WHERE resident_id=? UNION SELECT "
                    "asset_id FROM contract_fulfillments WHERE world_id=? AND "
                    "requester_id=? AND kind='lodging' AND status='active' AND "
                    "due_world_time>?"
                ),
                (wid, cid, cid, cid, wid, cid, to_iso(at)),
            )
        }
        # 哥哥，看见房门只让本人能指认入口，不会连私人房名一起获得。
        named = set(known)
        named.update(
            r[0]
            for r in c.execute(
                "SELECT DISTINCT json_extract(CASE WHEN json_valid(e.payload_json) THEN "
                "e.payload_json ELSE '{}' END,'$.room_id') FROM world_events e "
                "JOIN character_event_knowledge k ON k.event_id=e.id AND k.character_id=? "
                "JOIN event_observers o ON o.event_id=e.id AND o.character_id=? "
                "WHERE e.world_id=? AND e.event_type LIKE 'action.interior_%' "
                "AND o.channel='participant' AND k.source_kind='experienced' "
                "AND e.occurred_at<=? AND k.learned_world_time<=?",
                (cid, cid, wid, to_iso(at), to_iso(at)),
            )
            if r[0]
        )
        known.update(r["id"] for r in InteriorService.scene(c, actor)["doors"])
        if actor["current_room_id"]:
            known.add(actor["current_room_id"])
            named.add(actor["current_room_id"])
        for stay in c.execute(
            (
                "SELECT own_room_id,other_room_id FROM journey_separate_stays "
                "WHERE world_id=? AND (proposer_id=? OR recipient_id=?) AND "
                "status IN ('pending','accepted','active','completed')"
            ),
            (wid, cid, cid),
        ):
            known.update(stay)
            named.update(stay)
        priority = set()
        # 哥哥，双方真实核对过的两间房已是共同知识，不借此公开彼此的账务。
        if c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='journey_lodging_plans'"
        ).fetchone():
            for source in c.execute(
                "SELECT * FROM journey_lodging_plans WHERE world_id=? AND status='completed' "
                "AND (proposer_id=? OR recipient_id=?)",
                (wid, cid, cid),
            ).fetchall():
                try:
                    cls._completed_lodging(
                        c, wid, source["id"], source["proposer_id"], source["recipient_id"], at
                    )
                except ValueError:
                    continue
                known.update((source["own_room_id"], source["other_room_id"]))
                named.update((source["own_room_id"], source["other_room_id"]))
            if other:
                from world_engine.lodging_plans import LodgingPlanService

                for source in LodgingPlanService.handoff_sources(c, wid, cid, other, at):
                    priority.update((source["own_room_id"], source["other_room_id"]))
        result = []
        # 哥哥，当前可继续商量的两房先保留，旧住处不能把它们挤出已知候选上限。
        for rid in sorted(known, key=lambda rid: (rid not in priority, rid)):
            room = c.execute(
                (
                    "SELECT r.*,l.name location_name FROM life_rooms r JOIN "
                    "locations l ON l.id=r.location_id WHERE r.id=? AND r.world_id=? "
                    "AND l.is_active=1"
                ),
                (rid, wid),
            ).fetchone()
            if not room:
                continue
            value = {
                "id": rid,
                "name": room["name"] if (
                    rid in named or room["access_policy"] == "public"
                    or InteriorService.allowed(c, room, cid, at=at)
                ) else "私人房间的入口",
                "location_id": room["location_id"],
                "location_name": room["location_name"],
            }
            try:
                cls._access(c, wid, cid, rid, room["location_id"], at, at + timedelta(seconds=1))
                value["own_access"] = True
            except ValueError:
                value["own_access"] = False
            result.append(value)
        return result[:32]

    @classmethod
    def rows(cls, c, wid, cid, at, other=None):
        from world_engine.separate_stay_changes import SeparateStayChangeService

        args = [wid, cid, cid] + ([other, other] if other else [])
        result = []
        for row in c.execute(
            (
                "SELECT s.*,a.name own_room_name,b.name other_room_name,l.name "
                "location_name FROM journey_separate_stays s JOIN life_rooms a "
                "ON a.id=s.own_room_id JOIN life_rooms b ON b.id=s.other_room_id "
                "JOIN locations l ON l.id=s.location_id WHERE s.world_id=? AND "
                "(s.proposer_id=? OR s.recipient_id=?)"
            )
            + (" AND (s.proposer_id=? OR s.recipient_id=?)" if other else "")
            + (
                " ORDER BY CASE WHEN s.status IN ('pending','accepted','active') "
                "OR s.reunion_status='waiting' THEN 0 ELSE 1 END,s.rowid DESC LIM"
                "IT 12"
            ),
            args,
        ):
            value = dict(row)
            # 哥哥，能否提出改期只按双方已知的状态与时间展示，不读取伙伴私下入住进度。
            value["can_reschedule"] = row["status"] == "accepted" and at < from_iso(
                row["arrival_starts_at"]
            )
            value["reschedule_requests"] = SeparateStayChangeService.rows(
                c, row["id"], at, observer_id=cid
            )
            value["source_lodging_plan"] = cls._source_summary(c, value)
            value["other_id"] = (
                row["recipient_id"] if row["proposer_id"] == cid else row["proposer_id"]
            )
            value["can_end_reunion_wait"] = bool(
                row["status"] == "completed"
                and row["reunion_status"] == "met"
                and not row["reunion_wait_ended_at"]
                and at < from_iso(row["reunion_ends_at"])
            )
            value["participants"] = []
            own_part = None
            for part in cls._parts(c, row["id"]):
                # 哥哥，伙伴独自休息时的活动编号和私密中断细节不放进人物档案。
                p = {k: part[k] for k in ("character_id", "room_id", "status")}
                p["room_name"] = (
                    row["own_room_name"]
                    if part["character_id"] == row["proposer_id"]
                    else row["other_room_name"]
                )
                p["location_name"] = row["location_name"]
                if part["character_id"] == cid:
                    own_part = part
                    p.update(
                        {
                            k: part[k]
                            for k in (
                                "activity_id",
                                "checked_in_at",
                                "ends_at",
                                "finished_at",
                                "reason",
                            )
                        }
                    )
                else:
                    p["status"] = "completed" if row["reunion_status"] == "met" else "unknown"
                value["participants"].append(p)
            if (
                row["status"] in {"accepted", "active", "completed", "missed", "interrupted"}
                and row["reunion_status"] != "met"
            ):
                value["status"] = {
                    "waiting": "accepted",
                    "active": "active",
                    "completed": "accepted",
                    "missed": "missed",
                    "interrupted": "interrupted",
                    "cancelled": "cancelled",
                }[own_part["status"]]
            if row["status"] == "pending" and at >= from_iso(row["arrival_starts_at"]):
                value["status"] = "expired"
            result.append(value)
        from world_engine.intimacy import IntimacyService

        IntimacyService.mask_records(c, wid, cid, result, ("statement", "response", "reason"))
        return result

    @classmethod
    def context(cls, c, wid, cid, other, at):
        from world_engine.separate_stay_changes import SeparateStayChangeService

        keys = (
            "id",
            "proposer_id",
            "recipient_id",
            "journey_id",
            "source_lodging_plan_id",
            "source_lodging_plan",
            "own_room_id",
            "other_room_id",
            "own_room_name",
            "other_room_name",
            "location_id",
            "status",
            "arrival_starts_at",
            "arrival_ends_at",
            "minutes",
            "reunion_starts_at",
            "reunion_ends_at",
            "reunion_status",
            "can_end_reunion_wait",
            "participants",
            "can_reschedule",
        )
        return [
            {
                **{k: row[k] for k in keys},
                "reschedule_requests": SeparateStayChangeService.rows(
                    c, row["id"], at, compact=True, observer_id=cid
                ),
            }
            for row in cls.rows(c, wid, cid, at, other)
            if row["can_end_reunion_wait"]
            or row["status"] == "pending"
            or row["reunion_status"] == "waiting"
            or any(
                p["character_id"] == cid and p["status"] in {"waiting", "active"}
                for p in row["participants"]
            )
        ][:2]

    @classmethod
    def _reservation_end(cls, c, row, cid):
        if row["reunion_status"] == "waiting":
            return from_iso(row["reunion_ends_at"]), None
        part = c.execute(
            ("SELECT * FROM journey_separate_stay_participants WHERE stay_id=? AND character_id=?"),
            (row["id"], cid),
        ).fetchone()
        if part and part["status"] in {"waiting", "active"}:
            return from_iso(part["ends_at"] or row["latest_end_at"]), part["room_id"]
        return None, None

    @classmethod
    def assert_interval_free(cls, c, person, start, end, location_id, room_id=None, exclude=None):
        from world_engine.overnight import OvernightService

        for row in c.execute(
            (
                "SELECT * FROM journey_separate_stays WHERE world_id=? AND "
                "(proposer_id=? OR recipient_id=?) AND (status IN ('accepted',"
                "'active') OR (status='completed' AND reunion_status='waiting'))"
            ),
            (person["world_id"], person["id"], person["id"]),
        ):
            if row["id"] == exclude:
                continue
            reserve_end, end_room = cls._reservation_end(c, row, person["id"])
            if reserve_end is None:
                continue
            to_room = OvernightService._travel_gap(
                c,
                person,
                location_id,
                room_id,
                row["location_id"],
                cls._room_for(row, person["id"]),
            )
            from_room = OvernightService._travel_gap(
                c, person, row["location_id"], end_room, location_id, room_id
            )
            if start < reserve_end + from_room and end + to_room > from_iso(
                row["arrival_starts_at"]
            ):
                raise ValueError("这段时间与已确认的分房休息、退屋或室外会合及真实路程冲突")

    @classmethod
    def _schedule_free(cls, c, people, row, exclude=None):
        from world_engine.overnight import OvernightService

        start, end = from_iso(row["arrival_starts_at"]), from_iso(row["reunion_ends_at"])
        for person in people:
            rid = cls._room_for(row, person["id"])
            cls.assert_interval_free(c, person, start, end, row["location_id"], rid, exclude)
            OvernightService.assert_interval_free(
                c, person, start, end, row["location_id"], rid, exclude
            )
            for date in c.execute(
                (
                    "SELECT f.location_id,f.due_world_time,w.starts_world_time,"
                    "a.room_id FROM contract_fulfillments f LEFT JOIN "
                    "appointment_windows w ON w.contract_id=f.request_id LEFT JOIN "
                    "social_appointments a ON a.contract_id=f.request_id WHERE "
                    "f.world_id=? AND f.kind='appointment' AND f.status='active' AND "
                    "(f.requester_id=? OR f.recipient_id=?)"
                ),
                (person["world_id"], person["id"], person["id"]),
            ):
                begin = (
                    from_iso(date["starts_world_time"])
                    if date["starts_world_time"]
                    else from_iso(date["due_world_time"]) - timedelta(hours=1)
                )
                before = OvernightService._travel_gap(
                    c, person, date["location_id"], date["room_id"], row["location_id"], rid
                )
                after = OvernightService._travel_gap(
                    c, person, row["location_id"], None, date["location_id"], date["room_id"]
                )
                if start < from_iso(date["due_world_time"]) + before and end + after > begin:
                    raise ValueError("分房住宿或退屋会合与已有约会和实际路程冲突")

    @classmethod
    def _arrival_possible(cls, c, people, row, at):
        from world_engine.schedules import ScheduleService

        place = c.execute("SELECT * FROM locations WHERE id=?", (row["location_id"],)).fetchone()
        for person in people:
            if ScheduleService.earliest_arrival(
                c, person, place, at, room_id=cls._room_for(row, person["id"])
            ) > from_iso(row["arrival_ends_at"]):
                raise ValueError("本人现有活动与逐门入屋路程无法赶上约定窗口")

    @classmethod
    def apply(cls, c, actor, other, at, choice):
        c.execute("SAVEPOINT separate_stay_choice")
        try:
            return cls._apply(c, actor, other, at, choice)
        except Exception:
            c.execute("ROLLBACK TO separate_stay_choice")
            raise
        finally:
            c.execute("RELEASE separate_stay_choice")

    @classmethod
    def _apply(cls, c, actor, other, at, choice):
        from world_engine.intimacy import IntimacyService

        if (
            other is None
            or actor["world_id"] != other["world_id"]
            or bool(actor["is_player"]) == bool(other["is_player"])
        ):
            raise ValueError("请选择当前世界中与玩家共同旅行的另一位人物")
        cls.maintain(c, actor["world_id"], at)
        if choice.operation in {"reschedule", "accept_change", "decline_change", "withdraw_change"}:
            from world_engine.separate_stay_changes import SeparateStayChangeService

            return SeparateStayChangeService.apply(c, actor, other, at, choice)
        if choice.operation == "propose":
            from world_engine.journey_breaks import JourneyBreakService
            from world_engine.journey_room_appointments import JourneyRoomService
            from world_engine.journeys import JourneyService

            IntimacyService._reachable(c, actor, other)
            if IntimacyService.paused(c, actor["world_id"], actor["id"], other["id"]):
                raise ValueError("一方需要留出空间，暂不提出分房住宿")
            try:
                start = datetime.fromisoformat(choice.arrival_world_time)
            except (ValueError, TypeError) as exc:
                raise ValueError("请提供带时区的明确到场时间") from exc
            if start.tzinfo is None or not at < start <= at + timedelta(days=1):
                raise ValueError("到场须在未来一个世界日内并带时区")
            known = {
                r["id"]
                for r in cls.room_options(c, actor["world_id"], actor["id"], other["id"], at)
            }
            if not {choice.own_room_id, choice.other_room_id} <= known:
                raise ValueError("只能商量本人已认识的真实房间，不能枚举对方的私人住所")
            from world_engine.appointment_rooms import AppointmentRoomService

            room = AppointmentRoomService.path(c, actor["world_id"], choice.own_room_id)[0]
            arrival_end = start + timedelta(minutes=choice.window_minutes)
            rest_end = arrival_end + timedelta(minutes=choice.minutes)
            reunion_end = rest_end + timedelta(minutes=choice.reunion_window_minutes)
            row = {
                "id": str(uuid4()),
                "world_id": actor["world_id"],
                "proposer_id": actor["id"],
                "recipient_id": other["id"],
                "journey_id": choice.journey_id,
                "source_lodging_plan_id": choice.source_lodging_plan_id,
                "own_room_id": choice.own_room_id,
                "other_room_id": choice.other_room_id,
                "location_id": room["location_id"],
                "arrival_starts_at": to_iso(start),
                "arrival_ends_at": to_iso(arrival_end),
                "latest_end_at": to_iso(rest_end),
                "reunion_starts_at": to_iso(rest_end),
                "reunion_ends_at": to_iso(reunion_end),
                "minutes": choice.minutes,
            }
            trip, stop = JourneyRoomService.validate(
                c, actor, other, at, choice.journey_id, room["location_id"], reunion_end
            )
            row.update(
                stop_index=stop["stop_index"],
                arrival_event_id=stop["source_event_id"],
                pause_event_id=trip["source_event_id"],
            )
            cls._lodging_source(c, row, at)
            JourneyService._assert_breaks_finished(c, trip, at)
            cls._rooms_valid(c, row)
            cls._schedule_free(c, (actor, other), row)
            cls._arrival_possible(c, (actor, other), row, at)
            if c.execute(
                (
                    "SELECT 1 FROM journey_separate_stays WHERE world_id=? AND "
                    "status='pending' AND ((proposer_id=? AND recipient_id=?) OR "
                    "(proposer_id=? AND recipient_id=?))"
                ),
                (actor["world_id"], actor["id"], other["id"], other["id"], actor["id"]),
            ).fetchone():
                raise ValueError("双方已有待回应的分房住宿邀请")
            link = JourneyBreakService.prepare(c, actor, other, at, choice.journey_id)
            row.update(
                stop_index=stop["stop_index"],
                arrival_event_id=link["arrival_event_id"],
                pause_event_id=link["pause_event_id"],
            )
            summary = (
                "原旅行暂停；提出各自分房完整休息及随后回原室外站点会合，"
                "房租、入屋与恢复仍分别处理。"
            )
            event = cls._record(c, row, at, "proposed", summary + choice.statement)
            columns = (
                "id",
                "world_id",
                "proposer_id",
                "recipient_id",
                "journey_id",
                "source_lodging_plan_id",
                "stop_index",
                "arrival_event_id",
                "pause_event_id",
                "own_room_id",
                "other_room_id",
                "location_id",
                "arrival_starts_at",
                "arrival_ends_at",
                "latest_end_at",
                "minutes",
                "reunion_starts_at",
                "reunion_ends_at",
            )
            c.execute(
                "INSERT INTO journey_separate_stays("
                + ",".join(columns)
                + ",status,statement,proposed_at,source_event_id) VALUES ("
                + ",".join("?" for _ in columns)
                + ",'pending',?,?,?)",
                tuple(row[k] for k in columns) + (choice.statement, to_iso(at), event),
            )
            for person in (actor, other):
                c.execute(
                    (
                        "INSERT INTO journey_separate_stay_participants(stay_id,"
                        "character_id,room_id,source_event_id) VALUES (?,?,?,?)"
                    ),
                    (row["id"], person["id"], cls._room_for(row, person["id"]), event),
                )
            return {
                "status": "pending",
                "stay_id": row["id"],
                "event_id": event,
                "summary": summary,
            }
        found = c.execute(
            (
                "SELECT * FROM journey_separate_stays WHERE id=? AND world_id=? "
                "AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND "
                "recipient_id=?))"
            ),
            (choice.stay_id, actor["world_id"], actor["id"], other["id"], other["id"], actor["id"]),
        ).fetchone()
        if not found:
            raise ValueError("这份分房住宿不属于双方")
        row = dict(found)
        if choice.operation in {"accept", "decline"}:
            IntimacyService._reachable(c, actor, other)
            if row["status"] != "pending" or row["recipient_id"] != actor["id"]:
                raise ValueError("只有收到有效分房邀请的一方可以回应")
            if choice.operation == "accept":
                if IntimacyService.paused(c, actor["world_id"], actor["id"], other["id"]):
                    raise ValueError("一方仍需要留出空间")
                cls._lodging_source(c, row, at)
                cls._rooms_valid(c, row, at)
                cls._schedule_free(c, (actor, other), row, row["id"])
                cls._arrival_possible(c, (actor, other), row, at)
                status, summary = (
                    "accepted",
                    "双方确认各自房间、完整休息和室外会合；仍各自逐门入屋并开始本人休息。",
                )
            else:
                status, summary = "declined", "本次分房住宿已婉拒，原旅行不自动恢复。"
            event = cls._record(c, row, at, status, summary + choice.statement)
            c.execute(
                (
                    "UPDATE journey_separate_stays SET status=?,response=?,"
                    "source_event_id=?,reunion_status=? WHERE id=?"
                ),
                (
                    status,
                    choice.statement,
                    event,
                    "waiting" if status == "accepted" else "cancelled",
                    row["id"],
                ),
            )
            if status == "declined":
                c.execute(
                    (
                        "UPDATE journey_separate_stay_participants SET "
                        "status='cancelled',finished_at=?,source_event_id=? WHERE "
                        "stay_id=?"
                    ),
                    (to_iso(at), event, row["id"]),
                )
            else:
                npc = actor if not actor["is_player"] else other
                c.execute(
                    "UPDATE npc_daily_states SET next_world_time=? WHERE character_id=?",
                    (to_iso(at), npc["id"]),
                )
            return {"status": status, "stay_id": row["id"], "event_id": event, "summary": summary}
        if choice.operation == "check_in":
            if not actor["is_player"]:
                raise ValueError("NPC 应按本人已接受的安排自行入屋并开始休息")
            return cls._start(c, actor, row, at)
        if (
            row["status"] == "completed"
            and row["reunion_status"] == "met"
            and not row["reunion_wait_ended_at"]
            and at < from_iso(row["reunion_ends_at"])
        ):
            summary = (
                "本人明确提前结束会合后的窗口内等候；"
                "已经完成的分房休息和真实会合事实保留，恢复旅行仍另行商量。"
            )
            return cls._end_reunion_wait(c, row, at, summary)
        if (
            row["status"] not in {"pending", "accepted", "active", "completed"}
            or row["status"] == "completed"
            and row["reunion_status"] != "waiting"
            or row["status"] == "pending"
            and row["proposer_id"] != actor["id"]
        ):
            raise ValueError("只能撤回本人邀请，或退出尚未结束的住宿与会合承诺")
        return cls._cancel(c, row, at, "cancelled", "本人决定退出本次分房住宿及后续会合。")

    @classmethod
    def _start(cls, c, actor, row, at):
        from world_engine.families import FamilyService
        from world_engine.interiors import InteriorService
        from world_engine.intimacy import IntimacyService

        part = c.execute(
            ("SELECT * FROM journey_separate_stay_participants WHERE stay_id=? AND character_id=?"),
            (row["id"], actor["id"]),
        ).fetchone()
        if (
            row["status"] not in {"accepted", "active"}
            or part["status"] != "waiting"
            or not from_iso(row["arrival_starts_at"]) <= at <= from_iso(row["arrival_ends_at"])
        ):
            raise ValueError("请在已确认的到场窗内开始本人尚未开始的分房休息")
        if actor["current_room_id"] != part["room_id"] or not InteriorService.near_location(
            c, actor, row["location_id"]
        ):
            raise ValueError("本人须先逐门实际进入自己约定的房间，不能隔屋或自动移动入住")
        if IntimacyService.paused(c, row["world_id"], row["proposer_id"], row["recipient_id"]):
            raise ValueError("一方已表达暂停联系的界限")
        if (
            actor["health"] <= 0
            or actor["satiety"] <= 30
            or not actor["is_player"]
            and FamilyService.hungry_dependent(c, actor, at)
        ):
            raise ValueError("本人需要先处理身体、用饭或已承担的照护")
        end = at + timedelta(minutes=row["minutes"])
        cls._access(c, row["world_id"], actor["id"], part["room_id"], row["location_id"], at, end)
        cls._schedule_free(c, (actor,), row, row["id"])
        aid = LifeActivityService.start(c, actor, at, "rest", row["minutes"])
        summary = "一位参与者在自己的真实房间开始已同意的完整休息；另一人的活动仍由本人决定。"
        event = cls._record(c, row, at, "checked_in", summary, actor["id"])
        c.execute(
            (
                "UPDATE journey_separate_stay_participants SET status='active',"
                "activity_id=?,checked_in_at=?,ends_at=?,source_event_id=? WHERE "
                "stay_id=? AND character_id=?"
            ),
            (aid, to_iso(at), to_iso(end), event, row["id"], actor["id"]),
        )
        c.execute(
            "UPDATE journey_separate_stays SET status='active' WHERE id=?",
            (row["id"],),
        )
        from world_engine.separate_stay_changes import SeparateStayChangeService

        SeparateStayChangeService.cancel_for_stay(c, row["id"], at)
        return {
            "status": "active",
            "participant_status": "active",
            "stay_id": row["id"],
            "event_id": event,
            "summary": summary,
        }

    @classmethod
    def _end_part(cls, c, row, part, at, status, reason):
        activity = c.execute(
            "SELECT * FROM character_life_activities WHERE id=? AND status='running'",
            (part["activity_id"],),
        ).fetchone()
        if activity:
            LifeActivityService.finish(
                c, activity, at, "completed" if status == "completed" else "interrupted", reason
            )
        event = cls._record(
            c,
            row,
            at,
            "participant_" + status,
            "一位参与者的分房休息"
            + (
                "已完成完整时长。"
                if status == "completed"
                else "未完整完成；本人状态或约定条件需要先处理。"
            ),
            part["character_id"],
        )
        c.execute(
            (
                "UPDATE journey_separate_stay_participants SET status=?,"
                "finished_at=?,source_event_id=?,reason=? WHERE stay_id=? AND "
                "character_id=? AND status IN ('waiting','active')"
            ),
            (status, to_iso(at), event, reason, row["id"], part["character_id"]),
        )
        return event

    @classmethod
    def _cancel(cls, c, row, at, status, reason):
        for part in cls._parts(c, row["id"]):
            if part["status"] in {"waiting", "active"}:
                cls._end_part(
                    c,
                    row,
                    part,
                    at,
                    "interrupted" if part["status"] == "active" else "cancelled",
                    reason,
                )
        event = cls._record(
            c,
            row,
            at,
            status,
            reason + " 已完成的个人休息、经过时间和消耗保留，不自动退房或恢复旅行。",
        )
        c.execute(
            (
                "UPDATE journey_separate_stays SET status=?,"
                "reunion_status='cancelled',source_event_id=?,reason=? WHERE id=?"
            ),
            (status, event, reason, row["id"]),
        )
        from world_engine.separate_stay_changes import SeparateStayChangeService

        SeparateStayChangeService.cancel_for_stay(c, row["id"], at)
        return {"status": status, "stay_id": row["id"], "event_id": event, "summary": reason}

    @classmethod
    def maintain(cls, c, wid, at, complete=True):
        from world_engine.families import FamilyService
        from world_engine.intimacy import IntimacyService
        from world_engine.separate_stay_changes import SeparateStayChangeService

        SeparateStayChangeService.maintain(c, wid, at)

        for found in c.execute(
            (
                "SELECT * FROM journey_separate_stays WHERE world_id=? AND "
                "(status IN ('pending','accepted','active') OR "
                "(status='completed' AND (reunion_status='waiting' OR "
                "(reunion_status='met' AND reunion_wait_ended_at IS NULL))))"
            ),
            (wid,),
        ).fetchall():
            row = dict(found)
            if row["reunion_status"] == "met":
                if cls._trip_resumed(c, row):
                    cls._end_reunion_wait(
                        c,
                        row,
                        at,
                        "双方已经明确恢复原旅行，本次会合后的等候结束；已完成的分房休息与真实会合事实保留。",
                    )
                elif at >= from_iso(row["reunion_ends_at"]):
                    cls._end_reunion_wait(
                        c,
                        row,
                        from_iso(row["reunion_ends_at"]),
                        "原约定会合等候窗口届满；已完成的休息与真实会合事实保留，后续安排另行决定。",
                    )
                elif IntimacyService.paused(c, wid, row["proposer_id"], row["recipient_id"]):
                    cls._end_reunion_wait(
                        c, row, at, "一方表达暂停联系的界限，会合后的等候结束；已经完成的事实保留。"
                    )
                continue
            if row["status"] == "pending":
                trip = c.execute(
                    "SELECT status,next_index FROM joint_journeys WHERE id=?", (row["journey_id"],)
                ).fetchone()
                if (
                    not trip
                    or trip["status"] not in {"paused", "resume_pending"}
                    or trip["next_index"] != row["stop_index"] + 1
                ):
                    cls._cancel(
                        c, row, at, "cancelled", "原旅行或停留站点改变，尚未确认的分房邀请撤销"
                    )
                elif at >= from_iso(row["arrival_starts_at"]):
                    cls._cancel(c, row, at, "expired", "分房住宿未在到场前取得双方同意")
                continue
            paused = IntimacyService.paused(c, wid, row["proposer_id"], row["recipient_id"])
            if paused:
                cls._cancel(c, row, at, "interrupted", "一方表达暂停联系的界限，后续共同承诺停止")
                continue
            for part in cls._parts(c, row["id"]):
                if part["status"] == "waiting":
                    if at > from_iso(row["arrival_ends_at"]):
                        cls._end_part(
                            c,
                            row,
                            part,
                            at,
                            "missed",
                            "本人未在到场窗内实际开始休息；不推定故意失约",
                        )
                    continue
                if part["status"] != "active":
                    continue
                person = c.execute(
                    "SELECT * FROM characters WHERE id=?", (part["character_id"],)
                ).fetchone()
                activity = c.execute(
                    "SELECT * FROM character_life_activities WHERE id=?", (part["activity_id"],)
                ).fetchone()
                reason = ""
                end = from_iso(part["ends_at"])
                try:
                    cls._access(
                        c,
                        wid,
                        person["id"],
                        part["room_id"],
                        row["location_id"],
                        max(
                            from_iso(part["checked_in_at"]),
                            min(at, end - timedelta(microseconds=1)),
                        ),
                        end,
                    )
                except ValueError:
                    reason = "本人房间或外层通路的许可已不再覆盖完整休息"
                if person["health"] <= 0 or person["satiety"] <= 5:
                    reason = "本人需要处理身体或用饭"
                elif person["current_room_id"] != part["room_id"]:
                    reason = "本人离开了仍在休息的约定房间"
                elif not person["is_player"] and FamilyService.hungry_dependent(c, person, at):
                    reason = "本人需要先处理已承担的照护"
                if not activity or activity["status"] not in {"running", "completed"}:
                    reason = reason or "本人提前结束了本次绑定的休息"
                elif (
                    person["health"] < activity["initial_health"]
                    or great_circle_distance_km(
                        person["longitude"],
                        person["latitude"],
                        activity["longitude"],
                        activity["latitude"],
                    )
                    > 0.001
                ):
                    reason = reason or "本人的身体或所在位置发生变化"
                if reason:
                    cls._end_part(c, row, part, at, "interrupted", reason)
                elif complete and at >= end and activity["status"] == "completed":
                    cls._end_part(c, row, part, end, "completed", "本人实际休息了完整约定时长")
            parts = cls._parts(c, row["id"])
            if any(p["status"] in {"missed", "interrupted", "cancelled"} for p in parts):
                if row["reunion_status"] == "waiting":
                    event = cls._record(
                        c,
                        row,
                        at,
                        "reunion_cancelled",
                        "后续共同会合承诺已取消；各自独立发生的休息事实保留，原旅行不自动恢复。",
                    )
                    c.execute(
                        "UPDATE journey_separate_stays SET reunion_event_id=? WHERE id=?",
                        (event, row["id"]),
                    )
                c.execute(
                    "UPDATE journey_separate_stays SET reunion_status='cancelled' WHERE id=?",
                    (row["id"],),
                )
                row["reunion_status"] = "cancelled"
            if all(p["status"] == "completed" for p in parts) and row["status"] != "completed":
                c.execute(
                    "UPDATE journey_separate_stays SET status='completed' WHERE id=?",
                    (row["id"],),
                )
                row["status"] = "completed"
            elif all(p["status"] not in {"waiting", "active"} for p in parts) and row["status"] in {
                "accepted",
                "active",
            }:
                status = "missed" if all(p["status"] == "missed" for p in parts) else "interrupted"
                c.execute(
                    "UPDATE journey_separate_stays SET status=? WHERE id=?",
                    (status, row["id"]),
                )
                row["status"] = status
            if row["reunion_status"] != "waiting":
                continue
            if at > from_iso(row["reunion_ends_at"]):
                event = cls._record(
                    c,
                    row,
                    at,
                    "reunion_missed",
                    "约定室外会合窗已过，双方未在窗口内实际会合；住宿事实保留，原旅行不自动恢复。",
                )
                c.execute(
                    (
                        "UPDATE journey_separate_stays SET reunion_status='missed',"
                        "reunion_event_id=? WHERE id=?"
                    ),
                    (event, row["id"]),
                )
            elif (
                row["status"] == "completed"
                and at >= from_iso(row["reunion_starts_at"])
                and cls._at_reunion(c, row)
            ):
                event = cls._record(
                    c,
                    row,
                    at,
                    "reunited",
                    "双方各自在不同的真实房间完成完整休息后，分别退屋并在原旅行室外站点真实会合；没有同房相处或身体接触的推定，恢复旅行仍需双方重新同意。",
                )
                c.execute(
                    (
                        "UPDATE journey_separate_stays SET reunion_status='met',"
                        "reunited_at=?,reunion_event_id=?,source_event_id=? WHERE id=?"
                    ),
                    (to_iso(at), event, event, row["id"]),
                )

    @classmethod
    def _at_reunion(cls, c, row):
        from world_engine.player_steering import PlayerSteeringService
        from world_engine.shared_life import SharedLifeService

        people = cls._people(c, row)
        place = c.execute(
            "SELECT * FROM locations WHERE id=? AND is_active=1", (row["location_id"],)
        ).fetchone()
        if not place or not SharedLifeService._near(*people, "talk"):
            return False
        return all(
            p["health"] > 0
            and not p["current_room_id"]
            and not p["current_fixture_id"]
            and not p["active_vehicle_id"]
            and p["movement_type"] == "land"
            and p["space_layer"] == place["space_layer"]
            and great_circle_distance_km(
                p["longitude"], p["latitude"], place["longitude"], place["latitude"]
            )
            <= 0.025
            and not c.execute(
                ("SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'"),
                (p["id"],),
            ).fetchone()
            and not LifeActivityService.running(c, p["id"])
            and not PlayerSteeringService.active(c, p["id"])
            for p in people
        )

    @classmethod
    def _departure(cls, c, actor, row):
        from world_engine.overnight import OvernightService

        return OvernightService._departure(
            c, actor, {**dict(row), "room_id": cls._room_for(row, actor["id"])}
        )

    @classmethod
    def planned_minutes(cls, c, actor, at):
        times = []
        for row in c.execute(
            (
                "SELECT s.*,p.status participant_status FROM "
                "journey_separate_stays s JOIN "
                "journey_separate_stay_participants p ON p.stay_id=s.id WHERE "
                "s.world_id=? AND p.character_id=? AND (s.status IN ('accepted',"
                "'active') OR (s.status='completed' AND "
                "(s.reunion_status='waiting' OR (s.reunion_status='met' AND "
                "s.reunion_wait_ended_at IS NULL AND s.reunion_ends_at>?))))"
            ),
            (actor["world_id"], actor["id"], to_iso(at)),
        ):
            if row["reunion_status"] == "met" and cls._trip_resumed(c, row):
                continue
            if row["participant_status"] == "waiting":
                times.append(
                    from_iso(row["arrival_starts_at"])
                    if actor["current_room_id"] == cls._room_for(row, actor["id"])
                    else cls._departure(c, actor, row)
                )
            elif row["participant_status"] == "completed" and row["reunion_status"] in {
                "waiting",
                "met",
            }:
                times.append(at)
        return max(0, int((min(times) - at).total_seconds() // 60)) if times else None

    @classmethod
    def perform(cls, c, npc, at):
        from world_engine.daily_life import DailyLifeService
        from world_engine.interiors import InteriorService

        if npc["is_player"] or npc["health"] < 40 or npc["energy"] <= 10 or npc["satiety"] <= 30:
            return None
        found = c.execute(
            (
                "SELECT s.*,p.status participant_status FROM "
                "journey_separate_stays s JOIN "
                "journey_separate_stay_participants p ON p.stay_id=s.id WHERE "
                "s.world_id=? AND p.character_id=? AND (s.status IN ('accepted',"
                "'active') OR (s.status='completed' AND "
                "(s.reunion_status='waiting' OR (s.reunion_status='met' AND "
                "s.reunion_wait_ended_at IS NULL AND s.reunion_ends_at>?)))) "
                "ORDER BY s.arrival_starts_at LIMIT 1"
            ),
            (npc["world_id"], npc["id"], to_iso(at)),
        ).fetchone()
        if not found:
            return None
        row = dict(found)
        if row["reunion_status"] == "met" and cls._trip_resumed(c, row):
            return None
        destination = c.execute(
            "SELECT * FROM locations WHERE id=? AND is_active=1", (row["location_id"],)
        ).fetchone()
        if not destination:
            return "分房住宿地点已不可用，等待重新商量"
        if (
            row["participant_status"] == "completed"
            and row["reunion_status"] in {"waiting", "met"}
            and at <= from_iso(row["reunion_ends_at"])
        ):
            if npc["current_room_id"]:
                return DailyLifeService._travel(c, npc, at, destination)
            if (
                great_circle_distance_km(
                    npc["longitude"],
                    npc["latitude"],
                    destination["longitude"],
                    destination["latitude"],
                )
                > 0.001
            ):
                from world_engine.movement import MovementService

                MovementService().start(
                    c,
                    world_id=npc["world_id"],
                    character_id=npc["id"],
                    destination_longitude=destination["longitude"],
                    destination_latitude=destination["latitude"],
                    world_time=at,
                    minimum_distance_km=0,
                )
                return "前往原室外站点会合"
            return "已回到原室外站点，等候约定会合"
        if (
            row["participant_status"] != "waiting"
            or at < cls._departure(c, npc, row)
            or at > from_iso(row["arrival_ends_at"])
        ):
            return None
        rid = cls._room_for(row, npc["id"])
        try:
            cls._access(
                c,
                row["world_id"],
                npc["id"],
                rid,
                row["location_id"],
                max(at, from_iso(row["arrival_starts_at"])),
                from_iso(row["latest_end_at"]),
            )
        except ValueError:
            return "本人房间或门路许可不足，等待重新商量分房住宿"
        if npc["current_room_id"] == rid:
            if at >= from_iso(row["arrival_starts_at"]):
                cls._start(c, npc, row, at)
                return "在本人约定房间开始完整分房休息"
            return "已到自己的住宿房间，等候约定休息时间"
        from world_engine.appointment_rooms import AppointmentRoomService

        chain = AppointmentRoomService.path(c, row["world_id"], rid)
        if (
            npc["current_room_id"]
            and npc["current_room_id"] not in {r["id"] for r in chain}
            or not npc["current_room_id"]
            and not InteriorService.near_location(c, npc, row["location_id"])
        ):
            return DailyLifeService._travel(c, npc, at, destination)
        target = next(r for r in chain if r["parent_room_id"] == npc["current_room_id"])
        if not InteriorService.allowed(c, target, npc["id"], at=at):
            return "本人房间入口尚不具备合法进入条件"
        if npc["current_fixture_id"]:
            InteriorService.operate_fixture(c, npc, npc["current_fixture_id"], "stand", at)
            npc = c.execute("SELECT * FROM characters WHERE id=?", (npc["id"],)).fetchone()
        if target["door_locked"]:
            if not InteriorService.can_unlock(c, target, npc["id"], at=at):
                return "本人仍需取得房间的真实钥匙或开门条件"
            InteriorService.door(c, npc, target["id"], "unlock", at)
        if not target["door_open"]:
            InteriorService.door(c, npc, target["id"], "open", at)
        InteriorService.door(c, npc, target["id"], "enter", at)
        return "经过本人分房住宿入口"

    @staticmethod
    def blocks_journey(c, journey_id, at):
        return bool(
            c.execute(
                (
                    "SELECT 1 FROM journey_separate_stays WHERE journey_id=? AND "
                    "(status IN ('accepted','active') OR (status='pending' AND "
                    "arrival_starts_at>?) OR (status='completed' AND "
                    "reunion_status='waiting' AND reunion_ends_at>=?))"
                ),
                (journey_id, to_iso(at), to_iso(at)),
            ).fetchone()
        )

    @classmethod
    def new_journey_conflict(cls, c, person, at, ends_at, final_location_id):
        from world_engine.overnight import OvernightService

        for row in c.execute(
            (
                "SELECT * FROM journey_separate_stays WHERE world_id=? AND "
                "(proposer_id=? OR recipient_id=?) AND (status IN ('accepted',"
                "'active') OR (status='completed' AND reunion_status='waiting'))"
            ),
            (person["world_id"], person["id"], person["id"]),
        ):
            until, _ = cls._reservation_end(c, row, person["id"])
            if until is None or at >= until:
                continue
            if at < until and ends_at >= from_iso(row["arrival_starts_at"]):
                return True
            gap = OvernightService._travel_gap(
                c,
                person,
                final_location_id,
                None,
                row["location_id"],
                cls._room_for(row, person["id"]),
            )
            if ends_at + gap > from_iso(row["arrival_starts_at"]):
                return True
        return False

    @classmethod
    def boundary(cls, c, wid, at, result, satiety_rate=3):
        for row in c.execute(
            (
                "SELECT * FROM journey_separate_stays WHERE world_id=? AND "
                "(status IN ('pending','accepted','active') OR "
                "(status='completed' AND reunion_status='waiting'))"
            ),
            (wid,),
        ):
            for key in (
                "arrival_starts_at",
                "arrival_ends_at",
                "reunion_starts_at",
                "reunion_ends_at",
            ):
                if at < from_iso(row[key]) < result:
                    result = from_iso(row[key])
            parts = cls._parts(c, row["id"])
            for part in parts:
                person = c.execute(
                    "SELECT * FROM characters WHERE id=?", (part["character_id"],)
                ).fetchone()
                if part["status"] == "waiting" and row["status"] != "pending":
                    depart = cls._departure(c, person, row)
                    if at < depart < result:
                        result = depart
                    if at >= from_iso(row["arrival_ends_at"]):
                        result = min(result, at + timedelta(seconds=1))
                if part["status"] == "active":
                    end = from_iso(part["ends_at"])
                    if at < end < result:
                        result = end
                    result = min(result, at + timedelta(minutes=15))
                    residual = c.execute(
                        (
                            "SELECT satiety_residual FROM character_state_accumulators WHERE "
                            "character_id=?"
                        ),
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
                if part["status"] == "completed" and row["reunion_status"] == "waiting":
                    result = min(result, at + timedelta(minutes=5))
            for due in c.execute(
                "SELECT next_world_time FROM npc_daily_states WHERE character_id IN (?,?)",
                (row["proposer_id"], row["recipient_id"]),
            ):
                if due[0] and at < from_iso(due[0]) < result:
                    result = from_iso(due[0])
            if row["reunion_status"] == "waiting" and at >= from_iso(row["reunion_ends_at"]):
                result = min(result, at + timedelta(seconds=1))
        from world_engine.separate_stay_changes import SeparateStayChangeService

        return SeparateStayChangeService.boundary(c, wid, at, result)
