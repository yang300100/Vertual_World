"""哥哥，改期只是新的商量，另一人确认前原来的分房承诺继续生效。"""

import json
from datetime import datetime, timedelta
from uuid import uuid4

from world_engine.repository import from_iso, to_iso

SEPARATE_STAY_CHANGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS journey_separate_stay_changes (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 stay_id TEXT NOT NULL REFERENCES journey_separate_stays(id) ON DELETE CASCADE,
 proposer_id TEXT NOT NULL REFERENCES characters(id),
 recipient_id TEXT NOT NULL REFERENCES characters(id),
 status TEXT NOT NULL CHECK(status IN (
  'pending','accepted','declined','withdrawn','expired','cancelled')),
 base_event_id TEXT NOT NULL REFERENCES world_events(id),base_terms_json TEXT NOT NULL,
 old_arrival_starts_at TEXT NOT NULL,old_arrival_ends_at TEXT NOT NULL,
 old_latest_end_at TEXT NOT NULL,old_reunion_ends_at TEXT NOT NULL,
 new_arrival_starts_at TEXT NOT NULL,new_arrival_ends_at TEXT NOT NULL,
 new_latest_end_at TEXT NOT NULL,new_reunion_ends_at TEXT NOT NULL,
 statement TEXT NOT NULL,response TEXT NOT NULL DEFAULT '',proposed_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,proposal_event_id TEXT NOT NULL REFERENCES world_events(id),
 source_event_id TEXT NOT NULL REFERENCES world_events(id),reason TEXT NOT NULL DEFAULT '',
 CHECK(proposer_id<>recipient_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_separate_change_pending
 ON journey_separate_stay_changes(stay_id) WHERE status='pending';
CREATE INDEX IF NOT EXISTS idx_separate_change_due
 ON journey_separate_stay_changes(world_id,status,expires_at);
"""


class SeparateStayChangeService:
    BASE_KEYS = (
        "world_id",
        "proposer_id",
        "recipient_id",
        "journey_id",
        "stop_index",
        "source_lodging_plan_id",
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
    TIME_KEYS = ("arrival_starts_at", "arrival_ends_at", "latest_end_at", "reunion_ends_at")

    @staticmethod
    def _stay(c, wid, sid, first, second):
        row = c.execute(
            "SELECT * FROM journey_separate_stays WHERE id=? AND world_id=? "
            "AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
            (sid, wid, first, second, second, first),
        ).fetchone()
        if not row:
            raise ValueError("这份分房安排不属于双方")
        return dict(row)

    @classmethod
    def _unstarted(cls, c, row, at):
        if row["status"] != "accepted" or at >= from_iso(row["arrival_starts_at"]):
            return False
        parts = c.execute(
            "SELECT * FROM journey_separate_stay_participants WHERE stay_id=?", (row["id"],)
        ).fetchall()
        return {p["character_id"] for p in parts} == {
            row["proposer_id"],
            row["recipient_id"],
        } and all(
            p["status"] == "waiting"
            and not any(
                p[k]
                for k in (
                    "activity_id",
                    "checked_in_at",
                    "ends_at",
                    "finished_at",
                )
            )
            for p in parts
        )

    @classmethod
    def _base_matches(cls, row, request):
        try:
            return row["source_event_id"] == request["base_event_id"] and {
                key: row[key] for key in cls.BASE_KEYS
            } == json.loads(request["base_terms_json"])
        except (KeyError, TypeError, ValueError):
            return False

    @classmethod
    def _candidate(cls, row, start, at):
        if start.tzinfo is None or not at < start <= at + timedelta(days=1):
            raise ValueError("新的入住开始须在未来一个世界日内并带时区")
        if start == from_iso(row["arrival_starts_at"]):
            raise ValueError("新的入住时间与原约定相同，无需商量改期")
        candidate = dict(row)
        delta = start - from_iso(row["arrival_starts_at"])
        for key in cls.TIME_KEYS:
            candidate[key] = to_iso(from_iso(row[key]) + delta)
        candidate["reunion_starts_at"] = candidate["latest_end_at"]
        return candidate

    @staticmethod
    def _record(c, request, at, state, summary, actor_id=None):
        from world_engine.appointments import AppointmentService

        first_id = actor_id or request["proposer_id"]
        second_id = (
            request["recipient_id"]
            if first_id == request["proposer_id"]
            else request["proposer_id"]
        )
        first = c.execute("SELECT * FROM characters WHERE id=?", (first_id,)).fetchone()
        second = c.execute("SELECT * FROM characters WHERE id=?", (second_id,)).fetchone()
        payload = {
            key: request[key]
            for key in (
                "stay_id",
                "base_event_id",
                "old_arrival_starts_at",
                "old_arrival_ends_at",
                "old_latest_end_at",
                "old_reunion_ends_at",
                "new_arrival_starts_at",
                "new_arrival_ends_at",
                "new_latest_end_at",
                "new_reunion_ends_at",
                "expires_at",
            )
        }
        payload["change_id"] = request["id"]
        return AppointmentService._record(
            c,
            first,
            second,
            at,
            "separate_stay_change_" + state,
            summary,
            payload,
        )

    @classmethod
    def _finish(cls, c, request, at, status, reason, response="", actor_id=None):
        event = cls._record(c, request, at, status, reason + response, actor_id)
        c.execute(
            "UPDATE journey_separate_stay_changes SET status=?,response=?,source_event_id=?,"
            "reason=? "
            "WHERE id=? AND status='pending'",
            (status, response, event, reason, request["id"]),
        )
        return {
            "status": status,
            "stay_id": request["stay_id"],
            "change_id": request["id"],
            "event_id": event,
            "summary": reason,
        }

    @staticmethod
    def _validate(c, row, actor, other, at):
        from world_engine.interiors import InteriorService
        from world_engine.journey_room_appointments import JourneyRoomService
        from world_engine.separate_stays import SeparateStayService

        trip, stop = JourneyRoomService.validate(
            c,
            actor,
            other,
            at,
            row["journey_id"],
            row["location_id"],
            from_iso(row["reunion_ends_at"]),
        )
        if (
            trip["status"] != "paused"
            or trip["source_event_id"] != row["pause_event_id"]
            or stop["stop_index"] != row["stop_index"]
            or stop["source_event_id"] != row["arrival_event_id"]
        ):
            raise ValueError("原旅行、站点或暂停约定已变化，请重新商量")
        SeparateStayService._lodging_source(c, row, at)
        SeparateStayService._rooms_valid(c, row, at)
        SeparateStayService._schedule_free(c, (actor, other), row, row["id"])
        SeparateStayService._arrival_possible(c, (actor, other), row, at)
        # 哥哥，没有订房来源的旧安排也要重新核对真实钥匙，改期不创造新的开门权。
        for cid in (row["proposer_id"], row["recipient_id"]):
            for room in SeparateStayService._access(
                c,
                row["world_id"],
                cid,
                SeparateStayService._room_for(row, cid),
                row["location_id"],
                from_iso(row["arrival_starts_at"]),
                from_iso(row["latest_end_at"]),
            ):
                policy = c.execute(
                    "SELECT key_item_type_id FROM life_door_policies WHERE room_id=?", (room["id"],)
                ).fetchone()
                if (room["door_locked"] or policy and policy[0]) and not InteriorService.can_unlock(
                    c, room, cid, at=at
                ):
                    raise ValueError("本人尚缺分房房间或外层门路需要的真实钥匙")

    @classmethod
    def apply(cls, c, actor, other, at, choice):
        from world_engine.intimacy import IntimacyService

        row = cls._stay(c, actor["world_id"], choice.stay_id, actor["id"], other["id"])
        if choice.operation != "withdraw_change":
            IntimacyService._reachable(c, actor, other)
            if IntimacyService.paused(c, row["world_id"], actor["id"], other["id"]):
                raise ValueError("一方要求留出空间，暂不商量分房改期")
        if choice.operation == "reschedule":
            if not cls._unstarted(c, row, at):
                raise ValueError("只能在原入住窗开始前为尚未开始的已确认分房安排商量改期")
            if c.execute(
                "SELECT 1 FROM journey_separate_stay_changes WHERE stay_id=? AND status='pending'",
                (row["id"],),
            ).fetchone():
                raise ValueError("这份分房安排已有待回应的改期提议")
            try:
                start = datetime.fromisoformat(choice.arrival_world_time)
            except (ValueError, TypeError) as exc:
                raise ValueError("请提供带时区的明确新入住时间") from exc
            candidate = cls._candidate(row, start, at)
            cls._validate(c, candidate, actor, other, at)
            request = {
                "id": str(uuid4()),
                "world_id": row["world_id"],
                "stay_id": row["id"],
                "proposer_id": actor["id"],
                "recipient_id": other["id"],
                "base_event_id": row["source_event_id"],
                "base_terms_json": json.dumps(
                    {key: row[key] for key in cls.BASE_KEYS}, ensure_ascii=False
                ),
                "statement": choice.statement,
                "proposed_at": to_iso(at),
                "expires_at": to_iso(min(from_iso(row["arrival_starts_at"]), start)),
            }
            request.update({"old_" + key: row[key] for key in cls.TIME_KEYS})
            request.update({"new_" + key: candidate[key] for key in cls.TIME_KEYS})
            event = cls._record(
                c,
                request,
                at,
                "proposed",
                "提出分房改期，等待另一方独立同意；原时间仍然有效。" + choice.statement,
                actor["id"],
            )
            columns = tuple(request)
            c.execute(
                "INSERT INTO journey_separate_stay_changes("
                + ",".join(columns)
                + ",status,proposal_event_id,source_event_id) VALUES ("
                + ",".join("?" for _ in columns)
                + ",'pending',?,?)",
                tuple(request[key] for key in columns) + (event, event),
            )
            return {
                "status": "pending",
                "stay_id": row["id"],
                "change_id": request["id"],
                "event_id": event,
                "summary": "改期提议已记录；另一方确认前，原分房时间继续生效。",
            }
        found = c.execute(
            "SELECT * FROM journey_separate_stay_changes WHERE id=? AND stay_id=? AND world_id=?",
            (choice.change_id, row["id"], row["world_id"]),
        ).fetchone()
        if not found or found["status"] != "pending":
            raise ValueError("请回应这份安排中尚有效的真实改期提议")
        request = dict(found)
        if choice.operation == "withdraw_change":
            if actor["id"] != request["proposer_id"]:
                raise ValueError("只有本次改期的提出者可以撤回提议")
            return cls._finish(
                c,
                request,
                at,
                "withdrawn",
                "改期提议已撤回；原分房时间仍按原约定生效。",
                choice.statement,
                actor["id"],
            )
        if actor["id"] != request["recipient_id"]:
            raise ValueError("只有收到本次改期提议的另一方可以接受或婉拒")
        if choice.operation == "decline_change":
            return cls._finish(
                c,
                request,
                at,
                "declined",
                "改期提议已婉拒；原分房时间仍按原约定生效。",
                choice.statement,
                actor["id"],
            )
        if (
            at >= from_iso(request["expires_at"])
            or not cls._unstarted(c, row, at)
            or not cls._base_matches(row, request)
        ):
            raise ValueError("原分房安排或改期有效期已变化，请重新商量")
        candidate = cls._candidate(row, from_iso(request["new_arrival_starts_at"]), at)
        cls._validate(c, candidate, actor, other, at)
        result = cls._finish(
            c,
            request,
            at,
            "accepted",
            "双方确认分房改期；各自房间、完整休息时长和会合窗长度不变，仍须各自实际入住。",
            choice.statement,
            actor["id"],
        )
        c.execute(
            "UPDATE journey_separate_stays SET arrival_starts_at=?,arrival_ends_at=?,"
            "latest_end_at=?,"
            "reunion_starts_at=?,reunion_ends_at=?,source_event_id=? WHERE id=?",
            tuple(
                candidate[key]
                for key in (
                    "arrival_starts_at",
                    "arrival_ends_at",
                    "latest_end_at",
                    "reunion_starts_at",
                    "reunion_ends_at",
                )
            )
            + (result["event_id"], row["id"]),
        )
        npc = actor if not actor["is_player"] else other
        c.execute(
            "INSERT INTO npc_daily_states(character_id,world_id,next_world_time) VALUES (?,?,?) "
            "ON CONFLICT(character_id) DO UPDATE SET next_world_time=excluded.next_world_time",
            (npc["id"], row["world_id"], to_iso(at)),
        )
        return result

    @classmethod
    def cancel_for_stay(cls, c, sid, at, reason="原分房安排已变化，待回应的改期提议停止。"):
        for row in c.execute(
            "SELECT * FROM journey_separate_stay_changes WHERE stay_id=? AND status='pending'",
            (sid,),
        ).fetchall():
            cls._finish(c, dict(row), at, "cancelled", reason)

    @classmethod
    def maintain(cls, c, wid, at):
        from world_engine.intimacy import IntimacyService

        for found in c.execute(
            "SELECT * FROM journey_separate_stay_changes WHERE world_id=? AND status='pending'",
            (wid,),
        ).fetchall():
            request = dict(found)
            row = c.execute(
                "SELECT * FROM journey_separate_stays WHERE id=?", (request["stay_id"],)
            ).fetchone()
            if at >= from_iso(request["expires_at"]):
                cls._finish(
                    c,
                    request,
                    from_iso(request["expires_at"]),
                    "expired",
                    "改期未在原、新入住开始前取得双方同意；原分房安排按其实际状态继续处理。",
                )
            elif (
                not row
                or not cls._unstarted(c, dict(row), at)
                or not cls._base_matches(dict(row), request)
            ):
                cls._finish(c, request, at, "cancelled", "原分房安排已变化，待回应的改期提议停止。")
            elif IntimacyService.paused(c, wid, request["proposer_id"], request["recipient_id"]):
                cls._finish(
                    c, request, at, "cancelled", "一方表达暂停联系的界限，待回应的改期提议停止。"
                )

    @staticmethod
    def rows(c, sid, at, *, compact=False, observer_id=None):
        keys = (
            "id",
            "stay_id",
            "proposer_id",
            "recipient_id",
            "status",
            "statement",
            "response",
            "proposed_at",
            "expires_at",
            "old_arrival_starts_at",
            "old_arrival_ends_at",
            "old_latest_end_at",
            "old_reunion_ends_at",
            "new_arrival_starts_at",
            "new_arrival_ends_at",
            "new_latest_end_at",
            "new_reunion_ends_at",
        )
        rows = c.execute(
            "SELECT * FROM journey_separate_stay_changes WHERE stay_id=? "
            "ORDER BY CASE WHEN status='pending' THEN 0 ELSE 1 END,rowid DESC LIMIT 2",
            (sid,),
        ).fetchall()
        result = []
        for row in rows:
            value = {key: row[key] for key in keys}
            if value["status"] == "pending" and at >= from_iso(value["expires_at"]):
                value["status"] = "expired"
            if compact:
                if value["status"] != "pending":
                    continue
                value = {
                    key: value[key]
                    for key in (
                        "id",
                        "stay_id",
                        "proposer_id",
                        "recipient_id",
                        "status",
                        "new_arrival_starts_at",
                        "new_arrival_ends_at",
                        "new_latest_end_at",
                        "new_reunion_ends_at",
                        "expires_at",
                        "statement",
                    )
                }
            result.append(value)
        if observer_id is not None:
            from world_engine.intimacy import IntimacyService

            stay = c.execute(
                "SELECT world_id FROM journey_separate_stays WHERE id=?", (sid,)
            ).fetchone()
            if stay:
                IntimacyService.mask_records(
                    c, stay["world_id"], observer_id, result, ("statement", "response")
                )
        return result

    @staticmethod
    def boundary(c, wid, at, result):
        found = c.execute(
            "SELECT MIN(expires_at) FROM journey_separate_stay_changes "
            "WHERE world_id=? AND status='pending' AND expires_at>?",
            (wid, to_iso(at)),
        ).fetchone()
        return min(result, from_iso(found[0])) if found and found[0] else result
