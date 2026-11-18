"""哥哥，旅行停留和共同用餐休息分别同意，真实消耗与原行程都要保留。"""

from world_engine.geo import great_circle_distance_km
from world_engine.repository import from_iso, to_iso

JOURNEY_BREAK_SCHEMA = """
CREATE TABLE IF NOT EXISTS journey_breaks (
 moment_id TEXT PRIMARY KEY REFERENCES shared_life_moments(id) ON DELETE CASCADE,
 journey_id TEXT NOT NULL REFERENCES joint_journeys(id) ON DELETE CASCADE,
 stop_index INTEGER NOT NULL,
 arrival_event_id TEXT NOT NULL REFERENCES world_events(id),
 pause_event_id TEXT NOT NULL REFERENCES world_events(id),
 FOREIGN KEY(journey_id,stop_index) REFERENCES journey_stops(journey_id,stop_index)
);
CREATE INDEX IF NOT EXISTS idx_journey_breaks_trip ON journey_breaks(journey_id,stop_index);
"""


class JourneyBreakService:
    @staticmethod
    def expire_pending(c, wid, at):
        from world_engine.shared_life import SharedLifeService

        for row in c.execute(
            "SELECT m.* FROM journey_breaks b JOIN shared_life_moments m ON m.id=b.moment_id "
            "JOIN joint_journeys j ON j.id=b.journey_id JOIN journey_stops s ON s.journey_id=b.journey_id AND s.stop_index=b.stop_index "
            "JOIN locations l ON l.id=s.location_id WHERE m.world_id=? AND m.status='pending' AND "
            "(j.status NOT IN ('paused','resume_pending') OR j.deadline_at<=? OR m.expires_at<=? OR l.is_active=0 OR j.next_index<>b.stop_index+1)",
            (wid, to_iso(at), to_iso(at)),
        ).fetchall():
            first = c.execute(
                "SELECT * FROM characters WHERE id=?", (row["proposer_id"],)
            ).fetchone()
            second = c.execute(
                "SELECT * FROM characters WHERE id=?", (row["recipient_id"],)
            ).fetchone()
            event = SharedLifeService._record(
                c,
                first,
                second,
                at,
                "cancelled",
                "原旅行或停留邀请已失效，本次待回应相处撤销；没有开始活动或消耗食物。",
                {"moment_id": row["id"], "journey_break": JourneyBreakService.link(c, row["id"])},
            )
            c.execute(
                "UPDATE shared_life_moments SET status='cancelled',reason='原旅行或停留邀请已失效',source_event_id=? WHERE id=?",
                (event, row["id"]),
            )

    @staticmethod
    def validate(c, actor, other, at, journey_id):
        row = c.execute(
            "SELECT * FROM joint_journeys WHERE id=? AND world_id=? AND "
            "((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
            (journey_id, actor["world_id"], actor["id"], other["id"], other["id"], actor["id"]),
        ).fetchone()
        if (
            not row
            or row["status"] not in {"active", "paused"}
            or row["phase"] != "stopped"
            or at >= from_iso(row["deadline_at"])
        ):
            raise ValueError("只能在双方已实际到站、尚未结束的行程中商量停下用餐或休息")
        stop = c.execute(
            "SELECT s.*,l.longitude,l.latitude,l.is_active FROM journey_stops s "
            "JOIN locations l ON l.id=s.location_id WHERE s.journey_id=? AND s.stop_index=?",
            (row["id"], row["next_index"] - 1),
        ).fetchone()
        if (
            not stop
            or not stop["arrived_at"]
            or not stop["source_event_id"]
            or not stop["is_active"]
        ):
            raise ValueError("原停留站点缺少真实到站回执或已经不可用")
        for person in (actor, other):
            if (
                person["current_room_id"]
                or person["active_vehicle_id"]
                or person["movement_type"] != "land"
                or great_circle_distance_km(
                    person["longitude"], person["latitude"], stop["longitude"], stop["latitude"]
                )
                > 0.025
                or c.execute(
                    "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                    (person["id"],),
                ).fetchone()
            ):
                raise ValueError("双方需要实际留在同一室外停留站点，先停止其他移动")
        from world_engine.shared_life import SharedLifeService

        if not SharedLifeService._near(actor, other, "meal"):
            raise ValueError("请双方先实际会合到二十米内，再邀请停下用餐或休息")
        return row, stop

    @classmethod
    def prepare(cls, c, actor, other, at, journey_id):
        from world_engine.journeys import JourneyService

        row, stop = cls.validate(c, actor, other, at, journey_id)
        if row["status"] == "active":
            JourneyService.pause(
                c, row, at, "本人选择暂停原行程，另行邀请到站用餐或休息；结束后仍须双方重新同意恢复"
            )
            row = c.execute("SELECT * FROM joint_journeys WHERE id=?", (journey_id,)).fetchone()
        return {
            "journey_id": journey_id,
            "stop_index": stop["stop_index"],
            "arrival_event_id": stop["source_event_id"],
            "pause_event_id": row["source_event_id"],
        }

    @staticmethod
    def link(c, moment_id):
        row = c.execute("SELECT * FROM journey_breaks WHERE moment_id=?", (moment_id,)).fetchone()
        return dict(row) if row else None

    @classmethod
    def validate_acceptance(cls, c, moment, actor, other, at):
        link = cls.link(c, moment["id"])
        if not link:
            return None
        row, stop = cls.validate(c, actor, other, at, link["journey_id"])
        if (
            row["status"] != "paused"
            or stop["stop_index"] != link["stop_index"]
            or stop["source_event_id"] != link["arrival_event_id"]
        ):
            raise ValueError("原旅行的停留安排已经变化，请另行商量，不把旧邀请套到新站点")
        return link
