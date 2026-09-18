"""约定时间窗、改期审核与 NPC 日程冲突；不以台词修改既定安排。"""

import json
import math
from datetime import datetime, timedelta
from uuid import uuid4

from world_engine.geo import great_circle_distance_km
from world_engine.repository import from_iso, to_iso, utc_now


class ScheduleError(ValueError):
    pass


class ScheduleService:
    @staticmethod
    def parse_window(terms, at):
        raw = terms.get("meeting_world_time")
        if not isinstance(raw, str):
            raise ScheduleError("请提供带时区的明确见面时间")
        try:
            # Python 3.11+ 的 fromisoformat 原生接受 "Z" 后缀。
            start = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ScheduleError("见面时间格式无效") from exc
        if start.tzinfo is None or start.utcoffset() is None:
            raise ScheduleError("见面时间必须包含时区")
        if not at < start <= at + timedelta(days=365):
            raise ScheduleError("见面时间须晚于现在且在一个世界年以内")
        minutes = terms.get("meeting_minutes", 60)
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 5 <= minutes <= 240:
            raise ScheduleError("见面时间窗必须为 5 至 240 个世界分钟")
        return start, start + timedelta(minutes=minutes)

    @classmethod
    def validate(cls, c, wid, player_id, npc_id, terms, at, *, amendment=False):
        start, end = cls.parse_window(terms, at)
        parent = None
        if amendment:
            if (
                not isinstance(terms.get("appointment_id"), str)
                or not terms["appointment_id"].strip()
            ):
                raise ScheduleError("请指定要改期的原约定")
            parent = c.execute(
                "SELECT f.*,w.revision FROM contract_fulfillments f LEFT JOIN appointment_windows w "
                "ON w.contract_id=f.request_id WHERE f.request_id=? AND f.world_id=? "
                "AND f.requester_id=? AND f.recipient_id=? AND f.kind='appointment' "
                "AND f.status IN ('active','overdue')",
                (terms.get("appointment_id"), wid, player_id, npc_id),
            ).fetchone()
            if parent is None:
                raise ScheduleError("原约定不存在、不属于双方或已经结束")
            if parent["required_units"] != 1:
                raise ScheduleError("旧约定包含多次履约，请结束旧约定后分别商议见面时间")
            revision = terms.get("expected_revision")
            if (
                isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision != (parent["revision"] or 0)
            ):
                raise ScheduleError("原约定已变化，请重新核对时间后提出改期")
            payment = terms.get("payment", terms.get("amount", 0))
            if type(payment) is not int or payment != 0:
                raise ScheduleError("改期不重新收款；原托管报酬保持不变")
            location_id = parent["location_id"]
        else:
            location_id = terms.get("location_id")
            if type(terms.get("work_units", 1)) is not int or terms.get("work_units", 1) != 1:
                raise ScheduleError("一个明确时间窗对应一次见面")
        if not isinstance(location_id, str) or not location_id.strip():
            raise ScheduleError("请指定有效的见面地点标识")
        location = c.execute(
            "SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1",
            (location_id, wid),
        ).fetchone()
        if location is None:
            raise ScheduleError("请指定当前世界中有效的见面地点")
        for person_id in (player_id, npc_id):
            actor = c.execute(
                "SELECT * FROM characters WHERE id=? AND world_id=?", (person_id, wid)
            ).fetchone()
            if actor is None or actor["health"] <= 0:
                raise ScheduleError("见面参与者当前无法赴约")
            arrival = cls.earliest_arrival(c, actor, location, at)
            if arrival >= end:
                raise ScheduleError("当前活动和路程无法在该时间窗内完成，请选择更晚的时间")
            for other in c.execute(
                "SELECT f.request_id,l.longitude,l.latitude,w.starts_world_time,w.ends_world_time "
                "FROM appointment_windows w JOIN contract_fulfillments f ON f.request_id=w.contract_id "
                "JOIN locations l ON l.id=f.location_id WHERE f.world_id=? AND f.status='active' "
                "AND (f.requester_id=? OR f.recipient_id=?)",
                (wid, person_id, person_id),
            ):
                if parent and other["request_id"] == parent["request_id"]:
                    continue
                travel = timedelta(
                    hours=great_circle_distance_km(
                        location["longitude"],
                        location["latitude"],
                        other["longitude"],
                        other["latitude"],
                    )
                    / max(1, actor["movement_speed_kmh"])
                )
                if start < from_iso(other["ends_world_time"]) + travel and end + travel > from_iso(
                    other["starts_world_time"]
                ):
                    raise ScheduleError("双方已有其他约定，时间或往返路程冲突")
        return start, end, location_id, parent

    @staticmethod
    def earliest_arrival(c, actor, location, at, *, include_activity=True):
        ready = at
        longitude, latitude = actor["longitude"], actor["latitude"]
        if include_activity:
            activity = c.execute(
                "SELECT ends_world_time FROM character_life_activities WHERE character_id=? AND status='running'",
                (actor["id"],),
            ).fetchone()
            if activity:
                ready = max(ready, from_iso(activity[0]))
        movement = c.execute(
            "SELECT * FROM character_movements WHERE character_id=? AND status='moving'",
            (actor["id"],),
        ).fetchone()
        if movement:
            ready = max(ready, from_iso(movement["estimated_arrival_world"]))
            longitude, latitude = (
                movement["destination_longitude"],
                movement["destination_latitude"],
            )
        if actor["current_room_id"]:
            room_id = actor["current_room_id"]
            for _ in range(4):
                if not room_id:
                    break
                ready += timedelta(minutes=5)
                row = c.execute(
                    "SELECT parent_room_id FROM life_rooms WHERE id=?", (room_id,)
                ).fetchone()
                room_id = row[0] if row else None
        distance = great_circle_distance_km(
            longitude, latitude, location["longitude"], location["latitude"]
        )
        return ready + timedelta(
            hours=(distance if distance > 0.1 else 0) / max(1, actor["movement_speed_kmh"])
        )

    @classmethod
    def apply_amendment(cls, c, request, terms, at):
        start, end, _, parent = cls.validate(
            c,
            request["world_id"],
            request["requester_id"],
            request["recipient_id"],
            terms,
            at,
            amendment=True,
        )
        old = c.execute(
            "SELECT * FROM appointment_windows WHERE contract_id=?", (parent["request_id"],)
        ).fetchone()
        revision = (parent["revision"] or 0) + 1
        c.execute(
            "INSERT INTO appointment_revisions VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                request["world_id"],
                parent["request_id"],
                request["id"],
                revision,
                old["starts_world_time"] if old else None,
                parent["due_world_time"],
                to_iso(start),
                to_iso(end),
                to_iso(at),
            ),
        )
        c.execute(
            "INSERT INTO appointment_windows VALUES (?,?,?,?,?) ON CONFLICT(contract_id) DO UPDATE SET starts_world_time=excluded.starts_world_time,ends_world_time=excluded.ends_world_time,revision=excluded.revision",
            (parent["request_id"], request["world_id"], to_iso(start), to_iso(end), revision),
        )
        c.execute(
            "UPDATE contract_fulfillments SET status='active',due_world_time=? WHERE request_id=?",
            (to_iso(end), parent["request_id"]),
        )
        c.execute(
            "UPDATE npc_todos SET due_world_time=?,status='open',updated_at=? WHERE contract_id=?",
            (to_iso(end), to_iso(utc_now()), parent["request_id"]),
        )
        c.execute(
            "UPDATE npc_outreach_jobs SET status='cancelled',error='原约定时间已经改变' WHERE world_id=? AND status IN ('pending','failed','processing') AND source_event_id IN (SELECT id FROM world_events WHERE event_type='schedule.at_risk' AND json_extract(payload_json,'$.appointment_id')=?)",
            (request["world_id"], parent["request_id"]),
        )
        # 旧履约回执属于旧时间窗；保留审计，不能拿来完成新时间窗。
        return to_iso(end)

    @classmethod
    def assess(cls, c, actor, at):
        contract = c.execute(
            "SELECT f.*,w.starts_world_time,w.ends_world_time,w.revision,l.longitude,l.latitude,l.is_active "
            "FROM contract_fulfillments f LEFT JOIN appointment_windows w ON w.contract_id=f.request_id "
            "JOIN locations l ON l.id=f.location_id WHERE f.world_id=? AND f.recipient_id=? "
            "AND f.kind='appointment' AND f.status='active' AND f.due_world_time>? ORDER BY f.due_world_time LIMIT 1",
            (actor["world_id"], actor["id"], to_iso(at)),
        ).fetchone()
        if contract is None:
            return None
        start = from_iso(contract["starts_world_time"] or contract["due_world_time"]) - (
            timedelta(0) if contract["starts_world_time"] else timedelta(hours=1)
        )
        end = from_iso(contract["due_world_time"])
        travel = cls.earliest_arrival(c, actor, contract, at, include_activity=False) - at
        depart = start - travel - timedelta(minutes=10)
        reason = ""
        if at >= depart - timedelta(hours=2):
            if not contract["is_active"]:
                reason = "约定地点已经不可用"
            elif actor["health"] < 40 or actor["satiety"] <= 10 or actor["energy"] <= 10:
                reason = "需要先处理身体状况"
            elif cls.earliest_arrival(c, actor, contract, at) >= end:
                reason = "当前活动或路程可能导致迟到"
        return {"contract": contract, "depart_at": depart, "due": at >= depart, "reason": reason}

    @classmethod
    def record_assessment(cls, c, actor, at, plan):
        from world_engine.actions import ActionService

        signature = (
            json.dumps(
                [
                    plan["contract"]["request_id"],
                    plan["contract"]["revision"],
                    plan["due"],
                    plan["reason"],
                ],
                ensure_ascii=False,
            )
            if plan
            else "none"
        )
        previous = c.execute(
            "SELECT signature,source_event_id FROM npc_schedule_assessments WHERE character_id=?",
            (actor["id"],),
        ).fetchone()
        if previous and previous[0] == signature:
            return False
        if previous and previous["source_event_id"]:
            c.execute(
                "UPDATE npc_outreach_jobs SET status='cancelled',error='日程判断已经变化' WHERE source_event_id=? AND status IN ('pending','failed','processing')",
                (previous["source_event_id"],),
            )
        event = None
        if plan and plan["reason"]:
            event = ActionService._record_event(
                c,
                world_id=actor["world_id"],
                tick_id=str(uuid4()),
                occurred_at=at,
                event_type="schedule.at_risk",
                actor_id=actor["id"],
                target_id=plan["contract"]["requester_id"],
                location_id=plan["contract"]["location_id"],
                summary="约定可能延误；原时间仍然有效，可以重新商议时间。",
                payload={"appointment_id": plan["contract"]["request_id"]},
            )
            c.execute(
                "INSERT OR IGNORE INTO player_notifications(id,world_id,recipient_id,event_id,title,created_at) VALUES (?,?,?,?,?,?)",
                (
                    str(uuid4()),
                    actor["world_id"],
                    plan["contract"]["requester_id"],
                    event,
                    "一项约定可能延误，可查看时间并提出改期。",
                    to_iso(at),
                ),
            )
        c.execute(
            "INSERT INTO npc_schedule_assessments VALUES (?,?,?,?,?,?,?) ON CONFLICT(character_id) DO UPDATE SET signature=excluded.signature,contract_id=excluded.contract_id,reason=excluded.reason,checked_world_time=excluded.checked_world_time,source_event_id=excluded.source_event_id",
            (
                actor["id"],
                actor["world_id"],
                signature,
                plan["contract"]["request_id"] if plan else None,
                plan["reason"] if plan else "",
                to_iso(at),
                event,
            ),
        )
        return True

    @staticmethod
    def planned_minutes(plan, at):
        if plan is None:
            return None
        return max(0, math.floor((plan["depart_at"] - at).total_seconds() / 60))

    @staticmethod
    def list_for(c, wid, player_id):
        return [
            dict(row)
            for row in c.execute(
                "SELECT f.request_id AS id,f.recipient_id,f.location_id,f.status,f.due_world_time,"
                "w.starts_world_time,w.ends_world_time,COALESCE(w.revision,0) AS revision "
                "FROM contract_fulfillments f LEFT JOIN appointment_windows w ON w.contract_id=f.request_id "
                "WHERE f.world_id=? AND f.requester_id=? AND f.kind='appointment' AND f.status IN ('active','overdue') ORDER BY f.due_world_time",
                (wid, player_id),
            )
        ]
