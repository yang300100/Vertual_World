"""哥哥，共同生活先取得双方同意，再使用各自真实的时间与物品。"""

from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.actions import ActionService
from world_engine.geo import great_circle_distance_km
from world_engine.inventory import InventoryService
from world_engine.life import LifeActivityService
from world_engine.meal_sharing import MealSharingService
from world_engine.proximity import same_room
from world_engine.repository import from_iso, to_iso

SHARED_LIFE_SCHEMA = """
CREATE TABLE IF NOT EXISTS shared_life_moments (
 id TEXT PRIMARY KEY, world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 proposer_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 recipient_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 kind TEXT NOT NULL CHECK(kind IN ('meal','rest','talk','care','hug','hold_hands')),
 status TEXT NOT NULL CHECK(status IN ('pending','active','completed','declined','cancelled','interrupted')),
 minutes INTEGER NOT NULL CHECK(minutes BETWEEN 1 AND 120), statement TEXT NOT NULL,
 proposed_at TEXT NOT NULL, expires_at TEXT NOT NULL, started_at TEXT, ends_at TEXT,
 proposer_activity_id TEXT REFERENCES character_life_activities(id),
 recipient_activity_id TEXT REFERENCES character_life_activities(id),
 source_event_id TEXT NOT NULL REFERENCES world_events(id), reason TEXT NOT NULL DEFAULT '',
 CHECK(proposer_id<>recipient_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_shared_life_pair ON shared_life_moments(
 world_id,MIN(proposer_id,recipient_id),MAX(proposer_id,recipient_id)) WHERE status IN ('pending','active');
CREATE INDEX IF NOT EXISTS idx_shared_life_running ON shared_life_moments(world_id,status,ends_at);
CREATE TABLE IF NOT EXISTS social_choice_receipts (
 world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE, request_id TEXT NOT NULL,
 player_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 payload_hash TEXT NOT NULL, result_json TEXT NOT NULL,
 PRIMARY KEY(world_id,player_id,request_id)
);
CREATE TABLE IF NOT EXISTS preparation_follow_through (
 moment_id TEXT PRIMARY KEY REFERENCES shared_life_moments(id) ON DELETE CASCADE,
 plan_id TEXT NOT NULL REFERENCES shared_preparation_plans(id) ON DELETE CASCADE,
 preparation_event_id TEXT NOT NULL REFERENCES world_events(id)
);
CREATE INDEX IF NOT EXISTS idx_preparation_follow_through ON preparation_follow_through(plan_id);
"""

MOMENT_LABELS = {"meal": "共同用餐", "rest": "一起休息", "talk": "留时间聊天", "care": "陪伴休养", "hug": "拥抱", "hold_hands": "牵手"}


class SharedLifeIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    operation: Literal["invite", "accept", "decline", "end"]
    kind: Literal["meal", "rest", "talk", "care", "hug", "hold_hands"] | None = None
    moment_id: str | None = Field(default=None, min_length=1, max_length=100)
    preparation_plan_id: str | None = Field(default=None, min_length=1, max_length=100)
    journey_id: str | None = Field(default=None, min_length=1, max_length=100)
    meal_supply: Literal["each_own", "proposer"] = "each_own"
    minutes: int = Field(default=15, ge=1, le=120, strict=True)
    statement: str = Field(min_length=1, max_length=240)

    @model_validator(mode="before")
    @classmethod
    def default_meal_supply(cls, value):
        if isinstance(value, dict):
            # 哥哥，模型未指定时沿用既有默认十五分钟，不把空值当成新的条款。
            value = {k: v for k, v in value.items() if not (k in {"meal_supply", "minutes"} and v is None)}
        return value

    @model_validator(mode="after")
    def validate_choice(self):
        if self.operation == "invite" and (not self.kind or self.moment_id):
            raise ValueError("共同生活邀请需要活动类型，不能引用旧邀请")
        if self.operation != "invite" and not self.moment_id:
            raise ValueError("请引用双方真实的共同生活邀请")
        if self.preparation_plan_id and (self.operation != "invite" or self.kind not in {"meal", "rest", "talk"}):
            raise ValueError("只有新的用餐、休息或聊天邀请可以衔接共同筹备；回应时沿用原邀请")
        if self.meal_supply != "each_own" and (self.operation != "invite" or self.kind != "meal"):
            raise ValueError("只有新的用餐邀请可以承诺由邀请者提供双方食物；回应不能修改原来的安排")
        if self.journey_id and (self.operation != "invite" or self.kind not in {"meal","rest"} or self.preparation_plan_id):
            raise ValueError("旅行停留只衔接新的用餐或休息邀请；回应沿用原邀请，不能混用筹备来源")
        return self


