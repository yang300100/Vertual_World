"""NPC 的有依赖生活目标：只用真实工作、库存与规则行动推进。"""

import json
from datetime import timedelta
from typing import Literal
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from world_engine.repository import from_iso, to_iso

GOAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS npc_life_goals (
 id TEXT PRIMARY KEY,world_id TEXT NOT NULL REFERENCES worlds(id) ON DELETE CASCADE,
 character_id TEXT NOT NULL REFERENCES characters(id) ON DELETE CASCADE,
 plan_id TEXT NOT NULL REFERENCES npc_routine_plans(id) ON DELETE CASCADE,
 revision INTEGER NOT NULL,goal_key TEXT NOT NULL,spec_json TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'waiting',progress INTEGER NOT NULL DEFAULT 0,
 reason TEXT NOT NULL DEFAULT '',next_step_json TEXT NOT NULL DEFAULT '{}',
 retry_world_time TEXT,failures INTEGER NOT NULL DEFAULT 0,spent INTEGER NOT NULL DEFAULT 0,
 source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
 UNIQUE(plan_id,revision,goal_key)
);
CREATE TABLE IF NOT EXISTS npc_goal_steps (
 id TEXT PRIMARY KEY,goal_id TEXT NOT NULL REFERENCES npc_life_goals(id) ON DELETE CASCADE,
 world_time TEXT NOT NULL,kind TEXT NOT NULL,
 activity_id TEXT REFERENCES character_life_activities(id) ON DELETE SET NULL,
 source_event_id TEXT REFERENCES world_events(id) ON DELETE SET NULL,
 summary TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_npc_life_goals_actor ON npc_life_goals(character_id,status);
"""


class LifeGoalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    key: str = Field(min_length=1, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    title: str = Field(min_length=1, max_length=100)
    kind: Literal["reserve_money", "craft_stock"]
    target: int = Field(ge=1, le=100000, strict=True)
    priority: int = Field(default=5, ge=1, le=10, strict=True)
    depends_on: list[str] = Field(default_factory=list, max_length=7)
    recipe_id: str | None = None
    purchase_budget: int = Field(default=0, ge=0, le=100000, strict=True)
    work_location_ids: list[str] = Field(default_factory=list, max_length=8)
    deadline_world_time: AwareDatetime | None = None

    @model_validator(mode="after")
    def coherent(self):
        if (self.kind == "craft_stock") != bool(self.recipe_id):
            raise ValueError("制作库存目标必须指定配方，储蓄目标不能指定配方")
        if self.kind == "reserve_money" and self.purchase_budget:
            raise ValueError("储蓄目标不能设置物资采购预算")
        if len(set(self.depends_on)) != len(self.depends_on) or len(
            set(self.work_location_ids)
        ) != len(self.work_location_ids):
            raise ValueError("目标依赖和工作地点不能重复")
        return self


def validate_dependencies(goals):
    by_key = {goal.key: goal for goal in goals}
    if len(by_key) != len(goals):
        raise ValueError("生活目标标识不能重复")
    visited, visiting = set(), set()

    def visit(key):
        if key not in by_key:
            raise ValueError("生活目标依赖不存在")
        if key in visiting:
            raise ValueError("生活目标依赖不能形成循环")
        if key in visited:
            return
        visiting.add(key)
        for dependency in by_key[key].depends_on:
            visit(dependency)
        visiting.remove(key)
        visited.add(key)

    for key in by_key:
        visit(key)


class NpcGoalService:
    @staticmethod
    def register(c, wid, cid, plan_id, revision, specs, at):
        c.execute(
            "UPDATE npc_life_goals SET status='cancelled',reason='原作息与目标配置已修订，历史执行记录保留' WHERE plan_id=? AND status NOT IN ('completed','expired','cancelled')",
            (plan_id,),
        )
        for spec in specs:
            c.execute(
                "INSERT INTO npc_life_goals(id,world_id,character_id,plan_id,revision,goal_key,spec_json) VALUES (?,?,?,?,?,?,?)",
                (str(uuid4()), wid, cid, plan_id, revision, spec.key, spec.model_dump_json()),
            )
        c.execute(
            "UPDATE npc_daily_states SET next_world_time=?,last_slot=NULL WHERE character_id=?",
            (to_iso(at), cid),
        )

    @staticmethod
    def rows(c, cid, *, history=False):
        if history:
            return c.execute(
                "SELECT g.* FROM npc_life_goals g JOIN npc_routine_plans p ON p.id=g.plan_id WHERE g.character_id=? AND (g.revision!=p.revision OR json_extract(p.spec_json,'$.enabled')=0) ORDER BY g.rowid DESC LIMIT 16",
                (cid,),
            ).fetchall()
        return c.execute(
            "SELECT g.* FROM npc_life_goals g JOIN npc_routine_plans p ON p.id=g.plan_id "
            "WHERE g.character_id=? AND g.revision=p.revision AND json_extract(p.spec_json,'$.enabled')=1 ORDER BY g.goal_key",
            (cid,),
        ).fetchall()

    @classmethod
    def transition(cls, c, row, at, status, reason, next_step=None, retry=None, failed=False):
        from world_engine.actions import ActionService

        failures = row["failures"] + 1 if failed else 0
        next_step = next_step or {}
        event = row["source_event_id"]
        if (
            row["status"] != status
            or row["reason"] != reason
            or json.loads(row["next_step_json"]) != next_step
        ):
            event = ActionService._record_event(
                c,
                world_id=row["world_id"],
                tick_id=str(uuid4()),
                occurred_at=at,
                event_type="life.goal_plan_changed",
                actor_id=row["character_id"],
                target_id=None,
                location_id=None,
                summary="人物更新了自己的生活目标计划。",
                payload={"goal_id": row["id"], "information_scope": "private"},
            )
        c.execute(
            "UPDATE npc_life_goals SET status=?,reason=?,next_step_json=?,retry_world_time=?,failures=?,source_event_id=? WHERE id=?",
            (
                status,
                reason,
                json.dumps(next_step, ensure_ascii=False),
                to_iso(retry) if retry else None,
                failures,
                event,
                row["id"],
            ),
        )

    @classmethod
    def refresh(cls, c, actor, at):
        rows = cls.rows(c, actor["id"])
        for row in rows:
            if row["status"] in {"completed", "expired", "cancelled"}:
                continue
            goal = LifeGoalSpec.model_validate_json(row["spec_json"])
            if goal.deadline_world_time and at >= goal.deadline_world_time:
                cls.transition(c, row, at, "expired", "目标期限已到，未补造过去的完成结果")
                continue
            if goal.kind == "reserve_money":
                value = actor["money"]
            else:
                recipe = c.execute(
                    "SELECT spec_json FROM activity_recipes WHERE id=? AND world_id=?",
                    (goal.recipe_id, actor["world_id"]),
                ).fetchone()
                value = (
                    c.execute(
                        "SELECT COALESCE(SUM(quantity),0) FROM item_instances WHERE world_id=? AND item_type_id=? AND container_type='character_inventory' AND container_id=? AND owner_character_id=? AND condition>0 AND (spoils_world_time IS NULL OR spoils_world_time>?)",
                        (
                            actor["world_id"],
                            json.loads(recipe[0])["output_item_type_id"],
                            actor["id"],
                            actor["id"],
                            to_iso(at),
                        ),
                    ).fetchone()[0]
                    if recipe
                    else 0
                )
            c.execute("UPDATE npc_life_goals SET progress=? WHERE id=?", (max(0, value), row["id"]))
        # 依赖按实际完成状态逐层解锁；同一次刷新也能完成已经满足的后续目标。
        for _ in range(len(rows)):
            current = cls.rows(c, actor["id"])
            statuses = {row["goal_key"]: row["status"] for row in current}
            changed = False
            for row in current:
                goal = LifeGoalSpec.model_validate_json(row["spec_json"])
                if (
                    row["status"] not in {"completed", "expired", "cancelled"}
                    and row["progress"] >= goal.target
                    and all(statuses.get(key) == "completed" for key in goal.depends_on)
                ):
                    cls.transition(
                        c, row, at, "completed", "已由当前真实资金或本人随身库存满足目标"
                    )
                    changed = True
            if not changed:
                break

    @classmethod
    def block(cls, c, row, at, reason):
        minutes = min(360, 15 * 2 ** min(row["failures"], 5))
        cls.transition(
            c, row, at, "blocked", reason, retry=at + timedelta(minutes=minutes), failed=True
        )
        return reason

    @classmethod
    def step(cls, c, row, at, kind, summary, activity_id=None, event_id=None, spent=0):
        c.execute(
            "INSERT INTO npc_goal_steps VALUES (?,?,?,?,?,?,?)",
            (str(uuid4()), row["id"], to_iso(at), kind, activity_id, event_id, summary),
        )
        c.execute("UPDATE npc_life_goals SET spent=spent+? WHERE id=?", (spent, row["id"]))
        cls.transition(
            c,
            row,
            at,
            "active",
            summary,
            {"kind": kind, "activity_id": activity_id},
            at + timedelta(minutes=15),
        )
        return summary

    @staticmethod
    def travel(c, actor, at, location):
        from world_engine.daily_life import DailyLifeService

        c.execute("SAVEPOINT npc_goal_travel")
        try:
            result = DailyLifeService._travel(c, actor, at, location)
            c.execute("RELEASE SAVEPOINT npc_goal_travel")
            return result
        except Exception:
            c.execute("ROLLBACK TO SAVEPOINT npc_goal_travel")
            c.execute("RELEASE SAVEPOINT npc_goal_travel")
            raise

    @classmethod
    def perform(cls, c, actor, at, routine, appointment):
        from world_engine.activity_tasks import TaskService
        from world_engine.daily_life import DailyLifeService
        from world_engine.economy import EconomyService
        from world_engine.geo import great_circle_distance_km
        from world_engine.life import LifeActivityService
        from world_engine.routines import RoutineService
        from world_engine.schedules import ScheduleService

        cls.refresh(c, actor, at)
        rows = cls.rows(c, actor["id"])
        if not rows:
            return None
        if (
            LifeActivityService.running(c, actor["id"])
            or c.execute(
                "SELECT 1 FROM character_movements WHERE character_id=? AND status='moving'",
                (actor["id"],),
            ).fetchone()
        ):
            return "继续已经开始的目标活动或行程"
        statuses = {row["goal_key"]: row["status"] for row in rows}
        eligible = []
        for row in rows:
            goal = LifeGoalSpec.model_validate_json(row["spec_json"])
            if row["status"] in {"completed", "expired", "cancelled"}:
                continue
            if not all(statuses.get(key) == "completed" for key in goal.depends_on):
                cls.transition(c, row, at, "waiting", "等待前置目标真实完成")
                continue
            if row["retry_world_time"] and from_iso(row["retry_world_time"]) > at:
                continue
            eligible.append((row, goal))
        if not eligible:
            return "目标暂待条件变化，按自己的生活节奏安排"
        # 受阻目标退避后，其他可执行目标仍可得到机会。
        eligible.sort(
            key=lambda pair: (
                pair[0]["status"] == "blocked",
                -pair[1].priority,
                pair[0]["goal_key"],
            )
        )
        row, goal = eligible[0]
        spec = routine["spec"]
        deadline = routine["next_boundary"]
        if goal.deadline_world_time:
            deadline = min(deadline, goal.deadline_world_time)
        daily_remaining = RoutineService.work_remaining(c, actor, spec, at)
        if daily_remaining <= 0:
            return cls.block(c, row, at, "今日自动劳动额度已用完，留出休息时间")
        recipe = None
        if goal.kind == "craft_stock":
            recipe_row = c.execute(
                "SELECT spec_json FROM activity_recipes WHERE id=? AND world_id=?",
                (goal.recipe_id, actor["world_id"]),
            ).fetchone()
            if not recipe_row:
                return cls.block(c, row, at, "所需配方已不可用")
            recipe = json.loads(recipe_row[0])
            destination_id = recipe["location_id"]
        else:
            destination_id = None

        def work_destination():
            known = set(goal.work_location_ids) | {
                s.location_id for s in spec.slots if s.kind == "work"
            }
            places = []
            for lid in sorted(known):
                loc = c.execute(
                    "SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1",
                    (lid, actor["world_id"]),
                ).fetchone()
                if not loc:
                    continue
                account = c.execute(
                    "SELECT balance,wage FROM workplace_accounts WHERE location_id=?", (lid,)
                ).fetchone()
                if account and account["balance"] < account["wage"]:
                    continue
                if not account and loc["kind"] != "workplace":
                    continue
                distance = great_circle_distance_km(
                    actor["longitude"], actor["latitude"], loc["longitude"], loc["latitude"]
                )
                end = RoutineService.location_deadline(actor, loc, deadline, appointment)
                if (
                    distance <= spec.local_search_radius_km
                    and ScheduleService.earliest_arrival(c, actor, loc, at) + timedelta(minutes=60)
                    <= end
                ):
                    places.append((distance, lid, loc))
            return min(places, key=lambda item: (item[0], item[1]))[2] if places else None

        missing = TaskService.missing_supplies(c, actor, recipe, at) if recipe else []
        available_money = max(0, actor["money"] - spec.income_reserve)
        remaining_budget = max(0, goal.purchase_budget - row["spent"])
        prices = [
            price[0]
            for need in missing
            if (
                price := c.execute(
                    "SELECT price FROM world_item_profiles WHERE world_id=? AND item_type_id=?",
                    (actor["world_id"], need["item_type_id"]),
                ).fetchone()
            )
        ]
        must_fund = bool(
            recipe and any(available_money < price <= remaining_budget for price in prices)
        )
        location = (
            c.execute(
                "SELECT * FROM locations WHERE id=? AND world_id=? AND is_active=1",
                (destination_id, actor["world_id"]),
            ).fetchone()
            if recipe
            else work_destination()
        )
        minutes = recipe["duration_minutes"] if recipe else 60
        if must_fund:
            # 先留在配方地点尝试免费合法收取；确实买不起时才转去工作。
            at_recipe = (
                actor["current_location_id"] or actor["location_id"]
            ) == destination_id and not actor["current_room_id"]
            if not at_recipe:
                location = work_destination()
                minutes = 60
        if not location:
            return cls.block(c, row, at, "已知场所不可用、工资不足或无法在空闲时间抵达")
        end = RoutineService.location_deadline(actor, location, deadline, appointment)
        if (
            daily_remaining < minutes
            or ScheduleService.earliest_arrival(c, actor, location, at) + timedelta(minutes=minutes)
            > end
        ):
            return cls.block(c, row, at, "剩余时间不足，需要保留固定作息或赴约时间")
        distance = great_circle_distance_km(
            actor["longitude"], actor["latitude"], location["longitude"], location["latitude"]
        )
        if (
            actor["current_room_id"]
            or distance > 0.1
            or (actor["current_location_id"] or actor["location_id"]) != location["id"]
        ):
            if not actor["current_room_id"] and distance < 0.05:
                return cls.block(c, row, at, "近邻地点归属不一致，暂缓目标行程")
            try:
                result = cls.travel(c, actor, at, location)
            except ValueError as exc:
                return cls.block(c, row, at, str(exc))
            return cls.step(c, row, at, "travel", result)
        working = recipe is None or location["id"] != destination_id
        if not working and missing:
            if (end - at).total_seconds() < 60 * (minutes + 15):
                return cls.block(c, row, at, "来不及备料后完成工序")
            for need in missing:
                if actor["energy"] >= recipe["energy_cost"] + 3:
                    event = EconomyService.harvest(
                        c, actor, at, item_type_ids={need["item_type_id"]}, return_event=True
                    )
                    if event:
                        return cls.step(
                            c, row, at, "harvest", "为生活目标取得了一份真实物资", event_id=event
                        )
                purchase = EconomyService.buy_supply(
                    c,
                    actor,
                    at,
                    item_type_id=need["item_type_id"],
                    minimum_condition=need["minimum_condition"],
                    budget=min(
                        max(0, goal.purchase_budget - row["spent"]),
                        max(0, actor["money"] - spec.income_reserve),
                    ),
                )
                if purchase:
                    return cls.step(
                        c,
                        row,
                        at,
                        "purchase",
                        "为生活目标购入了一份物资",
                        event_id=purchase["source_event_id"],
                        spent=purchase["spent"],
                    )
            if must_fund:
                alternative = work_destination()
                if alternative and daily_remaining >= 60:
                    if alternative["id"] == location["id"]:
                        working = True
                    else:
                        try:
                            result = cls.travel(c, actor, at, alternative)
                        except ValueError as exc:
                            return cls.block(c, row, at, str(exc))
                        return cls.step(c, row, at, "travel", result + "，先补足备料资金")
            if not working:
                return cls.block(c, row, at, "缺少合法物资来源、背包空间或足够采购预算")
        c.execute("SAVEPOINT npc_goal_start")
        try:
            result = DailyLifeService._start(
                c,
                actor,
                at,
                "work" if working else "craft",
                recipe_id=None if working else goal.recipe_id,
            )
        except ValueError as exc:
            c.execute("ROLLBACK TO SAVEPOINT npc_goal_start")
            c.execute("RELEASE SAVEPOINT npc_goal_start")
            return cls.block(c, row, at, str(exc))
        c.execute("RELEASE SAVEPOINT npc_goal_start")
        active = LifeActivityService.running(c, actor["id"])
        return cls.step(
            c,
            row,
            at,
            "work" if working else "craft",
            result,
            active["id"],
            active["source_event_id"],
        )

    @classmethod
    def view(cls, c, cid, *, history=False):
        return [
            {
                "id": row["id"],
                "goal": json.loads(row["spec_json"]),
                "status": row["status"],
                "progress": row["progress"],
                "reason": row["reason"],
                "next_step": json.loads(row["next_step_json"]),
                "retry_world_time": row["retry_world_time"],
                "spent": row["spent"],
                "recent_steps": [
                    dict(step)
                    for step in c.execute(
                        "SELECT world_time,kind,activity_id,summary FROM npc_goal_steps WHERE goal_id=? ORDER BY world_time DESC,rowid DESC LIMIT 6",
                        (row["id"],),
                    )
                ],
            }
            for row in cls.rows(c, cid, history=history)
        ]
