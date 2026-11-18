"""哥哥，周期承诺须双方明确同意，每次仍用真实日程和相处回执兑现。"""

import json
from datetime import timedelta
from uuid import uuid4

from world_engine.appointment_meals import AppointmentMealService
from world_engine.repository import from_iso, to_iso

APPOINTMENT_SERIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS social_appointment_series (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 proposer_id TEXT NOT NULL REFERENCES characters(id), recipient_id TEXT NOT NULL REFERENCES characters(id),
 status TEXT NOT NULL CHECK(status IN ('pending','active','paused','resume_pending','ended','completed')),
 first_starts_at TEXT NOT NULL, interval_days INTEGER NOT NULL CHECK(interval_days BETWEEN 1 AND 90),
 occurrence_count INTEGER NOT NULL CHECK(occurrence_count BETWEEN 2 AND 24),
 next_index INTEGER NOT NULL DEFAULT 1, template_json TEXT NOT NULL,
 statement TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '', resume_requested_by TEXT REFERENCES characters(id),
 source_event_id TEXT NOT NULL REFERENCES world_events(id), created_at TEXT NOT NULL,
 CHECK(proposer_id<>recipient_id)
);
CREATE INDEX IF NOT EXISTS idx_appointment_series_live ON social_appointment_series(world_id,status);
CREATE TABLE IF NOT EXISTS appointment_series_occurrences (
 series_id TEXT NOT NULL REFERENCES social_appointment_series(id) ON DELETE CASCADE,
 occurrence_index INTEGER NOT NULL, starts_at TEXT NOT NULL,
 appointment_id TEXT UNIQUE REFERENCES social_appointments(id),
 status TEXT NOT NULL CHECK(status IN ('scheduled','skipped','blocked')),
 reason TEXT NOT NULL DEFAULT '', source_event_id TEXT NOT NULL REFERENCES world_events(id),
 PRIMARY KEY(series_id,occurrence_index)
);
"""


class AppointmentSeriesService:
    @staticmethod
    def validate_reschedule(c, date, start, end):
        linked = c.execute(
            "SELECT s.*,o.occurrence_index FROM social_appointment_series s JOIN appointment_series_occurrences o ON o.series_id=s.id WHERE o.appointment_id=? AND s.status='active'",
            (date["id"],),
        ).fetchone()
        if linked and linked["occurrence_index"] + 1 < linked["occurrence_count"]:
            next_start = from_iso(linked["first_starts_at"]) + timedelta(
                days=linked["interval_days"] * (linked["occurrence_index"] + 1)
            )
            if end > next_start:
                raise ValueError("这次改期会跨过下一次固定相处；请先结束旧周期，再共同商量新的节奏")

    @staticmethod
    def rows(c, wid, cid, other=None):
        args = [wid, cid, cid]
        pair = ""
        if other:
            pair = " AND (s.proposer_id=? OR s.recipient_id=?)"
            args += [other, other]
        result = []
        for row in c.execute(
            "SELECT s.* FROM social_appointment_series s WHERE s.world_id=? AND (s.proposer_id=? OR s.recipient_id=?)"
            + pair
            + " ORDER BY CASE WHEN s.status IN ('pending','active','paused','resume_pending') THEN 0 ELSE 1 END,s.created_at DESC LIMIT 12",
            args,
        ):
            value = dict(row)
            value.pop("template_json")
            template = json.loads(row["template_json"])
            value["meal_supply"] = template.get("meal_supply") or "each_own"
            value["meal_provider_id"] = row["proposer_id"] if template.get("meal_supply") == "proposer" else None
            value["other_id"] = (
                row["recipient_id"] if row["proposer_id"] == cid else row["proposer_id"]
            )
            occurrences = [
                dict(r)
                for r in c.execute(
                    "SELECT o.occurrence_index,o.starts_at,o.appointment_id,o.status scheduling_status,o.reason,a.status appointment_status "
                    "FROM appointment_series_occurrences o LEFT JOIN social_appointments a ON a.id=o.appointment_id WHERE o.series_id=? ORDER BY o.occurrence_index",
                    (row["id"],),
                )
            ]
            value["occurrences"] = occurrences
            value["completed_count"] = sum(
                r["appointment_status"] == "completed" for r in occurrences
            )
            value["missed_count"] = sum(r["appointment_status"] == "missed" for r in occurrences)
            result.append(value)
        from world_engine.intimacy import IntimacyService

        IntimacyService.mask_records(c, wid, cid, result, ("statement", "reason"))
        return result

    @staticmethod
    def _people(c, row):
        return [
            c.execute("SELECT * FROM characters WHERE id=?", (row[key],)).fetchone()
            for key in ("proposer_id", "recipient_id")
        ]

    @classmethod
    def _record(cls, c, row, at, state, summary, *, actor=None, payload=None):
        from world_engine.appointments import AppointmentService

        first, second = cls._people(c, row)
        if actor and first["id"] != actor["id"]:
            first, second = second, first
        return AppointmentService._record(
            c,
            first,
            second,
            at,
            "series_" + state,
            summary,
            {"series_id": row["id"], **(payload or {})},
        )

    @staticmethod
    def _template(choice):
        return {
            key: getattr(choice, key)
            for key in (
                "kind",
                "location_id",
                "room_id",
                "meeting_world_time",
                "window_minutes",
                "minutes",
                "statement",
                "meal_supply",
            )
        }

    @staticmethod
    def _choice(row, index):
        from world_engine.appointments import AppointmentIntent

        data = json.loads(row["template_json"])
        data["meeting_world_time"] = to_iso(
            from_iso(row["first_starts_at"]) + timedelta(days=row["interval_days"] * index)
        )
        return AppointmentIntent(operation="propose", **data)

    @classmethod
    def _validate_windows(cls, c, actor, other, at, row):
        from world_engine.appointments import AppointmentService

        for index in range(row["occurrence_count"]):
            choice = cls._choice(row, index)
            # 哥哥，周期约定不延长单次访问许可；每次都须已有相应房间出入权。
            AppointmentService._validate(c, actor, other, at, choice)

    @classmethod
    def propose(cls, c, actor, other, at, choice, appointment_id):
        if choice.repeat_count == 1:
            return None
        row = {
            "id": str(uuid4()),
            "world_id": actor["world_id"],
            "proposer_id": actor["id"],
            "recipient_id": other["id"],
            "first_starts_at": to_iso(from_iso(choice.meeting_world_time)),
            "interval_days": choice.repeat_every_days,
            "occurrence_count": choice.repeat_count,
            "template_json": json.dumps(cls._template(choice), ensure_ascii=False),
        }
        if c.execute(
            "SELECT 1 FROM social_appointment_series WHERE world_id=? AND status IN ('pending','active','paused','resume_pending') AND "
            "((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
            (actor["world_id"], actor["id"], other["id"], other["id"], actor["id"]),
        ).fetchone():
            raise ValueError("双方已有未结束的周期安排，请先商量暂停或结束旧安排")
        try:
            cls._validate_windows(c, actor, other, at, row)
        except ValueError as exc:
            raise ValueError(
                "周期安排的时间或房间条件尚不成立；临时室内接待请单次商量：" + str(exc)
            ) from exc
        event = cls._record(
            c,
            row,
            at,
            "proposed",
            f"提出每{choice.repeat_every_days}个世界日一次、共{choice.repeat_count}次的相处安排，等待对方分别决定是否接受整份周期承诺。",
            payload={
                "first_appointment_id": appointment_id,
                "interval_days": choice.repeat_every_days,
                "occurrence_count": choice.repeat_count,
            },
        )
        c.execute(
            "INSERT INTO social_appointment_series(id,world_id,proposer_id,recipient_id,status,first_starts_at,interval_days,occurrence_count,template_json,statement,source_event_id,created_at) VALUES (?,?,?,?,'pending',?,?,?,?,?,?,?)",
            (
                row["id"],
                row["world_id"],
                row["proposer_id"],
                row["recipient_id"],
                row["first_starts_at"],
                row["interval_days"],
                row["occurrence_count"],
                row["template_json"],
                choice.statement,
                event,
                to_iso(at),
            ),
        )
        c.execute(
            "INSERT INTO appointment_series_occurrences VALUES (?,0,?,?,'scheduled','',?)",
            (row["id"], row["first_starts_at"], appointment_id, event),
        )
        return row["id"]

    @classmethod
    def after_choice(cls, c, actor, other, at, choice, date):
        linked = c.execute(
            "SELECT s.*,o.occurrence_index FROM social_appointment_series s JOIN appointment_series_occurrences o ON o.series_id=s.id WHERE o.appointment_id=?",
            (date["id"],),
        ).fetchone()
        if choice.accept_recurrence and (not linked or linked["status"] != "pending"):
            raise ValueError("整份周期确认必须回应双方真实的待确认周期邀请")
        if choice.operation == "accept" and date["replaces_id"]:
            parent = c.execute(
                "SELECT * FROM appointment_series_occurrences WHERE appointment_id=?",
                (date["replaces_id"],),
            ).fetchone()
            if parent:
                c.execute(
                    "UPDATE appointment_series_occurrences SET appointment_id=?,starts_at=? WHERE series_id=? AND occurrence_index=?",
                    (
                        date["id"],
                        date["starts_at"],
                        parent["series_id"],
                        parent["occurrence_index"],
                    ),
                )
        if not linked or linked["status"] != "pending" or linked["occurrence_index"] != 0:
            return None
        if choice.operation == "accept":
            if choice.accept_recurrence:
                # 首次已经预留日程，复核未来窗口时只排除这一份自己的日程。
                for index in range(1, linked["occurrence_count"]):
                    from world_engine.appointments import AppointmentService

                    AppointmentService._validate(c, actor, other, at, cls._choice(linked, index))
                status = "active"
                summary = "双方明确确认了整份周期相处安排；每次到场与完成仍分别结算。"
            else:
                status = "ended"
                summary = "本次相处已接受，后续周期尚未得到双方同意，不会继续排程。"
        elif choice.operation in {"decline", "cancel"}:
            status = "ended"
            summary = "首次周期邀请已被拒绝或撤回，后续不再排程。"
        else:
            return None
        event = cls._record(
            c,
            linked,
            at,
            status,
            summary,
            actor=actor,
            payload={"accept_recurrence": choice.accept_recurrence},
        )
        c.execute(
            "UPDATE social_appointment_series SET status=?,reason=?,source_event_id=? WHERE id=?",
            (status, summary, event, linked["id"]),
        )
        return {"series_id": linked["id"], "summary": summary}

    @classmethod
    def apply(cls, c, actor, other, at, choice):
        from world_engine.intimacy import IntimacyService

        row = c.execute(
            "SELECT * FROM social_appointment_series WHERE id=? AND world_id=? AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
            (
                choice.series_id,
                actor["world_id"],
                actor["id"],
                other["id"],
                other["id"],
                actor["id"],
            ),
        ).fetchone()
        if not row:
            raise ValueError("这份周期安排不属于双方")
        if row["status"] in {"ended", "completed"}:
            raise ValueError("周期安排已经结束，新的安排须重新商量")
        if choice.operation == "pause_series":
            if row["status"] not in {"active", "resume_pending"}:
                raise ValueError("只有生效或等待恢复的周期安排能暂停")
            state = "paused"
            summary = "一方暂停了后续周期安排；正在进行的这一次相处可单独选择结束。"
            cls._cancel_upcoming(c, row, at, summary, suspend=True)
        elif choice.operation == "end_series":
            state = "ended"
            summary = "一方结束了后续周期安排；已经发生的每次相处与未履行记录保留。"
            cls._cancel_upcoming(c, row, at, summary)
        else:
            IntimacyService._reachable(c, actor, other)
            if IntimacyService.paused(c, actor["world_id"], actor["id"], other["id"]):
                raise ValueError("一方仍要求停止主动联系，暂不恢复周期相处")
            if row["status"] == "paused":
                state = "resume_pending"
                summary = "一方提出恢复后续周期安排，等待另一方明确确认。"
            elif row["status"] == "resume_pending" and row["resume_requested_by"] != actor["id"]:
                state = "active"
                summary = "双方同意恢复未来尚未经过的周期时段，不补写过去的相处。"
            else:
                raise ValueError("恢复周期安排需要另一方独立回应，不能接受自己的恢复提议")
        summary += choice.statement
        event = cls._record(c, row, at, state, summary, actor=actor)
        c.execute(
            "UPDATE social_appointment_series SET status=?,reason=?,resume_requested_by=?,source_event_id=? WHERE id=?",
            (state, summary, actor["id"] if state == "resume_pending" else None, event, row["id"]),
        )
        if state == "active":
            cls.maintain(c, actor["world_id"], at)
            latest = c.execute(
                "SELECT status,reason FROM social_appointment_series WHERE id=?", (row["id"],)
            ).fetchone()
            if latest["status"] != "active":
                state = latest["status"]
                summary += " " + latest["reason"]
        return {"status": state, "series_id": row["id"], "event_id": event, "summary": summary}

    @classmethod
    def _cancel_upcoming(cls, c, row, at, reason, *, suspend=False):
        from world_engine.appointments import AppointmentService

        first, second = cls._people(c, row)
        for date in c.execute(
            "SELECT a.* FROM social_appointments a WHERE (a.id IN (SELECT appointment_id FROM appointment_series_occurrences WHERE series_id=?) OR a.replaces_id IN (SELECT appointment_id FROM appointment_series_occurrences WHERE series_id=?)) AND a.status IN ('pending','accepted')",
            (row["id"], row["id"]),
        ).fetchall():
            AppointmentService._close_contract(c, date, "cancelled")
            event = AppointmentService._record(
                c,
                first,
                second,
                at,
                "cancelled",
                reason,
                {"appointment_id": date["id"], "series_id": row["id"]},
            )
            c.execute(
                "UPDATE social_appointments SET status='cancelled',reason=?,source_event_id=? WHERE id=?",
                (reason, event, date["id"]),
            )
            if suspend:
                c.execute(
                    "UPDATE appointment_series_occurrences SET status='blocked',reason=?,source_event_id=? WHERE series_id=? AND appointment_id=?",
                    (reason, event, row["id"], date["id"]),
                )

    @classmethod
    def maintain(cls, c, wid, at):
        from world_engine.appointments import AppointmentService
        from world_engine.intimacy import IntimacyService

        updates = 0
        for row in c.execute(
            "SELECT * FROM social_appointment_series WHERE world_id=? AND status IN ('pending','active')",
            (wid,),
        ).fetchall():
            first, second = cls._people(c, row)
            if row["status"] == "pending":
                date = c.execute(
                    "SELECT a.status FROM social_appointments a JOIN appointment_series_occurrences o ON o.appointment_id=a.id WHERE o.series_id=? AND o.occurrence_index=0",
                    (row["id"],),
                ).fetchone()
                if date and date[0] in {"expired", "declined", "cancelled"}:
                    event = cls._record(
                        c, row, at, "ended", "周期邀请未得到双方确认，后续不再排程。"
                    )
                    c.execute(
                        "UPDATE social_appointment_series SET status='ended',reason='周期邀请未确认',source_event_id=? WHERE id=?",
                        (event, row["id"]),
                    )
                    updates += 1
                continue
            if IntimacyService.paused(c, wid, first["id"], second["id"]):
                cls._pause_blocked(c, row, at, "一方表达了暂停主动联系的界限。")
                updates += 1
                continue
            if c.execute(
                "SELECT 1 FROM appointment_series_occurrences o JOIN social_appointments a ON a.id=o.appointment_id WHERE o.series_id=? AND a.status IN ('pending','accepted','active')",
                (row["id"],),
            ).fetchone():
                continue
            suspended = c.execute(
                "SELECT * FROM appointment_series_occurrences WHERE series_id=? AND status='blocked' ORDER BY occurrence_index LIMIT 1",
                (row["id"],),
            ).fetchone()
            restoring = suspended is not None
            index = suspended["occurrence_index"] if restoring else row["next_index"]
            while index < row["occurrence_count"]:
                choice = cls._choice(row, index)
                if restoring:
                    previous = c.execute(
                        "SELECT * FROM social_appointments WHERE id=?",
                        (suspended["appointment_id"],),
                    ).fetchone()
                    choice = choice.model_copy(
                        update={
                            "meeting_world_time": previous["starts_at"],
                            "kind": previous["kind"],
                            "location_id": previous["location_id"],
                            "room_id": previous["room_id"],
                            "minutes": previous["minutes"],
                            "window_minutes": int(
                                (
                                    from_iso(previous["ends_at"]) - from_iso(previous["starts_at"])
                                ).total_seconds()
                                / 60
                            ),
                        }
                    )
                start = from_iso(choice.meeting_world_time)
                if start <= at:
                    reason = "该次开始时间已经经过，未及时排入日程；不补造到场、相处或失约。"
                    event = cls._record(
                        c,
                        row,
                        at,
                        "skipped",
                        reason,
                        payload={"occurrence_index": index, "starts_at": to_iso(start)},
                    )
                    if restoring:
                        c.execute(
                            "UPDATE appointment_series_occurrences SET status='scheduled',reason=?,source_event_id=? WHERE series_id=? AND occurrence_index=?",
                            (reason, event, row["id"], index),
                        )
                        restoring = False
                        index = row["next_index"]
                        updates += 1
                        continue
                    c.execute(
                        "INSERT INTO appointment_series_occurrences VALUES (?,?,?,NULL,'skipped',?,?)",
                        (row["id"], index, to_iso(start), reason, event),
                    )
                    index += 1
                    updates += 1
                    c.execute(
                        "UPDATE social_appointment_series SET next_index=? WHERE id=?",
                        (index, row["id"]),
                    )
                    continue
                c.execute("SAVEPOINT series_schedule")
                try:
                    AppointmentService._validate(c, first, second, at, choice)
                    aid = str(uuid4())
                    summary = f"双方原先确认的周期安排第{index + 1}/{row['occurrence_count']}次已排入日程，仍需实际到场和完成。"
                    event = cls._record(
                        c,
                        row,
                        at,
                        "scheduled",
                        summary,
                        payload={
                            "appointment_id": aid,
                            "occurrence_index": index,
                            "starts_at": to_iso(start),
                            "restored_from_appointment_id": suspended["appointment_id"]
                            if restoring
                            else None,
                        },
                    )
                    c.execute(
                        "INSERT INTO social_appointments(id,world_id,proposer_id,recipient_id,kind,status,location_id,starts_at,ends_at,minutes,statement,response,proposed_at,room_id,source_event_id,replaces_id) VALUES (?,?,?,?,?,'accepted',?,?,?,?,?,?,?,?,?,?)",
                        (
                            aid,
                            wid,
                            first["id"],
                            second["id"],
                            choice.kind,
                            choice.location_id,
                            to_iso(start),
                            to_iso(start + timedelta(minutes=choice.window_minutes)),
                            choice.minutes,
                            f"周期安排第{index + 1}次，恢复先前双方确认的单次安排，具体供餐方以本次记录为准。"
                            if restoring else f"周期安排第{index + 1}次，沿用原约定：{choice.statement}"[:240],
                            "依据双方已明确确认的周期承诺排程。",
                            to_iso(at),
                            choice.room_id,
                            event,
                            suspended["appointment_id"] if restoring else None,
                        ),
                    )
                    provider_id = AppointmentMealService.terms(c, previous["id"])["meal_provider_id"] if restoring else AppointmentMealService.provider(c, first, choice)
                    AppointmentMealService.save(c, aid, provider_id, event)
                    date = c.execute(
                        "SELECT * FROM social_appointments WHERE id=?", (aid,)
                    ).fetchone()
                    AppointmentService._schedule(c, date, first, second, at)
                    if restoring:
                        c.execute(
                            "UPDATE appointment_series_occurrences SET appointment_id=?,status='scheduled',reason='',source_event_id=? WHERE series_id=? AND occurrence_index=?",
                            (aid, event, row["id"], index),
                        )
                    else:
                        c.execute(
                            "INSERT INTO appointment_series_occurrences VALUES (?,?,?,?,'scheduled','',?)",
                            (row["id"], index, to_iso(start), aid, event),
                        )
                        c.execute(
                            "UPDATE social_appointment_series SET next_index=? WHERE id=?",
                            (index + 1, row["id"]),
                        )
                except ValueError as exc:
                    c.execute("ROLLBACK TO series_schedule")
                    cls._pause_blocked(c, row, at, str(exc))
                finally:
                    c.execute("RELEASE series_schedule")
                updates += 1
                break
            else:
                event = cls._record(
                    c,
                    row,
                    at,
                    "completed",
                    "本轮周期时段均已结算；每次完成、取消、失约和跳过的实际结果分别保留。",
                )
                c.execute(
                    "UPDATE social_appointment_series SET status='completed',reason='本轮周期时段已结束',source_event_id=? WHERE id=?",
                    (event, row["id"]),
                )
                updates += 1
        return updates

    @classmethod
    def _pause_blocked(cls, c, row, at, reason):
        summary = "后续周期安排暂缓，需双方重新商量恢复：" + reason
        cls._cancel_upcoming(c, row, at, summary, suspend=True)
        event = cls._record(c, row, at, "paused", summary)
        c.execute(
            "UPDATE social_appointment_series SET status='paused',reason=?,resume_requested_by=NULL,source_event_id=? WHERE id=?",
            (summary, event, row["id"]),
        )
