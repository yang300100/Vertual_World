"""哥哥，共同筹备先独立同意分工，再以真实劳动、本人库存与到场核对兑现。"""

import json
from datetime import datetime, timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.appointment_rooms import AppointmentRoomService
from world_engine.food import FoodService
from world_engine.geo import great_circle_distance_km
from world_engine.life import LifeActivityService
from world_engine.proximity import same_room
from world_engine.repository import from_iso, to_iso

SHARED_PLAN_SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_preparation_plans (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 proposer_id TEXT NOT NULL REFERENCES characters(id), recipient_id TEXT NOT NULL REFERENCES characters(id),
 status TEXT NOT NULL CHECK(status IN ('pending','active','completed','declined','ended','expired')),
 title TEXT NOT NULL, statement TEXT NOT NULL, response TEXT NOT NULL DEFAULT '',
 location_id TEXT NOT NULL REFERENCES locations(id), room_id TEXT REFERENCES life_rooms(id),
 proposed_at TEXT NOT NULL, accepted_at TEXT, accepted_event_id TEXT REFERENCES world_events(id), deadline_at TEXT NOT NULL,
 source_event_id TEXT NOT NULL REFERENCES world_events(id), reason TEXT NOT NULL DEFAULT '',
 CHECK(proposer_id<>recipient_id)
);
CREATE INDEX IF NOT EXISTS idx_preparation_plan_due ON shared_preparation_plans(world_id,status,deadline_at);
CREATE INDEX IF NOT EXISTS idx_preparation_work_activity ON character_life_activities(world_id,character_id,kind,status,started_world_time);
CREATE UNIQUE INDEX IF NOT EXISTS idx_preparation_pair ON shared_preparation_plans(world_id,MIN(proposer_id,recipient_id),MAX(proposer_id,recipient_id)) WHERE status IN ('pending','active');
CREATE TABLE IF NOT EXISTS shared_preparation_roles (
 plan_id TEXT NOT NULL REFERENCES shared_preparation_plans(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id), kind TEXT NOT NULL CHECK(kind IN ('earn_money','prepare_food')),
 target INTEGER NOT NULL CHECK(target BETWEEN 1 AND 500), spending_limit INTEGER NOT NULL DEFAULT 0 CHECK(spending_limit BETWEEN 0 AND 100), spent INTEGER NOT NULL DEFAULT 0 CHECK(spent BETWEEN 0 AND spending_limit),
 progress INTEGER NOT NULL DEFAULT 0, ready_at TEXT, ready_event_id TEXT REFERENCES world_events(id),
 private_reason TEXT NOT NULL DEFAULT '', retry_at TEXT, PRIMARY KEY(plan_id,character_id), CHECK(kind<>'prepare_food' OR target<=12)
);
CREATE TABLE IF NOT EXISTS preparation_work_receipts (
 activity_id TEXT PRIMARY KEY REFERENCES character_life_activities(id),
 plan_id TEXT NOT NULL REFERENCES shared_preparation_plans(id), character_id TEXT NOT NULL REFERENCES characters(id),
 payment INTEGER NOT NULL, source_event_id TEXT NOT NULL REFERENCES world_events(id)
);
CREATE TABLE IF NOT EXISTS preparation_steps (
 id TEXT PRIMARY KEY, plan_id TEXT NOT NULL REFERENCES shared_preparation_plans(id),
 character_id TEXT NOT NULL REFERENCES characters(id), world_time TEXT NOT NULL, kind TEXT NOT NULL,
 activity_id TEXT REFERENCES character_life_activities(id), source_event_id TEXT REFERENCES world_events(id), spent INTEGER NOT NULL DEFAULT 0
);
"""

TASK_LABELS = {"earn_money": "通过劳动筹钱", "prepare_food": "备齐本人食物"}


class PreparationTask(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["earn_money", "prepare_food"]
    target: int = Field(ge=1, le=500, strict=True)
    spending_limit: int = Field(default=0, ge=0, le=100, strict=True)

    @model_validator(mode="before")
    @classmethod
    def defaults(cls, value):
        if isinstance(value, dict) and value.get("spending_limit") == None:
            value = {k: v for k, v in value.items() if k != "spending_limit"}
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.kind == "prepare_food" and self.target > 12:
            raise ValueError("一次共同筹备最多约定十二份食物")
        if self.kind == "earn_money" and self.spending_limit:
            raise ValueError("劳动筹钱不附加采购预算")
        return self


class SharedPlanIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: Literal["propose", "accept", "decline", "end", "check_in", "withdraw_readiness"]
    plan_id: str | None = Field(default=None, min_length=1, max_length=100)
    title: str = Field(default="共同筹备", min_length=1, max_length=80)
    location_id: str | None = Field(default=None, min_length=1, max_length=100)
    room_id: str | None = Field(default=None, min_length=1, max_length=100)
    deadline_world_time: str | None = Field(default=None, max_length=80)
    own_task: PreparationTask | None = None
    other_task: PreparationTask | None = None
    statement: str = Field(min_length=1, max_length=240)

    @model_validator(mode="before")
    @classmethod
    def defaults(cls, value):
        if isinstance(value, dict) and value.get("title") == None:
            value = {k: v for k, v in value.items() if k != "title"}
        return value

    @model_validator(mode="after")
    def coherent(self):
        if self.operation == "propose":
            if (
                self.plan_id
                or not self.location_id
                or not self.deadline_world_time
                or not self.own_task
                or not self.other_task
            ):
                raise ValueError("共同筹备需要真实地点、期限和双方各自的明确分工")
        elif not self.plan_id:
            raise ValueError("请引用双方真实的共同筹备计划")
        elif (
            self.own_task
            or self.other_task
            or self.location_id
            or self.room_id
            or self.deadline_world_time
        ):
            raise ValueError("回应已有分工不能悄悄更改条款；请另行商量新计划")
        return self


class SharedPlanService:
    @classmethod
    def context(cls, c, wid, cid, other, at):
        rows = cls.rows(c, wid, cid, at, other)
        selected = [p for p in rows if p["status"] in {"pending", "active"}] + [
            p for p in rows if p["status"] not in {"pending", "active"}
        ][:1]
        keys = (
            "id",
            "proposer_id",
            "recipient_id",
            "status",
            "title",
            "location_id",
            "location_name",
            "room_id",
            "room_name",
            "deadline_at",
            "statement",
            "response",
            "roles",
            "source_event_id",
            "follow_through",
            "can_follow_through",
        )
        return [
            {
                key: (
                    p[key][:160]
                    if isinstance(p[key], str) and key in {"statement", "response"}
                    else p[key]
                )
                for key in keys
            }
            for p in selected[:3]
        ]

    @staticmethod
    def rows(c, wid, cid, at, other=None):
        args = [wid, cid, cid]
        pair = ""
        if other:
            pair = " AND (p.proposer_id=? OR p.recipient_id=?)"
            args += [other, other]
        result = []
        for row in c.execute(
            "SELECT p.*,l.name location_name,r.name room_name FROM shared_preparation_plans p JOIN locations l ON l.id=p.location_id LEFT JOIN life_rooms r ON r.id=p.room_id "
            "WHERE p.world_id=? AND (p.proposer_id=? OR p.recipient_id=?)"
            + pair
            + " ORDER BY CASE WHEN p.status IN ('pending','active') THEN 0 ELSE 1 END,p.proposed_at DESC LIMIT 12",
            args,
        ):
            value = dict(row)
            value["other_id"] = (
                row["recipient_id"] if row["proposer_id"] == cid else row["proposer_id"]
            )
            if value["status"] in {"pending", "active"} and from_iso(row["deadline_at"]) <= at:
                value["status"] = "expired"
            roles = []
            for role in c.execute(
                "SELECT * FROM shared_preparation_roles WHERE plan_id=? ORDER BY character_id",
                (row["id"],),
            ):
                own = role["character_id"] == cid
                roles.append(
                    {
                        "character_id": role["character_id"],
                        "kind": role["kind"],
                        "label": TASK_LABELS[role["kind"]],
                        "target": role["target"],
                        "spending_limit": role["spending_limit"],
                        "progress": role["progress"]
                        if own
                        else role["target"]
                        if role["ready_at"]
                        else None,
                        "spent": role["spent"] if own else None,
                        "ready_at": role["ready_at"],
                        "ready_event_id": role["ready_event_id"],
                        "readiness_fresh": bool(
                            role["ready_at"]
                            and at - from_iso(role["ready_at"]) <= timedelta(days=1)
                        ),
                        "reason": role["private_reason"] if own else "",
                    }
                )
            value["roles"] = roles
            value["follow_through"] = [
                dict(m) for m in c.execute(
                    "SELECT m.id,m.proposer_id,m.recipient_id,m.kind,m.status,m.minutes,m.started_at,m.ends_at,m.reason,"
                    "m.source_event_id FROM preparation_follow_through f JOIN shared_life_moments m ON m.id=f.moment_id "
                    "WHERE f.plan_id=? ORDER BY m.rowid DESC LIMIT 4", (row["id"],),
                )
            ]
            for moment in value["follow_through"]:
                from world_engine.meal_sharing import MealSharingService
                moment.update(MealSharingService.terms(c, moment["id"]))
                if moment["status"] == "pending":
                    expiry = c.execute("SELECT expires_at FROM shared_life_moments WHERE id=?", (moment["id"],)).fetchone()[0]
                    if from_iso(expiry) <= at:
                        moment["status"] = "cancelled"
            value["can_follow_through"] = value["status"] == "completed" and not c.execute(
                "SELECT 1 FROM preparation_follow_through f JOIN shared_life_moments m ON m.id=f.moment_id "
                "WHERE f.plan_id=? AND (m.started_at IS NOT NULL OR m.status='active' OR (m.status='pending' AND m.expires_at>?))",
                (row["id"], to_iso(at)),
            ).fetchone()
            result.append(value)
        from world_engine.intimacy import IntimacyService

        IntimacyService.mask_records(
            c, wid, cid, result, ("title", "statement", "response", "reason")
        )
        for plan in result:
            IntimacyService.mask_records(c, wid, cid, plan["roles"], ("reason",))
            IntimacyService.mask_records(c, wid, cid, plan["follow_through"], ("reason",))
        return result

    @staticmethod
    def _record(c, actor, other, at, state, summary, payload):
        from world_engine.appointments import AppointmentService

        return AppointmentService._record(
            c, actor, other, at, "preparation_" + state, summary, payload
        )

    @staticmethod
    def _deadline(choice, at):
        try:
            end = datetime.fromisoformat(choice.deadline_world_time)
        except (ValueError, TypeError) as exc:
            raise ValueError("请给出带时区的明确筹备期限") from exc
        if (
            end.tzinfo is None
            or end.utcoffset() is None
            or not at + timedelta(hours=1) <= end <= at + timedelta(days=30)
        ):
            raise ValueError("筹备期限须带时区，并在一世界小时至三十个世界日以内")
        return end

    @staticmethod
    def _site(c, wid, lid, rid, first, second, at, end):
        place = c.execute(
            "SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1", (lid, wid)
        ).fetchone()
        if not place:
            raise ValueError("筹备核对地点不存在或已不可用")
        AppointmentRoomService.validate(c, wid, rid, lid, first, second, at, end)
        return place

    @staticmethod
    def _feasible(c, people, place, rid, at, end, tasks):
        from world_engine.schedules import ScheduleService

        for actor, task in zip(people, tasks):
            arrival = ScheduleService.earliest_arrival(c, actor, place, at, room_id=rid)
            if arrival + timedelta(hours=1 if task["kind"] == "earn_money" else 0) >= end:
                raise ValueError(
                    "现有活动、路程与完整劳动时间无法在期限前安排，请留出更充足的筹备时间"
                )

    @classmethod
    def apply(cls, c, actor, other, at, choice):
        from world_engine.intimacy import IntimacyService

        if (
            other is None
            or actor["world_id"] != other["world_id"]
            or actor["id"] == other["id"]
            or bool(actor["is_player"]) == bool(other["is_player"])
        ):
            raise ValueError("请选择同一世界里与玩家相处的另一位人物")
        wid = actor["world_id"]
        cls.maintain(c, wid, at)
        if choice.operation == "propose":
            IntimacyService._reachable(c, actor, other)
            if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                raise ValueError("一方要求留出空间，暂不提出共同筹备")
            end = cls._deadline(choice, at)
            site = cls._site(
                c, wid, choice.location_id, choice.room_id, actor["id"], other["id"], at, end
            )
            cls._feasible(
                c,
                (actor, other),
                site,
                choice.room_id,
                at,
                end,
                (choice.own_task.model_dump(), choice.other_task.model_dump()),
            )
            if c.execute(
                "SELECT 1 FROM shared_preparation_plans WHERE world_id=? AND status IN ('pending','active') AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
                (wid, actor["id"], other["id"], other["id"], actor["id"]),
            ).fetchone():
                raise ValueError("双方已有待回应或进行中的共同筹备，请先处理")
            pid = str(uuid4())
            event = cls._record(
                c,
                actor,
                other,
                at,
                "proposed",
                "提出共同筹备与分工，等待另一方明确同意：" + choice.statement,
                {
                    "plan_id": pid,
                    "own_task": choice.own_task.model_dump(),
                    "other_task": choice.other_task.model_dump(),
                    "location_id": choice.location_id,
                    "room_id": choice.room_id,
                    "deadline_at": to_iso(end),
                },
            )
            c.execute(
                "INSERT INTO shared_preparation_plans(id,world_id,proposer_id,recipient_id,status,title,statement,location_id,room_id,proposed_at,deadline_at,source_event_id) VALUES (?,?,?,?,'pending',?,?,?,?,?,?,?)",
                (
                    pid,
                    wid,
                    actor["id"],
                    other["id"],
                    choice.title,
                    choice.statement,
                    choice.location_id,
                    choice.room_id,
                    to_iso(at),
                    to_iso(end),
                    event,
                ),
            )
            for person, task in ((actor, choice.own_task), (other, choice.other_task)):
                c.execute(
                    "INSERT INTO shared_preparation_roles(plan_id,character_id,kind,target,spending_limit) VALUES (?,?,?,?,?)",
                    (pid, person["id"], task.kind, task.target, task.spending_limit),
                )
            return {
                "status": "pending",
                "plan_id": pid,
                "event_id": event,
                "summary": "分工已提出，等待另一方确认，尚未开始筹备。",
            }
        plan = c.execute(
            "SELECT * FROM shared_preparation_plans WHERE id=? AND world_id=? AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
            (choice.plan_id, wid, actor["id"], other["id"], other["id"], actor["id"]),
        ).fetchone()
        if not plan:
            raise ValueError("这份共同筹备不属于双方")
        if choice.operation in {"accept", "decline"}:
            if choice.operation == "accept":
                from world_engine.group_journeys import GroupJourneyService
                for person in (actor, other):
                    GroupJourneyService.assert_available(c, person)
            IntimacyService._reachable(c, actor, other)
            if plan["status"] != "pending" or plan["recipient_id"] != actor["id"]:
                raise ValueError("只有收到有效提议的人可以回应，不能接受自己的分工")
            state = "active" if choice.operation == "accept" else "declined"
            if state == "active":
                if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                    raise ValueError("一方仍要求停止主动联系")
                site = cls._site(
                    c,
                    wid,
                    plan["location_id"],
                    plan["room_id"],
                    actor["id"],
                    other["id"],
                    at,
                    from_iso(plan["deadline_at"]),
                )
                role_specs = [
                    dict(
                        c.execute(
                            "SELECT * FROM shared_preparation_roles WHERE plan_id=? AND character_id=?",
                            (plan["id"], p["id"]),
                        ).fetchone()
                    )
                    for p in (actor, other)
                ]
                cls._feasible(
                    c,
                    (actor, other),
                    site,
                    plan["room_id"],
                    at,
                    from_iso(plan["deadline_at"]),
                    role_specs,
                )
                for person in (actor, other):
                    if person["health"] <= 0:
                        raise ValueError("当前身体无法参加共同筹备")
                    if c.execute(
                        "SELECT 1 FROM shared_preparation_plans WHERE world_id=? AND status='active' AND (proposer_id=? OR recipient_id=?)",
                        (wid, person["id"], person["id"]),
                    ).fetchone():
                        raise ValueError("一方已有未完成的共同筹备，不能重复承诺同一份资源或劳动")
            summary = (
                "双方确认了各自的筹备职责，劳动、支出与到场核对仍需实际发生。"
                if state == "active"
                else "共同筹备提议已被婉拒。"
            )
            c.execute(
                "UPDATE shared_preparation_plans SET accepted_at=? WHERE id=?",
                (to_iso(at) if state == "active" else None, plan["id"]),
            )
            c.execute(
                "UPDATE npc_daily_states SET next_world_time=? WHERE character_id=?",
                (to_iso(at), actor["id"] if not actor["is_player"] else other["id"]),
            )
        elif choice.operation == "end":
            if (
                plan["status"] not in {"pending", "active"}
                or plan["status"] == "pending"
                and plan["proposer_id"] != actor["id"]
            ):
                raise ValueError("只能撤回自己的提议或退出已经确认的筹备")
            state = "ended"
            summary = "共同筹备已结束，实际劳动和支出保留，钱与物品仍各属本人。"
        elif choice.operation == "withdraw_readiness":
            if plan["status"] != "active":
                raise ValueError("筹备当前没有进行中")
            c.execute(
                "UPDATE shared_preparation_roles SET ready_at=NULL,ready_event_id=NULL WHERE plan_id=? AND character_id=?",
                (plan["id"], actor["id"]),
            )
            state = "active"
            summary = "本人撤回了上次筹备就绪的表示，可以继续商量。"
        else:
            if plan["status"] != "active":
                raise ValueError("筹备当前没有进行中")
            return cls.check_in(c, plan, actor, other, at, choice.statement)
        event = cls._record(
            c, actor, other, at, state, summary + choice.statement, {"plan_id": plan["id"]}
        )
        if choice.operation == "accept":
            c.execute(
                "UPDATE shared_preparation_plans SET accepted_event_id=? WHERE id=?",
                (event, plan["id"]),
            )
        c.execute(
            "UPDATE shared_preparation_plans SET status=?,response=?,reason=?,source_event_id=? WHERE id=?",
            (state, choice.statement, summary, event, plan["id"]),
        )
        cls.maintain(c, wid, at)
        return {"status": state, "plan_id": plan["id"], "event_id": event, "summary": summary}

    @staticmethod
    def _foods(c, actor, at):
        return [
            r
            for r in c.execute(
                "SELECT i.*,p.nutrition FROM item_instances i JOIN item_types t ON t.id=i.item_type_id JOIN world_item_profiles p ON p.item_type_id=i.item_type_id AND p.world_id=i.world_id "
                "WHERE i.world_id=? AND i.container_type='character_inventory' AND i.container_id=? AND i.owner_character_id=? AND i.quantity>0 AND i.condition>0 AND t.category='food' AND p.nutrition>0 "
                "AND NOT EXISTS (SELECT 1 FROM collective_food_decisions d WHERE d.batch_id=i.id AND d.actor_id=? AND d.policy IN ('avoid','reserve'))",
                (actor["world_id"], actor["id"], actor["id"], actor["id"]),
            )
            if not FoodService.spoiled(r, at)
        ]

    @classmethod
    def progress(cls, c, plan, role, actor, at):
        if role["kind"] == "prepare_food":
            value = sum(i["quantity"] for i in cls._foods(c, actor, at))
        else:
            for work in c.execute(
                "SELECT a.id,a.finish_event_id,d.result_json FROM character_life_activities a JOIN activity_task_details d ON d.activity_id=a.id "
                "WHERE a.world_id=? AND a.character_id=? AND a.kind='work' AND a.status='completed' AND a.started_world_time>=? AND a.finished_world_time<=? "
                "AND a.created_at>=(SELECT created_at FROM world_events WHERE id=?) AND a.id NOT IN (SELECT activity_id FROM preparation_work_receipts)",
                (
                    plan["world_id"],
                    actor["id"],
                    plan["accepted_at"],
                    to_iso(at),
                    plan["accepted_event_id"],
                ),
            ).fetchall():
                result = json.loads(work["result_json"])
                if (
                    result.get("completed") is True
                    and type(result.get("payment")) is int
                    and result["payment"] > 0
                ):
                    c.execute(
                        "INSERT INTO preparation_work_receipts VALUES (?,?,?,?,?)",
                        (
                            work["id"],
                            plan["id"],
                            actor["id"],
                            result["payment"],
                            work["finish_event_id"],
                        ),
                    )
            earned = c.execute(
                "SELECT COALESCE(SUM(payment),0) FROM preparation_work_receipts WHERE plan_id=? AND character_id=?",
                (plan["id"], actor["id"]),
            ).fetchone()[0]
            value = min(earned, actor["money"])
        value = max(0, min(role["target"], value))
        changed = value != role["progress"]
        c.execute(
            "UPDATE shared_preparation_roles SET progress=? WHERE plan_id=? AND character_id=?",
            (value, plan["id"], actor["id"]),
        )
        return value, changed

    @classmethod
    def _at_site(cls, c, plan, actor):
        location = c.execute(
            "SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1",
            (plan["location_id"], plan["world_id"]),
        ).fetchone()
        return bool(
            location
            and actor["health"] > 0
            and actor["space_layer"] == location["space_layer"]
            and actor["current_room_id"] == plan["room_id"]
            and great_circle_distance_km(
                actor["longitude"], actor["latitude"], location["longitude"], location["latitude"]
            )
            <= 0.1
            and not c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (actor["id"],),
            ).fetchone()
        )

    @classmethod
    def check_in(cls, c, plan, actor, other, at, statement):
        from world_engine.intimacy import IntimacyService

        if IntimacyService.paused(c, plan["world_id"], actor["id"], other["id"]):
            raise ValueError("一方要求留出空间，先商量是否继续共同核对")
        cls._site(
            c,
            plan["world_id"],
            plan["location_id"],
            plan["room_id"],
            actor["id"],
            other["id"],
            at,
            from_iso(plan["deadline_at"]),
        )
        if not cls._at_site(c, plan, actor):
            raise ValueError("请先实际到达约定地点或指定房间，再核对自己的筹备")
        LifeActivityService.assert_available(c, actor["id"])
        role = c.execute(
            "SELECT * FROM shared_preparation_roles WHERE plan_id=? AND character_id=?",
            (plan["id"], actor["id"]),
        ).fetchone()
        value, _ = cls.progress(c, plan, role, actor, at)
        if value < role["target"]:
            raise ValueError(
                "本人筹备尚未达到约定：筹钱需确认后真正完成劳动、收到报酬且目前仍持有相应资金；备食需本人携带足量可用食物"
            )
        event = cls._record(
            c,
            actor,
            other,
            at,
            "checked_in",
            "本人实际核对了约定的筹备份额，等待双方在同一处共同核对：" + statement,
            {"plan_id": plan["id"], "kind": role["kind"], "verified_target": role["target"]},
        )
        c.execute(
            "UPDATE shared_preparation_roles SET ready_at=?,ready_event_id=?,private_reason='' WHERE plan_id=? AND character_id=?",
            (to_iso(at), event, plan["id"], actor["id"]),
        )
        cls.maintain(c, plan["world_id"], at)
        latest = c.execute(
            "SELECT status,source_event_id FROM shared_preparation_plans WHERE id=?", (plan["id"],)
        ).fetchone()
        return {
            "status": latest["status"],
            "plan_id": plan["id"],
            "event_id": event,
            "summary": "双方在约定地点共同核对完成筹备，资源仍各自保管，后续使用由双方再商量。"
            if latest["status"] == "completed"
            else "本人筹备已核对，尚待双方共同核对。",
        }

    @classmethod
    def maintain(cls, c, wid, at):
        from world_engine.intimacy import IntimacyService

        updates = 0
        for plan in c.execute(
            "SELECT * FROM shared_preparation_plans WHERE world_id=? AND status IN ('pending','active')",
            (wid,),
        ).fetchall():
            first = c.execute(
                "SELECT * FROM characters WHERE id=?", (plan["proposer_id"],)
            ).fetchone()
            second = c.execute(
                "SELECT * FROM characters WHERE id=?", (plan["recipient_id"],)
            ).fetchone()
            if at >= from_iso(plan["deadline_at"]):
                event = cls._record(
                    c,
                    first,
                    second,
                    at,
                    "expired",
                    "筹备期限已到，未补造完成或归咎失约原因。",
                    {"plan_id": plan["id"]},
                )
                c.execute(
                    "UPDATE shared_preparation_plans SET status='expired',reason='筹备期限已到',source_event_id=? WHERE id=?",
                    (event, plan["id"]),
                )
                updates += 1
                continue
            if plan["status"] != "active":
                continue
            roles = c.execute(
                "SELECT * FROM shared_preparation_roles WHERE plan_id=?", (plan["id"],)
            ).fetchall()
            values = {}
            for role in roles:
                actor = first if role["character_id"] == first["id"] else second
                value, changed = cls.progress(c, plan, role, actor, at)
                values[actor["id"]] = value
                updates += int(changed)
            if not all(
                r["ready_at"]
                and at - from_iso(r["ready_at"]) <= timedelta(days=1)
                and values[r["character_id"]] >= r["target"]
                for r in roles
            ):
                continue
            if (
                IntimacyService.paused(c, wid, first["id"], second["id"])
                or not all(cls._at_site(c, plan, p) for p in (first, second))
                or not same_room(first, second)
                or great_circle_distance_km(
                    first["longitude"], first["latitude"], second["longitude"], second["latitude"]
                )
                > 0.02
            ):
                continue
            if any(LifeActivityService.running(c, p["id"]) for p in (first, second)):
                continue
            try:
                cls._site(
                    c,
                    wid,
                    plan["location_id"],
                    plan["room_id"],
                    first["id"],
                    second["id"],
                    at,
                    from_iso(plan["deadline_at"]),
                )
            except ValueError:
                continue
            event = cls._record(
                c,
                first,
                second,
                at,
                "completed",
                "双方实际在约定地点核对完成了各自筹备；钱和物品仍各属本人，后续使用另行商量。",
                {
                    "plan_id": plan["id"],
                    "readiness_event_ids": [r["ready_event_id"] for r in roles],
                },
            )
            c.execute(
                "UPDATE shared_preparation_plans SET status='completed',reason='双方共同核对完成筹备',source_event_id=? WHERE id=?",
                (event, plan["id"]),
            )
            updates += 1
            IntimacyService.remember_event(
                c,
                wid,
                first["id"],
                second["id"],
                event,
                at,
                "shared",
                "一起商量分工，并实际完成了共同筹备。",
            )
        return updates

    @classmethod
    def perform(cls, c, actor, at, places, appointment, routine):
        from world_engine.daily_life import DailyLifeService
        from world_engine.economy import EconomyService
        from world_engine.intimacy import IntimacyService
        from world_engine.schedules import ScheduleService

        plan = c.execute(
            "SELECT * FROM shared_preparation_plans WHERE world_id=? AND status='active' AND (proposer_id=? OR recipient_id=?) LIMIT 1",
            (actor["world_id"], actor["id"], actor["id"]),
        ).fetchone()
        if (
            not plan
            or actor["is_player"]
            or actor["health"] < 80
            or actor["satiety"] <= 30
            or actor["energy"] <= 25
            or DailyLifeService.is_sleep_time(actor, at)
        ):
            return None
        other = c.execute(
            "SELECT * FROM characters WHERE id=?",
            (plan["recipient_id"] if plan["proposer_id"] == actor["id"] else plan["proposer_id"],),
        ).fetchone()
        if IntimacyService.paused(c, actor["world_id"], actor["id"], other["id"]):
            return None
        role = c.execute(
            "SELECT * FROM shared_preparation_roles WHERE plan_id=? AND character_id=?",
            (plan["id"], actor["id"]),
        ).fetchone()
        if role["retry_at"] and from_iso(role["retry_at"]) > at:
            return None
        value, _ = cls.progress(c, plan, role, actor, at)
        c.execute(
            "UPDATE shared_preparation_roles SET retry_at=? WHERE plan_id=? AND character_id=?",
            (to_iso(at + timedelta(minutes=15)), plan["id"], actor["id"]),
        )
        if value >= role["target"]:
            try:
                location = cls._site(
                    c,
                    plan["world_id"],
                    plan["location_id"],
                    plan["room_id"],
                    actor["id"],
                    other["id"],
                    at,
                    from_iso(plan["deadline_at"]),
                )
            except ValueError as exc:
                c.execute(
                    "UPDATE shared_preparation_roles SET private_reason=? WHERE plan_id=? AND character_id=?",
                    (str(exc), plan["id"], actor["id"]),
                )
                return None
            if not cls._at_site(c, plan, actor):
                c.execute(
                    "UPDATE shared_preparation_roles SET retry_at=NULL WHERE plan_id=? AND character_id=?",
                    (plan["id"], actor["id"]),
                )
                if plan["room_id"]:
                    return AppointmentRoomService.approach(c, actor, plan, at, location)
                return DailyLifeService._travel(c, actor, at, location)
            if not role["ready_at"] or at - from_iso(role["ready_at"]) > timedelta(days=1):
                cls.check_in(
                    c, plan, actor, other, at, "我已经准备好自己承诺的份额，愿意和你一起核对。"
                )
                return "已核对本人筹备，等待共同核对"
            return None
        remaining = ScheduleService.planned_minutes(appointment, at)
        if role["kind"] == "earn_money":
            used = c.execute(
                "SELECT COUNT(*) FROM preparation_steps WHERE character_id=? AND kind='work' AND substr(world_time,1,10)=?",
                (actor["id"], at.date().isoformat()),
            ).fetchone()[0]
            if (
                used >= 2
                or remaining is not None
                and remaining < 60
                or at + timedelta(hours=1) > from_iso(plan["deadline_at"])
            ):
                return None
            intention = DailyLifeService._find_work(c, actor, at, places, appointment)
            activity = LifeActivityService.running(c, actor["id"])
            if activity and activity["kind"] == "work":
                c.execute(
                    "INSERT INTO preparation_steps VALUES (?,?,?,?, 'work',?,?,0)",
                    (
                        str(uuid4()),
                        plan["id"],
                        actor["id"],
                        to_iso(at),
                        activity["id"],
                        activity["source_event_id"],
                    ),
                )
            else:
                c.execute(
                    "UPDATE shared_preparation_roles SET private_reason=? WHERE plan_id=? AND character_id=?",
                    (intention, plan["id"], actor["id"]),
                )
            if (
                intention == "离开房间"
                or c.execute(
                    "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                    (actor["id"],),
                ).fetchone()
            ):
                c.execute(
                    "UPDATE shared_preparation_roles SET retry_at=NULL WHERE plan_id=? AND character_id=?",
                    (plan["id"], actor["id"]),
                )
            return intention
        budget = max(
            0,
            min(
                role["spending_limit"] - role["spent"],
                actor["money"] - max(10, routine["spec"].income_reserve if routine else 10),
            ),
        )
        bought = (
            EconomyService.buy_supply(c, actor, at, budget=budget, food_only=True)
            if budget
            else None
        )
        if bought:
            c.execute(
                "UPDATE shared_preparation_roles SET spent=spent+?,private_reason='' WHERE plan_id=? AND character_id=?",
                (bought["spent"], plan["id"], actor["id"]),
            )
            c.execute(
                "INSERT INTO preparation_steps VALUES (?,?,?,?, 'purchase',NULL,?,?)",
                (
                    str(uuid4()),
                    plan["id"],
                    actor["id"],
                    to_iso(at),
                    bought["source_event_id"],
                    bought["spent"],
                ),
            )
            return "用本人约定预算买到了筹备食物，尚未转交或共同用餐"
        if EconomyService.harvest(c, actor, at, food_only=True):
            return "从本人有权取用的真实来源准备食物"
        # 哥哥，筹备也是本人行动，只能依据亲历的食物来源而非远处实时库存选路。
        accessible = DailyLifeService.known_food_places(
            c, actor, at, [p["id"] for p in places]
        )
        candidates = [p for p in places if p["id"] in accessible and (
            actor["current_room_id"] or great_circle_distance_km(
                actor["longitude"], actor["latitude"], p["longitude"], p["latitude"]
            ) > 0.1
        )]
        if not candidates and not actor["current_room_id"]:
            familiar = DailyLifeService.familiar_place_to_search(
                c, actor, at, {p["id"]: p for p in places}
            )
            if familiar:
                candidates = [familiar]
        if actor["current_room_id"] or candidates:
            target = (
                min(
                    candidates,
                    key=lambda p: great_circle_distance_km(
                        actor["longitude"], actor["latitude"], p["longitude"], p["latitude"]
                    ),
                )
                if candidates
                else c.execute(
                    "SELECT * FROM locations WHERE id=?",
                    (actor["current_location_id"] or actor["location_id"],),
                ).fetchone()
            )
            if target and (
                actor["current_room_id"]
                or great_circle_distance_km(
                    actor["longitude"], actor["latitude"], target["longitude"], target["latitude"]
                )
                > 0.1
            ):
                c.execute(
                    "UPDATE shared_preparation_roles SET retry_at=NULL WHERE plan_id=? AND character_id=?",
                    (plan["id"], actor["id"]),
                )
                return DailyLifeService._travel(c, actor, at, target)
        c.execute(
            "UPDATE shared_preparation_roles SET private_reason='现有食物不足，采购预算或现场货源未满足，需要另行商量' WHERE plan_id=? AND character_id=?",
            (plan["id"], actor["id"]),
        )
        return None
