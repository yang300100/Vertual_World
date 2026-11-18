"""哥哥，未来的相处由双方约定，用实际到场和共同生活回执兑现。"""

import json
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.actions import ActionService
from world_engine.appointment_meals import AppointmentMealService
from world_engine.appointment_rooms import AppointmentRoomService
from world_engine.geo import great_circle_distance_km
from world_engine.repository import from_iso, to_iso, utc_now
from world_engine.schedules import ScheduleService
from world_engine.shared_life import MOMENT_LABELS, SharedLifeIntent, SharedLifeService

APPOINTMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS social_appointments (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 proposer_id TEXT NOT NULL REFERENCES characters(id), recipient_id TEXT NOT NULL REFERENCES characters(id),
 kind TEXT NOT NULL CHECK(kind IN ('talk','meal','rest','care')),
 status TEXT NOT NULL CHECK(status IN ('pending','accepted','active','completed','missed','interrupted','declined','cancelled','expired','superseded')),
 location_id TEXT NOT NULL REFERENCES locations(id), starts_at TEXT NOT NULL, ends_at TEXT NOT NULL,
 room_id TEXT REFERENCES life_rooms(id), visit_host_id TEXT REFERENCES characters(id),
 minutes INTEGER NOT NULL CHECK(minutes BETWEEN 5 AND 120), statement TEXT NOT NULL,
 response TEXT NOT NULL DEFAULT '', proposed_at TEXT NOT NULL,
 replaces_id TEXT REFERENCES social_appointments(id), repairs_id TEXT REFERENCES social_appointments(id),
 contract_id TEXT UNIQUE REFERENCES contract_fulfillments(request_id),
 moment_id TEXT UNIQUE REFERENCES shared_life_moments(id),
 source_event_id TEXT NOT NULL REFERENCES world_events(id), reason TEXT NOT NULL DEFAULT '',
 repaired_by TEXT REFERENCES social_appointments(id), CHECK(proposer_id<>recipient_id)
);
CREATE INDEX IF NOT EXISTS idx_social_appointments_due ON social_appointments(world_id,status,ends_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_social_appointments_pending ON social_appointments(
 world_id,MIN(proposer_id,recipient_id),MAX(proposer_id,recipient_id)) WHERE status='pending';
CREATE TABLE IF NOT EXISTS appointment_attendance (
 appointment_id TEXT NOT NULL REFERENCES social_appointments(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id), arrived_at TEXT NOT NULL,
 PRIMARY KEY(appointment_id,character_id)
);
"""


class AppointmentIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: Literal[
        "propose",
        "accept",
        "decline",
        "cancel",
        "reschedule",
        "attend",
        "pause_series",
        "resume_series",
        "end_series",
    ]
    appointment_id: str | None = Field(default=None, min_length=1, max_length=100)
    kind: Literal["talk", "meal", "rest", "care"] | None = None
    location_id: str | None = Field(default=None, min_length=1, max_length=100)
    room_id: str | None = Field(default=None, min_length=1, max_length=100)
    journey_id: str | None = Field(default=None, min_length=1, max_length=100)
    allow_visit: bool = Field(default=False, strict=True)
    meal_supply: Literal["each_own", "proposer"] | None = None
    meeting_world_time: str | None = Field(default=None, max_length=80)
    window_minutes: int = Field(default=60, ge=5, le=240, strict=True)
    minutes: int = Field(default=15, ge=5, le=120, strict=True)
    repairs_id: str | None = Field(default=None, min_length=1, max_length=100)
    series_id: str | None = Field(default=None, min_length=1, max_length=100)
    repeat_every_days: int | None = Field(default=None, ge=1, le=90, strict=True)
    repeat_count: int = Field(default=1, ge=1, le=24, strict=True)
    accept_recurrence: bool = Field(default=False, strict=True)
    statement: str = Field(min_length=1, max_length=240)

    @model_validator(mode="before")
    @classmethod
    def optional_defaults(cls, value):
        if isinstance(value, dict):
            value = {
                k: v
                for k, v in value.items()
                if v is not None
                or k
                not in {
                    "minutes",
                    "window_minutes",
                    "allow_visit",
                    "repeat_count",
                    "accept_recurrence",
                }
            }
        return value

    @model_validator(mode="after")
    def validate_choice(self):
        if self.journey_id and (self.operation!="propose" or self.kind!="rest" or not self.room_id or self.repeat_count!=1 or self.repeat_every_days or self.repairs_id or self.appointment_id):
            raise ValueError("旅行室内短休只关联新的单次房间休息邀请；回应沿用约会编号，不重填旅行来源")
        if self.meal_supply is not None and (self.operation not in {"propose", "reschedule"} or self.kind != "meal"):
            raise ValueError("供餐选择只用于新的用餐邀请或重新商量；回应已有安排不能修改谁提供食物")
        if self.operation in {"pause_series", "resume_series", "end_series"}:
            if (
                not self.series_id
                or self.appointment_id
                or self.allow_visit
                or self.accept_recurrence
                or self.repeat_count != 1
                or self.repeat_every_days
            ):
                raise ValueError("周期暂停、恢复和结束须单独引用真实周期编号")
            return self
        if self.series_id:
            raise ValueError("单次安排通过约会编号处理，不能附加周期编号")
        if self.repeat_count > 1 or self.repeat_every_days is not None:
            if (
                self.operation != "propose"
                or self.repeat_count < 2
                or self.repeat_every_days is None
                or self.repairs_id
            ):
                raise ValueError("周期邀请需要新提议、明确间隔与2至24次的总次数；补约仍单次商量")
        if self.accept_recurrence and self.operation != "accept":
            raise ValueError("只有回应对方的周期邀请时才能明确接受后续安排")
        if self.operation != "propose" and not self.appointment_id:
            raise ValueError("请引用双方真实的约会安排")
        if self.operation == "propose" and self.appointment_id:
            raise ValueError("新邀请不能冒用已有安排")
        if self.allow_visit and self.operation not in {"propose", "accept", "reschedule"}:
            raise ValueError("只有提出、改期或接受安排时才能表达本次接待许可")
        if self.operation in {"propose", "reschedule"} and (
            not self.kind
            or not self.location_id
            or not self.meeting_world_time
            or self.minutes > self.window_minutes
        ):
            raise ValueError("邀请需要地点、带时区的未来时间，以及时间窗内可完成的相处时长")
        return self


class AppointmentService:
    @staticmethod
    def rows(c, wid, cid, at, other=None):
        args = [wid, cid, cid]
        pair = ""
        if other:
            pair = " AND (a.proposer_id=? OR a.recipient_id=?)"
            args += [other, other]
        rows = c.execute(
            "SELECT a.*,l.name location_name,r.name room_name,o.series_id,o.occurrence_index FROM social_appointments a JOIN locations l ON l.id=a.location_id "
            "LEFT JOIN life_rooms r ON r.id=a.room_id "
            "LEFT JOIN appointment_series_occurrences o ON o.appointment_id=a.id "
            "WHERE a.world_id=? AND (a.proposer_id=? OR a.recipient_id=?)"
            + pair
            + " ORDER BY CASE WHEN a.status IN ('pending','accepted','active') THEN 0 ELSE 1 END,a.starts_at DESC,a.rowid DESC LIMIT 24",
            args,
        ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["other_id"] = (
                row["recipient_id"] if row["proposer_id"] == cid else row["proposer_id"]
            )
            value["label"] = MOMENT_LABELS[row["kind"]]
            value.update(AppointmentMealService.terms(c, row["id"]))
            from world_engine.journey_room_appointments import JourneyRoomService
            journey_link=JourneyRoomService.link(c,row["id"])
            value["journey_id"]=journey_link["journey_id"] if journey_link else None
            value["journey_stop_index"]=journey_link["stop_index"] if journey_link else None
            if row["status"] == "pending" and from_iso(row["starts_at"]) <= at:
                value["status"] = "expired"
            # 哥哥，远方人物的位置不能借约会界面泄漏，只显示自己的到场记录。
            own = c.execute(
                "SELECT arrived_at FROM appointment_attendance WHERE appointment_id=? AND character_id=?",
                (row["id"], cid),
            ).fetchone()
            value["own_arrived_at"] = own[0] if own else None
            result.append(value)
        from world_engine.intimacy import IntimacyService

        IntimacyService.mask_records(c, wid, cid, result, ("statement", "response", "reason"))
        return result

    @classmethod
    def context(cls, c, wid, cid, other, at):
        actor = c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
        options = []
        # 只提供眼前地点和本人实际到访过的地点，避免凭空知道陌生地点。
        for place in c.execute(
            "SELECT id,name,longitude,latitude FROM locations WHERE world_id=? AND is_active=1 AND "
            "(id=? OR id IN (SELECT DISTINCT e.location_id FROM world_events e JOIN event_observers o ON o.event_id=e.id "
            "WHERE e.world_id=? AND o.character_id=? AND o.channel='participant')) LIMIT 12",
            (wid, actor["current_location_id"] or actor["location_id"], wid, cid),
        ):
            options.append({"id": place["id"], "name": place["name"]})
        rows = cls.rows(c, wid, cid, at, other)
        keys = (
            "id",
            "proposer_id",
            "recipient_id",
            "kind",
            "status",
            "location_id",
            "location_name",
            "room_id",
            "room_name",
            "visit_host_id",
            "starts_at",
            "ends_at",
            "minutes",
            "statement",
            "response",
            "reason",
            "repairs_id",
            "replaces_id",
            "repaired_by",
            "source_event_id",
            "series_id",
            "occurrence_index",
            "meal_supply",
            "meal_provider_id",
            "journey_id",
            "journey_stop_index",
        )
        from world_engine.appointment_series import AppointmentSeriesService

        arrangements=[{k: row[k] for k in keys} for row in rows[:8]]
        for arrangement in arrangements:
            if arrangement["room_id"] and arrangement["status"] in {"pending","accepted","active"}:
                try:
                    room=AppointmentRoomService.path(c,wid,arrangement["room_id"])[0]
                    controller=AppointmentRoomService.controller(c,room,from_iso(arrangement["starts_at"]),from_iso(arrangement["ends_at"]))
                    arrangement["room_permissions"]={"expressed_host_controls_room":bool(arrangement["visit_host_id"] and controller==arrangement["visit_host_id"]),"access_requires_time_window_and_host_presence":True}
                except ValueError:
                    arrangement["room_permissions"]={"expressed_host_controls_room":False}
        return {
            "arrangements": arrangements,
            "place_options": options,
            "room_options": AppointmentRoomService.room_options(c, wid, cid, other, at),
            "series": [
                {**s, "occurrences": s["occurrences"][-3:]}
                for s in AppointmentSeriesService.rows(c, wid, cid, other)[:6]
            ],
        }

    @staticmethod
    def _record(c, first, second, at, state, summary, payload):
        event = ActionService._record_event(
            c,
            world_id=first["world_id"],
            tick_id=str(uuid4()),
            occurred_at=at,
            event_type="social.appointment_" + state,
            actor_id=first["id"],
            target_id=second["id"],
            location_id=None,
            summary=summary,
            payload=payload,
        )
        for person in (first, second):
            ActionService._record_memory(
                c,
                world_id=first["world_id"],
                character_id=person["id"],
                event_id=event,
                memory_type="experienced",
                summary=summary,
                importance=7 if state in {"completed", "missed", "repaired"} else 5,
            )
        player = first if first["is_player"] else second
        c.execute(
            "INSERT OR IGNORE INTO player_notifications(id,world_id,recipient_id,event_id,title,created_at) VALUES (?,?,?,?,?,?)",
            (str(uuid4()), first["world_id"], player["id"], event, summary[:180], to_iso(at)),
        )
        return event

    @staticmethod
    def _terms(choice):
        return {
            "meeting_world_time": choice.meeting_world_time,
            "meeting_minutes": choice.window_minutes,
            "location_id": choice.location_id,
            "room_id": choice.room_id,
            "payment": 0,
        }

    @classmethod
    def _validate(
        cls,
        c,
        actor,
        other,
        at,
        choice,
        replaces=None,
        *,
        visit_host_id=None,
        awaiting_host_id=None,
    ):
        start, end, _, _ = ScheduleService.validate(
            c,
            actor["world_id"],
            actor["id"],
            other["id"],
            cls._terms(choice),
            at,
            exclude_contract_id=replaces["contract_id"] if replaces else None,
        )
        place = c.execute("SELECT * FROM locations WHERE id=?", (choice.location_id,)).fetchone()
        AppointmentRoomService.validate(
            c,
            actor["world_id"],
            choice.room_id,
            choice.location_id,
            actor["id"],
            other["id"],
            start,
            end,
            visit_host_id=visit_host_id,
            awaiting_host_id=awaiting_host_id,
        )
        for person in (actor, other):
            if (
                ScheduleService.earliest_arrival(c, person, place, at, room_id=choice.room_id)
                + timedelta(minutes=choice.minutes)
                > end
            ):
                raise ValueError("路程与已有活动之后，已没有足够时间完成这次相处")
        return start, end

    @classmethod
    def apply(cls, c, actor, other, at, choice):
        # 哥哥，开始时的邀请、双方进食和约会状态必须一起成功，失败不能留下半顿饭。
        c.execute("SAVEPOINT appointment_apply")
        try:
            return cls._apply(c, actor, other, at, choice)
        except Exception:
            c.execute("ROLLBACK TO appointment_apply")
            raise
        finally:
            c.execute("RELEASE appointment_apply")

    @classmethod
    def _apply(cls, c, actor, other, at, choice):
        from world_engine.intimacy import IntimacyService

        if (
            other is None
            or actor["world_id"] != other["world_id"]
            or actor["id"] == other["id"]
            or bool(actor["is_player"]) == bool(other["is_player"])
        ):
            raise ValueError("请选择当前世界中与玩家相处的另一位人物")
        wid = actor["world_id"]
        cls.maintain(c, wid, at)
        from world_engine.appointment_series import AppointmentSeriesService

        if choice.operation in {"pause_series", "resume_series", "end_series"}:
            return AppointmentSeriesService.apply(c, actor, other, at, choice)
        current = None
        if choice.appointment_id:
            current = c.execute(
                "SELECT * FROM social_appointments WHERE id=? AND world_id=? AND "
                "((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
                (choice.appointment_id, wid, actor["id"], other["id"], other["id"], actor["id"]),
            ).fetchone()
            if current is None:
                raise ValueError("这份约会不属于双方")
        if choice.operation in {"propose", "reschedule"}:
            IntimacyService._reachable(c, actor, other)
            if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                raise ValueError("一方要求留出空间，暂不提出约会")
            if current and current["status"] != "accepted":
                raise ValueError("只能为尚未开始的已确认约会商量改期；失约后可以另提补约")
            if c.execute(
                "SELECT 1 FROM social_appointments WHERE world_id=? AND status='pending' AND "
                "((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
                (wid, actor["id"], other["id"], other["id"], actor["id"]),
            ).fetchone():
                raise ValueError("双方已有等待回应的约会邀请，请先处理")
            repair = None
            if choice.repairs_id:
                repair = c.execute(
                    "SELECT * FROM social_appointments WHERE id=? AND world_id=? AND status IN ('missed','interrupted') AND "
                    "((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
                    (choice.repairs_id, wid, actor["id"], other["id"], other["id"], actor["id"]),
                ).fetchone()
                if not repair or repair["repaired_by"]:
                    raise ValueError("补约须引用双方尚未补上的真实失约或中断记录")
            start, end = ScheduleService.parse_window(cls._terms(choice), at)
            host = None
            if choice.room_id:
                target = AppointmentRoomService.path(c, wid, choice.room_id)[0]
                host = AppointmentRoomService.controller(c, target, start, end)
            if choice.allow_visit and (not choice.room_id or host != actor["id"]):
                raise ValueError("只有对约会房间有接待权的人能明确允许本次访问")
            visit_host = actor["id"] if choice.allow_visit else None
            start, end = cls._validate(
                c,
                actor,
                other,
                at,
                choice,
                current,
                visit_host_id=visit_host,
                awaiting_host_id=other["id"] if host == other["id"] else None,
            )
            if current:
                AppointmentSeriesService.validate_reschedule(c, current, start, end)
            from world_engine.journey_room_appointments import JourneyRoomService
            journey_link=JourneyRoomService.prepare(c,actor,other,at,choice,start,end,current)
            provider_id = AppointmentMealService.provider(c, actor, choice, current)
            meal_note = (" 本次由提议者提供双方各一份自有食物，到场后重新核验。" if provider_id == actor["id"] else
                         " 本次沿用原来已承诺的供餐方，改期不会转移请客义务。" if provider_id else
                         " 本次双方各自准备一份食物。") if choice.kind == "meal" else ""
            aid = str(uuid4())
            event = cls._record(
                c,
                actor,
                other,
                at,
                "proposed",
                "提出了未来的共同安排，等待另一方明确回应：" + choice.statement + meal_note,
                {
                    "appointment_id": aid,
                    "replaces_id": choice.appointment_id,
                    "repairs_id": choice.repairs_id,
                    "room_id": choice.room_id,
                    "visit_host_id": visit_host,
                    "allow_visit": choice.allow_visit,
                    "meal_provider_id": provider_id,
                    "journey_room": journey_link,
                },
            )
            c.execute(
                "INSERT INTO social_appointments(id,world_id,proposer_id,recipient_id,kind,status,location_id,starts_at,ends_at,minutes,statement,proposed_at,replaces_id,repairs_id,source_event_id,room_id,visit_host_id) "
                "VALUES (?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?)",
                (
                    aid,
                    wid,
                    actor["id"],
                    other["id"],
                    choice.kind,
                    choice.location_id,
                    to_iso(start),
                    to_iso(end),
                    choice.minutes,
                    choice.statement,
                    to_iso(at),
                    choice.appointment_id,
                    choice.repairs_id,
                    event,
                    choice.room_id,
                    visit_host,
                ),
            )
            AppointmentMealService.save(c, aid, provider_id, event)
            JourneyRoomService.save(c,aid,journey_link)
            series_id = AppointmentSeriesService.propose(c, actor, other, at, choice, aid)
            return {
                "status": "pending",
                "appointment_id": aid,
                "event_id": event,
                "series_id": series_id,
                "summary": "改期邀请已记录，等待对方确认；原约定仍然有效。"
                if current
                else f"周期邀请已记录：每{choice.repeat_every_days}个世界日一次，共{choice.repeat_count}次，等待对方明确决定是否接受整份安排。"
                if series_id
                else "未来邀请已记录，等待对方确认，尚未开始相处。",
            }
        if choice.operation in {"accept", "decline"}:
            IntimacyService._reachable(c, actor, other)
            if current["status"] != "pending" or current["recipient_id"] != actor["id"]:
                raise ValueError("只有收到有效邀请的人能回应，不能接受自己的邀请")
            if choice.operation == "accept":
                if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                    raise ValueError("一方仍要求留出空间")
                replaced = (
                    c.execute(
                        "SELECT * FROM social_appointments WHERE id=?", (current["replaces_id"],)
                    ).fetchone()
                    if current["replaces_id"]
                    else None
                )
                if replaced and replaced["status"] != "accepted":
                    raise ValueError("原约定已变化，请重新商量")
                proposed = AppointmentIntent(
                    operation="propose",
                    kind=current["kind"],
                    location_id=current["location_id"],
                    room_id=current["room_id"],
                    meeting_world_time=current["starts_at"],
                    minutes=current["minutes"],
                    window_minutes=int(
                        (
                            from_iso(current["ends_at"]) - from_iso(current["starts_at"])
                        ).total_seconds()
                        / 60
                    ),
                    statement=current["statement"],
                )
                visit_host = current["visit_host_id"]
                if choice.allow_visit:
                    if not current["room_id"]:
                        raise ValueError("室外安排不需要房间访问许可")
                    room = AppointmentRoomService.path(c, wid, current["room_id"])[0]
                    if (
                        AppointmentRoomService.controller(
                            c, room, from_iso(current["starts_at"]), from_iso(current["ends_at"])
                        )
                        != actor["id"]
                    ):
                        raise ValueError("只有有接待权的人能允许本次访问")
                    visit_host = actor["id"]
                cls._validate(c, actor, other, at, proposed, replaced, visit_host_id=visit_host)
                if replaced:
                    AppointmentSeriesService.validate_reschedule(
                        c, replaced, from_iso(current["starts_at"]), from_iso(current["ends_at"])
                    )
                if replaced:
                    cls._close_contract(c, replaced, "cancelled")
                    c.execute(
                        "UPDATE social_appointments SET status='superseded',reason='双方确认了新的时间' WHERE id=?",
                        (replaced["id"],),
                    )
                cls._schedule(c, current, actor, other, at)
                c.execute(
                    "UPDATE social_appointments SET visit_host_id=? WHERE id=?",
                    (visit_host, current["id"]),
                )
            state = "accepted" if choice.operation == "accept" else "declined"
            summary = (
                "双方确认了未来安排，需各自实际到场后开始相处。"
                if state == "accepted"
                else "约会邀请已被婉拒。"
            ) + choice.statement
        elif choice.operation == "cancel":
            if (
                current["status"] not in {"pending", "accepted", "active"}
                or current["status"] == "pending"
                and current["proposer_id"] != actor["id"]
            ):
                raise ValueError("只能撤回自己的邀请，或退出双方已确认的安排")
            if current["status"] == "active":
                moment = c.execute(
                    "SELECT * FROM shared_life_moments WHERE id=?", (current["moment_id"],)
                ).fetchone()
                SharedLifeService.finish(c, moment, at, "interrupted", choice.statement)
                cls.maintain(c, wid, at)
                row = c.execute(
                    "SELECT * FROM social_appointments WHERE id=?", (current["id"],)
                ).fetchone()
                return {
                    "status": row["status"],
                    "appointment_id": row["id"],
                    "event_id": row["source_event_id"],
                    "summary": row["reason"],
                }
            cls._close_contract(c, current, "cancelled")
            state = "cancelled"
            summary = "一方取消了未来安排，原约定不再要求赴约：" + choice.statement
        else:
            return cls._attend(c, current, actor, other, at)
        event = cls._record(
            c,
            actor,
            other,
            at,
            state,
            summary,
            {
                "appointment_id": current["id"],
                "room_id": current["room_id"],
                "allow_visit": choice.allow_visit,
                "visit_host_id": visit_host
                if choice.operation == "accept"
                else current["visit_host_id"],
                **AppointmentMealService.terms(c, current["id"]),
            },
        )
        c.execute(
            "UPDATE social_appointments SET status=?,response=?,source_event_id=?,reason=? WHERE id=?",
            (state, choice.statement, event, summary, current["id"]),
        )
        date = c.execute(
            "SELECT * FROM social_appointments WHERE id=?", (current["id"],)
        ).fetchone()
        series_result = AppointmentSeriesService.after_choice(c, actor, other, at, choice, date)
        if series_result:
            summary += " " + series_result["summary"]
        return {
            "status": state,
            "appointment_id": current["id"],
            "event_id": event,
            "summary": summary,
            "series_id": series_result["series_id"] if series_result else None,
        }

    @staticmethod
    def _schedule(c, row, actor, other, at):
        from world_engine.contracts import ContractService

        player, npc = (actor, other) if actor["is_player"] else (other, actor)
        now = to_iso(utc_now())
        cid = str(uuid4())
        terms = {
            "meeting_world_time": row["starts_at"],
            "meeting_minutes": int(
                (from_iso(row["ends_at"]) - from_iso(row["starts_at"])).total_seconds() / 60
            ),
            "location_id": row["location_id"],
            "room_id": row["room_id"],
            "payment": 0,
        }
        c.execute(
            "INSERT INTO long_term_operation_requests(id,world_id,requester_id,recipient_id,operation_type,terms_json,status,source_event_id,created_at,updated_at) VALUES (?,?,?,?,?,?,'applied',?,?,?)",
            (
                cid,
                row["world_id"],
                player["id"],
                npc["id"],
                "约定",
                json.dumps(terms),
                row["source_event_id"],
                now,
                now,
            ),
        )
        # 日程兼容玩家/NPC的角色；真正的发起者始终保存在 social_appointments 中。
        request = c.execute(
            "SELECT * FROM long_term_operation_requests WHERE id=?", (cid,)
        ).fetchone()
        due = ContractService.start(c, request, terms, to_iso(at))
        c.execute(
            "INSERT INTO npc_todos(id,world_id,character_id,title,details,created_at,updated_at,contract_id,due_world_time) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                row["world_id"],
                npc["id"],
                MOMENT_LABELS[row["kind"]],
                row["statement"],
                now,
                now,
                cid,
                due,
            ),
        )
        c.execute("UPDATE social_appointments SET contract_id=? WHERE id=?", (cid, row["id"]))
        c.execute(
            "UPDATE npc_daily_states SET next_world_time=? WHERE character_id=?",
            (to_iso(at), npc["id"]),
        )

    @staticmethod
    def _close_contract(c, row, status):
        if row["contract_id"]:
            c.execute(
                "UPDATE contract_fulfillments SET status=? WHERE request_id=?",
                (status, row["contract_id"]),
            )
            c.execute(
                "UPDATE npc_todos SET status=? WHERE contract_id=?",
                ("done" if status == "completed" else "cancelled", row["contract_id"]),
            )
            c.execute(
                "UPDATE npc_outreach_jobs SET status='cancelled',error='原约会已结束或改期' WHERE status IN ('pending','processing','failed') AND source_event_id IN "
                "(SELECT id FROM world_events WHERE event_type='schedule.at_risk' AND json_extract(payload_json,'$.appointment_id')=?)",
                (row["contract_id"],),
            )

    @classmethod
    def _attend(cls, c, row, actor, other, at):
        if not actor["is_player"]:
            raise ValueError("NPC 依已确认日程赴约，开始相处仍需玩家本人选择")
        if (
            row["status"] != "accepted"
            or not from_iso(row["starts_at"]) <= at
            or at + timedelta(minutes=row["minutes"]) > from_iso(row["ends_at"])
        ):
            raise ValueError("尚未到约定时间、安排已结束，或剩余时间不足以完成相处")
        place = c.execute(
            "SELECT * FROM locations WHERE id=? AND is_active=1", (row["location_id"],)
        ).fetchone()
        if (
            place is None
            or not all(cls._at_place(c, p, place, row["room_id"]) for p in (actor, other))
            or not SharedLifeService._near(actor, other, row["kind"])
        ):
            raise ValueError(
                "请双方实际到达约定的室外场景或指定房间，走近到20米内；人物不会被自动移动"
            )
        room_risk = AppointmentRoomService.risk(c, row, at)
        if room_risk:
            raise ValueError(room_risk)
        provider_id = AppointmentMealService.terms(c, row["id"])["meal_provider_id"]
        inviter, responder = (actor, other) if provider_id == actor["id"] else (other, actor)
        # 未来邀请已取得双方对本次活动的同意，开始时仍核验身体、资源、界限与其它日程。
        invitation = SharedLifeService.apply(
            c,
            inviter,
            responder,
            at,
            SharedLifeIntent(
                operation="invite",
                kind=row["kind"],
                minutes=row["minutes"],
                statement=row["statement"],
                meal_supply="proposer" if provider_id else "each_own",
            ),
        )
        result = SharedLifeService.apply(
            c,
            responder,
            inviter,
            at,
            SharedLifeIntent(
                operation="accept",
                moment_id=invitation["moment_id"],
                statement="我已到场，愿意开始这次约定的相处。",
            ),
            exclude_contract_id=row["contract_id"],
        )
        for person in (actor, other):
            c.execute(
                "INSERT OR IGNORE INTO appointment_attendance VALUES (?,?,?)",
                (row["id"], person["id"], to_iso(at)),
            )
        event = cls._record(
            c,
            actor,
            other,
            at,
            "started",
            "双方已到场并开始相处，尚未完成。",
            {"appointment_id": row["id"], "moment_id": result["moment_id"], "meal_provider_id": provider_id},
        )
        c.execute(
            "UPDATE social_appointments SET status='active',moment_id=?,source_event_id=? WHERE id=?",
            (result["moment_id"], event, row["id"]),
        )
        return {
            "status": "active",
            "appointment_id": row["id"],
            "event_id": event,
            "summary": result["summary"],
        }

    @staticmethod
    def _at_place(c, person, place, room_id=None):
        return (
            person["health"] > 0
            and person["current_room_id"] == room_id
            and person["space_layer"] == place["space_layer"]
            and great_circle_distance_km(
                person["longitude"], person["latitude"], place["longitude"], place["latitude"]
            )
            <= 0.1
            and not c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (person["id"],),
            ).fetchone()
        )

    @classmethod
    def maintain(cls, c, wid, at):
        from world_engine.intimacy import IntimacyService
        from world_engine.society import SocietyService
        from world_engine.journey_room_appointments import JourneyRoomService
        JourneyRoomService.expire_pending(c,wid,at)

        updates = 0
        for row in c.execute(
            "SELECT * FROM social_appointments WHERE world_id=? AND status IN ('pending','accepted','active')",
            (wid,),
        ).fetchall():
            first = c.execute(
                "SELECT * FROM characters WHERE id=?", (row["proposer_id"],)
            ).fetchone()
            second = c.execute(
                "SELECT * FROM characters WHERE id=?", (row["recipient_id"],)
            ).fetchone()
            state = reason = None
            if row["status"] == "pending":
                if at >= from_iso(row["starts_at"]):
                    state, reason = "expired", "邀请未在开始前得到双方确认，不记为失约。"
            elif row["status"] == "active":
                moment = c.execute(
                    "SELECT * FROM shared_life_moments WHERE id=?", (row["moment_id"],)
                ).fetchone()
                if moment["status"] == "active" and (
                    risk := AppointmentRoomService.risk(c, row, at)
                ):
                    SharedLifeService.finish(
                        c, moment, at, "interrupted", "房间接待条件改变：" + risk
                    )
                    moment = c.execute(
                        "SELECT * FROM shared_life_moments WHERE id=?", (row["moment_id"],)
                    ).fetchone()
                if moment["status"] in {"completed", "interrupted"}:
                    state = moment["status"]
                    reason = (
                        "双方实际完成了约定的相处。"
                        if state == "completed"
                        else "实际共同活动提前中断：" + moment["reason"]
                    )
            else:
                place = c.execute(
                    "SELECT * FROM locations WHERE id=?", (row["location_id"],)
                ).fetchone()
                if from_iso(row["starts_at"]) <= at < from_iso(row["ends_at"]):
                    for person in (first, second):
                        if place["is_active"] and cls._at_place(c, person, place, row["room_id"]):
                            updates += c.execute(
                                "INSERT OR IGNORE INTO appointment_attendance VALUES (?,?,?)",
                                (row["id"], person["id"], to_iso(at)),
                            ).rowcount
                if at >= from_iso(row["ends_at"]):
                    state = "missed"
                    reason = "时间窗已结束，双方没有完成约定的共同活动。到场记录保留；未查明原因，不推定故意失约。"
            if not state:
                continue
            updates += 1
            cls._close_contract(
                c,
                row,
                "completed"
                if state == "completed"
                else "overdue"
                if state == "missed"
                else "cancelled",
            )
            event = cls._record(
                c,
                first,
                second,
                at,
                state,
                reason,
                {
                    "appointment_id": row["id"],
                    "moment_id": row["moment_id"],
                    "repairs_id": row["repairs_id"],
                },
            )
            c.execute(
                "UPDATE social_appointments SET status=?,reason=?,source_event_id=? WHERE id=?",
                (state, reason, event, row["id"]),
            )
            if state in {"completed", "missed", "interrupted"}:
                IntimacyService.remember_event(
                    c,
                    wid,
                    first["id"],
                    second["id"],
                    event,
                    at,
                    "shared" if state == "completed" else "conflict",
                    reason,
                )
            if state == "missed":
                # 只对独自到场的一方记一次落空；不把未知原因写成另一方的恶意。
                arrived = {
                    a[0]
                    for a in c.execute(
                        "SELECT character_id FROM appointment_attendance WHERE appointment_id=?",
                        (row["id"],),
                    )
                }
                if len(arrived) == 1:
                    observer = next(iter(arrived))
                    subject = second["id"] if observer == first["id"] else first["id"]
                    SocietyService._relationship(c, wid, observer, subject, -1, -2)
            if state == "completed" and row["repairs_id"]:
                changed = c.execute(
                    "UPDATE social_appointments SET repaired_by=? WHERE id=? AND status IN ('missed','interrupted') AND repaired_by IS NULL",
                    (row["id"], row["repairs_id"]),
                ).rowcount
                if changed:
                    repair_event = cls._record(
                        c,
                        first,
                        second,
                        at,
                        "repaired",
                        "一次双方确认的补约已实际完成；原先的失约、分歧与个人感受仍然保留。",
                        {
                            "appointment_id": row["id"],
                            "repairs_id": row["repairs_id"],
                            "completion_event_id": event,
                        },
                    )
                    IntimacyService.remember_event(
                        c,
                        wid,
                        first["id"],
                        second["id"],
                        repair_event,
                        at,
                        "repair",
                        "补约已用实际相处兑现，是否重新信任仍由各自决定。",
                    )
        from world_engine.appointment_series import AppointmentSeriesService

        updates += AppointmentSeriesService.maintain(c, wid, at)
        return updates
