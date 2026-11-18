"""哥哥，到站后的室内短休沿用约会与租住许可，谁也不被代为入屋。"""

from world_engine.repository import from_iso, to_iso

JOURNEY_ROOM_SCHEMA = """
CREATE TABLE IF NOT EXISTS journey_room_appointments (
 appointment_id TEXT PRIMARY KEY REFERENCES social_appointments(id) ON DELETE CASCADE,
 journey_id TEXT NOT NULL REFERENCES joint_journeys(id) ON DELETE CASCADE,
 stop_index INTEGER NOT NULL,
 arrival_event_id TEXT NOT NULL REFERENCES world_events(id),
 pause_event_id TEXT NOT NULL REFERENCES world_events(id),
 FOREIGN KEY(journey_id,stop_index) REFERENCES journey_stops(journey_id,stop_index)
);
CREATE INDEX IF NOT EXISTS idx_journey_room_trip ON journey_room_appointments(journey_id);
"""


class JourneyRoomService:
    @staticmethod
    def link(c, appointment_id):
        row = c.execute(
            "SELECT * FROM journey_room_appointments WHERE appointment_id=?", (appointment_id,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def validate(c, actor, other, at, journey_id, location_id, end):
        row = c.execute(
            "SELECT * FROM joint_journeys WHERE id=? AND world_id=? AND "
            "((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
            (journey_id, actor["world_id"], actor["id"], other["id"], other["id"], actor["id"]),
        ).fetchone()
        if (
            not row
            or row["status"] not in {"active", "paused"}
            or row["phase"] != "stopped"
            or from_iso(row["deadline_at"]) <= at
        ):
            raise ValueError("室内短休只能衔接双方已真实到站、尚未结束的原旅行")
        stop = c.execute(
            "SELECT * FROM journey_stops WHERE journey_id=? AND stop_index=?",
            (row["id"], row["next_index"] - 1),
        ).fetchone()
        if (
            not stop
            or not stop["arrived_at"]
            or not stop["source_event_id"]
            or stop["location_id"] != location_id
        ):
            raise ValueError("室内短休的房间必须属于原行程已真实到达的当前停留站点")
        if end > from_iso(row["deadline_at"]):
            raise ValueError("室内安排的时间窗超过原旅行期限，请缩短、改期或另行结束旅行再商量")
        return row, stop

    @classmethod
    def prepare(cls, c, actor, other, at, choice, start, end, current=None):
        source = cls.link(c, current["id"]) if current else None
        jid = choice.journey_id or (source["journey_id"] if source else None)
        if not jid:
            return None
        if (
            choice.kind != "rest"
            or not choice.room_id
            or choice.repeat_count != 1
            or choice.repeat_every_days
            or choice.repairs_id
        ):
            raise ValueError("到站室内安排只支持单次休息，房租和其它活动仍需另行商量")
        row, stop = cls.validate(c, actor, other, at, jid, choice.location_id, end)
        if source and source["stop_index"] != stop["stop_index"]:
            raise ValueError("原旅行站点已变化，旧室内安排不能悄悄改为别处的旅途相处")
        from world_engine.journeys import JourneyService

        if not current:
            JourneyService._assert_breaks_finished(c, row, at)
            from world_engine.journey_breaks import JourneyBreakService

            row, stop = JourneyBreakService.validate(c, actor, other, at, jid)
        if row["status"] == "active":
            JourneyService.pause(
                c,
                row,
                at,
                "本人选择暂停原旅行，另行商量到站室内短休；进屋、房租、接待与恢复分别确认",
            )
            row = c.execute("SELECT * FROM joint_journeys WHERE id=?", (jid,)).fetchone()
        return {
            "journey_id": jid,
            "stop_index": stop["stop_index"],
            "arrival_event_id": stop["source_event_id"],
            "pause_event_id": row["source_event_id"],
        }

    @staticmethod
    def save(c, appointment_id, link):
        if link:
            c.execute(
                "INSERT INTO journey_room_appointments VALUES (?,?,?,?,?)",
                (
                    appointment_id,
                    link["journey_id"],
                    link["stop_index"],
                    link["arrival_event_id"],
                    link["pause_event_id"],
                ),
            )

    @staticmethod
    def assert_finished(c, jid, at):
        if c.execute(
            "SELECT 1 FROM journey_room_appointments b JOIN social_appointments a ON a.id=b.appointment_id "
            "WHERE b.journey_id=? AND (a.status IN ('accepted','active') OR (a.status='pending' AND a.starts_at>?))",
            (jid, to_iso(at)),
        ).fetchone():
            raise ValueError("请先处理关联的室内休息安排，并实际回到室外停留站点，再商量恢复旅行")

    @staticmethod
    def activity_risk(c, moment_id, at):
        from world_engine.appointment_rooms import AppointmentRoomService

        row = c.execute(
            "SELECT a.* FROM social_appointments a JOIN journey_room_appointments b ON b.appointment_id=a.id WHERE a.moment_id=? AND a.status='active'",
            (moment_id,),
        ).fetchone()
        return AppointmentRoomService.risk(c, row, at) if row else ""

    @classmethod
    def expire_pending(cls, c, wid, at):
        from world_engine.appointments import AppointmentService

        for row in c.execute(
            "SELECT a.* FROM journey_room_appointments b JOIN social_appointments a ON a.id=b.appointment_id "
            "JOIN joint_journeys j ON j.id=b.journey_id JOIN journey_stops s ON s.journey_id=b.journey_id AND s.stop_index=b.stop_index "
            "JOIN locations l ON l.id=s.location_id WHERE a.world_id=? AND a.status='pending' AND "
            "(j.status NOT IN ('paused','resume_pending') OR j.deadline_at<=? OR l.is_active=0 OR j.next_index<>b.stop_index+1)",
            (wid, to_iso(at)),
        ).fetchall():
            first = c.execute(
                "SELECT * FROM characters WHERE id=?", (row["proposer_id"],)
            ).fetchone()
            second = c.execute(
                "SELECT * FROM characters WHERE id=?", (row["recipient_id"],)
            ).fetchone()
            event = AppointmentService._record(
                c,
                first,
                second,
                at,
                "cancelled",
                "原旅行或停留站点已失效，尚未确认的室内短休邀请撤销；没有付房费或代为入屋。",
                {"appointment_id": row["id"], "journey_room": cls.link(c, row["id"])},
            )
            c.execute(
                "UPDATE social_appointments SET status='cancelled',reason='原旅行或停留站点已失效',source_event_id=? WHERE id=?",
                (event, row["id"]),
            )