class SharedLifeService:
    @staticmethod
    def rows(c, wid, cid, at, other=None):
        result = []
        condition = " AND (proposer_id=? OR recipient_id=?)" if other else ""
        args = [wid, cid, cid] + ([other, other] if other else [])
        for row in c.execute(
            "SELECT * FROM shared_life_moments WHERE world_id=? AND (proposer_id=? OR recipient_id=?)"+condition+
            " ORDER BY CASE status WHEN 'active' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, proposed_at DESC LIMIT 24", args,
        ):
            value = dict(row)
            value["other_id"] = row["recipient_id"] if row["proposer_id"] == cid else row["proposer_id"]
            if other and value["other_id"] != other:
                continue
            value["label"] = MOMENT_LABELS[row["kind"]]
            link = c.execute("SELECT plan_id FROM preparation_follow_through WHERE moment_id=?", (row["id"],)).fetchone()
            value["preparation_plan_id"] = link[0] if link else None
            from world_engine.journey_breaks import JourneyBreakService
            journey_link = JourneyBreakService.link(c,row["id"])
            value["journey_id"] = journey_link["journey_id"] if journey_link else None
            value["journey_stop_index"] = journey_link["stop_index"] if journey_link else None
            value.update(MealSharingService.terms(c, row["id"]))
            if row["status"] == "pending" and from_iso(row["expires_at"]) <= at:
                value["status"] = "cancelled"
            result.append(value)
        return result

    @staticmethod
    def _near(first, second, kind):
        radius = 0.003 if kind in {"hug", "hold_hands"} else 0.02
        return same_room(first, second) and great_circle_distance_km(
            first["longitude"], first["latitude"], second["longitude"], second["latitude"]
        ) <= radius

    @staticmethod
    def _food(c, person, at):
        rows = MealSharingService.foods(c, person, at)
        if rows:
            return rows[0]
        raise ValueError("共同用餐需要双方各有一份可食用的随身食物；可以先购买或明确赠与")

    @staticmethod
    def _preparation(c, actor, other, plan_id):
        """哥哥，筹备经历只能由原来的双方续接，不能重复兑现已经开始的活动。"""
        plan = c.execute(
            "SELECT * FROM shared_preparation_plans WHERE id=? AND world_id=? AND "
            "((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
            (plan_id, actor["world_id"], actor["id"], other["id"], other["id"], actor["id"]),
        ).fetchone()
        if not plan or plan["status"] != "completed":
            raise ValueError("只能续接双方已共同核对完成的筹备")
        if c.execute(
            "SELECT 1 FROM preparation_follow_through f JOIN shared_life_moments m ON m.id=f.moment_id "
            "WHERE f.plan_id=? AND (m.status IN ('pending','active') OR m.started_at IS NOT NULL)", (plan_id,),
        ).fetchone():
            raise ValueError("这份筹备已有待回应或实际开始过的后续活动；新的相处可另行邀请")
        return plan

    @staticmethod
    def _record(c, first, second, at, state, summary, payload):
        event = ActionService._record_event(c, world_id=first["world_id"], tick_id=str(uuid4()), occurred_at=at,
            event_type=f"social.shared_{state}", actor_id=first["id"], target_id=second["id"],
            location_id=first["current_location_id"] or first["location_id"], summary=summary, payload=payload)
        for person in (first, second):
            ActionService._record_memory(c, world_id=first["world_id"], character_id=person["id"], event_id=event,
                                        memory_type="experienced", summary=summary, importance=7 if state == "completed" else 5)
        return event

    @classmethod
    def apply(cls, c, actor, other, at, choice, *, exclude_contract_id=None):
        # 哥哥，进食、转交、活动与邀请状态一起成功，拒绝时不留下半次请客。
        c.execute("SAVEPOINT shared_life_apply")
        try:
            return cls._apply(c, actor, other, at, choice, exclude_contract_id=exclude_contract_id)
        except Exception:
            c.execute("ROLLBACK TO shared_life_apply")
            raise
        finally:
            c.execute("RELEASE shared_life_apply")

    @classmethod
    def _apply(cls, c, actor, other, at, choice, *, exclude_contract_id=None):
        from world_engine.intimacy import IntimacyService
        if other is None or actor["world_id"] != other["world_id"] or actor["id"] == other["id"]:
            raise ValueError("请选择同一世界中的另一位人物")
        wid = actor["world_id"]
        from world_engine.journey_breaks import JourneyBreakService
        JourneyBreakService.expire_pending(c,wid,at)
        if choice.operation == "invite":
            IntimacyService._reachable(c, actor, other)
            if IntimacyService.paused(c, wid, actor["id"], other["id"]):
                raise ValueError("一方要求留出空间，暂不提出新的共同生活邀请")
            if actor["health"] <= 0 or other["health"] <= 0:
                raise ValueError("当前身体状态无法参与共同生活")
            c.execute("UPDATE shared_life_moments SET status='cancelled',reason='邀请已过期' WHERE world_id=? AND status='pending' AND expires_at<=?", (wid, to_iso(at)))
            preparation = cls._preparation(c, actor, other, choice.preparation_plan_id) if choice.preparation_plan_id else None
            if c.execute("SELECT 1 FROM shared_life_moments WHERE world_id=? AND status IN ('pending','active') AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
                         (wid, actor["id"], other["id"], other["id"], actor["id"])).fetchone():
                raise ValueError("双方已有待回应或进行中的共同生活安排")
            mid = str(uuid4())
            from world_engine.journey_breaks import JourneyBreakService
            journey_link = JourneyBreakService.prepare(c,actor,other,at,choice.journey_id) if choice.journey_id else None
            minutes = 1 if choice.kind == "hug" else choice.minutes
            terms = "邀请者愿意提供双方各一份食物，尚未转交或消耗。" if choice.meal_supply == "proposer" else ""
            event = cls._record(c, actor, other, at, "invited", f"一方邀请{MOMENT_LABELS[choice.kind]}，等待另一方回应：{choice.statement}" + terms, {"moment_id": mid, "kind": choice.kind, "preparation_plan_id": choice.preparation_plan_id, "meal_supply": choice.meal_supply,"journey_break":journey_link})
            expires=at+timedelta(days=1)
            if journey_link:
                deadline=c.execute("SELECT deadline_at FROM joint_journeys WHERE id=?",(journey_link["journey_id"],)).fetchone()[0]
                expires=min(expires,from_iso(deadline))
            c.execute("INSERT INTO shared_life_moments(id,world_id,proposer_id,recipient_id,kind,status,minutes,statement,proposed_at,expires_at,source_event_id) VALUES (?,?,?,?,?,'pending',?,?,?,?,?)",
                      (mid, wid, actor["id"], other["id"], choice.kind, minutes, choice.statement, to_iso(at), to_iso(expires), event))
            if preparation:
                c.execute("INSERT INTO preparation_follow_through VALUES (?,?,?)", (mid, preparation["id"], preparation["source_event_id"]))
            if journey_link:
                c.execute("INSERT INTO journey_breaks VALUES (?,?,?,?,?)",(mid,journey_link["journey_id"],journey_link["stop_index"],journey_link["arrival_event_id"],journey_link["pause_event_id"]))
            if choice.meal_supply == "proposer":
                c.execute("INSERT INTO shared_meal_offers VALUES (?,?,?)", (mid, actor["id"], event))
            return {"status": "pending", "moment_id": mid, "event_id": event, "summary": "原旅行已暂停，邀请已表达；尚未开始用餐或休息，恢复旅行仍须双方另行同意。" if journey_link else "邀请已表达，尚未开始共同活动。"}
        moment = c.execute("SELECT * FROM shared_life_moments WHERE id=? AND world_id=? AND ((proposer_id=? AND recipient_id=?) OR (proposer_id=? AND recipient_id=?))",
                           (choice.moment_id, wid, actor["id"], other["id"], other["id"], actor["id"])).fetchone()
        if not moment:
            raise ValueError("这份邀请不属于双方")
        if choice.operation == "end":
            if moment["status"] == "active":
                return cls.finish(c, moment, at, "interrupted", choice.statement)
            if moment["status"] != "pending" or moment["proposer_id"] != actor["id"]:
                raise ValueError("只能撤回自己的待回应邀请，或退出进行中的活动")
            state = "cancelled"
        else:
            IntimacyService._reachable(c, actor, other)
            if moment["recipient_id"] != actor["id"] or moment["status"] != "pending" or from_iso(moment["expires_at"]) <= at:
                raise ValueError("只有收到这份有效邀请的人能够回应")
            state = "declined" if choice.operation == "decline" else "active"
        if state != "active":
            event = cls._record(c, actor, other, at, state, f"共同生活邀请已{'被拒绝' if state=='declined' else '撤回'}：{choice.statement}", {"moment_id": moment["id"]})
            c.execute("UPDATE shared_life_moments SET status=?,reason=?,source_event_id=? WHERE id=?", (state, choice.statement, event, moment["id"]))
            return {"status": state, "moment_id": moment["id"], "event_id": event, "summary": choice.statement}
        if IntimacyService.paused(c, wid, actor["id"], other["id"]):
            raise ValueError("一方仍要求留出空间，暂不开始共同活动")
        if not cls._near(actor, other, moment["kind"]):
            raise ValueError("请先实际走到同一场景；牵手、拥抱需在3米内，其余共同活动需在20米内")
        until = at + timedelta(minutes=moment["minutes"])
        from world_engine.journey_breaks import JourneyBreakService
        journey_link = JourneyBreakService.validate_acceptance(c,moment,actor,other,at)
        from world_engine.schedules import ScheduleService
        for person in (actor, other):
            from world_engine.group_journeys import GroupJourneyService
            GroupJourneyService.assert_available(c, person)
            if person["health"] <= 0:
                raise ValueError("当前身体状态无法参加")
            LifeActivityService.assert_available(c, person["id"])
            plan = ScheduleService.assess(c, person, at, exclude_contract_id=exclude_contract_id)
            if plan and plan["depart_at"] <= until:
                raise ValueError("这段时间已有需要准备的赴约安排，可以商量短些或改期")
            if c.execute("SELECT 1 FROM shared_life_moments WHERE world_id=? AND status='active' AND (proposer_id=? OR recipient_id=?)", (wid, person["id"], person["id"])).fetchone():
                raise ValueError("一方已有正在进行的共同生活安排")
        food = MealSharingService.prepare(c, moment, actor, other, at)
        before = InventoryService.snapshot(c, wid) if food else None
        activities = {}
        for person in (actor, other):
            resting = moment["kind"] in {"rest", "care"}
            activities[person["id"]] = LifeActivityService.start(c, person, at, "rest" if resting else "wait", moment["minutes"])
        receipts = MealSharingService.consume(c, food, at)
        summary = f"双方开始{MOMENT_LABELS[moment['kind']]}，计划持续{moment['minutes']}个世界分钟，尚未完成。"
        if food:
            summary += "邀请者实际提供了两份自己的食物，双方各吃一份，已经转交并进食，尚未完成相处。" if MealSharingService.terms(c, moment["id"])["meal_supply"] == "proposer" else "双方各自吃掉一份自己的随身食物。"
        link = c.execute("SELECT plan_id,preparation_event_id FROM preparation_follow_through WHERE moment_id=?", (moment["id"],)).fetchone()
        if link:
            summary += "这是双方另行同意的筹备后相处，筹备预算仍各自保管。"
        if journey_link:
            summary += "这是原行程暂停后另行同意的到站相处；结束不自动恢复旅行。"
        event = cls._record(c, actor, other, at, "started", summary,
                            {"moment_id": moment["id"], "kind": moment["kind"], "food_items": [r["consumed_item_id"] for r in receipts], "meal_servings": receipts,
                             "preparation_plan_id": link["plan_id"] if link else None,
                             "preparation_event_id": link["preparation_event_id"] if link else None,"journey_break":journey_link})
        if food:
            MealSharingService.record(c, moment["id"], event, receipts)
            InventoryService.audit(c, wid, event, before)
        c.execute("UPDATE shared_life_moments SET status='active',started_at=?,ends_at=?,proposer_activity_id=?,recipient_activity_id=?,source_event_id=? WHERE id=?",
                  (to_iso(at), to_iso(until), activities[moment["proposer_id"]], activities[moment["recipient_id"]], event, moment["id"]))
        return {"status": "active", "moment_id": moment["id"], "event_id": event, "summary": summary}

    @classmethod
    def finish(cls, c, moment, at, state, reason):
        current = c.execute("SELECT * FROM shared_life_moments WHERE id=?", (moment["id"],)).fetchone()
        if current["status"] != "active":
            return {"status": current["status"], "moment_id": current["id"], "event_id": current["source_event_id"], "summary": current["reason"]}
        moment = current
        first = c.execute("SELECT * FROM characters WHERE id=?", (moment["proposer_id"],)).fetchone()
        second = c.execute("SELECT * FROM characters WHERE id=?", (moment["recipient_id"],)).fetchone()
        for aid in (moment["proposer_activity_id"], moment["recipient_activity_id"]):
            activity = c.execute("SELECT * FROM character_life_activities WHERE id=? AND status='running'", (aid,)).fetchone()
            if activity:
                LifeActivityService.finish(c, activity, at, "completed" if state == "completed" else "cancelled", reason)
        summary = f"双方的{MOMENT_LABELS[moment['kind']]}{'完成' if state=='completed' else '中止'}：{reason}"
        if MealSharingService.terms(c, moment["id"])["meal_supply"] == "proposer":
            summary += "；本次由邀请者提供双方食物，已经实际吃掉的食物不退回，也不形成欠款或其他关系义务。"
        link = c.execute("SELECT plan_id,preparation_event_id FROM preparation_follow_through WHERE moment_id=?", (moment["id"],)).fetchone()
        if link:
            summary += "；这是共同筹备之后另行同意的相处，实际消耗保留。"
        from world_engine.journey_breaks import JourneyBreakService
        journey_link=JourneyBreakService.link(c,moment["id"])
        if journey_link:
            summary += "；本次相处结束不自动恢复原旅行，已消耗的时间与食物保留。"
        event = cls._record(c, first, second, at, state, summary, {"moment_id": moment["id"], "kind": moment["kind"], "minutes": moment["minutes"],
            "preparation_plan_id": link["plan_id"] if link else None, "preparation_event_id": link["preparation_event_id"] if link else None,"journey_break":journey_link})
        c.execute("UPDATE shared_life_moments SET status=?,reason=?,source_event_id=? WHERE id=?", (state, reason, event, moment["id"]))
        if state == "completed":
            # 哥哥，共同经历次数来自实际完成的相处，不额外发放亲和或信任数值。
            for observer, subject in ((first, second), (second, first)):
                c.execute("UPDATE character_acquaintances SET shared_experiences=shared_experiences+1 WHERE observer_id=? AND subject_id=?",
                          (observer["id"], subject["id"]))
        from world_engine.intimacy import IntimacyService
        IntimacyService.remember_event(c, first["world_id"], first["id"], second["id"], event, at, "shared", summary)
        return {"status": state, "moment_id": moment["id"], "event_id": event, "summary": summary}

    @classmethod
    def maintain(cls, c, wid, at):
        from world_engine.intimacy import IntimacyService
        from world_engine.journey_breaks import JourneyBreakService
        from world_engine.journey_room_appointments import JourneyRoomService
        JourneyBreakService.expire_pending(c,wid,at)
        for moment in c.execute("SELECT * FROM shared_life_moments WHERE world_id=? AND status='active'", (wid,)).fetchall():
            people = [c.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone() for cid in (moment["proposer_id"], moment["recipient_id"])]
            activities = [c.execute("SELECT status FROM character_life_activities WHERE id=?", (aid,)).fetchone() for aid in (moment["proposer_activity_id"], moment["recipient_activity_id"])]
            if any(p is None or p["health"] <= 0 for p in people) or not cls._near(*people, moment["kind"]):
                cls.finish(c, moment, at, "interrupted", "双方位置或身体状态发生变化")
            elif IntimacyService.paused(c, wid, people[0]["id"], people[1]["id"]):
                cls.finish(c, moment, at, "interrupted", "一方表达了需要独处的界限")
            elif moment["kind"] != "meal" and any(p["satiety"] <= 5 for p in people):
                cls.finish(c, moment, at, "interrupted", "一方需要先处理进食的身体需要")
            elif moment["kind"] not in {"rest", "care"} and any(p["energy"] <= 5 for p in people):
                cls.finish(c, moment, at, "interrupted", "一方需要先休息")
            elif any(a is None or a[0] not in {"running", "completed"} for a in activities):
                cls.finish(c, moment, at, "interrupted", "一方提前结束了自己的活动")
            elif room_risk := JourneyRoomService.activity_risk(c,moment["id"],at):
                cls.finish(c,moment,at,"interrupted","旅途室内接待条件改变："+room_risk)
            elif at >= from_iso(moment["ends_at"]):
                cls.finish(c, moment, from_iso(moment["ends_at"]), "completed", "双方实际度过了约定的相处时间")
