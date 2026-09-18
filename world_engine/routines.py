"""可登记的周期作息与真实执行记录；习惯不等于已经完成的活动。"""

import json
import math
from datetime import UTC, datetime, time, timedelta
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from world_engine.geo import great_circle_distance_km
from world_engine.life import LifeActivityService
from world_engine.npc_goals import LifeGoalSpec, NpcGoalService, validate_dependencies
from world_engine.repository import from_iso, to_iso
from world_engine.schedules import ScheduleService


class RoutineSlot(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    key: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    name: str = Field(min_length=1, max_length=80)
    weekdays: list[Annotated[int, Field(ge=0, le=6, strict=True)]] = Field(
        default_factory=lambda: list(range(7)), min_length=1, max_length=7
    )
    starts_at: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    duration_minutes: int = Field(ge=5, le=720, strict=True)
    target_minutes: int | None = Field(default=None, ge=1, le=720, strict=True)
    kind: Literal["work", "rest", "craft"]
    location_id: str = Field(min_length=1, max_length=100)
    recipe_id: str | None = None
    supply_purchase_budget: int = Field(default=0, ge=0, le=100000, strict=True)
    visibility: Literal["private", "public"] = "private"


class RoutinePlanSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    element_type: Literal["npc_routine"] = "npc_routine"
    name: str = Field(min_length=1, max_length=80)
    character_id: str = Field(min_length=1, max_length=100)
    expected_revision: int | None = Field(default=None, ge=1, strict=True)
    enabled: bool = Field(default=True, strict=True)
    utc_offset_minutes: int = Field(default=0, ge=-720, le=840, strict=True)
    goal: Literal["stable_routine", "maintain_reserve"] = "stable_routine"
    income_reserve: int = Field(default=30, ge=0, le=100000, strict=True)
    max_work_minutes_per_day: int = Field(default=240, ge=60, le=720, strict=True)
    allow_alternative_work: bool = Field(default=True, strict=True)
    local_search_radius_km: int = Field(default=10, ge=1, le=30, strict=True)
    slots: list[RoutineSlot] = Field(min_length=1, max_length=16)
    ambitions: list[LifeGoalSpec] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def coherent_week(self):
        validate_dependencies(self.ambitions)
        keys = set()
        windows = []
        for slot in self.slots:
            if slot.key in keys or len(slot.weekdays) != len(set(slot.weekdays)):
                raise ValueError("时段标识和星期不能重复")
            keys.add(slot.key)
            if slot.target_minutes and slot.target_minutes > slot.duration_minutes:
                raise ValueError("目标活动分钟数不能超过时间窗")
            if slot.kind == "work" and (
                slot.duration_minutes < 60
                or slot.target_minutes is not None
                and slot.target_minutes % 60
            ):
                raise ValueError("工作至少需要一小时，目标分钟数须为60的倍数")
            if (slot.kind == "craft") != bool(slot.recipe_id):
                raise ValueError("制作必须指定已登记配方；其他时段不能指定配方")
            if slot.kind != "craft" and slot.supply_purchase_budget:
                raise ValueError("只有制作时段可以设置准备材料的采购预算")
            hour, minute = map(int, slot.starts_at.split(":"))
            for day in slot.weekdays:
                start = day * 1440 + hour * 60 + minute
                for earlier, later in windows:
                    if any(
                        start < later + shift and start + slot.duration_minutes > earlier + shift
                        for shift in (-10080, 0, 10080)
                    ):
                        raise ValueError("每周作息时段重叠，包括跨午夜和跨周的部分")
                windows.append((start, start + slot.duration_minutes))
        return self


class RoutineService:
    @staticmethod
    def register(c, wid, rid, spec, at):
        actor = c.execute(
            "SELECT * FROM characters WHERE id=? AND world_id=? AND is_player=0",
            (spec.character_id, wid),
        ).fetchone()
        if actor is None or spec.enabled and actor["health"] <= 0:
            raise ValueError("作息必须属于当前世界仍存活的 NPC，不能替玩家安排人生")
        old = c.execute(
            "SELECT * FROM npc_routine_plans WHERE character_id=?", (spec.character_id,)
        ).fetchone()
        if (
            old
            and spec.expected_revision != old["revision"]
            or not old
            and spec.expected_revision is not None
        ):
            raise ValueError("作息版本已变化，请读取当前记录后再修订")
        for slot in spec.slots:
            if old and not spec.enabled:
                break
            location = c.execute(
                "SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1",
                (slot.location_id, wid),
            ).fetchone()
            if location is None:
                raise ValueError("作息引用了不存在、停用或其他世界的地点")
            if (
                slot.kind == "work"
                and location["kind"] != "workplace"
                and not c.execute(
                    "SELECT 1 FROM workplace_accounts WHERE world_id=? AND location_id=?",
                    (wid, slot.location_id),
                ).fetchone()
            ):
                raise ValueError("工作时段需要已有工作场所或已登记工资账户")
            if slot.kind == "craft":
                recipe = c.execute(
                    "SELECT spec_json FROM activity_recipes WHERE id=? AND world_id=?",
                    (slot.recipe_id, wid),
                ).fetchone()
                if not recipe:
                    raise ValueError("制作时段需要已登记配方")
                recipe = json.loads(recipe[0])
                if (
                    recipe["kind"] != "craft"
                    or recipe["location_id"] != slot.location_id
                    or recipe["duration_minutes"] > slot.duration_minutes
                ):
                    raise ValueError("制作配方的类型、地点或时长与时段不符")
                if slot.target_minutes and slot.target_minutes % recipe["duration_minutes"]:
                    raise ValueError("制作目标分钟数须为完整工序时长的倍数")
        if spec.enabled:
            for goal in spec.ambitions:
                if goal.deadline_world_time and goal.deadline_world_time <= at:
                    raise ValueError("生活目标的期限必须晚于登记生效时间")
                work_ids = set(goal.work_location_ids) | {slot.location_id for slot in spec.slots if slot.kind == "work"}
                if goal.kind == "reserve_money" and not work_ids:
                    raise ValueError("储蓄目标至少需要一个已知工作地点")
                for lid in work_ids:
                    location = c.execute("SELECT kind FROM locations WHERE id=? AND world_id=? AND is_active=1", (lid,wid)).fetchone()
                    if not location or location[0]!="workplace" and not c.execute("SELECT 1 FROM workplace_accounts WHERE world_id=? AND location_id=?",(wid,lid)).fetchone():
                        raise ValueError("生活目标必须引用当前世界有效的工作地点")
                if goal.kind == "craft_stock":
                    recipe = c.execute("SELECT spec_json FROM activity_recipes WHERE id=? AND world_id=?", (goal.recipe_id,wid)).fetchone()
                    if not recipe or json.loads(recipe[0])["kind"]!="craft":
                        raise ValueError("生活目标需要当前世界已登记的制作配方")
        pid = old["id"] if old else str(uuid4())
        revision = old["revision"] + 1 if old else 1
        if old:
            for row in c.execute(
                "SELECT * FROM npc_routine_occurrences WHERE plan_id=? AND status IN ('planned','travelling','active','deferred')",
                (pid,),
            ).fetchall():
                RoutineService.transition(
                    c, row, at, "superseded", "作息已修订；已经开始的活动继续按原规则结算"
                )
        c.execute(
            "INSERT INTO npc_routine_plans VALUES (?,?,?,?,?,?,?) ON CONFLICT(character_id) DO UPDATE SET registration_id=excluded.registration_id,revision=excluded.revision,spec_json=excluded.spec_json,effective_world_time=excluded.effective_world_time",
            (pid, wid, spec.character_id, rid, revision, spec.model_dump_json(), to_iso(at)),
        )
        c.execute(
            "INSERT INTO npc_routine_cursors VALUES (?,?,?) ON CONFLICT(plan_id) DO UPDATE SET revision=excluded.revision,last_world_time=excluded.last_world_time",
            (pid, revision, to_iso(at)),
        )
        NpcGoalService.register(c,wid,spec.character_id,pid,revision,spec.ambitions if spec.enabled else [],at)
        return pid

    @staticmethod
    def transition(c, row, at, status, reason, next_attempt=None):
        from world_engine.actions import ActionService

        current = c.execute(
            "SELECT status,reason FROM npc_routine_occurrences WHERE id=?", (row["id"],)
        ).fetchone()
        if current["status"] != status or current["reason"] != reason:
            event = ActionService._record_event(
                c,
                world_id=row["world_id"],
                tick_id=str(uuid4()),
                occurred_at=at,
                event_type="life.routine_changed",
                actor_id=row["character_id"],
                target_id=None,
                location_id=None,
                summary=f"周期安排更新：{reason}。",
                payload={"occurrence_id": row["id"], "status": status},
            )
            from world_engine.event_history import EventHistoryService

            if row["source_event_id"]:
                EventHistoryService.link(
                    c, event, row["source_event_id"], "routine_progress", strict=False
                )
            if status in {"completed", "partial", "missed", "superseded"}:
                for activity in c.execute(
                    "SELECT a.source_event_id,a.finish_event_id FROM npc_routine_activities r JOIN character_life_activities a ON a.id=r.activity_id WHERE r.occurrence_id=?",
                    (row["id"],),
                ):
                    source = activity["finish_event_id"] or activity["source_event_id"]
                    if source:
                        EventHistoryService.link(c, event, source, "routine_activity", strict=False)
            c.execute(
                "UPDATE npc_routine_occurrences SET source_event_id=? WHERE id=?",
                (event, row["id"]),
            )
        c.execute(
            "UPDATE npc_routine_occurrences SET status=?,reason=?,next_attempt_world_time=? WHERE id=?",
            (status, reason, to_iso(next_attempt) if next_attempt else None, row["id"]),
        )

    @staticmethod
    def completed_minutes(c, oid):
        total = 0
        for row in c.execute(
            "SELECT a.* FROM npc_routine_activities r JOIN character_life_activities a ON a.id=r.activity_id WHERE r.occurrence_id=? AND a.status='completed'",
            (oid,),
        ):
            total += max(
                0,
                int(
                    (
                        from_iso(row["ends_world_time"]) - from_iso(row["started_world_time"])
                    ).total_seconds()
                    // 60
                ),
            )
        return total

    @classmethod
    def finish_due(cls, c, wid, at):
        for row in c.execute(
            "SELECT * FROM npc_routine_occurrences WHERE world_id=? AND ends_world_time<=? AND status IN ('planned','travelling','active','deferred')",
            (wid, to_iso(at)),
        ).fetchall():
            done = cls.completed_minutes(c, row["id"])
            state = (
                "completed" if done >= row["required_minutes"] else "partial" if done else "missed"
            )
            cls.transition(
                c,
                row,
                at,
                state,
                f"时段结束，实际完成 {done}/{row['required_minutes']} 分钟；"
                + (row["reason"] or "按实际活动记录结算"),
            )

    @staticmethod
    def work_remaining(c, actor, spec, at):
        offset = timedelta(minutes=spec.utc_offset_minutes)
        day_start = (at + offset).replace(hour=0, minute=0, second=0, microsecond=0) - offset
        seconds = 0
        for row in c.execute(
            "SELECT * FROM character_life_activities WHERE character_id=? AND kind IN ('work','craft','repair') AND started_world_time<? AND ends_world_time>?",
            (actor["id"], to_iso(at), to_iso(day_start)),
        ):
            end = min(
                at,
                from_iso(row["finished_world_time"] or row["last_processed_world_time"]),
                from_iso(row["ends_world_time"]),
            )
            seconds += max(
                0, (end - max(day_start, from_iso(row["started_world_time"]))).total_seconds()
            )
        return max(0, spec.max_work_minutes_per_day - math.ceil(seconds / 60))

    @staticmethod
    def required_minutes(c, slot):
        unit = 60 if slot.kind == "work" else 1
        if slot.kind == "craft":
            recipe = c.execute(
                "SELECT spec_json FROM activity_recipes WHERE id=?", (slot.recipe_id,)
            ).fetchone()
            unit = json.loads(recipe[0])["duration_minutes"] if recipe else slot.duration_minutes
        return slot.target_minutes or max(unit, slot.duration_minutes // unit * unit)

    @classmethod
    def record_skipped_windows(cls, c, plan, spec, at):
        from world_engine.actions import ActionService

        cursor = c.execute(
            "SELECT * FROM npc_routine_cursors WHERE plan_id=?", (plan["id"],)
        ).fetchone()
        previous = (
            from_iso(cursor["last_world_time"])
            if cursor and cursor["revision"] == plan["revision"]
            else from_iso(plan["effective_world_time"])
        )
        if at <= previous:
            return
        beginning = max(previous, at - timedelta(days=7))
        if beginning > previous:
            ActionService._record_event(
                c,
                world_id=plan["world_id"],
                tick_id=str(uuid4()),
                occurred_at=at,
                event_type="life.routine_gap",
                actor_id=plan["character_id"],
                target_id=None,
                location_id=None,
                summary="较长时间跨度中的旧作息未逐项执行，不补造工作或收益。",
                payload={"from": to_iso(previous), "to": to_iso(beginning), "plan_id": plan["id"]},
            )
        offset = timedelta(minutes=spec.utc_offset_minutes)
        first = (beginning + offset).date() - timedelta(days=1)
        last = (at + offset).date()
        for day_index in range((last - first).days + 1):
            day = first + timedelta(days=day_index)
            for slot in spec.slots:
                if day.weekday() not in slot.weekdays:
                    continue
                hour, minute = map(int, slot.starts_at.split(":"))
                start = datetime.combine(day, time(hour, minute), tzinfo=UTC) - offset
                end = start + timedelta(minutes=slot.duration_minutes)
                if not beginning < end <= at:
                    continue
                inserted = c.execute(
                    "INSERT OR IGNORE INTO npc_routine_occurrences(id,plan_id,world_id,character_id,revision,slot_key,starts_world_time,ends_world_time,slot_json,required_minutes) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(uuid4()),
                        plan["id"],
                        plan["world_id"],
                        plan["character_id"],
                        plan["revision"],
                        slot.key,
                        to_iso(start),
                        to_iso(end),
                        slot.model_dump_json(),
                        cls.required_minutes(c, slot),
                    ),
                ).rowcount
                if inserted:
                    row = c.execute(
                        "SELECT * FROM npc_routine_occurrences WHERE plan_id=? AND revision=? AND slot_key=? AND starts_world_time=?",
                        (plan["id"], plan["revision"], slot.key, to_iso(start)),
                    ).fetchone()
                    cls.transition(
                        c, row, at, "missed", "时间已经跨过该时段，没有实际活动，不补发收益"
                    )
        c.execute(
            "INSERT INTO npc_routine_cursors VALUES (?,?,?) ON CONFLICT(plan_id) DO UPDATE SET revision=excluded.revision,last_world_time=excluded.last_world_time",
            (plan["id"], plan["revision"], to_iso(at)),
        )

    @classmethod
    def context(cls, c, actor, at):
        plan = c.execute(
            "SELECT * FROM npc_routine_plans WHERE character_id=?", (actor["id"],)
        ).fetchone()
        if not plan:
            return None
        spec = RoutinePlanSpec.model_validate_json(plan["spec_json"])
        if not spec.enabled:
            return None
        cls.record_skipped_windows(c, plan, spec, at)
        local = at + timedelta(minutes=spec.utc_offset_minutes)
        windows = []
        for delta in range(-1, 8):
            day = local.date() + timedelta(days=delta)
            for slot in spec.slots:
                if day.weekday() not in slot.weekdays:
                    continue
                hour, minute = map(int, slot.starts_at.split(":"))
                start = datetime.combine(day, time(hour, minute), tzinfo=UTC) - timedelta(
                    minutes=spec.utc_offset_minutes
                )
                end = start + timedelta(minutes=slot.duration_minutes)
                if end <= at or end <= from_iso(plan["effective_world_time"]):
                    continue
                windows.append((start, end, slot))
        if not windows:
            return {
                "plan": plan,
                "spec": spec,
                "slot": None,
                "next_boundary": at + timedelta(hours=1),
                "changed": False,
            }
        start, end, slot = min(windows, key=lambda item: item[0])
        location = c.execute(
            "SELECT * FROM locations WHERE id=? AND world_id=?",
            (slot.location_id, actor["world_id"]),
        ).fetchone()
        travel = (
            ScheduleService.earliest_arrival(c, actor, location, at, include_activity=False) - at
            if location
            else timedelta(0)
        )
        departure = start - travel - timedelta(minutes=5)
        if at < departure:
            return {
                "plan": plan,
                "spec": spec,
                "slot": None,
                "next_boundary": departure,
                "changed": False,
            }
        required = cls.required_minutes(c, slot)
        inserted = c.execute(
            "INSERT OR IGNORE INTO npc_routine_occurrences(id,plan_id,world_id,character_id,revision,slot_key,starts_world_time,ends_world_time,slot_json,required_minutes) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                plan["id"],
                actor["world_id"],
                actor["id"],
                plan["revision"],
                slot.key,
                to_iso(start),
                to_iso(end),
                slot.model_dump_json(),
                required,
            ),
        ).rowcount
        occurrence = c.execute(
            "SELECT * FROM npc_routine_occurrences WHERE plan_id=? AND revision=? AND slot_key=? AND starts_world_time=?",
            (plan["id"], plan["revision"], slot.key, to_iso(start)),
        ).fetchone()
        retry = (
            from_iso(occurrence["next_attempt_world_time"])
            if occurrence["next_attempt_world_time"]
            else None
        )
        return {
            "plan": plan,
            "spec": spec,
            "slot": slot,
            "occurrence": occurrence,
            "location": location,
            "start": start,
            "end": end,
            "next_boundary": min(
                [end] + ([start] if at < start else []) + ([retry] if retry and retry > at else [])
            ),
            "changed": bool(inserted) or bool(retry and at >= retry),
        }

    @classmethod
    def choose_workplace(cls, c, actor, context, at, appointment=None):
        from world_engine.actions import ActionService

        slot, spec, plan = context["slot"], context["spec"], context["plan"]
        candidates = []
        for loc in c.execute(
            "SELECT l.*,a.wage,a.balance FROM locations l LEFT JOIN workplace_accounts a ON a.location_id=l.id WHERE l.world_id=? AND l.is_active=1 AND (l.kind='workplace' OR a.location_id IS NOT NULL)",
            (actor["world_id"],),
        ):
            if loc["balance"] is not None and loc["balance"] < loc["wage"]:
                continue
            if loc["id"] != slot.location_id and (
                not spec.allow_alternative_work
                or great_circle_distance_km(
                    actor["longitude"], actor["latitude"], loc["longitude"], loc["latitude"]
                )
                > spec.local_search_radius_km
            ):
                continue
            deadline = cls.location_deadline(actor, loc, context["end"], appointment)
            if (
                max(context["start"], ScheduleService.earliest_arrival(c, actor, loc, at))
                + timedelta(minutes=60)
                > deadline
            ):
                continue
            candidates.append(loc)
        if not candidates:
            return None
        day = (at + timedelta(minutes=spec.utc_offset_minutes)).date().isoformat()
        scope = f"{plan['revision']}:{slot.key}"
        previous = c.execute(
            "SELECT * FROM npc_work_preferences WHERE character_id=?", (actor["id"],)
        ).fetchone()
        chosen = next(
            (
                loc
                for loc in candidates
                if previous
                and previous["scope_key"] == scope
                and previous["selected_world_day"] == day
                and loc["id"] == previous["location_id"]
            ),
            None,
        )
        if chosen is None:
            chosen = next(
                (loc for loc in candidates if loc["id"] == slot.location_id), None
            ) or min(
                candidates,
                key=lambda loc: great_circle_distance_km(
                    actor["longitude"], actor["latitude"], loc["longitude"], loc["latitude"]
                ),
            )
        reason = (
            "继续熟悉的工作场所"
            if chosen["id"] == slot.location_id
            else "原场所条件不足，选择可抵达且有报酬来源的替代工作"
        )
        source = previous["source_event_id"] if previous else None
        if (
            not previous
            or previous["location_id"] != chosen["id"]
            or previous["scope_key"] != scope
        ):
            source = ActionService._record_event(
                c,
                world_id=actor["world_id"],
                tick_id=str(uuid4()),
                occurred_at=at,
                event_type="life.workplace_chosen",
                actor_id=actor["id"],
                target_id=None,
                location_id=chosen["id"],
                summary=reason,
                payload={
                    "plan_id": plan["id"],
                    "preferred_location_id": slot.location_id,
                    "chosen_location_id": chosen["id"],
                },
            )
        c.execute(
            "INSERT INTO npc_work_preferences VALUES (?,?,?,?,?,?,?) ON CONFLICT(character_id) DO UPDATE SET plan_id=excluded.plan_id,scope_key=excluded.scope_key,location_id=excluded.location_id,selected_world_day=excluded.selected_world_day,reason=excluded.reason,source_event_id=excluded.source_event_id",
            (actor["id"], plan["id"], scope, chosen["id"], day, reason, source),
        )
        return chosen

    @staticmethod
    def location_deadline(actor, location, end, appointment):
        if appointment is None:
            return end
        contract = appointment["contract"]
        start = (
            from_iso(contract["starts_world_time"])
            if contract["starts_world_time"]
            else from_iso(contract["due_world_time"]) - timedelta(hours=1)
        )
        returning = timedelta(
            hours=great_circle_distance_km(
                location["longitude"],
                location["latitude"],
                contract["longitude"],
                contract["latitude"],
            )
            / max(1, actor["movement_speed_kmh"])
        )
        return min(end, start - returning - timedelta(minutes=10))

    @classmethod
    def defer(cls, c, context, at, reason):
        if (
            context
            and context.get("occurrence")
            and context["occurrence"]["status"] in {"planned", "active", "travelling", "deferred"}
        ):
            cls.transition(
                c,
                context["occurrence"],
                at,
                "deferred",
                reason,
                min(context["end"], at + timedelta(minutes=15)),
            )

    @classmethod
    def perform(cls, c, actor, at, context, appointment):
        from world_engine.daily_life import DailyLifeService

        if context["slot"] is None:
            return "按自己的作息自由安排"
        row, slot, spec = context["occurrence"], context["slot"], context["spec"]
        if row["status"] not in {"planned", "travelling", "active", "deferred"}:
            return "本时段的安排已经结束"
        supply = c.execute(
            "SELECT world_time FROM npc_routine_supplies WHERE occurrence_id=? ORDER BY world_time DESC LIMIT 1",
            (row["id"],),
        ).fetchone()
        if supply and at < from_iso(supply[0]) + timedelta(minutes=15):
            return "正在整理刚取得的材料或工具"
        done = cls.completed_minutes(c, row["id"])
        if done >= row["required_minutes"]:
            cls.transition(c, row, at, "completed", "已完成本时段约定的实际活动量")
            return "已完成作息安排"
        if (
            slot.kind == "work"
            and spec.goal == "maintain_reserve"
            and actor["money"] >= spec.income_reserve
        ):
            cls.transition(c, row, at, "skipped", "当前生活储备已足够，今天不再额外补工")
            return "储备已足够，留出个人时间"
        location = (
            cls.choose_workplace(c, actor, context, at, appointment)
            if slot.kind == "work"
            else context["location"]
        )
        if location is None or not location["is_active"]:
            cls.defer(c, context, at, "场所、工资预算或通勤时间条件暂不满足")
            return "暂缓作息安排，等待条件变化"
        deadline = cls.location_deadline(actor, location, context["end"], appointment)
        if ScheduleService.earliest_arrival(c, actor, location, at) >= deadline:
            cls.defer(c, context, at, "通勤会占用时段或赴约时间，暂不离开")
            return "留在附近准备后续安排"
        distance = great_circle_distance_km(
            actor["longitude"], actor["latitude"], location["longitude"], location["latitude"]
        )
        different_place = (actor["current_location_id"] or actor["location_id"]) != location["id"]
        if (actor["current_room_id"] and slot.kind != "rest") or distance > 0.1 or different_place:
            if not actor["current_room_id"] and distance < 0.05:
                cls.defer(c, context, at, "当前位置与作息地点归属不一致，需要先核对近邻地点")
                return "暂缓安排，避免在错误场所结算"
            intention = DailyLifeService._travel(c, actor, at, location)
            cls.transition(c, row, at, "travelling", "正在按真实路线前往作息地点")
            return intention
        if at < context["start"]:
            cls.transition(c, row, at, "deferred", "已到地点，等待时段开始", context["start"])
            return "已到地点，等待作息开始"
        free = max(0, int((deadline - at).total_seconds() // 60))
        planned = ScheduleService.planned_minutes(appointment, at)
        if planned is not None:
            free = min(free, planned)
        if slot.kind != "rest":
            free = min(free, cls.work_remaining(c, actor, spec, at))
        recipe = (
            c.execute(
                "SELECT spec_json FROM activity_recipes WHERE id=?", (slot.recipe_id,)
            ).fetchone()
            if slot.kind == "craft"
            else None
        )
        if slot.kind == "craft" and recipe is None:
            cls.defer(c, context, at, "所需配方已经不可用")
            return "暂缓制作，等待规则补齐"
        needed = (
            60
            if slot.kind == "work"
            else min(120, row["required_minutes"] - done, free)
            if slot.kind == "rest"
            else json.loads(recipe[0])["duration_minutes"]
        )
        if free < needed or needed <= 0:
            cls.defer(c, context, at, "剩余时间、赴约安排或今日活动额度不足")
            return "留出休息或赴约时间"
        if recipe:
            from world_engine.production import ProductionService

            preparation = ProductionService.prepare_routine(
                c, actor, at, context, json.loads(recipe[0]), free, needed
            )
            if preparation is not None:
                cls.defer(c, context, at, preparation)
                return preparation
        c.execute("SAVEPOINT routine_start_activity")
        try:
            if slot.kind == "rest":
                intention = DailyLifeService._rest_at_home(c, actor, at, needed)
            else:
                intention = DailyLifeService._start(
                    c, actor, at, slot.kind, recipe_id=slot.recipe_id
                )
        except ValueError as exc:
            c.execute("ROLLBACK TO SAVEPOINT routine_start_activity")
            c.execute("RELEASE SAVEPOINT routine_start_activity")
            cls.defer(c, context, at, str(exc))
            return "暂缓作息安排，当前条件不足"
        c.execute("RELEASE SAVEPOINT routine_start_activity")
        active = LifeActivityService.running(c, actor["id"])
        if active:
            c.execute(
                "INSERT OR IGNORE INTO npc_routine_activities VALUES (?,?)",
                (row["id"], active["id"]),
            )
            cls.transition(c, row, at, "active", "实际活动已开始，按世界时间结算")
        return intention

    @staticmethod
    def view(c, wid, cid, *, public_only=False):
        plan = c.execute(
            "SELECT * FROM npc_routine_plans WHERE world_id=? AND character_id=?", (wid, cid)
        ).fetchone()
        if not plan:
            return None
        spec = json.loads(plan["spec_json"])
        if public_only:
            if not spec["enabled"]:
                return None
            slots = [
                {
                    key: slot[key]
                    for key in (
                        "name",
                        "weekdays",
                        "starts_at",
                        "duration_minutes",
                        "kind",
                        "location_id",
                    )
                }
                for slot in spec["slots"]
                if slot["visibility"] == "public"
            ]
            return (
                {
                    "character_id": cid,
                    "utc_offset_minutes": spec["utc_offset_minutes"],
                    "slots": slots,
                }
                if slots
                else None
            )
        return {
            "id": plan["id"],
            "revision": plan["revision"],
            "spec": spec,
            "life_goals": NpcGoalService.view(c,cid),
            "life_goal_history": NpcGoalService.view(c,cid,history=True),
            "recent_occurrences": [
                dict(row)
                for row in c.execute(
                    "SELECT id,slot_key,starts_world_time,ends_world_time,status,reason,required_minutes FROM npc_routine_occurrences WHERE plan_id=? ORDER BY starts_world_time DESC LIMIT 12",
                    (plan["id"],),
                )
            ],
            "recent_supplies": [dict(entry) for entry in c.execute(
                "SELECT s.*,t.name FROM npc_routine_supplies s "
                "JOIN npc_routine_occurrences o ON o.id=s.occurrence_id "
                "JOIN item_types t ON t.id=s.item_type_id "
                "WHERE o.plan_id=? ORDER BY s.world_time DESC LIMIT 20", (plan["id"],)
            )],
            "work_preference": dict(row)
            if (
                row := c.execute(
                    "SELECT location_id,selected_world_day,reason FROM npc_work_preferences WHERE character_id=?",
                    (cid,),
                ).fetchone()
            )
            else None,
        }
